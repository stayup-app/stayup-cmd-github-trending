#!/usr/bin/env python3
"""
Stayup — scrapes GitHub's trending repositories and stores them via stayup-api.

Three sources are tracked, one per trending window:
  https://github.com/trending?since=daily
  https://github.com/trending?since=weekly
  https://github.com/trending?since=monthly

github.com/trending has no public API, so each window is a plain HTML page
scraped on every run. Each run fully replaces the stored snapshot for every
window, so this provider always holds exactly one row per window, refreshed
on every execution.

Talks to stayup-api over HTTP (STAYUP_API_URL + STAYUP_API_KEY) — it never
touches a database directly. See stayup-api/docs/self-hosting-and-providers.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

PROVIDER_TYPE = "github_trending"

# Nom affiché du provider dans les apps (fallback : nom de table capitalisé).
DISPLAY_NAME = "GitHub Trending"

# Où ce connecteur se classe parmi les autres dans la barre latérale.
SORT_ORDER = 50

# Instance stayup-api à laquelle parler, et la clé qui authentifie ce
# connecteur pour le provider 'github_trending' — obtenue depuis l'admin de
# cette instance (voir stayup-api/docs/self-hosting-and-providers.md).
API_URL = os.environ.get("STAYUP_API_URL", "http://localhost:3000").rstrip("/")
API_KEY = os.environ.get("STAYUP_API_KEY")

# Manifeste d'affichage : comment les 3 apps (ui / desktop / mobile) rendent les
# lignes de ce connecteur, sans une ligne de code côté app. stayup-api le relaie
# tel quel depuis provider_registry.template, sans jamais l'interpréter.
# Schéma : voir stayup-api/docs/self-hosting-and-providers.md.
#
# Une entrée = une fenêtre (daily/weekly/monthly) dont le `content` JSON porte
# la liste `repos`. L'entrée de liste résume la fenêtre ; le volet de lecture
# est le tableau de ses dépôts (comme github.com/trending).
DISPLAY_TEMPLATE = {
    "version": 1,
    "display": {
        "name": DISPLAY_NAME,
        # Icône auto-descriptive (tracé SVG teintable). Flèche « tendance ».
        "icon": {
            "paths": [
                "M22 7 13.5 15.5 8.5 10.5 2 17",
                "M16 7h6v6",
            ],
            "viewBox": "0 0 24 24",
            "stroke": True,
        },
        "accent": "#f4b585",
        "sortOrder": SORT_ORDER,
        # Libellé court du flux dans la sidebar : daily / weekly / monthly. Lu
        # depuis l'URL (et non config.since) pour rester correct même quand le
        # flux a été ajouté via le formulaire `form` ci-dessous, qui ne renseigne
        # que `repository.url`.
        "feedLabel": {
            "path": "$source.url",
            "cases": {
                "https://github.com/trending?since=daily": "daily",
                "https://github.com/trending?since=weekly": "weekly",
                "https://github.com/trending?since=monthly": "monthly",
            },
        },
    },
    "item": {
        "parseContentAsJson": True,
        "vars": {
            "window": {
                "path": "since",
                "cases": {"daily": "today", "weekly": "this week", "monthly": "this month"},
            }
        },
        "fields": {
            "title": "GitHub Trending — {window}",
            "subtitle": "{count} repositories",
            "summary": "The {count} repositories trending {window} on GitHub.",
            "url": "url",
            "timestamp": "fetched_at",
        },
    },
    "list": {
        "layout": "row",
        "primary": "title",
        "secondary": "subtitle",
        "meta": "timestamp",
    },
    "detail": {
        "mode": "table",
        "title": "Trending {window}",
        "collection": "repos",
        "rowLink": "url",
        "columns": [
            {"label": "#", "field": "rank", "align": "right", "width": "2.5rem"},
            {
                "label": "Repository",
                "field": "{owner}/{name}",
                "link": "url",
                "emphasis": True,
            },
            {"label": "Description", "field": "description", "muted": True, "truncate": True},
            {"label": "Language", "field": "language"},
            {"label": "Stars", "field": "stars", "align": "right", "format": "compactNumber"},
            {"label": "Forks", "field": "forks", "align": "right", "format": "compactNumber"},
            {
                "label": "This period",
                "field": "stars_period",
                "align": "right",
                "format": "compactNumber",
                "prefix": "+",
                "accent": True,
            },
        ],
        "openUrl": "url",
        "openLabel": "Open on github.com/trending",
    },
    # Champ « ajouter un flux » : une seule saisie (daily / weekly / monthly) au
    # lieu de l'URL complète. stayup-ui construit lui-même `repository.url` à
    # partir de `urlTemplate` — l'URL produite est identique à celle des 3 sources
    # seedées (voir SOURCES), donc un ajout manuel se déduplique avec la source
    # existante au lieu d'en créer une quatrième.
    "form": {
        "label": "Trending window (daily, weekly or monthly)",
        "placeholder": "daily",
        "urlTemplate": "https://github.com/trending?since={value}",
        "pattern": "^(daily|weekly|monthly)$",
        # Tolère le collage d'une URL complète : on en extrait la fenêtre.
        "transform": {"trim": True, "extract": r"[?&]since=([a-z]+)"},
    },
}

# The three tracked trending windows. Seeded automatically on every run.
SOURCE_URLS = [
    "https://github.com/trending?since=daily",
    "https://github.com/trending?since=weekly",
    "https://github.com/trending?since=monthly",
]

PERIOD_STARS_RE = re.compile(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)")

WINDOW_RE = re.compile(r"[?&]since=(daily|weekly|monthly)\b")


def window_from_url(url: str) -> str | None:
    """Return the trending window (daily / weekly / monthly) encoded in a source URL, or None."""
    match = WINDOW_RE.search(url)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# stayup-api client
# ---------------------------------------------------------------------------


def api_request(method: str, path: str, **kwargs) -> dict | None:
    """Call one of stayup-api's /connector-api/github_trending/* endpoints.

    Raises RuntimeError if STAYUP_API_KEY isn't set, or requests.HTTPError on
    a non-2xx response (via raise_for_status).
    """
    if not API_KEY:
        raise RuntimeError("STAYUP_API_KEY is not set.")
    url = f"{API_URL}/connector-api/{PROVIDER_TYPE}{path}"
    headers = {"Authorization": f"Bearer {API_KEY}"}
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def register_provider() -> None:
    """Auto-déclaration au démarrage — nom affiché et manifeste d'affichage."""
    api_request(
        "POST",
        "/register",
        json={
            "displayName": DISPLAY_NAME,
            "sortOrder": SORT_ORDER,
            "template": DISPLAY_TEMPLATE,
        },
    )


def ensure_sources() -> None:
    """Track the three trending windows if they aren't already. Idempotent on URL."""
    for url in SOURCE_URLS:
        api_request("POST", "/sources", json={"url": url})


def get_sources() -> list[tuple[int, str, dict]]:
    """Return all tracked sources as (id, url, config) tuples."""
    result = api_request("GET", "/sources")
    return [(s["id"], s["url"], s.get("config") or {}) for s in result["sources"]]


def replace_entry(
    repository_id: int, version: str, content: str, entry_datetime: datetime, executed_at: datetime
) -> None:
    """Replace the stored snapshot for one window — keeps exactly one entry per source.

    Deletes the previous snapshot first (retentionDays=0 purges everything
    already stored, since it's necessarily in the past), then stores the new
    one — same order as the original DELETE-then-INSERT, to avoid any risk of
    the fresh row being purged by a same-instant comparison.
    """
    api_request("DELETE", f"/sources/{repository_id}/old-items", params={"retentionDays": 0})
    api_request(
        "POST",
        "/items",
        json={
            "items": [
                {
                    "repositoryId": repository_id,
                    "version": version,
                    "content": content,
                    "datetime": entry_datetime.isoformat(),
                    "executedAt": executed_at.isoformat(),
                    "success": True,
                }
            ]
        },
    )


def save_error(repository_id: int | None, error: str, executed_at: datetime) -> None:
    """Persist a per-source failure."""
    api_request(
        "POST",
        "/errors",
        json={"repositoryId": repository_id, "error": error, "executedAt": executed_at.isoformat()},
    )


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


def process_repository(repository_id: int, repository_url: str, executed_at: datetime, config: dict) -> None:
    """Refresh the stored snapshot for one trending window.

    On success the previous entry is replaced. On any failure the previous
    snapshot is kept and the error is logged via the API — the run never crashes.
    """
    # La fenêtre vient de l'URL en priorité : un flux ajouté via le formulaire
    # `form` n'a pas de `config.since` (le formulaire ne renseigne que l'URL).
    since = window_from_url(repository_url) or config.get("since") or "daily"
    try:
        repos = fetch_trending(repository_url)
        content = build_content(since, repository_url, repos, executed_at)
        version = f"{since}@{executed_at:%Y-%m-%d}"
        replace_entry(repository_id, version, content, executed_at, executed_at)
    except Exception as e:
        save_error(repository_id, str(e), executed_at)
        print(f"[{repository_url}] Error: {e}", file=sys.stderr)


def main() -> None:
    register_provider()
    ensure_sources()

    executed_at = datetime.now(tz=timezone.utc)
    for repository_id, repository_url, config in get_sources():
        process_repository(repository_id, repository_url, executed_at, config)


if __name__ == "__main__":  # pragma: no cover
    main()
