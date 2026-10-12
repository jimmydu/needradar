"""Remote-work job boards collector: RemoteOK + Remotive + WeWorkRemotely RSS
+ Himalayas API + Working Nomads feed.

Job postings are EMPLOYER demand (not user pain) — signal_type='远程招聘'.
Only Worldwide/Anywhere/Global-remote jobs are kept (US-only/EU-only are
irrelevant for a China-based senior developer). RemoteOK API terms require
linking back and attribution (kept in raw_json).
"""
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

UA = "needradar/0.1 (contact: local research project; source attribution: Remote OK / Remotive / WWR / Himalayas / Working Nomads)"
REMOTEOK_URL = "https://remoteok.com/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
WWR_URL = "https://weworkremotely.com/remote-jobs.rss"
HIMALAYAS_URL = "https://himalayas.app/jobs/api"
WORKINGNOMADS_URL = "https://www.workingnomads.com/jobs/feed"
MAX_RETRIES = 3
DESC_MAX = 2000
_TAG_RE = re.compile(r"<[^>]+>")
_WORLDWIDE_OK = re.compile(r"worldwide|anywhere|global|no location|everywhere", re.I)
SKILL_LEXICON = ["python", "react", "node", "typescript", "javascript", "golang", "go",
                 "rust", "java", "aws", "devops", "ml", "machine learning", "ai",
                 "frontend", "backend", "fullstack", "full-stack", "ios", "android",
                 "data", "analytics", "product", "marketing", "design", "rails",
                 "kubernetes", "postgres", "security", "senior"]


def _skill(title, tags=None):
    if isinstance(tags, str):
        tags = [tags]
    flat = []
    for t in (tags or []):
        if isinstance(t, dict):
            t = t.get("name") or t.get("title") or t.get("slug") or ""
        flat.append(str(t).lower())
    text = (title or "").lower() + " " + " ".join(flat)
    hits = [s for s in SKILL_LEXICON if s in text]
    senior = "senior" in text or "sr." in text or "staff" in text or "lead" in text
    return (hits[0] if hits else "general"), senior


def _worldwide(location):
    """True if no restriction or explicitly worldwide; False for geo-gated."""
    if not location or not str(location).strip():
        return True
    return bool(_WORLDWIDE_OK.search(str(location)))


def _get(url, json_mode=True, params=None):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
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


def _parse_dt(s, rfc2822=False):
    if not s:
        return None
    try:
        if rfc2822:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(s).replace(tzinfo=None)
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _record(source, sid, title, desc, posted, url, raw):
    now = datetime.utcnow()
    return {"source": source, "source_id": str(sid), "signal_type": "远程招聘",
            "title": title, "description": (desc or "")[:DESC_MAX],
            "amount": None, "naics": None, "state": None,
            "posted_at": posted, "fetched_at": now, "url": url, "rating": None,
            "raw_json": json.dumps(raw, ensure_ascii=False)}


def collect(date_from, date_to, session, worldwide_only=True):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0,
             "geo_filtered": 0}
    start_dt = datetime.combine(date_from, datetime.min.time())
    records = []

    # --- RemoteOK (latest ~100, unpaginated)
    data = _get(REMOTEOK_URL)
    stats["requests"] += 1
    if data:
        for j in data:
            if not isinstance(j, dict) or "id" not in j:
                continue
            posted = _parse_dt(j.get("date")) or _parse_dt(
                datetime.utcfromtimestamp(int(j["epoch"])).isoformat() if j.get("epoch") else None)
            if posted and posted < start_dt:
                continue
            if worldwide_only and not _worldwide(j.get("location")):
                stats["geo_filtered"] += 1
                continue
            skill, senior = _skill(j.get("position"), j.get("tags"))
            records.append(_record(
                "remoteok", j.get("id") or j.get("slug"),
                f"{j.get('position')} @ {j.get('company')}",
                _TAG_RE.sub("", j.get("description") or ""), posted, j.get("url"),
                {"company": j.get("company"), "position": j.get("position"),
                 "tags": j.get("tags"), "location": j.get("location"),
                 "salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"),
                 "skill": skill, "senior": senior,
                 "attribution": "Remote OK (remoteok.com)"}))
    else:
        stats["failed"] += 1
    time.sleep(2)

    # --- Remotive (limited page; limit=500 is ignored beyond ~18 — accepted)
    data = _get(REMOTIVE_URL, params={"limit": 500})
    stats["requests"] += 1
    if data:
        for j in data.get("jobs", []):
            posted = _parse_dt(j.get("publication_date"))
            if posted and posted < start_dt:
                continue
            if worldwide_only and not _worldwide(j.get("candidate_required_location")):
                stats["geo_filtered"] += 1
                continue
            skill, senior = _skill(j.get("title"), j.get("tags"))
            records.append(_record(
                "remotive", j.get("id"),
                f"{j.get('title')} @ {j.get('company_name')}",
                _TAG_RE.sub("", j.get("description") or ""), posted, j.get("url"),
                {"company": j.get("company_name"), "category": j.get("category"),
                 "job_type": j.get("job_type"), "salary": j.get("salary"),
                 "location": j.get("candidate_required_location"),
                 "skill": skill, "senior": senior,
                 "attribution": "Remotive (remotive.com)"}))
    else:
        stats["failed"] += 1
    time.sleep(2)

    # --- WeWorkRemotely RSS
    xml_text = _get(WWR_URL, json_mode=False)
    stats["requests"] += 1
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for it in root.iter("item"):
                posted = _parse_dt(it.findtext("pubDate"), rfc2822=True)
                if posted and posted < start_dt:
                    continue
                title = it.findtext("title") or ""
                skill, senior = _skill(title)
                records.append(_record(
                    "weworkremotely", it.findtext("guid") or it.findtext("link"),
                    title, _TAG_RE.sub("", it.findtext("description") or ""),
                    posted, it.findtext("link"),
                    {"skill": skill, "senior": senior,
                     "attribution": "We Work Remotely"}))
        except ET.ParseError as e:
            log.warning("wwr parse error: %s", e)
            stats["failed"] += 1
    else:
        stats["failed"] += 1
    time.sleep(2)

    # --- Himalayas API (cursor 分页；limit 上限 20/页，2026-10-12 实测)
    cursor = None
    for _page in range(10):  # 200 jobs/天足够覆盖日增量
        params = {"limit": 20}
        if cursor:
            params["cursor"] = cursor
        data = _get(HIMALAYAS_URL, params=params)
        stats["requests"] += 1
        if not data:
            stats["failed"] += 1
            break
        for j in data.get("jobs", []):
            posted = _parse_dt(j.get("pubDate"))
            if posted and posted < start_dt:
                continue
            locs = j.get("locationRestrictions") or j.get("locations") or []
            loc_text = " ".join(str(x) for x in locs) if locs else ""
            # locationRestrictions 为空 = worldwide；含具体地区 = 地理受限
            if worldwide_only and loc_text and not _WORLDWIDE_OK.search(loc_text):
                stats["geo_filtered"] += 1
                continue
            title = j.get("title") or ""
            company = j.get("companyName") or j.get("companySlug")
            skill, _ = _skill(title, j.get("tags") or j.get("categories"))
            sen = j.get("seniority") or ""
            if isinstance(sen, list):
                sen = " ".join(str(x) for x in sen)
            senior = str(sen).lower() in ("senior", "lead", "staff", "principal")
            records.append(_record(
                "himalayas", j.get("guid") or j.get("id") or title,
                f"{title} @ {company or ''}",
                _TAG_RE.sub("", j.get("description") or j.get("excerpt") or ""),
                posted, j.get("applicationLink") or j.get("url"),
                {"company": company, "tags": j.get("categories") or j.get("parentCategories"),
                 "locations": loc_text, "salary_min": j.get("minSalary"),
                 "salary_max": j.get("maxSalary"), "currency": j.get("currency"),
                 "salary_period": j.get("salaryPeriod"),
                 "skill": skill, "senior": senior,
                 "attribution": "Himalayas (himalayas.app)"}))
        cursor = data.get("nextCursor")
        if not cursor:
            break
        time.sleep(2)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("remote jobs: %d in window, %d inserted, %d geo-filtered",
             len(records), ins, stats["geo_filtered"])
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Remote-work jobs collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--all-geo", action="store_true", help="不做 Worldwide 过滤")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session(), worldwide_only=not args.all_geo)
    print(f"Remote jobs {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
