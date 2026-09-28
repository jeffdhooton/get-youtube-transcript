"""Validate agent-authored analysis and render it separately from source text."""
from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import quote

from transcript import escape, timestamp


def validate_analysis(payload, source, frames):
    if not isinstance(payload, dict):
        raise ValueError("Analysis must be a JSON object")
    allowed = {"intent", "summary", "key_moments", "entities", "concepts", "limitations"}
    if set(payload) - allowed:
        raise ValueError("Unknown analysis fields")
    if not isinstance(payload.get("intent"), str) or not payload["intent"].strip():
        raise ValueError("Analysis needs an intent")
    for key in ("summary", "entities", "concepts", "limitations"):
        values = payload.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"{key} must be a list of nonempty strings")
    if not payload.get("summary"):
        raise ValueError("Analysis needs a nonempty summary")
    moments = payload.get("key_moments")
    if not isinstance(moments, list) or not moments:
        raise ValueError("Analysis needs timestamped key_moments")
    duration = source.get("duration_seconds")
    for moment in moments:
        if not isinstance(moment, dict) or set(moment) - {"timestamp_seconds", "text", "evidence", "frame_id"}:
            raise ValueError("Invalid key moment fields")
        point = moment.get("timestamp_seconds")
        if (type(point) not in (float, int) or not math.isfinite(point) or point < 0
                or (duration is not None and point >= duration)):
            raise ValueError("Key moment timestamp is outside the source duration")
        if not isinstance(moment.get("text"), str) or not moment["text"].strip():
            raise ValueError("Key moment needs text")
        evidence = moment.get("evidence")
        if evidence not in ("transcript", "visual", "both"):
            raise ValueError("Key moment evidence must be transcript, visual, or both")
        frame = frames.get(moment.get("frame_id"))
        if evidence in ("visual", "both") and frame is None:
            raise ValueError("Visual claims require a captured frame_id")
        if moment.get("frame_id") is not None and frame is None:
            raise ValueError("Unknown frame_id")
        if frame and abs(point - frame["timestamp_seconds"]) > 1:
            raise ValueError("Visual claim timestamp must be within one second of its frame")
        if evidence in ("transcript", "both") and not any(
                s["start"] - 1 <= point <= s["end"] + 1 for s in source["segments"]):
            raise ValueError("Transcript claim timestamp has no nearby speech segment")


def render_analysis(payload, source, transcript_path, frames, directory):
    def link(path):
        # The note lives in _analysis; assets and transcript remain in the vault.
        return quote("../" + str(Path(path).relative_to(directory)), safe="/")

    title = escape(source["title"])
    lines = [f"# {title} — analysis", "",
             "Agent-generated interpretation of the linked source evidence.", "",
             f"[Source transcript]({link(transcript_path)}) · "
             f"[Captured source JSON]({link(directory / source['source_file'])}) · [Video]({source['url']})", "",
             f"**Intent:** {escape(payload['intent'])}", "", "## Takeaways", ""]
    lines.extend(f"- {escape(text)}" for text in payload["summary"])
    lines.extend(["", "## Key moments", ""])
    used_frames = set()
    for moment in payload["key_moments"]:
        point = moment["timestamp_seconds"]
        lines.append(f"- [{timestamp(point)}]({source['url']}&t={int(point)}s) "
                     f"{escape(moment['text'])} ({moment['evidence']})")
        frame = frames.get(moment.get("frame_id"))
        if frame and frame["id"] not in used_frames:
            actual = frame["timestamp_seconds"]
            lines.extend(["", f"![Frame at {actual:g}s]({link(frame['absolute_path'])})", "",
                          f"Captured at {actual:g}s; requested {frame['requested_timestamp_seconds']:g}s.", ""])
            used_frames.add(frame["id"])
    for key, heading in (("entities", "People, tools and organizations"),
                         ("concepts", "Concepts"), ("limitations", "Limitations")):
        if payload.get(key):
            lines.extend(["", f"## {heading}", ""])
            lines.extend(f"- {escape(text)}" for text in payload[key])
    lines.extend(["", "Screenshots cover selected moments only; unsampled visual events may be missing."
                  if frames else "Evidence is transcript-only; no screenshots were inspected.", ""])
    return "\n".join(lines)
