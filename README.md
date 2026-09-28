# get-youtube-transcript

An agent skill (Claude Code, Codex, and other `SKILL.md`-aware tools) that
saves one YouTube video's transcript as timestamped Markdown, ready for an
Obsidian vault or any notes folder.

- Uses the video's original-language manual captions, then automatic
  captions, then local Whisper transcription (`mlx-whisper`,
  large-v3-turbo) on Apple Silicon when captions are missing or unusable.
- Preserves wording; every paragraph links back to its timestamp, and
  chapters become headings.
- Writes a JSON sidecar with the source segments and raw caption/ASR data.
- Re-running on the same video returns the existing note, so your edits
  survive. `--refresh` replaces it deliberately.
- Stops on HTTP 401/403/429, challenges, or restricted videos. It never
  retries, switches clients, uses cookies, or goes through a proxy.

## Install

Requires [uv](https://docs.astral.sh/uv/). Local transcription also needs an
Apple Silicon Mac and FFmpeg (`brew install ffmpeg`). yt-dlp works best with
Node or Deno on `PATH`.

```sh
git clone https://github.com/jeffdhooton/get-youtube-transcript.git
ln -s "$PWD/get-youtube-transcript" ~/.claude/skills/get-youtube   # Claude Code
ln -s "$PWD/get-youtube-transcript" ~/.agents/skills/get-youtube   # Codex and others
```

Then give your agent a YouTube link and ask it to save the transcript.

## Command line

```sh
export GET_YOUTUBE_OUTPUT_DIR=~/notes/youtube   # default: ~/Personal/Content/sources/youtube
uv run --frozen --project . --extra local python scripts/capture.py "https://youtu.be/VIDEO_ID"
```

| Option | Meaning |
|---|---|
| `--language en` | Select or hint the source language; does not translate |
| `--transcribe` | Use local speech recognition even if captions exist |
| `--output-dir PATH` | Destination folder (overrides `GET_YOUTUBE_OUTPUT_DIR`) |
| `--refresh` | Replace an existing capture, including edits to its note |

The result JSON goes to stdout; progress goes to stderr. Each run leaves a
record in `<output-dir>/_runs/`.

## Tests

```sh
uv run --frozen --project . --extra local pytest tests
```

The tests run offline against fixtures.

## License

MIT
