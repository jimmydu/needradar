"""SAM.gov Get Opportunities API v2 collector.

Quota-aware: non-federal keys allow ~10 requests/day, so this collector uses
large pages (limit=1000) and stops gracefully when quota is exhausted.
Later wiring to Prefect: `collect(date_from, date_to)` is the task entrypoint.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.sam.gov/opportunities/v2/search"
PAGE_SIZE = 1000
MAX_RETRIES = 3
PTYPE_SIGNAL = {
    "o": "招标",   # Solicitation
    "k": "招标",   # Combined Synopsis/Solicitation
    "r": "意向",   # Sources Sought
    "a": "中标",   # Award Notice
}


def load_api_key():
    path = os.environ.get("SAM_GOV_API_KEY_FILE", "./sam.gov.api.key")
    with open(path) as f:
        key = f.read().strip()
    if not key:
        raise RuntimeError(f"SAM.gov API key file {path} is empty")
    return key


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class QuotaExhausted(Exception):
    pass


def _get(api_key, params):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(API_URL, params={"api_key": api_key, **params}, timeout=60)
        except requests.RequestException as e:
            last_exc = e
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            log.warning("quota exhausted (HTTP 429), remaining: %s",
                        resp.headers.get("X-Rate-Limit-Remaining", "?"))
            raise QuotaExhausted
        if resp.status_code == 403 and "quota" in resp.text.lower():
            log.warning("quota exhausted (HTTP 403): %s", resp.text[:200])
            raise QuotaExhausted
        if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
            log.warning("HTTP %d, retry %d/%d", resp.status_code, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    if last_exc:
        raise last_exc
    raise RuntimeError("SAM.gov request failed after retries")


def collect(date_from, date_to, session):
    """Fetch opportunities in [date_from, date_to] for all ptypes, upsert into DB.

    Returns dict with inserted/skipped/failed counts and requests used.
    """
    api_key = load_api_key()
    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()

    for ptype, signal_type in PTYPE_SIGNAL.items():
        offset = 0
        try:
            while True:
                data = _get(api_key, {
                    "postedFrom": date_from.strftime("%m/%d/%Y"),
                    "postedTo": date_to.strftime("%m/%d/%Y"),
                    "ptype": ptype,
                    "limit": PAGE_SIZE,
                    "offset": offset,
                })
                stats["requests"] += 1
                opps = data.get("opportunitiesData", [])
                log.info("ptype=%s offset=%d -> %d records (total=%s)",
                         ptype, offset, len(opps), data.get("totalRecords"))

                records = []
                for o in opps:
                    notice_id = o.get("noticeId")
                    if not notice_id:
                        continue
                    pop = o.get("placeOfPerformance") or {}
                    award = o.get("award") or {}
                    amount = award.get("amount")
                    try:
                        amount = float(amount) if amount not in (None, "") else None
                    except (TypeError, ValueError):
                        amount = None
                    records.append({
                        "source": "sam_gov",
                        "source_id": str(notice_id),
                        "signal_type": signal_type,
                        "title": o.get("title"),
                        "description": o.get("description"),
                        "amount": amount,
                        "naics": o.get("naicsCode"),
                        "state": (pop.get("state") or {}).get("code"),
                        "posted_at": _parse_dt(o.get("postedDate")),
                        "fetched_at": now,
                        "url": o.get("uiLink"),
                        "raw_json": __import__("json").dumps(o, ensure_ascii=False),
                    })
                from storage import upsert_signals
                ins, skip = upsert_signals(session, records)
                stats["inserted"] += ins
                stats["skipped"] += skip

                if len(opps) < PAGE_SIZE or offset + len(opps) >= data.get("totalRecords", 0):
                    break
                offset += PAGE_SIZE
        except QuotaExhausted:
            log.warning("stopping SAM.gov collection early: quota exhausted")
            break
        except Exception as e:
            log.error("ptype=%s failed: %s", ptype, e)
            stats["failed"] += 1

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="SAM.gov opportunities collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"SAM.gov {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
