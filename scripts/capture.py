#!/usr/bin/env python3
"""Capture one YouTube transcript; diagnostics on stderr, result JSON on stdout."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit
import uuid

from sources import AccessDenied, CaptureError, Sources, language_matches, progress
from transcript import UnusableCaptions, find_existing, read_metadata, save_capture, validate_segments


def video_id(url):
    u = urlsplit(url.strip())
    if u.scheme not in ("https", "http") or u.username or u.password or u.port:
        raise ValueError("Provide a public YouTube video URL")
    host = (u.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        candidate = u.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if u.path == "/watch":
            values = parse_qs(u.query).get("v", [])
            candidate = values[0] if len(values) == 1 else ""
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", u.path)
            candidate = match[1] if match else ""
    else:
        candidate = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        raise ValueError("Expected a single YouTube video URL, not a playlist or channel")
    return candidate


@contextmanager
def capture_lock(directory, vid):
    lock_dir = directory / "_runs"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{vid}.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptureError("Another capture of this video is already running") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def capture(url, output_dir, *, language=None, transcribe=False, refresh=False, source=None):
    vid = video_id(url)
    directory = Path(output_dir).expanduser().resolve()
    if language and not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]+)*", language):
        raise ValueError("Use a language code such as en, fr, or pt-BR")
    with capture_lock(directory, vid):
        existing = find_existing(directory, vid)
        if existing and not refresh:
            meta = read_metadata(existing)
            if language and not language_matches(meta.get("language"), language):
                raise CaptureError("Existing capture uses a different language; use --refresh to replace it")
            if transcribe and meta.get("body_source") != "local_whisper":
                raise CaptureError("Existing capture uses captions; use --transcribe --refresh to replace it")
            return {"status": "existing", "path": str(existing), **meta}
        canonical = f"https://www.youtube.com/watch?v={vid}"
        log_path = directory / "_runs" / f"{vid}-{uuid.uuid4().hex}.json"
        record = {"url": canonical, "started_at": datetime.now(timezone.utc).isoformat(),
                  "scope": "Single video metadata, captions, or original audio for local transcription",
                  "network": "Direct public access; no proxy or account cookies", "concurrency": 1,
                  "request_timeout_seconds": 30, "retries": 0, "target_visible_mutations": [],
                  "retention": str(directory), "stop_conditions": ["401", "403", "429", "challenge", "denied playability"],
                  "status": "running"}
        log_path.write_text(json.dumps(record, indent=2) + "\n")
        own_source = source is None
        try:
            source = source or Sources()
            video = source.metadata(canonical)
            if video.id != vid:
                raise CaptureError("Retrieved video ID differs from the requested video")
            transcript = None
            if not transcribe:
                try:
                    transcript = source.captions(language)
                    if transcript:
                        validate_segments(transcript.segments, video.duration)
                except (UnusableCaptions, ValueError):
                    progress("Captions are structurally unusable; using local transcription.")
                    transcript = None
            if transcript is None:
                with tempfile.TemporaryDirectory(prefix="get-youtube-") as temp:
                    temporary = Path(temp)
                    source.preflight(temporary, language)
                    audio_path = source.download_audio(temporary, language)
                    transcript = source.transcribe(audio_path, language)
            path = save_capture(directory, video, transcript, existing=existing)
            record.update(status="saved", path=str(path), body_source=transcript.source)
            return {"status": "saved", "path": str(path), **read_metadata(path)}
        except BaseException as exc:
            record.update(status="stopped" if isinstance(exc, AccessDenied) else "failed",
                          error_type=type(exc).__name__)
            raise
        finally:
            if own_source and source:
                source.close()
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            log_path.write_text(json.dumps(record, indent=2) + "\n")


def main(argv=None, *, source=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(os.environ.get("GET_YOUTUBE_OUTPUT_DIR") or Path.home() / "Personal/Content/sources/youtube"))
    parser.add_argument("--language", help="Source language code; does not translate")
    parser.add_argument("--transcribe", action="store_true", help="Use local speech recognition even when captions exist")
    parser.add_argument("--refresh", action="store_true", help="Explicitly replace an existing capture")
    args = parser.parse_args(argv)
    try:
        result = capture(args.url, args.output_dir, language=args.language,
                         transcribe=args.transcribe, refresh=args.refresh, source=source)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except KeyboardInterrupt:
        progress("Cancelled; temporary audio removed and existing notes preserved.")
        return 130
    except Exception as exc:
        progress(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
