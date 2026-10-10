#!/bin/bash
# NeedRadar 每日批处理：采集 -> 抽取 -> 出榜。cron 友好（绝对路径、单实例锁）。
set -uo pipefail
export LC_ALL=en_US.UTF-8  # macOS bash 3.2 在 C locale 下会误解析多字节字符

PROJECT=/Users/jimmy/work/need-radar
PY=$PROJECT/.venv/bin/python
LOGDIR=$PROJECT/logs
LOG=$LOGDIR/daily_$(date +%Y%m%d).log
LOCK=/tmp/needradar_daily.lock
mkdir -p "$LOGDIR"

# LLM 轻量档（本地 Ollama，免费）；cron 环境无 shell 配置，显式 export
export NEEDRADAR_LLM_LIGHT=http://localhost:11434/v1
export NEEDRADAR_LLM_LIGHT_MODEL=gemma4:e4b

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9 2>/dev/null; then
    # macOS 无 flock 时退化为 pidfile 检查
    if [ -f "$LOCK.pid" ] && kill -0 "$(cat "$LOCK.pid")" 2>/dev/null; then
        echo "$(date '+%F %T') another instance running, exit" >> "$LOG"
        exit 0
    fi
fi
echo $$ > "$LOCK.pid"

cd "$PROJECT" || exit 1
{
    echo "===== daily run $(date '+%F %T') ====="

    if /usr/bin/pgrep -f "ollama" >/dev/null 2>&1 && curl -s -m 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "ollama: OK"
        LLM_OK=1
    else
        echo "ollama: NOT RUNNING — 抽取/裁决将跳过 LLM（纯规则），请启动 Ollama"
        LLM_OK=0
    fi

    # SAM.gov 配额 ~10次/天：run_daily 默认窗口（昨天）= 4 请求，每天一次安全
    echo "--- collect ---"
    "$PY" run_daily.py

    echo "--- extract ---"
    # 增量抽取：每日新增有限，limit 防失控（e4b 约 18s/条）
    "$PY" run_extract.py --limit 150

    echo "--- rank ---"
    "$PY" run_rank.py

    echo "===== done $(date '+%F %T') ====="
} >> "$LOG" 2>&1

rm -f "$LOCK.pid"
tail -5 "$LOG"
