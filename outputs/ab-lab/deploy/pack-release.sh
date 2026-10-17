#!/usr/bin/env bash
# Build a source archive of the current tree. Does not read the server or include data.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd -- "$root/../.." && pwd)"
handoff="$repo/HANDOFF.md"
stamp="$(date -u +%Y%m%d)"
out="${1:-$root/deploy/dist/ab-lab-$stamp.tar.gz}"
mkdir -p "$(dirname -- "$out")"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stage="$tmp/ab-lab-$stamp"
mkdir -p "$stage/ab-lab"
tar -C "$root" \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='deploy/dist' \
  --exclude='data' \
  -cf - . | tar -C "$stage/ab-lab" -xf -
if [[ ! -f "$handoff" ]]; then
  echo "Missing $handoff" >&2
  exit 1
fi
cp -- "$handoff" "$stage/交接.md"
(
  cd "$stage"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum
) > "$stage/SHA256SUMS"
tar -C "$tmp" -czf "$out" "ab-lab-$stamp"
sha256sum "$out"
echo "PACKED $out"
