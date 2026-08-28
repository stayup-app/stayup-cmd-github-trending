# stayup-cmd-github-trending

[![CI](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/ci.yml)
[![Daily GitHub Trending](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/daily.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-github-trending/actions/workflows/daily.yml)

**Website:** https://stayup-ui.vercel.app

Scrapes [github.com/trending](https://github.com/trending) and stores the ranked list of
repositories in PostgreSQL, one row per time window.

## How it works

`github.com/trending` has no public API, so each window is a plain HTML page scraped on every run.
Three sources are tracked and seeded automatically on startup:

| `repository.url`                                | `repository.config`   |
|------------------------------------------------|-----------------------|
| `https://github.com/trending?since=daily`       | `{"since": "daily"}`   |
| `https://github.com/trending?since=weekly`      | `{"since": "weekly"}`  |
| `https://github.com/trending?since=monthly`     | `{"since": "monthly"}` |

On each run, for every window:

1. Fetch the trending page and parse each `article.Box-row` (rank, owner, name, URL, description,
   language, total stars, total forks, stars gained in the window).
2. **Replace** the stored snapshot for that window: the previous `connector_github_trending` row is
   deleted and a fresh one inserted.

So `connector_github_trending` always holds **exactly three rows** — one per window — each refreshed
on every execution. If a window fails to fetch, its previous snapshot is kept and the error is
written to the `log` table; the run never crashes.

> Unlike the other `stayup-cmd-*` collectors, this one keeps no history and ignores
> `config.retention_days`: a trending list is a full snapshot that is entirely replaced each day.

## Database schema

```
repository                       -- shared, seeded by this collector (type = 'github_trending')
  id          SERIAL PK
  url         TEXT UNIQUE
  type        TEXT               -- 'github_trending'
  config      JSONB              -- {"since": "daily" | "weekly" | "monthly"}

connector_github_trending
  id            SERIAL PK
  repository_id → repository.id  -- exactly one row per source
  version       TEXT             -- e.g. "daily@2026-08-28"
  content       TEXT             -- JSON snapshot (see below)
  datetime      TIMESTAMPTZ      -- snapshot time (= executed_at)
  executed_at   TIMESTAMPTZ
  success       BOOLEAN

log
  id            SERIAL PK
  repository_id → repository.id
  error         TEXT
  executed_at   TIMESTAMPTZ

provider_registry               -- shared; this collector upserts exactly its own row
  name          TEXT PK          -- 'github_trending'
  display_name  TEXT             -- 'GitHub Trending'
  sort_order    INTEGER
  template      JSONB            -- display manifest (see below)
```

### Display template

On every run this collector upserts a **display template** into `provider_registry.template`
(`DISPLAY_TEMPLATE` in `fetch_trending.py`). `stayup-api` relays it verbatim on
`GET /connectors/providers`, and the 3 client apps (`stayup-ui`, `stayup-desktop`,
`stayup-mobile`) render this connector's feed straight from it — **no per-connector code
in any app**. One `connector_github_trending` row is one trending window, so the feed
entry summarises the window (`GitHub Trending — today · N repositories`) and the reading
pane is a `mode: table` view over the embedded `repos` list, mirroring
[github.com/trending](https://github.com/trending): rank · `owner/name` (linked) ·
description · language · stars · forks · stars this period.

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

## Setup

### With Docker (recommended)

```bash
cp .env.example .env
# Fill in DB_NAME, DB_USER, DB_PASSWORD

docker compose up db -d
docker compose run --rm fetch_trending
```

### Without Docker

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:password@host:5432/dbname
python fetch_trending.py
```

Tables are created automatically on the first run.

## Automation

`.github/workflows/daily.yml` runs every day at **00:00 UTC** (also triggerable manually from
**Actions → Daily GitHub Trending → Run workflow**).

Required secret:

- `DATABASE_URL` — connection string to your production database.

Configure it in **Settings → Secrets and variables → Actions → New repository secret**, or:

```bash
gh secret set DATABASE_URL -R stayup-app/stayup-cmd-github-trending
```

`.github/workflows/ci.yml` runs on every push and pull request to `main`: **ruff** + **black**
lint, then the unit and functional test suite against a temporary PostgreSQL service. The build
fails if line coverage of `fetch_trending.py` drops below **100%** (`--cov-fail-under=100`, set in
`pyproject.toml`); the unit tests alone reach 100%, the functional tests add real-database checks.

## Development

```bash
# Install the pre-commit hook (runs linter + tests before every commit)
cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

# Lint + tests via Docker
docker compose run --rm --entrypoint="" test sh -c "ruff check . && black --check ."
docker compose run --rm test
```

Run the suite directly (needs a PostgreSQL for the functional tests; the unit tests need neither
a database nor network and already give 100% coverage):

```bash
pip install -r requirements-dev.txt
pytest tests/ -v                 # coverage report + 100% gate come from pyproject.toml
pytest tests/test_unit.py -v     # unit only, no PostgreSQL required
```
