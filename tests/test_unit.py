"""Unit tests — no external dependencies (DB, network)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fetch_trending import (
    _parse_int,
    build_content,
    ensure_sources,
    fetch_trending,
    get_repositories,
    init_db,
    process_repository,
    replace_entry,
    save_error,
)

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "trending.html").read_text(encoding="utf-8")


def make_conn_mock():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


# ---------------------------------------------------------------------------
# _parse_int
# ---------------------------------------------------------------------------


class TestParseInt:
    def test_strips_commas(self):
        assert _parse_int("9,491") == 9491

    def test_extracts_leading_number_from_sentence(self):
        assert _parse_int("1,984 stars today") == 1984

    def test_none_input(self):
        assert _parse_int(None) is None

    def test_empty_or_non_numeric(self):
        assert _parse_int("") is None
        assert _parse_int("   ") is None


# ---------------------------------------------------------------------------
# fetch_trending
# ---------------------------------------------------------------------------


class TestFetchTrending:
    @patch("fetch_trending.requests.get")
    def test_parses_every_row(self, mock_get):
        mock_get.return_value.text = FIXTURE_HTML
        mock_get.return_value.raise_for_status = MagicMock()

        repos = fetch_trending("https://github.com/trending?since=daily")

        assert [r["rank"] for r in repos] == [1, 2, 3]
        assert [r["full_name"] for r in repos] == [
            "bilawalsidhu/gods-eye-view",
            "zedeus/nitter",
            "TauricResearch/TradingAgents",
        ]

    @patch("fetch_trending.requests.get")
    def test_full_row_fields(self, mock_get):
        mock_get.return_value.text = FIXTURE_HTML
        mock_get.return_value.raise_for_status = MagicMock()

        repo = fetch_trending("https://github.com/trending")[0]

        assert repo["owner"] == "bilawalsidhu"
        assert repo["name"] == "gods-eye-view"
        assert repo["url"] == "https://github.com/bilawalsidhu/gods-eye-view"
        assert repo["description"].startswith("A spy satellite simulator")
        assert repo["language"] == "JavaScript"
        assert repo["stars"] == 9491
        assert repo["forks"] == 2017
        assert repo["stars_period"] == 1984

    @patch("fetch_trending.requests.get")
    def test_missing_description_and_language_become_none(self, mock_get):
        mock_get.return_value.text = FIXTURE_HTML
        mock_get.return_value.raise_for_status = MagicMock()

        repo = fetch_trending("https://github.com/trending")[1]

        assert repo["description"] is None
        assert repo["language"] is None
        assert repo["stars"] == 14012
        assert repo["stars_period"] == 7

    @patch("fetch_trending.requests.get")
    def test_weekly_period_counter(self, mock_get):
        mock_get.return_value.text = FIXTURE_HTML
        mock_get.return_value.raise_for_status = MagicMock()

        repo = fetch_trending("https://github.com/trending?since=weekly")[2]

        assert repo["language"] == "Python"
        assert repo["stars_period"] == 512

    @patch("fetch_trending.requests.get")
    def test_raises_when_no_rows(self, mock_get):
        mock_get.return_value.text = "<html><body><p>nothing here</p></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()

        with pytest.raises(RuntimeError, match="No trending repositories"):
            fetch_trending("https://github.com/trending")

    @patch("fetch_trending.requests.get")
    def test_propagates_http_error(self, mock_get):
        mock_get.return_value.raise_for_status.side_effect = Exception("429 Too Many Requests")

        with pytest.raises(Exception, match="429"):
            fetch_trending("https://github.com/trending")


# ---------------------------------------------------------------------------
# build_content
# ---------------------------------------------------------------------------


class TestBuildContent:
    def test_shape(self):
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)
        repos = [{"rank": 1, "full_name": "a/b"}, {"rank": 2, "full_name": "c/d"}]

        payload = json.loads(build_content("daily", "https://github.com/trending", repos, executed_at))

        assert payload["since"] == "daily"
        assert payload["url"] == "https://github.com/trending"
        assert payload["count"] == 2
        assert payload["fetched_at"] == "2026-08-28T00:00:00+00:00"
        assert payload["repos"] == repos

    def test_keeps_unicode_readable(self):
        repos = [{"rank": 1, "description": "café ☕"}]
        raw = build_content("daily", "u", repos, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert "café ☕" in raw


# ---------------------------------------------------------------------------
# DB helpers (mocked connection)
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_executes_ddl_and_commits(self):
        conn, cursor = make_conn_mock()
        init_db(conn)
        assert cursor.execute.call_count == 1
        conn.commit.assert_called_once()

    def test_ddl_defines_table_and_registers_provider(self):
        conn, cursor = make_conn_mock()
        init_db(conn)
        sql = cursor.execute.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS connector_github_trending" in sql
        assert "INSERT INTO provider_registry" in sql
        assert "'github_trending', 'GitHub Trending'" in sql


class TestEnsureSources:
    def test_inserts_the_three_windows(self):
        conn, cursor = make_conn_mock()
        ensure_sources(conn)
        assert cursor.execute.call_count == 3
        conn.commit.assert_called_once()
        urls = {call.args[1][0] for call in cursor.execute.call_args_list}
        assert urls == {
            "https://github.com/trending?since=daily",
            "https://github.com/trending?since=weekly",
            "https://github.com/trending?since=monthly",
        }

    def test_insert_is_idempotent_sql(self):
        conn, cursor = make_conn_mock()
        ensure_sources(conn)
        sql = cursor.execute.call_args[0][0]
        assert "ON CONFLICT (url) DO NOTHING" in sql


class TestGetRepositories:
    def test_filters_on_provider_type(self):
        conn, cursor = make_conn_mock()
        cursor.fetchall.return_value = []
        get_repositories(conn)
        sql, params = cursor.execute.call_args[0]
        assert "WHERE type = %s" in sql
        assert params == ("github_trending",)

    def test_returns_tuples_with_parsed_config(self):
        conn, cursor = make_conn_mock()
        cursor.fetchall.return_value = [
            (1, "https://github.com/trending?since=daily", '{"since": "daily"}'),
            (2, "https://github.com/trending?since=weekly", {"since": "weekly"}),
        ]
        result = get_repositories(conn)
        assert result[0] == (1, "https://github.com/trending?since=daily", {"since": "daily"})
        assert result[1] == (2, "https://github.com/trending?since=weekly", {"since": "weekly"})


class TestReplaceEntry:
    def test_deletes_then_inserts_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        replace_entry(conn, 3, "daily@2026-08-28", "{}", executed_at, executed_at)

        assert cursor.execute.call_count == 2
        delete_sql = cursor.execute.call_args_list[0].args[0]
        insert_sql = cursor.execute.call_args_list[1].args[0]
        assert delete_sql.strip().startswith("DELETE FROM connector_github_trending")
        assert "INSERT INTO connector_github_trending" in insert_sql
        assert "TRUE" in insert_sql
        conn.commit.assert_called_once()

    def test_insert_params(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)
        replace_entry(conn, 7, "weekly@2026-08-28", '{"x": 1}', executed_at, executed_at)
        params = cursor.execute.call_args_list[1].args[1]
        assert params == (7, "weekly@2026-08-28", '{"x": 1}', executed_at, executed_at)


class TestSaveError:
    def test_inserts_into_log_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        save_error(conn, 5, "boom", executed_at)
        sql, params = cursor.execute.call_args[0]
        assert "INSERT INTO log" in sql
        assert params == (5, "boom", executed_at)
        conn.commit.assert_called_once()

    def test_accepts_none_repository_id(self):
        conn, cursor = make_conn_mock()
        save_error(conn, None, "network error", datetime.now(tz=timezone.utc))
        assert cursor.execute.call_args[0][1][0] is None


# ---------------------------------------------------------------------------
# process_repository (mocked connection + network)
# ---------------------------------------------------------------------------


class TestProcessRepository:
    @patch("fetch_trending.fetch_trending")
    def test_success_replaces_entry(self, mock_fetch):
        mock_fetch.return_value = [{"rank": 1, "full_name": "a/b"}]
        conn, cursor = make_conn_mock()
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)

        process_repository(conn, 1, "https://github.com/trending?since=daily", executed_at, {"since": "daily"})

        insert_sql = cursor.execute.call_args_list[-1].args[0]
        assert "INSERT INTO connector_github_trending" in insert_sql
        version = cursor.execute.call_args_list[-1].args[1][1]
        assert version == "daily@2026-08-28"

    @patch("fetch_trending.fetch_trending")
    def test_failure_is_logged_not_raised(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("rate limited")
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)

        process_repository(conn, 1, "https://github.com/trending", executed_at, {"since": "daily"})

        sql = cursor.execute.call_args[0][0]
        assert "INSERT INTO log" in sql
        assert "rate limited" in cursor.execute.call_args[0][1][1]
