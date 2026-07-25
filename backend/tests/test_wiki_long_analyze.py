from app.services.wiki_long_analyze import split_analyze_windows


def test_split_covers_full_text_without_gaps():
    parts = [f"段落{i}。" + ("内容" * 50) for i in range(30)]
    text = "\n\n".join(parts)
    windows = split_analyze_windows(text, target_chars=800, overlap_chars=100)
    assert len(windows) >= 2
    assert windows[0]["start"] == 0
    assert windows[-1]["end"] == len(text)
    covered = 0
    for w in windows:
        assert w["start"] <= covered
        assert w["end"] > w["start"]
        assert w["main"] == text[w["start"] : w["end"]]
        covered = max(covered, w["end"])
    assert covered == len(text)
    assert windows[1]["overlap_before"]
    assert windows[1]["overlap_before"] in windows[0]["main"]


def test_split_short_text_single_window():
    text = "短文档仅一窗。"
    windows = split_analyze_windows(text, target_chars=12000, overlap_chars=1000)
    assert len(windows) == 1
    assert windows[0]["main"] == text
    assert windows[0]["overlap_before"] == ""
