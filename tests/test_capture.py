import json
from pathlib import Path

import pytest

import capture as c
from transcript import Segment, Transcript, Video, UnusableCaptions
from sources import AccessDenied, CaptureError, choose_caption, choose_audio, GuardedYoutubeDL, GuardedYoutubeIE, RequestStopped


@pytest.mark.parametrize("url", ["https://youtu.be/abcdefghijk?t=2", "https://www.youtube.com/watch?v=abcdefghijk&list=PLx", "https://m.youtube.com/shorts/abcdefghijk", "https://youtube.com/live/abcdefghijk", "https://youtube.com/embed/abcdefghijk"])
def test_video_url_forms(url):
    assert c.video_id(url) == "abcdefghijk"


@pytest.mark.parametrize("url", ["https://youtube.com.evil/watch?v=abcdefghijk", "https://youtube.com/playlist?list=x", "https://youtube.com/@user", "file:///abcdefghijk", "https://youtube.com/watch?v=abc", "https://user:pass@youtube.com/watch?v=abcdefghijk", "https://youtu.be:123/abcdefghijk"])
def test_invalid_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        c.video_id(url)


def track(lang="en", fmt="json3", translated=False):
    return {"ext": fmt, "url": f"https://www.youtube.com/api/timedtext?lang={lang}" + ("&tlang=de" if translated else "")}


def test_manual_track_preferred_and_translation_excluded():
    info = {"language": "en", "subtitles": {"en": [track()]}, "automatic_captions": {"en-orig": [track()], "de": [track(translated=True)]}}
    assert choose_caption(info, None)[0:2] == ("en", "youtube_manual_captions")
    with pytest.raises(CaptureError):
        choose_caption(info, "de")


def test_original_asr_language_resolves_multiple_manual_languages():
    info = {"subtitles": {"en": [track()], "fr": [track("fr")]}, "automatic_captions": {"en-orig": [track()]}}
    assert choose_caption(info, None)[0] == "en"


def test_ambiguous_manual_languages_do_not_pick_arbitrarily():
    assert choose_caption({"subtitles": {"en": [track()], "fr": [track("fr")]}}, None) is None


def test_single_manual_language_is_usable():
    assert choose_caption({"subtitles": {"en": [track()]}}, None)[0] == "en"


def audio(lang, original=False):
    return {"format_id": lang, "url": "https://example.test/audio", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "aac", "language": lang, "language_preference": 10 if original else -1, "abr": 128}


def test_original_audio_beats_dub_and_rejects_wrong_language():
    info = {"formats": [audio("fr"), audio("en", True)]}
    assert choose_audio(info, None)["language"] == "en"
    with pytest.raises(CaptureError):
        choose_audio(info, "fr")


def test_ambiguous_audio_languages_fail():
    with pytest.raises(CaptureError):
        choose_audio({"formats": [audio("en"), audio("fr")]}, None)


class FakeSource:
    def __init__(self, captions=True, failure=None):
        self.has_captions, self.failure, self.audio_path = captions, failure, None
    def metadata(self, url):
        return Video("abcdefghijk", "Fixture", channel="Local fixture", duration=10)
    def captions(self, language):
        if self.failure:
            raise self.failure
        if self.has_captions:
            return Transcript([Segment(0, 2, "Hello fixture.")], "en", "youtube_manual_captions")
    def preflight(self, directory, language=None):
        pass
    def download_audio(self, directory, language):
        self.audio_path = directory / "audio.m4a"
        self.audio_path.write_bytes(b"local synthetic placeholder")
        return self.audio_path
    def transcribe(self, path, language):
        return Transcript([Segment(0, 2, "Local words.")], "en", "local_whisper", model="fixture")
    def close(self):
        pass


def test_cli_caption_capture_and_repeat_preserve_edits(tmp_path, capsys):
    assert c.main(["https://youtu.be/abcdefghijk", "--output-dir", str(tmp_path)], source=FakeSource()) == 0
    result = json.loads(capsys.readouterr().out)
    path = Path(result["path"])
    assert result["body_source"] == "youtube_manual_captions"
    path.write_text(path.read_text() + "My notes")
    assert c.main(["https://youtu.be/abcdefghijk", "--output-dir", str(tmp_path)], source=FakeSource(failure=AssertionError("no network"))) == 0
    assert "My notes" in path.read_text()
    assert json.loads(capsys.readouterr().out)["status"] == "existing"


@pytest.mark.parametrize("failure", [None, UnusableCaptions("empty")])
def test_local_fallback_saves_note_and_removes_audio(tmp_path, failure):
    source = FakeSource(captions=False, failure=failure)
    result = c.capture("https://youtu.be/abcdefghijk", tmp_path, source=source)
    assert result["body_source"] == "local_whisper"
    assert "Local words." in Path(result["path"]).read_text()
    assert source.audio_path is not None and not source.audio_path.exists()


def test_access_refusal_never_falls_back(tmp_path):
    source = FakeSource(failure=AccessDenied("403"))
    with pytest.raises(AccessDenied):
        c.capture("https://youtu.be/abcdefghijk", tmp_path, source=source)
    assert source.audio_path is None
    assert not list(tmp_path.glob("*.md"))
    log = json.loads(next((tmp_path / "_runs").glob("*.json")).read_text())
    assert log["status"] == "stopped"


def test_forced_transcription_ignores_caption_availability(tmp_path):
    result = c.capture("https://youtu.be/abcdefghijk", tmp_path, source=FakeSource(), transcribe=True)
    assert result["body_source"] == "local_whisper"


def test_changed_language_requires_refresh(tmp_path):
    c.capture("https://youtu.be/abcdefghijk", tmp_path, source=FakeSource())
    with pytest.raises(CaptureError, match="refresh"):
        c.capture("https://youtu.be/abcdefghijk", tmp_path, source=FakeSource(), language="fr")


def test_cancel_removes_audio_and_preserves_existing_note(tmp_path):
    result = c.capture("https://youtu.be/abcdefghijk", tmp_path, source=FakeSource())
    original = Path(result["path"]).read_bytes()
    class CancelSource(FakeSource):
        def transcribe(self, path, language):
            raise KeyboardInterrupt()
    source = CancelSource(captions=False)
    with pytest.raises(KeyboardInterrupt):
        c.capture("https://youtu.be/abcdefghijk", tmp_path, source=source, refresh=True)
    assert not source.audio_path.exists()
    assert Path(result["path"]).read_bytes() == original


def test_playability_denial_interrupts_extractor_before_client_fallback():
    ie = GuardedYoutubeIE()
    with pytest.raises(RequestStopped):
        ie._parse_json('{"playabilityStatus":{"status":"LOGIN_REQUIRED","reason":"Sign in"}}', "abcdefghijk")


def test_http_denial_is_sticky_and_not_caught_as_ordinary_error(monkeypatch):
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.exceptions import HTTPError
    from yt_dlp.networking import Response
    from io import BytesIO
    count = 0
    def denied(self, request):
        nonlocal count
        count += 1
        raise HTTPError(Response(BytesIO(b"denied"), "https://www.youtube.com/", {}, 403))
    monkeypatch.setattr(YoutubeDL, "urlopen", denied)
    with GuardedYoutubeDL({}, auto_init=False) as ydl:
        for _ in range(2):
            with pytest.raises(RequestStopped):
                ydl.urlopen("https://www.youtube.com/")
    assert count == 1


def test_upfront_primary_client_requests_player_without_client_fallback(monkeypatch):
    from sources import Sources
    source = Sources()
    ie = source.ydl.get_info_extractor("Youtube")
    ie.initialize()
    initial = {"videoDetails": {"videoId": "abcdefghijk"}, "playabilityStatus": {"status": "OK"}}
    monkeypatch.setattr(ie, "_search_json", lambda *a, **kw: initial)
    monkeypatch.setattr(ie, "_extract_player_url", lambda *a, **kw: "https://www.youtube.com/s/player/example/base.js")
    monkeypatch.setattr(ie, "_download_ytcfg", lambda *a, **kw: {})
    monkeypatch.setattr(ie, "fetch_po_token", lambda *a, **kw: None)
    requests = []
    def player(client, vid, **kwargs):
        requests.append(client)
        return {**initial, "streamingData": {"adaptiveFormats": [{"itag": 140}]}}
    monkeypatch.setattr(ie, "_extract_player_response", player)
    clients = ie._get_requested_clients("https://www.youtube.com/watch?v=abcdefghijk", {}, False)
    responses, _ = ie._extract_player_responses(clients, "abcdefghijk", "fixture", "web", {}, False)
    assert requests == ["visionos"]
    assert any((r.get("streamingData") or {}).get("adaptiveFormats") for r in responses)
    source.close()


def test_transient_network_failure_cannot_be_swallowed_into_audio_fallback(monkeypatch):
    from sources import RequestFailed
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.exceptions import TransportError
    def broken(self, request):
        raise TransportError("Connection lost")
    monkeypatch.setattr(YoutubeDL, "urlopen", broken)
    with GuardedYoutubeDL({}, auto_init=False) as ydl:
        with pytest.raises(RequestFailed):
            ydl.urlopen("https://www.youtube.com/")


def test_empty_asr_result_is_failure_and_cleans_audio(tmp_path):
    class EmptySource(FakeSource):
        def transcribe(self, path, language):
            return Transcript([], "en", "local_whisper")
    source = EmptySource(captions=False)
    with pytest.raises(ValueError):
        c.capture("https://youtu.be/abcdefghijk", tmp_path, source=source)
    assert not list(tmp_path.glob("*.md"))
    assert not source.audio_path.exists()


def test_uploaded_translation_cannot_override_known_original_language():
    info = {"subtitles": {"fr": [track("fr")]}, "automatic_captions": {"en-orig": [track()]}}
    with pytest.raises(CaptureError, match="original"):
        choose_caption(info, "fr")
