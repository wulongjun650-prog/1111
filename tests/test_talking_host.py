from pathlib import Path

from backend.config import ROOT
from backend.host_cut import ASSETS, PHRASES
from backend.talking_host import TAKES, diagnose, setup_commands


def test_takes_cover_all_phrases():
    covered: list[int] = []
    for take in TAKES:
        covered.extend(range(take["start"], take["end"]))
    assert covered == list(range(len(PHRASES)))
    assert TAKES[0]["id"] == "stand"
    assert TAKES[0]["end"] == 9
    assert TAKES[1]["id"] == "sit"


def test_take_images_exist():
    for take in TAKES:
        path = ASSETS / take["image"]
        assert path.exists(), path
        assert path.stat().st_size > 100_000


def test_diagnose_reports_gpu_gap_on_cloud():
    info = diagnose()
    assert "ok" in info
    assert info["setup"] == setup_commands()
    assert isinstance(info["message"], str)
    assert info["ok"] is False
    assert info["sadtalker"] is None or not Path(info["sadtalker"]).joinpath("missing").exists()


def test_readme_leads_with_local_gpu():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "NVIDIA" in text
    assert "run-talking-host.bat" in text
    assert "python -m backend.talking_host" in text
