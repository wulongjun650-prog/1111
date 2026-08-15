@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 映界 Lumina · 本机真人开口
if not exist "vendor\sadtalker-venv\Scripts\python.exe" (
  echo 第一次运行会下载 SadTalker 和模型，大约 2GB，需要 NVIDIA 显卡。
  powershell -ExecutionPolicy Bypass -File "%~dp0scripts\setup_talking_host.ps1"
  if errorlevel 1 (
    echo 安装失败。请看上面的报错。
    pause
    exit /b 1
  )
)
set PYTHONPATH=.
python -m backend.talking_host
echo.
echo 成片在 samples\host-talking.mp4
pause
