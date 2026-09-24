#!/bin/bash
# 在 103.121.95.90 上执行，将 jpvapou 经验追加到 AI-PROMPT.txt
set -euo pipefail
PROMPT="/data/automation/AI-PROMPT.txt"
SECTION="/data/automation/AI-PROMPT-jpvapou-section.txt"
MARKER="## 目标三：jpvapou.com"

if [[ ! -f "$PROMPT" ]]; then
  echo "ERROR: $PROMPT not found" >&2
  exit 1
fi

if grep -q "$MARKER" "$PROMPT"; then
  echo "jpvapou section already exists, skip"
  exit 0
fi

if [[ ! -f "$SECTION" ]]; then
  echo "ERROR: $SECTION not found, copy AI-PROMPT-jpvapou-section.txt to server first" >&2
  exit 1
fi

cp -a "$PROMPT" "${PROMPT}.bak.$(date +%Y%m%d_%H%M%S)"
cat "$SECTION" >> "$PROMPT"
echo "Appended jpvapou section to $PROMPT"
