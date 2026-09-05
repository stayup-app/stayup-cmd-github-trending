# stayup-cmd-github-trending

[![CI](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/ci.yml)
[![Daily GitHub Trending](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/daily.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/daily.yml)

**Website:** https://stayup-ui.vercel.app

Scrapes [github.com/trending](https://github.com/trending) and stores the ranked list of
repositories via [stayup-api](https://github.com/stayup-app/stayup-api) — this script never touches
a database directly, it only calls `stayup-api`'s `/connector-api/github_trending/*` endpoints — one
entry per time window.

## How it works

`github.com/trending` has no public API, so each window is a plain HTML page scraped on every run.
Three sources are tracked and seeded automatically on startup:

- `https://github.com/trending?since=daily`
- `https://github.com/trending?since=weekly`
- `https://github.com/trending?since=monthly`

On each run, for every window:

1. Fetch the trending page and parse each `article.Box-row` (rank, owner, name, URL, description,
   language, total stars, total forks, stars gained in the window).
2. **Replace** the stored snapshot for that window: the previous entry is deleted
   (`DELETE .../old-items?retentionDays=0`, which purges everything already stored) and a fresh one
   inserted.

So this provider always holds **exactly one entry per window** — three total — each refreshed on
every execution. If a window fails to fetch, its previous snapshot is kept and the error is logged
via the API; the run never crashes.

The window a source tracks is read from its `repository.url` (`?since=…`); `config.since` is only a
fallback. So a source added through the app — where the display template's `form` block (see below)
asks for a single word, **`daily` / `weekly` / `monthly`**, instead of the full URL — works even
though the app stores no `config`.

> Unlike the other `stayup-cmd-*` collectors, this one keeps no history: a trending list is a full
> snapshot that is entirely replaced each day.

### Display template

On every run this collector registers a **display template** (`DISPLAY_TEMPLATE` in
`fetch_trending.py`) via `POST /connector-api/github_trending/register`. `stayup-api` relays it
verbatim on `GET /connectors/providers`, and the 3 client apps (`stayup-ui`, `stayup-desktop`,
`stayup-mobile`) render this connector's feed straight from it — **no per-connector code
in any app**. One entry is one trending window, so the feed entry summarises the window
(`GitHub Trending — today · N repositories`) and the reading pane is a `mode: table` view over the
embedded `repos` list, mirroring [github.com/trending](https://github.com/trending): rank ·
`owner/name` (linked) · description · language · stars · forks · stars this period.

The template also carries a `form` block, so the app's "add a flux" dialog shows a **single field**
for this provider: type `daily`, `weekly` or `monthly` (or paste a full `…/trending?since=…` URL) and
the app builds the source URL itself. The feed's sidebar label (`feedLabel`) is derived from that
URL, so it reads `daily` / `weekly` / `monthly` whether the source was seeded or added in-app.

The template shape is documented in
[`stayup-api/docs/display-templates.md`](https://github.com/stayup-app/stayup-api/blob/main/docs/display-templates.md).

### `content` JSON format

```json
{
  "since": "daily",
  "url": "https://github.com/trending?since=daily",
  "count": 25,
  "fetched_at": "2026-08-28T00:00:03+00:00",
  "repos": [
    {
      "rank": 1,
      "owner": "octocat",
      "name": "hello-world",
      "full_name": "octocat/hello-world",
      "url": "https://github.com/octocat/hello-world",
      "description": "My first repository on GitHub!",
      "language": "Python",
      "stars": 12345,
      "forks": 678,
      "stars_period": 456
    }
  ]
}
```

## Requirements

- Python 3.13, or [Docker](https://www.docker.com/)
- A `stayup-api` instance (the public one, or your own — see [self-hosting-and-providers.md](https://github.com/stayup-app/stayup-api/blob/main/docs/self-hosting-and-providers.md))
- An API key for the `github_trending` provider, created from that instance's admin panel (Connector keys → New key, provider `github_trending`). The key is shown once — copy it right away.

## Setup

### With Docker (recommended)

```bash
cp .env.example .env
# Fill in STAYUP_API_URL and STAYUP_API_KEY

docker compose run --rm fetch_trending
```

### Without Docker

```bash
pip install -r requirements.txt
STAYUP_API_URL=... STAYUP_API_KEY=... python fetch_trending.py
```

> **Note:** the provider registers itself and seeds its three windows automatically on every run —
> nothing to create by hand beyond the key.

## Automation

`.github/workflows/daily.yml` runs every day at **00:00 UTC** (also triggerable manually from
**Actions → Daily GitHub Trending → Run workflow**).

Required secrets — `STAYUP_API_URL` and `STAYUP_API_KEY` — configured in
**Settings → Secrets and variables → Actions → New repository secret**, or:

```bash
gh secret set STAYUP_API_URL -R stayup-app/stayup-cmd-github-trending
gh secret set STAYUP_API_KEY -R stayup-app/stayup-cmd-github-trending
```

`.github/workflows/ci.yml` runs on every push and pull request to `main`: **ruff** + **black**
lint, then the test suite. The build fails if line coverage of `fetch_trending.py` drops below
**100%** (`--cov-fail-under=100`, set in `pyproject.toml`) — `stayup-api` and network calls are
mocked, so the tests need neither a database nor real network access.

## Development

```bash
# Install the pre-commit hook (runs linter + tests before every commit)
cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

# Lint + tests via Docker
docker compose run --rm --entrypoint="" test sh -c "ruff check . && black --check ."
docker compose run --rm test
```

Run the suite directly:

```bash
pip install -r requirements-dev.txt
pytest tests/ -v     # coverage report + 100% gate come from pyproject.toml
```
