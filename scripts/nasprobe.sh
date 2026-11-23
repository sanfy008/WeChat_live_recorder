#!/usr/bin/env bash
# NAS 一次调用取全诊断信息。目的：把"容器/代码/日志/进程"这些排查项
# 合并成一次 SSH，避免每条命令都重连（每次连接 1-2s + 认证开销）。
# 用法: bash scripts/nasprobe.sh [额外要 grep 的关键词]
set -u
ssh -o BatchMode=yes -o ConnectTimeout=8 wong@192.168.68.49 '
echo "### DATE"; date "+%F %T %Z"
echo "### CONTAINERS"; docker ps -a --format "{{.Names}}|{{.Image}}|{{.Status}}" 2>/dev/null
echo "### CODEFILES"; for p in /app/wxlive/nas_runner.py /app/wxlive/stream_capture.py; do
  if [ -f "$p" ]; then echo "$p $(sha1sum "$p" | cut -c1-12) $(stat -c %y "$p" | cut -d. -f1)"; fi
done
echo "### GREP_FIX"; for p in /app/wxlive/nas_runner.py /app/wxlive/stream_capture.py; do
  [ -f "$p" ] && echo "-- $p" && grep -n "按时间补计\|文件已停止增长\|last_grow_log\|stale_restarts" "$p" | head -8
done
echo "### FFMPEG"; ps -eo pid,etime,args | grep "[f]fmpeg" | sed "s/\(.\{150\}\).*/\1/"
echo "### RECFILES"; ls -la /app/data/recordings/ 2>/dev/null | tail -8
echo "### END"
'
