"""Google News RSS keyword-search collector (no auth).

Same shape as collectors/reddit_rss.py. Google News links are redirect
links — stored as-is; the publisher name goes into raw_json.
Query list: ./news_queries.txt (NEWS_QUERIES_FILE overrides).
Signals are a topic radar, not a daily demand list — trigger-event typing
happens in the extraction layer (extractor/rules.py).
"""
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search"
USER_AGENT = "needradar/0.1 (contact: local research project)"
QUERIES_FILE = os.environ.get("NEWS_QUERIES_FILE", "./news_queries.txt")
REQUEST_INTERVAL = 3.0
DESC_MAX = 5000
_TAG_RE = __import__("re").compile(r"<[^>]+>")


def load_queries(path=QUERIES_FILE):
    queries = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                queries.append(line)
    return queries


def _fetch(query):
    headers = {"User-Agent": USER_AGENT}
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    for attempt in range(2):
        try:
            resp = requests.get(RSS_URL, params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            log.warning("request error: %s", e)
            return None
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (429, 503) and attempt == 0:
            wait = int(resp.headers.get("Retry-After", 15))
            log.warning("HTTP %d, backing off %ds", resp.status_code, wait)
            time.sleep(wait)
            continue
        log.warning("HTTP %d for query %r", resp.status_code, query)
        return None
    return None


def _parse_dt(s):
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _parse_feed(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("feed parse error: %s", e)
        return []
    items = []
    for it in root.iter("item"):
        src = it.find("source")
        items.append({
            "title": it.findtext("title"),
            "link": it.findtext("link"),
            "published": it.findtext("pubDate"),
            "description": _TAG_RE.sub("", it.findtext("description") or "").strip(),
            "publisher": src.text if src is not None else None,
            "guid": it.findtext("guid"),
        })
    return items


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0, "queries": {}}
    now = datetime.utcnow()
    start_dt = datetime.combine(date_from, datetime.min.time())

    for q in load_queries():
        xml_text = _fetch(q)
        stats["requests"] += 1
        if xml_text is None:
            stats["failed"] += 1
            stats["queries"][q] = "failed"
            time.sleep(REQUEST_INTERVAL)
            continue

        records = []
        for it in _parse_feed(xml_text):
            posted = _parse_dt(it["published"])
            if posted and posted < start_dt:
                continue
            sid = it["guid"] or it["link"]
            if not sid:
                continue
            records.append({
                "source": "news",
                "source_id": sid,
                "signal_type": "news",
                "title": it["title"],
                "description": it["description"][:DESC_MAX],
                "amount": None, "naics": None, "state": None,
                "posted_at": posted,
                "fetched_at": now,
                "url": it["link"],
                "rating": None,
                "raw_json": __import__("json").dumps({
                    "query": q,
                    "publisher": it["publisher"],
                }, ensure_ascii=False),
            })
        ins, skip = upsert_signals(session, records)
        stats["inserted"] += ins
        stats["skipped"] += skip
        stats["queries"][q] = f"{len(records)} in window, {ins} inserted"
        log.info("news %r -> %s", q, stats["queries"][q])
        time.sleep(REQUEST_INTERVAL)

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Google News RSS collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"News {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
