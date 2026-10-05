# ipdump

Mirror your Instapaper account locally and export it as plain Markdown files with local images.

- **sync** incrementally downloads bookmark metadata, article text and images into a local SQLite
  database. It is throttled (random 1–6 s between API calls, backoff on rate limits) and resumable,
  and it prints what changed since the last run. Liked articles are fetched first.
- **export** writes one Markdown file per article, with YAML front matter and images alongside,
  mirroring Instapaper: `home/`, `archive/`, one dir per folder, plus copies of liked articles in
  `liked/`, each grouped by year.

Built on the [Instapaper API v2](https://www.instapaper.com/developers) and the official
[`instapaper-api`](https://pypi.org/project/instapaper-api/) SDK. Read-only: it never changes
anything in your Instapaper account. See [SPEC.md](SPEC.md) for the details.

## Install

Needs [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> && cd app-instapaper-dump
uv sync
```

## Credentials

One personal access token for your own account:

1. Go to [instapaper.com/developers/applications](https://www.instapaper.com/developers/applications)
   and create an application (any name, e.g. "my export").
2. Open it and select **Generate access token**. It is shown only once; regenerating or revoking it
   on the same page disables the old one.
3. Provide it via the environment:

   ```sh
   export INSTAPAPER_TOKEN=...
   ```

Keep the token private, and don't commit it. Article text is free for **personal use** only (the
developer who registered the application reads their own account), per the
[API terms](https://www.instapaper.com/developers/overview/api-terms).

## Use

```sh
uv run ipdump sync                    # metadata + up to 200 article texts + their images
uv run ipdump sync --max-articles 0   # no cap; a huge archive can take many hours
uv run ipdump export                  # Markdown into ./export
uv run ipdump export --out ~/Notes/instapaper
```

Run `sync` as often as you like (e.g. daily from cron). Each run only fetches what changed, and an
interrupted run resumes where it stopped. Repeat `export` runs only write files that changed. The
export never deletes anything, so to drop articles you deleted, unliked or moved, delete the
export directory and export again.

Data lives in `~/.local/share/ipdump/` (`instapaper.db` and `images/`); override with `--data DIR`.
`--delay LO HI` changes the wait between API calls.

## Development

```sh
uv run pytest
```
