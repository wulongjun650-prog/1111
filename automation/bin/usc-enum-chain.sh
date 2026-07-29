#!/usr/bin/env bash
# 自动链式跑字典: gap → dense1 → dense2 → dense3 → dense4
set -euo pipefail

export PATH="/data/venvs/pentest/bin:/data/automation/bin:/data/tools:/usr/local/bin:/usr/bin:$PATH"

BIN="/data/automation/bin"
STATE="/data/automation/results/us-campus.co.kr/email_enum_state"
LOG="/data/logs/usc-enum-fast"
CHAIN_LOG="$LOG/chain.log"
PID_FILE="$LOG/chain.pid"

HIT_GOAL="${HIT_GOAL:-5000}"
WORKERS="${WORKERS:-40}"
SHARDS="${SHARDS:-4}"
BATCH_SIZE="${BATCH_SIZE:-5000}"
TARGET="${TARGET:-10000}"
USE_PROXY="${USE_PROXY:-0}"
# 默认: gap → dense1-5
DICT_PHASES="${DICT_PHASES:-gap dense1 dense2 dense3 dense4 dense5}"

mkdir -p "$LOG"

hit_count() {
  python3 - <<'PY'
import json
from pathlib import Path
seen = set()
for p in [Path("/data/automation/results/us-campus.co.kr/email_enum_state/hits_all.jsonl")] + sorted(Path("/data/automation/results/us-campus.co.kr").glob("email_enum*/hits.jsonl")):
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            em = (json.loads(line).get("email") or "").lower()
            if em:
                seen.add(em)
        except Exception:
            pass
print(len(seen))
PY
}

dict_progress() {
  local dict="$1"
  local dir
  if [ "$dict" = "gap" ]; then
    dir="$STATE/gap"
  elif [[ "$dict" == dense* ]]; then
    dir="$STATE/$dict"
  else
    dir="$STATE/shard_${dict}"
  fi
  python3 - <<PY
import json
from pathlib import Path
p = Path("$dir") / "state.json"
if not p.exists():
    print("0 0 0")
else:
    s = json.loads(p.read_text())
    print(s.get("line", 0), s.get("total", 0), s.get("hits", 0))
PY
}

dict_running() {
  local dict="$1"
  pgrep -f "usc-enum-fast.py --dict ${dict} " >/dev/null 2>&1
}

dict_complete() {
  local dict="$1"
  read -r line total _ <<<"$(dict_progress "$dict")"
  [ -n "$total" ] && [ "$total" -gt 0 ] && [ "$line" -ge "$total" ]
}

start_dict() {
  local dict="$1"
  local log="$LOG/${dict}.log"
  echo "[chain] starting dict=$dict workers=$WORKERS target=$HIT_GOAL" | tee -a "$CHAIN_LOG"
  nohup python3 "$BIN/usc-enum-fast.py" \
    --dict "$dict" \
    --shard 0 \
    --shards 1 \
    --workers "$WORKERS" \
    --batch-size "$BATCH_SIZE" \
    --target "$HIT_GOAL" \
    >>"$log" 2>&1 &
  sleep 2
}

wait_dict() {
  local dict="$1"
  while dict_running "$dict"; do
    local hits
    hits=$(hit_count)
    read -r line total _ <<<"$(dict_progress "$dict")"
    echo "[chain] dict=$dict line=$line/$total hits=$hits goal=$HIT_GOAL" | tee -a "$CHAIN_LOG"
    if [ "$hits" -ge "$HIT_GOAL" ]; then
      echo "[chain] goal reached $hits >= $HIT_GOAL, stopping" | tee -a "$CHAIN_LOG"
      "$BIN/usc-enum-launch.sh" stop
      return 0
    fi
    sleep 60
  done
}

shard_progress() {
  local shard="$1"
  python3 - <<PY
import json
from pathlib import Path
p = Path("$STATE") / "shard_$shard" / "state.json"
if not p.exists():
    print("0 0 0")
else:
    s = json.loads(p.read_text())
    print(s.get("line", 0), s.get("total", 0), s.get("hits", 0))
PY
}

shard_running() {
  local shard="$1"
  pgrep -f "usc-enum-fast.py --shard ${shard} " >/dev/null 2>&1
}

shard_complete() {
  local shard="$1"
  read -r line total _ <<<"$(shard_progress "$shard")"
  [ -n "$total" ] && [ "$total" -gt 0 ] && [ "$line" -ge "$total" ]
}

start_shard_direct() {
  local shard="$1"
  echo "[chain] starting shard=$shard workers=$WORKERS" | tee -a "$CHAIN_LOG"
  nohup python3 "$BIN/usc-enum-fast.py" \
    --dict slim \
    --shard "$shard" \
    --shards "$SHARDS" \
    --workers "$WORKERS" \
    --batch-size "$BATCH_SIZE" \
    --target "$TARGET" \
    >>"$LOG/shard_${shard}.log" 2>&1 &
  sleep 2
}

wait_shard() {
  local shard="$1"
  while shard_running "$shard"; do
    local hits
    hits=$(hit_count)
    read -r line total _ <<<"$(shard_progress "$shard")"
    echo "[chain] shard=$shard line=$line/$total hits=$hits goal=$HIT_GOAL" | tee -a "$CHAIN_LOG"
    if [ "$hits" -ge "$HIT_GOAL" ]; then
      echo "[chain] goal reached $hits >= $HIT_GOAL, stopping" | tee -a "$CHAIN_LOG"
      "$BIN/usc-enum-launch.sh" stop
      return 0
    fi
    sleep 60
  done
}

run_slim_shards() {
  for ((shard=0; shard<SHARDS; shard++)); do
    local hits
    hits=$(hit_count)
    if [ "$hits" -ge "$HIT_GOAL" ]; then
      echo "[chain] already at $hits hits, done" | tee -a "$CHAIN_LOG"
      return 0
    fi

    if shard_complete "$shard" && ! shard_running "$shard"; then
      echo "[chain] shard=$shard already complete, skip" | tee -a "$CHAIN_LOG"
      continue
    fi

    if ! shard_running "$shard"; then
      start_shard_direct "$shard"
    else
      echo "[chain] shard=$shard already running, waiting" | tee -a "$CHAIN_LOG"
    fi

    wait_shard "$shard"
    hits=$(hit_count)
    echo "[chain] shard=$shard finished, total hits=$hits" | tee -a "$CHAIN_LOG"
    if [ "$hits" -ge "$HIT_GOAL" ]; then
      return 0
    fi
  done
}

run_dict_phases() {
  for dict in $DICT_PHASES; do
    local hits
    hits=$(hit_count)
    if [ "$hits" -ge "$HIT_GOAL" ]; then
      echo "[chain] goal reached at $hits, skip remaining phases" | tee -a "$CHAIN_LOG"
      return 0
    fi

    if dict_complete "$dict" && ! dict_running "$dict"; then
      echo "[chain] dict=$dict already complete, skip" | tee -a "$CHAIN_LOG"
      continue
    fi

    if dict_running "$dict"; then
      echo "[chain] dict=$dict already running, waiting" | tee -a "$CHAIN_LOG"
    else
      start_dict "$dict"
    fi

    wait_dict "$dict"
    hits=$(hit_count)
    echo "[chain] dict=$dict finished, total hits=$hits" | tee -a "$CHAIN_LOG"
  done
}

run_chain() {
  echo "[chain] started hit_goal=$HIT_GOAL phases=$DICT_PHASES workers=$WORKERS" | tee -a "$CHAIN_LOG"
  USE_PROXY=0 "$BIN/usc-enum-launch.sh" bootstrap 2>&1 | tee -a "$CHAIN_LOG" || true

  # slim shards 仅在未跑完时执行
  local need_slim=0
  for ((shard=0; shard<SHARDS; shard++)); do
    if ! shard_complete "$shard"; then
      need_slim=1
      break
    fi
  done
  if [ "$need_slim" -eq 1 ]; then
    run_slim_shards
  else
    echo "[chain] all slim shards complete, skip" | tee -a "$CHAIN_LOG"
  fi

  run_dict_phases

  USE_PROXY=0 "$BIN/usc-enum-launch.sh" merge 2>&1 | tee -a "$CHAIN_LOG"
  local hits
  hits=$(hit_count)
  echo "[chain] ALL DONE hits=$hits goal=$HIT_GOAL" | tee -a "$CHAIN_LOG"
}

case "${1:-start}" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "[chain] already running pid=$(cat "$PID_FILE")"
      exit 0
    fi
    nohup bash "$0" run >>"$CHAIN_LOG" 2>&1 &
    echo $! >"$PID_FILE"
    echo "[chain] supervisor pid=$(cat "$PID_FILE") log=$CHAIN_LOG phases=$DICT_PHASES"
    ;;
  run)
    run_chain
    rm -f "$PID_FILE"
    ;;
  stop)
    rm -f "$PID_FILE"
    pkill -f "usc-enum-chain.sh run" 2>/dev/null || true
    "$BIN/usc-enum-launch.sh" stop
    echo "[chain] stopped"
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "[chain] supervisor running pid=$(cat "$PID_FILE")"
    else
      echo "[chain] supervisor not running"
    fi
    echo "[chain] hits=$(hit_count) goal=$HIT_GOAL phases=$DICT_PHASES"
    for dict in $DICT_PHASES; do
      read -r line total dhits <<<"$(dict_progress "$dict")"
      if [ "${total:-0}" -gt 0 ]; then
        echo "[chain] $dict: line=$line/$total hits=$dhits complete=$( [ "$line" -ge "$total" ] && echo yes || echo no )"
      fi
    done
    tail -5 "$CHAIN_LOG" 2>/dev/null || true
    pgrep -af "usc-enum-fast.py" || echo "no enum process"
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    echo "  env: HIT_GOAL=5000 WORKERS=40 DICT_PHASES='gap dense1 dense2 dense3 dense4'"
    exit 1
    ;;
esac
