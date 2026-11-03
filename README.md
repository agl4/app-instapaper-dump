# ipdump

Mirror your Instapaper account locally and export it as plain Markdown files with local images.

- **sync** incrementally downloads bookmark metadata from Instapaper, then the articles and images from
  the original websites, cleaned down to the article, into a local SQLite database. Instapaper's own copy
  is only requested for articles the web can't provide, such as paywalled or removed ones. It is
  throttled (random 1–6 s between API calls with backoff on rate limits, 1–3 s between web pages, 50–250 ms between images)
  and resumable, and it prints what changed since the last run. Liked articles are fetched first.
- **export** writes one Markdown file per article that has its full text synced, with YAML front matter and images alongside,
  mirroring Instapaper: `home/`, `archive/`, one dir per folder, plus copies of liked articles in
  `liked/`, each grouped by year.

Built on the [Instapaper API v2](https://www.instapaper.com/developers) and the official
[`instapaper-api`](https://pypi.org/project/instapaper-api/) SDK. Read-only: it never changes
anything in your Instapaper account. Article cleanup uses [trafilatura](https://trafilatura.readthedocs.io/).
See [SPEC.md](SPEC.md) for the details.

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
3. Provide it as `INSTAPAPER_TOKEN`. ipdump only reads the environment, so pick one of:

   **A `.env` file** in the project folder (already in `.gitignore`):

   ```sh
   echo 'INSTAPAPER_TOKEN=your-token' > .env
   chmod 600 .env
   uv run --env-file .env ipdump sync
   ```

   To skip `--env-file` on every run, set `UV_ENV_FILE=.env` once in your shell
   (fish: `set -Ux UV_ENV_FILE .env`).

   **The macOS Keychain**, so the token never sits in a file:

   ```sh
   security add-generic-password -s instapaper -a "$USER" -w 'your-token'
   export INSTAPAPER_TOKEN="$(security find-generic-password -s instapaper -w)"
   ```

   Put the second line in your shell config (fish:
   `set -gx INSTAPAPER_TOKEN (security find-generic-password -s instapaper -w)` in
   `~/.config/fish/config.fish`).

Keep the token private, and don't commit it. Article text is free for **personal use** only (the
developer who registered the application reads their own account), per the
[API terms](https://www.instapaper.com/developers/overview/api-terms).

## Use

```sh
uv run ipdump sync                    # metadata + up to 200 web articles + up to 200 from Instapaper
uv run ipdump sync --max-articles 0   # no cap; a huge archive can take many hours
uv run ipdump sync --full-import      # also read every list, for bookmarks older than the change feed
uv run ipdump export                  # Markdown into ./export
uv run ipdump export --out ~/Notes/instapaper
```

Run `sync` as often as you like (e.g. daily from cron). Each run only fetches what changed, and an
interrupted run (Ctrl-C, crash, network loss) resumes where it stopped. Ctrl-C lets the
current article finish (text + images) before stopping; press it twice to stop immediately.

A plain `sync` only sees Instapaper's change feed, which in practice misses bookmarks saved before
about 2025. `--full-import` reads every list to get them, at the cost of many API calls, so use it for
the first run if you want the whole account, then weekly at most. If it gets rate limited, the next
`--full-import` resumes it; plain syncs in between don't touch it.

Repeat `export` runs only write files that changed. The
export never deletes anything, so to drop articles you deleted, unliked or moved, delete the
export directory and export again.

Data lives in `~/.local/share/ipdump/` (`instapaper.db` and `images/`); override with `--data DIR`.
`--delay LO HI` changes the wait between Instapaper API calls; web pages always wait 1–3 s and images 50–250 ms.

## Limits of the web download

- A soft paywall with a long teaser and no paywall marker is saved as complete, so Instapaper is never
  asked for that article.
- An article under 150 words costs one API call, because it might be a stub. The longer version is kept.
- Pages that need JavaScript to render use Instapaper's copy, or are recorded as failed if it has none.
- Many large publishers return 403 despite the browser `User-Agent`. Those use Instapaper's copy too, so
  a high block rate means more API calls.
- An article that neither source can provide is not retried.
- Author names on web articles come from page metadata and are sometimes wrong.

## Development

```sh
uv run pytest
```
