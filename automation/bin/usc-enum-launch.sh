#!/usr/bin/env bash
# us-campus 邮箱枚举 v2 启动器: bootstrap -> 多进程分片
set -euo pipefail

export PATH="/data/venvs/pentest/bin:/data/automation/bin:/data/tools:/data/go/bin:/usr/local/bin:/usr/bin:$PATH"

BIN="/data/automation/bin"
STATE="/data/automation/results/us-campus.co.kr/email_enum_state"
LOG="/data/logs/usc-enum-fast"
WORKERS="${WORKERS:-100}"
SHARDS="${SHARDS:-4}"
ACTIVE_SHARDS="${ACTIVE_SHARDS:-2}"   # 先起 2 个分片，稳定后可改 4
TARGET="${TARGET:-10000}"

mkdir -p "$LOG" "$STATE"

merge_hits() {
  python3 - <<'PY'
import json
from pathlib import Path
base = Path("/data/automation/results/us-campus.co.kr")
state = base / "email_enum_state"
hits = {}
for p in list(state.glob("hits_all.jsonl")) + sorted(base.glob("email_enum*/hits.jsonl")):
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            r = json.loads(line)
            em = (r.get("email") or "").lower()
            if em:
                hits[em] = r
        except Exception:
            pass
out = state / "hits_merged.json"
out.write_text(json.dumps(list(hits.values()), indent=2, ensure_ascii=False), encoding="utf-8")
print("merged", len(hits), "->", out)
PY
}

stop_all() {
  pkill -f "usc-enum-fast.py" 2>/dev/null || true
}

start_shard() {
  local shard="$1"
  local log="$LOG/shard_${shard}.log"
  echo "[launch] shard $shard workers=$WORKERS -> $log"
  nohup python3 "$BIN/usc-enum-fast.py" \
    --shard "$shard" \
    --shards "$SHARDS" \
    --workers "$WORKERS" \
    --target "$TARGET" \
    >>"$log" 2>&1 &
}

case "${1:-start}" in
  start)
    stop_all
    echo "[launch] bootstrap tested db..."
    python3 "$BIN/usc-enum-bootstrap.py" | tee -a "$LOG/bootstrap.log"
    for ((i=0; i<ACTIVE_SHARDS; i++)); do
      start_shard "$i"
      sleep 2
    done
    echo "[launch] started $ACTIVE_SHARDS shards (of $SHARDS). logs: $LOG/"
    ;;
  start-all)
    stop_all
    echo "[launch] bootstrap tested db..."
    python3 "$BIN/usc-enum-bootstrap.py" | tee -a "$LOG/bootstrap.log"
    for ((i=0; i<SHARDS; i++)); do
      start_shard "$i"
      sleep 2
    done
    echo "[launch] started all $SHARDS shards"
    ;;
  stop)
    stop_all
    echo "[launch] stopped"
    ;;
  status)
    pgrep -af "usc-enum-fast.py" || echo "not running"
    for f in "$STATE"/shard_*/state.json; do
      [ -f "$f" ] && echo "--- $f ---" && cat "$f"
    done
  ;;
  merge)
    merge_hits
    ;;
  bootstrap)
    python3 "$BIN/usc-enum-bootstrap.py"
    ;;
  *)
    echo "usage: $0 {start|start-all|stop|status|merge|bootstrap}"
    echo "  env: WORKERS=100 SHARDS=4 ACTIVE_SHARDS=2 TARGET=10000"
    exit 1
    ;;
esac
