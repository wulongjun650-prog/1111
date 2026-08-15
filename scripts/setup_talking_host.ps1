$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
Write-Host "映界 Lumina · 安装本机真人开口（需要 NVIDIA 显卡）"

function Need-Cmd($name, $hint) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    Write-Host "缺少 $name。$hint"
    exit 1
  }
}

if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
  Write-Host "没有检测到 nvidia-smi。请在有 NVIDIA 显卡、已装驱动的 Windows 电脑上运行。"
  exit 1
}
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

Need-Cmd git "请安装 Git：https://git-scm.com/download/win"
Need-Cmd python "请安装 Python 3.10：https://www.python.org/downloads/ （勾选 Add python.exe to PATH）"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "没有 FFmpeg。正在尝试 winget 安装…"
  try {
    winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
  } catch {
    Write-Host "请手动安装 FFmpeg 并加入 PATH：https://www.gyan.dev/ffmpeg/builds/"
    exit 1
  }
}

python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force -Path vendor | Out-Null
if (-not (Test-Path "vendor\SadTalker\inference.py")) {
  git clone --depth 1 https://github.com/OpenTalker/SadTalker.git vendor\SadTalker
}

$venvPy = "vendor\sadtalker-venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  python -m venv vendor\sadtalker-venv
}
& $venvPy -m pip install -U pip wheel
& $venvPy -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
& $venvPy -c "import torch; assert torch.cuda.is_available(), 'CUDA 不可用，请检查 NVIDIA 驱动与 CUDA 版 PyTorch'; print(torch.cuda.get_device_name(0))"
& $venvPy -m pip install -r scripts\sadtalker-pip.txt
python scripts\download_sadtalker_models.py --dir vendor\SadTalker

Write-Host ""
Write-Host "安装完成。生成开口成片："
Write-Host "  python -m backend.talking_host"
Write-Host "或双击 run-talking-host.bat，或启动工作室后点「本机真人开口」。"
