"""Download SadTalker checkpoints into vendor/SadTalker."""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "vendor" / "SadTalker"

CHECKPOINTS = [
    (
        "checkpoints/mapping_00109-model.pth.tar",
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/mapping_00109-model.pth.tar",
    ),
    (
        "checkpoints/mapping_00229-model.pth.tar",
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/mapping_00229-model.pth.tar",
    ),
    (
        "checkpoints/SadTalker_V0.0.2_256.safetensors",
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/SadTalker_V0.0.2_256.safetensors",
    ),
    (
        "checkpoints/SadTalker_V0.0.2_512.safetensors",
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/SadTalker_V0.0.2_512.safetensors",
    ),
]

GFPGAN = [
    (
        "gfpgan/weights/alignment_WFLW_4HG.pth",
        "https://github.com/xinntao/facexlib/releases/download/v0.1.0/alignment_WFLW_4HG.pth",
    ),
    (
        "gfpgan/weights/detection_Resnet50_Final.pth",
        "https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth",
    ),
    (
        "gfpgan/weights/GFPGANv1.4.pth",
        "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth",
    ),
    (
        "gfpgan/weights/parsing_parsenet.pth",
        "https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth",
    ),
]


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"skip {dest.name} ({dest.stat().st_size} bytes)")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"download {dest.name}")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    print(f"  -> {dest} ({dest.stat().st_size} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()
    root: Path = args.dir
    root.mkdir(parents=True, exist_ok=True)
    failed = []
    for rel, url in CHECKPOINTS + GFPGAN:
        dest = root / rel
        try:
            _download(url, dest)
        except Exception as exc:
            print(f"FAIL {rel}: {exc}", file=sys.stderr)
            failed.append(rel)
    if failed:
        print("部分权重下载失败。GitHub 打不开时请开代理后重跑安装脚本。", file=sys.stderr)
        raise SystemExit(1)
    print("SadTalker 权重已就绪")


if __name__ == "__main__":
    main()
