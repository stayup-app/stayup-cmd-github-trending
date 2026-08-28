"""
Functional tests — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

These tests TRUNCATE the repository / connector_github_trending / log tables,
so never point them at a production database — use a throwaway PostgreSQL.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from fetch_trending import (
    DISPLAY_TEMPLATE,
    ensure_sources,
    get_repositories,
    init_db,
    process_repository,
    replace_entry,
)


def make_conn():
    try:
        return psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            dbname=os.environ.get("DB_NAME", "stayup"),
            user=os.environ.get("DB_USER", "stayup"),
            password=os.environ.get("DB_PASSWORD", "stayup"),
        )
    except psycopg2.OperationalError as e:
        pytest.skip(f"PostgreSQL unavailable: {e}")


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    conn = make_conn()
    init_db(conn)
    conn.close()


@pytest.fixture
def db_conn():
    conn = make_conn()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE connector_github_trending, log, repository RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


def fake_repos(n=3):
    return [{"rank": i, "full_name": f"owner{i}/repo{i}", "stars": i * 100} for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# init_db — provider registration
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_registers_name_and_display_template(self, db_conn):
        init_db(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT display_name, sort_order, template FROM provider_registry WHERE name = 'github_trending'"
            )
            display_name, sort_order, template = cur.fetchone()
        assert display_name == "GitHub Trending"
        assert sort_order == 50
        # psycopg2 returns a JSONB column already decoded.
        assert template == DISPLAY_TEMPLATE

    def test_is_idempotent(self, db_conn):
        init_db(db_conn)
        init_db(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM provider_registry WHERE name = 'github_trending'")
            assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# ensure_sources
# ---------------------------------------------------------------------------


class TestEnsureSources:
    def test_creates_exactly_three_windows(self, db_conn):
        ensure_sources(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("SELECT url, config->>'since' FROM repository WHERE type = 'github_trending' ORDER BY id")
            rows = cur.fetchall()
        assert [r[1] for r in rows] == ["daily", "weekly", "monthly"]

    def test_is_idempotent(self, db_conn):
        ensure_sources(db_conn)
        ensure_sources(db_conn)
        ensure_sources(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repository WHERE type = 'github_trending'")
            assert cur.fetchone()[0] == 3


class TestGetRepositories:
    def test_returns_the_three_seeded_sources(self, db_conn):
        ensure_sources(db_conn)
        repos = get_repositories(db_conn)
        assert len(repos) == 3
        assert repos[0][2] == {"since": "daily"}
        assert all(url.startswith("https://github.com/trending?since=") for _, url, _ in repos)


# ---------------------------------------------------------------------------
# replace_entry
# ---------------------------------------------------------------------------


class TestReplaceEntry:
    def _seed_one(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO repository (url, type, config) VALUES (%s, 'github_trending', %s) RETURNING id",
                ("https://github.com/trending?since=daily", json.dumps({"since": "daily"})),
            )
            repo_id = cur.fetchone()[0]
        conn.commit()
        return repo_id

    def test_first_call_inserts_one_row(self, db_conn):
        repo_id = self._seed_one(db_conn)
        now = datetime.now(tz=timezone.utc)
        replace_entry(db_conn, repo_id, "daily@2026-08-28", '{"count": 1}', now, now)
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_github_trending WHERE repository_id = %s", (repo_id,))
            assert cur.fetchone()[0] == 1

    def test_second_call_replaces_in_place(self, db_conn):
        repo_id = self._seed_one(db_conn)
        old = datetime.now(tz=timezone.utc) - timedelta(days=1)
        new = datetime.now(tz=timezone.utc)
        replace_entry(db_conn, repo_id, "daily@2026-08-27", '{"count": 1}', old, old)
        replace_entry(db_conn, repo_id, "daily@2026-08-28", '{"count": 2}', new, new)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT version, content FROM connector_github_trending WHERE repository_id = %s",
                (repo_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "daily@2026-08-28"
        assert rows[0][1] == '{"count": 2}'


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_run_over_three_sources_yields_exactly_three_rows(self, db_conn, monkeypatch):
        monkeypatch.setattr("fetch_trending.fetch_trending", lambda url: fake_repos(5))
        ensure_sources(db_conn)
        executed_at = datetime.now(tz=timezone.utc)

        for repo_id, url, config in get_repositories(db_conn):
            process_repository(db_conn, repo_id, url, executed_at, config)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_github_trending")
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT COUNT(DISTINCT repository_id) FROM connector_github_trending")
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT DISTINCT (content::json->>'count') FROM connector_github_trending")
            assert cur.fetchone()[0] == "5"

    def test_second_run_keeps_it_at_three_rows(self, db_conn, monkeypatch):
        monkeypatch.setattr("fetch_trending.fetch_trending", lambda url: fake_repos(3))
        ensure_sources(db_conn)

        for _ in range(2):
            executed_at = datetime.now(tz=timezone.utc)
            for repo_id, url, config in get_repositories(db_conn):
                process_repository(db_conn, repo_id, url, executed_at, config)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_github_trending")
            assert cur.fetchone()[0] == 3

    def test_failure_keeps_previous_snapshot_and_logs(self, db_conn, monkeypatch):
        ensure_sources(db_conn)
        repo_id, url, config = get_repositories(db_conn)[0]
        good = datetime.now(tz=timezone.utc) - timedelta(days=1)
        replace_entry(db_conn, repo_id, "daily@old", '{"count": 9}', good, good)

        def boom(_url):
            raise RuntimeError("rate limited")

        monkeypatch.setattr("fetch_trending.fetch_trending", boom)
        process_repository(db_conn, repo_id, url, datetime.now(tz=timezone.utc), config)

        with db_conn.cursor() as cur:
            cur.execute("SELECT version, content FROM connector_github_trending WHERE repository_id = %s", (repo_id,))
            row = cur.fetchone()
            assert row == ("daily@old", '{"count": 9}')
            cur.execute("SELECT error FROM log WHERE repository_id = %s", (repo_id,))
            assert "rate limited" in cur.fetchone()[0]
