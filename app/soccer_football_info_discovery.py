"""Supplementary worldwide discovery; records remain DISCOVERED and UNVERIFIED."""

import argparse
import gzip
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PROVIDER = "soccer-football-info"
BANGKOK = ZoneInfo("Asia/Bangkok")


def parse_start(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Window start must be timezone-aware")
    return value.astimezone(timezone.utc)


def provider_kickoff(value):
    """Provider naive date/time is interpreted as UTC, based on prior live testing.

    Keep this assumption here so later timezone evidence can change it centrally.
    """
    if not isinstance(value, str):
        raise ValueError("Invalid provider kickoff")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        raise ValueError("Invalid provider kickoff") from None


def required(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"Missing or invalid {label}")
    if (label.endswith("name") or label == "provider status") and not isinstance(value, str):
        raise ValueError(f"Invalid {label}")
    return value


def normalize_match(match):
    if not isinstance(match, dict):
        raise ValueError("Invalid fixture object")
    status = match.get("status")
    required(status, "provider status")
    # Every other state (including unknown future states) is explicitly excluded.
    if status != "NOT_STARTED":
        return None
    competition = match.get("championship")
    home, away = match.get("teamA"), match.get("teamB")
    if not all(isinstance(item, dict) for item in (competition, home, away)):
        raise ValueError("Invalid competition or team object")
    kickoff = provider_kickoff(match.get("date"))
    return {
        "provider": PROVIDER, "fixture_id": required(match.get("id"), "fixture ID"),
        "competition_id": required(competition.get("id"), "competition ID"),
        "competition": required(competition.get("name"), "competition name"),
        "country": None,
        "home_team_id": required(home.get("id"), "home team ID"),
        "home_team": required(home.get("name"), "home team name"),
        "away_team_id": required(away.get("id"), "away team ID"),
        "away_team": required(away.get("name"), "away team name"),
        "kickoff_utc": kickoff.isoformat(), "kickoff_bangkok": kickoff.astimezone(BANGKOK).isoformat(),
        "status": "SCHEDULED", "provider_status": status,
        "discovery_status": "DISCOVERED", "verification_status": "UNVERIFIED",
    }


def validate_page(data, page):
    if not isinstance(data, dict) or data.get("status") not in ("success", "SUCCESS", "OK", "ok", 200):
        raise RuntimeError("Provider error envelope or invalid status")
    if "errors" not in data or data["errors"] not in ([], {}, None):
        raise RuntimeError("Provider error envelope")
    if not isinstance(data.get("result"), list):
        raise RuntimeError("Invalid provider result list")
    pagination = data.get("pagination")
    if not isinstance(pagination, list) or len(pagination) != 1 or not isinstance(pagination[0], dict):
        raise RuntimeError("Invalid pagination metadata")
    meta = pagination[0]
    for field in ("page", "per_page", "items"):
        if type(meta.get(field)) is not int:
            raise RuntimeError("Invalid pagination values")
    if meta["page"] != page or meta["per_page"] <= 0 or meta["items"] < 0:
        raise RuntimeError("Invalid pagination values")
    pages = max(1, math.ceil(meta["items"] / meta["per_page"]))
    expected = min(meta["per_page"], max(0, meta["items"] - (page - 1) * meta["per_page"]))
    if page > pages or len(data["result"]) != expected:
        raise RuntimeError("Pagination and result length disagree")
    for match in data["result"]:
        normalize_match(match)
    return pages, (meta["items"], meta["per_page"])


def credentials():
    key, host = os.environ.get("RAPIDAPI_KEY"), os.environ.get("SOCCER_FOOTBALL_INFO_HOST")
    if not key:
        raise RuntimeError("RAPIDAPI_KEY is missing")
    if not host:
        raise RuntimeError("SOCCER_FOOTBALL_INFO_HOST is missing")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("Invalid SOCCER_FOOTBALL_INFO_HOST")
    return key, host


def fetch_page(day, page, key, host):
    query = urllib.parse.urlencode({"d": day.strftime("%Y%m%d"), "p": page, "l": "en_US", "f": "json"})
    request = urllib.request.Request(f"https://{host}/matches/day/basic/?{query}", headers={
        "X-RapidAPI-Key": key, "X-RapidAPI-Host": host,
        "User-Agent": "football-intelligence/0.4A", "Accept": "application/json", "Accept-Encoding": "gzip",
    })
    # No redirects: urllib would forward sensitive headers and add unbudgeted requests.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=30) as response:
            body = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower().strip()
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding not in ("", "identity"):
                raise RuntimeError("Unsupported provider content encoding")
            data = json.loads(body)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Soccer Football Info HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError, EOFError):
        raise RuntimeError("Provider transport or JSON decoding failed") from None
    validate_page(data, page)
    return data


def write_cache(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def collect(start, hours=24, max_requests=40, cache_dir=Path("data/cache/soccer_football_info"), refresh=False):
    key, host = credentials()
    if start.tzinfo is None or hours <= 0 or not math.isfinite(hours) or type(max_requests) is not int or max_requests <= 0:
        raise ValueError("Invalid window or request budget")
    start = start.astimezone(timezone.utc)
    end = start + timedelta(hours=hours)
    dates = []
    day = start.date()
    while datetime.combine(day, datetime.min.time(), timezone.utc) < end:
        dates.append(day)
        day += timedelta(days=1)
    coverage = {"complete": True, "dates_requested": [d.isoformat() for d in dates],
                "pages_available": {}, "pages_processed": 0, "network_requests": 0,
                "cache_hits": 0, "max_requests": max_requests, "reason": None, "per_date": {}}
    fixtures = {}
    for day in dates:
        detail = {"pages_available": None, "pages_processed": 0, "complete": False}
        coverage["per_date"][day.isoformat()] = detail
        coverage["pages_available"][day.isoformat()] = None
        page, pages, previous_meta = 1, 1, None
        while page <= pages:
            path = Path(cache_dir) / day.isoformat() / f"page-{page:03d}.json"
            data = None
            if path.exists() and not refresh:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    validate_page(data, page)
                except (OSError, ValueError, RuntimeError):
                    data = None
                else:
                    coverage["cache_hits"] += 1
            if data is None:
                if coverage["network_requests"] >= max_requests:
                    coverage["complete"] = False
                    coverage["reason"] = "Request budget exhausted; provider pages remain uninspected"
                    break
                coverage["network_requests"] += 1
                data = fetch_page(day, page, key, host)
                write_cache(path, data)
            pages, metadata = validate_page(data, page)
            if previous_meta is not None and metadata != previous_meta:
                raise RuntimeError("Pagination changed during collection; coverage cannot be established")
            previous_meta = metadata
            detail["pages_available"] = pages
            coverage["pages_available"][day.isoformat()] = pages
            for raw in data["result"]:
                fixture = normalize_match(raw)
                if fixture is None:
                    continue
                identifier = str(fixture["fixture_id"])
                if identifier in fixtures and fixtures[identifier] != fixture:
                    raise ValueError("Conflicting duplicate Soccer Football Info fixture")
                fixtures[identifier] = fixture
            detail["pages_processed"] += 1
            coverage["pages_processed"] += 1
            page += 1
        detail["complete"] = page > pages
    selected = [f for f in fixtures.values() if start <= datetime.fromisoformat(f["kickoff_utc"]) < end]
    selected.sort(key=lambda f: (f["kickoff_utc"], str(f["fixture_id"])))
    return {"provider": PROVIDER, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat(),
            "total_fixtures": len(selected), "fixtures": selected, "coverage": coverage}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", default=None)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/soccer_football_info"))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.hours) or args.hours <= 0:
        parser.error("--hours must be positive and finite")
    if args.max_requests <= 0:
        parser.error("--max-requests must be positive")
    try:
        start = parse_start(args.start) if args.start else datetime.now(timezone.utc)
    except ValueError:
        parser.error("--from must be a timezone-aware ISO-8601 datetime")
    try:
        report = collect(start, args.hours, args.max_requests, args.cache_dir, args.refresh)
    except (RuntimeError, ValueError, OSError, OverflowError):
        # Do not echo provider payloads, URLs, filesystem paths, or credentials.
        print("ERROR: Discovery failed; check credentials, provider response, window, and cache access", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
