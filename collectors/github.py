"""GitHub collector, two channels, tokenless mode (search API ~10 req/min).

Channel 1 — rising repos (success samples, same semantics as appstore_indie):
recently-created repos with fast star growth.
Channel 2 — high-upvote pain issues on those repos + seed popular repos
(pain signals through the normal prefilter/LLM/cluster path).

Set GITHUB_TOKEN (read-only PAT, github.com/settings/tokens) to raise rate
limits. Never commit the token. State file enables resume across rate-limit
breaks.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"
STATE_FILE = os.environ.get("GITHUB_STATE_FILE", "./github_state.json")
INTERVAL_NO_TOKEN = 6.0   # unauthenticated search limit: 10/min
INTERVAL_TOKEN = 2.0
MAX_RETRIES = 3
RISING_MIN_STARS = 500
RISING_MAX_AGE_DAYS = 90
ISSUE_MIN_REACTIONS = 5
SEED_POPULAR_REPOS = ["microsoft/vscode", "ollama/ollama", "n8n-io/n8n",
                      "langchain-ai/langchain", "immich-app/immich",
                      "supabase/supabase", "actualbudget/actual",
                      "coollabsio/coolify", "firecrawl/firecrawl",
                      "browser-use/browser-use"]
PAIN_WORDS = ("please add", "support", "doesn't work", "doesnt work",
              "frustrating", "feature request", "enhancement", "broken",
              "missing", "would be great", "wish")


def _headers():
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "needradar/0.1 (research)"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _search(path, params, state):
    """One search call with backoff + state persistence for resume."""
    interval = INTERVAL_TOKEN if os.environ.get("GITHUB_TOKEN") else INTERVAL_NO_TOKEN
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(f"{API}{path}", headers=_headers(),
                                params={k: v for k, v in params.items() if v is not None},
                                timeout=30)
        except requests.RequestException as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(interval * (attempt + 1))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (403, 429):
            wait = int(resp.headers.get("Retry-After", 60))
            log.warning("rate limited, backing off %ds (state saved)", wait)
            _save_state(state)
            time.sleep(min(wait, 120))
            continue
        log.warning("HTTP %d: %s", resp.status_code, resp.text[:150])
        return None
    return None


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _rising_repos(stats, state):
    since = (date.today() - timedelta(days=RISING_MAX_AGE_DAYS)).isoformat()
    data = _search("/search/repositories", {
        "q": f"created:>{since} stars:>{RISING_MIN_STARS}",
        "sort": "stars", "order": "desc", "per_page": 50,
    }, state)
    stats["requests"] += 1
    time.sleep(INTERVAL_NO_TOKEN if not os.environ.get("GITHUB_TOKEN") else INTERVAL_TOKEN)
    return (data or {}).get("items", [])


def _pain_issues(repo_full, stats, state):
    # sort by thumbs-up and filter pain words client-side: label vocabularies
    # differ per repo and break the search query (2026-10-11: label:enhancement
    # returned total_count=0 for repos that do have such issues)
    data = _search("/search/issues", {
        "q": f"repo:{repo_full} is:issue is:open",
        "sort": "reactions-+1", "order": "desc", "per_page": 10,
    }, state)
    stats["requests"] += 1
    time.sleep(INTERVAL_NO_TOKEN if not os.environ.get("GITHUB_TOKEN") else INTERVAL_TOKEN)
    items = (data or {}).get("items", [])
    out = []
    for it in items:
        text = f"{it.get('title', '')} {it.get('body') or ''}".lower()
        labels = {l.get("name", "").lower() for l in it.get("labels", [])}
        if any(w in text for w in PAIN_WORDS) or labels & {"enhancement", "feature request", "feature-request"}:
            out.append(it)
    return out


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    state = _load_state()
    now = datetime.utcnow()
    records = []

    # channel 1: rising repos
    repos = _rising_repos(stats, state)
    rising = []
    for r in repos:
        rid = str(r["id"])
        rising.append(r["full_name"])
        records.append({
            "source": "github_rising",
            "source_id": rid,
            "signal_type": "成功样本",
            "title": f"{r['full_name']} — GitHub 飙升仓库",
            "description": (r.get("description") or "")[:5000],
            "amount": None, "naics": None, "state": None,
            "posted_at": _parse_dt(r.get("created_at")),
            "fetched_at": now,
            "url": r.get("html_url"),
            "rating": None,
            "raw_json": json.dumps({
                "full_name": r["full_name"], "stars": r.get("stargazers_count"),
                "language": r.get("language"), "created_at": r.get("created_at"),
                "pushed_at": r.get("pushed_at"), "topics": r.get("topics"),
                "forks": r.get("forks_count"), "open_issues": r.get("open_issues_count"),
            }, ensure_ascii=False),
        })
    log.info("github_rising: %d repos (created<90d, stars>%d)", len(rising),
             RISING_MIN_STARS)

    # channel 2: pain issues on rising + seed popular repos
    repo_list = list(dict.fromkeys(rising + state.get("pending_repos", [])
                                   + SEED_POPULAR_REPOS))[:12]
    done = state.get("done_issue_repos", [])
    for repo in repo_list:
        if repo in done:
            continue
        issues = _pain_issues(repo, stats, state)
        for it in issues:
            reactions = (it.get("reactions") or {}).get("+1", 0)
            if reactions < ISSUE_MIN_REACTIONS:
                continue
            labels = [l.get("name") for l in it.get("labels", [])]
            records.append({
                "source": "github_issues",
                "source_id": str(it["id"]),
                "signal_type": "post",
                "title": it.get("title"),
                "description": (it.get("body") or "")[:5000],
                "amount": None, "naics": None, "state": None,
                "posted_at": _parse_dt(it.get("created_at")),
                "fetched_at": now,
                "url": it.get("html_url"),
                "rating": reactions,  # thumbs-up = demand strength
                "raw_json": json.dumps({
                    "repo": repo, "reactions_plus1": reactions,
                    "labels": labels, "comments": it.get("comments"),
                }, ensure_ascii=False),
            })
        done.append(repo)
        state["done_issue_repos"] = done
        _save_state(state)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    _save_state(state)
    log.info("github: %d records, %d inserted", len(records), ins)
    return stats


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError, TypeError):
        return None


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="GitHub collector (rising repos + pain issues)")
    args = p.parse_args()
    from storage import get_session
    stats = collect(None, None, get_session())
    print(f"GitHub: {stats}")


if __name__ == "__main__":
    main()
