#!/usr/bin/env python3
"""Add selected screenshots or save an agent-authored analysis of a saved transcript."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import yaml

from analysis_note import render_analysis, validate_analysis
from capture import capture_lock, video_id
from frames import extract_frames, parse_timestamps, preflight_frames
from sources import AccessDenied, CaptureError, Sources, progress
from transcript import Segment, _atomic_note, find_existing, read_metadata, validate_segments


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _local_file(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise CaptureError("Evidence must use a relative local path")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise CaptureError("Evidence path is outside its source directory")
    return path


def load_capture(directory, vid):
    note = find_existing(directory, vid)
    if note is None:
        raise CaptureError("Run capture.py for this video before adding screenshots or analysis")
    meta = read_metadata(note)
    relative = meta.get("source_file")
    source_path = _local_file(directory, relative)
    if not source_path.is_relative_to((directory / "_data" / vid).resolve()):
        raise CaptureError("Transcript evidence is outside the video source directory")
    data = json.loads(source_path.read_text(encoding="utf-8"))
    if data.get("video_id") != vid or data.get("url") != f"https://www.youtube.com/watch?v={vid}":
        raise CaptureError("Transcript source does not match the requested video")
    if _digest(data.get("segments")) != data.get("segments_sha256"):
        raise CaptureError("Transcript source segment hash does not match")
    validate_segments([Segment(**s) for s in data["segments"]], data.get("duration_seconds"))
    return note, relative, data


def load_visuals(path, directory, vid, source_file):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_relative_to((directory / "_data" / vid / "visuals").resolve()):
        raise CaptureError("Visual manifest is outside this video's evidence directory")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("video_id") != vid or data.get("source_file") != source_file:
        raise CaptureError("Visual evidence belongs to a different transcript source")
    frames = {}
    for frame in data["frames"]:
        image = _local_file(path.parent, frame["path"])
        if hashlib.sha256(image.read_bytes()).hexdigest() != frame["sha256"]:
            raise CaptureError("Screenshot hash does not match; use frames --refresh")
        frames[frame["id"]] = {**frame, "absolute_path": str(image)}
    if not frames or len(frames) != len(data["frames"]):
        raise CaptureError("Visual manifest has missing or duplicate frames")
    return data, frames


def capture_visuals(url, output_dir, timestamps, *, resolution=1024, max_frames=12, refresh=False, source=None):
    vid = video_id(url)
    directory = Path(output_dir).expanduser().resolve()
    points = parse_timestamps(timestamps, max_frames=max_frames)
    with capture_lock(directory, vid):
        note, source_file, transcript = load_capture(directory, vid)
        points = parse_timestamps(timestamps, duration=transcript.get("duration_seconds"), max_frames=max_frames)
        request = {"source_file": source_file, "timestamps": points, "resolution": resolution, "version": 1}
        request_key = _digest(request)
        root = directory / "_data" / vid / "visuals"
        if not refresh:
            candidates = []
            for path in root.glob("*/manifest.json"):
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("request_key") == request_key:
                    candidates.append((cached["captured_at"], path))
            if candidates:
                _, path = max(candidates)
                _, frames = load_visuals(path, directory, vid, source_file)
                return {"status": "existing", "manifest_path": str(path), "frames": list(frames.values())}
        preflight_frames(resolution)
        run_id = uuid.uuid4().hex
        log_path = directory / "_runs" / f"visuals-{vid}-{run_id}.json"
        record = {"url": transcript["url"], "started_at": datetime.now(timezone.utc).isoformat(),
                  "scope": "Single video's metadata and one video stream for requested screenshots",
                  "timestamps": points, "max_frames": max_frames, "network": "Direct public access; no proxy or cookies",
                  "concurrency": 1, "retries": 0, "request_timeout_seconds": 30,
                  "target_visible_mutations": [], "retention": str(root),
                  "stop_conditions": ["401", "403", "429", "challenge", "denied playability"], "status": "running"}
        log_path.write_text(json.dumps(record, indent=2) + "\n")
        own_source = source is None
        try:
            source = source or Sources()
            video = source.metadata(transcript["url"])
            if video.id != vid:
                raise CaptureError("Retrieved video ID differs from requested video")
            parse_timestamps(timestamps, duration=video.duration, max_frames=max_frames)
            root.mkdir(parents=True, exist_ok=True)
            # Stage all frames together so failure cannot publish a partial manifest.
            with tempfile.TemporaryDirectory(prefix=".pending-", dir=root) as stage:
                stage = Path(stage)
                with tempfile.TemporaryDirectory(prefix="get-youtube-video-") as temp:
                    media = source.download_video(Path(temp))
                    frames = extract_frames(media, stage, points, resolution=resolution)
                for frame in frames:
                    image = Path(frame["path"])
                    frame["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
                    frame["path"] = image.name
                manifest = {"version": 1, "video_id": vid, "url": transcript["url"],
                            "source_file": source_file, "segments_sha256": transcript["segments_sha256"],
                            "request_key": request_key, "resolution": resolution,
                            "captured_at": datetime.now(timezone.utc).isoformat(), "frames": frames}
                (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
                destination = root / run_id
                stage.rename(destination)
            path = destination / "manifest.json"
            _, frames = load_visuals(path, directory, vid, source_file)
            record.update(status="saved", manifest_path=str(path))
            return {"status": "saved", "manifest_path": str(path), "frames": list(frames.values())}
        except BaseException as exc:
            record.update(status="stopped" if isinstance(exc, AccessDenied) else "failed", error_type=type(exc).__name__)
            raise
        finally:
            try:
                if own_source and source:
                    source.close()
            finally:
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                log_path.write_text(json.dumps(record, indent=2) + "\n")


def save_analysis(url, output_dir, payload, *, visuals_path=None, refresh=False):
    vid = video_id(url)
    directory = Path(output_dir).expanduser().resolve()
    with capture_lock(directory, vid):
        transcript_path, source_file, data = load_capture(directory, vid)
        analysis_dir = directory / "_analysis"
        existing = find_existing(analysis_dir, vid)
        # Match the readable source filename. Identity lives in frontmatter,
        # so user-renamed notes are still reused and legacy ID-only names migrate.
        path = analysis_dir / transcript_path.name
        if existing and existing.name != f"{vid}.md":
            path = existing
        if path != existing and (path.exists() or path.is_symlink()):
            raise CaptureError("Refusing to overwrite an unrelated analysis note")
        if existing and not refresh:
            old = read_metadata(existing)
            if old.get("source_file") != source_file:
                raise CaptureError("Transcript changed; use analyze --refresh to replace its analysis")
            if existing != path:
                existing.rename(path)
            return {"status": "existing", "path": str(path), "source_file": source_file}
        frames = {}
        if visuals_path:
            _, frames = load_visuals(visuals_path, directory, vid, source_file)
        validate_analysis(payload, data, frames)
        body = render_analysis(payload, data, transcript_path, frames, directory)
        relative = Path("_data") / vid / f"analysis-{uuid.uuid4().hex}.json"
        meta = {"type": "youtube_analysis", "video_id": vid, "url": data["url"], "title": data["title"],
                "body_source": "agent_analysis", "source_file": source_file,
                "segments_sha256": data["segments_sha256"], "analysis_file": str(relative),
                "generated_at": datetime.now(timezone.utc).isoformat(), "intent": payload["intent"]}
        if visuals_path:
            meta["visuals_file"] = str(Path(visuals_path).expanduser().resolve().relative_to(directory))
        sidecar = directory / relative
        with sidecar.open("x", encoding="utf-8") as f:
            json.dump({**meta, "analysis": payload}, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_note(existing or path, "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body)
        if existing and existing != path:
            existing.rename(path)
        return {"status": "saved", "path": str(path), "source_file": source_file}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    frames = commands.add_parser("frames", help="Capture specific moments from an already saved video transcript")
    analysis = commands.add_parser("analyze", help="Save completed agent-authored JSON as a linked analysis note (offline)")
    for command in (frames, analysis):
        command.add_argument("url")
        command.add_argument("--output-dir", type=Path, default=Path(os.environ.get("GET_YOUTUBE_OUTPUT_DIR")
                             or Path.home() / "Personal/Content/sources/youtube"))
        command.add_argument("--refresh", action="store_true", help="Refresh this artifact; never changes the source transcript")
    frames.add_argument("--timestamps", required=True, help="Comma-separated SS, MM:SS or HH:MM:SS")
    frames.add_argument("--resolution", type=int, default=1024)
    frames.add_argument("--max-frames", type=int, default=12, help="Requested frame budget, 1–24")
    analysis.add_argument("--input", type=Path, required=True, help="Completed analysis JSON; see references/analysis.md")
    analysis.add_argument("--visuals", type=Path, help="Screenshot manifest from the frames command")
    args = parser.parse_args(argv)
    try:
        if args.command == "frames":
            result = capture_visuals(args.url, args.output_dir, args.timestamps, resolution=args.resolution,
                                     max_frames=args.max_frames, refresh=args.refresh)
        else:
            result = save_analysis(args.url, args.output_dir, json.loads(args.input.read_text(encoding="utf-8")),
                                   visuals_path=args.visuals, refresh=args.refresh)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        progress("Cancelled; temporary media removed and prior artifacts preserved.")
        return 130
    except Exception as exc:
        progress(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
