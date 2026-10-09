"""CFPB Consumer Complaint Database collector (no auth, no key).

API: https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/
Since 2026-08 CFPB no longer publishes complaint narratives; structured
fields remain, which is what we use.
Prefect wiring: `collect(date_from, date_to)` is the task entrypoint.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API_URL = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/"
SEARCH_URL = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/"
# Upstream bug (cfpb/cfpb.github.io#292, open since 2025-03): the `frm` offset
# parameter is silently ignored, so classic paging is impossible. `search_after`
# is broken too (424 from OpenSearch). Workaround: one request per day with a
# large page size; daily complaint volume (~7k) fits well under 10000.
PAGE_SIZE = 10000
MAX_RETRIES = 3


def _get(params):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(API_URL, params=params, timeout=60)
        except requests.RequestException as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
            log.warning("HTTP %d, retry %d/%d", resp.status_code, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("CFPB request failed after retries")


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def collect(date_from, date_to, session):
    """Fetch complaints received in [date_from, date_to], one request per day."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()

    day = date_from
    while day <= date_to:
        try:
            data = _get({
                "date_received_min": day.strftime("%Y-%m-%d"),
                "date_received_max": day.strftime("%Y-%m-%d"),
                "size": PAGE_SIZE,
                "sort": "created_date_desc",
            })
            stats["requests"] += 1
            hits = data.get("hits") or {}
            rows = hits.get("hits") or []
            total = (hits.get("total") or {}).get("value", 0)
            if total > len(rows):
                log.error("%s: total=%d > fetched=%d, data incomplete (API paging broken)",
                          day, total, len(rows))
                stats["failed"] += 1
            log.info("%s -> %d records (total=%d)", day, len(rows), total)

            records = []
            for h in rows:
                c = h.get("_source") or {}
                cid = c.get("complaint_id") or h.get("_id")
                if not cid:
                    continue
                product = c.get("product") or ""
                issue = c.get("issue") or ""
                records.append({
                    "source": "cfpb",
                    "source_id": str(cid),
                    "signal_type": "complaint",
                    "title": f"{product} - {issue}".strip(" -"),
                    "description": c.get("consumer_complaint_narrative") or None,
                    "amount": None,
                    "naics": None,
                    "state": c.get("state"),
                    "posted_at": _parse_dt(c.get("date_received")),
                    "fetched_at": now,
                    "url": SEARCH_URL,
                    "rating": None,
                    "raw_json": json.dumps({
                        "product": c.get("product"),
                        "sub_product": c.get("sub_product"),
                        "issue": c.get("issue"),
                        "sub_issue": c.get("sub_issue"),
                        "company": c.get("company"),
                        "company_response": c.get("company_response"),
                        "company_public_response": c.get("company_public_response"),
                        "timely": c.get("timely"),
                        "submitted_via": c.get("submitted_via"),
                        "zip_code": c.get("zip_code"),
                        "tags": c.get("tags"),
                        "complaint": c,
                    }, ensure_ascii=False),
                })
            ins, skip = upsert_signals(session, records)
            stats["inserted"] += ins
            stats["skipped"] += skip
        except Exception as e:
            log.error("%s failed: %s", day, e)
            stats["failed"] += 1
        day += timedelta(days=1)
        time.sleep(0.5)

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="CFPB complaints collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"CFPB {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
