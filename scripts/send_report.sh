#!/bin/bash
# NeedRadar 报告推送：最新 reports/top50_*.md 要点发飞书（cc-connect）。
set -uo pipefail
export LC_ALL=en_US.UTF-8  # macOS bash 3.2 在 C locale 下会误解析多字节字符

PROJECT=/Users/jimmy/work/need-radar
LOGDIR=$PROJECT/logs
LOG=$LOGDIR/send_$(date +%Y%m%d).log
mkdir -p "$LOGDIR"

cd "$PROJECT" || exit 1
REPORT=$(ls -t reports/top50_*.md 2>/dev/null | head -1)
if [ -z "$REPORT" ]; then
    echo "$(date '+%F %T') no report found" >> "$LOG"
    exit 1
fi

SOCK="$HOME/.cc-connect/run/api.sock"
if [ ! -S "$SOCK" ]; then
    echo "$(date '+%F %T') cc-connect daemon not running ($SOCK missing)" >> "$LOG"
    exit 1
fi
KEY=$(curl -s -m 5 --unix-socket "$SOCK" http://localhost/sessions \
    | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['session_key'])" 2>/dev/null)
if [ -z "$KEY" ]; then
    echo "$(date '+%F %T') no cc-connect session" >> "$LOG"
    exit 1
fi

# 飞书消息不宜过长：标题 + 过滤统计 + 主榜每条的主题摘要
RPT_DATE=$(basename "$REPORT" .md | sed 's/top50_//')
{
    echo "📡 NeedRadar 需求榜单 $RPT_DATE"
    echo
    grep "^过滤统计" "$REPORT"
    echo
    awk '/^## 主榜/{f=1;next} /^## /{f=0} f && /^### /{print}' "$REPORT" | sed 's/^### //'
    echo
    echo "（完整报告：${REPORT}）"
} > /tmp/needradar_report_msg.txt

OUT=$(cc-connect send -p kimi-main -s "$KEY" --stdin < /tmp/needradar_report_msg.txt 2>&1)
echo "$(date '+%F %T') send: $OUT" >> "$LOG"
echo "$OUT"
