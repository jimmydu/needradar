"""Shopify App Store collector (server-rendered, no auth).

Category pages expose app cards via data-app-card-* attributes; each app page
has JSON-LD (name/description/brand/aggregateRating), a /partners/<slug>
link, and pricing tiers. Shopify merchants pay readily for tools, so top apps
are first-class 成功样本 signals (P4 in extractor/rules.py).
"""
import json
import logging
import re
import time
import xml.etree.ElementTree as ET  # noqa: F401 (kept for symmetry, unused)
from datetime import date, datetime, timedelta

import requests

from storage import RawSignal

log = logging.getLogger(__name__)

BASE = "https://apps.shopify.com"
CATEGORIES_INDEX = f"{BASE}/categories"
# discovered from the index page 2026-10-11; keep the six legacy aliases too
CATEGORIES = [
    "finding-products", "finding-products-sourcing-options-dropshipping",
    "marketing-and-conversion", "marketing-and-conversion-marketing-email-marketing",
    "marketing-and-conversion-social-trust-product-reviews",
    "marketing-and-conversion-upsell-and-bundles",
    "orders-and-shipping", "orders-and-shipping-shipping-solutions-shipping",
    "sales-channels", "sales-channels-selling-online-marketplaces",
    "selling-products", "store-design",
    "store-design-internationalization-currency-and-translation",
    "store-design-site-optimization-seo", "store-management",
    "marketing", "sales", "orders-and-shipping", "inventory-management",
    "store-design", "customer-support",
]
PAGES_PER_CATEGORY = 2
CARD_LIMIT = 40          # per page
DETAIL_INTERVAL = 0.4
DETAIL_REFRESH_DAYS = 7
MAX_RETRIES = 3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_CARD_RE = re.compile(
    r'data-app-card-handle-value="([^"]+)"\s*\n?\s*'
    r'data-app-card-name-value="([^"]+)"')
_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
_PARTNER_RE = re.compile(r'href="https://apps\.shopify\.com/partners/([a-z0-9-]+)"')
_PRICE_RE = re.compile(r"(Free plan|Free trial|\$[\d.]+/month)")


def _get(url):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (403, 404):
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
        except requests.RequestException as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
        time.sleep(2 ** attempt)
    return None


def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _app_details(handle):
    html = _get(f"{BASE}/{handle}")
    if not html:
        return None
    out = {}
    m = _LD_RE.search(html)
    if m:
        try:
            ld = json.loads(m.group(1))
            out["name"] = ld.get("name")
            out["description"] = ld.get("description")
            out["brand"] = ld.get("brand")
            agg = ld.get("aggregateRating") or {}
            out["rating"] = agg.get("ratingValue")
            out["rating_count"] = agg.get("ratingCount")
        except ValueError:
            pass
    p = _PARTNER_RE.search(html)
    out["partner"] = p.group(1) if p else None
    prices = sorted(set(_PRICE_RE.findall(html)))
    out["pricing"] = prices[:4]
    return out


def _partner_app_count(slug):
    html = _get(f"{BASE}/partners/{slug}")
    if not html:
        return None
    return len(set(_CARD_RE.findall(html))) or None


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()

    cards = {}  # handle -> (name, category)
    for cat in CATEGORIES:
        for page in range(1, PAGES_PER_CATEGORY + 1):
            url = f"{BASE}/categories/{cat}" + (f"?page={page}" if page > 1 else "")
            html = _get(url)
            stats["requests"] += 1
            if not html:
                stats["failed"] += 1
                continue
            for handle, name in _CARD_RE.findall(html)[:CARD_LIMIT]:
                cards.setdefault(handle, (name, cat))
            time.sleep(1)
    log.info("shopify: %d unique apps across %d categories", len(cards), len(CATEGORIES))

    # skip detail fetch for apps refreshed recently (their raw_json has it all)
    fresh = {r.source_id for r in session.query(RawSignal).filter_by(source="shopify_apps")
             if r.fetched_at and (now - r.fetched_at.replace(tzinfo=None)).days < DETAIL_REFRESH_DAYS}

    partner_counts = {}
    records = []
    for handle, (name, cat) in cards.items():
        if handle in fresh:
            old = session.query(RawSignal).filter_by(
                source="shopify_apps", source_id=handle).first()
            det = json.loads(old.raw_json or "{}")
        else:
            det = _app_details(handle)
            stats["requests"] += 1
            time.sleep(DETAIL_INTERVAL)
            if not det:
                continue
        partner = det.get("partner")
        if partner and partner not in partner_counts:
            partner_counts[partner] = _partner_app_count(partner)
            stats["requests"] += 1
            time.sleep(DETAIL_INTERVAL)
        app_count = partner_counts.get(partner)
        indie_score = 0
        reasons = []
        if app_count is not None and app_count <= 5:
            indie_score += 2
            reasons.append(f"名下app≤5({app_count})")
        if (det.get("rating_count") or 0) < 200_000:
            indie_score += 1
            reasons.append("评分数较小")

        records.append({
            "source": "shopify_apps",
            "source_id": handle,
            "signal_type": "成功样本",
            "title": f"{det.get('name') or name} — Shopify 生态高收入工具",
            "description": (f"{det.get('name') or name}（{det.get('brand') or partner or ''}）："
                            f"Shopify {cat} 类 top app，评分 {det.get('rating')} / "
                            f"{det.get('rating_count') or 0} 条评论，定价 "
                            f"{', '.join(det.get('pricing') or []) or '未知'}。"
                            f"{(det.get('description') or '')[:300]}"),
            "amount": None, "naics": None, "state": None,
            "posted_at": now, "fetched_at": now,
            "url": f"{BASE}/{handle}",
            "rating": int(det["rating"]) if det.get("rating") else None,
            "raw_json": json.dumps({
                "handle": handle, "category": cat, "brand": det.get("brand"),
                "partner": partner, "partner_app_count": app_count,
                "rating": det.get("rating"), "rating_count": det.get("rating_count"),
                "pricing": det.get("pricing"), "indie_score": indie_score,
                "indie_reasons": "; ".join(reasons),
            }, ensure_ascii=False),
        })
    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("shopify: %d app signals, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Shopify App Store collector")
    args = p.parse_args()
    from storage import get_session
    stats = collect(None, None, get_session())
    print(f"Shopify apps: {stats}")


if __name__ == "__main__":
    main()
