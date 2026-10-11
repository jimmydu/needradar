"""Shopify category fragmentation scan: find niche categories without a
dominant player ("抄成功者"升级版——不抄第一名，找没有 Gorgias 的缝隙品类).

Methodology: review counts are a market-share PROXY (more reviews ~ more
installed base). Per category: top1/top3 share, HHI, mid-waist thickness
(50-2000 reviews), indie viability (partner with <=5 apps), monetization
(paid tier presence). Categories with <10 apps are skipped.
"""
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from statistics import mean

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from storage import RawSignal, get_session

log = logging.getLogger(__name__)

MIN_APPS = 10


def _norm(s):
    import re
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def load_blacklist(path="./indie_blacklist.txt"):
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                out.append(_norm(line))
    return out


def category_metrics(apps, blacklist):
    """apps: list of raw_json dicts for one category."""
    rcs = sorted((a.get("rating_count") or 0 for a in apps), reverse=True)
    total = sum(rcs) or 1
    top1 = rcs[0] / total if rcs else 0
    top3 = sum(rcs[:3]) / total
    hhi = sum((rc / total) ** 2 for rc in rcs) if total else 0
    waist = [a for a in apps if 50 <= (a.get("rating_count") or 0) <= 2000]
    indie = [a for a in apps
             if (a.get("partner_app_count") is not None and a["partner_app_count"] <= 5
                 and not next((b for b in blacklist if b in _norm(a.get("brand") or a.get("partner") or "")), None))]
    paid = [a for a in apps
            if any(p.startswith("$") for p in (a.get("pricing") or []))]
    # niche score: fragmented + thick waist + indie viable + monetized
    frag = (1 - top3) * 0.6 + (1 - min(hhi / 0.3, 1)) * 0.4
    waist_score = min(len(waist) / 10, 1)
    indie_score = min(len(indie) / 10, 1)
    paid_score = len(paid) / len(apps) if apps else 0
    score = 0.35 * frag + 0.25 * waist_score + 0.20 * indie_score + 0.20 * paid_score
    return {
        "n_apps": len(apps), "top1_share": round(top1, 3), "top3_share": round(top3, 3),
        "hhi": round(hhi, 3), "waist_n": len(waist), "indie_n": len(indie),
        "paid_share": round(paid_score, 2), "niche_score": round(score, 3),
        "waist_apps": sorted(waist, key=lambda a: -(a.get("rating_count") or 0))[:5],
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = get_session()
    blacklist = load_blacklist()

    by_cat = defaultdict(list)
    for r in session.query(RawSignal).filter_by(source="shopify_apps"):
        raw = json.loads(r.raw_json or "{}")
        raw["_url"] = r.url
        by_cat[raw.get("category") or "misc"].append(raw)

    rows = []
    for cat, apps in by_cat.items():
        if len(apps) < MIN_APPS:
            log.info("skip %s (%d apps)", cat, len(apps))
            continue
        m = category_metrics(apps, blacklist)
        m["category"] = cat
        rows.append(m)
    rows.sort(key=lambda r: -r["niche_score"])

    today = date.today().isoformat()
    lines = [
        f"# Shopify 缝隙品类扫描（{today}）",
        "",
        "方法论：评论数作市场份额代理估算；缝隙分数 = 0.35×分散度（Top3 占比与 HHI 合成）"
        " + 0.25×腰部厚度（50-2000 评论的 app 数） + 0.20×indie 可活度（partner 名下≤5 个 app 且非黑名单）"
        " + 0.20×变现验证（含付费档位占比）。<10 个 app 的品类不排。",
        "",
    ]
    for rank, m in enumerate(rows[:20], 1):
        lines.append(f"## {rank}. {m['category']} — 缝隙分数 {m['niche_score']}")
        lines.append(
            f"- Top1/Top3 评论占比：{m['top1_share']:.0%}/{m['top3_share']:.0%}｜"
            f"HHI {m['hhi']}｜腰部 {m['waist_n']} 个｜indie {m['indie_n']} 个｜"
            f"含付费档 {m['paid_share']:.0%}（共 {m['n_apps']} 个 app）")
        for a in m["waist_apps"]:
            pricing = ", ".join(a.get("pricing") or []) or "未知"
            lines.append(f"  - {a.get('brand') or a.get('handle')}（{a.get('rating_count') or 0} 评论，{pricing}）"
                         f" {a.get('_url') or ''}")
        lines.append("")

    path = f"reports/shopify_niches_{today.replace('-', '')}.md"
    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"\n扫描品类 {len(rows)} 个（跳过 <{MIN_APPS} app 的），报告: {path}")
    for rank, m in enumerate(rows[:10], 1):
        print(f"{rank:2d}. {m['category']:55s} 缝隙分数 {m['niche_score']:.3f} "
              f"(top3 {m['top3_share']:.0%}, 腰部 {m['waist_n']}, indie {m['indie_n']}, 付费 {m['paid_share']:.0%})")


if __name__ == "__main__":
    main()
