"""Plain-Python tool functions for LLM agents. :mod:`mcp_server` exposes them over MCP;
they also work directly and from the CLI (``sceneoverflow describe media/``).

Everything returns JSON-serialisable dicts so an agent can read a project, write or
edit a script, run it, read the resulting timeline, and look at frames.
"""
from __future__ import annotations

import os
import tempfile
import traceback
from pathlib import Path

from .anchors import MarkerSet, detect_scenes, detect_silences
from .clip import Sequence
from .graph import AUDIO, IMAGE, VIDEO
from .media import MediaError, kind_of, scan_dir
from .project import Project, run_script
from .timing import fmt_time


def _source_info(project: Project, path: str, words: bool = False, silences: bool = True,
                 scenes: bool = True) -> dict:
    clip = project.load(path)
    src = project.source(path)
    info = {
        "path": src.path, "name": src.name, "kind": src.kind, "duration": src.duration,
        "duration_text": fmt_time(src.duration), "width": src.width, "height": src.height, "fps": src.fps,
        "has_audio": src.has_audio,
        "markers": MarkerSet(path).to_dict(),
    }
    if src.kind in (VIDEO, AUDIO):
        if silences and (src.has_audio or src.kind == AUDIO):
            info["silences"] = [{"start": s.start, "end": s.end} for s in clip.silences()]
        if scenes and src.kind == VIDEO:
            info["scenes"] = [float(t) for t in clip.scenes()]
        if words and (src.has_audio or src.kind == AUDIO):
            try:
                info["words"] = clip.words().to_list()
            except ImportError as e:
                info["words_error"] = str(e)
    return info


def analyze(target: str | Path, words: bool = False, cache_dir: str | Path | None = None,
            silences: bool = True, scenes: bool = True) -> dict:
    """Describe a media file or a whole media directory: durations, markers, silences, scenes."""
    target = Path(target)
    if target.is_dir():
        project = Project(target, cache_dir=cache_dir)
        found = scan_dir(target)
        files = found[VIDEO] + found[AUDIO] + found[IMAGE]
        return {"media_dir": str(target.resolve()),
                "files": [_source_info(project, f, words, silences, scenes) for f in files]}
    if not target.exists():
        raise MediaError(f"no such file: {target}")
    kind_of(target)
    project = Project(None, cache_dir=cache_dir or target.resolve().parent / ".sceneoverflow")
    return {"files": [_source_info(project, str(target), words, silences, scenes)]}


def run_edit(script: str | Path | None = None, code: str | None = None, media: str | Path | None = None,
             out: str | Path | None = None, mode: str = "preview", cache_dir: str | Path | None = None,
             png: str | Path | None = None) -> dict:
    """Run an edit script (a path, or inline ``code``) and return its timeline.

    Returns ``{"ok", "describe", "timeline", "out", "preview", "png", "error"}``.
    """
    if code is not None:
        d = Path(media).resolve().parent if media else Path(tempfile.mkdtemp(prefix="sceneoverflow-"))
        script = d / "_agent_edit.py"
        Path(script).write_text(code)
    if script is None:
        raise ValueError("run_edit needs a script path or code")
    try:
        res = run_script(script, media=media, out=out, mode=mode, cache_dir=cache_dir)
    except Exception as e:  # report, don't raise: the agent needs the traceback text
        tb = traceback.format_exc()
        user_lines = [ln for ln in tb.splitlines() if str(Path(script).name) in ln]
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb,
                "script_lines": user_lines}
    clip = res["clip"]
    info = {"ok": True, "describe": clip.describe(), "timeline": clip.to_json(), "out": res["out"],
            "preview": clip.preview() if mode == "preview" else None, "png": None,
            "stats": {"rendered": len(res["stats"].rendered), "cached": len(res["stats"].cached)}}
    if png:
        try:
            info["png"] = clip.timeline_png(str(png))
        except ImportError as e:
            info["png_error"] = str(e)
    return info


def frame_at(target: str | Path, t, media: str | Path | None = None, out: str | Path | None = None,
             width: int | None = 640, cache_dir: str | Path | None = None) -> str:
    """JPEG of the frame at time ``t`` from a media file or from a script's output. Returns the path."""
    target = Path(target)
    if target.suffix == ".py":
        res = run_script(target, media=media, mode="preview", cache_dir=cache_dir)
        clip = res["clip"]
    else:
        project = Project(None, cache_dir=cache_dir or target.resolve().parent / ".sceneoverflow")
        clip = project.load(target)
    return clip.frame_at(t, str(out) if out else None, width)


def thumbnails(target: str | Path, media: str | Path | None = None, out: str | Path | None = None,
               count: int = 8, cache_dir: str | Path | None = None) -> str:
    target = Path(target)
    if target.suffix == ".py":
        clip = run_script(target, media=media, mode="preview", cache_dir=cache_dir)["clip"]
    else:
        project = Project(None, cache_dir=cache_dir or target.resolve().parent / ".sceneoverflow")
        clip = project.load(target)
    return clip.thumbnails(str(out) if out else None, count)


def set_marker(path: str | Path, name: str, at) -> dict:
    ms = MarkerSet(path)
    ms.set(name, at)
    return ms.to_dict()


API_REFERENCE = """\
SceneOverflow edit scripts are Python. Decorate one function with @edit; it receives
(videos, sounds, pictures) — MediaLists indexed by number or filename — and returns a Clip.
Times: seconds, "12s", "500ms", "1:23.5", "120f", a TimeRef, or a Span.

Clip (video/audio):
  .trim(start, end=None) .split_at(*t) / .cut(*t) -> Sequence   .remove(*spans) .keep(*spans)
  .head(d) .tail(d)   a + b (concat)   .speed(f)   .fade_in(d) .fade_out(d) .fade(d)   .volume(g)
  .with_audio(sound, at=0, mode="mix"|"replace"|"duck", gain=1) / .dub(sound)
  .overlay(image_or_video, at, for_=None, pos="top-right"|(x,y), width=None)
  .text(str, at, for_=None, pos="bottom", size=36, color="white", box=None)
  .resize(w, h)   .audio (audio track as a clip)
Anchors: .marks["name"] (source clips) .silences(min_len, threshold_db) .scenes(threshold)
         .words() -> Transcript with .find("phrase") -> Span, .between(a, b)
Image clip: .as_clip("3s")
Sequence: [i] .get(i) .drop(*i) / .delete(*i) .keep(*i) .map(fn) .join()
Output: .describe() .to_json() .render(path) .preview() .frame_at(t) .timeline_png(path)
"""
