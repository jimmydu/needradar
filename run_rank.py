"""Ranking entrypoint: aggregate, score, print and export the Top N report.

    python run_rank.py                 # top 10, 4-week window, writes report + DB
    python run_rank.py --top 20 --weeks 4 --dry-run   # no writes, no LLM
"""
import argparse
import json
import logging
import os
from datetime import date

from aggregator.pipeline import run
from storage import get_session


def fmt_amount(a):
    return f"${a:,.0f}" if a else "无金额线索"


def render_report(results, usage, top, window_desc, run_date):
    def section(items, title):
        lines = [f"## {title}", ""]
        for rank, r in enumerate(items, 1):
            st = r["stats"]
            lines += [
                f"### {rank}. {r['summary']}",
                "",
                f"- 关键词：{'、'.join(r['keywords'][:6])}",
                f"- WPS 总分：**{r['wps']}**（等级 {r['wps_p']} / 密度 {r['wps_density']} / "
                f"多样性 {r['wps_diversity']} / 金额 {r['wps_amount']} / 趋势 {r['wps_trend']} / 地域 {r['wps_geo']}）",
                f"- 置信度：{r['confidence']}｜可进入性：{r['accessibility']}｜最终分：**{r['final_score']}**",
                f"- 证据：{st['n']} 条信号，来源 {json.dumps(dict(st['sources']), ensure_ascii=False)}，"
                f"P 级分布 {json.dumps({str(k): v for k, v in st['p_dist'].items()})}，"
                f"中位金额 {fmt_amount(st['median_amount'])}",
            ]
            if r["quotes"]:
                lines.append("- 原文引用：")
                for q in r["quotes"]:
                    lines.append(f"  > {q}")
            lines.append("")
        return lines

    by_wps = sorted(results, key=lambda r: -r["wps"])[:top]
    lines = [
        f"# NeedRadar Top {top} 需求榜单（{run_date}）",
        "",
        f"窗口：{window_desc}｜主榜按 最终分 = WPS × 置信度 × 可进入性 排序；附纯 WPS 榜（§9.5）",
        "",
    ]
    lines += section(results[:top], "主榜（最终分）")
    lines += section(by_wps, "纯 WPS 榜")
    lines += [
        "---",
        f"LLM 合并裁决调用 {usage['calls']} 次，"
        f"prompt {usage['prompt_tokens']} tokens，completion {usage['completion_tokens']} tokens。",
        "注：本榜单为公式自动输出，按 §11 流程需人工审核 Top 20 后定稿。",
    ]
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description="NeedRadar aggregation + WPS ranking")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--weeks", type=int, default=4)
    p.add_argument("--dry-run", action="store_true", help="不写库、不调 LLM，只打印")
    p.add_argument("--no-llm", action="store_true", help="跳过 LLM 合并裁决")
    args = p.parse_args()

    session = get_session()
    results, usage = run(session, weeks=args.weeks,
                         use_llm=not (args.no_llm or args.dry_run),
                         dry_run=args.dry_run)

    print(f"\n===== NeedRadar Top {args.top}（最终分排序） =====")
    for rank, r in enumerate(results[:args.top], 1):
        st = r["stats"]
        print(f"{rank:2d}. [{r['final_score']:.3f}] WPS={r['wps']:.3f} "
              f"conf={r['confidence']:.2f} acc={r['accessibility']:.2f} "
              f"P{st['p_max']} n={st['n']} src={st['n_sources']} | {r['summary'][:70]}")

    run_date = date.today().isoformat()
    report = render_report(results, usage, args.top,
                           f"过去 {args.weeks} 周滚动", run_date)
    if not args.dry_run:
        os.makedirs("reports", exist_ok=True)
        path = f"reports/top10_{run_date.replace('-', '')}.md"
        with open(path, "w") as f:
            f.write(report)
        print(f"\n报告已导出: {path}")
    print(f"LLM 调用: {usage['calls']} 次, tokens: {usage['prompt_tokens']}+{usage['completion_tokens']}")


if __name__ == "__main__":
    main()
