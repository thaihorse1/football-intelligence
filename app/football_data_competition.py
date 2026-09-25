import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


BASE_URL = "https://api.football-data.org/v4"
BANGKOK = ZoneInfo("Asia/Bangkok")
UPCOMING_STATUSES = {"SCHEDULED", "TIMED"}


def fetch_matches(competition: str, start: datetime, end: datetime):
    key = os.environ.get("FOOTBALL_DATA_API_KEY")

    if not key:
        raise RuntimeError("FOOTBALL_DATA_API_KEY is missing")

    params = urllib.parse.urlencode(
        {
            "dateFrom": start.date().isoformat(),
            "dateTo": end.date().isoformat(),
        }
    )

    url = (
        f"{BASE_URL}/competitions/{competition}/matches"
        f"?{params}"
    )

    request = urllib.request.Request(
        url,
        headers={
            "X-Auth-Token": key,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            return json.load(response)

    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"football-data.org HTTP {exc.code}"
        ) from exc


def parse_utc(value: str):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def normalize_match(match):
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}
    competition = match.get("competition") or {}

    kickoff = parse_utc(match["utcDate"])

    return {
        "provider": "football-data.org",
        "fixture_id": match.get("id"),
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
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--competition",
        default="BL1",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=30,
    )

    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=args.days)

    try:
        data = fetch_matches(
            args.competition,
            now,
            end,
        )

        fixtures = []

        for match in data.get("matches", []):
            if match.get("status") not in UPCOMING_STATUSES:
                continue

            fixture = normalize_match(match)

            kickoff = datetime.fromisoformat(
                fixture["kickoff_utc"]
            )

            if now <= kickoff < end:
                fixtures.append(fixture)

        fixtures.sort(
            key=lambda x: x["kickoff_utc"]
        )

        report = {
            "provider": "football-data.org",
            "competition_code": args.competition,
            "generated_at_utc": now.isoformat(),
            "window_start_utc": now.isoformat(),
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

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()