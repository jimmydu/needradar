"""Reddit RSS collector (no auth) — fallback while the official Data API
requires manual approval (Responsible Builder Policy, since 2025-11).

Limitations vs the API version: no score/comment counts, only ~25 latest
posts per sub. The OAuth collector (collectors/reddit.py) stays for later.
Prefect wiring: `collect(date_from, date_to)` is the task entrypoint.
"""
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

RSS_URL = "https://www.reddit.com/r/{sub}/.rss"
USER_AGENT = "needradar/0.1 (contact: local research project)"
SUBS_FILE = os.environ.get("REDDIT_SUBS_FILE", "./reddit_subs.txt")
REQUEST_INTERVAL = 5.0
ATOM = "{http://www.w3.org/2005/Atom}"
DESC_MAX = 5000

_TAG_RE = re.compile(r"<[^>]+>")


def load_subs(path=SUBS_FILE):
    subs = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                subs.append(line)
    return subs


def _fetch(url):
    """Fetch RSS XML; on 429/503 back off and retry, then give up (returns None)."""
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            log.warning("request error: %s", e)
            return None
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (429, 503) and attempt < 2:
            wait = int(resp.headers.get("Retry-After", 15)) + attempt * 15
            log.warning("HTTP %d, backing off %ds", resp.status_code, wait)
            time.sleep(wait)
            continue
        log.warning("HTTP %d for %s", resp.status_code, url)
        return None
    return None


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _parse_feed(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("feed parse error: %s", e)
        return []
    entries = []
    for e in root.findall(f"{ATOM}entry"):
        author = e.find(f"{ATOM}author/{ATOM}name")
        link = None
        for l in e.findall(f"{ATOM}link"):
            if l.get("rel") in (None, "alternate"):
                link = l.get("href")
                break
        flair = e.find(f"{ATOM}category")
        entries.append({
            "id": (e.findtext(f"{ATOM}id") or "").strip(),
            "title": e.findtext(f"{ATOM}title"),
            "content": e.findtext(f"{ATOM}content"),
            "updated": e.findtext(f"{ATOM}updated"),
            "author": author.text if author is not None else None,
            "link": link,
            "flair": flair.get("label") if flair is not None else None,
        })
    return entries


def _strip_html(s):
    return _TAG_RE.sub("", s or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#32;", " ").strip()


def collect(date_from, date_to, session):
    """Fetch RSS for each monitored sub; keep posts >= date_from."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0, "subs": {}}
    now = datetime.utcnow()
    start_dt = datetime.combine(date_from, datetime.min.time())

    for sub in load_subs():
        xml_text = _fetch(RSS_URL.format(sub=sub))
        stats["requests"] += 1
        if xml_text is None:
            stats["failed"] += 1
            stats["subs"][sub] = "failed"
            time.sleep(REQUEST_INTERVAL)
            continue

        records = []
        for e in _parse_feed(xml_text):
            posted = _parse_dt(e["updated"])
            if not e["id"] or (posted and posted < start_dt):
                continue
            records.append({
                "source": "reddit_rss",
                "source_id": e["id"],
                "signal_type": "post",
                "title": e["title"],
                "description": _strip_html(e["content"])[:DESC_MAX],
                "amount": None,
                "naics": None,
                "state": None,
                "posted_at": posted,
                "fetched_at": now,
                "url": e["link"],
                "rating": None,
                "raw_json": json.dumps({
                    "subreddit": sub,
                    "author": e["author"],
                    "link": e["link"],
                    "flair": e["flair"],
                }, ensure_ascii=False),
            })
        ins, skip = upsert_signals(session, records)
        stats["inserted"] += ins
        stats["skipped"] += skip
        stats["subs"][sub] = f"{len(records)} in window, {ins} inserted"
        log.info("r/%s -> %s", sub, stats["subs"][sub])
        time.sleep(REQUEST_INTERVAL)

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Reddit RSS collector (no auth)")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Reddit RSS {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
