import json
from pathlib import Path

import pytest
import yaml

import transcript as t


def video(title="An example", date="20260920"):
    return t.Video("abcdefghijk", title, channel="A Channel", upload_date=date, duration=4000)


def sample():
    return t.Transcript([t.Segment(0, 2, "Hello world."), t.Segment(3601, 3603, "Again.")], "en", "youtube_manual_captions")


def test_json3_preserves_unicode_and_expands_entities():
    raw = json.dumps({"events": [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Café &amp; "}, {"utf8": "tea"}]}]})
    assert t.parse_captions(raw, "json3") == [t.Segment(1, 3, "Café & tea")]


def test_rolling_overlap_removed_but_deliberate_repetition_retained():
    raw = "WEBVTT\n\n00:00.000 --> 00:02.000\nHello there\n\n00:00.000 --> 00:03.000\nHello there friend\n\n00:04.000 --> 00:05.000\nyes\n\n00:06.000 --> 00:07.000\nyes\n"
    segments = t.parse_captions(raw, "vtt", rolling=True)
    assert [s.text for s in segments] == ["Hello there", "friend", "yes", "yes"]


@pytest.mark.parametrize("rolling", [False, True])
def test_overlapping_deliberate_repetition_is_preserved(rolling):
    raw = "WEBVTT\n\n00:00.000 --> 00:02.000\nGo go\n\n00:01.000 --> 00:03.000\ngo go!\n"
    assert [s.text for s in t.parse_captions(raw, "vtt", rolling=rolling)] == ["Go go", "go go!"]


def test_vtt_markup_and_literal_angle_brackets():
    raw = "WEBVTT\n\n00:00.000 --> 00:02.000\n<c>Hello</c> &lt;world&gt; <00:00.500>again\n"
    assert t.parse_captions(raw, "vtt")[0].text == "Hello <world> again"


@pytest.mark.parametrize("raw,fmt", [("oops", "json3"), ('{"events": []}', "json3"), ("WEBVTT\n", "vtt")])
def test_unusable_captions_are_explicit(raw, fmt):
    with pytest.raises(t.UnusableCaptions):
        t.parse_captions(raw, fmt)


@pytest.mark.parametrize("segment", [t.Segment(-1, 2, "x"), t.Segment(2, 1, "x"), t.Segment(float("nan"), 2, "x"), t.Segment(1, 2, " ")])
def test_invalid_segments_cannot_be_saved(segment, tmp_path):
    with pytest.raises(ValueError):
        t.save_capture(tmp_path, video(), t.Transcript([segment], "en", "local_whisper"))
    assert not list(tmp_path.glob("*.md"))


def test_note_links_sidecar_and_roundtrips_metadata(tmp_path):
    path = t.save_capture(tmp_path, video('A: "title" / ../../ # [x] <script>'), sample())
    assert path.parent == tmp_path
    body = path.read_text()
    meta = yaml.safe_load(body.split("---", 2)[1])
    assert meta["title"] == 'A: "title" / ../../ # [x] <script>'
    assert meta["published_at"] == "2026-09-20"
    assert "[01:00:01](https://www.youtube.com/watch?v=abcdefghijk&t=3601s)" in body
    assert "<script>" not in body.split("---", 2)[2]
    data = json.loads((tmp_path / meta["source_file"]).read_text())
    assert data["segments"][1] == {"start": 3601, "end": 3603, "text": "Again."}
    assert data["body_source"] == "youtube_manual_captions"


def test_failed_refresh_preserves_old_note_and_evidence(tmp_path, monkeypatch):
    path = t.save_capture(tmp_path, video(), sample())
    original = path.read_bytes()
    old_sources = list((tmp_path / "_data").rglob("*.json"))
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(t.os, "replace", fail)
    with pytest.raises(OSError):
        t.save_capture(tmp_path, video("Changed"), sample(), existing=path)
    assert path.read_bytes() == original
    assert all(p.exists() for p in old_sources)


def test_existing_capture_found_by_id_not_changed_title(tmp_path):
    path = t.save_capture(tmp_path, video(), sample())
    path.write_text(path.read_text() + "\nMy own notes.\n")
    found = t.find_existing(tmp_path, "abcdefghijk")
    assert found == path
    assert "My own notes." in found.read_text()


def test_missing_date_is_not_capture_date(tmp_path):
    path = t.save_capture(tmp_path, video(date=None), sample())
    assert path.name.startswith("undated-")
    assert "published_at:" not in path.read_text()


def test_chapters_are_source_supplied_and_text_is_not_executable(tmp_path):
    v = video()
    v.chapters = [{"start_time": 0, "title": "[Opening](evil)"}, {"start_time": 3500, "title": "Later"}]
    result = t.save_capture(tmp_path, v, sample()).read_text()
    assert "## Later" in result
    assert "## \\[Opening\\]" in result
