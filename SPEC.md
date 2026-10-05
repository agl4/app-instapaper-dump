# ipdump: incremental Instapaper mirror → plain-Markdown export

## Context
The user has a huge Instapaper archive and wants it as plain files. The **export is the product**.
SQLite only holds the raw API responses so the sync can be continuous and incremental without
getting banned. Images must be local so the Markdown renders offline. Built on Instapaper API v2 via
the official SDK `instapaper-api` 1.0.0 (stdlib-only, Python ≥3.10), managed with **uv**.
The repo is empty (README only).

Facts from the spec and SDK source (verified):
- `GET /bookmarks?since=<ts>` returns every change across the whole account plus `deleted_ids`.
  The limit is ≤ 500; paging uses `offset += len(bookmarks)+len(deleted_ids)` and stops on a short page.
  `since=1` returns the full account, so the initial import uses the same code as the incremental one.
- SDK `client.bookmarks.changes(since, limit, offset)` fetches one page. Use it instead of `sync()`,
  which buffers the whole account in memory and can't resume.
- Bookmarks have no `updated_at`, so changes are detected by diffing against the stored JSON.
- `client.bookmarks.parse(id)` returns body HTML, `images[]`, `words`, and metadata. It costs one call
  per article and can return 402 or 429.
- The SDK has no retry or throttling, but `Instapaper(token, transport=...)` accepts a pluggable
  transport. The errors are typed (`RateLimitError`, `ServerError`, `QuotaExceededError`).
- `client.folders.list()` is a single cheap call.

User decisions: the export mirrors Instapaper's own model. Home, Archive and user folders are
locations, and each article is in exactly one of them. Liked is a flag, so liked articles also get
a real copy in `liked/`. There is no `all/` dir. Deleted bookmarks are dropped from the export. No search, no changelog
table/command, no highlights. Delay between API calls is random, 1–6 s.

## Layout
uv: `uv init --app` (+ hatchling `[build-system]` so the `ipdump` entry point installs from the flat
`ipdump.py`), `uv add instapaper-api markdownify`, `uv add --dev pytest`. Commit `uv.lock` and
`.python-version`. Run everything with `uv run ipdump …` / `uv run pytest`.
```
pyproject.toml, uv.lock, .python-version
ipdump.py        # everything (~250 lines)
test_ipdump.py   # fake transport, no network
```
Data dir: `~/.local/share/ipdump/` (`--data`), containing `instapaper.db` and `images/`.

## Storage (raw API data)
- `bookmarks(id PK, json, deleted_at)`: the raw bookmark object from the change feed
- `content(bookmark_id PK, json, fetched_at, error)`: the raw parse response
- `folders(id PK, json)`
- `images(url PK, file, error)`: `file` is the name `<sha256(url)[:16]>.<ext>`. The file lives on
  disk, not in the DB, sharded squid-style as `images/<ab>/<cd>/<abcd…>.<ext>` (first two hex pairs),
  so no directory holds more than a few files even for hundreds of thousands of images.
- `state(key PK, value)`: `since`, `pending_since`, `pending_offset`, `pending_started`

## Sync: `uv run ipdump sync [--max-articles N] [--delay LO HI]`
**Throttled transport** wraps the SDK's `urllib_transport`. Before each API request it sleeps for
`random.uniform(1, 6)` seconds (overridable with `--delay`). On 429 or 5xx it waits for `Retry-After`,
or backs off exponentially up to 300 s, for at most ~6 tries. After that it returns the response so
the SDK raises the typed error.

1. **Metadata.** Resume from `pending_since/offset` if set. Otherwise use `since = state.since or 1`
   and record the start time. Commit each page of `changes(...)` in its own transaction: upsert the
   raw JSON and print one line per new or changed bookmark (the top-level keys that differ, e.g.
   `~ 123 "Title": liked false→true`). Mark `deleted_ids` with `deleted_at`. Save the offset.
   A crash resumes on the next run. When finished, set `since = started - 60` (overlap for clock
   skew) and refresh `folders`.
2. **Article text.** Process bookmarks that have no content and aren't deleted, **liked first**,
   then newest first (`ORDER BY liked DESC, time DESC` on the stored JSON). Liked articles are the
   most important ones, so they become complete and exportable before the rest of the backlog.
   An article liked later jumps the queue on the next run. Up to `--max-articles` per run (default
   200, `0` = all), calling `parse(id)` and storing the raw JSON.
   A 400 or 404 is recorded in `error` and skipped. A 402 or exhausted 429 stops cleanly, and the
   next run resumes. If a bookmark's `url` changes, its content row is deleted so it gets re-parsed.
3. **Images, per article.** Right after an article's text is stored, its images are downloaded
   (every URL in `content.images` plus `<img src>` in the body that isn't already in `images`), so
   each step leaves a complete article. This uses plain `urllib` to the third-party hosts with a 30 s
   timeout and no API throttling. Failures are recorded in `error` and not retried. Before fetching
   new articles, a catch-up pass downloads images that an interrupted run left behind.
   **Ctrl-C** during an article is held until its text and images are stored, then sync stops;
   a second Ctrl-C aborts at once (the catch-up pass covers that case).

The summary line looks like `+12 new, ~5 changed, -1 deleted, 30 articles, 211 images (4,210 articles remaining, 37 liked)`.

## Export: `uv run ipdump export [--out EXPORT_DIR]` (default `./export`). The main feature.
Reads only from SQLite and `images/`, with no API calls. Only bookmarks with full article text are
exported; deleted bookmarks and ones whose text isn't synced yet (or failed to parse) are skipped.

**Directories.** These mirror Instapaper. Each article goes into exactly one location dir, plus
a copy in `liked/` if it's liked:
```
EXPORT_DIR/home/<year>/<name>.md           not archived, folder_id null
EXPORT_DIR/archive/<year>/<name>.md        archived == true
EXPORT_DIR/<Folder Title>/<year>/<name>.md not archived, folder_id == that folder
EXPORT_DIR/liked/<year>/<name>.md          liked == true (real copy; liked is a flag, not a location)
EXPORT_DIR/…/<year>/<name>_files/<img>     the article's images, copied alongside each .md
```
- Location rule: archiving moves a bookmark out of its folder, as it does in the Instapaper app, so
  archived bookmarks go to `archive/`. If the API still returns a `folder_id` for an archived
  bookmark, `archived` wins.
- `<year>` is the year of the saved time (`time`, UTC).
- `<name>` = `2014-03-01t10-22-00_<safe title>`. The saved time is UTC ISO, lower-cased, with `:`
  replaced by `-`. The safe title is lower-cased and accent-free (`Árvíztűrő` → `arvizturo`, plus
  `ß→ss`, `ø→o`, `ł→l`…). Every run of non-alphanumerics becomes a single `-`, it is capped at 80 chars,
  and it falls back to `untitled`. Non-Latin scripts are kept, not transliterated. If two files
  collide (same second, same title), `_<id>` is appended.
- The folder dir name uses the same sanitizer.
  `# ponytail:` a folder named "home"/"archive"/"liked" would merge with those dirs; prefix if it ever happens.

**File contents.**
- YAML front-matter holds all the metadata the API gives: id, url, title, author, author_url,
  description, image, saved, published, progress, progress_at, liked, archived, folder, tags (names),
  category, language, words, paywalled, private_source, images. Values are written with `json.dumps(v)`, since
  JSON is valid YAML, so PyYAML isn't needed.
- `language` is the ISO 639-1 code detected from title + body text with `langdetect` (seeded, so
  stable across exports), with a prior of en > hu > de that only tips close calls; any of its 55
  languages can still come out. `null` if undetectable.
- `images` maps each local file in `_files/` back to its original URL, so the exported article
  stands alone without the DB. It is built from the `images` table and written as a block mapping
  in body order (key = sha file name, value = `json.dumps(url)`). Images that failed to download
  aren't listed, because they keep their remote URL in the body.
  ```yaml
  images:
    3f9a1c0b7d2e4a51.jpg: "https://cdn.example.com/2014/03/photo.jpg?w=800"
    a07c55e1b2f9d830.png: "https://example.com/chart.png"
  ```
- Body: rewrite `<img src>` in the HTML to `<name>_files/<file>` for every downloaded image, then
  run `markdownify`. Images that haven't been downloaded keep their remote URL.

**Re-export.** The export only adds and updates files. It never deletes anything. To get a clean
export (after deletions, unlikes, or moves), delete `EXPORT_DIR` yourself and export again.
- `EXPORT_DIR/.ipdump-manifest` is a JSON file mapping `path → sha256` of what was written. A file
  is skipped when its new hash matches the manifest and the file still exists, so repeat exports of
  a huge archive don't rewrite anything. Image copies are recorded the same way. The manifest is
  rewritten at the end of each run.

## Skipped (add when needed)
Search, changelog table, highlights, OAuth flow (a personal token is enough), writing back to
Instapaper, export filters, tag dirs.

## Verification
- `test_ipdump.py` uses a fake `Transport` with canned JSON and has `time.sleep` monkeypatched:
  - The initial sync stores rows. A second sync with a changed `liked` and one deleted id prints the
    diff line and sets `deleted_at`. A sync interrupted mid-paging resumes at the saved offset.
    A 429 with `Retry-After` gets retried.
  - Export with one liked article in folder "Tech" and a fake image produces
    `tech/2014/2014-…_title.md` and `liked/2014/…`, each with a `_files/` image and a relative
    `![](…_files/…)` link, and a front-matter `images` entry mapping that file to its URL.
    A second export writes nothing (checked via file mtimes). Deleted bookmarks and ones without text are not exported.
- Live: `INSTAPAPER_TOKEN=… uv run ipdump sync --max-articles 5`, then `uv run ipdump export`,
  then open a liked article's .md in a Markdown viewer to check that the images render offline.
  Rerun sync: expect `0 changed`.
