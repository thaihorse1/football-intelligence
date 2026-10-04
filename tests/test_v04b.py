import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import build_candidate_universe as c

START = datetime(2026, 10, 10, tzinfo=timezone.utc)
REGISTRY = json.loads(Path("config/competition_registry.json").read_text())


def fixture(provider=c.DISCOVERY, identifier="s1", minutes=60, home="Bayern Munich", away="Borussia Dortmund"):
    when = START + timedelta(minutes=minutes)
    record = {"provider": provider, "fixture_id": identifier, "competition": "Bundesliga",
              "home_team": home, "away_team": away, "home_team_id": "h1", "away_team_id": "a1",
              "kickoff_utc": when.isoformat(), "kickoff_bangkok": (when + timedelta(hours=7)).replace(tzinfo=timezone(timedelta(hours=7))).isoformat(),
              "status": "SCHEDULED"}
    if provider == c.DISCOVERY:
        record.update(competition="Bundesliga I", competition_id="a0d28d6b99d45e79", country=None,
                      provider_status="NOT_STARTED", discovery_status="DISCOVERED", verification_status="UNVERIFIED")
    elif provider != "openligadb":
        record["competition_code"] = "BL1"
    return record


def report(provider=c.DISCOVERY, fixtures=None):
    result = {"provider": provider, "window_start_utc": START.isoformat(),
              "window_end_utc": (START + timedelta(days=3)).isoformat(),
              "fixtures": [fixture(provider)] if fixtures is None else fixtures}
    if provider == c.DISCOVERY:
        result["coverage"] = {"complete": True, "reason": None}
    elif provider == "openligadb":
        result["coverage"] = {"bl1": {"status": "OK"}, "bl2": {"status": "ERROR"}}
    else:
        result["competition_code"] = "BL1"
        if provider == "api-football":
            result["league_id"] = 78
    return result


class CandidateTests(unittest.TestCase):
    def build(self, discovery=None, **sources):
        return c.build_report(report() if discovery is None else discovery, sources, REGISTRY)

    def candidate(self, **sources):
        return self.build(**sources)["candidates"][0]

    def test_discovery_only_and_not_provided(self):
        candidate = self.candidate()
        self.assertEqual(candidate["candidate_id"], "sfi:s1")
        self.assertEqual(candidate["candidate_status"], "ACTIVE")
        self.assertEqual(candidate["verification_status"], "UNVERIFIED")
        self.assertEqual(list(candidate["verifiers"]), list(c.VERIFIERS))
        for evidence in candidate["verifiers"].values():
            self.assertEqual(evidence, {"coverage": "NOT_PROVIDED", "result": None})

    def test_exact_alias_and_fuzzy(self):
        for home, method in (("Bayern Munich", "CANONICAL_EXACT"), ("FC Bayern Munich", "CANONICAL_EXACT"), ("Bayern Munic", "FUZZY")):
            with self.subTest(home=home):
                verifier = report("football-data.org", [fixture("football-data.org", home=home)])
                candidate = self.candidate(**{"football-data.org": verifier})
                self.assertEqual(candidate["verification_status"], "VERIFIED")
                evidence = candidate["verifiers"]["football-data.org"]
                self.assertEqual(evidence["match_method"], method)
                self.assertGreaterEqual(evidence["team_similarity"], c.FUZZY_THRESHOLD)
                self.assertEqual(evidence["observation"], verifier["fixtures"][0])

    def test_existing_alias_match(self):
        discovery = report(fixtures=[fixture(home="TSG 1899 Hoffenheim")])
        verifier = report("football-data.org", [fixture("football-data.org", home="Hoffenheim")])
        candidate = self.build(discovery, **{"football-data.org": verifier})["candidates"][0]
        self.assertEqual(candidate["verification_status"], "VERIFIED")
        self.assertEqual(candidate["verifiers"]["football-data.org"]["match_method"], "CANONICAL_EXACT")

    def test_multiple_independent_sources(self):
        sources = {p: report(p) for p in c.VERIFIERS}
        candidate = self.candidate(**sources)
        self.assertEqual(candidate["verification_status"], "MULTI_SOURCE_VERIFIED")
        self.assertEqual(candidate["candidate_status"], "ACTIVE")

    def test_kickoff_boundaries(self):
        for delta, status, result in ((15, "VERIFIED", "MATCH"), (16, "CONFLICT", "MATCH"),
                                      (1440, "CONFLICT", "MATCH"), (1441, "UNVERIFIED", "NO_MATCH")):
            with self.subTest(delta=delta):
                candidate = self.candidate(**{"football-data.org": report("football-data.org", [fixture("football-data.org", minutes=60 + delta)])})
                self.assertEqual(candidate["verification_status"], status)
                self.assertEqual(candidate["verifiers"]["football-data.org"]["result"], result)
                self.assertEqual(candidate["candidate_status"], "REVIEW" if status == "CONFLICT" else "ACTIVE")
                if status == "CONFLICT":
                    self.assertEqual(candidate["conflicts"], [{"provider": "football-data.org", "type": "KICKOFF_CONFLICT"}])

    def test_reversed_pair_and_empty_complete(self):
        for fixtures in ([], [fixture("football-data.org", home="Borussia Dortmund", away="Bayern Munich")]):
            candidate = self.candidate(**{"football-data.org": report("football-data.org", fixtures)})
            self.assertEqual(candidate["verifiers"]["football-data.org"], {"coverage": "COMPLETE", "result": "NO_MATCH"})
            self.assertEqual(candidate["verification_status"], "UNVERIFIED")
            self.assertEqual(candidate["candidate_status"], "ACTIVE")

    def test_ambiguity_preserves_every_observation_and_precedence(self):
        first, second = fixture("football-data.org", "one"), fixture("football-data.org", "two")
        candidate = self.candidate(**{"football-data.org": report("football-data.org", [second, first]),
                                      "api-football": report("api-football", [fixture("api-football", minutes=100)])})
        self.assertEqual(candidate["verification_status"], "AMBIGUOUS")
        self.assertEqual(candidate["candidate_status"], "REVIEW")
        evidence = candidate["verifiers"]["football-data.org"]
        self.assertEqual(evidence["candidate_fixture_ids"], ["one", "two"])
        self.assertNotIn("observation", evidence)
        self.assertEqual([item["observation"] for item in evidence["candidates"]], [first, second])
        self.assertTrue(candidate["conflicts"])

    def test_exact_match_ignores_fuzzy_alternative(self):
        exact = fixture("football-data.org", "exact")
        fuzzy = fixture("football-data.org", "fuzzy", home="Bayern Munic")
        self.assertGreaterEqual(c.team_similarity(fixture(), fuzzy), c.FUZZY_THRESHOLD)
        self.assertFalse(c.exact_team_match(fixture(), fuzzy))
        for observations in ([fuzzy, exact], [exact, fuzzy]):
            candidate = self.candidate(**{"football-data.org": report("football-data.org", observations)})
            evidence = candidate["verifiers"]["football-data.org"]
            self.assertEqual(evidence["result"], "MATCH")
            self.assertEqual(evidence["fixture_id"], "exact")
            self.assertEqual(evidence["match_method"], "CANONICAL_EXACT")
            self.assertEqual(evidence["observation"], exact)
            self.assertNotIn("candidate_fixture_ids", evidence)
            self.assertEqual(candidate["verification_status"], "VERIFIED")

    def test_two_fuzzy_matches_without_exact_are_ambiguous(self):
        first = fixture("football-data.org", "one", home="Bayern Munic")
        second = fixture("football-data.org", "two", home="Bayern Munih")
        for observation in (first, second):
            self.assertFalse(c.exact_team_match(fixture(), observation))
            self.assertGreaterEqual(c.team_similarity(fixture(), observation), c.FUZZY_THRESHOLD)
        candidate = self.candidate(**{"football-data.org": report("football-data.org", [second, first])})
        evidence = candidate["verifiers"]["football-data.org"]
        self.assertEqual(candidate["verification_status"], "AMBIGUOUS")
        self.assertEqual(evidence["candidate_fixture_ids"], ["one", "two"])
        self.assertTrue(all(item["match_method"] == "FUZZY" for item in evidence["candidates"]))

    def test_not_applicable_window_competition_and_league(self):
        for change in ("window", "report_competition", "fixture_competition", "name"):
            discovery, verifier = report(), report("football-data.org")
            if change == "window":
                verifier["window_start_utc"] = (START + timedelta(days=1)).isoformat()
            elif change == "report_competition":
                verifier["competition_code"] = "PL"
            elif change == "fixture_competition":
                discovery["fixtures"][0]["competition_id"] = "other"
            else:
                discovery["fixtures"][0]["competition"] = "Bundesliga II"
            candidate = self.build(discovery, **{"football-data.org": verifier})["candidates"][0]
            self.assertEqual(candidate["verifiers"]["football-data.org"]["coverage"], "NOT_APPLICABLE")
        verifier = report("api-football")
        verifier["league_id"] = 39
        self.assertEqual(self.candidate(**{"api-football": verifier})["verifiers"]["api-football"]["coverage"], "NOT_APPLICABLE")

    def test_incomplete_verifier_and_discovery(self):
        verifier = report("openligadb")
        verifier["coverage"]["bl1"]["status"] = "ERROR"
        candidate = self.candidate(**{"openligadb": verifier})
        self.assertEqual(candidate["verifiers"]["openligadb"], {"coverage": "INCOMPLETE", "result": None})
        for provider in ("football-data.org", "api-football"):
            verifier = report(provider)
            verifier["coverage"] = {"complete": False}
            self.assertEqual(self.candidate(**{provider: verifier})["verifiers"][provider]["coverage"], "INCOMPLETE")
        discovery = report()
        discovery["coverage"] = {"complete": False, "reason": "budget"}
        output = self.build(discovery)
        self.assertFalse(output["discovery"]["coverage_complete"])
        self.assertEqual(output["discovery"]["coverage"], discovery["coverage"])

    def test_duplicate_validation(self):
        for provider in (c.DISCOVERY, *c.VERIFIERS):
            first = fixture(provider)
            identical = report(provider, [first, copy.deepcopy(first)])
            if provider == c.DISCOVERY:
                self.assertEqual(self.build(identical)["total_candidates"], 1)
            else:
                self.assertEqual(self.candidate(**{provider: identical})["verifiers"][provider]["result"], "MATCH")
            other = fixture(provider, minutes=61)
            conflict = report(provider, [first, other])
            with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
                self.build(conflict) if provider == c.DISCOVERY else self.candidate(**{provider: conflict})

    def test_order_summary_and_no_mutation(self):
        discovery = report(fixtures=[fixture(identifier="z"), fixture(identifier="a"), fixture(identifier="late", minutes=100)])
        sources = {p: report(p) for p in c.VERIFIERS}
        original = copy.deepcopy((discovery, sources, REGISTRY))
        output = c.build_report(discovery, sources, REGISTRY)
        self.assertEqual([item["candidate_id"] for item in output["candidates"]], ["sfi:a", "sfi:z", "sfi:late"])
        self.assertEqual((discovery, sources, REGISTRY), original)
        self.assertEqual(sum(output["summary"].values()), output["total_candidates"])
        self.assertEqual(output["summary"]["multi_source_verified"], 2)
        self.assertEqual(output["summary"]["conflict"], 1)

    def test_invalid_reports_and_required_fields(self):
        for field, value in (("provider", "wrong"), ("fixtures", {}), ("coverage", {}),
                             ("window_start_utc", "bad"), ("window_end_utc", START.isoformat())):
            discovery = report()
            discovery[field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                self.build(discovery)
        for field, value in (("fixture_id", None), ("home_team", ""), ("away_team", 3),
                             ("kickoff_utc", "2026-10-10T01:00:00"), ("kickoff_bangkok", "bad"),
                             ("competition_id", None), ("home_team_id", False), ("status", "FINISHED")):
            discovery = report()
            discovery["fixtures"][0][field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                self.build(discovery)
        with self.assertRaises(ValueError):
            self.build(**{"openligadb": {**report("openligadb"), "coverage": {"bl1": {"status": "unknown"}}}})

    def test_invalid_registry(self):
        for registry in ({}, [], {**REGISTRY, "other": {}}, {"bundesliga": {}}):
            with self.assertRaises(ValueError):
                c.build_report(report(), registry=registry)
        bad = copy.deepcopy(REGISTRY)
        bad["bundesliga"]["api-football"]["league_ids"] = [True]
        with self.assertRaises(ValueError):
            c.build_report(report(), registry=bad)

    def test_cli_json_and_sanitized_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            path.write_text(json.dumps(report()))
            with patch.object(sys, "argv", ["handoff", "--discovery", str(path)]), patch("sys.stdout", new_callable=io.StringIO) as output:
                c.main()
            self.assertEqual(json.loads(output.getvalue())["schema_version"], "0.4B")
            path.write_text("secret-malformed-json")
            with patch.object(sys, "argv", ["handoff", "--discovery", str(path)]), patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as error:
                with self.assertRaises(SystemExit) as exit_code:
                    c.main()
                self.assertEqual(exit_code.exception.code, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertNotIn("secret", error.getvalue())
                self.assertNotIn(str(path), error.getvalue())


if __name__ == "__main__":
    unittest.main()
