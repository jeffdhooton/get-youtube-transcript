import json
from pathlib import Path
import shutil
import subprocess

import pytest

from capture import capture
from sources import AccessDenied, CaptureError
from test_capture import FakeSource
from transcript import read_metadata

URL = "https://youtu.be/abcdefghijk"


@pytest.fixture
def clip(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for the real frame extraction tests")
    path = tmp_path / "synthetic.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc2=size=160x90:rate=4:duration=3", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", str(path),
    ], check=True, capture_output=True)
    return path


@pytest.fixture
def saved(tmp_path):
    return Path(capture(URL, tmp_path / "vault", source=FakeSource())["path"])


class VideoSource(FakeSource):
    def __init__(self, clip):
        super().__init__()
        self.clip = clip
        self.temporary = None

    def download_video(self, directory):
        self.temporary = directory / "video.mp4"
        shutil.copyfile(self.clip, self.temporary)
        return self.temporary


def test_fractional_frame_timestamps_are_actual_decoded_times(clip, tmp_path):
    from frames import extract_frames
    frames = extract_frames(clip, tmp_path / "frames", [0.1, 1.1, 2.5], resolution=320)
    assert [f["requested_timestamp_seconds"] for f in frames] == [0.1, 1.1, 2.5]
    assert [f["timestamp_seconds"] for f in frames] == pytest.approx([0.25, 1.25, 2.5])
    assert all(Path(f["path"]).read_bytes().startswith(b"\xff\xd8") for f in frames)


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "1:99", "1,,2", "", "1:2:3:4"])
def test_bad_frame_requests_rejected(value):
    from frames import parse_timestamps
    with pytest.raises(ValueError):
        parse_timestamps(value)


def test_frame_requests_normalize_and_enforce_budget():
    from frames import parse_timestamps
    assert parse_timestamps("01:02.5,0,62.5,1:00:00") == [0, 62.5, 3600]
    with pytest.raises(ValueError, match="budget"):
        parse_timestamps("0,1,2", max_frames=2)
    with pytest.raises(ValueError, match="duration"):
        parse_timestamps("10", duration=10)


def test_visual_capture_reuses_cache_and_preserves_transcript(saved, clip):
    from enrich import capture_visuals
    before = saved.read_bytes()
    source = VideoSource(clip)
    result = capture_visuals(URL, saved.parent, "0.1,1.1", source=source)
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert result["status"] == "saved"
    assert manifest["video_id"] == "abcdefghijk"
    assert manifest["frames"][1]["timestamp_seconds"] == pytest.approx(1.25)
    assert saved.read_bytes() == before
    assert not source.temporary.exists()
    class Offline:
        def metadata(self, url):
            raise AssertionError("Cached images must not access the network")
    again = capture_visuals(URL, saved.parent, "1.1,0.1", source=Offline())
    assert again["status"] == "existing"
    assert again["manifest_path"] == result["manifest_path"]


def test_access_refusal_leaves_no_visual_success_or_changed_transcript(saved):
    from enrich import capture_visuals
    before = saved.read_bytes()
    class Denied:
        def metadata(self, url):
            raise AccessDenied("403")
    with pytest.raises(AccessDenied):
        capture_visuals(URL, saved.parent, "1", source=Denied())
    assert saved.read_bytes() == before
    assert not list(saved.parent.rglob("manifest.json"))
    logs = [json.loads(p.read_text()) for p in (saved.parent / "_runs").glob("visuals-*.json")]
    assert logs[-1]["status"] == "stopped"


def test_failed_frame_extraction_cleans_temp_and_keeps_prior_evidence(saved, clip):
    from enrich import capture_visuals
    initial = capture_visuals(URL, saved.parent, "1", source=VideoSource(clip))
    source = VideoSource(clip)
    # The source metadata says 10s, but the downloaded fixture ends at 3s.
    with pytest.raises(CaptureError):
        capture_visuals(URL, saved.parent, "9", source=source)
    assert not source.temporary.exists()
    assert Path(initial["manifest_path"]).exists()
    assert len(list(saved.parent.rglob("manifest.json"))) == 1


def payload():
    return {
        "intent": "Understand the dashboard demo",
        "summary": ["The presenter introduces a dashboard."],
        "key_moments": [{"timestamp_seconds": 1, "text": "The dashboard is introduced.", "evidence": "transcript"}],
        "entities": ["Dashboard"], "concepts": ["Overview before detail"],
        "limitations": ["Only selected moments were inspected."],
    }


def test_analysis_saves_separate_linked_note_and_preserves_edits(saved):
    from enrich import save_analysis
    before = saved.read_bytes()
    result = save_analysis(URL, saved.parent, payload())
    note = Path(result["path"])
    meta = read_metadata(note)
    assert meta["type"] == "youtube_analysis"
    assert meta["source_file"] == read_metadata(saved)["source_file"]
    assert "youtube.com/watch?v=abcdefghijk&t=1s" in note.read_text()
    assert "../" + saved.name in note.read_text()
    assert "agent_analysis" == meta["body_source"]
    note.write_text(note.read_text() + "\nMy edits.\n")
    again = save_analysis(URL, saved.parent, payload())
    assert again["status"] == "existing"
    assert "My edits." in note.read_text()
    assert saved.read_bytes() == before


def test_analysis_embeds_only_evidenced_visual_claims(saved, clip):
    from enrich import capture_visuals, save_analysis
    visuals = capture_visuals(URL, saved.parent, "1.1", source=VideoSource(clip))
    data = payload()
    data["key_moments"] = [{"timestamp_seconds": 1.25, "text": "A test pattern is visible.",
                            "evidence": "visual", "frame_id": "frame-001"}]
    with pytest.raises(ValueError, match="frame"):
        save_analysis(URL, saved.parent, data)
    result = save_analysis(URL, saved.parent, data, visuals_path=visuals["manifest_path"])
    assert "![" in Path(result["path"]).read_text()
    assert "frame-001.jpg" in Path(result["path"]).read_text()


def test_analysis_rejects_out_of_range_claim_and_empty_summary(saved):
    from enrich import save_analysis
    for data in [{**payload(), "summary": []},
                 {**payload(), "key_moments": [{"timestamp_seconds": 50, "text": "Bad time", "evidence": "transcript"}]}]:
        with pytest.raises(ValueError):
            save_analysis(URL, saved.parent, data)
    assert not list((saved.parent / "_analysis").glob("*.md"))


def test_source_refresh_invalidates_analysis_and_visual_pairing(saved, clip):
    from enrich import capture_visuals, save_analysis
    visuals = capture_visuals(URL, saved.parent, "1", source=VideoSource(clip))
    result = save_analysis(URL, saved.parent, payload())
    capture(URL, saved.parent, source=FakeSource(), refresh=True)
    with pytest.raises(CaptureError, match="refresh"):
        save_analysis(URL, saved.parent, payload())
    with pytest.raises(CaptureError, match="source"):
        save_analysis(URL, saved.parent, payload(), visuals_path=visuals["manifest_path"], refresh=True)
    assert Path(result["path"]).exists()


def test_missing_transcript_fails_without_network(tmp_path):
    from enrich import capture_visuals
    with pytest.raises(CaptureError, match="capture"):
        capture_visuals(URL, tmp_path, "1")


def test_corrupt_source_sidecar_is_rejected(saved):
    from enrich import save_analysis
    sidecar = saved.parent / read_metadata(saved)["source_file"]
    data = json.loads(sidecar.read_text())
    data["segments"][0]["text"] = "Changed after capture"
    sidecar.write_text(json.dumps(data))
    with pytest.raises(CaptureError, match="hash"):
        save_analysis(URL, saved.parent, payload())


def test_analyze_cli_emits_json(saved, tmp_path, capsys):
    from enrich import main
    source = tmp_path / "analysis.json"
    source.write_text(json.dumps(payload()))
    assert main(["analyze", URL, "--output-dir", str(saved.parent), "--input", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["path"]).is_file()


def test_latest_refreshed_visuals_win_even_when_uuid_sorts_earlier(saved, clip, monkeypatch):
    from types import SimpleNamespace
    import enrich
    identifiers = iter(["f" * 32, "0" * 32])
    monkeypatch.setattr(enrich.uuid, "uuid4", lambda: SimpleNamespace(hex=next(identifiers)))
    old = enrich.capture_visuals(URL, saved.parent, "1", source=VideoSource(clip))
    new = enrich.capture_visuals(URL, saved.parent, "1", refresh=True, source=VideoSource(clip))
    again = enrich.capture_visuals(URL, saved.parent, "1", source=object())
    assert again["manifest_path"] == new["manifest_path"]
    assert Path(old["manifest_path"]).exists()


def test_analysis_body_links_to_immutable_source_evidence(saved):
    from enrich import save_analysis
    result = save_analysis(URL, saved.parent, payload())
    sidecar = read_metadata(saved)["source_file"]
    body = Path(result["path"]).read_text().split("---", 2)[2]
    assert f"](../{sidecar})" in body


def test_visual_cancellation_removes_download_and_unpublished_frames(saved, clip, monkeypatch):
    import enrich
    source = VideoSource(clip)
    def cancel(media, stage, *args, **kwargs):
        (stage / "incomplete.jpg").write_bytes(b"partial")
        raise KeyboardInterrupt()
    monkeypatch.setattr(enrich, "extract_frames", cancel)
    with pytest.raises(KeyboardInterrupt):
        enrich.capture_visuals(URL, saved.parent, "1", source=source)
    assert not source.temporary.exists()
    assert not list(saved.parent.rglob("*.jpg"))
    assert not list(saved.parent.rglob("manifest.json"))


def test_corrupt_cached_screenshot_is_not_silently_reused(saved, clip):
    from enrich import capture_visuals, save_analysis
    result = capture_visuals(URL, saved.parent, "1", source=VideoSource(clip))
    Path(result["frames"][0]["absolute_path"]).write_bytes(b"changed")
    with pytest.raises(CaptureError, match="hash"):
        save_analysis(URL, saved.parent, payload(), visuals_path=result["manifest_path"])
    with pytest.raises(CaptureError, match="refresh"):
        capture_visuals(URL, saved.parent, "1", source=object())


def test_failed_analysis_refresh_keeps_prior_note_and_sidecar(saved, monkeypatch):
    import enrich
    result = enrich.save_analysis(URL, saved.parent, payload())
    note = Path(result["path"])
    before = note.read_bytes()
    old_data = saved.parent / read_metadata(note)["analysis_file"]
    def fail(*args):
        raise OSError("Disk full")
    monkeypatch.setattr(enrich, "_atomic_note", fail)
    with pytest.raises(OSError):
        enrich.save_analysis(URL, saved.parent, payload(), refresh=True)
    assert note.read_bytes() == before
    assert old_data.exists()
