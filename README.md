# 映界 Lumina

本地可运行的视频生成工作室：一句话 → 分镜 → 旁白 → 运镜成片。

不依赖付费视频模型。画面由电影级程序化光影引擎绘制，旁白使用 Microsoft Edge 神经网络语音，成片由 FFmpeg 完成 Ken Burns 运镜、配乐与字幕烧录。

## 能力

- 8 种视觉风格：电影感、国风水墨、赛博朋克、纪录片、产品广告、梦幻、科技 HUD、绘本童话
- 画幅：16:9 / 9:16 / 1:1 / 21:9
- 默认约 30 秒成片（滑杆 12–60 秒）；旁白拟合进设定时长
- 中英旁白，以及粤语（云龙 / 晓曼 / 晓佳）
- 可编辑分镜旁白后再成片
- 实时进度、分镜预览、MP4 下载
- 粤语豪华车清货广告分镜：`python3 -m backend.campaign` → `samples/car-ad-30s.mp4`

## 运行

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
