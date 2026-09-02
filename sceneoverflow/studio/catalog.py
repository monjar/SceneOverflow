"""Completion catalog: the scripting API as data, generated from the real classes so it
cannot drift. Served by the studio at ``/api/catalog`` and printed by ``sceneoverflow api --json``."""
from __future__ import annotations

import inspect
from typing import Any

from ..anchors import MarkerSet, Transcript
from ..clip import TRANSITIONS, Clip, MediaList, Sequence
from ..project import Project
from ..render.renderer import _POSITIONS

_SKIP = {"node", "project", "clips", "label"}


def _methods(cls: type, skip: set[str] = frozenset()) -> list[dict[str, Any]]:
    out = []
    for name, member in inspect.getmembers(cls):
        if name.startswith("_") or name in _SKIP or name in skip:
            continue
        if isinstance(inspect.getattr_static(cls, name), property):
            doc = (inspect.getdoc(member) or "").splitlines()
            out.append({"name": name, "kind": "property", "sig": "", "params": [], "doc": doc[0] if doc else ""})
            continue
        if not callable(member):
            continue
        try:
            sig = inspect.signature(member)
        except (TypeError, ValueError):
            continue
        params = []
        for p in list(sig.parameters.values()):
            if p.name in ("self", "cls"):
                continue
            entry = {"name": p.name}
            if p.default is not inspect.Parameter.empty:
                entry["default"] = repr(p.default)
            if p.kind == p.VAR_POSITIONAL:
                entry["name"] = "*" + p.name
            elif p.kind == p.VAR_KEYWORD:
                entry["name"] = "**" + p.name
            params.append(entry)
        shown = ", ".join(e["name"] + (f"={e['default']}" if "default" in e else "") for e in params)
        doc = (inspect.getdoc(member) or "").splitlines()
        out.append({"name": name, "kind": "method", "sig": f"({shown})", "params": params,
                    "doc": doc[0] if doc else ""})
    return out


def catalog(project: Project | None = None) -> dict[str, Any]:
    cat: dict[str, Any] = {
        "clip": _methods(Clip),
        "sequence": _methods(Sequence),
        "medialist": _methods(MediaList),
        "project": _methods(Project, skip={"source", "load", "render", "summary", "sources"}) + _methods(Project)[:0],
        "markers": _methods(MarkerSet),
        "transcript": _methods(Transcript),
        "transitions": sorted(TRANSITIONS),
        "positions": sorted(_POSITIONS),
        "audio_modes": ["mix", "replace", "duck"],
        "time_examples": ['"12s"', '"500ms"', '"1:23.5"', '"120f"'],
        "media": {"videos": [], "sounds": [], "pictures": []},
        "marker_names": {},
    }
    # keep only the project methods that make sense inside a script
    cat["project"] = [m for m in cat["project"] if m["name"] in ("blank", "title", "videos", "sounds", "pictures",
                                                                  "profile", "mode", "media_dir")]
    if project is not None:
        for key, ml in (("videos", project.videos), ("sounds", project.sounds), ("pictures", project.pictures)):
            cat["media"][key] = [c.name for c in ml]
            for c in ml:
                marks = MarkerSet(c.path)
                if len(marks):
                    cat["marker_names"][c.name] = [name for name in marks]
    return cat
