"""Kickstarter collector — lightweight scraper (no official API).

Strategy: discover/advanced pages (Technology software-ish categories,
sort=most_funded/newest) -> project page's embedded `window.current_project`
JSON. Cloudflare currently challenges plain requests (403 "Just a moment"):
the collector degrades gracefully (logs, returns failed stats, never blocks
other collectors). Zero-risk fallback not yet wired: Webrobots monthly CSV.
"""
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

import requests

from aggregator.fit import PHYSICAL_KEYWORDS

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# Technology > Apps / Software category ids (best-effort; KS changes these)
DISCOVER_URLS = [
    "https://www.kickstarter.com/discover/advanced?category_id=523&sort=newest",   # Apps
    "https://www.kickstarter.com/discover/advanced?category_id=51&sort=newest",    # Software
    "https://www.kickstarter.com/discover/advanced?category_id=523&sort=most_funded",
]
REQUEST_INTERVAL = 3.0
_PROJECT_RE = re.compile(r"window\.current_project\s*=\s*\"(.*?)\";", re.DOTALL)


def _fetch(url):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        log.warning("request error: %s", e)
        return None
    if resp.status_code == 403 or "Just a moment" in resp.text[:2000]:
        log.warning("blocked by anti-bot (HTTP %s) for %s", resp.status_code, url)
        return None
    if resp.status_code != 200:
        log.warning("HTTP %d for %s", resp.status_code, url)
        return None
    return resp.text


def _extract_projects(html):
    """Pull embedded project JSON blobs out of a discover page."""
    projects = []
    for m in _PROJECT_RE.finditer(html):
        try:
            # the blob is a JSON-encoded string (double-escaped)
            blob = json.loads(f'"{m.group(1)}"')
            projects.append(json.loads(blob))
        except (json.JSONDecodeError, ValueError):
            continue
    return projects


def _looks_hardware(p):
    text = f"{p.get('name', '')} {p.get('blurb', '')}".lower()
    return bool(set(text.split()) & PHYSICAL_KEYWORDS)


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()
    start_ts = datetime.combine(date_from, datetime.min.time()).timestamp()

    any_ok = False
    records = []
    for url in DISCOVER_URLS:
        html = _fetch(url)
        stats["requests"] += 1
        if not html:
            stats["failed"] += 1
            time.sleep(REQUEST_INTERVAL)
            continue
        any_ok = True
        for p in _extract_projects(html):
            pid = p.get("id")
            launched = p.get("launched_at") or p.get("created_at")
            if not pid or (launched and launched < start_ts):
                continue
            cat = ((p.get("category") or {}).get("name") or "")
            records.append({
                "source": "kickstarter",
                "source_id": str(pid),
                "signal_type": "众筹预售",
                "title": p.get("name"),
                "description": (p.get("blurb") or "")[:5000],
                "amount": float(p["pledged"]) if p.get("pledged") is not None else None,
                "naics": None,
                "state": (p.get("location") or {}).get("state"),
                "posted_at": datetime.utcfromtimestamp(launched) if launched else None,
                "fetched_at": now,
                "url": (p.get("urls") or {}).get("web", {}).get("project"),
                "rating": None,
                "raw_json": json.dumps({
                    "goal": p.get("goal"),
                    "pledged": p.get("pledged"),
                    "backers_count": p.get("backers_count"),
                    "category": cat,
                    "currency": p.get("currency"),
                    "hardware_hint": _looks_hardware(p),
                    "project": p,
                }, ensure_ascii=False),
            })
        time.sleep(REQUEST_INTERVAL)

    if records:
        ins, skip = upsert_signals(session, records)
        stats["inserted"] = ins
        stats["skipped"] = skip
    if not any_ok:
        log.error("kickstarter: all discover pages blocked/failed; "
                  "consider Webrobots monthly CSV fallback")
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Kickstarter collector (light scraper)")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Kickstarter {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
