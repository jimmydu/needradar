"""USAspending.gov collector (no key, no quota).

Later wiring to Prefect: `collect(date_from, date_to)` is the task entrypoint.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
CONTRACT_TYPES = ["A", "B", "C", "D"]
FIELDS = [
    "Award ID", "Recipient Name", "Start Date", "Award Amount",
    "Awarding Agency", "Awarding Sub Agency", "NAICS",
    "recipient_location_state_code", "Place of Performance State Code",
    "Description",
]
PAGE_SIZE = 100
MAX_RETRIES = 3


def _post(body):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(API_URL, json=body, timeout=60)
        except requests.RequestException as e:
            last_exc = e
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
            log.warning("HTTP %d, retry %d/%d", resp.status_code, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    if last_exc:
        raise last_exc
    raise RuntimeError("USAspending request failed after retries")


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _naics(row):
    n = row.get("NAICS")
    if isinstance(n, dict):
        return n.get("code")
    return str(n) if n else None


def collect(date_from, date_to, session):
    """Fetch contract awards in [date_from, date_to], upsert into DB."""
    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()
    page = 1

    try:
        while True:
            body = {
                "filters": {
                    "time_period": [{
                        "start_date": date_from.strftime("%Y-%m-%d"),
                        "end_date": date_to.strftime("%Y-%m-%d"),
                    }],
                    "award_type_codes": CONTRACT_TYPES,
                },
                "fields": FIELDS,
                "limit": PAGE_SIZE,
                "page": page,
                "sort": "Award Amount",
                "order": "desc",
            }
            data = _post(body)
            stats["requests"] += 1
            results = data.get("results", [])
            log.info("page=%d -> %d records", page, len(results))

            records = []
            for r in results:
                award_id = r.get("Award ID") or r.get("generated_internal_id")
                if not award_id:
                    continue
                records.append({
                    "source": "usaspending",
                    "source_id": str(r.get("generated_internal_id") or award_id),
                    "signal_type": "支出",
                    "title": r.get("Description") or f"Contract award {award_id}",
                    "description": r.get("Description"),
                    "amount": float(r["Award Amount"]) if r.get("Award Amount") is not None else None,
                    "naics": _naics(r),
                    "state": r.get("Place of Performance State Code") or r.get("recipient_location_state_code"),
                    "posted_at": _parse_dt(r.get("Start Date")),
                    "fetched_at": now,
                    "url": f"https://www.usaspending.gov/award/{r.get('generated_internal_id')}" if r.get("generated_internal_id") else None,
                    "raw_json": json.dumps(r, ensure_ascii=False),
                })
            from storage import upsert_signals
            ins, skip = upsert_signals(session, records)
            stats["inserted"] += ins
            stats["skipped"] += skip

            if not data.get("page_metadata", {}).get("hasNext") or not results:
                break
            page += 1
    except Exception as e:
        log.error("USAspending collection failed: %s", e)
        stats["failed"] += 1

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="USAspending awards collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--max-pages", type=int, default=0, help="0 = no limit")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"USAspending {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
