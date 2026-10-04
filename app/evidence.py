"""Offline evidence contract: preserve source-reported claims without judging truth."""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SOURCE_TYPES = frozenset({"OFFICIAL", "PRIMARY_MEDIA", "REPUTABLE_MEDIA", "SPECIALIST", "TIPSTER", "OTHER"})
EVIDENCE_TYPES = frozenset({"PLAYER_AVAILABILITY", "LINEUP_ROTATION", "SCHEDULE_MOTIVATION", "TACTICAL",
                            "MANAGER_COMMENT", "TIPSTER_ANGLE", "LOCAL_NEWS", "OTHER"})
ACQUISITION_METHODS = frozenset({"API", "DIRECT_HTTP", "SCRAPY", "CRAWL4AI", "FIRECRAWL", "USER_SUPPLIED"})
SOURCE_REGISTRY_PATH = Path("config/evidence_sources.json")
REQUIRED_FIELDS = frozenset({"evidence_type", "subject", "claim", "source_id", "source_url",
                             "published_at_utc", "retrieved_at_utc", "acquisition_method", "status", "raw_text"})


def nonempty_string(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected nonempty string")
    return value


def optional_string(value):
    return None if value is None else nonempty_string(value)


def allowed(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError("Unsupported contract value")
    return value


def normalize_timestamp(value):
    nonempty_string(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def validate_url(value):
    nonempty_string(value)
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("Invalid source URL")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise ValueError("Source URL must have an HTTP(S) host")
    # Accessing port also validates malformed/out-of-range port syntax locally.
    _ = parsed.port
    return value


def validate_registry(registry):
    if not isinstance(registry, dict) or not registry:
        raise ValueError("Source registry must be a nonempty object")
    for source_id, source in registry.items():
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source_id):
            raise ValueError("Invalid source ID")
        if not isinstance(source, dict) or set(source) != {"name", "source_type", "enabled"}:
            raise ValueError("Invalid source registry fields")
        nonempty_string(source["name"])
        allowed(source["source_type"], SOURCE_TYPES)
        if type(source["enabled"]) is not bool:
            raise ValueError("Source enabled must be boolean")
    return registry


def normalize_raw_text(value):
    nonempty_string(value)
    normalized = "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
    return nonempty_string(normalized)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def evidence_identity(record):
    return {"candidate_id": record["candidate_id"], "evidence_type": record["evidence_type"],
            "subject": record["subject"], "claim": record["claim"], "source_id": record["source"]["id"],
            "source_url": record["source"]["url"], "published_at_utc": record["published_at_utc"],
            "content_hash": record["content_hash"]}


def stable_evidence_id(identity):
    return "ev:" + hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def _normalize_record(candidate_id, raw, registry):
    if not isinstance(raw, dict) or not REQUIRED_FIELDS <= set(raw) or set(raw) - (REQUIRED_FIELDS | {"title"}):
        raise ValueError("Invalid raw evidence fields")
    source_id = nonempty_string(raw["source_id"])
    if source_id not in registry or not registry[source_id]["enabled"]:
        raise ValueError("Unknown or disabled evidence source")
    source = registry[source_id]
    normalized_text = normalize_raw_text(raw["raw_text"])
    record = {
        "candidate_id": candidate_id, "evidence_type": allowed(raw["evidence_type"], EVIDENCE_TYPES),
        "subject": optional_string(raw["subject"]), "claim": nonempty_string(raw["claim"]),
        "source": {"id": source_id, "name": source["name"], "source_type": source["source_type"],
                   "url": validate_url(raw["source_url"])},
        "title": optional_string(raw.get("title")),
        "published_at_utc": None if raw["published_at_utc"] is None else normalize_timestamp(raw["published_at_utc"]),
        "retrieved_at_utc": normalize_timestamp(raw["retrieved_at_utc"]),
        "acquisition": {"method": allowed(raw["acquisition_method"], ACQUISITION_METHODS)},
        "status": allowed(raw["status"], {"REPORTED"}), "raw_text": normalized_text,
        "content_hash": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
    }
    return {"evidence_id": stable_evidence_id(evidence_identity(record)), **record}


def normalize_record(candidate_id, raw, registry):
    """Validate and normalize one raw observation without altering its claim."""
    nonempty_string(candidate_id)
    validate_registry(registry)
    return _normalize_record(candidate_id, raw, registry)


def build_bundle(candidate_id, raw_evidence, source_registry):
    """Deduplicate by stable identity, retaining the latest retrieval deterministically."""
    nonempty_string(candidate_id)
    validate_registry(source_registry)
    if not isinstance(raw_evidence, list):
        raise ValueError("Evidence must be a list")
    records = {}
    for raw in raw_evidence:
        record = _normalize_record(candidate_id, raw, source_registry)
        previous = records.get(record["evidence_id"])
        if previous is not None:
            if evidence_identity(previous) != evidence_identity(record) or previous["raw_text"] != record["raw_text"]:
                raise ValueError("Inconsistent evidence ID collision")
            # Equal retrieval times use canonical record text as a stable tie-break.
            if (datetime.fromisoformat(record["retrieved_at_utc"]), canonical_json(record)) <= (
                    datetime.fromisoformat(previous["retrieved_at_utc"]), canonical_json(previous)):
                continue
        records[record["evidence_id"]] = record
    def sort_key(record):
        published = record["published_at_utc"]
        return (published is None, datetime.fromisoformat(published) if published is not None else datetime.max.replace(tzinfo=timezone.utc),
                datetime.fromisoformat(record["retrieved_at_utc"]), record["source"]["id"], record["evidence_id"])
    evidence = sorted(records.values(), key=sort_key)
    by_evidence_type, by_source_type = {}, {}
    for record in evidence:
        kind, source_type = record["evidence_type"], record["source"]["source_type"]
        by_evidence_type[kind] = by_evidence_type.get(kind, 0) + 1
        by_source_type[source_type] = by_source_type.get(source_type, 0) + 1
    return {"schema_version": "0.5A", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "candidate_id": candidate_id, "total_evidence": len(evidence),
            "summary": {"by_evidence_type": dict(sorted(by_evidence_type.items())),
                        "by_source_type": dict(sorted(by_source_type.items()))}, "evidence": evidence}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-registry", type=Path, default=SOURCE_REGISTRY_PATH)
    args = parser.parse_args()
    try:
        data, registry = load_json(args.input), load_json(args.source_registry)
        if not isinstance(data, dict) or set(data) != {"candidate_id", "evidence"}:
            raise ValueError("Invalid evidence input envelope")
        bundle = build_bundle(data["candidate_id"], data["evidence"], registry)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
        print("ERROR: Invalid evidence input or source registry", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(bundle, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
