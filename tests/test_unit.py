"""Unit tests — no external dependencies. stayup-api itself is mocked
(unittest.mock.patch on `requests.request`); its actual behavior is covered
by stayup-api's own test suite. github.com/trending's HTML is mocked too."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from fetch_trending import (
    DISPLAY_TEMPLATE,
    SOURCE_URLS,
    _parse_int,
    build_content,
    ensure_sources,
    fetch_trending,
    get_sources,
    main,
    process_repository,
    register_provider,
    replace_entry,
    save_error,
    window_from_url,
)

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "trending.html").read_text(encoding="utf-8")


def mock_response(json_body=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.content = b"{}" if json_body is not None else b""
    response.json.return_value = json_body
    response.raise_for_status.return_value = None
    return response


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
    def test_skips_rows_without_a_repository_link(self, mock_get):
        mock_get.return_value.text = (
            "<html><body>"
            '<article class="Box-row"><div>promo, no link</div></article>'
            '<article class="Box-row"><h2><a href="/octocat/hello">octocat / hello</a></h2></article>'
            "</body></html>"
        )
        mock_get.return_value.raise_for_status = MagicMock()

        repos = fetch_trending("https://github.com/trending")

        assert [r["full_name"] for r in repos] == ["octocat/hello"]
        assert repos[0]["rank"] == 2  # enumerate keeps the row's position

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
# window_from_url
# ---------------------------------------------------------------------------


class TestWindowFromUrl:
    def test_extracts_each_window(self):
        assert window_from_url("https://github.com/trending?since=daily") == "daily"
        assert window_from_url("https://github.com/trending?since=weekly") == "weekly"
        assert window_from_url("https://github.com/trending?since=monthly") == "monthly"

    def test_reads_since_among_other_query_params(self):
        assert window_from_url("https://github.com/trending?spoken_language_code=en&since=weekly") == "weekly"

    def test_none_when_absent_or_unknown(self):
        assert window_from_url("https://github.com/trending") is None
        assert window_from_url("https://github.com/trending?since=yearly") is None


# ---------------------------------------------------------------------------
# stayup-api client
# ---------------------------------------------------------------------------


@patch("fetch_trending.API_KEY", "test-key")
class TestRegisterProvider:
    @patch("fetch_trending.requests.request")
    def test_posts_display_name_sort_order_and_template(self, mock_request):
        mock_request.return_value = mock_response()
        register_provider()
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert url.endswith("/connector-api/github_trending/register")
        body = mock_request.call_args.kwargs["json"]
        assert body["displayName"] == "GitHub Trending"
        assert body["sortOrder"] == 50
        assert body["template"] == DISPLAY_TEMPLATE


class TestApiRequestWithoutKey:
    @patch("fetch_trending.API_KEY", None)
    def test_raises_when_no_api_key_is_configured(self):
        with pytest.raises(RuntimeError, match="STAYUP_API_KEY"):
            register_provider()


@patch("fetch_trending.API_KEY", "test-key")
class TestEnsureSources:
    @patch("fetch_trending.requests.request")
    def test_posts_the_three_windows(self, mock_request):
        mock_request.return_value = mock_response({"id": 1, "url": "u"})
        ensure_sources()
        assert mock_request.call_count == 3
        urls = {c.kwargs["json"]["url"] for c in mock_request.call_args_list}
        assert urls == set(SOURCE_URLS)
        for c in mock_request.call_args_list:
            assert c.args[0] == "POST"
            assert c.args[1].endswith("/connector-api/github_trending/sources")


@patch("fetch_trending.API_KEY", "test-key")
class TestGetSources:
    @patch("fetch_trending.requests.request")
    def test_returns_id_url_config_tuples(self, mock_request):
        mock_request.return_value = mock_response(
            {"sources": [{"id": 1, "url": "https://github.com/trending?since=daily", "config": {}}]}
        )
        assert get_sources() == [(1, "https://github.com/trending?since=daily", {})]


@patch("fetch_trending.API_KEY", "test-key")
class TestReplaceEntry:
    @patch("fetch_trending.requests.request")
    def test_deletes_everything_then_inserts(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime.now(tz=timezone.utc)
        replace_entry(3, "daily@2026-08-28", "{}", executed_at, executed_at)

        assert mock_request.call_count == 2
        delete_call, insert_call = mock_request.call_args_list
        assert delete_call.args[0] == "DELETE"
        assert delete_call.args[1].endswith("/connector-api/github_trending/sources/3/old-items")
        assert delete_call.kwargs["params"] == {"retentionDays": 0}
        assert insert_call.args[0] == "POST"
        assert insert_call.args[1].endswith("/connector-api/github_trending/items")

    @patch("fetch_trending.requests.request")
    def test_insert_item_fields(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)
        replace_entry(7, "weekly@2026-08-28", '{"x": 1}', executed_at, executed_at)

        item = mock_request.call_args_list[1].kwargs["json"]["items"][0]
        assert item == {
            "repositoryId": 7,
            "version": "weekly@2026-08-28",
            "content": '{"x": 1}',
            "datetime": executed_at.isoformat(),
            "executedAt": executed_at.isoformat(),
            "success": True,
        }


@patch("fetch_trending.API_KEY", "test-key")
class TestSaveError:
    @patch("fetch_trending.requests.request")
    def test_posts_the_error(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime.now(tz=timezone.utc)
        save_error(5, "boom", executed_at)
        body = mock_request.call_args.kwargs["json"]
        assert body == {"repositoryId": 5, "error": "boom", "executedAt": executed_at.isoformat()}

    @patch("fetch_trending.requests.request")
    def test_accepts_none_repository_id(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        save_error(None, "network error", datetime.now(tz=timezone.utc))
        assert mock_request.call_args.kwargs["json"]["repositoryId"] is None


class TestDisplayTemplate:
    def test_round_trips_through_json_unchanged(self):
        assert json.loads(json.dumps(DISPLAY_TEMPLATE)) == DISPLAY_TEMPLATE

    def test_ships_a_self_describing_icon(self):
        icon = DISPLAY_TEMPLATE["display"]["icon"]
        assert isinstance(icon, dict)
        assert icon["paths"]
        assert all(p[:1] in ("M", "m") for p in icon["paths"])
        assert icon["viewBox"] == "0 0 24 24"

    def test_declares_a_table_detail_over_the_repos_list(self):
        assert DISPLAY_TEMPLATE["version"] == 1
        assert DISPLAY_TEMPLATE["display"]["name"] == "GitHub Trending"
        assert DISPLAY_TEMPLATE["item"]["parseContentAsJson"] is True
        detail = DISPLAY_TEMPLATE["detail"]
        assert detail["mode"] == "table"
        assert detail["collection"] == "repos"
        labels = [c["label"] for c in detail["columns"]]
        assert labels == ["#", "Repository", "Description", "Language", "Stars", "Forks", "This period"]
        assert all("field" in c for c in detail["columns"])

    def test_every_interpolation_token_is_produced_by_the_connector(self):
        # Tokens used in {template} strings must resolve against the stored content
        # (see build_content) or the `vars` aliases — never a field the app can't fill.
        # `form` is excluded: its {value} is the user's form input, filled by stayup-ui.
        produced = {"since", "url", "count", "fetched_at", "repos", "window"}
        content_keys = {
            "rank",
            "owner",
            "name",
            "full_name",
            "url",
            "description",
            "language",
            "stars",
            "forks",
            "stars_period",
        }
        blob = json.dumps({k: v for k, v in DISPLAY_TEMPLATE.items() if k != "form"})
        tokens = set(re.findall(r"\{(\w+)\}", blob))
        assert tokens <= produced | content_keys

    def test_feed_label_maps_each_window_url_to_its_word(self):
        # Le libellé du flux se lit sur l'URL (pas config.since) pour rester juste
        # même quand le flux a été ajouté via `form`, qui ne pose que l'URL.
        cases = DISPLAY_TEMPLATE["display"]["feedLabel"]["cases"]
        assert cases == {
            "https://github.com/trending?since=daily": "daily",
            "https://github.com/trending?since=weekly": "weekly",
            "https://github.com/trending?since=monthly": "monthly",
        }

    def test_add_flux_form_takes_a_single_window_word(self):
        form = DISPLAY_TEMPLATE["form"]
        assert form["urlTemplate"] == "https://github.com/trending?since={value}"
        assert re.match(form["pattern"], "weekly")
        assert re.match(form["pattern"], "https://github.com/trending?since=weekly") is None
        # chaque mot du formulaire reconstruit exactement l'URL d'une source seedée
        for word in ("daily", "weekly", "monthly"):
            assert form["urlTemplate"].replace("{value}", word) in SOURCE_URLS


# ---------------------------------------------------------------------------
# process_repository — end to end, stayup-api and network mocked
# ---------------------------------------------------------------------------


@patch("fetch_trending.API_KEY", "test-key")
class TestProcessRepository:
    @patch("fetch_trending.save_error")
    @patch("fetch_trending.replace_entry")
    @patch("fetch_trending.fetch_trending")
    def test_success_replaces_entry(self, mock_fetch, mock_replace, mock_save_error):
        mock_fetch.return_value = [{"rank": 1, "full_name": "a/b"}]
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)

        process_repository(1, "https://github.com/trending?since=daily", executed_at, {"since": "daily"})

        mock_replace.assert_called_once()
        assert mock_replace.call_args[0][1] == "daily@2026-08-28"
        mock_save_error.assert_not_called()

    @patch("fetch_trending.save_error")
    @patch("fetch_trending.replace_entry")
    @patch("fetch_trending.fetch_trending")
    def test_failure_is_logged_not_raised(self, mock_fetch, mock_replace, mock_save_error):
        mock_fetch.side_effect = RuntimeError("rate limited")
        executed_at = datetime.now(tz=timezone.utc)

        process_repository(1, "https://github.com/trending", executed_at, {"since": "daily"})

        mock_replace.assert_not_called()
        mock_save_error.assert_called_once_with(1, "rate limited", executed_at)

    @patch("fetch_trending.replace_entry")
    @patch("fetch_trending.fetch_trending")
    def test_defaults_to_daily_when_since_missing(self, mock_fetch, mock_replace):
        mock_fetch.return_value = [{"rank": 1, "full_name": "a/b"}]
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)
        process_repository(1, "https://github.com/trending", executed_at, {})
        assert mock_replace.call_args[0][1] == "daily@2026-08-28"

    @patch("fetch_trending.replace_entry")
    @patch("fetch_trending.fetch_trending")
    def test_derives_window_from_url_when_config_has_no_since(self, mock_fetch, mock_replace):
        # Cas d'un flux ajouté via le formulaire `form` : config vide, fenêtre dans l'URL.
        mock_fetch.return_value = [{"rank": 1, "full_name": "a/b"}]
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)
        process_repository(1, "https://github.com/trending?since=weekly", executed_at, {})
        assert mock_replace.call_args[0][1] == "weekly@2026-08-28"

    @patch("fetch_trending.replace_entry")
    @patch("fetch_trending.fetch_trending")
    def test_falls_back_to_config_since_when_url_has_no_window(self, mock_fetch, mock_replace):
        mock_fetch.return_value = [{"rank": 1, "full_name": "a/b"}]
        executed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)
        process_repository(1, "https://github.com/trending", executed_at, {"since": "monthly"})
        assert mock_replace.call_args[0][1] == "monthly@2026-08-28"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    @patch("fetch_trending.process_repository")
    @patch("fetch_trending.get_sources")
    @patch("fetch_trending.ensure_sources")
    @patch("fetch_trending.register_provider")
    def test_processes_every_source(self, mock_register, mock_seed, mock_get, mock_process):
        mock_get.return_value = [
            (1, "https://github.com/trending?since=daily", {"since": "daily"}),
            (2, "https://github.com/trending?since=weekly", {"since": "weekly"}),
        ]

        main()

        mock_register.assert_called_once()
        mock_seed.assert_called_once()
        assert mock_process.call_count == 2
        assert mock_process.call_args_list[0] == call(
            1, "https://github.com/trending?since=daily", ANY, {"since": "daily"}
        )
