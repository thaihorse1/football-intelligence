import copy
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import soccer_football_info_discovery as c

START = datetime(2026, 10, 10, tzinfo=timezone.utc)
ENV = {"RAPIDAPI_KEY": "secret-test", "SOCCER_FOOTBALL_INFO_HOST": "soccer-football-info.p.rapidapi.com"}


def match(identifier="m1", date="2026-10-10 12:00:00", status="NOT_STARTED"):
    return {"id": identifier, "date": date, "status": status,
            "championship": {"id": "c1", "name": "League"},
            "teamA": {"id": "h1", "name": "Home"}, "teamB": {"id": "a1", "name": "Away"}}


def page(matches=None, number=1, items=None, per_page=25):
    matches = [match()] if matches is None else matches
    return {"status": "success", "errors": [], "pagination": [{"page": number, "per_page": per_page,
            "items": len(matches) if items is None else items}], "result": matches}


class Tests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)

    def collect(self, **kwargs):
        return c.collect(START, cache_dir=self.cache, **kwargs)

    def test_missing_credentials(self):
        for name in ENV:
            with patch.dict(os.environ, {k: v for k, v in ENV.items() if k != name}, clear=True), patch.object(c, "fetch_page") as fetch:
                with self.assertRaisesRegex(RuntimeError, name):
                    self.collect()
                fetch.assert_not_called()

    def test_transport_headers_query_and_compression(self):
        for compressed in (False, True):
            response = io.BytesIO(gzip.compress(json.dumps(page()).encode()) if compressed else json.dumps(page()).encode())
            response.headers = {"Content-Encoding": "gzip"} if compressed else {}
            with patch.object(c.urllib.request.OpenerDirector, "open", return_value=response) as fetch:
                self.assertEqual(c.fetch_page(START.date(), 1, ENV["RAPIDAPI_KEY"], ENV["SOCCER_FOOTBALL_INFO_HOST"]), page())
            request = fetch.call_args.args[0]
            self.assertEqual(request.get_header("X-rapidapi-key"), ENV["RAPIDAPI_KEY"])
            self.assertEqual(request.get_header("X-rapidapi-host"), ENV["SOCCER_FOOTBALL_INFO_HOST"])
            self.assertTrue(request.get_header("User-agent"))
            self.assertNotIn(ENV["RAPIDAPI_KEY"], request.full_url)
            self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"d": ["20261010"], "p": ["1"], "l": ["en_US"], "f": ["json"]})

    def test_transport_errors(self):
        for failure in (urllib.error.HTTPError("https://test", 429, "secret", {}, None), urllib.error.URLError("secret")):
            with patch.object(c.urllib.request.OpenerDirector, "open", side_effect=failure):
                with self.assertRaises(RuntimeError) as error:
                    c.fetch_page(START.date(), 1, "secret", "test")
                self.assertNotIn("secret", str(error.exception))
        for body in (b"bad-json", b"not-gzip"):
            response = io.BytesIO(body)
            response.headers = {"Content-Encoding": "gzip"} if body == b"not-gzip" else {}
            with patch.object(c.urllib.request.OpenerDirector, "open", return_value=response), self.assertRaises(RuntimeError):
                c.fetch_page(START.date(), 1, "secret", "test")

    def test_response_validation(self):
        bad = [[], {}, page()]
        bad[-1]["errors"] = ["error"]
        for field, value in (("status", "error"), ("result", {}), ("pagination", [])):
            data = page()
            data[field] = value
            bad.append(data)
        for field, value in (("page", 2), ("per_page", 0), ("items", -1), ("items", "1"), ("items", 2)):
            data = page()
            data["pagination"][0][field] = value
            bad.append(data)
        for data in bad:
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                c.validate_page(data, 1)
        self.assertEqual(c.validate_page(page([match()] * 25, items=51), 1)[0], 3)
        self.assertEqual(c.validate_page(page([]), 1)[0], 1)

    def test_schema_and_immutability(self):
        raw = match()
        original = copy.deepcopy(raw)
        fixture = c.normalize_match(raw)
        self.assertEqual(raw, original)
        self.assertEqual(fixture["kickoff_utc"], "2026-10-10T12:00:00+00:00")
        self.assertEqual(fixture["kickoff_bangkok"], "2026-10-10T19:00:00+07:00")
        for key, value in (("fixture_id", "m1"), ("competition_id", "c1"), ("home_team_id", "h1"), ("away_team_id", "a1"), ("status", "SCHEDULED"), ("discovery_status", "DISCOVERED"), ("verification_status", "UNVERIFIED")):
            self.assertEqual(fixture[key], value)
        # Verified BASIC mapping: teamA is home and teamB is away.
        self.assertEqual(fixture["home_team"], raw["teamA"]["name"])
        self.assertEqual(fixture["away_team"], raw["teamB"]["name"])
        self.assertIsNone(fixture["country"])
        for status in ("FINISHED", "IN_PLAY", "CANCELLED", "POSTPONED", "UNKNOWN"):
            self.assertIsNone(c.normalize_match(match(status=status)))
        for field, value in (("id", None), ("date", "bad"), ("teamA", {}), ("teamB", {}), ("status", None)):
            raw = match()
            raw[field] = value
            with self.assertRaises(ValueError):
                c.normalize_match(raw)

    def test_window_duplicates_sort(self):
        matches = [match(), match("early", "2026-10-10T00:00:00Z"), match(),
                   match("end", "2026-10-11T00:00:00Z"), match("before", "2026-10-09T23:59:59Z")]
        original = copy.deepcopy(matches)
        with patch.object(c, "fetch_page", return_value=page(matches)):
            report = self.collect()
        self.assertEqual([f["fixture_id"] for f in report["fixtures"]], ["early", "m1"])
        self.assertEqual(matches, original)
        with patch.object(c, "fetch_page", return_value=page([match(), match(date="2026-10-10 14:00:00")])):
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                self.collect(refresh=True)

    def test_cache_and_refresh(self):
        with patch.object(c, "fetch_page", return_value=page()) as fetch:
            first = self.collect()
            second = self.collect()
            self.collect(refresh=True)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(first["coverage"]["network_requests"], 1)
        self.assertEqual(second["coverage"]["network_requests"], 0)
        self.assertEqual(second["coverage"]["cache_hits"], 1)
        self.assertEqual(json.loads((self.cache / "2026-10-10/page-001.json").read_text()), page())

    def test_atomic_write_failure(self):
        target = self.cache / "page.json"
        c.write_cache(target, page())
        with self.assertRaises(TypeError):
            c.write_cache(target, {"bad": object()})
        self.assertEqual(json.loads(target.read_text()), page())
        self.assertEqual(list(self.cache.iterdir()), [target])

    def test_budget_partial_complete_and_multidate(self):
        def fetch(day, number, *args):
            return page([match(f"{day}-{number}", f"{day}T12:00:00Z")], number, items=2, per_page=1)
        with patch.object(c, "fetch_page", side_effect=fetch) as network:
            partial = self.collect(hours=48, max_requests=1)
            self.assertEqual(network.call_count, 1)
            self.assertFalse(partial["coverage"]["complete"])
            self.assertTrue(partial["coverage"]["reason"])
            complete = self.collect(hours=48, max_requests=3)
        self.assertTrue(complete["coverage"]["complete"])
        self.assertIsNone(complete["coverage"]["reason"])
        self.assertEqual(complete["coverage"]["pages_processed"], 4)
        self.assertEqual(complete["coverage"]["network_requests"], 3)
        self.assertEqual(len(complete["coverage"]["per_date"]), 2)
        self.assertEqual(len(complete["fixtures"]), 4)

    def test_invalid_cache_reacquired_and_pagination_change_fails(self):
        path = self.cache / "2026-10-10/page-001.json"
        path.parent.mkdir()
        path.write_text("invalid")
        with patch.object(c, "fetch_page", return_value=page()) as fetch:
            self.assertTrue(self.collect()["coverage"]["complete"])
            fetch.assert_called_once()
        responses = [page([match("one")], 1, items=2, per_page=1),
                     page([match("two")], 2, items=3, per_page=1)]
        with patch.object(c, "fetch_page", side_effect=responses), self.assertRaisesRegex(RuntimeError, "Pagination changed"):
            self.collect(refresh=True)

    def test_timezone_window_and_exclusive_midnight(self):
        self.assertEqual(c.parse_start("2026-10-10T07:00:00+07:00"), START)
        self.assertEqual(c.provider_kickoff("2026-10-10T19:00:00+07:00").hour, 12)
        with patch.object(c, "fetch_page", return_value=page([])) as fetch:
            report = self.collect()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(report["coverage"]["dates_requested"], ["2026-10-10"])

    def test_cli_validation_and_json(self):
        for args in (["--hours", "0"], ["--hours", "nan"], ["--max-requests", "0"], ["--from", "2026-10-10"]):
            with patch.object(sys, "argv", ["collector"] + args), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                c.main()
            self.assertEqual(error.exception.code, 2)
        with patch.object(sys, "argv", ["collector", "--from", START.isoformat(), "--cache-dir", str(self.cache)]), patch.object(c, "fetch_page", return_value=page([])), patch("sys.stdout", new_callable=io.StringIO) as output:
            c.main()
        report = json.loads(output.getvalue())
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(report["total_fixtures"], 0)


if __name__ == "__main__":
    unittest.main()
