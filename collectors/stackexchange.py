"""Stack Exchange OpenData collector (official API, no key, 300 req/day).

Incremental by last question date (state file). Score = demand strength proxy.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.stackexchange.com/2.3/questions"
STATE_FILE = os.environ.get("SE_STATE_FILE", "./stackexchange_state.json")
PAGE_SIZE = 100
MAX_PAGES = 3  # stay well under the 300/day quota (1 request per page)


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    state = _load_state()
    last_ts = state.get("last_creation_ts", 0)
    now = datetime.utcnow()
    start_ts = datetime.combine(date_from, datetime.min.time()).timestamp()
    floor = max(last_ts, start_ts)

    records = []
    newest = last_ts
    for page in range(1, MAX_PAGES + 1):
        try:
            resp = requests.get(API_URL, params={
                "site": "opendata", "order": "desc", "sort": "creation",
                "pagesize": PAGE_SIZE, "page": page,
                "fromdate": int(floor) if floor else None,
            }, timeout=30)
        except requests.RequestException as e:
            log.warning("request error: %s", e)
            stats["failed"] += 1
            break
        stats["requests"] += 1
        if resp.status_code != 200:
            log.warning("HTTP %d: %s", resp.status_code, resp.text[:150])
            stats["failed"] += 1
            break
        body = resp.json()
        if body.get("error_id"):
            log.warning("SE API error: %s", body.get("error_message"))
            stats["failed"] += 1
            break
        items = body.get("items", [])
        for q in items:
            created = q.get("creation_date", 0)
            newest = max(newest, created)
            if created <= floor:
                continue
            records.append({
                "source": "stackexchange",
                "source_id": str(q["question_id"]),
                "signal_type": "数据求源",
                "title": q.get("title"),
                "description": (q.get("body_markdown") or "")[:5000],
                "amount": None, "naics": None, "state": None,
                "posted_at": datetime.utcfromtimestamp(created),
                "fetched_at": now,
                "url": q.get("link"),
                "rating": q.get("score"),  # votes = demand strength proxy
                "raw_json": json.dumps({
                    "tags": q.get("tags"), "score": q.get("score"),
                    "answer_count": q.get("answer_count"),
                    "view_count": q.get("view_count"),
                    "is_answered": q.get("is_answered"),
                }, ensure_ascii=False),
            })
        if not body.get("has_more") or not items:
            break
        time.sleep(1)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    state["last_creation_ts"] = newest
    _save_state(state)
    log.info("stackexchange opendata: %d new questions, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Stack Exchange OpenData collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"StackExchange {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
