import json
import os
import urllib.request

from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.football-data.org/v4"
API_KEY = os.environ["FOOTBALL_DATA_API_KEY"]

today = datetime.now(timezone.utc).date()

checks = [
    ("Premier League — upcoming", "PL", today, today + timedelta(days=30)),
    ("Brazil Série A — upcoming", "BSA", today, today + timedelta(days=30)),
    ("Premier League — recent", "PL", today - timedelta(days=14), today),
]

for label, competition, start, end in checks:

    url = (
        f"{BASE_URL}/competitions/{competition}/matches"
        f"?dateFrom={start.isoformat()}"
        f"&dateTo={end.isoformat()}"
    )

    request = urllib.request.Request(
        url,
        headers={"X-Auth-Token": API_KEY},
    )

    print(f"\n=== {label} ===")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)

        print("Result set:", data.get("resultSet"))
        print("Applied filters:", data.get("filters"))

        matches = data.get("matches", [])
        print("Matches returned:", len(matches))

        for match in matches[:5]:
            home = match.get("homeTeam") or {}
            away = match.get("awayTeam") or {}

            print(
                match.get("utcDate"),
                "|",
                home.get("name"),
                "vs",
                away.get("name"),
                "|",
                match.get("status"),
            )

    except Exception as error:
        print("Request failed:", type(error).__name__, str(error))