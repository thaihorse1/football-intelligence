"""Collect upcoming Bundesliga fixtures from the direct API-SPORTS API."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE_URL = "https://v3.football.api-sports.io"
PROVIDER = "api-football"
LEAGUE_ID = 78
BANGKOK = ZoneInfo("Asia/Bangkok")


def fetch_matches(season, start, end):
    key = os.environ.get("API_FOOTBALL_API_KEY")
    if not key:
        raise RuntimeError("API_FOOTBALL_API_KEY is missing")
    params = urllib.parse.urlencode({
        "league": LEAGUE_ID, "season": season,
        "from": start.date().isoformat(), "to": end.date().isoformat(),
        "timezone": "UTC",
    })
    request = urllib.request.Request(
        f"{BASE_URL}/fixtures?{params}",
        headers={"x-apisports-key": key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API-Football HTTP {exc.code}") from exc
    if not isinstance(data, dict) or data.get("errors"):
        raise RuntimeError("API-Football returned an API error or invalid response")
    if not isinstance(data.get("response"), list):
        raise RuntimeError("API-Football response must contain a fixture list")
    # Do not silently publish a partial report if the API adds pagination.
    paging = data.get("paging") or {}
    if paging.get("total", 1) > 1:
        raise RuntimeError("API-Football returned multiple pages; refusing a partial report")
    return data["response"]


def normalize_match(match):
    fixture = match.get("fixture") or {}
    league = match.get("league") or {}
    teams = match.get("teams") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}
    raw_status = (fixture.get("status") or {}).get("short")
    if raw_status != "NS" or league.get("id") != LEAGUE_ID:
        return None
    if fixture.get("id") is None or not home.get("name") or not away.get("name"):
        raise ValueError("Missing fixture ID or team name")
    kickoff = datetime.fromisoformat(fixture["date"].replace("Z", "+00:00"))
    if kickoff.tzinfo is None:
        raise ValueError("API-Football kickoff must include a timezone")
    kickoff = kickoff.astimezone(timezone.utc)
    return {
        "provider": PROVIDER, "fixture_id": fixture["id"],
        "competition_code": "BL1", "competition": "Bundesliga",
        "home_team_id": home.get("id"), "home_team": home["name"],
        "away_team_id": away.get("id"), "away_team": away["name"],
        "kickoff_utc": kickoff.isoformat(),
        "kickoff_bangkok": kickoff.astimezone(BANGKOK).isoformat(),
        "status": "SCHEDULED", "provider_status": raw_status,
        "verification_status": "SOURCE_ONLY",
    }


def build_report(matches, start, end, seasons):
    fixtures = {}
    for match in matches:
        fixture = normalize_match(match)
        if fixture is None:
            continue
        kickoff = datetime.fromisoformat(fixture["kickoff_utc"])
        if start <= kickoff < end:
            previous = fixtures.get(fixture["fixture_id"])
            if previous is not None and previous != fixture:
                raise ValueError("Conflicting duplicate API-Football fixture")
            fixtures[fixture["fixture_id"]] = fixture
    return {
        "provider": PROVIDER, "competition_code": "BL1",
        "league_id": LEAGUE_ID, "seasons": seasons,
        "generated_at_utc": start.isoformat(),
        "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat(),
        "total_fixtures": len(fixtures),
        "fixtures": sorted(fixtures.values(), key=lambda item: item["kickoff_utc"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--season", type=int, help="Season starting year; default covers the window")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=args.days)
    first = start.year if start.month >= 7 else start.year - 1
    last_day = end - timedelta(microseconds=1)
    last = last_day.year if last_day.month >= 7 else last_day.year - 1
    seasons = [args.season] if args.season is not None else list(range(first, last + 1))
    try:
        matches = []
        for season in seasons:
            matches.extend(fetch_matches(season, start, end))
        print(json.dumps(build_report(matches, start, end, seasons), indent=2, ensure_ascii=False))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
