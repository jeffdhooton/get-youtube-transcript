import wave

import pytest

from sources import CaptureError, transcribe_local, preflight_local


def test_digital_silence_cannot_become_hallucinated_speech(tmp_path):
    pytest.importorskip("mlx_whisper")
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * 16000)
    with pytest.raises(CaptureError, match="silence"):
        transcribe_local(path)


def test_missing_ffmpeg_fails_before_audio_download(tmp_path, monkeypatch):
    monkeypatch.setattr("sources.platform.system", lambda: "Darwin")
    monkeypatch.setattr("sources.platform.machine", lambda: "arm64")
    monkeypatch.setattr("sources.shutil.which", lambda _: None)
    with pytest.raises(CaptureError, match="FFmpeg"):
        preflight_local(tmp_path)


@pytest.mark.parametrize("hint,want", [("pt-BR", "pt"), ("en-US", "en"), ("zh-Hans", "zh"), ("EN", "en"), (None, None)])
def test_whisper_language_normalization(hint, want):
    from sources import whisper_language
    assert whisper_language(hint) == want


def test_unsupported_language_fails_preflight(tmp_path):
    with pytest.raises(CaptureError, match="language"):
        preflight_local(tmp_path, "zzz")


def test_incompatible_architecture_fails_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr("sources.platform.machine", lambda: "x86_64")
    with pytest.raises(CaptureError, match="Apple Silicon"):
        preflight_local(tmp_path)


def test_model_failure_preserves_existing_note_and_cleans_audio(tmp_path):
    from test_capture import FakeSource
    from capture import capture
    from pathlib import Path
    result = capture("https://youtu.be/abcdefghijk", tmp_path, source=FakeSource())
    path = Path(result["path"])
    original = path.read_bytes()
    class BrokenModel(FakeSource):
        def transcribe(self, path, language):
            raise RuntimeError("Model load failed")
    source = BrokenModel(captions=False)
    with pytest.raises(RuntimeError):
        capture("https://youtu.be/abcdefghijk", tmp_path, source=source, refresh=True)
    assert path.read_bytes() == original
    assert not source.audio_path.exists()
