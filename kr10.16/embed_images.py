#!/usr/bin/env python3
"""Inline static/picture assets into index.html as data: URIs."""
import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIME = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


def data_uri(path: Path) -> str:
    ext = path.suffix.lower()
    mime = MIME.get(ext)
    if not mime:
        raise ValueError(f"Unsupported image type: {path}")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def main() -> None:
    template = (ROOT / "index.source.html").read_text(encoding="utf-8")
    if not (ROOT / "index.source.html").exists():
        # First run: treat current external-ref HTML as template if present
        ext_html = ROOT / "index.html"
        if "static/picture/" in ext_html.read_text(encoding="utf-8"):
            template = ext_html.read_text(encoding="utf-8")
        else:
            raise SystemExit("Need index.source.html with static/picture/ src paths")

    def repl(match: re.Match[str]) -> str:
        rel = match.group(1)
        path = ROOT / rel
        return f'src="{data_uri(path)}"'

    out = re.sub(r'src="(static/picture/[^"]+)"', repl, template)
    (ROOT / "index.html").write_text(out, encoding="utf-8")
    print("Wrote", ROOT / "index.html", f"({len(out)} bytes)")


if __name__ == "__main__":
    main()
