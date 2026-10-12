"""Remote-work deep report: worldwide senior-dev lens.

User profile: China/Shanghai-based senior programmer, strong English,
ex-foreign-enterprise. Only worldwide-remote jobs matter. Output:
reports/remote_jobs_deep_YYYYMMDD.md
"""
import json
import logging
from collections import Counter
from datetime import date

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from storage import RawSignal, get_session

log = logging.getLogger(__name__)

PROFILE_NOTE = """## 用户画像适配分析

- **时区**：Worldwide 岗位多数要求欧美工作时间重叠（overlap），中国时区（UTC+8）与欧洲有 2-4 小时黄金重叠，与美国西海岸几乎无重叠——优先选 async-first 或 Europe-friendly 公司，纯 US-hours 岗位慎投。
- **支付**：Deel / Payoneer / Wise 收款均可行；外包合同通常走 contractor 模式（发票自开），注意国内的个人外汇结算与社保安排。
- **用工形态**：对中国开发者的常见合规形态是 EOR（Deel/Remote.com 代雇）或纯 contractor；JD 里写 "employee via EOR" 的比写 "US payroll only" 的更可行。
- **资历定位**：资深+外企经验适配 senior/staff 且要求 written English 的岗位；避开标 "US timezone required" 的。
"""


def main():
    logging.basicConfig(level=logging.INFO)
    s = get_session()
    rows = s.query(RawSignal).filter_by(signal_type="远程招聘").all()
    log.info("total remote-job signals: %d", len(rows))

    today = date.today().isoformat()
    skills = Counter()
    senior_n = 0
    salaries = []
    by_source = Counter()
    jobs = []
    for r in rows:
        raw = json.loads(r.raw_json or "{}")
        by_source[r.source] += 1
        if raw.get("skill"):
            skills[raw["skill"]] += 1
        if raw.get("senior"):
            senior_n += 1
        if raw.get("salary_min") and raw.get("salary_max"):
            salaries.append((raw["salary_min"], raw["salary_max"], r.title, r.url))
        jobs.append((r.posted_at, raw.get("senior", False), raw.get("skill", "general"),
                     r.title, r.url, r.source))

    jobs.sort(key=lambda x: (x[0] is None, -(x[0].timestamp() if x[0] else 0), not x[1]))
    top = [j for j in jobs if j[1]][:10] or jobs[:10]

    sal_mid = sorted((a + b) / 2 for a, b, _, _ in salaries)
    sal_txt = (f"{len(salaries)} 条带薪资；中位约 ${sal_mid[len(sal_mid)//2]:,.0f}"
               if sal_mid else "带薪资字段的极少")

    lines = [
        f"# 远程工作深挖报告（{today}）",
        "",
        f"本期 Worldwide 远程岗位（四源去重后）：**{len(rows)}** 条"
        f"（{', '.join(f'{k} {v}' for k, v in by_source.most_common())}）",
        "",
        "## 技能栈分布（Top 10）",
        "",
        *[f"- {k}: {v}" for k, v in skills.most_common(10)],
        "",
        f"## 薪资分布",
        "",
        f"- {sal_txt}",
        f"- senior/lead/staff 级别占比：{senior_n}/{len(rows)}",
        "",
        "## 值得关注的岗位 Top 10（senior 优先 + 最新）",
        "",
    ]
    for i, (_, senior, skill, title, url, src) in enumerate(top, 1):
        lines.append(f"{i}. {'[senior] ' if senior else ''}{title}（{skill}）— {url}")
    lines += ["", PROFILE_NOTE]
    path = f"reports/remote_jobs_deep_{today.replace('-', '')}.md"
    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(path)


if __name__ == "__main__":
    main()
