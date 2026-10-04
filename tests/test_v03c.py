import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import api_football_fixtures as collector
from app import reconcile_fixtures as reconciliation

START = datetime(2026, 9, 26, tzinfo=timezone.utc)
END = START + timedelta(days=30)


def raw_fixture(identifier=1, date="2026-09-27T17:30:00+02:00", status="NS"):
    return {
        "fixture": {"id": identifier, "date": date, "status": {"short": status}},
        "league": {"id": 78, "name": "Bundesliga"},
        "teams": {
            "home": {"id": 1, "name": "Bayern Munich"},
            "away": {"id": 2, "name": "Borussia Dortmund"},
        },
    }


def fixture(identifier=1, minutes=0, home="Bayern Munich", away="Borussia Dortmund"):
    return {
        "fixture_id": identifier, "competition_code": "BL1", "competition": "Bundesliga",
        "home_team": home, "away_team": away,
        "kickoff_utc": (START + timedelta(minutes=minutes)).isoformat(),
    }


class CollectorTests(unittest.TestCase):
    def test_normalized_schema_and_timezones(self):
        match = collector.normalize_match(raw_fixture())
        self.assertEqual(match["kickoff_utc"], "2026-09-27T15:30:00+00:00")
        self.assertEqual(match["kickoff_bangkok"], "2026-09-27T22:30:00+07:00")
        self.assertEqual(match["status"], "SCHEDULED")
        self.assertEqual(match["provider_status"], "NS")
        self.assertEqual(match["competition_code"], "BL1")
        self.assertEqual(match["verification_status"], "SOURCE_ONLY")

    def test_window_deduplication_and_sorting(self):
        first = raw_fixture(1, START.isoformat())
        later = raw_fixture(2)
        matches = [later, first, later, raw_fixture(3, END.isoformat()),
                   raw_fixture(4, (START - timedelta(seconds=1)).isoformat())]
        report = collector.build_report(matches, START, END, [2026])
        self.assertEqual([m["fixture_id"] for m in report["fixtures"]], [1, 2])
        self.assertEqual(report["total_fixtures"], 2)

    def test_non_scheduled_and_other_leagues_excluded(self):
        for status in ["TBD", "PST", "CANC", "FT", "1H", "ABD", "SUSP"]:
            with self.subTest(status=status):
                self.assertIsNone(collector.normalize_match(raw_fixture(status=status)))
        match = raw_fixture()
        match["league"]["id"] = 39
        self.assertIsNone(collector.normalize_match(match))

    def test_malformed_scheduled_matches_fail(self):
        for date in ["bad", "2026-09-27T15:00:00"]:
            with self.assertRaises(ValueError):
                collector.normalize_match(raw_fixture(date=date))
        match = raw_fixture()
        match["teams"]["home"]["name"] = None
        with self.assertRaises(ValueError):
            collector.normalize_match(match)

    def test_conflicting_duplicate_fails(self):
        with self.assertRaises(ValueError):
            collector.build_report([raw_fixture(), raw_fixture(date="2026-09-28T15:00:00Z")], START, END, [2026])

    def test_missing_key_makes_no_request(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(collector.urllib.request, "urlopen") as fetch:
            with self.assertRaisesRegex(RuntimeError, "API_FOOTBALL_API_KEY"):
                collector.fetch_matches(2026, START, END)
            fetch.assert_not_called()

    def test_request_and_response(self):
        payload = {"errors": [], "response": [raw_fixture()], "paging": {"current": 1, "total": 1}}
        with patch.dict(os.environ, {"API_FOOTBALL_API_KEY": "test-key"}), patch.object(
            collector.urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))
        ) as fetch:
            self.assertEqual(collector.fetch_matches(2026, START, END), payload["response"])
        request = fetch.call_args.args[0]
        self.assertEqual(request.get_header("X-apisports-key"), "test-key")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["league"], ["78"])
        self.assertNotIn("test-key", request.full_url)
        self.assertEqual(fetch.call_args.kwargs["timeout"], 30)

    def test_api_errors_malformed_payload_and_pagination(self):
        for payload in [{"errors": {"token": "invalid"}, "response": []}, [], {},
                        {"response": {}, "errors": []},
                        {"response": [], "paging": {"total": 2}}]:
            with self.subTest(payload=payload), patch.dict(os.environ, {"API_FOOTBALL_API_KEY": "test"}), patch.object(
                collector.urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))
            ):
                with self.assertRaises(RuntimeError):
                    collector.fetch_matches(2026, START, END)

    def test_http_error(self):
        error = urllib.error.HTTPError("https://example.test", 429, "quota", {}, None)
        with patch.dict(os.environ, {"API_FOOTBALL_API_KEY": "test"}), patch.object(
            collector.urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                collector.fetch_matches(2026, START, END)

    def test_cli_season_boundary(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 25, tzinfo=timezone.utc)
        with patch.object(collector, "datetime", Clock), patch.object(
            sys, "argv", ["collector", "--days", "10"]
        ), patch.object(collector, "fetch_matches", return_value=[]) as fetch, patch("sys.stdout", new_callable=io.StringIO) as output:
            collector.main()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [2025, 2026])
        self.assertEqual(json.loads(output.getvalue())["total_fixtures"], 0)

    def test_cli_error_has_no_report(self):
        with patch.object(sys, "argv", ["collector"]), patch.object(
            collector, "fetch_matches", side_effect=RuntimeError("API error")
        ), patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                collector.main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(output.getvalue(), "")


class ReconciliationTests(unittest.TestCase):
    def test_legacy_output_shape_and_statuses(self):
        a, b = fixture(), fixture(2)
        result = reconciliation.reconcile([a], [b])
        self.assertEqual(result, [{"status": "MATCHED", "football_data": a, "openliga": b,
                                  "team_similarity": 1.0, "kickoff_difference_minutes": 0.0}])
        self.assertEqual(reconciliation.reconcile([a], [fixture(2, 16)])[0]["status"], "KICKOFF_CONFLICT")
        self.assertEqual(reconciliation.reconcile([a], [])[0]["status"], "ONLY_FOOTBALL_DATA")
        self.assertEqual(reconciliation.reconcile([], [b])[0]["status"], "ONLY_OPENLIGA")

    def test_three_agree_and_missing_source(self):
        result = reconciliation.reconcile([fixture()], [fixture(2)], [fixture(3)])[0]
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(result["missing_sources"], [])
        result = reconciliation.reconcile([], [fixture(2)], [fixture(3)])[0]
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["missing_sources"], ["football_data"])
        self.assertEqual(reconciliation.reconcile([], [], [fixture()])[0]["status"], "ONLY_API_FOOTBALL")
        self.assertEqual(reconciliation.reconcile([], [], []), [])

    def test_pairwise_tolerance_and_majority_does_not_hide_conflict(self):
        for minutes, expected in [(15, "MATCHED"), (16, "KICKOFF_CONFLICT")]:
            result = reconciliation.reconcile([fixture()], [fixture(2)], [fixture(3, minutes)])[0]
            self.assertEqual(result["status"], expected)
        result = reconciliation.reconcile([fixture()], [fixture(2, -10)], [fixture(3, 10)])[0]
        self.assertEqual(result["kickoff_difference_minutes"], 20)
        self.assertEqual(result["status"], "KICKOFF_CONFLICT")

    def test_fuzzy_names_and_both_conflicts(self):
        result = reconciliation.reconcile([fixture()], [fixture(2)], [fixture(3, 20, home="Bayern Munic")])[0]
        self.assertEqual(result["conflicts"], ["TEAM_NAME_CONFLICT", "KICKOFF_CONFLICT"])
        result = reconciliation.reconcile([fixture()], [], [fixture(3, home="Bayern Munic")])[0]
        self.assertEqual(result["status"], "TEAM_NAME_CONFLICT")

    def test_repeated_match_is_ambiguous_and_preserved(self):
        result = reconciliation.reconcile([fixture(), fixture(4, 60)], [fixture(2)], [fixture(3)])
        self.assertEqual(len(result), 4)
        self.assertTrue(all(row["status"] == "AMBIGUOUS_MATCH" for row in result))
        self.assertTrue(all(row["source_count"] == 1 for row in result))

    def test_nontransitive_time_chain_is_ambiguous(self):
        result = reconciliation.reconcile([fixture()], [fixture(2, 1000)], [fixture(3, 2000)])
        self.assertEqual(len(result), 3)
        self.assertTrue(all(row["status"] == "AMBIGUOUS_MATCH" for row in result))

    def test_far_apart_and_reversed_matches_are_separate(self):
        self.assertEqual(len(reconciliation.reconcile([fixture()], [], [fixture(3, 1441)])), 2)
        reverse = fixture(3, home="Borussia Dortmund", away="Bayern Munich")
        self.assertEqual(len(reconciliation.reconcile([fixture()], [], [reverse])), 2)

    def test_inputs_unchanged(self):
        inputs = ([fixture()], [fixture(2)], [fixture(3)])
        original = copy.deepcopy(inputs)
        reconciliation.reconcile(*inputs)
        self.assertEqual(inputs, original)

    def test_cli_three_provider_report(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for i in range(3):
                path = Path(directory) / f"{i}.json"
                path.write_text(json.dumps({"fixtures": [fixture(i)]}))
                paths.append(str(path))
            result = subprocess.run(
                [sys.executable, "-m", "app.reconcile_fixtures", "--football-data", paths[0],
                 "--openliga", paths[1], "--api-football", paths[2]],
                capture_output=True, text=True, check=True,
            )
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"], {"MATCHED": 1})
        self.assertEqual(report["api_football_fixtures"], 1)
        self.assertEqual(len(report["sources"]), 3)


if __name__ == "__main__":
    unittest.main()
