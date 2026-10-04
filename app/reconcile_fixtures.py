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


def reconcile(football_matches, openliga_matches, api_football_matches=None):
    if api_football_matches is not None:
        return reconcile_three(football_matches, openliga_matches, api_football_matches)
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


def reconcile_three(football_matches, openliga_matches, api_football_matches):
    """Require pairwise identity agreement; never let a majority hide conflicts."""
    sources = {
        "football_data": football_matches,
        "openliga": openliga_matches,
        "api_football": api_football_matches,
    }
    nodes = [(source, match) for source, matches in sources.items() for match in matches]
    edges = {index: set() for index in range(len(nodes))}
    for i, (source, match) in enumerate(nodes):
        for j in range(i + 1, len(nodes)):
            other_source, other = nodes[j]
            if source == other_source:
                continue
            if kickoff_difference_minutes(match, other) > 24 * 60:
                continue
            if exact_team_match(match, other) or team_similarity(match, other) >= FUZZY_THRESHOLD:
                edges[i].add(j)
                edges[j].add(i)

    def record(indices, ambiguous=False):
        observations = {name: None for name in sources}
        for index in indices:
            source, match = nodes[index]
            observations[source] = match
        pairs = [
            (nodes[i][1], nodes[j][1])
            for offset, i in enumerate(indices) for j in indices[offset + 1:]
        ]
        diff = max((kickoff_difference_minutes(a, b) for a, b in pairs), default=None)
        similarity = min((team_similarity(a, b) for a, b in pairs), default=None)
        conflicts = []
        if pairs and not all(exact_team_match(a, b) for a, b in pairs):
            conflicts.append("TEAM_NAME_CONFLICT")
        if diff is not None and diff > KICKOFF_TOLERANCE_MINUTES:
            conflicts.append("KICKOFF_CONFLICT")
        if ambiguous:
            status = "AMBIGUOUS_MATCH"
        elif not pairs:
            status = "ONLY_" + nodes[indices[0]][0].upper()
        elif "KICKOFF_CONFLICT" in conflicts:
            status = "KICKOFF_CONFLICT"
        elif conflicts:
            status = "TEAM_NAME_CONFLICT"
        else:
            status = "MATCHED"
        return {
            "status": status, **observations,
            "source_count": len(indices),
            "missing_sources": [name for name, match in observations.items() if match is None],
            "conflicts": conflicts,
            "team_similarity": round(similarity, 3) if similarity is not None else None,
            "kickoff_difference_minutes": round(diff, 1) if diff is not None else None,
        }

    results, visited = [], set()
    for index in edges:
        if index in visited:
            continue
        component, pending = set(), [index]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(edges[current] - component)
        visited.update(component)
        indices = sorted(component)
        unique_sources = len({nodes[i][0] for i in indices}) == len(indices)
        complete = all(component - {i} <= edges[i] for i in indices)
        if unique_sources and complete:
            results.append(record(indices))
        else:
            for i in indices:
                result = record([i], ambiguous=True)
                result["candidate_fixtures"] = [
                    {"source": nodes[j][0], "fixture_id": nodes[j][1]["fixture_id"]}
                    for j in indices if j != i
                ]
                results.append(result)
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

    parser.add_argument(
        "--api-football",
        help="Optional API-Football report; enables three-source reconciliation",
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

    api_matches = None
    if args.api_football:
        api_report = load_json(args.api_football)
        api_matches = [
            match for match in api_report["fixtures"]
            if match.get("competition_code") == "BL1"
        ]
        football_matches = [
            match for match in football_matches
            if match.get("competition_code") == "BL1"
        ]

    results = reconcile(football_matches, openliga_matches, api_matches)

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

    if api_matches is not None:
        report["sources"].append("api-football")
        report["api_football_fixtures"] = len(api_matches)

    print(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()