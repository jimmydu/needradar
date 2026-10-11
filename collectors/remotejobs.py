"""Remote-work job boards collector: RemoteOK API + Remotive API + WWR RSS.

Job postings are EMPLOYER demand (not user pain) — signal_type='远程招聘',
clustered separately from pain topics by source. RemoteOK API terms require
linking back and mentioning Remote OK as the source (kept in raw_json).
"""
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

UA = "needradar/0.1 (contact: local research project; source attribution: Remote OK / Remotive / WWR)"
REMOTEOK_URL = "https://remoteok.com/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
WWR_URL = "https://weworkremotely.com/remote-jobs.rss"
MAX_RETRIES = 3
DESC_MAX = 2000
_TAG_RE = re.compile(r"<[^>]+>")


def _get(url, json_mode=True):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if resp.status_code == 200:
                return resp.json() if json_mode else resp.text
            if resp.status_code == 429:
                time.sleep(2 ** attempt * 5)
                continue
            log.warning("HTTP %d for %s", resp.status_code, url)
            return None
        except (requests.RequestException, ValueError) as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
    return None


def _parse_dt(s, fmt=None):
    if not s:
        return None
    try:
        if fmt == "rfc2822":
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(s).replace(tzinfo=None)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _record(source, sid, title, desc, posted, url, raw):
    now = datetime.utcnow()
    return {
        "source": source, "source_id": str(sid), "signal_type": "远程招聘",
        "title": title, "description": (desc or "")[:DESC_MAX],
        "amount": None, "naics": None, "state": None,
        "posted_at": posted, "fetched_at": now, "url": url, "rating": None,
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    start_dt = datetime.combine(date_from, datetime.min.time())
    records = []

    # RemoteOK (JSON array; first element is metadata/legal)
    data = _get(REMOTEOK_URL)
    stats["requests"] += 1
    if data:
        for j in data:
            if not isinstance(j, dict) or "id" not in j:
                continue
            posted = _parse_dt(j.get("date") or j.get("epoch") and
                               datetime.utcfromtimestamp(int(j["epoch"])).isoformat())
            if posted and posted < start_dt:
                continue
            records.append(_record(
                "remoteok", j.get("id") or j.get("slug"),
                f"{j.get('position')} @ {j.get('company')}",
                _TAG_RE.sub("", j.get("description") or "")[:DESC_MAX],
                posted, j.get("url"),
                {"company": j.get("company"), "position": j.get("position"),
                 "tags": j.get("tags"), "location": j.get("location"),
                 "salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"),
                 "attribution": "Remote OK (remoteok.com)"}))
    else:
        stats["failed"] += 1
    time.sleep(2)

    # Remotive (JSON {"jobs": [...]})
    data = _get(REMOTIVE_URL + "?limit=100")
    stats["requests"] += 1
    if data:
        for j in data.get("jobs", []):
            posted = _parse_dt(j.get("publication_date"))
            if posted and posted < start_dt:
                continue
            records.append(_record(
                "remotive", j.get("id"),
                f"{j.get('title')} @ {j.get('company_name')}",
                _TAG_RE.sub("", j.get("description") or "")[:DESC_MAX],
                posted, j.get("url"),
                {"company": j.get("company_name"), "category": j.get("category"),
                 "job_type": j.get("job_type"), "salary": j.get("salary"),
                 "candidate_required_location": j.get("candidate_required_location"),
                 "attribution": "Remotive (remotive.com)"}))
    else:
        stats["failed"] += 1
    time.sleep(2)

    # WeWorkRemotely (RSS)
    xml_text = _get(WWR_URL, json_mode=False)
    stats["requests"] += 1
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for it in root.iter("item"):
                posted = _parse_dt(it.findtext("pubDate"), fmt="rfc2822")
                if posted and posted < start_dt:
                    continue
                link = it.findtext("link")
                records.append(_record(
                    "weworkremotely", it.findtext("guid") or link,
                    it.findtext("title"),
                    _TAG_RE.sub("", it.findtext("description") or "")[:DESC_MAX],
                    posted, link,
                    {"attribution": "We Work Remotely"}))
        except ET.ParseError as e:
            log.warning("wwr parse error: %s", e)
            stats["failed"] += 1
    else:
        stats["failed"] += 1

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("remote jobs: %d in window, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Remote-work jobs collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Remote jobs {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
