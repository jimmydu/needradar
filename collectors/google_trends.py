"""Google Trends collector via pytrends (unofficial, rate-limited).

Daily cadence. Roots x verticals from trends_queries.txt; each combo pulls
related rising queries ("breakout"/"+X%") as demand-direction signals.
One signal per rising query per day (deduped by source+id). Heavy caching:
state file stores last run date; same-day rerun exits instantly.
"""
import json
import logging
import os
import time
from datetime import date, datetime

log = logging.getLogger(__name__)

QUERIES_FILE = os.environ.get("TRENDS_QUERIES_FILE", "./trends_queries.txt")
STATE_FILE = os.environ.get("TRENDS_STATE_FILE", "./trends_state.json")
MAX_COMBOS = 30            # combos per run, keeps the rate limiter calm
INTERVAL = 6.0


def _load_queries(path=QUERIES_FILE):
    lines = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                lines.append(line)
    # file layout: roots first block, verticals second (same list syntax);
    # collector treats first 5 as roots and the rest as verticals
    return lines


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    today = date.today().isoformat()
    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    if state.get("run_date") == today:
        log.info("google_trends: already ran today, skip")
        return stats

    try:
        from pytrends.request import TrendReq
    except ImportError:
        log.error("pytrends not installed")
        stats["failed"] = 1
        return stats

    lines = _load_queries()
    roots = lines[:5]
    verticals = lines[5:]
    # single-term queries only: two-word combos are too narrow for Trends'
    # related-queries endpoint (2026-10-11 实测 30 组合 0 召回)
    combos = (roots + verticals)[:MAX_COMBOS]
    pt = TrendReq(hl="en-US", tz=360, timeout=(10, 30))
    now = datetime.utcnow()
    records = []

    for combo in combos:
        try:
            pt.build_payload([combo], timeframe="today 3-m", geo="US")
            stats["requests"] += 1
            rising = pt.related_queries().get(combo, {}).get("rising")
            if rising is None or rising.empty:
                time.sleep(INTERVAL)
                continue
            for _, row in rising.head(5).iterrows():
                q = row.get("query")
                val = str(row.get("value"))
                records.append({
                    "source": "google_trends",
                    "source_id": f"{today}_{combo}_{q}",
                    "signal_type": "趋势上升",
                    "title": f"[{combo}] 关联上升查询: {q}",
                    "description": f"Google Trends 上升查询（近 3 月，US）：词根组合「{combo}」下「{q}」热度 {val}",
                    "amount": None, "naics": None, "state": None,
                    "posted_at": now, "fetched_at": now,
                    "url": f"https://trends.google.com/trends/explore?q={combo.replace(' ', '+')}&geo=US",
                    "rating": None,
                    "raw_json": json.dumps({"combo": combo, "query": q,
                                            "value": val}, ensure_ascii=False),
                })
        except Exception as e:
            log.warning("combo %r failed: %s", combo, e)
            stats["failed"] += 1
        time.sleep(INTERVAL)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    state["run_date"] = today
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    log.info("google_trends: %d combos, %d rising queries inserted", len(combos), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Google Trends collector")
    args = p.parse_args()
    from storage import get_session
    stats = collect(None, None, get_session())
    print(f"Google Trends: {stats}")


if __name__ == "__main__":
    main()
