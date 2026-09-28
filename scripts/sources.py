"""Public YouTube acquisition and local Apple Silicon transcription."""
from __future__ import annotations

from contextlib import redirect_stdout
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re
import shutil
import sys
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.networking.exceptions import HTTPError

from transcript import Segment, Transcript, Video, UnusableCaptions, parse_captions, validate_segments

MODEL = "mlx-community/whisper-large-v3-turbo"


class CaptureError(Exception):
    pass


class AccessDenied(CaptureError):
    pass


class RequestStopped(BaseException):
    """Escape yt-dlp's catch-and-continue handlers; caught at our API boundary."""


class RequestFailed(BaseException):
    """Prevent an ordinary network failure from being mistaken for no captions."""


def progress(message):
    # Download URLs can carry tokens. Never retain them in messages or logs.
    print(re.sub(r"https?://\S+", "[URL]", str(message)), file=sys.stderr, flush=True)


def check_challenge(text):
    if re.search(r'g-recaptcha|<title>\s*(?:Sorry|Just a moment)|<form[^>]+action=["\'][^"\']*/sorry/', text, re.I):
        raise RequestStopped("Challenge page received; stopped without retrying")


class GuardedYoutubeDL(YoutubeDL):
    _stopped = False

    def urlopen(self, req):
        if self._stopped:
            raise RequestStopped("This run already encountered an access refusal")
        try:
            result = super().urlopen(req)
        except HTTPError as exc:
            if exc.status in (401, 403, 429):
                self._stopped = True
                raise RequestStopped(f"HTTP {exc.status}; stopped without retrying") from None
            raise RequestFailed(f"HTTP {exc.status}; request failed without retrying") from None
        except Exception as exc:
            raise RequestFailed(f"Network request failed ({type(exc).__name__}); no fallback attempted") from None
        if "/sorry/" in result.url or urlsplit(result.url).hostname == "consent.youtube.com":
            self._stopped = True
            result.close()
            raise RequestStopped("Access/consent challenge; stopped without retrying")
        return result


class GuardedYoutubeIE(YoutubeIE):
    # Keep yt-dlp's extractor key so embedded result metadata remains compatible.
    @classmethod
    def ie_key(cls):
        return "Youtube"

    def _parse_json(self, *args, **kwargs):
        result = super()._parse_json(*args, **kwargs)
        status = result.get("playabilityStatus", {}).get("status") if isinstance(result, dict) else None
        if status and status != "OK":
            if self._downloader:
                self._downloader._stopped = True
            raise RequestStopped(f"YouTube playability status {status}; no alternate client attempted")
        return result

    def _webpage_read_content(self, *args, **kwargs):
        result = super()._webpage_read_content(*args, **kwargs)
        check_challenge(result)
        return result


class Logger:
    def debug(self, msg):
        if not str(msg).startswith("[debug]"):
            progress(msg)
    info = debug
    warning = debug
    error = debug


def base_language(value):
    return (value or "").removesuffix("-orig").lower()


def language_matches(actual, requested):
    a, r = base_language(actual), base_language(requested)
    return a == r or (("-" not in a or "-" not in r) and a.split("-")[0] == r.split("-")[0])


def original_language(info):
    original = {base_language(k) for k in (info.get("automatic_captions") or {}) if k.endswith("-orig")}
    original.update(base_language(f["language"]) for f in info.get("formats", [])
                    if f.get("language") and f.get("language_preference") == 10)
    if not original and info.get("language"):
        original.add(base_language(info["language"]))
    return next(iter(original)) if len(original) == 1 else None


def check_source_language(info, language):
    original = original_language(info)
    if language and original and not language_matches(original, language):
        raise CaptureError(f"Requested language {language} differs from known original language {original}; translation is not supported")
    return original


def choose_caption(info, language):
    original = check_source_language(info, language)
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    candidates = []
    for source, tracks in [("youtube_manual_captions", manual), ("youtube_auto_captions", automatic)]:
        for code, formats in tracks.items():
            if code == "live_chat":
                continue
            usable = [f for f in formats if f.get("ext") in ("json3", "vtt") and f.get("url")
                      and not parse_qs(urlsplit(f["url"]).query).get("tlang")]
            if usable:
                best = min(usable, key=lambda f: (f["ext"] != "json3"))
                candidates.append((base_language(code), source, best))
    if not language:
        languages = {original} if original else {c[0] for c in candidates}
        if len(languages) != 1:
            return None
        language = next(iter(languages))
    matches = [c for c in candidates if language_matches(c[0], language)]
    return matches[0] if matches else None


def choose_audio(info, language):
    original = check_source_language(info, language)
    formats = [f for f in info.get("formats", []) if f.get("vcodec") == "none"
               and f.get("acodec") not in (None, "none") and f.get("url")
               and f.get("protocol") in ("https", "http")]
    originals = [f for f in formats if f.get("language_preference") == 10]
    if originals:
        formats = originals
    else:
        formats = [f for f in formats if not re.search(r"dubbed|descriptive|audio description", f.get("format_note") or "", re.I)]
        languages = {base_language(f["language"]) for f in formats if f.get("language")}
        if len(languages) > 1 or (languages and any(not f.get("language") for f in formats)):
            raise CaptureError("Original audio language is ambiguous; cannot safely choose a dubbed track")
    if original:
        formats = [f for f in formats if not f.get("language") or language_matches(f["language"], original)]
    if language:
        formats = [f for f in formats if not f.get("language") or language_matches(f["language"], language)]
    if not formats:
        raise CaptureError("No usable original audio-only format in the requested language")
    return max(formats, key=lambda f: (f.get("abr") or f.get("tbr") or 0))


def choose_video(info):
    # One direct video stream; no external downloader, stream merging or audio
    # language selection is needed to inspect pixels. Bound download resolution.
    formats = [f for f in info.get("formats", []) if f.get("vcodec") not in (None, "none")
               and f.get("url") and f.get("protocol") in ("https", "http")
               and not f.get("has_drm") and 0 < (f.get("height") or 0) <= 1080
               and f.get("ext") in ("mp4", "webm")]
    if not formats:
        raise CaptureError("No usable direct video format at or below 1080p")
    return max(formats, key=lambda f: (f.get("height") or 0,
                                      f.get("acodec") == "none", f.get("tbr") or 0))


class Sources:
    def __init__(self):
        runtime = next((name for name in ("deno", "node") if shutil.which(name)), None)
        opts = {"noplaylist": True, "quiet": True, "logger": Logger(), "socket_timeout": 30,
                "retries": 0, "fragment_retries": 0, "extractor_retries": 0, "file_access_retries": 0,
                "concurrent_fragment_downloads": 1, "skip_unavailable_fragments": False,
                "proxy": "", "cachedir": False, "ignore_no_formats_error": True,
                # Choose yt-dlp's documented primary public client UP FRONT.
                # Do not cycle clients after missing media or an access refusal.
                "extractor_args": {"youtube": {"player_client": ["visionos"],
                                                "skip": ["translated_subs", "hls", "dash"]}},
                "js_runtimes": {runtime: {"path": shutil.which(runtime)}} if runtime else {}}
        self.ydl = GuardedYoutubeDL(opts, auto_init=False)
        self.ydl.add_info_extractor(GuardedYoutubeIE())
        self.info = {}

    def metadata(self, url):
        progress("Fetching video metadata and caption inventory…")
        try:
            self.info = self.ydl.extract_info(url, download=False)
        except RequestStopped as exc:
            raise AccessDenied(str(exc)) from None
        except RequestFailed as exc:
            raise CaptureError(str(exc)) from None
        if not self.info or self.info.get("_type", "video") != "video":
            raise CaptureError("Expected a single available video")
        if self.info.get("is_live") or self.info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            raise CaptureError("Active, upcoming, or processing livestreams are not supported; use a completed replay")
        if self.info.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
            raise AccessDenied("Video requires private or restricted access")
        i = self.info
        chapters = [{"start_time": c["start_time"], "title": c["title"]} for c in (i.get("chapters") or [])
                    if isinstance(c.get("start_time"), (int, float)) and c.get("title")]
        return Video(i["id"], i.get("title") or i["id"], i.get("channel") or i.get("uploader"),
                     i.get("uploader_id"), i.get("upload_date"), i.get("duration"), chapters)

    def captions(self, language):
        selected = choose_caption(self.info, language)
        if selected is None:
            return None
        code, source, track = selected
        progress(f"Fetching {code} {source.replace('_', ' ')}…")
        try:
            with self.ydl.urlopen(track["url"]) as response:
                raw = response.read(20 * 1024 * 1024 + 1)
            if len(raw) > 20 * 1024 * 1024:
                raise CaptureError("Caption track exceeds the 20 MiB limit")
            raw = raw.decode("utf-8-sig")
            check_challenge(raw)
        except RequestStopped as exc:
            raise AccessDenied(str(exc)) from None
        except RequestFailed as exc:
            raise CaptureError(str(exc)) from None
        except UnicodeDecodeError as exc:
            raise UnusableCaptions("Caption encoding is invalid") from exc
        return Transcript(parse_captions(raw, track["ext"], rolling=source == "youtube_auto_captions"), code, source,
                          raw={"format": track["ext"], "content": raw}, versions={"yt-dlp": version("yt-dlp")})

    def preflight(self, directory, language=None):
        preflight_local(directory, language)

    def download_audio(self, directory, language):
        fmt = choose_audio(self.info, language)
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if shutil.disk_usage(directory).free < max(2_000_000_000, size * 3):
            raise CaptureError("Insufficient free disk for audio and transcription")
        progress("Downloading original audio for local transcription…")
        self.ydl.params.update({"outtmpl": {"default": str(directory / "audio.%(ext)s")},
                                "overwrites": False, "continuedl": False})
        selected = {**self.info, **fmt}
        selected.pop("requested_formats", None)
        selected.pop("requested_downloads", None)
        try:
            self.ydl.process_info(selected)
        except RequestStopped as exc:
            raise AccessDenied(str(exc)) from None
        except RequestFailed as exc:
            raise CaptureError(str(exc)) from None
        files = [p for p in directory.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
        if len(files) != 1 or files[0].stat().st_size == 0:
            raise CaptureError("Audio download did not produce a complete file")
        return files[0]

    def transcribe(self, path, language):
        return transcribe_local(path, language)

    def download_video(self, directory):
        fmt = choose_video(self.info)
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if shutil.disk_usage(directory).free < max(2_000_000_000, size * 2):
            raise CaptureError("Insufficient free disk for the temporary video")
        progress("Downloading video for selected screenshots (temporary, at most 1080p)…")
        self.ydl.params.update({"outtmpl": {"default": str(directory / "video.%(ext)s")},
                               "overwrites": False, "continuedl": False})
        selected = {**self.info, **fmt}
        selected.pop("requested_formats", None)
        selected.pop("requested_downloads", None)
        try:
            self.ydl.process_info(selected)
        except RequestStopped as exc:
            raise AccessDenied(str(exc)) from None
        except RequestFailed as exc:
            raise CaptureError(str(exc)) from None
        expected = directory / f"video.{fmt['ext']}"
        if (not expected.is_file() or not expected.stat().st_size
                or list(directory.glob("*.part")) or list(directory.glob("*.ytdl"))
                or self.ydl._download_retcode):
            raise CaptureError("Video download did not produce a complete file")
        return expected

    def close(self):
        self.ydl.close()


def whisper_language(language):
    if language is None:
        return None
    from mlx_whisper.tokenizer import LANGUAGES
    normalized = language.lower().split("-")[0]
    if normalized not in LANGUAGES:
        raise CaptureError(f"Unsupported local transcription language: {language}")
    return normalized


def preflight_local(directory, language=None):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise CaptureError("Local transcription requires an Apple Silicon Mac")
    if not shutil.which("ffmpeg"):
        raise CaptureError("Local transcription requires FFmpeg on PATH")
    if shutil.disk_usage(directory).free < 4_000_000_000:
        raise CaptureError("Local transcription needs at least 4 GB free for audio and model weights")
    try:
        import mlx_whisper  # noqa: F401
    except ImportError as exc:
        raise CaptureError("Install local dependencies: uv sync --extra local in the skill directory") from exc
    whisper_language(language)


def transcribe_local(path, language=None):
    import mlx_whisper
    import numpy as np
    from mlx_whisper.audio import load_audio
    language = whisper_language(language)
    audio = load_audio(str(path))
    if not audio.size or np.max(np.abs(audio)) <= 1e-5:
        raise CaptureError("Audio contains only silence; no transcript saved")
    progress("Transcribing locally with Whisper large-v3-turbo (first run downloads model weights)…")
    # MLX prints language detection/progress to stdout; keep CLI output machine-readable.
    with redirect_stdout(sys.stderr):
        result = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL,
                                        task="transcribe", language=language, verbose=None,
                                        condition_on_previous_text=False)
    segments = [Segment(float(s["start"]), float(s["end"]), s["text"].strip())
                for s in result["segments"] if s["text"].strip()]
    validate_segments(segments)
    # Retain scalar ASR evidence, not token arrays / tensor objects.
    raw = [{k: s[k] for k in ("start", "end", "text", "avg_logprob", "no_speech_prob", "compression_ratio") if k in s}
           for s in result["segments"]]
    return Transcript(segments, result["language"], "local_whisper", model=MODEL, raw=raw,
                      versions={"mlx-whisper": version("mlx-whisper"), "yt-dlp": version("yt-dlp")})
