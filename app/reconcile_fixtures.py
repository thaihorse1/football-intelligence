import argparse
import json
import re
import unicodedata

from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


ALIASES_FILE = Path("config/team_aliases.json")

KICKOFF_TOLERANCE_MINUTES = 15
FUZZY_THRESHOLD = 0.78

GENERIC_CLUB_WORDS = {
    "fc",
    "afc",
    "cf",
    "sc",
    "sv",
    "vfl",
    "vfb",
    "tsg",
    "club",
    "football",
}


def load_team_aliases():
    if not ALIASES_FILE.exists():
        return {}

    with ALIASES_FILE.open(
        encoding="utf-8"
    ) as file:
        return json.load(file)


TEAM_ALIASES = load_team_aliases()


def load_json(path):
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def canonical_team(name):
    name = name.replace("ß", "ss")

    normalized = unicodedata.normalize(
        "NFKD",
        name,
    )

    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )

    normalized = normalized.lower()

    tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    tokens = [
        token
        for token in tokens
        if token not in GENERIC_CLUB_WORDS
    ]

    canonical = " ".join(tokens)

    return TEAM_ALIASES.get(
        canonical,
        canonical,
    )


def kickoff(match):
    return datetime.fromisoformat(
        match["kickoff_utc"]
    )


def kickoff_difference_minutes(a, b):
    return abs(
        (kickoff(a) - kickoff(b)).total_seconds()
    ) / 60


def team_similarity(a, b):
    home = SequenceMatcher(
        None,
        canonical_team(a["home_team"]),
        canonical_team(b["home_team"]),
    ).ratio()

    away = SequenceMatcher(
        None,
        canonical_team(a["away_team"]),
        canonical_team(b["away_team"]),
    ).ratio()

    return (home + away) / 2


def exact_team_match(a, b):
    return (
        canonical_team(a["home_team"])
        == canonical_team(b["home_team"])
        and
        canonical_team(a["away_team"])
        == canonical_team(b["away_team"])
    )


def reconciliation_record(
    status,
    football_data=None,
    openliga=None,
    similarity=None,
    kickoff_diff=None,
):
    return {
        "status": status,
        "football_data": football_data,
        "openliga": openliga,
        "team_similarity": similarity,
        "kickoff_difference_minutes": kickoff_diff,
    }


def reconcile(football_matches, openliga_matches):
    results = []
    used_openliga = set()

    for fd in football_matches:
        exact_candidate = None

        for index, ol in enumerate(openliga_matches):
            if index in used_openliga:
                continue

            if exact_team_match(fd, ol):
                exact_candidate = (index, ol)
                break

        if exact_candidate:
            index, ol = exact_candidate
            used_openliga.add(index)

            diff = kickoff_difference_minutes(
                fd,
                ol,
            )

            status = (
                "MATCHED"
                if diff <= KICKOFF_TOLERANCE_MINUTES
                else "KICKOFF_CONFLICT"
            )

            results.append(
                reconciliation_record(
                    status=status,
                    football_data=fd,
                    openliga=ol,
                    similarity=1.0,
                    kickoff_diff=round(diff, 1),
                )
            )

            continue

        best = None

        for index, ol in enumerate(openliga_matches):
            if index in used_openliga:
                continue

            diff = kickoff_difference_minutes(
                fd,
                ol,
            )

            if diff > 24 * 60:
                continue

            similarity = team_similarity(
                fd,
                ol,
            )

            if best is None or similarity > best[0]:
                best = (
                    similarity,
                    index,
                    ol,
                    diff,
                )

        if best and best[0] >= FUZZY_THRESHOLD:
            similarity, index, ol, diff = best

            used_openliga.add(index)

            if diff <= KICKOFF_TOLERANCE_MINUTES:
                status = "TEAM_NAME_CONFLICT"
            else:
                status = "KICKOFF_CONFLICT"

            results.append(
                reconciliation_record(
                    status=status,
                    football_data=fd,
                    openliga=ol,
                    similarity=round(
                        similarity,
                        3,
                    ),
                    kickoff_diff=round(diff, 1),
                )
            )

        else:
            results.append(
                reconciliation_record(
                    status="ONLY_FOOTBALL_DATA",
                    football_data=fd,
                )
            )

    for index, ol in enumerate(openliga_matches):
        if index not in used_openliga:
            results.append(
                reconciliation_record(
                    status="ONLY_OPENLIGA",
                    openliga=ol,
                )
            )

    return results


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--football-data",
        default="data/football_data_bl1.json",
    )

    parser.add_argument(
        "--openliga",
        default="data/openliga_fixtures.json",
    )

    args = parser.parse_args()

    fd_report = load_json(args.football_data)
    ol_report = load_json(args.openliga)

    football_matches = fd_report["fixtures"]

    openliga_matches = [
        match
        for match in ol_report["fixtures"]
        if match["competition"] == "Bundesliga"
    ]

    results = reconcile(
        football_matches,
        openliga_matches,
    )

    summary = {}

    for result in results:
        status = result["status"]

        summary[status] = (
            summary.get(status, 0) + 1
        )

    report = {
        "sources": [
            "football-data.org",
            "openligadb",
        ],
        "football_data_fixtures": len(
            football_matches
        ),
        "openliga_fixtures": len(
            openliga_matches
        ),
        "summary": summary,
        "results": results,
    }

    print(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()