import { useEffect, useMemo, useState } from "react";

type Meta = {
  styles: Record<string, { label: string; hint: string }>;
  aspects: string[];
  voices: Record<string, string>;
  samples: string[];
};

type Scene = {
  index: number;
  beat: string;
  title: string;
  visual: string;
  motif: string;
  narration: string;
  duration: number;
  motion: string;
};

type Storyboard = {
  title: string;
  prompt: string;
  style: string;
  aspect: string;
  voice: string;
  language: string;
  duration: number;
  seed: number;
  motifs: string[];
  scenes: Scene[];
  logline: string;
};

type Job = {
  id: string;
  status: string;
  progress: number;
  message: string;
  stills?: string[];
  video?: string | null;
};

const DEFAULT_META: Meta = {
  styles: {},
  aspects: ["16:9", "9:16", "1:1", "21:9"],
  voices: {},
  samples: [],
};

export default function App() {
  const [meta, setMeta] = useState<Meta>(DEFAULT_META);
  const [prompt, setPrompt] = useState("一座被云海托起的仙山，少年持灯走入晨雾，寻找失落的星图。");
  const [style, setStyle] = useState("cinematic");
  const [aspect, setAspect] = useState("16:9");
  const [voice, setVoice] = useState("xiaoxiao");
  const [duration, setDuration] = useState(30);
  const [board, setBoard] = useState<Storyboard | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/meta")
      .then((r) => r.json())
      .then(setMeta)
      .catch(() => setError("无法连接成片引擎，请先启动后端。"));
  }, []);

  useEffect(() => {
    if (!job?.id || job.status === "done" || job.status === "error") return;
    const es = new EventSource(`/api/jobs/${job.id}/events`);
    es.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      setJob((prev) =>
        prev
          ? {
              ...prev,
              progress: data.progress ?? prev.progress,
              message: data.message ?? prev.message,
              status: data.status ?? prev.status,
              stills: data.stills ?? prev.stills,
              video: data.video ?? prev.video,
            }
          : prev
      );
      if (data.status === "done" || data.status === "error") {
        setBusy(false);
        es.close();
      }
    };
    es.onerror = () => {
      fetch(`/api/jobs/${job.id}`)
        .then((r) => r.json())
        .then((full) => {
          setJob(full);
          if (full.status === "done" || full.status === "error") setBusy(false);
        });
    };
    return () => es.close();
  }, [job?.id, job?.status]);

  const frameClass = useMemo(() => {
    if (aspect === "9:16") return "preview-frame portrait";
    if (aspect === "1:1") return "preview-frame square";
    return "preview-frame";
  }, [aspect]);

  async function makeBoard() {
    setError("");
    setBusy(true);
    try {
      const res = await fetch("/api/storyboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, style, aspect, voice, duration }),
      });
      if (!res.ok) throw new Error("分镜生成失败");
      setBoard(await res.json());
      setJob(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "分镜失败");
    } finally {
      setBusy(false);
    }
  }

  async function renderVideo() {
    if (!board) return;
    setError("");
    setBusy(true);
    try {
      const res = await fetch("/api/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ storyboard: board }),
      });
      if (!res.ok) throw new Error("成片任务提交失败");
      const data = await res.json();
      setJob({ id: data.job_id, status: "queued", progress: 0, message: "任务已入队", stills: [] });
    } catch (e) {
      setBusy(false);
      setError(e instanceof Error ? e.message : "成片失败");
    }
  }

  function patchScene(index: number, narration: string) {
    if (!board) return;
    setBoard({
      ...board,
      scenes: board.scenes.map((s) => (s.index === index ? { ...s, narration } : s)),
    });
  }

  const preview = job?.video || (job?.stills && job.stills[job.stills.length - 1]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <b>映界 Lumina</b>
          <span>Video Generation Studio</span>
        </div>
        <div className="top-meta">一句话 · 分镜 · 旁白 · 成片</div>
      </header>

      <div className="layout">
        <section className="panel">
          <label className="k">故事</label>
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="写下你想看见的世界…" />
          <div className="samples">
            {meta.samples.map((s) => (
              <button key={s} className={"chip" + (s === prompt ? " active" : "")} onClick={() => setPrompt(s)}>
                {s.slice(0, 16)}…
              </button>
            ))}
          </div>

          <label className="k">风格</label>
          <div className="grid">
            {Object.entries(meta.styles).map(([key, val]) => (
              <button key={key} className={"tile" + (style === key ? " active" : "")} onClick={() => setStyle(key)}>
                <b>{val.label}</b>
                <em>{val.hint}</em>
              </button>
            ))}
          </div>

          <div className="row">
            <div>
              <label className="k">画幅 {aspect}</label>
              <select value={aspect} onChange={(e) => setAspect(e.target.value)}>
                {meta.aspects.map((a) => (
                  <option key={a}>{a}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="k">旁白</label>
              <select value={voice} onChange={(e) => setVoice(e.target.value)}>
                {Object.entries(meta.voices).map(([k, label]) => (
                  <option key={k} value={k}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <label className="k">时长 {duration}s</label>
          <input type="range" min={12} max={60} value={duration} onChange={(e) => setDuration(Number(e.target.value))} />

          <div className="actions">
            <button className="btn ghost" disabled={busy || prompt.trim().length < 2} onClick={makeBoard}>
              生成分镜
            </button>
            <button className="btn primary" disabled={busy || !board} onClick={renderVideo}>
              开始成片
            </button>
            <button
              className="btn ghost"
              disabled={busy}
              onClick={async () => {
                setError("");
                setBusy(true);
                try {
                  const res = await fetch("/api/campaigns/car-ad", { method: "POST" });
                  if (!res.ok) throw new Error("粤语车广告渲染失败");
                  const data = await res.json();
                  setJob({
                    id: "car-ad",
                    status: "done",
                    progress: 100,
                    message: "粤语清货广告 30s 已成片",
                    video: data.video,
                  });
                } catch (e) {
                  setError(e instanceof Error ? e.message : "广告成片失败");
                } finally {
                  setBusy(false);
                }
              }}
            >
              渲染粤语车广告 30s
            </button>
          </div>
          {error && <p className="hint" style={{ color: "var(--danger)" }}>{error}</p>}
          {job && (
            <div className="progress">
              <div className="bar">
                <i style={{ width: `${job.progress}%` }} />
              </div>
              <p>
                {job.progress}% · {job.message}
              </p>
            </div>
          )}
        </section>

        <section className="panel">
          <div className={frameClass}>
            {job?.video ? (
              <video src={job.video} controls autoPlay />
            ) : preview ? (
              <img src={preview} alt="preview" />
            ) : (
              <div className="placeholder">
                <strong>{board?.title || "尚未成片"}</strong>
                先写故事，再出分镜，最后渲染成片。
              </div>
            )}
          </div>

          {job?.video && (
            <p className="hint">
              <a href={job.video} download style={{ color: "var(--gold)" }}>
                下载 MP4
              </a>
            </p>
          )}

          {board && (
            <>
              <p className="hint">
                {board.title} · {board.scenes.length} 场 · {board.motifs.join(" / ")}
              </p>
              <div className="strip">
                {board.scenes.map((scene) => (
                  <article className="card scene-edit" key={scene.index}>
                    {job?.stills?.[scene.index] && <img src={job.stills[scene.index]} alt={scene.beat} />}
                    <div className="meta">
                      <b>
                        {String(scene.index + 1).padStart(2, "0")} {scene.beat}
                      </b>
                      <span>
                        {scene.motif} · {scene.duration}s
                      </span>
                      <textarea value={scene.narration} onChange={(e) => patchScene(scene.index, e.target.value)} />
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
