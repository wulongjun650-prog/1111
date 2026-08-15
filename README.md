# 映界 Lumina

本地视频生成工作室。

**真人开口必须用你自己的电脑（NVIDIA 显卡）。** 云端没有 GPU，做不出人在说话、转头；静帧推拉不是开口。

## 本机真人开口（需要 NVIDIA 显卡）

同一男人先站在奔驰 / 宝马 / 奥迪 / Tesla 中间讲前两段口播，再坐进驾驶位介绍皮笼、天窗、360、WhatsApp。口型与头部动作由本机 [SadTalker](https://github.com/OpenTalker/SadTalker) 驱动，不是幻灯片。

成片：`samples/host-talking.mp4`

### Windows

1. 安装 [Python 3.10](https://www.python.org/downloads/)（勾选 Add to PATH）、[Git](https://git-scm.com/download/win)、NVIDIA 驱动。FFmpeg 安装脚本会尝试 `winget install Gyan.FFmpeg`。
2. 双击 `run-talking-host.bat`  
   或在项目目录 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_talking_host.ps1
python -m backend.talking_host
```

第一次会下载约 2GB 模型，之后只跑成片。显存 8GB 用 256，10GB 以上自动用 512。

### Linux

```bash
bash scripts/setup_talking_host.sh
PYTHONPATH=. python3 -m backend.talking_host
```

也可 `./start.sh` 打开 http://127.0.0.1:8000 ，点 **本机真人开口（需 NVIDIA 显卡）**。

只检查本机是否就绪：

```bash
PYTHONPATH=. python3 -m backend.talking_host --status
```

---

## 能力

- 8 种视觉风格：电影感、国风水墨、赛博朋克、纪录片、产品广告、梦幻、科技 HUD、绘本童话
- 画幅：16:9 / 9:16 / 1:1 / 21:9
- 默认约 30 秒成片（滑杆 12–60 秒）；旁白拟合进设定时长
- 中英旁白，以及粤语（云龙 / 晓曼 / 晓佳）
- 可编辑分镜旁白后再成片
- 实时进度、分镜预览、MP4 下载
- 粤语豪华车清货广告分镜：`python3 -m backend.campaign` → `samples/car-ad-30s.mp4`

## 运行（普通分镜成片，不需要显卡）

需要 Python 3.10+、Node.js 18+、FFmpeg。

```bash
chmod +x start.sh
./start.sh
```

浏览器打开 http://127.0.0.1:8000

开发模式：

```bash
python3 -m pip install -r requirements.txt
PYTHONPATH=. python3 -m uvicorn backend.main:app --reload --port 8000
# 另开终端
cd frontend && npm install && npm run dev
```

## 流程

1. 写下故事或点子
2. 选择风格、画幅、时长、音色
3. **生成分镜**，按场次改旁白
4. **开始成片**，等待渲染
5. 预览并下载 MP4

引擎会识别山海、城市、星空、产品等母题，自动分配镜头运动（推进 / 拉远 / 横移）。
