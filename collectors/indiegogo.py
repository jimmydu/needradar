"""Indiegogo collector via the official public API (no auth, Gamefound platform).

One GET returns all active crowdfunding projects (~200). No category field:
software/hardware split is done by text rules (aggregator/fit.py keyword lists).
Prefect wiring: `collect(date_from, date_to)` is the task entrypoint.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta

import requests

from aggregator.fit import PHYSICAL_KEYWORDS, SOFTWARE_KEYWORDS

log = logging.getLogger(__name__)

API_URL = "https://www.indiegogo.com/api/public/projects/getActiveCrowdfundingProjects"
DETAIL_URL = "https://www.indiegogo.com/api/public/projects/getCrowdfundingProject"
MAX_RETRIES = 3


def _get(url, params=None):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                log.warning("rate limited, retry %d/%d", attempt + 1, MAX_RETRIES)
                time.sleep(2 ** attempt * 5)
                continue
            resp.raise_for_status()
        except (requests.RequestException, ValueError) as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
    return None


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _looks_hardware(name, desc):
    text = f"{name} {desc}".lower()
    words = set(text.replace("/", " ").split())
    return bool(words & PHYSICAL_KEYWORDS)


def collect(date_from, date_to, session):
    """Fetch active crowdfunding projects; keep those started >= date_from."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    data = _get(API_URL)
    stats["requests"] += 1
    if not data:
        stats["failed"] = 1
        return stats

    now = datetime.utcnow()
    start_dt = datetime.combine(date_from, datetime.min.time())
    records = []
    hw_skipped = 0
    for p in data:
        name = p.get("projectName") or ""
        desc = p.get("shortDescription") or ""
        if _looks_hardware(name, desc) and not any(
                k in f"{name} {desc}".lower() for k in ("app", "software", "platform")):
            hw_skipped += 1
            continue
        started = _parse_dt(p.get("campaignStartDate"))
        # keep everything newly started in window; also keep recently-ended big
        # campaigns out of scope for MVP (active list only)
        if started and started < start_dt:
            continue
        slug = p.get("projectUrlName") or p.get("projectHomeUrl")
        if not slug:
            continue
        records.append({
            "source": "indiegogo",
            "source_id": str(slug),
            "signal_type": "众筹预售",
            "title": name,
            "description": desc[:5000],
            "amount": float(p["fundsGathered"]) if p.get("fundsGathered") is not None else None,
            "naics": None,
            "state": None,
            "posted_at": started,
            "fetched_at": now,
            "url": p.get("projectHomeUrl"),
            "rating": None,
            "raw_json": json.dumps({
                "campaignGoal": p.get("campaignGoal"),
                "fundsGathered": p.get("fundsGathered"),
                "backerCount": p.get("backerCount"),
                "commentCount": p.get("commentCount"),
                "currency": p.get("currencyShortName"),
                "campaignStartDate": p.get("campaignStartDate"),
                "campaignEndDate": p.get("campaignEndDate"),
                "projectType": p.get("projectType"),
                "project": p,
            }, ensure_ascii=False),
        })
    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("indiegogo: %d active, %d in window, %d hardware-filtered",
             len(data), len(records), hw_skipped)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Indiegogo collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Indiegogo {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
