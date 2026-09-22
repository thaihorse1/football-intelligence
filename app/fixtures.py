
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


API_URL = "https://api.football-data.org/v4/competitions/PL/matches"
BANGKOK = ZoneInfo("Asia/Bangkok")

# Initial discovery window: upcoming 72 hours.
WINDOW_HOURS = 24 * 30

UPCOMING_STATUSES = {"SCHEDULED", "TIMED"}


def fetch_matches(start, end):
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY")

    if not api_key:
        raise RuntimeError(
            "FOOTBALL_DATA_API_KEY is missing"
        )

    # Fetch a slightly wider UTC date range, then apply
    # our exact 72-hour window locally.
    params = urllib.parse.urlencode({
        "dateFrom": start.date().isoformat(),
        "dateTo": (
            end.date() + timedelta(days=1)
        ).isoformat(),
    })

    request = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={
            "X-Auth-Token": api_key,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request, timeout=30
        ) as response:
            return json.load(response)

    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise RuntimeError(
                "Football API rate limit reached"
            ) from error

        if error.code in (401, 403):
            raise RuntimeError(
                f"Football API access denied: HTTP {error.code}"
            ) from error

        raise RuntimeError(
            f"Football API request failed: HTTP {error.code}"
        ) from error


def normalize_match(match):
    kickoff = datetime.fromisoformat(
        match["utcDate"].replace("Z", "+00:00")
    )

    competition = match.get("competition") or {}
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}

    return {
        "fixture_id": match["id"],
        "provider": "football-data.org",
        "competition_code": competition.get("code"),
        "competition": competition.get("name"),
        "home_team_id": home.get("id"),
        "home_team": home.get("name"),
        "away_team_id": away.get("id"),
        "away_team": away.get("name"),
        "kickoff_utc": kickoff.isoformat(),
        "kickoff_bangkok": kickoff.astimezone(
            BANGKOK
        ).isoformat(),
        "status": match.get("status"),
    }


def main():
    start = datetime.now(timezone.utc)
    end = start + timedelta(hours=WINDOW_HOURS)

    data = fetch_matches(start, end)

    fixtures = []
    seen = set()

    for match in data.get("matches", []):
        if match.get("status") not in UPCOMING_STATUSES:
            continue

        if not match.get("utcDate"):
            continue

        kickoff = datetime.fromisoformat(
            match["utcDate"].replace("Z", "+00:00")
        )

        if not start <= kickoff < end:
            continue

        match_id = match["id"]

        if match_id in seen:
            continue

        seen.add(match_id)
        fixtures.append(normalize_match(match))

    fixtures.sort(
        key=lambda match: match["kickoff_utc"]
    )

    report = {
        "provider": "football-data.org",
        "generated_at_utc": start.isoformat(),
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "total_fixtures": len(fixtures),
        "fixtures": fixtures,
    }

    print(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)