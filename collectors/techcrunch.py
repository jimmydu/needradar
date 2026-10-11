"""TechCrunch tag feeds collector (free, server-rendered RSS).

Feeds: startups + acquisitions + ma. M&A-ish items are demand-direction
radar signals; the 3-field extraction (acquirer/acquiree/domain) happens in
the extraction layer for trigger_type='并购' news signals.
"""
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

log = logging.getLogger(__name__)

FEEDS = [
    "https://techcrunch.com/category/startups/feed/",
    "https://techcrunch.com/tag/acquisitions/feed/",
    "https://techcrunch.com/tag/ma/feed/",
]
USER_AGENT = "needradar/0.1 (contact: local research project)"
_TAG_RE = __import__("re").compile(r"<[^>]+>")


def _fetch(url):
    for attempt in range(2):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code == 200:
                return resp.text
            log.warning("HTTP %d for %s", resp.status_code, url)
            return None
        except requests.RequestException as e:
            log.warning("request error: %s", e)
            time.sleep(5)
    return None


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()
    records = []
    for url in FEEDS:
        xml_text = _fetch(url)
        stats["requests"] += 1
        if not xml_text:
            stats["failed"] += 1
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning("parse error %s: %s", url, e)
            stats["failed"] += 1
            continue
        for it in root.iter("item"):
            link = it.findtext("link")
            if not link:
                continue
            pub = it.findtext("pubDate")
            posted = None
            if pub:
                from email.utils import parsedate_to_datetime
                try:
                    posted = parsedate_to_datetime(pub).replace(tzinfo=None)
                except (TypeError, ValueError):
                    pass
            records.append({
                "source": "techcrunch",
                "source_id": it.findtext("guid") or link,
                "signal_type": "news",
                "title": it.findtext("title"),
                "description": _TAG_RE.sub("", it.findtext("description") or "")[:3000],
                "amount": None, "naics": None, "state": None,
                "posted_at": posted, "fetched_at": now,
                "url": link, "rating": None,
                "raw_json": json.dumps({"feed": url,
                                        "publisher": "TechCrunch"}, ensure_ascii=False),
            })
        time.sleep(2)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("techcrunch: %d items, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = argparse.ArgumentParser().parse_args()
    from storage import get_session
    print(f"TechCrunch: {collect(None, None, get_session())}")


if __name__ == "__main__":
    main()
