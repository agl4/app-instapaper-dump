"""ipdump: incremental Instapaper mirror -> plain-Markdown export. See SPEC.md."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import random
import re
import signal
import sqlite3
import sys
import time
import unicodedata
import urllib.request
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from instapaper import Instapaper
from instapaper._transport import decode_body, urllib_transport
import langdetect.detector_factory as langdetect_factory
from langdetect import DetectorFactory, LangDetectException
from instapaper.errors import (
    AuthenticationError,
    BadRequestError,
    InstapaperConnectionError,
    InstapaperError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    ServerError,
)
from markdownify import markdownify
import trafilatura

PAGE = 500
# sites only: API calls keep the SDK's own header
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
WEB_DELAY = (1.0, 3.0)  # seconds before every page download; --delay is only for the API
IMAGE_DELAY = (0.05, 0.25)  # seconds before every image download
MAX_PAGE = 20 * 1024 * 1024
MIN_WORDS = 150  # below this a page is probably a stub, so Instapaper gets asked for its copy
PAYWALL = re.compile(rb"""["']?isAccessibleForFree["']?\s*:\s*["']?false""", re.I)
# languages the user reads, most likely first: a prior that only tips close calls; others still detectable
LANGUAGE_PRIOR = {"en": 8, "hu": 4, "de": 2}
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
            last_try = attempt == self.tries - 1
            time.sleep(random.uniform(*self.delay))
            try:
                response = self.send(request)
            except InstapaperConnectionError as e:  # reset, timeout, DNS: retry like a 5xx
                if last_try:
                    raise
                what, wait = f"connection problem ({e.__cause__ or e})", min(300, 10 * 2**attempt)
            else:
                if (response.status != 429 and response.status < 500) or last_try:
                    break
                wait = _retry_after(response.headers) or min(300, 10 * 2**attempt)
                what = f"{'rate limited' if response.status == 429 else 'server error'} (HTTP {response.status})"
            log(f"  {what}, cooling down {wait:.0f}s, then retry {attempt + 2}/{self.tries}")
            time.sleep(wait)
        self.last = decode_body(response.body)
        return response


def log(msg):
    print(msg, flush=True)


def _title(b, width=70):
    t = (b.get("title") or b.get("url") or str(b.get("id"))).strip()
    return t if len(t) <= width else t[: width - 1] + "…"


def _retry_after(headers):
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return min(300.0, float(value))
    except (TypeError, ValueError):
        return None


def image_path(data: Path, name: str) -> Path:
    """Squid-style cache layout: images/3f/9a/3f9a1c0b7d2e4a51.jpg (256 x 256 dirs)."""
    return data / "images" / name[:2] / name[2:4] / name


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


def sync(db, data: Path, client, throttle: Throttle, max_articles=200, full=False):
    # Every page/article/image is its own transaction, so Ctrl-C loses at most the item in flight.
    counts = Counter()
    meta_error = None
    try:
        try:
            if full:
                full_import(db, client, throttle, counts)
            sync_meta(db, client, throttle, counts)
        except InstapaperError as e:  # the web doesn't care: download what we have links for
            meta_error = e
            log(f"Bookmarks: stopped ({e}), downloading from the links already known")
        sync_missing_images(db, data, counts)
        sync_web_articles(db, data, max_articles, counts)
        if meta_error:  # the Instapaper pass would only hit the same problem
            raise meta_error
        sync_api_articles(db, data, client, throttle, max_articles, counts)
    finally:
        summary(db, counts)
    return counts


def summary(db, counts):
    left, liked = db.execute(
        "select count(*), coalesce(sum(json_extract(b.json, '$.liked')), 0) from bookmarks b"
        " left join content c on c.bookmark_id = b.id where b.deleted_at is null and c.bookmark_id is null"
    ).fetchone()
    waiting = db.execute(
        "select count(*) from content c join bookmarks b on b.id = c.bookmark_id"
        " where b.deleted_at is null and c.error like 'web:%'"
    ).fetchone()[0]
    log(
        f"Done: +{counts['new']} new, ~{counts['changed']} changed, -{counts['deleted']} deleted, "
        f"{counts['articles']} articles from the web, {counts['api']} from Instapaper, {counts['images']} images "
        f"({left:,} articles remaining, {liked} liked, {waiting:,} waiting for Instapaper)"
    )


def sync_meta(db, client, throttle, counts):
    since = get(db, "pending_since")
    if since is None:  # fresh run; otherwise resume the interrupted one
        since, offset, started = get(db, "since", 1), 0, int(time.time())
        with db:
            put(db, pending_since=since, pending_offset=0, pending_started=started)
    else:
        offset, started = get(db, "pending_offset"), get(db, "pending_started")
    log(f"Bookmarks: syncing changes since {iso(since) if since > 1 else 'the beginning'}"
        + (f", resuming at {offset}" if offset else ""))
    while True:
        page = client.bookmarks.changes(since, limit=PAGE, offset=offset)
        raw = throttle.last.get("bookmarks") or []
        log(f"  got {len(raw)} bookmarks, {len(page.deleted_ids)} deleted (at {offset + len(raw) + len(page.deleted_ids)})")
        with db:
            for bookmark in raw:
                upsert(db, bookmark, counts)
            for bid in page.deleted_ids:
                cur = db.execute(
                    "update bookmarks set deleted_at = ? where id = ? and deleted_at is null", (int(time.time()), bid)
                )
                if cur.rowcount:
                    counts["deleted"] += 1
                    log(f"- {bid}")
            offset += len(raw) + len(page.deleted_ids)
            put(db, pending_offset=offset)
        if len(raw) + len(page.deleted_ids) < PAGE:
            break
    refresh_folders(db, client, throttle)
    with db:
        db.execute("delete from state where key like 'pending_%'")
        put(db, since=max(1, started - 60))  # overlap for clock skew


def refresh_folders(db, client, throttle):
    log("Folders: refreshing")
    client.folders.list()
    with db:
        db.execute("delete from folders")
        db.executemany(
            "insert into folders(id, json) values (?, ?)",
            [(f["id"], json.dumps(f, sort_keys=True)) for f in throttle.last.get("folders") or []],
        )


def full_import(db, client, throttle, counts):
    """Import of every bookmark by paging home, archive and each folder (sync --full-import).

    The `since` change feed only returns bookmarks with recorded changes (in practice ~2025 on),
    not the whole account, so only this reads older bookmarks. It is many API calls, so it runs on
    request; an interrupted one resumes on the next request, a finished one starts over.
    Every bookmark is in exactly one of these lists; liked is just a flag.
    """
    if get(db, "import_started") is None:
        refresh_folders(db, client, throttle)
        with db:
            put(db, import_started=int(time.time()), import_index=0, import_offset=0)
    titles = {fid: json.loads(j).get("title") for fid, j in db.execute("select id, json from folders")}
    lists = [("home", None, "home"), ("archive", None, "archive")] + [
        ("folder", fid, f"folder {title!r}") for fid, title in sorted(titles.items())
    ]
    index, offset = get(db, "import_index"), get(db, "import_offset")
    log(f"Full import: reading {len(lists)} lists (home, archive, {len(lists) - 2} folders)"
        + (f", resuming at {lists[index][2] if index < len(lists) else 'end'} {offset}" if index or offset else ""))
    # ponytail: offset paging over a live list can skip an item if one is added/moved mid-import;
    # the change feed afterwards catches anything that changed, not items that merely shifted
    while index < len(lists):
        section, folder_id, name = lists[index]
        page = client.bookmarks.list(section=section, folder_id=folder_id, limit=PAGE, offset=offset)
        raw = throttle.last.get("bookmarks") or []
        with db:
            for bookmark in raw:
                upsert(db, bookmark, counts, quiet=True)
            offset += len(raw)
            log(f"  {name}: {offset}/{page.total}")
            if len(raw) < PAGE or offset >= page.total:
                index, offset = index + 1, 0
            put(db, import_index=index, import_offset=offset)
    with db:
        if get(db, "since") is None:  # first ever sync: the feed only needs changes made during the import
            put(db, since=max(1, get(db, "import_started") - 60))
        db.execute("delete from state where key like 'import_%'")


def upsert(db, b, counts, quiet=False):
    new = json.dumps(b, sort_keys=True)
    row = db.execute("select json, deleted_at from bookmarks where id = ?", (b["id"],)).fetchone()
    if row and row[0] == new and row[1] is None:
        return
    title = b.get("title") or ""
    if row is None:
        counts["new"] += 1
        if not quiet:
            log(f'+ {b["id"]} "{title}"')
    else:
        old = json.loads(row[0])
        diff = [f"{k} {_short(old.get(k))}→{_short(b.get(k))}" for k in sorted(old | b) if old.get(k) != b.get(k)]
        counts["changed"] += 1
        log(f'~ {b["id"]} "{title}": {", ".join(diff) or "undeleted"}')
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


def web_wait(delay=WEB_DELAY):
    # ponytail: sequential; add a thread pool if a full-archive run is too slow
    time.sleep(random.uniform(*delay))


def fetch_page(url):
    """Download a web page. Returns (html bytes, URL after redirects); raises on anything unusable."""
    if not (url or "").startswith(("http://", "https://")):  # urlopen would happily read file://
        raise ValueError(f"not a web URL ({url})")
    web_wait()
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as r:
        ctype = r.headers.get_content_type()
        if ctype not in ("text/html", "application/xhtml+xml"):
            raise ValueError(f"not html ({ctype})")
        return r.read(MAX_PAGE), r.geturl()


def clean_article(html, url):
    """Strip a page down to its article, in the same shape as an Instapaper parse response."""
    body = trafilatura.extract(html, url=url, output_format="html", include_images=True, include_links=True,
                               include_tables=True, include_formatting=True)
    if not body:
        raise ValueError("no article text")
    soup = BeautifulSoup(body, "html.parser")
    for table in reversed(soup.find_all("table")):  # layout tables: else the whole article lands in one Markdown cell
        if len(table.find_all(["td", "th"])) == 1:
            for tag in [table, *table.find_all(["tbody", "tr", "td", "th"])]:
                tag.unwrap()
    for img, absolute in body_images(soup, url):
        img["src"] = absolute
    for a in soup.find_all("a", href=True):
        a["href"] = urljoin(url, a["href"].strip())
    meta = trafilatura.extract_metadata(html, default_url=url)
    date = meta.date if meta else None
    root = soup.body or soup
    return {
        "metadata": {
            "author": {"name": meta.author if meta else None},
            "pubtime": int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) if date else None,
        },
        "content": {"body": root.decode_contents().strip(), "images": [], "words": len(root.get_text(" ").split())},
        "source": "web",
    }


def incomplete_reason(html, words):
    """Why this download may not be the whole article, or None. Paywalled pages still answer 200."""
    # ponytail: heuristic; soft paywalls with a long teaser and no JSON-LD marker pass as complete
    if PAYWALL.search(html):
        return "paywalled"
    return f"too short ({words} words)" if words < MIN_WORDS else None


def store_content(db, bid, parsed, error):
    with db:
        db.execute("insert or replace into content values (?, ?, ?, ?)",
                   (bid, json.dumps(parsed) if parsed else None, int(time.time()), error))


def _words(parsed):
    return ((parsed or {}).get("content") or {}).get("words") or 0


def sync_web_articles(db, data, max_articles, counts):
    """Download and clean each article, then its images. What the web can't provide is flagged `web: ...`."""
    sql = (
        "select b.id, b.json from bookmarks b left join content c on c.bookmark_id = b.id"
        f" where b.deleted_at is null and c.bookmark_id is null {PRIORITY}"
    )
    todo = db.execute(sql).fetchall()
    batch = todo[:max_articles] if max_articles else todo
    liked = sum(bool(json.loads(bj).get("liked")) for _, bj in batch)
    log(f"Articles: downloading {len(batch)} ({liked} liked) of {len(todo)} missing from the web")
    for i, (bid, bj) in enumerate(batch, 1):
        b = json.loads(bj)
        prefix = f"  [{i}/{len(batch)}] {'♥ ' if b.get('liked') else ''}{_title(b)}"
        with finish_first():
            try:
                html, url = fetch_page(b.get("url"))
                parsed = clean_article(html, url)
            except Exception as e:  # any third-party failure is just recorded
                store_content(db, bid, None, f"web: {e or type(e).__name__}")
                log(f"{prefix}: {e or type(e).__name__}, leaving for Instapaper")
                continue
            reason = incomplete_reason(html, _words(parsed))
            store_content(db, bid, parsed, f"web: {reason}" if reason else None)
            counts["articles"] += 1
            log(f"{prefix}: {_words(parsed)} words, " + fetch_article_images(db, data, parsed, b, counts)
                + (f", {reason}, leaving for Instapaper" if reason else ""))


def sync_api_articles(db, data, client, throttle, max_articles, counts):
    """Ask Instapaper only for what the web pass flagged; a stub is kept unless its copy is longer."""
    sql = (
        "select b.id, b.json, c.json from bookmarks b join content c on c.bookmark_id = b.id"
        f" where b.deleted_at is null and c.error like 'web:%' {PRIORITY}"
    )
    todo = db.execute(sql).fetchall()
    batch = todo[:max_articles] if max_articles else todo
    log(f"Instapaper: fetching {len(batch)} of {len(todo)} articles the web couldn't provide")
    for i, (bid, bj, cj) in enumerate(batch, 1):
        b, stub = json.loads(bj), json.loads(cj) if cj else None
        prefix = f"  [{i}/{len(batch)}] {'♥ ' if b.get('liked') else ''}{_title(b)}"
        with finish_first():
            try:
                client.bookmarks.parse(bid)
                parsed = throttle.last
            except (BadRequestError, NotFoundError) as e:
                store_content(db, bid, stub, f"instapaper: {e}")
                log(f"{prefix}: no text ({e})")
                continue
            except (QuotaExceededError, RateLimitError, ServerError) as e:
                log(f"Instapaper: stopping, next run resumes ({e})")
                return
            best = parsed if _words(parsed) > _words(stub) or not stub else stub
            store_content(db, bid, best, None)
            counts["api"] += best is parsed
            log(f"{prefix}: {_words(best)} words" + ("" if best is parsed else " (kept the web copy)") + ", "
                + fetch_article_images(db, data, best, b, counts))


@contextmanager
def finish_first():
    """Hold the first Ctrl-C until the block (one article: text + images) is done; a second aborts at once."""
    hit = []

    def handler(signum, frame):
        if hit:
            raise KeyboardInterrupt
        hit.append(signum)
        log("  Ctrl-C: finishing this article first, press again to abort now")

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if hit:  # block finished normally (also via continue/return): now stop
        raise KeyboardInterrupt


def body_images(soup, base):
    """(img tag, absolute URL) for every <img src>; sync and export both use this so they agree."""
    return [(img, urljoin(base or "", img["src"].strip())) for img in soup.find_all("img", src=True)]


def article_images(parsed, base):
    """Absolute image URLs: <img src> in body order, then content.images."""
    c = parsed.get("content") or {}
    body = [u for _, u in body_images(BeautifulSoup(c.get("body") or "", "html.parser"), base)]
    urls = body + [urljoin(base or "", s.strip()) for s in c.get("images") or []]
    return list(dict.fromkeys(u for u in urls if u.startswith(("http://", "https://"))))


def fetch_article_images(db, data, parsed, b, counts):
    """Download an article's images not fetched before; returns e.g. '5 images (1 failed)'."""
    new = [u for u in article_images(parsed, b.get("url"))
           if not db.execute("select 1 from images where url = ?", (u,)).fetchone()]
    failed = 0
    for url in new:
        file, error = fetch_image(data, url)
        with db:
            db.execute("insert into images values (?, ?, ?)", (url, file, error))
        counts["images"] += 1
        failed += error is not None
    return f"{len(new)} images" + (f" ({failed} failed)" if failed else "")


def sync_missing_images(db, data, counts):
    """Catch up images an interrupted run left behind, and retry failures that may be transient."""
    with db:  # 4xx and non-images are permanent; timeouts, resets, 5xx get another go
        db.execute("delete from images where error not like 'HTTP Error 4%' and error not like 'not an image%'")
    # ponytail: rescans every article body each run; add a per-article "images done" flag if this gets slow
    rows = db.execute(
        "select c.json, b.json from content c join bookmarks b on b.id = c.bookmark_id"
        f" where c.json is not null and b.deleted_at is null {PRIORITY}"
    ).fetchall()
    for cj, bj in rows:
        b, before = json.loads(bj), counts["images"]
        with finish_first():
            done = fetch_article_images(db, data, json.loads(cj), b, counts)
        if counts["images"] > before:
            log(f"  catch-up {_title(b)}: {done}")


def fetch_image(data: Path, url: str):
    """Download to image_path(<sha256(url)[:16]>.<ext>). Returns (file name, None) or (None, error)."""
    try:
        web_wait(IMAGE_DELAY)
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=30) as r:
            body, ctype = r.read(), r.headers.get_content_type()
    except Exception as e:  # any third-party failure is just recorded
        return None, str(e) or type(e).__name__
    if ctype in ("text/html", "application/xhtml+xml", "application/json"):  # error/login page served as 200
        return None, f"not an image ({ctype})"
    suffix = Path(urlparse(url).path).suffix.lower()
    ext = (mimetypes.guess_extension(ctype) if ctype.startswith("image/") else None) or (
        suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else ".bin"
    )
    name = hashlib.sha256(url.encode()).hexdigest()[:16] + ext
    path = image_path(data, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, body)
    return name, None


# ---------- export ----------


# letters that don't decompose into base letter + accent
_UNACCENT = str.maketrans({"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"})


def safe(s, cap=80):
    """Lower-case, accent-free, dash-separated: 'Árvíztűrő: Tükör!' -> 'arvizturo-tukor'."""
    s = unicodedata.normalize("NFKD", (s or "").lower().translate(_UNACCENT))
    s = "".join(c for c in s if not unicodedata.combining(c))
    # ponytail: non-Latin scripts (CJK, Cyrillic) are kept as letters, not transliterated
    s = re.sub(r"[\W_]+", "-", s)
    return s[:cap].strip("-") or "untitled"


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

    # only articles with full text; the rest appear once sync has fetched them
    rows = db.execute(
        "select b.id, b.json, c.json from bookmarks b join content c on c.bookmark_id = b.id"
        " where b.deleted_at is null and json_extract(c.json, '$.content.body') <> '' order by b.id"
    )
    rows = rows.fetchall()
    log(f"Export: {len(rows)} articles with full text -> {out}")
    try:
        for bid, bj, cj in rows:
            b, parsed = json.loads(bj), json.loads(cj)
            year = datetime.fromtimestamp(b.get("time") or 0, timezone.utc).year
            if b.get("archived"):
                location = "archive"
            elif b.get("folder_id") is None:
                location = "home"
            else:
                # ponytail: a folder titled home/archive/liked merges with those dirs; prefix it if that ever happens
                location = safe(folders.get(b["folder_id"]) or str(b["folder_id"]))
            dirs = [f"{location}/{year}"] + ([f"liked/{year}"] if b.get("liked") else [])
            stem = f"{(iso(b.get('time')) or '1970-01-01T00:00:00Z')[:19].replace(':', '-').lower()}_{safe(b.get('title'))}"
            if any((d, stem) in used for d in dirs):
                stem += f"_{bid}"
            used.update((d, stem) for d in dirs)

            text, images = render(b, parsed, stem, folders, files)
            payload = text.encode()
            digest = hashlib.sha256(payload).hexdigest()
            for d in dirs:
                write(f"{d}/{stem}.md", payload, digest)
                for f in images:
                    write(f"{d}/{stem}_files/{f}", image_path(data, f), f)
            count += 1
            if count % 500 == 0:
                log(f"  {count}/{len(rows)}, {written} files written")
    finally:  # also on Ctrl-C, so the next run skips what's already written
        out.mkdir(parents=True, exist_ok=True)
        atomic_write(manifest_path, json.dumps(manifest, indent=0, sort_keys=True).encode())
        log(f"exported {count} articles, wrote {written} files (unchanged ones skipped)")
    return written


def language(text):
    """ISO 639-1 code of the text (e.g. 'hu'), or None if it can't be told."""
    if not text.strip():
        return None
    DetectorFactory.seed = 0  # deterministic, so re-exports don't flip-flop
    langdetect_factory.init_factory()  # loads profiles once
    factory = langdetect_factory._factory
    detector = factory.create()
    detector.set_prior_map({lang: LANGUAGE_PRIOR.get(lang, 1) for lang in factory.get_lang_list()})
    detector.append(text[:10000])
    try:
        return detector.detect()
    except LangDetectException:
        return None


def render(b, parsed, stem, folders, files):
    """Markdown text for one bookmark, plus {local file: original url} of the images it links."""
    meta = parsed.get("metadata") or {}
    content = parsed["content"]
    images = {}
    soup = BeautifulSoup(content["body"], "html.parser")
    for img, url in body_images(soup, b.get("url")):
        if url in files:
            img["src"] = f"{stem}_files/{files[url]}"
            images[files[url]] = url
    body = markdownify(str(soup), heading_style="ATX").strip()

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
        "language": language(f"{b.get('title') or ''}\n{soup.get_text(' ')}"),
        "words": content.get("words"),
        "paywalled": content.get("paywalled"),
        "private_source": b.get("private_source"),
    }
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()]
    lines += ["images:"] + [f"  {f}: {json.dumps(u, ensure_ascii=False)}" for f, u in images.items()] if images else ["images: {}"]
    return "\n".join(lines + ["---", "", body, ""]), images


# ---------- cli ----------


def diagnose(e):
    """Turn an API failure into what it most likely means for the account."""
    if isinstance(e, AuthenticationError):
        return ("Instapaper rejected the token (401). It was revoked or regenerated, or access to the "
                "account was blocked. Generate a new token on your application's page; if that fails "
                "too, sign in at instapaper.com to check the account.")
    if isinstance(e, PermissionDeniedError):
        return (f"Instapaper refused access (403: {e.message}). 'Application is suspended' means the app "
                "was blocked: contact Instapaper support, retrying won't help.")
    if isinstance(e, RateLimitError):
        return "still rate limited after every retry (429). Wait an hour or more before syncing again."
    if isinstance(e, InstapaperConnectionError):
        return (f"Instapaper kept dropping the connection after every retry ({e}). If instapaper.com "
                "loads in a browser but this persists, this IP is probably blocked for a while; "
                "wait a few hours, then sync with a longer --delay.")
    return str(e)


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", type=Path, default=Path.home() / ".local/share/ipdump", help="database + image cache")
    parser = argparse.ArgumentParser(prog="ipdump", description="Mirror Instapaper locally and export Markdown.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", parents=[common], help="incremental sync (needs INSTAPAPER_TOKEN)")
    s.add_argument("--max-articles", type=int, default=200, help="articles per run for each of web and Instapaper, 0 = all")
    s.add_argument("--delay", type=float, nargs=2, default=(1.0, 6.0), metavar=("LO", "HI"), help="seconds between Instapaper API calls (web pages always wait 1-3 s, images 50-250 ms)")
    s.add_argument("--full-import", action="store_true", help="also read every list, for bookmarks the change feed doesn't have; many API calls, weekly is plenty")
    e = sub.add_parser("export", parents=[common], help="write Markdown files")
    e.add_argument("--out", type=Path, default=Path("export"))
    args = parser.parse_args(argv)

    db = open_db(args.data)
    try:
        if args.cmd == "sync":
            token = os.environ.get("INSTAPAPER_TOKEN") or parser.error("set INSTAPAPER_TOKEN")
            throttle = Throttle(tuple(args.delay))
            sync(db, args.data, Instapaper(token, transport=throttle), throttle, args.max_articles, args.full_import)
        else:
            export(db, args.data, args.out)
    except KeyboardInterrupt:
        log("interrupted; progress is saved, run again to continue")
        sys.exit(130)
    except InstapaperError as e:  # nothing is lost either way: every finished step is saved
        log(f"error: {diagnose(e)}\nprogress is saved; run again once that is resolved")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
