# get-youtube-transcript

An agent skill that saves a YouTube transcript, captures selected screenshots,
and writes a separate analysis with timestamped evidence. Notes are Markdown,
ready for an Obsidian vault or any notes folder. It works with Claude Code,
Codex, and other tools that read `SKILL.md` skills. Transcript capture is the
default; screenshots and analysis run when requested.

- Uses the video's original-language manual captions, then automatic
  captions, then local Whisper transcription (`mlx-whisper`,
  large-v3-turbo) on Apple Silicon when captions are missing or unusable.
- Preserves wording; every paragraph links back to its timestamp, and
  chapters become headings.
- Writes a JSON sidecar with the source segments and raw caption/ASR data.
- Re-running on the same video returns the existing note, so your edits
  survive. `--refresh` replaces it deliberately.
- Optionally capture selected screenshots and save a separate, linked analysis
  note. The current agent writes the analysis; no additional API key is needed.
- Analysis includes takeaways, timestamped key moments, optional people/tools,
  concepts, and limitations. Each moment identifies transcript, visual, or
  combined evidence; referenced screenshots are embedded in the note.
- Transcript and analysis notes use matching readable filenames. Source JSON,
  screenshots, and manifests retain separate IDs for evidence tracking.
- Stops on HTTP 401/403/429, challenges, or restricted videos. It never
  retries, switches clients, uses cookies, or goes through a proxy.

**Contents:** [Where it runs](#where-it-runs) ·
[Prerequisites](#prerequisites) · [Install](#install) ·
[Choose where notes go](#choose-where-notes-go) ·
[Check the install](#check-the-install) · [Use it](#use-it) ·
[Command line](#command-line) ·
[Screenshots and analysis](#screenshots-and-analysis) ·
[Saved files](#saved-files) · [Reuse and refresh](#reuse-and-refresh) ·
[Update and uninstall](#update-and-uninstall) ·
[Troubleshooting](#troubleshooting) ·
[Setup instructions for agents](#setup-instructions-for-agents) · [Tests](#tests)

## Where it runs

The skill runs a Python helper **on your own computer**. It needs a local
shell, your filesystem, and (for Whisper) an Apple Silicon Mac. That decides
which apps can use it:

| App | Works? | Skills folder |
|---|---|---|
| Claude Code CLI | Yes | `~/.claude/skills/` |
| Claude Code in VS Code / JetBrains | Yes | `~/.claude/skills/` |
| Claude Desktop app, **Code** tab (local session) | Yes, see [PATH note](#the-desktop-app-cannot-find-uv-or-ffmpeg) | `~/.claude/skills/` |
| Codex CLI and Codex IDE extension | Yes | `~/.agents/skills/` |
| ChatGPT desktop app, Codex | Yes, see [PATH note](#the-desktop-app-cannot-find-uv-or-ffmpeg) | `~/.agents/skills/` |
| Claude Desktop / claude.ai **chat** (ZIP upload) | No | — |
| ChatGPT chat (web, desktop, mobile) | No | — |

Chat skills run in a cloud sandbox, not on your Mac. The sandbox has no
Apple Silicon for Whisper and can't reach your notes folder, so a capture
there would land in a throwaway container. For the desktop apps, use their
coding agent (Claude's **Code** tab, or Codex in the ChatGPT app).

The helpers use Unix file locking: macOS and Linux can capture captions,
extract screenshots with FFmpeg, and save analysis. Local Whisper requires
Apple Silicon. Native Windows is not supported; use a Linux environment such
as WSL for the caption, screenshot, and analysis workflows.

## Prerequisites

| Requirement | Needed for | Install |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Everything | `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Git | Cloning | Comes with Xcode Command Line Tools: `xcode-select --install` |
| Apple Silicon Mac (M1 or later) | Local Whisper fallback | — |
| FFmpeg on `PATH` | Screenshots and local Whisper fallback | `brew install ffmpeg` |
| 4 GB free disk | Local Whisper fallback (audio + model weights) | — |
| Temporary video space | Screenshots; at least 2 GB free, or twice the estimated video size if larger | — |
| Node or Deno on `PATH` | Recommended; yt-dlp uses it for YouTube's player JavaScript | `brew install node` or `brew install deno` |

You don't need to install Python. uv downloads Python 3.12 and the locked
dependencies into the skill's own `.venv` on first run.

Caption capture and saving completed analysis JSON do not require FFmpeg or
Apple Silicon. Screenshot capture needs FFmpeg; local transcription needs
both. Missing requirements produce an error before media download.

## Install

### 1. Clone the repo

Clone it anywhere that you won't delete. The install links to this folder
rather than copying it, so `git pull` updates every app at once.

```sh
mkdir -p ~/workspace && cd ~/workspace
git clone https://github.com/jeffdhooton/get-youtube-transcript.git
cd get-youtube-transcript
```

### 2. Install the dependencies

```sh
uv sync --frozen --extra local
```

On an Intel Mac or Linux the `local` extra installs nothing extra. Caption
capture, screenshots with FFmpeg, and analysis still work; local Whisper does
not.

### 3. Link the skill into your agent

The link name must be `get-youtube`, matching the `name` in `SKILL.md`.
Run the lines for the apps you use (from inside the repo):

```sh
# Claude Code: CLI, IDE extensions, and the Desktop app's Code tab
mkdir -p ~/.claude/skills
ln -s "$PWD" ~/.claude/skills/get-youtube

# Codex: CLI, IDE extension, and Codex in the ChatGPT desktop app
mkdir -p ~/.agents/skills
ln -s "$PWD" ~/.agents/skills/get-youtube
```

Both Claude Code and Codex follow symlinked skill folders. To install for
one project only, link into that project's `.claude/skills/` (Claude) or
`.agents/skills/` (Codex) instead.

### 4. Reload the agent

- **Claude Code** picks up new skills in a running session. If
  `~/.claude/skills` did not exist when the session started, run
  `/reload-skills` or start a new session.
- **Codex** detects new skills automatically; restart it if the skill does
  not appear.
- **Desktop apps:** quit and reopen the app if the skill is missing, and
  read the [PATH note](#the-desktop-app-cannot-find-uv-or-ffmpeg).

## Choose where notes go

By default notes go to `~/Personal/Content/sources/youtube/`. To change it,
set `GET_YOUTUBE_OUTPUT_DIR`. Because agents run the helper in their own
process, put it where the agent will see it:

- **Claude Code:** add it to `~/.claude/settings.json`:

  ```json
  { "env": { "GET_YOUTUBE_OUTPUT_DIR": "/Users/you/Notes/YouTube" } }
  ```

- **Codex CLI and terminal use:** export it in `~/.zshrc` (or your shell's
  profile):

  ```sh
  export GET_YOUTUBE_OUTPUT_DIR="$HOME/Notes/YouTube"
  ```

- **Desktop apps:** set it where you set `PATH` (see
  [the PATH note](#the-desktop-app-cannot-find-uv-or-ffmpeg)).

You can also tell the agent per request ("save it to ~/Notes/YouTube"); it
passes `--output-dir`, which overrides the variable. The folder is created
if it doesn't exist.

## Check the install

```sh
cd ~/workspace/get-youtube-transcript
uv run --frozen --extra local pytest tests          # offline
uv run --frozen --extra local python scripts/capture.py --help
uv run --frozen --extra local python scripts/enrich.py frames --help
uv run --frozen --extra local python scripts/enrich.py analyze --help
```

Then, in your agent, confirm that the skill is listed: type `/get-youtube` in
Claude Code or `$get-youtube` in Codex.

The first Whisper transcription downloads the model weights (roughly
1.5 GB, cached under `~/.cache/huggingface/`). After that it runs offline
except for the audio download.

## Use it

Give your agent a link and ask for the transcript:

> Save the transcript of https://youtu.be/VIDEO_ID

Or invoke it explicitly: `/get-youtube <url>` in Claude Code,
`$get-youtube <url>` in Codex. Useful follow-ups:

- "Transcribe it locally even though it has captions" → `--transcribe`
- "Get the Spanish captions" → `--language es` (selects a source language;
  it does not translate)
- "Recapture it" → `--refresh` (replaces the existing note and your edits)
- "Summarize the argument and save timestamped takeaways" → transcript-only
  analysis; no video download needed
- "Inspect the demo and save takeaways with screenshots" → selected frames
  plus an analysis grounded in the transcript and images
- "Save screenshots at 1:05 and 2:30.5" → frames without an analysis note

If a transcript already exists, switching from captions to local transcription
requires `--transcribe --refresh`. Changing its language also requires
`--refresh`. Language selection must match the original audio when known;
translated captions and dubbed audio are not a translation feature.

The agent reports the saved path, title, channel, language, and where the
text came from (`body_source`: manual captions, automatic captions, or local
Whisper).

Watch, `youtu.be`, Shorts, embed, and finished live-replay links all work.
Timestamp and playlist parameters on a video URL still capture the complete
single video; they do not trim it or collect the playlist.
Playlists, members-only or private videos, and live or upcoming streams
don't. Replays still processing after a livestream are also rejected.

## Command line

The helper also runs without an agent:

```sh
export GET_YOUTUBE_OUTPUT_DIR=~/notes/youtube   # default: ~/Personal/Content/sources/youtube
uv run --frozen --project ~/workspace/get-youtube-transcript --extra local \
  python ~/workspace/get-youtube-transcript/scripts/capture.py "https://youtu.be/VIDEO_ID"
```

| Option | Meaning |
|---|---|
| `--language en` | Select or hint the source language; does not translate |
| `--transcribe` | Use local speech recognition even if captions exist |
| `--output-dir PATH` | Destination folder (overrides `GET_YOUTUBE_OUTPUT_DIR`) |
| `--refresh` | Replace an existing capture, including edits to its note |

All three commands write result JSON to stdout and progress/errors to stderr.
Exit code 0 means saved (or already saved), 1 means failed or stopped, 2 means
invalid command-line arguments, and 130 means cancelled. Results distinguish
`status: "saved"` from `status: "existing"`. Transcript and analysis results
include the absolute note `path`; frame results include `manifest_path` and
frame details. Network capture attempts leave records in `<output-dir>/_runs/`;
cached reuse and offline analysis do not create new retrieval records.

## Screenshots and analysis

Ask your agent, for example:

> Save this video's transcript, inspect the dashboard demo, and save the key
> takeaways with screenshots.

The skill reads the transcript, chooses relevant moments, views the extracted
images, and saves a separate analysis under `_analysis/` using the same readable
filename as the source transcript (date, channel, title, and video ID). The original
transcript stays intact. Speech-only analysis does not need a video download.

The underlying helper also supports two explicit commands. Run these from the
repository, using the same output folder as the source capture:

```sh
# Requires a transcript previously saved by capture.py in the same output folder.
uv run --frozen --extra local python scripts/enrich.py frames "YOUTUBE_URL" \
  --timestamps "1:05,2:30.5,7:10"

# After the agent reads the evidence and writes completed analysis JSON:
uv run --frozen --extra local python scripts/enrich.py analyze "YOUTUBE_URL" \
  --input /path/to/analysis.json --visuals /path/to/manifest.json
```

Both accept `--output-dir` (with the same environment variable and default as
`capture.py`). Options for `enrich.py frames`:

| Option | Meaning |
|---|---|
| `--timestamps "65,2:30.5,1:02:03"` | Required comma-separated seconds, `MM:SS`, or `HH:MM:SS`; fractional seconds supported |
| `--max-frames 12` | Budget for distinct requested moments, default 12, range 1–24; requests over budget fail |
| `--resolution 1024` | Maximum image width, default 1024, range 256–1920 pixels; height capped at 1920, aspect ratio preserved, no upscaling |
| `--output-dir PATH` | Folder containing the saved transcript |
| `--refresh` | Recapture this set while retaining previous evidence |

Timestamps are sorted and duplicates removed. They must be nonnegative and
before the end of the video when its duration is known. The agent normally
chooses 3–8 meaningful moments from the transcript and your question.

The frames command returns `manifest_path` and frame
IDs, absolute paths, requested times, and actual decoded times. It needs FFmpeg,
downloads one video stream up to 1080p, and removes the temporary video afterward.
JPEGs and the manifest stay under `_data/VIDEO_ID/visuals/`. A few screenshots can
still require downloading the full video. If no direct video format is usable,
the command fails without changing the transcript.

Repeating the same timestamps and resolution against the same source capture
reuses the images without network access. Each frame records its requested
time, actual decoded time, and integrity hash. The agent views every image
used for a visual claim and cites its actual time. These are selected stills;
they do not establish what happened between sampled moments.

Options for `enrich.py analyze`:

| Option | Meaning |
|---|---|
| `--input PATH` | Required completed analysis JSON, authored by the agent or supplied by you |
| `--visuals PATH` | Optional screenshot manifest returned by `frames`; required for visual claims |
| `--output-dir PATH` | Folder containing the saved transcript and evidence |
| `--refresh` | Replace the existing analysis note, including edits |

The analyze command runs offline, validates the supplied JSON, and saves the
completed note with source links, citations, and referenced images. Omit
`--visuals` for transcript-only analysis. It does not call an LLM or generate
the input JSON itself. Required fields are `intent`, nonempty `summary`, and
nonempty `key_moments`; optional lists are `entities`, `concepts`, and
`limitations`. Each moment has a timestamp, text, and evidence type
(`transcript`, `visual`, or `both`). Visual claims need a valid `frame_id`.
The validator checks nearby speech, frame timestamps, source identity, and
evidence integrity; it cannot fact-check the interpretation.

Existing analysis notes are preserved;
`analyze --refresh` explicitly replaces one. Neither command refreshes or edits
the transcript. A source refresh requires new analysis and matching visual
evidence. See [the analysis workflow and JSON schema](references/analysis.md).

Existing ID-only analysis filenames are renamed on reuse without rewriting
their contents. Identity is matched by video ID in frontmatter, so manually
renamed analysis notes are also found and preserved.

## Saved files

Under your output folder, a capture with screenshots and analysis looks like:

```text
youtube/
├── YYYY-MM-DD-channel-video-title-VIDEO_ID.md
├── _analysis/
│   └── YYYY-MM-DD-channel-video-title-VIDEO_ID.md
├── _data/
│   └── VIDEO_ID/
│       ├── SOURCE_ID.json
│       ├── analysis-ANALYSIS_ID.json
│       └── visuals/
│           └── CAPTURE_ID/
│               ├── manifest.json
│               ├── frame-001.jpg
│               └── frame-002.jpg
└── _runs/
    ├── VIDEO_ID-RUN_ID.json
    └── visuals-VIDEO_ID-RUN_ID.json
```

The date is the video's publication date (`undated` when unavailable).
Transcript frontmatter includes video identity, title/channel, capture time,
language, duration when available, `body_source`, and the relative `source_file`.
The source JSON retains timed segments, raw captions or ASR evidence, chapters,
tool versions, and a segment hash. Source labels are
`youtube_manual_captions`, `youtube_auto_captions`, and `local_whisper`.

Analysis frontmatter uses `body_source: agent_analysis` and links its source
and analysis JSON, plus the visual manifest when supplied. The body links back
to the transcript, source JSON, and YouTube video. Keep `_data/` alongside the
notes so evidence links and embedded screenshots continue to work. Temporary
audio/video is removed on completion, failure, or cancellation; saved evidence
and cached Whisper model weights remain.

## Reuse and refresh

| Artifact | Default on repeat | Explicit refresh |
|---|---|---|
| Transcript | Reuse the note by video ID, preserving edits and manual renames | `capture.py --refresh` replaces the note and creates a new source JSON |
| Screenshots | Reuse a matching source/timestamps/resolution request after integrity checks | `enrich.py frames --refresh` creates a new set; old images remain |
| Analysis | Reuse the existing note for that video, preserving edits | `enrich.py analyze --refresh` replaces the note and creates a new analysis JSON |

There is one current analysis note per video. A new question does not create
another note automatically; explicitly refresh to replace it. Refreshing the
transcript does not update analysis or images automatically: generate analysis
again with `analyze --refresh`, and capture matching screenshots if needed.
Old manifests cannot be used with a new source capture. Older JSON sidecars
and image sets remain for provenance. Screenshot and analysis commands never
modify the transcript.

## Update and uninstall

```sh
cd ~/workspace/get-youtube-transcript && git pull && uv sync --frozen --extra local
```

To uninstall, delete the links and the clone:

```sh
rm ~/.claude/skills/get-youtube ~/.agents/skills/get-youtube
rm -rf ~/workspace/get-youtube-transcript
```

Your saved notes are not touched.

## Troubleshooting

### The desktop app cannot find uv or ffmpeg

macOS apps opened from the Dock or Spotlight don't load your shell profile,
so `PATH` is only `/usr/bin:/bin:/usr/sbin:/sbin`. Homebrew tools in
`/opt/homebrew/bin` and uv in `~/.local/bin` are missing, and the skill
fails with "command not found" or "requires FFmpeg on PATH".

- **Claude Desktop:** in the prompt box, open the environment menu, hover
  **Local**, click the gear, and add
  `PATH=/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`
  (use your real home path, and add your Node's `bin` folder if it lives
  elsewhere). Put `GET_YOUTUBE_OUTPUT_DIR` here too.
- **Any app:** starting it from a terminal (`open -a Claude`) passes your
  shell's `PATH` along.

Run `which uv ffmpeg node` in a terminal to find the folders to add.

### Other problems

| Symptom | Cause and fix |
|---|---|
| Skill doesn't appear | The link name must be `get-youtube` and must point at the folder that holds `SKILL.md`. Check with `ls -l ~/.claude/skills/get-youtube/SKILL.md`. Then reload the agent. |
| `Install local dependencies: uv sync --extra local` | Run `uv sync --frozen --extra local` in the repo. |
| `Local transcription requires an Apple Silicon Mac` | The video has no usable captions and this machine can't run Whisper. |
| `Screenshot capture requires FFmpeg on PATH` | Install FFmpeg and make it visible to the agent; Apple Silicon is not required for screenshots. |
| `needs at least 4 GB free` | Free disk space; the check covers audio and model weights. |
| `AccessDenied`, HTTP 401/403/429, or a challenge | YouTube refused the request. The run stops; the skill will not retry through cookies, proxies, or other clients. |
| `Original audio language is ambiguous` | The available tracks do not identify the original reliably. The helper stops instead of guessing; `--language` cannot resolve every ambiguous track inventory. |
| An old note comes back | Captures are reused. Ask for a recapture (`--refresh`). |
| `Run capture.py ... before adding screenshots or analysis` | Save the transcript first and use the same `--output-dir` for all commands. |
| `No usable direct video format at or below 1080p` | Screenshots cannot be captured from the available stream inventory; the saved transcript remains usable. |
| `Transcript changed` or `Visual evidence belongs to a different transcript source` | Regenerate analysis with `analyze --refresh` and obtain screenshots for the current source if needed. |
| `Screenshot hash does not match` | Saved evidence changed; explicitly recapture it with `frames --refresh`. |
| `Multiple notes have this video ID` | Resolve duplicate notes in the relevant folder before continuing. |

## Setup instructions for agents

This section is for an AI agent asked to install this skill. Paste this to
your agent:

> Install the get-youtube skill from
> https://github.com/jeffdhooton/get-youtube-transcript by following the
> "Setup instructions for agents" section of its README.

**Agent: follow these steps in order. Stop and report if a step fails; do
not work around a failure with unrelated tools.**

1. **Check the platform.** Run `uname -sm`. `Darwin arm64` gets full
   support. Other macOS/Linux machines support captions, screenshots with
   FFmpeg, and analysis, but not local Whisper. Native Windows needs a Linux
   environment such as WSL before continuing.
2. **Check prerequisites.** Run `command -v git uv ffmpeg node deno`.
   - `git` or `uv` missing: ask the user before installing. Suggest
     `brew install uv` if `brew` exists, else the official uv installer.
   - `ffmpeg` missing: needed for screenshots on any supported platform and
     local Whisper on Apple Silicon. Ask before installing (for example,
     `brew install ffmpeg` on macOS).
   - Neither `node` nor `deno`: mention it's recommended; don't install
     without asking.
3. **Clone.** If `~/workspace/get-youtube-transcript` does not exist, ask the
   user where they keep repos (default `~/workspace`) and clone there with
   `git clone https://github.com/jeffdhooton/get-youtube-transcript.git`.
   If it exists, run `git -C <repo> pull --ff-only`.
4. **Install dependencies.** In the repo: `uv sync --frozen --extra local`.
5. **Link the skill.** Work out which agents the user has: `~/.claude`
   exists → Claude Code; `~/.codex` or `~/.agents` exists → Codex. For each,
   `mkdir -p` the skills folder, then create the link
   `ln -s <repo> ~/.claude/skills/get-youtube` and/or
   `ln -s <repo> ~/.agents/skills/get-youtube`. If something already exists
   at that path, show it to the user and ask before replacing it. Never
   copy the folder; link it.
6. **Set the output folder.** Ask the user where transcripts should go
   (default `~/Personal/Content/sources/youtube`). If they choose another
   folder, set `GET_YOUTUBE_OUTPUT_DIR` as described in
   [Choose where notes go](#choose-where-notes-go) for each agent you linked.
   Merge into existing settings files; never overwrite them.
7. **Desktop apps.** If the user runs Claude Desktop or the ChatGPT desktop
   app, give them the [PATH note](#the-desktop-app-cannot-find-uv-or-ffmpeg)
   with the real output of `dirname "$(command -v uv)"` and
   `dirname "$(command -v ffmpeg)"`. You cannot set the app's environment
   yourself; the user sets it in the app.
8. **Verify.** Run `uv run --frozen --extra local pytest tests` in the repo
   and show the result. Confirm that
   `ls -l <skills-folder>/get-youtube/SKILL.md` resolves for each link.
9. **Report.** Tell the user: the repo path, which agents got links, the
   output folder, whether local Whisper is available, and any desktop PATH
   step they still need to do. Do **not** capture a real video as a test
   unless the user asks and gives you a URL.

## Tests

```sh
uv run --frozen --project . --extra local pytest tests
```

The tests run offline against fixtures. They cover caption parsing, source
selection and refusals, transcript preservation, frame budgets and timestamps,
evidence integrity, analysis validation, readable filenames, legacy-name
migration, and refresh behavior. Frame integration tests use a locally generated
FFmpeg video and skip when FFmpeg or ffprobe is unavailable; no YouTube fetch
is needed.

## License

MIT
