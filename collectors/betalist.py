"""BetaList collector (new indie-product launches, server-rendered HTML).

Probed alternatives: acquire.com (JS-rendered listings + no public API found),
Indie Hackers (revenue pages JS-rendered), Product Hunt (API needs token),
Microlaunch (JS-rendered). BetaList is the accessible one.
"""
import logging
import re
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

URL = "https://betalist.com/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MAX_RETRIES = 3

_ROW_RE = re.compile(
    r'href="(/startups/[a-z0-9\-]+)"></a>.*?'
    r'<span class="[^"]*font-medium[^"]*">(.*?)</span>\s*'
    r'<span class="[^"]*text-gray-500[^"]*">(.*?)</span>',
    re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _fetch(url):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 403:
                log.warning("blocked (403) for %s", url)
                return None
        except requests.RequestException as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
        time.sleep(2 ** attempt)
    return None


def _clean(s):
    return re.sub(r"\s+", " ", _TAG_RE.sub("", s or "")).strip()


def parse_startups(html):
    startups = []
    seen = set()
    for m in _ROW_RE.finditer(html):
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        startups.append({
            "slug": slug,
            "name": _clean(m.group(2)),
            "tagline": _clean(m.group(3))[:300],
        })
    return startups


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    html = _fetch(URL)
    stats["requests"] += 1
    if not html:
        stats["failed"] = 1
        return stats

    now = datetime.utcnow()
    records = []
    for st in parse_startups(html):
        records.append({
            "source": "betalist",
            "source_id": st["slug"],
            "signal_type": "新产品",
            "title": st["name"],
            "description": st["tagline"][:5000],
            "amount": None, "naics": None, "state": None,
            "posted_at": now,  # launch-listing page has no per-item date
            "fetched_at": now,
            "url": f"https://betalist.com{st['slug']}",
            "rating": None,
            "raw_json": __import__("json").dumps(
                {"slug": st["slug"], "name": st["name"], "tagline": st["tagline"]},
                ensure_ascii=False),
        })
    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("betalist: %d startups, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="BetaList collector")
    args = p.parse_args()
    from storage import get_session
    stats = collect(None, None, get_session())
    print(f"BetaList: {stats}")


if __name__ == "__main__":
    main()
