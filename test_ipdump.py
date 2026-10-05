import json
import signal
from urllib.parse import parse_qs, urlparse

import pytest
from instapaper import Instapaper
from instapaper._transport import HTTPResponse
from instapaper.errors import InstapaperConnectionError

import ipdump

T2014 = 1393669320  # 2014-03-01T10:22:00Z


def bm(id, title, **kw):
    return {"id": id, "title": title, "url": f"https://ex.com/{id}", "time": T2014, "liked": False,
            "archived": False, "folder_id": None, "tags": [], "progress": {"percentage": 0}} | kw


class FakeAPI:
    """Serves a list of changed bookmarks paged by offset, plus folders and parse."""

    def __init__(self, bookmarks, deleted=(), folders=()):
        self.items = [("b", b) for b in bookmarks] + [("d", d) for d in deleted]
        self.folders, self.offsets, self.statuses, self.fail_at = list(folders), [], [], None

    def __call__(self, req):
        if self.statuses:
            return HTTPResponse(self.statuses.pop(0), b"{}", {"Retry-After": "7"})
        url = urlparse(req.url)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/2/folders":
            body = {"folders": self.folders}
        elif url.path.endswith("/parse"):
            bid = url.path.split("/")[-2]
            body = {"metadata": {"author": {"name": "Ann"}},
                    "content": {"body": f'<h1>A{bid}</h1><p><img src="/pic{bid}.jpg"></p>', "images": [], "words": 3}}
        else:
            off = int(q["offset"])
            if off == self.fail_at:
                raise InstapaperConnectionError("boom")
            self.offsets.append(off)
            chunk = self.items[off:off + int(q["limit"])]
            body = {"bookmarks": [x for k, x in chunk if k == "b"], "deleted_ids": [x for k, x in chunk if k == "d"]}
        return HTTPResponse(200, json.dumps(body).encode())


@pytest.fixture
def env(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(ipdump.time, "sleep", sleeps.append)
    monkeypatch.setattr(ipdump, "PAGE", 2)

    def fake_fetch(data, url):
        name = "abc.jpg"
        ipdump.image_path(data, name).parent.mkdir(parents=True, exist_ok=True)
        ipdump.image_path(data, name).write_bytes(b"jpg")
        return name, None

    monkeypatch.setattr(ipdump, "fetch_image", fake_fetch)
    db = ipdump.open_db(tmp_path / "data")

    def run(api, **kw):
        t = ipdump.Throttle((0, 0), send=api)
        return ipdump.sync(db, tmp_path / "data", Instapaper("tok", transport=t), t, **kw)

    return db, run, sleeps, tmp_path


def test_sync_diff_delete_resume(env, capsys):
    db, run, _, _ = env
    api = FakeAPI([bm(1, "One"), bm(2, "Two"), bm(3, "Three")])
    c = run(api)
    assert (c["new"], c["articles"], c["images"]) == (3, 3, 3)
    assert api.offsets == [0, 2]

    api = FakeAPI([bm(1, "One", liked=True)], deleted=[2])
    capsys.readouterr()
    c = run(api)
    out = capsys.readouterr().out
    assert '~ 1 "One": liked false→true' in out
    assert (c["changed"], c["deleted"]) == (1, 1)
    assert db.execute("select deleted_at from bookmarks where id=2").fetchone()[0]

    # interrupted mid-paging resumes at the saved offset
    api = FakeAPI([bm(4, "Four"), bm(5, "Five"), bm(6, "Six")])
    api.fail_at = 2
    with pytest.raises(InstapaperConnectionError):
        run(api)
    api.fail_at, api.offsets = None, []
    run(api)
    assert api.offsets == [2]
    assert db.execute("select count(*) from bookmarks").fetchone()[0] == 6


def test_rate_limit_retry(env):
    _, run, sleeps, _ = env
    api = FakeAPI([bm(1, "One")])
    api.statuses = [429, 503]
    run(api)
    assert 7.0 in sleeps


def test_export(env):
    db, run, _, tmp = env
    tech = [{"id": 9, "title": "Tech"}]
    run(FakeAPI([bm(1, "My: Title?", liked=True, folder_id=9), bm(2, "Gone"), bm(3, "No text")], folders=tech))
    run(FakeAPI([], deleted=[2], folders=tech))
    db.execute("delete from content where bookmark_id = 3")  # as if not fetched yet
    out = tmp / "export"
    assert ipdump.export(db, tmp / "data", out) == 4  # 2 md + 2 images

    name = "2014-03-01t10-22-00_my-title"
    for d in ("tech/2014", "liked/2014"):
        md = (out / d / f"{name}.md").read_text()
        assert f"![]({name}_files/abc.jpg)" in md
        assert '  abc.jpg: "https://ex.com/pic1.jpg"' in md
        assert 'folder: "Tech"' in md and 'author: "Ann"' in md
        assert (out / d / f"{name}_files/abc.jpg").read_bytes() == b"jpg"
    assert not list(out.rglob("*gone*"))
    assert not list(out.rglob("*no-text*"))

    mtimes = {p: p.stat().st_mtime_ns for p in out.rglob("*") if p.is_file() and p.name != ".ipdump-manifest"}
    assert ipdump.export(db, tmp / "data", out) == 0
    assert mtimes == {p: p.stat().st_mtime_ns for p in mtimes}


def test_ctrl_c_keeps_progress(env, monkeypatch, capsys):
    db, run, _, tmp = env
    api = FakeAPI([bm(1, "One"), bm(2, "Two"), bm(3, "Three")])
    send = api.__call__
    calls = []

    def interrupt_second_parse(req):
        if req.url.split("?")[0].endswith("/parse"):
            calls.append(req.url)
            if len(calls) == 2:
                raise KeyboardInterrupt
        return send(req)

    with pytest.raises(KeyboardInterrupt):
        run(interrupt_second_parse)
    assert "+3 new" in capsys.readouterr().out  # summary still printed
    assert db.execute("select count(*) from content").fetchone()[0] == 1
    assert run(api)["articles"] == 2  # resumes with the rest

    out = tmp / "export"
    real = ipdump.atomic_write
    n = []

    def interrupt_third_write(path, payload):
        n.append(path)
        if len(n) == 3:
            raise KeyboardInterrupt
        real(path, payload)

    monkeypatch.setattr(ipdump, "atomic_write", interrupt_third_write)
    with pytest.raises(KeyboardInterrupt):
        ipdump.export(db, tmp / "data", out)
    monkeypatch.setattr(ipdump, "atomic_write", real)
    assert len(json.loads((out / ".ipdump-manifest").read_text())) == 2
    assert not list(out.rglob("*.part"))
    assert ipdump.export(db, tmp / "data", out) == 4  # 3 md + 1 image left, done ones skipped


def test_ctrl_c_finishes_current_article(env):
    db, run, _, _ = env
    api = FakeAPI([bm(1, "One"), bm(2, "Two"), bm(3, "Three")])
    parses = []

    def ctrl_c_during_second_parse(req):
        if req.url.split("?")[0].endswith("/parse"):
            parses.append(req.url)
            if len(parses) == 2:
                signal.raise_signal(signal.SIGINT)
        return api(req)

    with pytest.raises(KeyboardInterrupt):
        run(ctrl_c_during_second_parse)
    assert len(parses) == 2  # third article never started
    assert db.execute("select count(*) from content").fetchone()[0] == 2
    assert db.execute("select count(*) from images").fetchone()[0] == 2  # second article's image too
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_main_ctrl_c_exit_code(tmp_path, monkeypatch):
    def boom(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr(ipdump, "export", boom)
    with pytest.raises(SystemExit) as e:
        ipdump.main(["export", "--data", str(tmp_path)])
    assert e.value.code == 130


def test_safe():
    assert ipdump.safe('  a/b: "Árvíztűrő"  Tükör ') == "a-b-arvizturo-tukor"
    assert ipdump.safe("Straße_Øl — Łódź?!") == "strasse-ol-lodz"
    assert ipdump.safe("...") == "untitled"


def test_language():
    assert ipdump.language("The quick brown fox jumps over the lazy dog and runs into the forest.") == "en"
    assert ipdump.language("Árvíztűrő tükörfúrógép, a magyar nyelv nagyon szép és gazdag.") == "hu"
    assert ipdump.language("Der schnelle braune Fuchs springt über den faulen Hund.") == "de"
    assert ipdump.language("Le renard brun rapide saute par-dessus le chien paresseux.") == "fr"
    assert ipdump.language(" 1234 ") is None
