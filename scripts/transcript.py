"""Transcript normalization and local persistence; no network operations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

import yaml


class UnusableCaptions(ValueError):
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Video:
    id: str
    title: str
    channel: str | None = None
    handle: str | None = None
    upload_date: str | None = None
    duration: float | None = None
    chapters: list[dict] = field(default_factory=list)

    @property
    def url(self):
        return f"https://www.youtube.com/watch?v={self.id}"


@dataclass
class Transcript:
    segments: list[Segment]
    language: str
    source: str
    model: str | None = None
    raw: object = None
    versions: dict = field(default_factory=dict)


def validate_segments(segments, duration=None):
    if not segments:
        raise ValueError("Transcript has no speech segments")
    previous = -1
    for s in segments:
        if (not math.isfinite(s.start) or not math.isfinite(s.end)
                or s.start < 0 or s.end < s.start or s.start < previous or not s.text.strip()):
            raise ValueError("Transcript contains invalid or unordered segments")
        if duration and s.end > duration + 10:
            raise ValueError("Transcript timestamps exceed the video duration")
        previous = s.start


def _seconds(value):
    parts = value.replace(",", ".").split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def _clean(text, vtt=False):
    if vtt:
        text = re.sub(r"</?(?:c(?:\.[^ >]+)?|v(?: [^>]+)?|lang(?: [^>]+)?|b|i|u|ruby|rt)>|<\d\d:[\d:.]+>", "", text)
    return " ".join(html.unescape(text).split())


def parse_captions(raw: str, fmt: str, *, rolling=False) -> list[Segment]:
    try:
        segments = []
        append_events = set()
        if fmt == "json3":
            for e in json.loads(raw)["events"]:
                text = _clean("".join(s.get("utf8", "") for s in e.get("segs", [])))
                if text:
                    start = float(e["tStartMs"]) / 1000
                    segments.append(Segment(start, start + float(e.get("dDurationMs", 0)) / 1000, text))
                    if e.get("aAppend"):
                        append_events.add(len(segments) - 1)
        elif fmt == "vtt":
            for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
                lines = block.splitlines()
                for i, line in enumerate(lines):
                    match = re.match(r"([\d:.]+)\s+-->\s+([\d:.]+)", line)
                    if match:
                        text = _clean(" ".join(lines[i + 1:]), vtt=True)
                        if text:
                            segments.append(Segment(_seconds(match[1]), _seconds(match[2]), text))
                        break
        else:
            raise ValueError("Unsupported caption format")
        validate_segments(segments)
        # Only an automatic caption update with the SAME time anchor and a
        # complete matching prefix establishes a repeated display. Ordinary
        # overlapping cues can be deliberate repeated speech, even for ASR.
        result = []
        previous = None
        for index, s in enumerate(segments):
            words = s.text.split()
            if rolling and previous and s.start == previous.start and index not in append_events:
                old = previous.text.split()
                if words[:len(old)] == old:
                    words = words[len(old):]
            if words:
                result.append(Segment(s.start, s.end, " ".join(words)))
            previous = s
        validate_segments(result)
        return result
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UnusableCaptions("Caption track is empty or malformed") from exc


def escape(text):
    # HTML entities are decoded by Markdown rendering, without creating tags.
    text = html.escape(" ".join(str(text).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#!|~])", r"\\\1", text)


def timestamp(seconds):
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


def render_paragraphs(segments, video_id, chapters=()):
    output, group = [], []
    chapter_index = 0
    chapters = sorted((c for c in chapters if isinstance(c.get("start_time"), (int, float))
                       and c.get("title")), key=lambda c: c["start_time"])

    def flush():
        if group:
            start = group[0].start
            text = escape(" ".join(s.text for s in group))
            output.append(f"[{timestamp(start)}](https://www.youtube.com/watch?v={video_id}&t={int(start)}s) {text}")
            group.clear()

    for segment in segments:
        while chapter_index < len(chapters) and chapters[chapter_index]["start_time"] <= segment.start:
            flush()
            output.append(f"## {escape(chapters[chapter_index]['title'])}")
            chapter_index += 1
        if group and (segment.end - group[0].start > 60 or segment.start - group[-1].end > 2
                      or (segment.start - group[0].start >= 30 and group[-1].text.endswith((".", "?", "!")))):
            flush()
        group.append(segment)
    flush()
    return "\n\n".join(output)


def read_metadata(path):
    with path.open(encoding="utf-8") as f:
        if f.readline().strip() != "---":
            return {}
        lines = []
        for _ in range(100):
            line = f.readline()
            if line.strip() == "---":
                value = yaml.safe_load("".join(lines))
                return value if isinstance(value, dict) else {}
            lines.append(line)
    return {}


def find_existing(directory, video_id):
    found = []
    for path in directory.glob("*.md"):
        try:
            if read_metadata(path).get("video_id") == video_id:
                found.append(path)
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
    if len(found) > 1:
        raise ValueError("Multiple notes have this video ID; resolve duplicates before refreshing")
    return found[0] if found else None


def _atomic_note(path, content):
    fd, name = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_capture(directory: Path, video: Video, transcript: Transcript, existing=None):
    validate_segments(transcript.segments, video.duration)
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video.id):
        raise ValueError("Invalid video ID")
    directory.mkdir(parents=True, exist_ok=True)
    captured = datetime.now(timezone.utc).isoformat()
    published = None
    if video.upload_date:
        try:
            published = datetime.strptime(video.upload_date, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    slug = re.sub(r"[^a-z0-9]+", "-", f"{video.channel or 'unknown'} {video.title}".lower()).strip("-")[:100].rstrip("-") or "video"
    path = existing or directory / f"{published or 'undated'}-{slug}-{video.id}.md"
    if existing is None and path.exists():
        raise ValueError("Refusing to overwrite an unrelated existing file")
    relative = Path("_data") / video.id / f"{uuid.uuid4().hex}.json"
    meta = {"url": video.url, "video_id": video.id, "type": "youtube", "title": video.title,
            "author": video.handle, "author_name": video.channel, "published_at": published,
            "captured_at": captured, "duration_seconds": video.duration, "language": transcript.language,
            "body_source": transcript.source, "transcription_model": transcript.model,
            "source_file": str(relative), "tags": []}
    meta = {k: v for k, v in meta.items() if v is not None}
    data = {**meta, "segments": [asdict(s) for s in transcript.segments], "raw": transcript.raw,
            "tool_versions": transcript.versions, "chapters": video.chapters}
    data["segments_sha256"] = hashlib.sha256(json.dumps(data["segments"], ensure_ascii=False).encode()).hexdigest()
    sidecar = directory / relative
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with sidecar.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    body = render_paragraphs(transcript.segments, video.id, video.chapters)
    _atomic_note(path, "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + f"---\n# {escape(video.title)}\n\n{body}\n")
    return path
