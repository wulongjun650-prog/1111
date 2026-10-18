#!/usr/bin/env bash
set -euo pipefail
OUT="${1:-/workspace/capture/api-probe}"
mkdir -p "$OUT"

HOSTS=(
  "https://api-node.openlover.ai"
  "https://api.genspicy.ai"
)

PATHS=(
  "/"
  "/admin"
  "/admin/docs"
  "/admin/swagger"
  "/admin/swagger.json"
  "/admin/openapi.json"
  "/admin/api-docs"
  "/docs"
  "/swagger"
  "/swagger.json"
  "/openapi.json"
  "/api-docs"
  "/v3/api-docs"
  "/health"
  "/admin/health"
)

UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0"

report="$OUT/probe-report.txt"
: > "$report"

for host in "${HOSTS[@]}"; do
  echo "======== $host ========" | tee -a "$report"
  for p in "${PATHS[@]}"; do
    url="${host}${p}"
    code=$(curl -sS -o "$OUT/$(echo "${host}${p}" | sed 's|https://||;s|/|_|g').body" -w "%{http_code}" -A "$UA" "$url" || echo "000")
    body_file="$OUT/$(echo "${host}${p}" | sed 's|https://||;s|/|_|g').body"
    snippet=$(head -c 400 "$body_file" 2>/dev/null | tr '\n' ' ')
    echo "$p -> HTTP $code | $snippet" | tee -a "$report"
  done
done

# Admin API routes from handoff (unauthenticated GET)
BASE="https://api-node.openlover.ai/admin"
routes=(
  advertisers advertising auth/genspicy/sso-ticket auth/login content
  content/template-video-tags content/templates dashboard diamond-management
  diamond-records distribution-partners distributors emails generate-cache
  genspicy/account-monitoring genspicy/annotations genspicy/chargebacks
  genspicy/cloudwatch genspicy/health genspicy/payment-management
  genspicy/payment-orders login members meta-pixels page published rankings
  review settings templates
)
echo "======== admin route probe ========" | tee -a "$report"
for r in "${routes[@]}"; do
  url="$BASE/$r"
  body=$(curl -sS -A "$UA" "$url")
  echo "/$r -> $body" | tee -a "$report"
done

echo "Done: $report"
