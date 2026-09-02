# SceneOverflow: code-first video editor for programmers and LLM agents

> **Status (2026-09):** Phases 1, 2 and 3 are implemented and tested. See `README.md`.
> The design below is the original plan and still describes the architecture as built,
> with these deviations: the studio uses stdlib `http.server` with Server-Sent Events
> and a single vanilla-JS page instead of FastAPI + websockets + a framework, so the
> package stays dependency-free; the studio runs the script in a subprocess (as
> planned) and additionally serves frames, a media list, and a marker endpoint; the module is `timing.py`
> not `time.py`; the interactive marker tool drives mpv over its IPC socket from the
> terminal; final-quality export goes through a full-resolution all-intra mezzanine so
> the same copy-path pipeline serves preview and final; the MCP server also has
> `thumbnails`, `set_marker`, `render`, `studio_state` and `api_reference` tools. Post-plan
> additions: video-on-video compositing (`pip`, `beside`, opacity, audio mix, picture on
> picture), syntax highlighting in the studio editor, and `sceneoverflow init`, which writes
> `.mcp.json` + `CLAUDE.md` so Claude Code can drive a project.

## Context

Empty repo. Goal: a Python library where the edit is a script, not a GUI session. The unsolved problem the user raised: video is temporal and visual, so a script author cannot know "cut at 12s" without watching. The plan below treats that as the central design problem, not an afterthought. The UI is secondary and phased.

Assumptions made (no blocking questions):
- Language: Python 3.11+, single package `sceneoverflow`.
- Backend: ffmpeg/ffprobe subprocess. Not MoviePy (slow, frame-by-frame in Python), not a custom decoder.
- Phase 1 is CLI + library + Jupyter preview. The web studio is Phase 3.

## Honest positioning

What already exists: MoviePy (Python, imperative, slow), ffmpeg-python (filtergraph builder, no editing semantics), Remotion / Motion Canvas (TypeScript, React-render-per-frame, aimed at motion graphics not footage editing), auto-editor (silence cutting CLI), Descript (closed, edit-by-transcript).

"Chain methods on a clip" is not new; MoviePy has it. The defensible differentiators are:
1. **Anchors instead of timestamps** (markers, transcript words, silence, scene changes) so scripts are readable and writable blind.
2. **Declarative op graph with source-line provenance**, which makes a live inspector and node-level caching possible.
3. **Timeline that serializes to text/JSON** so an LLM agent can write the script, read the result, and iterate. This is the "AI enthusiast" hook and costs almost nothing once (2) exists.

If none of these three ship, the project is a worse MoviePy. Build them in that order.

## Core design

### 1. Everything is an immutable node in an op graph

`edit()` never touches files. Every method returns a new node; rendering is on demand.

```python
class Clip:        # video+audio, or audio-only, or image
    src, start, end, ops: tuple[Op, ...]
class Sequence:    # ordered list of Clips (result of split), supports join()
class Timeline:    # tracks: video, audio overlays, image overlays
```

Node = `(op_name, params, input_hashes)` -> content hash. Two nodes with the same hash render to the same bytes. This is what makes re-running the script on every keystroke cheap: only dirty nodes re-render.

Each node captures `inspect.stack()` at construction (file, line). This is the link the inspector uses to map "this segment on the timeline" <-> "this line in your script".

### 2. Time is a resolvable expression, not a float

Accept `"12s"`, `"1:23.5"`, `"120f"`, `12.0`, and anchor objects. Anchors resolve lazily at render/preview time against the source media's analysis cache.

Anchor sources, cheapest first:
- **Markers**: sidecar `clip.mp4.marks.json` written by the CLI/notebook/studio. Referenced as `v.marks["intro_end"]`. Git-friendly.
- **Silence**: `v.silences(min_len="0.5s", threshold=-35)` via `silencedetect`. Enables `v.remove(*v.silences())` (auto-editor in one line).
- **Scene changes**: `v.scenes()` via `select='gt(scene,0.4)'`.
- **Transcript**: `v.words()` via faster-whisper (optional extra). `v.words.find("let's begin").start`, `v.words["thanks for watching":].start`. This is edit-by-text, the Descript model.
- **Beats** (audio): librosa onset detection, optional extra. Enables `img_seq.cut_on(beats)`.

Rule: an anchor is a `TimeRef` with `.start`, `.end`, `.mid`, and `+`/`-` arithmetic with durations. Users still see raw seconds in errors and the inspector.

### 3. API surface (Phase 1)

The user's example, adjusted. `cut()` returning a list and then `.delete(1)` on the list works but hides intent. Provide both a low-level split and a high-level remove.

```python
from sceneoverflow import project

@project.edit
def edit(videos, sounds, pictures):
    v = videos[0]
    parts = v.split_at("12s", "14s")        # Sequence of 3
    v = parts.drop(1).join()               # == v.remove("12s", "14s")
    v = v.remove(*v.silences())            # anchor-driven
    fullsound = sounds.join()
    v = v.with_audio(fullsound, at=0, mode="mix")   # user's .dub
    v = v.overlay(pictures[0], at=v.marks["logo"], for_="3s", pos="top-right")
    return v.fade_in("0.5s").fade_out("0.5s")
```

Method list for Phase 1: `split_at`, `remove`, `trim`, `join`/`concat`, `with_audio` (replace/mix/duck), `overlay`, `speed`, `fade_in/out`, `resize`, `text`. Nothing else. Each maps to a known ffmpeg filter.

Time-literal parser and anchor resolution are the only nontrivial Python; the rest is filtergraph string building.

### 4. Rendering strategy (the thing that makes preview tolerable)

- **Import step**: on first touch, generate a **proxy** per source: 480p, all-intra (`-g 1` or ProRes proxy / MJPEG), constant frame rate, plus `ffprobe` metadata cached as JSON. All-intra means any cut is a lossless stream copy (`-c copy`), no re-encode, sub-second.
- **Preview render**: resolve the graph against proxies. Cuts and concats are `-c copy` + concat demuxer. Only nodes with filters (overlay, fade, speed, mix) re-encode, and only that node's span.
- **Cache**: `.sceneoverflow/cache/<hash>.mp4`. Hash includes op params and input hashes. Re-running `edit()` after changing one cut point re-renders one segment.
- **Final export**: same graph against originals, single `filter_complex` where possible, segment-and-concat fallback when the graph exceeds ffmpeg's practical filtergraph size.

Realistic expectation: preview latency for cut/join edits ~0.2–1s on proxies. Any edit that re-encodes a long span costs seconds. "Real-time on every keystroke" is honest only for cut-class ops; say so in the docs.

### 5. Seeing the edit: three tiers, phased

**Tier A (Phase 1, cheap): text and notebook.**
- `timeline.describe()` -> human/LLM-readable text and `timeline.to_json()`. Example:
  ```
  00:00.000-00:12.000  intro.mp4[0.0-12.0]          edit.py:5
  00:12.000-00:47.310  intro.mp4[14.0-49.31]        edit.py:5
  00:00.000-00:47.310  audio: music.mp3 (mix)       edit.py:8
  ```
- Jupyter `_repr_html_` on Clip/Timeline: `<video>` of the proxy render plus a thumbnail strip with timestamps. Zero frontend work, immediate payoff.
- `sceneoverflow mark <file>`: minimal player (mpv via IPC socket, or OpenCV window) where pressing `m` writes a named marker to the sidecar. Solves "which second" for users without the studio.

**Tier B (Phase 2): CLI watch mode.** `sceneoverflow watch edit.py` re-executes on save, re-renders dirty nodes, prints `describe()` diff, writes `preview.mp4`. Pair with any player that reloads.

**Tier C (Phase 3): Studio.** FastAPI server + single-page frontend (Svelte or plain TS, one timeline component, no framework sprawl).
- Server: watches script, runs it in a subprocess (isolation from user code crashes), serves proxy segments via HTTP range, pushes graph JSON over websocket.
- Client: `<video>` element for playback, canvas timeline with tracks and thumbnails. Bidirectional link: hover a timeline segment highlights the source line (provenance from step 1); click on timeline copies a `TimeRef` literal or adds a marker into the sidecar, which the script can then reference by name. Optional: embedded Monaco editor so edit + preview live in one tab.
- Do not implement client-side compositing. The server renders; the browser plays files.

### 6. LLM agent surface (Phase 2, small)

Once `describe()` and `to_json()` exist:
- `sceneoverflow describe media/` gives an agent the transcript, silences, scenes, durations as JSON.
- Expose the same as an MCP server (`sceneoverflow mcp`) with tools: `analyze(path)`, `run_edit(script)`, `describe_timeline()`, `render_preview()`, `frame_at(t)` returning a JPEG so a vision model can inspect a moment. This turns the library into something an agent can drive end-to-end. Cheap because the primitives already exist.

## Phase 1 implementation (what to build now)

Package layout:
```
sceneoverflow/
  __init__.py          project.edit decorator, Clip/Sequence/Timeline exports
  time.py              parse_time(), TimeRef, Duration, anchor arithmetic
  graph.py             Node, Op, hashing, provenance capture
  clip.py              Clip/Sequence/Timeline public API (methods above)
  media.py             ffprobe wrapper, proxy generation, analysis cache
  anchors.py           markers sidecar, silences(), scenes(); words() behind extra
  render/
    ffmpeg.py          subprocess runner, filtergraph builder
    planner.py         graph -> render plan (copy vs re-encode, segment/concat)
    cache.py           content-addressed cache
  describe.py          describe()/to_json()
  notebook.py          _repr_html_
  cli.py               sceneoverflow run|describe|mark|proxy
tests/
  test_time.py, test_graph.py, test_planner.py (no ffmpeg), test_render.py (needs ffmpeg, generates synthetic clips with lavfi testsrc)
pyproject.toml         deps: none required beyond stdlib + typer/click; extras: [whisper], [beats], [studio]
README.md              the example above, honest latency notes
```

Order of work:
1. `time.py` + `graph.py` with tests. Pure Python.
2. `media.py` probe + proxy generation against `lavfi testsrc` synthetic clips.
3. `clip.py` split/remove/trim/join + `planner.py` copy-path rendering. First end-to-end: the user's example minus dub.
4. `with_audio`, `overlay`, `fade`, `speed` via filtergraph.
5. `anchors.py` markers + silences + scenes.
6. `describe.py`, `notebook.py`, `cli.py`.
7. README with the positioning above.

Out of scope for Phase 1: whisper, beats, studio, MCP, transitions beyond fade, color ops, GPU encoding.

## Verification

- Unit: `pytest tests/` without ffmpeg for time/graph/planner.
- Integration: `pytest -m ffmpeg` generates 3 synthetic clips via `ffmpeg -f lavfi -i testsrc=duration=30`, runs the README example, asserts output duration with ffprobe (30s minus removed 2s = 28s), asserts the cache hit count on a second run is 100%, and asserts changing one cut point re-renders exactly one segment.
- Manual: `sceneoverflow run examples/basic.py` produces `out.mp4`; `describe()` output matches the expected table; Jupyter cell shows the player.
- ffmpeg is not installed in this container. Phase 1 CI needs `apt-get install ffmpeg` or a static build in the setup step.

## Risks stated plainly

- Filtergraph complexity grows fast with overlays and mixes. The segment-and-concat fallback in `planner.py` is mandatory, not optional.
- Variable frame rate phone footage breaks exact cuts. Proxies are forced CFR, and final export must `-vsync cfr` or cuts drift from what the preview showed.
- Audio/video split-and-join on `-c copy` requires cut points to land on proxy keyframes, which all-intra guarantees. Original-quality export re-encodes at cut boundaries or re-encodes fully. Accept it.
- The studio (Phase 3) is more work than Phases 1 and 2 combined. Do not start it before `describe()` and the notebook repr prove the model.
