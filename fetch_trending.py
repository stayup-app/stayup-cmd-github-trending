#!/usr/bin/env python3
"""
Stayup — scrapes GitHub's trending repositories and stores them in PostgreSQL.

Three sources are tracked, one per trending window:
  https://github.com/trending?since=daily
  https://github.com/trending?since=weekly
  https://github.com/trending?since=monthly

github.com/trending has no public API, so each window is a plain HTML page
scraped on every run. Each run fully replaces the stored snapshot for every
window, so connector_github_trending always holds exactly three rows — one
ranked list of repositories per window, refreshed on every execution.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import psycopg2
import requests
from bs4 import BeautifulSoup

DDL = """
CREATE TABLE IF NOT EXISTS repository (
    id          SERIAL PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    config      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS connector_github_trending (
    id          SERIAL PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repository(id),
    version     TEXT,
    content     TEXT NOT NULL,
    datetime    TIMESTAMPTZ,
    executed_at TIMESTAMPTZ NOT NULL,
    success     BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id          SERIAL PRIMARY KEY,
    repository_id  INTEGER,
    error       TEXT NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL
);

-- Registre partagé des providers : chaque collecteur y déclare son nom affiché au
-- démarrage. L'API stayup-api lit cette table pour construire une UI dynamique ;
-- elle ne connaît aucun nom de provider en dur, seulement les tables connector_*.
CREATE TABLE IF NOT EXISTS provider_registry (
    name          TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 100,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO provider_registry (name, display_name, sort_order)
VALUES ('github_trending', 'GitHub Trending', 50)
ON CONFLICT (name) DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = NOW();
"""

PROVIDER_TYPE = "github_trending"

# The three tracked trending windows. Seeded automatically on every run.
SOURCES = [
    ("https://github.com/trending?since=daily", {"since": "daily"}),
    ("https://github.com/trending?since=weekly", {"since": "weekly"}),
    ("https://github.com/trending?since=monthly", {"since": "monthly"}),
]

PERIOD_STARS_RE = re.compile(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_db_conn() -> psycopg2.extensions.connection:
    """Return a psycopg2 connection.

    Reads DATABASE_URL first; falls back to individual DB_* environment
    variables (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def init_db(conn: psycopg2.extensions.connection) -> None:
    """Create tables if they don't exist and register the provider."""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def ensure_sources(conn: psycopg2.extensions.connection) -> None:
    """Insert the three tracked trending windows if they are not present yet."""
    with conn.cursor() as cur:
        for url, config in SOURCES:
            cur.execute(
                """
                INSERT INTO repository (url, type, config)
                VALUES (%s, %s, %s)
                ON CONFLICT (url) DO NOTHING
                """,
                (url, PROVIDER_TYPE, json.dumps(config)),
            )
    conn.commit()


def get_repositories(conn: psycopg2.extensions.connection) -> list[tuple[int, str, dict]]:
    """Return tracked repositories of type 'github_trending' as (id, url, config) tuples."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, url, config FROM repository WHERE type = %s ORDER BY id",
            (PROVIDER_TYPE,),
        )
        rows = cur.fetchall()
        return [(row[0], row[1], json.loads(row[2]) if isinstance(row[2], str) else (row[2] or {})) for row in rows]


def replace_entry(
    conn: psycopg2.extensions.connection,
    repository_id: int,
    version: str,
    content: str,
    entry_datetime: datetime,
    executed_at: datetime,
) -> None:
    """Replace the stored snapshot for one window — keeps exactly one row per repository."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM connector_github_trending WHERE repository_id = %s", (repository_id,))
        cur.execute(
            """
            INSERT INTO connector_github_trending
                (repository_id, version, content, datetime, executed_at, success)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            """,
            (repository_id, version, content, entry_datetime, executed_at),
        )
    conn.commit()


def save_error(
    conn: psycopg2.extensions.connection,
    repository_id: int | None,
    error: str,
    executed_at: datetime,
) -> None:
    """Persist a per-source failure to the log table."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO log (repository_id, error, executed_at) VALUES (%s, %s, %s)",
            (repository_id, error, executed_at),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def _parse_int(text: str | None) -> int | None:
    """Return the integer value of a comma-grouped number string, or None."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_repo_row(row, rank: int) -> dict | None:
    """Extract one repository from an <article class="Box-row"> element."""
    link = row.select_one("h2 a")
    if link is None or not link.get("href"):
        return None
    full_name = link["href"].strip("/")
    owner, _, name = full_name.partition("/")

    description_el = row.select_one("p")
    language_el = row.select_one('[itemprop="programmingLanguage"]')
    stars_el = row.select_one('a[href$="/stargazers"]')
    forks_el = row.select_one('a[href$="/forks"]')

    period_el = row.select_one("span.d-inline-block.float-sm-right")
    period_text = period_el.get_text(" ", strip=True) if period_el else row.get_text(" ", strip=True)
    period_match = PERIOD_STARS_RE.search(period_text)

    return {
        "rank": rank,
        "owner": owner,
        "name": name,
        "full_name": full_name,
        "url": f"https://github.com/{full_name}",
        "description": description_el.get_text(strip=True) if description_el else None,
        "language": language_el.get_text(strip=True) if language_el else None,
        "stars": _parse_int(stars_el.get_text() if stars_el else None),
        "forks": _parse_int(forks_el.get_text() if forks_el else None),
        "stars_period": _parse_int(period_match.group(1)) if period_match else None,
    }


def fetch_trending(url: str) -> list[dict]:
    """Fetch a github.com/trending page and return its ranked list of repositories.

    Raises RuntimeError if the page contains no repository rows (layout change or
    rate limiting) so the failure is logged per source instead of silently
    storing an empty list.
    """
    resp = requests.get(url, timeout=30, headers={"User-Agent": "stayup-github-trending/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    repos = []
    for rank, row in enumerate(soup.select("article.Box-row"), start=1):
        repo = _parse_repo_row(row, rank)
        if repo is not None:
            repos.append(repo)

    if not repos:
        raise RuntimeError("No trending repositories found")
    return repos


def build_content(since: str, url: str, repos: list[dict], executed_at: datetime) -> str:
    """Serialize a trending snapshot to the JSON string stored in `content`."""
    return json.dumps(
        {
            "since": since,
            "url": url,
            "count": len(repos),
            "fetched_at": executed_at.isoformat(),
            "repos": repos,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def process_repository(
    conn: psycopg2.extensions.connection,
    repository_id: int,
    repository_url: str,
    executed_at: datetime,
    config: dict,
) -> None:
    """Refresh the stored snapshot for one trending window.

    On success the previous row is replaced. On any failure the previous snapshot
    is kept and the error is written to the `log` table — the run never crashes.
    """
    since = config.get("since", "daily")
    try:
        repos = fetch_trending(repository_url)
        content = build_content(since, repository_url, repos, executed_at)
        version = f"{since}@{executed_at:%Y-%m-%d}"
        replace_entry(conn, repository_id, version, content, executed_at, executed_at)
    except Exception as e:
        save_error(conn, repository_id, str(e), executed_at)
        print(f"[{repository_url}] Error: {e}", file=sys.stderr)


def main() -> None:
    conn = get_db_conn()
    try:
        init_db(conn)
        ensure_sources(conn)

        executed_at = datetime.now(tz=timezone.utc)
        for repository_id, repository_url, config in get_repositories(conn):
            process_repository(conn, repository_id, repository_url, executed_at, config)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
