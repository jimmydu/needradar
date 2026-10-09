"""Freelancer.com collector via the official API (requires approved access token).

Docs: https://developers.freelancer.com
Auth: header `freelancer-oauth-v1: {access_token}`.
Token from env FREELANCER_ACCESS_TOKEN, or file ./freelancer.api.key
(FREELANCER_API_KEY_FILE overrides). Skips gracefully when not configured.
Per API terms, data is refreshed daily (24h cache rule) — see README.
Prefect wiring: `collect(date_from, date_to)` is the task entrypoint.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API_BASE = "https://www.freelancer.com/api"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_RETRIES = 3
DESC_MAX = 5000
KEY_FILE = os.environ.get("FREELANCER_API_KEY_FILE", "./freelancer.api.key")


def _load_token():
    token = os.environ.get("FREELANCER_ACCESS_TOKEN")
    if not token and os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            token = f.read().strip()
    if not token:
        log.warning("Freelancer token not configured (set FREELANCER_ACCESS_TOKEN or "
                    "./freelancer.api.key); skipping freelancer source")
        return None
    return token


def _get(path, headers, params):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
            log.warning("rate limited, retry %d/%d", attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            log.error("Freelancer auth rejected (HTTP %d): token invalid or not approved", resp.status_code)
            return None
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            log.warning("API error: %s", str(body)[:200])
            return None
        return body.get("result") or {}
    return None


def _amount(project):
    budget = project.get("budget") or {}
    if budget.get("maximum") is not None:
        return float(budget["maximum"])
    if budget.get("average") is not None:
        return float(budget["average"])
    return None


def _signal_type(project):
    status = (project.get("status") or "").lower()
    if status in ("active", "open"):
        return "外包发布"
    if status in ("complete", "closed") and (project.get("bids") or 0) > 0:
        return "已完成外包"
    return "外包发布"


def collect(date_from, date_to, session):
    """Fetch active projects (newest first); keep those submitted >= date_from."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    token = _load_token()
    if not token:
        return stats
    headers = {"freelancer-oauth-v1": token}

    now = datetime.utcnow()
    start_ts = datetime.combine(date_from, datetime.min.time()).timestamp()

    for page in range(MAX_PAGES):
        result = _get("/projects/0.1/projects/active", headers, {
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "sort_field": "time_submitted",
            "reverse_sort": "true",
            "compact": "false",
            "full_description": "true",
        })
        stats["requests"] += 1
        if result is None:
            stats["failed"] += 1
            break
        projects = result.get("projects") or []
        if not projects:
            break

        records, seen_older = [], False
        for pr in projects:
            submitted = pr.get("time_submitted")
            if submitted and submitted < start_ts:
                seen_older = True
                continue
            owner = pr.get("owner") or {}
            records.append({
                "source": "freelancer",
                "source_id": str(pr.get("id")),
                "signal_type": _signal_type(pr),
                "title": pr.get("title"),
                "description": (pr.get("description") or pr.get("preview_description") or "")[:DESC_MAX],
                "amount": _amount(pr),
                "naics": None,
                "state": None,
                "posted_at": datetime.utcfromtimestamp(submitted) if submitted else None,
                "fetched_at": now,
                "url": pr.get("seo_url") and f"https://www.freelancer.com/projects/{pr['seo_url']}" or None,
                "rating": None,
                "raw_json": json.dumps({
                    "budget": pr.get("budget"),
                    "hourly": pr.get("type") == "hourly" or (pr.get("budget") or {}).get("project_type") == "hourly",
                    "bids": pr.get("bids"),
                    "bid_stats": pr.get("bid_stats"),
                    "owner_country": (owner.get("location") or {}).get("country"),
                    "status": pr.get("status"),
                    "jobs": pr.get("jobs"),
                    "project": pr,
                }, ensure_ascii=False),
            })
        ins, skip = upsert_signals(session, records)
        stats["inserted"] += ins
        stats["skipped"] += skip
        log.info("page=%d -> %d in window, %d inserted", page + 1, len(records), ins)
        if seen_older or len(projects) < PAGE_SIZE:
            break
        time.sleep(0.5)

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Freelancer.com projects collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Freelancer {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
