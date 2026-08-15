#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "映界 Lumina · 安装本机真人开口（需要 NVIDIA 显卡）"

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "没有检测到 nvidia-smi。请在有 NVIDIA 显卡的电脑上运行。"
  exit 1
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true

if ! command -v git >/dev/null 2>&1; then
  echo "需要 Git"
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "需要 FFmpeg，请先安装并加入 PATH"
  exit 1
fi

PY=""
for c in python3.10 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done
if [[ -z "$PY" ]]; then
  echo "需要 Python 3.10+"
  exit 1
fi
echo "使用 $PY $($PY --version)"

"$PY" -m pip install -r requirements.txt
mkdir -p vendor
if [[ ! -f vendor/SadTalker/inference.py ]]; then
  git clone --depth 1 https://github.com/OpenTalker/SadTalker.git vendor/SadTalker
fi

if [[ ! -x vendor/sadtalker-venv/bin/python ]]; then
  "$PY" -m venv vendor/sadtalker-venv
fi
VPY=vendor/sadtalker-venv/bin/python
"$VPY" -m pip install -U pip wheel
"$VPY" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
"$VPY" -c "import torch; assert torch.cuda.is_available(), 'CUDA 不可用，请检查 NVIDIA 驱动与 CUDA 版 PyTorch'; print(torch.cuda.get_device_name(0))"
"$VPY" -m pip install -r scripts/sadtalker-pip.txt
"$PY" scripts/download_sadtalker_models.py --dir vendor/SadTalker

echo
echo "安装完成。生成开口成片："
echo "  PYTHONPATH=. $PY -m backend.talking_host"
echo "或启动工作室后点「本机真人开口」。"
