"""ipdump: incremental Instapaper mirror -> plain-Markdown export. See SPEC.md."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from instapaper import Instapaper
from instapaper._transport import decode_body, urllib_transport
from instapaper.errors import (
    BadRequestError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    ServerError,
)
from markdownify import markdownify

PAGE = 500
UA = "Mozilla/5.0 (ipdump)"
CATEGORIES = {0: "article", 1: "email", 2: "video", 3: "pdf", 4: "social"}
SCHEMA = """
create table if not exists bookmarks(id integer primary key, json text not null, deleted_at integer);
create table if not exists content(bookmark_id integer primary key, json text, fetched_at integer, error text);
create table if not exists folders(id integer primary key, json text not null);
create table if not exists images(url text primary key, file text, error text);
create table if not exists state(key text primary key, value);
"""
# liked first, then newest saved
PRIORITY = "order by json_extract(b.json, '$.liked') desc, json_extract(b.json, '$.time') desc"


class Throttle:
    """SDK transport: random delay before every API call, retries 429/5xx, keeps the last raw JSON.

    The SDK models drop keys they don't know, so the raw body in `last` is what gets stored.
    """

    def __init__(self, delay=(1.0, 6.0), send=urllib_transport, tries=6):
        self.delay, self.send, self.tries, self.last = delay, send, tries, None

    def __call__(self, request):
        for attempt in range(self.tries):
            time.sleep(random.uniform(*self.delay))
            response = self.send(request)
            if (response.status != 429 and response.status < 500) or attempt == self.tries - 1:
                break
            wait = _retry_after(response.headers) or min(300, 10 * 2**attempt)
            print(f"  HTTP {response.status}, retrying in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
        self.last = decode_body(response.body)
        return response


def _retry_after(headers):
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return min(300.0, float(value))
    except (TypeError, ValueError):
        return None


def open_db(data: Path) -> sqlite3.Connection:
    (data / "images").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data / "instapaper.db")
    db.executescript(SCHEMA)
    return db


def get(db, key, default=None):
    row = db.execute("select value from state where key = ?", (key,)).fetchone()
    return default if row is None else row[0]


def put(db, **values):
    db.executemany("insert or replace into state(key, value) values (?, ?)", values.items())


# ---------- sync ----------


def atomic_write(path: Path, payload: bytes):
    """Write via a temp file so an interrupt never leaves a half-written file behind."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def sync(db, data: Path, client, throttle: Throttle, max_articles=200):
    # Every page/article/image is its own transaction, so Ctrl-C loses at most the item in flight.
    counts = Counter()
    try:
        sync_meta(db, client, throttle, counts)
        sync_articles(db, client, throttle, max_articles, counts)
        sync_images(db, data, counts)
    finally:
        summary(db, counts)
    return counts


def summary(db, counts):
    left, liked = db.execute(
        "select count(*), coalesce(sum(json_extract(b.json, '$.liked')), 0) from bookmarks b"
        " left join content c on c.bookmark_id = b.id where b.deleted_at is null and c.bookmark_id is null"
    ).fetchone()
    print(
        f"+{counts['new']} new, ~{counts['changed']} changed, -{counts['deleted']} deleted, "
        f"{counts['articles']} articles, {counts['images']} images ({left:,} articles remaining, {liked} liked)"
    )


def sync_meta(db, client, throttle, counts):
    since = get(db, "pending_since")
    if since is None:  # fresh run; otherwise resume the interrupted one
        since, offset, started = get(db, "since", 1), 0, int(time.time())
        with db:
            put(db, pending_since=since, pending_offset=0, pending_started=started)
    else:
        offset, started = get(db, "pending_offset"), get(db, "pending_started")
    while True:
        page = client.bookmarks.changes(since, limit=PAGE, offset=offset)
        raw = throttle.last.get("bookmarks") or []
        with db:
            for bookmark in raw:
                upsert(db, bookmark, counts)
            for bid in page.deleted_ids:
                cur = db.execute(
                    "update bookmarks set deleted_at = ? where id = ? and deleted_at is null", (int(time.time()), bid)
                )
                if cur.rowcount:
                    counts["deleted"] += 1
                    print(f"- {bid}")
            offset += len(raw) + len(page.deleted_ids)
            put(db, pending_offset=offset)
        if len(raw) + len(page.deleted_ids) < PAGE:
            break
    client.folders.list()
    with db:
        db.execute("delete from folders")
        db.executemany(
            "insert into folders(id, json) values (?, ?)",
            [(f["id"], json.dumps(f, sort_keys=True)) for f in throttle.last.get("folders") or []],
        )
        db.execute("delete from state where key like 'pending_%'")
        put(db, since=max(1, started - 60))  # overlap for clock skew


def upsert(db, b, counts):
    new = json.dumps(b, sort_keys=True)
    row = db.execute("select json, deleted_at from bookmarks where id = ?", (b["id"],)).fetchone()
    if row and row[0] == new and row[1] is None:
        return
    title = b.get("title") or ""
    if row is None:
        counts["new"] += 1
        print(f'+ {b["id"]} "{title}"')
    else:
        old = json.loads(row[0])
        diff = [f"{k} {_short(old.get(k))}→{_short(b.get(k))}" for k in sorted(old | b) if old.get(k) != b.get(k)]
        counts["changed"] += 1
        print(f'~ {b["id"]} "{title}": {", ".join(diff) or "undeleted"}')
        if old.get("url") != b.get("url"):
            db.execute("delete from content where bookmark_id = ?", (b["id"],))
    db.execute(
        "insert into bookmarks(id, json, deleted_at) values (?, ?, null)"
        " on conflict(id) do update set json = excluded.json, deleted_at = null",
        (b["id"], new),
    )


def _short(v):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 60 else s[:57] + "..."


def sync_articles(db, client, throttle, max_articles, counts):
    sql = (
        "select b.id from bookmarks b left join content c on c.bookmark_id = b.id"
        f" where b.deleted_at is null and c.bookmark_id is null {PRIORITY}"
    )
    if max_articles:
        sql += f" limit {int(max_articles)}"
    for (bid,) in db.execute(sql).fetchall():
        try:
            client.bookmarks.parse(bid)
            row = (bid, json.dumps(throttle.last), None)
        except (BadRequestError, NotFoundError) as e:
            row = (bid, None, str(e))
        except (QuotaExceededError, RateLimitError, ServerError) as e:
            print(f"stopping article fetch, next run resumes: {e}", file=sys.stderr)
            return
        with db:
            db.execute("insert or replace into content values (?, ?, ?, ?)", (bid, row[1], int(time.time()), row[2]))
        counts["articles"] += 1


_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)""", re.I)


def article_images(parsed, base):
    """Absolute image URLs: <img src> in body order, then content.images."""
    c = parsed.get("content") or {}
    srcs = [html.unescape(s) for s in _IMG_SRC.findall(c.get("body") or "")] + list(c.get("images") or [])
    urls = (urljoin(base or "", s.strip()) for s in srcs)
    return list(dict.fromkeys(u for u in urls if u.startswith(("http://", "https://"))))


def sync_images(db, data, counts):
    # ponytail: rescans every article body each run; add a per-article "images done" flag if this gets slow
    rows = db.execute(
        "select c.json, b.json from content c join bookmarks b on b.id = c.bookmark_id"
        f" where c.json is not null and b.deleted_at is null {PRIORITY}"
    ).fetchall()
    for cj, bj in rows:
        for url in article_images(json.loads(cj), json.loads(bj).get("url")):
            if db.execute("select 1 from images where url = ?", (url,)).fetchone():
                continue
            file, error = fetch_image(data / "images", url)
            with db:
                db.execute("insert into images values (?, ?, ?)", (url, file, error))
            counts["images"] += 1


def fetch_image(folder: Path, url: str):
    """Download to folder/<sha256(url)[:16]>.<ext>. Returns (file, None) or (None, error)."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=30) as r:
            body, ctype = r.read(), r.headers.get_content_type()
    except Exception as e:  # any third-party failure is just recorded
        return None, str(e) or type(e).__name__
    suffix = Path(urlparse(url).path).suffix.lower()
    ext = (mimetypes.guess_extension(ctype) if ctype.startswith("image/") else None) or (
        suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else ".bin"
    )
    name = hashlib.sha256(url.encode()).hexdigest()[:16] + ext
    atomic_write(folder / name, body)
    return name, None


# ---------- export ----------


def safe(s, cap=80):
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f\x7f]', "-", s or "")
    s = re.sub(r"-{2,}", "-", re.sub(r"\s+", "-", s))
    return s[:cap].strip(".-") or "untitled"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def export(db, data: Path, out: Path):
    folders = {fid: json.loads(j).get("title") for fid, j in db.execute("select id, json from folders")}
    files = dict(db.execute("select url, file from images where file is not null"))
    manifest_path = out / ".ipdump-manifest"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    used, written, count = set(), 0, 0

    def write(rel, payload: bytes | Path, digest):
        nonlocal written
        path = out / rel
        if manifest.get(rel) == digest and path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, payload.read_bytes() if isinstance(payload, Path) else payload)
        manifest[rel] = digest  # only after the file is fully in place
        written += 1

    rows = db.execute(
        "select b.id, b.json, c.json from bookmarks b left join content c on c.bookmark_id = b.id"
        " where b.deleted_at is null order by b.id"
    )
    try:
        for bid, bj, cj in rows:
            b, parsed = json.loads(bj), json.loads(cj) if cj else None
            year = datetime.fromtimestamp(b.get("time") or 0, timezone.utc).year
            if b.get("archived"):
                location = "archive"
            elif b.get("folder_id") is None:
                location = "home"
            else:
                # ponytail: a folder titled home/archive/liked merges with those dirs; prefix it if that ever happens
                location = safe(folders.get(b["folder_id"]) or str(b["folder_id"]))
            dirs = [f"{location}/{year}"] + ([f"liked/{year}"] if b.get("liked") else [])
            stem = f"{(iso(b.get('time')) or '1970-01-01T00:00:00Z')[:19].replace(':', '-')}_{safe(b.get('title'))}"
            if any((d, stem) in used for d in dirs):
                stem += f"_{bid}"
            used.update((d, stem) for d in dirs)

            text, images = render(b, parsed, stem, folders, files)
            payload = text.encode()
            digest = hashlib.sha256(payload).hexdigest()
            for d in dirs:
                write(f"{d}/{stem}.md", payload, digest)
                for f in images:
                    write(f"{d}/{stem}_files/{f}", data / "images" / f, f)
            count += 1
    finally:  # also on Ctrl-C, so the next run skips what's already written
        out.mkdir(parents=True, exist_ok=True)
        atomic_write(manifest_path, json.dumps(manifest, indent=0, sort_keys=True).encode())
        print(f"exported {count} articles, wrote {written} files to {out}")
    return written


def render(b, parsed, stem, folders, files):
    """Markdown text for one bookmark, plus {local file: original url} of the images it links."""
    meta = (parsed or {}).get("metadata") or {}
    content = (parsed or {}).get("content") or {}
    images = {}
    if content.get("body"):
        soup = BeautifulSoup(content["body"], "html.parser")
        for img in soup.find_all("img", src=True):
            url = urljoin(b.get("url") or "", html.unescape(img["src"]).strip())
            if url in files:
                img["src"] = f"{stem}_files/{files[url]}"
                images[files[url]] = url
        body = markdownify(str(soup), heading_style="ATX").strip()
    else:
        body = f"{b.get('description') or ''}\n\n_(article text not synced yet)_".strip()

    author = meta.get("author") if isinstance(meta.get("author"), dict) else {}
    progress = b.get("progress") or {}
    category = b.get("category")
    fm = {
        "id": b["id"],
        "url": b.get("url"),
        "title": b.get("title"),
        "author": author.get("name") or b.get("author"),
        "author_url": author.get("url"),
        "description": b.get("description"),
        "image": b.get("image"),
        "saved": iso(b.get("time")),
        "published": iso(b.get("pubtime") or meta.get("pubtime")),
        "progress": progress.get("percentage"),
        "progress_at": iso(progress.get("timestamp")),
        "liked": bool(b.get("liked")),
        "archived": bool(b.get("archived")),
        "folder": folders.get(b.get("folder_id")),
        "tags": [t.get("name") for t in b.get("tags") or []],
        "category": CATEGORIES.get(category, category),
        "words": content.get("words"),
        "paywalled": content.get("paywalled"),
        "private_source": b.get("private_source"),
    }
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()]
    lines += ["images:"] + [f"  {f}: {json.dumps(u, ensure_ascii=False)}" for f, u in images.items()] if images else ["images: {}"]
    return "\n".join(lines + ["---", "", body, ""]), images


# ---------- cli ----------


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", type=Path, default=Path.home() / ".local/share/ipdump", help="database + image cache")
    parser = argparse.ArgumentParser(prog="ipdump", description="Mirror Instapaper locally and export Markdown.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", parents=[common], help="incremental sync (needs INSTAPAPER_TOKEN)")
    s.add_argument("--max-articles", type=int, default=200, help="article texts per run, 0 = all")
    s.add_argument("--delay", type=float, nargs=2, default=(1.0, 6.0), metavar=("LO", "HI"), help="seconds between API calls")
    e = sub.add_parser("export", parents=[common], help="write Markdown files")
    e.add_argument("--out", type=Path, default=Path("export"))
    args = parser.parse_args(argv)

    db = open_db(args.data)
    try:
        if args.cmd == "sync":
            token = os.environ.get("INSTAPAPER_TOKEN") or parser.error("set INSTAPAPER_TOKEN")
            throttle = Throttle(tuple(args.delay))
            sync(db, args.data, Instapaper(token, transport=throttle), throttle, args.max_articles)
        else:
            export(db, args.data, args.out)
    except KeyboardInterrupt:
        print("interrupted; progress is saved, run again to continue", file=sys.stderr)
        sys.exit(130)
    finally:
        db.close()


if __name__ == "__main__":
    main()
