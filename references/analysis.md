# Screenshots and analysis

Use this workflow when the user asks to analyze a saved YouTube video, understand a demo, inspect specific moments, or save takeaways. A request to save only a transcript still uses `capture.py` alone.

## 1. Read the evidence

Run the normal capture workflow first; an `existing` result is fine. Read the note and its `source_file` JSON sidecar. Use the sidecar's segments for citations: the note may contain the user's edits. Preserve the user's question as the analysis intent. Do not invent a question or ask for one when the request already gives a useful purpose.

For speech-only questions, proceed directly to analysis. For demos, slides, code, UI, or explicit screenshot requests, choose a small set of meaningful moments from the transcript and the user's question. Prefer 3–8 moments, with room to inspect a nearby moment when a screenshot misses the relevant change. The helper defaults to a budget of 12 and accepts at most 24 requested frames per invocation. A timestamp in the original URL still does not narrow the source transcript.

## 2. Capture and inspect selected frames

Resolve `SKILL_DIR` to the actual directory containing SKILL.md, following symlinks. Before live screenshot retrieval apply the same external-product-research policy and single-video scope as capture. The helper writes a run record before network access. If any earlier stage in this task encountered a refusal or challenge, stop; do not use screenshot capture as another access method.

```sh
uv run --frozen --project "$SKILL_DIR" --extra local \
  python "$SKILL_DIR/scripts/enrich.py" frames "YOUTUBE_URL" \
  --timestamps "1:05,2:30.5,7:10"
```

Use the same `--output-dir` as capture if customized. Optional controls:

- `--resolution 1024`: maximum image width, default 1024, accepted range 256–1920; height is capped at 1920. Source videos are selected at up to 1080p. A larger output limit cannot recover detail absent from the source.
- `--max-frames 12`: reject requests exceeding this budget, rather than silently drop moments. Valid range 1–24.
- `--refresh`: recapture this set; old evidence and transcript stay intact.

The helper downloads one video stream into a temporary directory and uses local FFmpeg. It can require a full video download even for a few screenshots. No audio is uploaded and no transcription runs during this step. Video is removed on success, failure, or cancellation; JPEGs and a manifest remain under `_data/VIDEO_ID/visuals/`. Identical requests reuse saved evidence without network access.

Read stdout JSON. Each frame has an `id`, `absolute_path`, `requested_timestamp_seconds`, and actual `timestamp_seconds`. **View every frame used for a visual claim with the host's image-viewing tool.** A path or manifest alone is not visual evidence. Cite actual timestamps. These are selected stills, not continuous viewing; do not claim to have seen unsampled transitions or infer motion from one image. Avoid deduplicating code/UI screenshots just because they look similar.

If frames fail, report the limitation and preserve the transcript. After a refusal, end the run without more retrieval or finalizing an analysis as if it succeeded. Other failures may still permit a clearly transcript-only analysis if that answers the user. Never claim visual understanding from transcript-only evidence.

## 3. Write and save the analysis

Use the current agent to produce completed JSON, grounded in the source segments and any images actually inspected. Write it to a temporary file using a file-writing tool. Do not put unfinished placeholders or instructions into narrative fields. This helper validates structure and evidence references; it cannot fact-check the agent's interpretation.

The schema below is illustrative; replace all values with actual observations:

```json
{
  "intent": "Understand how the presenter configures the dashboard",
  "summary": ["The presenter configures the dashboard before demonstrating filters."],
  "key_moments": [
    {
      "timestamp_seconds": 65,
      "text": "The presenter explains the filter configuration.",
      "evidence": "transcript"
    },
    {
      "timestamp_seconds": 150.52,
      "text": "The dashboard displays a date filter above the chart.",
      "evidence": "visual",
      "frame_id": "frame-002"
    }
  ],
  "entities": ["The dashboard tool named in the transcript"],
  "concepts": ["Configure filters before comparing results"],
  "limitations": ["Only the selected screenshots were inspected."]
}
```

`intent`, nonempty `summary`, and nonempty `key_moments` are required. `entities`, `concepts`, and `limitations` are optional lists of strings. Each moment needs a finite nonnegative timestamp, text, and `evidence` (`transcript`, `visual`, or `both`). Transcript evidence must have speech within one second of that timestamp. Visual/both requires a valid `frame_id` and a timestamp within one second of that frame. Use actual frame timestamps for visual claims. Summary, entities, and concepts should synthesize the cited evidence; distinguish uncertainty and inference in the wording. The helper escapes Markdown in narrative fields, so use plain text.

```sh
uv run --frozen --project "$SKILL_DIR" --extra local \
  python "$SKILL_DIR/scripts/enrich.py" analyze "YOUTUBE_URL" \
  --input "/absolute/path/analysis.json" \
  --visuals "/absolute/path/from/frames/result/manifest.json"
```

Omit `--visuals` for transcript-only analysis. This command is entirely local and does not call an LLM. It saves `_analysis/VIDEO_ID.md`, with timestamp links, transcript link, and images referenced by key moments. Frontmatter identifies it as `agent_analysis` and links the source and analysis sidecars. It never changes the source note.

An existing analysis is returned unchanged. To replace it for a new question or revision, use `analyze --refresh` only when requested. If the transcript was refreshed, regenerate the analysis and any screenshots against the current source; old source sidecars and images remain intact. Do not pass an old manifest with a new source capture. After successful saving, remove only the temporary input JSON you created; its contents are retained in the analysis sidecar.

Read the saved note to verify links and content. Report its absolute path and the source transcript path, clarify whether screenshots were inspected, and state any material missing evidence. A reused result must be described as reused, not newly generated.
