"""Manual signal entry CLI.

For high-value sources without a legal API (e.g. Kickstarter most-funded):
a human browses, then types the signal in. Records land in raw_signals with
source='manual' and go through the same extract/score pipeline as automated
collection.

Usage:
    python manual_entry.py                  # interactive
    python manual_entry.py --import signals.json
"""
import argparse
import hashlib
import json
from datetime import date, datetime

from storage import get_session, upsert_signals

SIGNAL_TYPES = {
    "1": "外包发布",
    "2": "众筹预售",
    "3": "招标/采购",
    "4": "付费抱怨/求推荐",
    "5": "其他",
}
PLATFORM_HINTS = "kickstarter / indiegogo / 朋友推荐 / 展会 / 其他自由文本"


def make_source_id(title, platform, url):
    h = hashlib.sha256(f"{title}|{platform}|{url}".encode("utf-8")).hexdigest()
    return f"manual_{h[:12]}"


def build_record(item, now=None):
    """Normalize a dict (interactive or JSON import) into a raw_signals record."""
    now = now or datetime.utcnow()
    title = (item.get("title") or "").strip()
    platform = (item.get("platform") or "").strip()
    if not title or not platform:
        raise ValueError("title and platform are required")
    url = (item.get("url") or "").strip() or None

    signal_type = item.get("signal_type") or ""
    signal_type = SIGNAL_TYPES.get(signal_type, signal_type)  # accept menu number or literal
    if signal_type not in SIGNAL_TYPES.values():
        raise ValueError(f"invalid signal_type: {item.get('signal_type')!r} (use 1-5 or {sorted(SIGNAL_TYPES.values())})")

    amount = item.get("amount")
    try:
        amount = float(amount) if amount not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError(f"invalid amount: {item.get('amount')!r}")

    p_level = item.get("p_level")
    try:
        p_level = int(p_level) if p_level not in (None, "") else None
        if p_level is not None and not 1 <= p_level <= 5:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"invalid p_level: {item.get('p_level')!r} (1-5 or empty)")

    posted_at = item.get("posted_at") or date.today().isoformat()
    try:
        posted_at = datetime.strptime(str(posted_at)[:10], "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"invalid posted_at: {item.get('posted_at')!r} (YYYY-MM-DD)")

    return {
        "source": "manual",
        "source_id": make_source_id(title, platform, url),
        "signal_type": signal_type,
        "title": title,
        "description": (item.get("description") or "").strip() or None,
        "amount": amount,
        "naics": None,
        "state": None,
        "posted_at": posted_at,
        "fetched_at": now,
        "url": url,
        "rating": None,
        "raw_json": json.dumps({
            "platform": platform,
            "p_level": p_level,
            "amount_note": item.get("amount_note") or None,
            "notes": item.get("notes") or None,
        }, ensure_ascii=False),
    }


def _ask(prompt, default=None):
    s = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return s or (default or "")


def _ask_multiline(prompt):
    print(f"{prompt}（可多行，空行结束）:")
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def interactive(session):
    n = 0
    while True:
        print("\n--- 手动录入信号 ---")
        item = {
            "title": _ask("标题（必填）"),
            "platform": _ask(f"来源平台（必填，如 {PLATFORM_HINTS}）"),
            "url": _ask("URL（证据链接，可空）"),
            "description": _ask_multiline("描述"),
            "amount": _ask("金额线索（数字，可空）"),
            "amount_note": _ask("金额说明（如 '已筹得 $120,000，目标 $20,000'，可空）"),
            "signal_type": _ask("信号类型 1=外包发布 2=众筹预售 3=招标/采购 4=付费抱怨/求推荐 5=其他（必填）"),
            "p_level": _ask("付费信号等级 P（1-5，判断不准可留空）"),
            "posted_at": _ask("发布/观察日期（YYYY-MM-DD）", date.today().isoformat()),
            "notes": _ask("备注（可空）"),
        }
        try:
            rec = build_record(item)
        except ValueError as e:
            print(f"输入有误：{e}，本条未入库")
        else:
            print("\n摘要：")
            print(f"  标题: {rec['title']}\n  平台: {json.loads(rec['raw_json'])['platform']}"
                  f"\n  类型: {rec['signal_type']}  P: {json.loads(rec['raw_json'])['p_level']}"
                  f"\n  金额: {rec['amount']}  日期: {rec['posted_at'].date()}  URL: {rec['url']}")
            if _ask("确认入库？(y/n)", "y").lower() == "y":
                ins, skip = upsert_signals(session, [rec])
                print("已入库" if ins else "已存在（重复信号，跳过）")
                n += ins
        if _ask("继续录入下一条？(y/n)", "n").lower() != "y":
            break
    print(f"\n本次共入库 {n} 条")


def batch_import(session, path):
    with open(path) as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise SystemExit("JSON 顶层必须是数组")

    records, errors = [], []
    for i, item in enumerate(items):
        try:
            records.append(build_record(item))
        except (ValueError, AttributeError) as e:
            errors.append((i, (item or {}).get("title"), str(e)))
    ins, skip = upsert_signals(session, records)
    print(f"导入 {path}: 成功 {ins}，重复跳过 {skip}，失败 {len(errors)}")
    for i, title, err in errors:
        print(f"  失败[{i}] {title}: {err}")


def main():
    p = argparse.ArgumentParser(description="NeedRadar 手动信号录入")
    p.add_argument("--import", dest="import_file", metavar="JSON_FILE")
    args = p.parse_args()

    session = get_session()
    if args.import_file:
        batch_import(session, args.import_file)
    else:
        try:
            interactive(session)
        except (EOFError, KeyboardInterrupt):
            print("\n已退出")


if __name__ == "__main__":
    main()
