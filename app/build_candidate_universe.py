"""Build an offline, discovery-centered candidate universe from saved reports."""

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.reconcile_fixtures import (
    canonical_team, kickoff, kickoff_difference_minutes, team_similarity,
    exact_team_match, KICKOFF_TOLERANCE_MINUTES, FUZZY_THRESHOLD,
)

DISCOVERY = "soccer-football-info"
VERIFIERS = ("football-data.org", "openligadb", "api-football")
REGISTRY_PATH = Path("config/competition_registry.json")
SEARCH_MINUTES = 24 * 60


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError("Invalid fixture or competition ID")
    return str(value)


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid required text")
    return value


def timestamp(value):
    text(value)
    parsed = kickoff({"kickoff_utc": value})
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Kickoff must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def validate_registry(registry):
    if not isinstance(registry, dict) or set(registry) != {"bundesliga"}:
        raise ValueError("Registry must contain only Bundesliga")
    entry = registry["bundesliga"]
    if not isinstance(entry, dict) or set(entry) != {"canonical_name", DISCOVERY, *VERIFIERS}:
        raise ValueError("Invalid competition registry")
    text(entry["canonical_name"])
    fields = {DISCOVERY: ("competition_ids", "names"), "football-data.org": ("competition_codes",),
              "openligadb": ("league_codes",), "api-football": ("competition_codes", "league_ids")}
    for provider, keys in fields.items():
        mapping = entry[provider]
        if not isinstance(mapping, dict) or set(mapping) != set(keys):
            raise ValueError("Invalid registry mapping")
        for key in keys:
            values = mapping[key]
            if not isinstance(values, list) or not values:
                raise ValueError("Invalid registry identities")
            for value in values:
                if key == "league_ids":
                    if type(value) is not int or value <= 0:
                        raise ValueError("Invalid league ID")
                else:
                    text(value)
            if len(set(values)) != len(values):
                raise ValueError("Duplicate registry identity")
    return entry


def validate_report(report, provider):
    if not isinstance(report, dict) or report.get("provider") != provider or not isinstance(report.get("fixtures"), list):
        raise ValueError("Invalid report shape or provider")
    start, end = timestamp(report.get("window_start_utc")), timestamp(report.get("window_end_utc"))
    if start >= end:
        raise ValueError("Invalid report window")
    coverage = report.get("coverage")
    if provider == DISCOVERY:
        if not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool:
            raise ValueError("Missing discovery coverage")
    elif provider == "openligadb":
        if not isinstance(coverage, dict):
            raise ValueError("Missing league coverage")
        for league, detail in coverage.items():
            text(league)
            if not isinstance(detail, dict) or detail.get("status") not in ("OK", "ERROR"):
                raise ValueError("Invalid league coverage")
    elif coverage is not None:
        if not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool:
            raise ValueError("Invalid verifier coverage")
    if provider in ("football-data.org", "api-football"):
        text(report.get("competition_code"))
        if "league_id" in report and (type(report["league_id"]) is not int or report["league_id"] <= 0):
            raise ValueError("Invalid report league ID")
    unique = {}
    for fixture in report["fixtures"]:
        if not isinstance(fixture, dict) or fixture.get("provider") != provider:
            raise ValueError("Invalid provider observation")
        key = identifier(fixture.get("fixture_id"))
        for field in ("competition", "home_team", "away_team", "status"):
            text(fixture.get(field))
        for field in ("home_team", "away_team"):
            if not canonical_team(fixture[field]):
                raise ValueError("Empty canonical team identity")
        timestamp(fixture.get("kickoff_utc"))
        if provider == DISCOVERY:
            for field in ("competition_id", "home_team_id", "away_team_id"):
                identifier(fixture.get(field))
            timestamp(fixture.get("kickoff_bangkok"))
            if (fixture["status"] != "SCHEDULED" or fixture.get("provider_status") != "NOT_STARTED"
                    or fixture.get("discovery_status") != "DISCOVERED" or fixture.get("verification_status") != "UNVERIFIED"):
                raise ValueError("Invalid discovery state")
            if not start <= kickoff(fixture) < end:
                raise ValueError("Discovery fixture outside report window")
        elif provider in ("football-data.org", "api-football"):
            text(fixture.get("competition_code"))
        if key in unique and unique[key] != fixture:
            raise ValueError("Conflicting duplicate provider fixture ID")
        unique[key] = fixture
    return list(unique.values())


def mapped_discovery(fixture, entry):
    mapping = entry[DISCOVERY]
    # IDs are decisive; exact registered names are an additional identity check.
    return (str(fixture["competition_id"]) in mapping["competition_ids"]
            and fixture["competition"] in mapping["names"])


def competition_matches(observation, provider, entry):
    mapping = entry[provider]
    if provider == "openligadb":
        # Existing normalized OpenLigaDB fixtures omit league codes.
        return observation.get("competition") == entry["canonical_name"]
    code_matches = observation.get("competition_code") in mapping["competition_codes"]
    if provider == "api-football" and "league_id" in observation:
        return code_matches and observation["league_id"] in mapping["league_ids"]
    return code_matches


def coverage_state(fixture, report, provider, entry):
    if report is None:
        return "NOT_PROVIDED"
    if not mapped_discovery(fixture, entry):
        return "NOT_APPLICABLE"
    if not timestamp(report["window_start_utc"]) <= kickoff(fixture) < timestamp(report["window_end_utc"]):
        return "NOT_APPLICABLE"
    mapping = entry[provider]
    if provider == "openligadb":
        details = [report["coverage"].get(code) for code in mapping["league_codes"]]
        if any(detail is not None and detail["status"] == "ERROR" for detail in details):
            return "INCOMPLETE"
        if not all(detail is not None and detail["status"] == "OK" for detail in details):
            return "NOT_APPLICABLE"
    else:
        if not competition_matches(report, provider, entry):
            return "NOT_APPLICABLE"
        if report.get("coverage", {}).get("complete") is False or report.get("status") == "ERROR" or report.get("errors"):
            return "INCOMPLETE"
    return "COMPLETE"


def match_evidence(anchor, observations, provider, entry):
    exact_candidates, fuzzy_pool = [], []
    for observation in observations:
        if not competition_matches(observation, provider, entry):
            continue
        difference = kickoff_difference_minutes(anchor, observation)
        if difference > SEARCH_MINUTES:
            continue
        if exact_team_match(anchor, observation):
            exact_candidates.append((observation, difference))
        else:
            # Never reinterpret a reversed pairing as an oriented fuzzy match.
            reversed_pair = {"home_team": observation["away_team"], "away_team": observation["home_team"]}
            if not exact_team_match(anchor, reversed_pair):
                fuzzy_pool.append((observation, difference))
    plausible = []
    for observation, difference in exact_candidates or fuzzy_pool:
        similarity = team_similarity(anchor, observation)
        if not exact_candidates and similarity < FUZZY_THRESHOLD:
            continue
        plausible.append({"fixture_id": observation["fixture_id"],
                          "match_method": "CANONICAL_EXACT" if exact_candidates else "FUZZY",
                          "team_similarity": similarity, "kickoff_difference_minutes": difference,
                          "conflicts": ["KICKOFF_CONFLICT"] if difference > KICKOFF_TOLERANCE_MINUTES else [],
                          "observation": copy.deepcopy(observation)})
    plausible.sort(key=lambda item: (timestamp(item["observation"]["kickoff_utc"]), str(item["fixture_id"])))
    if not plausible:
        return {"result": "NO_MATCH"}
    if len(plausible) > 1:
        return {"result": "AMBIGUOUS", "candidate_fixture_ids": [p["fixture_id"] for p in plausible],
                "candidates": plausible}
    return {"result": "MATCH", **plausible[0]}


def build_report(discovery, verifiers=None, registry=None):
    if registry is None:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    entry = validate_registry(registry)
    anchors = validate_report(discovery, DISCOVERY)
    verifiers = {} if verifiers is None else verifiers
    if not isinstance(verifiers, dict) or set(verifiers) - set(VERIFIERS):
        raise ValueError("Unknown verifier")
    observations = {p: validate_report(verifiers[p], p) if verifiers.get(p) is not None else [] for p in VERIFIERS}
    candidates, used_ids = [], set()
    summary = dict.fromkeys(("unverified", "verified", "multi_source_verified", "conflict", "ambiguous"), 0)
    for anchor in anchors:
        candidate_id = "sfi:" + identifier(anchor["fixture_id"])
        if candidate_id in used_ids:
            raise ValueError("Candidate ID collision")
        used_ids.add(candidate_id)
        evidence, conflicts = {}, []
        for provider in VERIFIERS:
            coverage = coverage_state(anchor, verifiers.get(provider), provider, entry)
            evidence[provider] = {"coverage": coverage, "result": None}
            if coverage == "COMPLETE":
                evidence[provider].update(match_evidence(anchor, observations[provider], provider, entry))
                if evidence[provider]["result"] == "MATCH":
                    for conflict in evidence[provider]["conflicts"]:
                        conflicts.append({"provider": provider, "type": conflict})
        if any(e["result"] == "AMBIGUOUS" for e in evidence.values()):
            status = "AMBIGUOUS"
        elif conflicts:
            status = "CONFLICT"
        else:
            count = sum(e["result"] == "MATCH" for e in evidence.values())
            status = "MULTI_SOURCE_VERIFIED" if count >= 2 else "VERIFIED" if count else "UNVERIFIED"
        summary[status.lower()] += 1
        candidates.append({"candidate_id": candidate_id,
                           "candidate_status": "REVIEW" if status in ("AMBIGUOUS", "CONFLICT") else "ACTIVE",
                           "verification_status": status,
                           "fixture": {k: copy.deepcopy(anchor[k]) for k in ("competition_id", "competition", "home_team_id",
                                       "home_team", "away_team_id", "away_team", "kickoff_utc", "kickoff_bangkok", "status")},
                           "discovery": copy.deepcopy(anchor), "verifiers": evidence, "conflicts": conflicts})
    candidates.sort(key=lambda item: (timestamp(item["fixture"]["kickoff_utc"]), item["candidate_id"]))
    return {"schema_version": "0.4B", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "discovery": {"provider": DISCOVERY, "coverage_complete": discovery["coverage"]["complete"],
                          "coverage": copy.deepcopy(discovery["coverage"]),
                          "window_start_utc": discovery["window_start_utc"], "window_end_utc": discovery["window_end_utc"]},
            "total_candidates": len(candidates), "summary": summary, "candidates": candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", required=True, type=Path)
    for flag in ("football-data", "openligadb", "api-football"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--competition-registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args()
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        verifiers = {provider: read(path) for provider, path in zip(VERIFIERS,
                     (args.football_data, args.openligadb, args.api_football)) if path is not None}
        report = build_report(read(args.discovery), verifiers, read(args.competition_registry))
    except (ValueError, TypeError, KeyError, OSError, OverflowError):
        print("ERROR: Invalid candidate-universe input or competition registry", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
