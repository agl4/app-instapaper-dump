import json
import signal
from email.message import Message
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
    """Serves the change feed (paged by offset), section lists, folders and parse."""

    def __init__(self, bookmarks, deleted=(), folders=(), lists=None):
        self.items = [("b", b) for b in bookmarks] + [("d", d) for d in deleted]
        self.folders, self.offsets, self.statuses, self.fail_at = list(folders), [], [], None
        self.lists, self.list_calls, self.list_fail_at = lists or {}, [], None  # {"home": [...], "9": [...]}

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
        elif "since" not in q:
            key = q.get("folder_id") or q.get("section", "home")
            off, items = int(q["offset"]), self.lists.get(key, [])
            if (key, off) == self.list_fail_at:
                raise InstapaperConnectionError("boom")
            self.list_calls.append((key, off))
            body = {"bookmarks": items[off:off + int(q["limit"])], "total": len(items)}
        else:
            off = int(q["offset"])
            if off == self.fail_at:
                raise InstapaperConnectionError("boom")
            self.offsets.append(off)
            chunk = self.items[off:off + int(q["limit"])]
            body = {"bookmarks": [x for k, x in chunk if k == "b"], "deleted_ids": [x for k, x in chunk if k == "d"]}
        return HTTPResponse(200, json.dumps(body).encode())


def fake_article(html, url, words=200):
    bid = url.split("/")[-1]
    return {"metadata": {"author": {"name": "Ann"}}, "source": "web",
            "content": {"body": f'<h1>A{bid}</h1><p><img src="/pic{bid}.jpg"></p>', "images": [], "words": words}}


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
    monkeypatch.setattr(ipdump, "fetch_page", lambda url: (url.encode(), url))
    monkeypatch.setattr(ipdump, "clean_article", fake_article)
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


def test_full_import_reads_every_list_and_resumes(env, capsys):
    db, run, _, _ = env
    lists = {"home": [bm(1, "H1"), bm(2, "H2"), bm(3, "H3")],
             "archive": [bm(4, "A", archived=True)],
             "9": [bm(5, "F", folder_id=9, liked=True)]}
    api = FakeAPI([], folders=[{"id": 9, "title": "Tech"}], lists=lists)
    api.list_fail_at = ("home", 2)
    with pytest.raises(InstapaperConnectionError):
        run(api, max_articles=0, full=True)
    api.list_fail_at, api.list_calls = None, []
    run(api, max_articles=0)
    assert api.list_calls == []  # optional: a plain sync leaves the unfinished import alone
    c = run(api, max_articles=1, full=True)
    assert api.list_calls == [("home", 2), ("archive", 0), ("9", 0)]  # resumed mid-home
    assert db.execute("select count(*) from bookmarks").fetchone()[0] == 5
    # the failed first run still downloaded the known links 1, 2; now liked 5 goes before 3 and 4
    assert c["articles"] == 1
    assert [r[0] for r in db.execute("select bookmark_id from content order by 1")] == [1, 2, 5]
    out = capsys.readouterr().out
    assert "  folder 'Tech': 1/1" in out and "+ 1 " not in out  # progress per page, no line per bookmark

    api.list_calls = []
    run(api, max_articles=0)
    assert api.list_calls == []
    run(api, max_articles=0, full=True)
    assert api.list_calls == [("home", 0), ("home", 2), ("archive", 0), ("9", 0)]  # finished, so it starts over


def test_connection_reset_is_retried(env, capsys):
    db, run, sleeps, _ = env
    api = FakeAPI([bm(1, "One")])
    resets = [1, 1]

    def flaky(req):
        if resets:
            resets.pop()
            raise InstapaperConnectionError("Could not reach Instapaper: reset")
        return api(req)

    run(flaky)
    assert "connection problem" in capsys.readouterr().out
    assert sleeps.count(10) == 1 and sleeps.count(20) == 1  # backoff 10s, 20s
    assert db.execute("select count(*) from bookmarks").fetchone()[0] == 1


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
    page = ipdump.fetch_page
    calls = []

    def interrupt_second_page(url):
        calls.append(url)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return page(url)

    monkeypatch.setattr(ipdump, "fetch_page", interrupt_second_page)
    with pytest.raises(KeyboardInterrupt):
        run(api)
    monkeypatch.setattr(ipdump, "fetch_page", page)
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


def test_ctrl_c_finishes_current_article(env, monkeypatch):
    db, run, _, _ = env
    api = FakeAPI([bm(1, "One"), bm(2, "Two"), bm(3, "Three")])
    page = ipdump.fetch_page
    pages = []

    def ctrl_c_during_second_page(url):
        pages.append(url)
        if len(pages) == 2:
            signal.raise_signal(signal.SIGINT)
        return page(url)

    monkeypatch.setattr(ipdump, "fetch_page", ctrl_c_during_second_page)
    with pytest.raises(KeyboardInterrupt):
        run(api)
    assert len(pages) == 2  # third article never started
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


def test_sync_and_export_agree_on_images(tmp_path):
    body = '<img data-src="/lazy.jpg" src="/real.jpg"><img src="/q?a=1&amp;copy=2">'
    parsed = {"content": {"body": body, "images": []}}
    b = bm(1, "T")
    urls = ipdump.article_images(parsed, b["url"])
    assert urls == ["https://ex.com/real.jpg", "https://ex.com/q?a=1&copy=2"]
    text, images = ipdump.render(b, parsed, "s", {}, {u: f"{i}.jpg" for i, u in enumerate(urls)})
    assert images == {"0.jpg": urls[0], "1.jpg": urls[1]} and "s_files/1.jpg" in text


def test_transient_image_failures_are_retried(env, monkeypatch):
    db, run, _, tmp_path = env
    run(FakeAPI([bm(1, "A"), bm(2, "B"), bm(3, "C")]))
    db.execute("update images set file = null, error = 'timed out' where url like '%pic1%'")
    db.execute("update images set file = null, error = 'HTTP Error 404: Not Found' where url like '%pic2%'")
    db.execute("update images set file = null, error = 'not an image (text/html)' where url like '%pic3%'")
    fetched = []
    monkeypatch.setattr(ipdump, "fetch_image", lambda data, url: fetched.append(url) or ("x.jpg", None))
    ipdump.sync_missing_images(db, tmp_path / "data", ipdump.Counter())
    assert fetched == ["https://ex.com/pic1.jpg"]


def fake_response(ctype, body=b"<html>login</html>", url="https://ex.com/final"):
    class R:
        headers = Message()
        headers["Content-Type"] = ctype
        __enter__ = lambda self: self
        __exit__ = lambda self, *a: None
        read = lambda self, size=-1: body
        geturl = lambda self: url

    return R()


def test_fetch_image_rejects_html(tmp_path, monkeypatch):
    monkeypatch.setattr(ipdump.time, "sleep", lambda s: None)
    monkeypatch.setattr(ipdump.urllib.request, "urlopen", lambda *a, **kw: fake_response("text/html; charset=utf-8"))
    assert ipdump.fetch_image(tmp_path, "https://ex.com/a.jpg") == (None, "not an image (text/html)")


ARTICLE = """<html><head><title>T</title></head><body>
<nav><a href="/home">Home</a> <a href="/about">About us</a></nav>
<article><h1>The real title</h1>
<p>%s</p><p><img src="/pics/a.jpg" alt="a"></p><p>See <a href="/more">more of this</a> over here, it is worth reading.</p></article>
<footer>Copyright footer text</footer></body></html>""" % ("A sentence of real article text goes right here. " * 40)


def test_clean_article_keeps_the_article_only():
    parsed = ipdump.clean_article(ARTICLE.encode(), "https://ex.com/a/b")
    body = parsed["content"]["body"]
    assert "real article text" in body and "<html" not in body
    assert "Copyright footer" not in body and "About us" not in body
    assert 'src="https://ex.com/pics/a.jpg"' in body and 'href="https://ex.com/more"' in body
    assert parsed["content"]["words"] > ipdump.MIN_WORDS and parsed["source"] == "web"
    with pytest.raises(ValueError):
        ipdump.clean_article(b"<html><body></body></html>", "https://ex.com/")


def test_clean_article_unwraps_layout_tables_only():
    words = "A sentence of real article text goes right here. " * 40
    html = f"""<html><body><table><tr><td><article><h1>Title</h1><p>{words}</p>
    <table><tr><th>Year</th><th>Sales</th></tr><tr><td>2020</td><td>15</td></tr></table>
    <p>{words}</p></article></td></tr></table></body></html>"""
    body = ipdump.clean_article(html.encode(), "https://ex.com/")["content"]["body"]
    assert body.count("<table") == 1 and "<th>Year</th>" in body


def test_incomplete_reason():
    assert ipdump.incomplete_reason(b"<html>", 500) is None
    assert ipdump.incomplete_reason(b"<html>", 20) == "too short (20 words)"
    assert ipdump.incomplete_reason(b'{"isAccessibleForFree": "False"}', 500) == "paywalled"
    assert ipdump.incomplete_reason(b'{"isAccessibleForFree":true}', 500) is None


def parse_calls(api):
    calls = []

    def send(req):
        if urlparse(req.url).path.endswith("/parse"):
            calls.append(req.url)
        return api(req)

    return send, calls


def test_good_web_article_never_asks_instapaper(env, capsys):
    db, run, _, _ = env
    send, calls = parse_calls(FakeAPI([bm(1, "One")]))
    c = run(send)
    assert calls == [] and (c["articles"], c["api"]) == (1, 0)
    assert json.loads(db.execute("select json from content").fetchone()[0])["source"] == "web"
    assert "1 articles from the web, 0 from Instapaper" in capsys.readouterr().out


def test_failed_download_falls_back_to_instapaper(env, monkeypatch):
    db, run, _, tmp = env

    def gone(url):
        raise ValueError("HTTP Error 404: Not Found")

    monkeypatch.setattr(ipdump, "fetch_page", gone)
    send, calls = parse_calls(FakeAPI([bm(1, "One")]))
    c = run(send)
    assert len(calls) == 1 and (c["articles"], c["api"]) == (0, 1)
    assert db.execute("select error from content").fetchone()[0] is None
    assert "source" not in json.loads(db.execute("select json from content").fetchone()[0])
    assert ipdump.export(db, tmp / "data", tmp / "export") == 2  # md + image


def test_stub_replaced_only_by_a_longer_copy(env, monkeypatch):
    db, run, _, _ = env
    # FakeAPI's parse says 3 words
    monkeypatch.setattr(ipdump, "clean_article", lambda h, u: fake_article(h, u, words=2))
    run(FakeAPI([bm(1, "Shorter than Instapaper")]))
    row = db.execute("select json, error from content where bookmark_id = 1").fetchone()
    assert "source" not in json.loads(row[0]) and row[1] is None

    # a stub is flagged by its word count, here with an Instapaper copy that is no better
    monkeypatch.setattr(ipdump, "clean_article", lambda h, u: fake_article(h, u, words=100))
    run(FakeAPI([bm(2, "Longer than Instapaper")]))
    row = db.execute("select json, error from content where bookmark_id = 2").fetchone()
    assert json.loads(row[0])["source"] == "web" and row[1] is None


def test_stub_kept_when_instapaper_has_no_copy(env, monkeypatch):
    db, run, _, tmp = env
    monkeypatch.setattr(ipdump, "clean_article", lambda h, u: fake_article(h, u, words=20))
    api = FakeAPI([bm(1, "One")])

    def no_copy(req):
        if urlparse(req.url).path.endswith("/parse"):
            return HTTPResponse(404, b"{}")
        return api(req)

    run(no_copy)
    row = db.execute("select json, error from content").fetchone()
    assert json.loads(row[0])["source"] == "web" and row[1].startswith("instapaper: ")
    assert ipdump.export(db, tmp / "data", tmp / "export") == 2
    # final: neither the web nor Instapaper is asked again
    send, calls = parse_calls(FakeAPI([]))
    assert run(send)["articles"] == 0 and calls == []


def test_rate_limit_stops_instapaper_pass_and_resumes_without_redownload(env, monkeypatch):
    db, run, _, _ = env
    pages = []

    def gone(url):
        pages.append(url)
        raise ValueError("blocked")

    monkeypatch.setattr(ipdump, "fetch_page", gone)
    api = FakeAPI([bm(1, "One"), bm(2, "Two")])

    def limited(req):
        if urlparse(req.url).path.endswith("/parse"):
            return HTTPResponse(429, b"{}")
        return api(req)

    assert run(limited)["api"] == 0
    assert len(pages) == 2
    assert db.execute("select count(*) from content where error like 'web:%'").fetchone()[0] == 2

    assert run(FakeAPI([]))["api"] == 2
    assert len(pages) == 2  # pages not downloaded again
    assert db.execute("select count(*) from content where error is null").fetchone()[0] == 2


def test_metadata_error_still_downloads_but_skips_instapaper(env, monkeypatch):
    db, run, _, _ = env
    run(FakeAPI([bm(1, "One")]), max_articles=1)
    db.execute("delete from content")
    db.commit()

    def gone(url):
        raise ValueError("blocked")

    calls = []
    api = FakeAPI([bm(2, "Two")])
    api.fail_at = 0

    def send(req):
        if urlparse(req.url).path.endswith("/parse"):
            calls.append(req.url)
        return api(req)

    with pytest.raises(InstapaperConnectionError):
        run(send)
    assert db.execute("select count(*) from content").fetchone()[0] == 1  # known link downloaded
    db.execute("delete from content")
    db.commit()

    monkeypatch.setattr(ipdump, "fetch_page", gone)
    with pytest.raises(InstapaperConnectionError):
        run(send)
    assert calls == []  # flagged, but the Instapaper pass didn't run


def test_fetch_page_rejects_what_it_should_and_waits(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ipdump.time, "sleep", sleeps.append)
    seen = []

    def urlopen(request, timeout):
        seen.append(request.full_url)
        return fake_response(urlopen.ctype)

    monkeypatch.setattr(ipdump.urllib.request, "urlopen", urlopen)
    for bad in ("file:///etc/passwd", "instapaper-private://email/1", "", None):
        with pytest.raises(ValueError, match="not a web URL"):
            ipdump.fetch_page(bad)
    assert seen == [] and sleeps == []

    urlopen.ctype = "application/pdf"
    with pytest.raises(ValueError, match="not html"):
        ipdump.fetch_page("https://ex.com/a.pdf")
    urlopen.ctype = "text/html; charset=utf-8"
    assert ipdump.fetch_page("https://ex.com/a") == (b"<html>login</html>", "https://ex.com/final")
    assert len(sleeps) == 2 and all(1 <= s <= 3 for s in sleeps)


def test_fetch_image_waits(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(ipdump.time, "sleep", sleeps.append)
    monkeypatch.setattr(ipdump.urllib.request, "urlopen", lambda *a, **kw: fake_response("image/png", b"png"))
    ipdump.fetch_image(tmp_path, "https://ex.com/a.png")
    assert len(sleeps) == 1 and 0.05 <= sleeps[0] <= 0.25
