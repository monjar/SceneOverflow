# SceneOverflow

Code-first video editor on top of ffmpeg. Edits are Python scripts; the library builds an
immutable op graph, conforms sources to all-intra proxies, and renders through a
content-addressed cache. Read `README.md` for the user view and `DESIGN.md` for the why.

## Working in this repo

- `pip install -e '.[dev]'` then `pytest` (needs ffmpeg on PATH; about 2 minutes). Pure
  Python tests: `pytest -m "not ffmpeg"`.
- Layout: `sceneoverflow/clip.py` is the public API, `render/renderer.py` has one method per
  op, `describe.py` turns a graph into segments, `studio/` is the browser UI (stdlib server +
  one vanilla-JS page), `agent.py` + `mcp_server.py` are the tools for LLM agents.
- Ops available today: source, image_clip, audio_of, trim, concat, xfade, with_audio, volume,
  loudnorm, overlay, beside, speed, fade, resize, text, subtitles, crop, still, color.
- Adding an op: `graph.duration_of`, a `Clip` method, `Renderer._op_<name>`, a branch in
  `describe.segments`, a test in `tests/test_render.py` against the synthetic fixtures in
  `tests/conftest.py`.
- Every intermediate must stay conformed (profile resolution, CFR, all-intra, one audio
  stream); otherwise `concat` stream-copy breaks. Re-encode ops use `profile.video_encode_args()`.
- No new runtime dependencies. Optional features go behind extras.

## Driving an edit from Claude Code

`.mcp.json` registers the `sceneoverflow` MCP server against `examples/media`. Tools:
`analyze`, `api_reference`, `run_edit` (inline code or a script path), `frame_at`,
`thumbnails`, `render`, `set_marker`, `studio_state`. Typical loop: analyze the media,
write or edit the script, `run_edit` and read the timeline, `frame_at` to verify a moment,
`render` to export. If the user has `sceneoverflow studio` open on the script, saving the
file re-runs it there; `studio_state` shows the result.

In any other project, `sceneoverflow init` writes `.mcp.json`, a `CLAUDE.md` with the
scripting API, a starter `edit.py`, and `media/`.
