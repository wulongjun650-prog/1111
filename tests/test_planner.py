from backend.planner import plan


def test_plan_chinese_scenes():
    board = plan("仙山云海，少年持灯。", "guofeng", 24, "16:9", "xiaoxiao")
    assert board["language"] == "zh"
    assert 4 <= len(board["scenes"]) <= 6
    assert board["scenes"][0]["narration"]
    assert abs(sum(s["duration"] for s in board["scenes"]) - 24) < 0.2


def test_plan_english_motifs():
    board = plan("A rainy neon city in 2049", "cyberpunk", 15, "9:16", "jenny")
    assert board["language"] == "en"
    assert "city" in board["motifs"]
    assert board["aspect"] == "9:16"
