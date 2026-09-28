"""Extract a bounded set of local video frames with measured timestamps."""
from __future__ import annotations

import math
from pathlib import Path
import re
import shutil
import subprocess

from sources import CaptureError


def parse_timestamps(value, *, duration=None, max_frames=12):
    if type(max_frames) is not int or not 1 <= max_frames <= 24:
        raise ValueError("Frame budget must be between 1 and 24")
    points = []
    for token in value.split(","):
        token = token.strip()
        if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", token):
            raise ValueError("Timestamps must be SS, MM:SS or HH:MM:SS, separated by commas")
        parts = [float(p) for p in token.split(":")]
        if any(p >= 60 for p in parts[1:]):
            raise ValueError("Timestamp minutes/seconds after a colon must be below 60")
        point = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
        if not math.isfinite(point) or point < 0:
            raise ValueError("Timestamps must be finite and nonnegative")
        if duration is not None and point >= duration:
            raise ValueError("Requested timestamp reaches or exceeds the video duration")
        points.append(point)
    points = sorted(set(points))
    if len(points) > max_frames:
        raise ValueError(f"Requested timestamps exceed the frame budget ({max_frames})")
    return points


def preflight_frames(resolution):
    if type(resolution) is not int or not 256 <= resolution <= 1920:
        raise ValueError("Frame resolution must be between 256 and 1920 pixels")
    if not shutil.which("ffmpeg"):
        raise CaptureError("Screenshot capture requires FFmpeg on PATH")


def extract_frames(video_path, out_dir, timestamps, *, resolution=1024):
    preflight_frames(resolution)
    path = Path(video_path).resolve(strict=True)
    if not path.is_file():
        raise CaptureError("Screenshot input must be a downloaded local video")
    if not timestamps or len(timestamps) > 24 or any(not math.isfinite(t) or t < 0 for t in timestamps):
        raise ValueError("Provide 1–24 finite nonnegative timestamps")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, point in enumerate(timestamps, 1):
        frame_id = f"frame-{index:03d}"
        target = out_dir / f"{frame_id}.jpg"
        # Preserve source timestamps normalized to the media start. Measuring
        # showinfo directly avoids rounding the seek into a different time base.
        # Only local file/pipe protocols may be used by the media decoder.
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-n",
               "-copyts", "-start_at_zero", "-protocol_whitelist", "file,pipe",
               "-ss", f"{point:.6f}", "-i", str(path),
               "-map", "0:v:0", "-an", "-sn", "-frames:v", "1",
               "-vf", f"scale=w='min(iw,{resolution})':h='min(ih,1920)':force_original_aspect_ratio=decrease,showinfo",
               "-fps_mode", "passthrough", "-q:v", "3", str(target)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise CaptureError("Frame extraction timed out") from exc
        if result.returncode or not target.is_file() or not target.stat().st_size:
            raise CaptureError(f"Could not decode a frame at {point:g}s")
        match = re.search(r"\[Parsed_showinfo_[^\]]+\].*?\bn:\s*0\s+.*?pts_time:([\d.eE+\-]+)", result.stderr)
        if not match:
            raise CaptureError("FFmpeg did not report the frame timestamp")
        actual = float(match[1])
        if not math.isfinite(actual) or actual < point - 0.001:
            raise CaptureError("FFmpeg reported an invalid frame timestamp")
        frames.append({"id": frame_id, "requested_timestamp_seconds": point,
                       "timestamp_seconds": round(actual, 6), "path": str(target.resolve())})
    return frames
