import copy
import hashlib
import io
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import evidence as e

CANDIDATE = "candidate:any-format"
REGISTRY = json.loads(Path("config/evidence_sources.json").read_text())


def raw(**changes):
    record = {"evidence_type": "PLAYER_AVAILABILITY", "subject": "Player Name",
              "claim": "Source reports player doubtful", "source_id": "wettbasis",
              "source_url": "https://example.com/article", "title": "Match preview",
              "published_at_utc": "2026-10-04T08:00:00+00:00",
              "retrieved_at_utc": "2026-10-04T09:00:00+00:00",
              "acquisition_method": "DIRECT_HTTP", "status": "REPORTED", "raw_text": "Relevant source text"}
    record.update(changes)
    return record


class EvidenceTests(unittest.TestCase):
    def normalize(self, record=None, registry=None):
        return e.normalize_record(CANDIDATE, raw() if record is None else record, REGISTRY if registry is None else registry)

    def bundle(self, records):
        return e.build_bundle(CANDIDATE, records, REGISTRY)

    def test_valid_record_schema_and_provenance(self):
        record = self.normalize()
        self.assertEqual(set(record), {"evidence_id", "candidate_id", "evidence_type", "subject", "claim", "source",
                                      "title", "published_at_utc", "retrieved_at_utc", "acquisition", "status", "raw_text", "content_hash"})
        self.assertEqual(record["candidate_id"], CANDIDATE)
        self.assertEqual(record["source"], {"id": "wettbasis", "name": "Wettbasis", "source_type": "TIPSTER", "url": raw()["source_url"]})
        self.assertEqual(record["claim"], raw()["claim"])
        self.assertEqual(record["status"], "REPORTED")
        self.assertEqual(record["acquisition"], {"method": "DIRECT_HTTP"})
        self.assertRegex(record["evidence_id"], r"^ev:[0-9a-f]{64}$")

    def test_initial_registry(self):
        self.assertEqual(set(REGISTRY), {"wettbasis", "wettfreunde", "leaguelane", "gooners-guide"})
        e.validate_registry(REGISTRY)
        for source in REGISTRY.values():
            self.assertEqual(source["source_type"], "TIPSTER")
            self.assertTrue(source["enabled"])

    def test_registry_validation(self):
        invalid = [None, [], {}, {"": {}}, {"Upper": {}}, {"has space": {}}, {"id_underscore": {}}, {1: {}},
                   {"source": None}]
        for field, value in (("name", ""), ("name", 3), ("source_type", "UNKNOWN"), ("source_type", []),
                             ("enabled", 1), ("enabled", "true")):
            registry = copy.deepcopy(REGISTRY)
            registry["wettbasis"][field] = value
            invalid.append(registry)
        registry = copy.deepcopy(REGISTRY)
        registry["wettbasis"]["url"] = "https://example.com"
        invalid.append(registry)
        registry = copy.deepcopy(REGISTRY)
        del registry["wettbasis"]["name"]
        invalid.append(registry)
        for registry in invalid:
            with self.subTest(registry=registry), self.assertRaises(ValueError):
                e.validate_registry(registry)

    def test_source_id_leading_hyphen_rejected(self):
        with self.assertRaises(ValueError):
            e.validate_registry({"-source": REGISTRY["wettbasis"]})

    def test_source_id_trailing_hyphen_rejected(self):
        with self.assertRaises(ValueError):
            e.validate_registry({"source-": REGISTRY["wettbasis"]})

    def test_source_id_consecutive_hyphens_rejected(self):
        with self.assertRaises(ValueError):
            e.validate_registry({"gooners--guide": REGISTRY["gooners-guide"]})

    def test_unknown_and_disabled_sources(self):
        with self.assertRaises(ValueError):
            self.normalize(raw(source_id="unknown"))
        registry = copy.deepcopy(REGISTRY)
        registry["wettbasis"]["enabled"] = False
        with self.assertRaises(ValueError):
            self.normalize(registry=registry)

    def test_all_source_types(self):
        for kind in e.SOURCE_TYPES:
            registry = {"synthetic-1": {"name": "Synthetic", "source_type": kind, "enabled": True}}
            self.assertEqual(self.normalize(raw(source_id="synthetic-1"), registry)["source"]["source_type"], kind)

    def test_all_evidence_types(self):
        for kind in e.EVIDENCE_TYPES:
            self.assertEqual(self.normalize(raw(evidence_type=kind))["evidence_type"], kind)

    def test_all_acquisition_methods(self):
        for method in e.ACQUISITION_METHODS:
            self.assertEqual(self.normalize(raw(acquisition_method=method))["acquisition"]["method"], method)

    def test_null_subject_publication_and_optional_title(self):
        record = raw(subject=None, published_at_utc=None)
        del record["title"]
        normalized = self.normalize(record)
        for field in ("subject", "published_at_utc", "title"):
            self.assertIsNone(normalized[field])
        self.assertIsNone(self.normalize(raw(title=None))["title"])

    def test_timestamps_normalize_to_utc(self):
        first = self.normalize()
        second = self.normalize(raw(published_at_utc="2026-10-04T15:00:00+07:00", retrieved_at_utc="2026-10-04T11:00:00+02:00"))
        self.assertEqual(first, second)
        self.assertEqual(self.normalize(raw(published_at_utc="2026-10-04T08:00:00Z"))["evidence_id"], first["evidence_id"])

    def test_invalid_and_naive_timestamps(self):
        for field in ("published_at_utc", "retrieved_at_utc"):
            for value in ("invalid", "2026-10-04T08:00:00", "2026-10-04", "2026-02-30T00:00:00Z", 123, ""):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.normalize(raw(**{field: value}))
        with self.assertRaises(ValueError):
            self.normalize(raw(retrieved_at_utc=None))

    def test_http_https_and_url_preservation(self):
        for url in ("http://example.com/article", "https://example.com:443/a?b=1#part", "https://[::1]/article", "HTTPS://example.com/CaseSensitive"):
            self.assertEqual(self.normalize(raw(source_url=url))["source"]["url"], url)

    def test_invalid_urls(self):
        for url in ("ftp://example.com/a", "file:///tmp/article", "javascript:alert(1)", "https:///article", "//example.com", "https://", "https://:80/a", "https://example.com:invalid/a", "https://example.com:99999/", "https://[broken/", "https://example.com/has space", "https://example.com/\n", "", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.normalize(raw(source_url=url))

    def test_raw_text_newlines_and_trailing_whitespace(self):
        text = " \r\nFirst  internal  space  \t\rSecond\t \r\nThird  \r\n\t "
        self.assertEqual(self.normalize(raw(raw_text=text))["raw_text"], "First  internal  space\nSecond\nThird")
        self.assertNotEqual(self.normalize(raw(raw_text="Case"))["content_hash"], self.normalize(raw(raw_text="case"))["content_hash"])
        self.assertNotEqual(self.normalize(raw(raw_text="a  b"))["content_hash"], self.normalize(raw(raw_text="a b"))["content_hash"])

    def test_content_hash_expected_digest(self):
        record = self.normalize(raw(raw_text="\r\nÄ Text  \r\nSecond\t \r"))
        self.assertEqual(record["content_hash"], hashlib.sha256("Ä Text\nSecond".encode("utf-8")).hexdigest())
        self.assertEqual(record, self.normalize(raw(raw_text="Ä Text\nSecond")))

    def test_evidence_id_expected_identity(self):
        record = self.normalize()
        identity = {"candidate_id": CANDIDATE, "evidence_type": "PLAYER_AVAILABILITY", "subject": "Player Name",
                    "claim": raw()["claim"], "source_id": "wettbasis", "source_url": raw()["source_url"],
                    "published_at_utc": "2026-10-04T08:00:00+00:00", "content_hash": record["content_hash"]}
        serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(record["evidence_id"], "ev:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest())

    def test_retrieval_and_method_independent_identity(self):
        original = self.normalize()["evidence_id"]
        self.assertEqual(self.normalize(raw(retrieved_at_utc="2026-10-05T09:00:00Z"))["evidence_id"], original)
        self.assertEqual(self.normalize(raw(acquisition_method="USER_SUPPLIED"))["evidence_id"], original)
        self.assertEqual(self.normalize(raw(title="Different title"))["evidence_id"], original)

    def test_identity_changes_for_each_identity_field(self):
        original = self.normalize()["evidence_id"]
        for changes in ({"claim": "Different claim"}, {"source_url": "https://example.com/other"}, {"raw_text": "Different content"},
                        {"source_id": "wettfreunde"}, {"subject": None}, {"evidence_type": "OTHER"}, {"published_at_utc": None}):
            self.assertNotEqual(self.normalize(raw(**changes))["evidence_id"], original)
        self.assertNotEqual(e.normalize_record("other-candidate", raw(), REGISTRY)["evidence_id"], original)

    def test_exact_duplicates_collapse(self):
        bundle = self.bundle([raw(), raw(), raw(raw_text="Relevant source text  \r\n")])
        self.assertEqual(bundle["total_evidence"], 1)

    def test_duplicates_retain_latest_retrieval(self):
        earlier = raw()
        latest = raw(retrieved_at_utc="2026-10-05T18:00:00+07:00", acquisition_method="USER_SUPPLIED", title="Latest title")
        for records in ([earlier, latest], [latest, earlier]):
            selected = self.bundle(records)["evidence"][0]
            self.assertEqual(selected["retrieved_at_utc"], "2026-10-05T11:00:00+00:00")
            self.assertEqual(selected["acquisition"]["method"], "USER_SUPPLIED")
            self.assertEqual(selected["title"], "Latest title")

    def test_equal_retrieval_tie_is_deterministic(self):
        records = [raw(), raw(acquisition_method="USER_SUPPLIED", title="Other title")]
        self.assertEqual(self.bundle(records)["evidence"], self.bundle(list(reversed(records)))["evidence"])

    def test_contradictory_claims_coexist(self):
        records = [raw(claim="Player expected to miss match"), raw(source_id="wettfreunde", claim="Player returned to full training")]
        self.assertEqual(self.bundle(records)["total_evidence"], 2)

    def test_same_source_different_claims_coexist(self):
        self.assertEqual(self.bundle([raw(), raw(claim="Player available")])["total_evidence"], 2)

    def test_different_sources_identical_content_coexist(self):
        self.assertEqual(self.bundle([raw(), raw(source_id="wettfreunde")])["total_evidence"], 2)

    def test_deterministic_ordering(self):
        records = [raw(claim="later", published_at_utc="2026-10-05T08:00:00Z"), raw(claim="unknown", published_at_utc=None), raw(claim="earlier"), raw(source_id="wettfreunde", claim="other source")]
        expected = self.bundle(records)["evidence"]
        for permutation in itertools.permutations(records):
            self.assertEqual(self.bundle(list(permutation))["evidence"], expected)
        self.assertEqual(expected[-1]["claim"], "unknown")
        self.assertEqual(expected[-2]["claim"], "later")

    def test_summary_and_empty_bundle(self):
        registry = copy.deepcopy(REGISTRY)
        registry["synthetic"] = {"name": "Synthetic", "source_type": "OFFICIAL", "enabled": True}
        records = [raw(), raw(claim="other"), raw(source_id="synthetic", evidence_type="TIPSTER_ANGLE")]
        bundle = e.build_bundle(CANDIDATE, records, registry)
        self.assertEqual(bundle["summary"], {"by_evidence_type": {"PLAYER_AVAILABILITY": 2, "TIPSTER_ANGLE": 1}, "by_source_type": {"OFFICIAL": 1, "TIPSTER": 2}})
        for counts in bundle["summary"].values():
            self.assertEqual(list(counts), sorted(counts))
        empty = self.bundle([])
        self.assertEqual(empty["total_evidence"], 0)
        self.assertEqual(empty["summary"], {"by_evidence_type": {}, "by_source_type": {}})

    def test_malformed_records_and_required_values(self):
        for field in e.REQUIRED_FIELDS:
            record = raw()
            del record[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                self.normalize(record)
        for field, value in (("claim", " "), ("raw_text", "\r\n\t"), ("evidence_type", "WRONG"), ("acquisition_method", "WRONG"),
                             ("status", "CONFIRMED"), ("status", False), ("source_id", [])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize(raw(**{field: value}))
        for value in ([], "bad", {**raw(), "unknown": "field"}):
            with self.assertRaises(ValueError):
                self.normalize(value)
        for candidate in (None, "", "   ", 123):
            with self.assertRaises(ValueError):
                e.build_bundle(candidate, [], REGISTRY)
        with self.assertRaises(ValueError):
            self.bundle({})

    def test_malformed_subject_and_title(self):
        for field in ("subject", "title"):
            for value in ("", "  ", 1, {}, []):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.normalize(raw(**{field: value}))

    def test_internal_collision_rejected(self):
        with patch.object(e, "stable_evidence_id", return_value="ev:forced-collision"), self.assertRaisesRegex(ValueError, "collision"):
            self.bundle([raw(), raw(claim="Different identity")])

    def test_input_objects_unchanged(self):
        records, registry = [raw(), raw(raw_text=" trailing  \r\n")], copy.deepcopy(REGISTRY)
        original = copy.deepcopy((records, registry))
        e.build_bundle(CANDIDATE, records, registry)
        self.assertEqual((records, registry), original)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text('{"source":{},"source":{}}')
            with self.assertRaises(ValueError):
                e.load_json(path)

    def test_cli_valid_json_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path, registry_path = Path(directory) / "input.json", Path(directory) / "registry.json"
            registry_path.write_text(json.dumps(REGISTRY))
            argv = ["evidence", "--input", str(path), "--source-registry", str(registry_path)]
            path.write_text(json.dumps({"candidate_id": CANDIDATE, "evidence": [raw()]}))
            with patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as output:
                e.main()
            self.assertEqual(json.loads(output.getvalue())["schema_version"], "0.5A")
            failures = ["secret-not-json", "[]", '{}', json.dumps({"candidate_id": CANDIDATE, "evidence": [raw(), raw(source_id="secret-invalid")]})]
            for invalid in failures:
                path.write_text(invalid)
                with patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as error:
                    with self.assertRaises(SystemExit) as exit_code:
                        e.main()
                    self.assertNotEqual(exit_code.exception.code, 0)
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(error.getvalue(), "ERROR: Invalid evidence input or source registry\n")


if __name__ == "__main__":
    unittest.main()
