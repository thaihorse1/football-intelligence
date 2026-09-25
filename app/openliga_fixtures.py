
import json
import os
import sys
import urllib.request

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


API_BASE = "https://api.openligadb.de"
BANGKOK = ZoneInfo("Asia/Bangkok")

WINDOW_DAYS = 30

# Initial German football coverage.
LEAGUES = {
    "bl1": "Bundesliga",
    "bl2": "2. Bundesliga",
    "bl3": "3. Liga",
}


def fetch_league(league, season):
    url = (
        f"{API_BASE}/getmatchdata/"
        f"{league}/{season}"
    )

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "FootballIntelligence/0.3",
        },
    )

    with urllib.request.urlopen(
        request, timeout=30
    ) as response:

        matches = json.load(response)

    if not isinstance(matches, list):
        raise ValueError(
            f"Unexpected API response for {league}"
        )

    return matches


def parse_kickoff(value):
    if not value:
        return None

    kickoff = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    # The provider's field is explicitly named
    # matchDateTimeUTC. A timestamp without an
    # offset is therefore interpreted as UTC.
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(
            tzinfo=timezone.utc
        )

    return kickoff.astimezone(timezone.utc)


def normalize_match(match, league_name):
    home = match.get("team1") or {}
    away = match.get("team2") or {}

    kickoff = parse_kickoff(
        match.get("matchDateTimeUTC")
    )

    if kickoff is None:
        return None

    if not home.get("teamName"):
        return None

    if not away.get("teamName"):
        return None

    match_id = match.get("matchID")

    if match_id is None:
        return None

    return {
        "provider": "openligadb",
        "fixture_id": match_id,
        "competition": league_name,
        "home_team_id": home.get("teamId"),
        "home_team": home.get("teamName"),
        "away_team_id": away.get("teamId"),
        "away_team": away.get("teamName"),
        "kickoff_utc": kickoff.isoformat(),
        "kickoff_bangkok": kickoff.astimezone(
            BANGKOK
        ).isoformat(),
        "status": "SCHEDULED",
        "verification_status": "SOURCE_ONLY",
    }


def main():
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=WINDOW_DAYS)

    # German league seasons normally begin in
    # the second half of the calendar year.
    season = (
        now.year if now.month >= 7
        else now.year - 1
    )

    fixtures = []
    seen = set()
    coverage = {}

    for league, league_name in LEAGUES.items():

        try:
            raw_matches = fetch_league(
                league, season
            )

        except Exception as error:
            print(
                f"WARNING: {league}: {error}",
                file=sys.stderr,
            )
            coverage[league] = {
                "status": "ERROR",
                "fixtures": 0,
            }
            continue

        count = 0

        for match in raw_matches:

            if match.get("matchIsFinished"):
                continue

            try:
                fixture = normalize_match(
                    match, league_name
                )
            except (ValueError, TypeError):
                continue

            if fixture is None:
                continue

            kickoff = datetime.fromisoformat(
                fixture["kickoff_utc"]
            )

            if not now <= kickoff < end:
                continue

            key = fixture["fixture_id"]

            if key in seen:
                continue

            seen.add(key)
            fixtures.append(fixture)
            count += 1

        coverage[league] = {
            "status": "OK",
            "fixtures": count,
        }

    fixtures.sort(
        key=lambda item: item["kickoff_utc"]
    )

    report = {
        "provider": "openligadb",
        "generated_at_utc": now.isoformat(),
        "window_start_utc": now.isoformat(),
        "window_end_utc": end.isoformat(),
        "total_fixtures": len(fixtures),
        "coverage": coverage,
        "fixtures": fixtures,
    }

    print(json.dumps(
        report,
        indent=2,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
