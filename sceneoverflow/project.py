"""Project: media discovery, profiles, renderers, and the ``@edit`` entry point."""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from pathlib import Path

from .clip import Clip, EditError, MediaList, Sequence
from .graph import AUDIO, IMAGE, VIDEO, make
from .media import MediaError, Source, ffprobe_json, file_sig, final_profile, preview_profile, scan_dir
from .render import Cache, Renderer

MODES = ("preview", "final")


class Project:
    """Owns the cache, the quality profile, and the media lists.

    ``mode="preview"`` renders everything at 640x360 from all-intra proxies.
    ``mode="final"`` renders from full-resolution mezzanines of the originals.
    """

    def __init__(self, media: str | Path | None = None, cache_dir: str | Path | None = None,
                 mode: str = "preview", fps: float | None = None, log=None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.media_dir = str(Path(media).resolve()) if media else None
        self.cache = Cache(cache_dir or (Path(self.media_dir).parent / ".sceneoverflow" if self.media_dir else None))
        self.mode = mode
        self.log = log or (lambda msg: None)
        self._sources: dict[str, Source] = {}
        self._fps_override = fps
        self.videos = MediaList([], self, "videos")
        self.sounds = MediaList([], self, "sounds")
        self.pictures = MediaList([], self, "pictures")
        if self.media_dir:
            found = scan_dir(self.media_dir)
            self.videos = MediaList([self.load(p) for p in found[VIDEO]], self, "videos")
            self.sounds = MediaList([self.load(p) for p in found[AUDIO]], self, "sounds")
            self.pictures = MediaList([self.load(p) for p in found[IMAGE]], self, "pictures")
        self._profiles = {}

    # ---------------------------------------------------------- sources
    def source(self, path: str | Path) -> Source:
        path = str(Path(path).resolve())
        if not os.path.exists(path):
            raise MediaError(f"no such file: {path}")
        sig = file_sig(path)
        cached = self.cache.json_get("probe", sig)
        if cached is None:
            cached = ffprobe_json(path)
            self.cache.json_set("probe", sig, cached)
        src = Source.from_probe(path, cached)
        self._sources[path] = src
        return src

    def load(self, path: str | Path) -> Clip:
        """A clip for any media file (video, audio, or image)."""
        s = self.source(path)
        node = make("source", s.kind, {"path": s.path, "sig": s.sig, "duration": s.duration,
                                       "has_audio": s.has_audio, "width": s.width, "height": s.height})
        return Clip(node, self)

    @property
    def sources(self) -> list[Source]:
        return list(self._sources.values())

    # --------------------------------------------------------- profiles
    def _base_fps(self) -> float:
        if self._fps_override:
            return self._fps_override
        vids = [s for s in self._sources.values() if s.kind == VIDEO and s.fps]
        return round(vids[0].fps, 3) if vids else 30.0

    @property
    def preview_profile(self):
        if "preview" not in self._profiles:
            self._profiles["preview"] = preview_profile(self._base_fps())
        return self._profiles["preview"]

    @property
    def final_profile(self):
        if "final" not in self._profiles:
            vids = [s for s in self._sources.values() if s.kind == VIDEO and s.width]
            w, h = (vids[0].width, vids[0].height) if vids else (1280, 720)
            self._profiles["final"] = final_profile(w, h, self._base_fps())
        return self._profiles["final"]

    @property
    def profile(self):
        return self.final_profile if self.mode == "final" else self.preview_profile

    @property
    def preview_renderer(self) -> Renderer:
        if not hasattr(self, "_preview_renderer"):
            self._preview_renderer = Renderer(self.cache, self.preview_profile, self.log)
        return self._preview_renderer

    @property
    def final_renderer(self) -> Renderer:
        if not hasattr(self, "_final_renderer"):
            self._final_renderer = Renderer(self.cache, self.final_profile, self.log)
        return self._final_renderer

    @property
    def renderer(self) -> Renderer:
        return self.final_renderer if self.mode == "final" else self.preview_renderer

    # ------------------------------------------------------------ output
    def render(self, clip: Clip | Sequence, out: str | Path) -> str:
        if isinstance(clip, Sequence):
            clip = clip.join()
        return clip.render(str(out))

    def summary(self) -> str:
        lines = [f"media: {self.media_dir or '(none)'}", f"cache: {self.cache.root}",
                 f"mode: {self.mode} ({self.profile.key})"]
        for label, ml in (("videos", self.videos), ("sounds", self.sounds), ("pictures", self.pictures)):
            lines.append(f"{label}: " + (", ".join(f"{c.name} ({c.duration:.1f}s)" if c.kind != IMAGE else c.name
                                                   for c in ml) or "-"))
        return "\n".join(lines)


# ------------------------------------------------------------- @edit entry
_REGISTRY: list = []


def edit(fn):
    """Mark the edit function of a script. ``sceneoverflow run script.py`` calls it with
    ``(videos, sounds, pictures)``, or fewer arguments if the function takes fewer."""
    _REGISTRY.append(fn)
    return fn


def _call_edit(fn, project: Project):
    params = list(inspect.signature(fn).parameters.values())
    args = [project.videos, project.sounds, project.pictures]
    kwargs = {}
    if any(p.name == "project" for p in params):
        kwargs["project"] = project
        params = [p for p in params if p.name != "project"]
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) > 3 and any(p.default is p.empty for p in positional[3:]):
        raise EditError("edit() takes at most (videos, sounds, pictures[, project=...])")
    return fn(*args[:len(positional)], **kwargs)


def load_script(script: str | Path):
    """Import a script and return its edit function (``@edit``-decorated or named ``edit``)."""
    script = Path(script).resolve()
    if not script.exists():
        raise FileNotFoundError(script)
    before = len(_REGISTRY)
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location(f"_sceneoverflow_script_{script.stem}", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(script.parent))
    if len(_REGISTRY) > before:
        fn = _REGISTRY[-1]
        del _REGISTRY[before:]
        return fn
    if hasattr(mod, "edit") and callable(mod.edit):
        return mod.edit
    raise EditError(f"{script.name} has no @edit function (or a function named edit)")


def run_script(script: str | Path, media: str | Path | None = None, out: str | Path | None = None,
               mode: str = "preview", cache_dir: str | Path | None = None, log=None) -> dict:
    """Run a script end to end. Returns ``{"clip", "project", "out", "stats"}``."""
    fn = load_script(script)
    media = media or Path(script).resolve().parent / "media"
    if not Path(media).is_dir():
        raise MediaError(f"media directory not found: {media} (pass --media)")
    project = Project(media, cache_dir=cache_dir, mode=mode, log=log)
    result = _call_edit(fn, project)
    if isinstance(result, Sequence):
        result = result.join()
    if not isinstance(result, Clip):
        raise EditError(f"edit() must return a Clip or Sequence, got {type(result).__name__}")
    project.renderer.stats.reset()
    out_path = None
    if out:
        out_path = project.render(result, out)
    return {"clip": result, "project": project, "out": out_path, "stats": project.renderer.stats}
