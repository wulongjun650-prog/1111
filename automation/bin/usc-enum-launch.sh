#!/usr/bin/env bash
# us-campus 邮箱枚举 v3 启动器 (支持多代理并行)
set -euo pipefail

export PATH="/data/venvs/pentest/bin:/data/automation/bin:/data/tools:/data/go/bin:/usr/local/bin:/usr/bin:$PATH"

BIN="/data/automation/bin"
CFG="/data/automation/config"
STATE="/data/automation/results/us-campus.co.kr/email_enum_state"
LOG="/data/logs/usc-enum-fast"
PROXY_FILE="${PROXY_FILE:-$CFG/proxies-8.txt}"
WORKERS="${WORKERS:-40}"
SHARDS="${SHARDS:-8}"
ACTIVE_SHARDS="${ACTIVE_SHARDS:-8}"
TARGET="${TARGET:-10000}"
BATCH_SIZE="${BATCH_SIZE:-5000}"

mkdir -p "$LOG" "$STATE" "$CFG"

proxy_url_for_line() {
  local line="$1"
  local host port user pass
  IFS=: read -r host port user pass <<<"$line"
  echo "http://${user}:${pass}@${host}:${port}"
}

merge_hits() {
  python3 - <<'PY'
import json
from pathlib import Path
base = Path("/data/automation/results/us-campus.co.kr")
state = base / "email_enum_state"
hits = {}
for p in [state / "hits_all.jsonl"] + sorted(base.glob("email_enum*/hits.jsonl")):
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

bootstrap_if_needed() {
  if python3 "$BIN/usc-enum-bootstrap.py" --check 2>/dev/null; then
    echo "[launch] bootstrap skipped (tested.db fresh)"
    return 0
  fi
  echo "[launch] incremental bootstrap..."
  python3 "$BIN/usc-enum-bootstrap.py" | tee -a "$LOG/bootstrap.log"
}

stop_all() {
  pkill -f "usc-enum-fast.py" 2>/dev/null || true
  sleep 1
}

start_shard() {
  local shard="$1"
  local proxy="${2:-}"
  local log="$LOG/shard_${shard}.log"
  local extra=()
  if [ -n "$proxy" ]; then
    extra+=(--proxy "$proxy")
    echo "[launch] shard=$shard workers=$WORKERS proxy=${proxy#*@} -> $log"
  else
    echo "[launch] shard=$shard workers=$WORKERS (direct) -> $log"
  fi
  nohup python3 "$BIN/usc-enum-fast.py" \
    --shard "$shard" \
    --shards "$SHARDS" \
    --workers "$WORKERS" \
    --batch-size "$BATCH_SIZE" \
    --target "$TARGET" \
    "${extra[@]}" \
    >>"$log" 2>&1 &
}

start_with_proxies() {
  local n="$ACTIVE_SHARDS"
  if [ ! -f "$PROXY_FILE" ]; then
    echo "[launch] missing proxy file: $PROXY_FILE" >&2
    exit 1
  fi
  mapfile -t PROXIES < <(grep -v '^[[:space:]]*$' "$PROXY_FILE" | head -n "$n")
  if [ "${#PROXIES[@]}" -lt "$n" ]; then
    echo "[launch] need $n proxies, got ${#PROXIES[@]} in $PROXY_FILE" >&2
    exit 1
  fi
  for ((i=0; i<n; i++)); do
    start_shard "$i" "$(proxy_url_for_line "${PROXIES[$i]}")"
    sleep 1
  done
}

case "${1:-start}" in
  start)
    stop_all
    bootstrap_if_needed
    if [ -f "$PROXY_FILE" ] && [ "${USE_PROXY:-1}" = "1" ]; then
      start_with_proxies
    else
      for ((i=0; i<ACTIVE_SHARDS; i++)); do
        start_shard "$i"
        sleep 1
      done
    fi
    echo "[launch] started $ACTIVE_SHARDS shard(s), logs: $LOG/"
    ;;
  start-8)
    stop_all
    bootstrap_if_needed
    SHARDS=8 ACTIVE_SHARDS=8 start_with_proxies
    echo "[launch] started 8 proxy shards, logs: $LOG/"
    ;;
  start-all)
    stop_all
    bootstrap_if_needed
    if [ -f "$PROXY_FILE" ] && [ "${USE_PROXY:-1}" = "1" ]; then
      ACTIVE_SHARDS="$SHARDS" start_with_proxies
    else
      for ((i=0; i<SHARDS; i++)); do
        start_shard "$i"
        sleep 1
      done
    fi
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
    python3 "$BIN/usc-enum-bootstrap.py" "${@:2}"
    ;;
  rebuild-candidates)
    stop_all
    rm -f "$STATE"/shard_*/candidates.txt "$STATE"/shard_*/candidates.meta.json
    echo "[launch] candidate files cleared, run start to rebuild"
    ;;
  test-proxies)
    python3 - <<'PY'
import sys, urllib.request, json
from pathlib import Path
pf = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/automation/config/proxies-8.txt")
for i, line in enumerate(pf.read_text().splitlines()):
    line = line.strip()
    if not line:
        continue
    h, p, u, pw = line.split(":", 3)
    url = f"http://{u}:{pw}@{h}:{p}"
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
        t0 = __import__("time").time()
        r = op.open(urllib.request.Request("https://ipinfo.io/json"), timeout=15)
        info = json.loads(r.read())
        dt = __import__("time").time() - t0
        print(f"#{i} OK ip={info.get('ip')} city={info.get('city')} country={info.get('country')} {dt:.1f}s")
    except Exception as e:
        print(f"#{i} FAIL {line[:30]}... {e}")
PY
    "$PROXY_FILE"
    ;;
  *)
    echo "usage: $0 {start|start-8|start-all|stop|status|merge|bootstrap|rebuild-candidates|test-proxies}"
    echo "  env: WORKERS=40 SHARDS=8 ACTIVE_SHARDS=8 PROXY_FILE=... USE_PROXY=0"
    exit 1
    ;;
esac
