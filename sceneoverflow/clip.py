"""Public editing API: :class:`Clip`, :class:`Sequence`, :class:`MediaList`.

Every method returns a new object; nothing is rendered until you ask.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable, Iterable, overload

import hashlib
from pathlib import Path

from . import anchors
from .graph import AUDIO, IMAGE, VIDEO, Node, make
from .timing import Span, TimeLike, TimeRef, fmt_time, parse_span, parse_time

if TYPE_CHECKING:
    from .project import Project


class EditError(ValueError):
    pass


TRANSITIONS = {
    "fade", "fadeblack", "fadewhite", "fadegrays", "dissolve", "pixelize", "distance", "hblur", "radial",
    "wipeleft", "wiperight", "wipeup", "wipedown", "wipetl", "wipetr", "wipebl", "wipebr",
    "slideleft", "slideright", "slideup", "slidedown", "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circlecrop", "rectcrop", "circleclose", "circleopen", "horzclose", "horzopen", "vertclose", "vertopen",
    "diagbl", "diagbr", "diagtl", "diagtr", "hlslice", "hrslice", "vuslice", "vdslice", "squeezev", "squeezeh",
    "zoomin", "coverleft", "coverright", "coverup", "coverdown", "revealleft", "revealright", "revealup", "revealdown",
}


class Clip:
    """A video, audio, or image node with editing methods."""

    def __init__(self, node: Node, project: "Project"):
        self.node = node
        self.project = project

    # ------------------------------------------------------------ basics
    @property
    def kind(self) -> str:
        return self.node.kind

    @property
    def duration(self) -> float:
        return self.node.duration

    @property
    def end(self) -> TimeRef:
        return TimeRef(self.duration, "end")

    @property
    def fps(self) -> float:
        return self.project.profile.fps

    @property
    def is_source(self) -> bool:
        return self.node.op == "source"

    @property
    def path(self) -> str | None:
        return self.node.params.get("path") if self.is_source else None

    @property
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else f"{self.node.op}:{self.node.hash}"

    def _t(self, t: TimeLike) -> float:
        return parse_time(t, self.fps)

    def _new(self, op: str, kind: str, params: dict, inputs: Iterable[Node]) -> "Clip":
        return Clip(make(op, kind, params, list(inputs)), self.project)

    def _need(self, *kinds: str, what: str = "this operation") -> None:
        if self.kind not in kinds:
            raise EditError(f"{what} needs a {'/'.join(kinds)} clip, got {self.kind} ({self.name})")

    def _check_range(self, s: float, e: float) -> None:
        if e <= s:
            raise EditError(f"empty range {fmt_time(s)}-{fmt_time(e)} on {self.name}")
        if e > self.duration + 1e-3:
            raise EditError(f"{fmt_time(e)} is past the end of {self.name} ({fmt_time(self.duration)})")

    def __repr__(self) -> str:
        return f"<Clip {self.kind} {self.name} {fmt_time(self.duration)}>"

    # ----------------------------------------------------------- cutting
    def trim(self, start: TimeLike = 0, end: TimeLike | None = None) -> "Clip":
        """Keep ``[start, end)``. Also accepts a single :class:`Span`: ``v.trim(v.words.find("hi"))``."""
        self._need(VIDEO, AUDIO, what="trim")
        if isinstance(start, Span) and end is None:
            s, e = start.start, start.end
        else:
            s = self._t(start)
            e = self.duration if end is None else self._t(end)
        self._check_range(s, e)
        if s == 0 and abs(e - self.duration) < 1e-6:
            return self
        return self._new("trim", self.kind, {"start": s, "end": e}, [self.node])

    def split_at(self, *times: TimeLike) -> "Sequence":
        """Cut at each time; returns the pieces in order (``n`` cuts, ``n+1`` pieces)."""
        self._need(VIDEO, AUDIO, what="split_at")
        pts = sorted({self._t(t) for t in times})
        pts = [p for p in pts if 0 < p < self.duration]
        bounds = [0.0] + pts + [self.duration]
        return Sequence([self.trim(a, b) for a, b in zip(bounds, bounds[1:])], self.project)

    def cut(self, *times: TimeLike) -> "Sequence":
        """Alias of :meth:`split_at`."""
        return self.split_at(*times)

    def _spans(self, items) -> list[Span]:
        out = []
        for it in items:
            if isinstance(it, Span):
                out.append(it)
            elif isinstance(it, (list, tuple)) and len(it) == 2 and not isinstance(it[0], (list, tuple, Span)):
                out.append(parse_span(it, self.fps))
            elif isinstance(it, (list, tuple)):
                out.extend(self._spans(it))
            else:
                raise EditError(f"expected a Span or (start, end) pair, got {it!r}")
        return out

    @staticmethod
    def _merge(spans: list[Span], lo: float, hi: float) -> list[Span]:
        clipped = [c for s in spans if (c := s.clip_to(lo, hi))]
        clipped.sort(key=lambda s: s.start)
        merged: list[Span] = []
        for s in clipped:
            if merged and s.start <= merged[-1].end:
                merged[-1] = Span(merged[-1].start, max(merged[-1].end, s.end), merged[-1].label)
            else:
                merged.append(s)
        return merged

    def remove(self, *spans) -> "Clip":
        """Delete ranges and join what is left. ``v.remove(("12s", "14s"), *v.silences())``."""
        self._need(VIDEO, AUDIO, what="remove")
        holes = self._merge(self._spans(spans), 0.0, self.duration)
        if not holes:
            return self
        keep, cursor = [], 0.0
        for h in holes:
            if h.start > cursor:
                keep.append(Span(cursor, h.start))
            cursor = h.end
        if cursor < self.duration:
            keep.append(Span(cursor, self.duration))
        if not keep:
            raise EditError(f"remove() would delete all of {self.name}")
        return Sequence([self.trim(s.start, s.end) for s in keep], self.project).join()

    def keep(self, *spans) -> "Clip":
        """Inverse of :meth:`remove`: keep only these ranges, in time order."""
        self._need(VIDEO, AUDIO, what="keep")
        parts = self._merge(self._spans(spans), 0.0, self.duration)
        if not parts:
            raise EditError("keep() needs at least one non-empty range")
        return Sequence([self.trim(s.start, s.end) for s in parts], self.project).join()

    def loop(self, times: int) -> "Clip":
        """Repeat this clip ``times`` times back to back."""
        if times < 1:
            raise EditError("loop needs times >= 1")
        return Sequence([self] * times, self.project).join()

    def still(self, at: TimeLike = 0) -> "Clip":
        """The frame at ``at`` as an image clip (use ``.as_clip(d)`` to hold it)."""
        self._need(VIDEO, what="still")
        t = min(self._t(at), max(0.0, self.duration - 0.04))
        return self._new("still", IMAGE, {"at": t}, [self.node])

    def freeze(self, at: TimeLike, duration: TimeLike = "2s") -> "Clip":
        """Freeze-frame: hold the frame at ``at`` for ``duration`` and continue."""
        self._need(VIDEO, what="freeze")
        t = self._t(at)
        held = self.still(t).as_clip(duration)
        parts = [self.trim(0, t), held] if t > 0 else [held]
        if t < self.duration:
            parts.append(self.trim(t, self.duration))
        return Sequence(parts, self.project).join()

    def crossfade(self, other: "Clip", duration: TimeLike = "0.5s", transition: str = "fade") -> "Clip":
        """This clip into ``other`` with a transition. See :meth:`Sequence.join`."""
        return Sequence([self, other], self.project).join(transition=transition, duration=duration)

    def head(self, duration: TimeLike) -> "Clip":
        return self.trim(0, min(self._t(duration), self.duration))

    def tail(self, duration: TimeLike) -> "Clip":
        return self.trim(max(0.0, self.duration - self._t(duration)), self.duration)

    # ---------------------------------------------------------- combining
    def __add__(self, other: "Clip") -> "Clip":
        return Sequence([self, other], self.project).join()

    def as_clip(self, duration: TimeLike) -> "Clip":
        """Turn an image into a still video clip of the given duration."""
        self._need(IMAGE, what="as_clip")
        d = self._t(duration)
        if d <= 0:
            raise EditError("as_clip duration must be positive")
        return self._new("image_clip", VIDEO, {"duration": d}, [self.node])

    @property
    def audio(self) -> "Clip":
        """The audio track of a video clip as an audio clip."""
        if self.kind == AUDIO:
            return self
        self._need(VIDEO, what="audio")
        return self._new("audio_of", AUDIO, {}, [self.node])

    def with_audio(self, audio: "Clip", at: TimeLike = 0, mode: str = "mix", gain: float = 1.0) -> "Clip":
        """Add a sound at ``at``. ``mode``: ``mix`` (layer), ``replace``, or ``duck`` (lower the
        existing audio while the new one plays). Output keeps this clip's duration."""
        self._need(VIDEO, what="with_audio")
        if mode not in ("mix", "replace", "duck"):
            raise EditError(f"with_audio mode must be mix/replace/duck, got {mode!r}")
        a = audio.audio if audio.kind == VIDEO else audio
        if a.kind != AUDIO:
            raise EditError(f"with_audio needs an audio (or video) clip, got {audio.kind}")
        return self._new("with_audio", VIDEO, {"at": self._t(at), "mode": mode, "gain": float(gain)},
                         [self.node, a.node])

    def dub(self, audio: "Clip", at: TimeLike = 0, gain: float = 1.0) -> "Clip":
        """Alias of ``with_audio(..., mode="mix")``."""
        return self.with_audio(audio, at, "mix", gain)

    def overlay(self, top: "Clip", at: TimeLike = 0, for_: TimeLike | None = None, pos="top-right",
                width: int | None = None, scale: float | None = None, opacity: float = 1.0, audio: bool = False,
                gain: float = 1.0, margin: int = 16) -> "Clip":
        """Composite ``top`` (an image or a video) on this clip.

        ``pos`` is a name (top-left, top-right, bottom-left, bottom-right, center, top, bottom)
        or ``(x, y)``. Size it with ``width`` (pixels) or ``scale`` (fraction of this clip's
        width). ``opacity`` 0..1. A video ``top`` starts playing at ``at``; ``for_`` defaults to
        its own length; ``audio=True`` mixes its sound in at ``gain``. Works on an image base
        too (picture on picture), which yields an image."""
        if top.kind not in (IMAGE, VIDEO):
            raise EditError("overlay needs an image or video clip on top")
        if not 0.0 <= opacity <= 1.0:
            raise EditError("opacity must be between 0 and 1")
        if scale is not None and not 0.0 < scale <= 1.0:
            raise EditError("scale must be a fraction of the base width, between 0 and 1")
        if self.kind == IMAGE:
            if top.kind != IMAGE:
                raise EditError("a video on top of a picture: turn the picture into a clip first, "
                                "pictures[i].as_clip('5s').overlay(video)")
            return self._new("overlay", IMAGE, {"pos": pos, "width": width, "scale": scale, "opacity": opacity,
                                                "margin": margin}, [self.node, top.node])
        self._need(VIDEO, what="overlay")
        at_s = self._t(at)
        if at_s >= self.duration:
            raise EditError(f"overlay at {fmt_time(at_s)} is past the end of {self.name}")
        dur = None if for_ is None else self._t(for_)
        if dur is None and top.kind == VIDEO:
            dur = min(top.duration, self.duration - at_s)
        return self._new("overlay", VIDEO, {"at": at_s, "duration": dur, "pos": pos, "width": width, "scale": scale,
                                            "opacity": opacity, "audio": bool(audio), "gain": float(gain),
                                            "margin": margin}, [self.node, top.node])

    def pip(self, top: "Clip", at: TimeLike = 0, for_: TimeLike | None = None, pos="bottom-right",
            scale: float = 0.3, audio: bool = False, opacity: float = 1.0) -> "Clip":
        """Picture-in-picture: ``overlay`` sized to a fraction of this clip's width."""
        return self.overlay(top, at=at, for_=for_, pos=pos, scale=scale, audio=audio, opacity=opacity)

    def beside(self, other: "Clip", vertical: bool = False) -> "Clip":
        """This clip and ``other`` side by side (or stacked with ``vertical=True``), both audios
        mixed, letterboxed into the frame. Runs for the longer of the two."""
        self._need(VIDEO, what="beside")
        if other.kind != VIDEO:
            raise EditError("beside needs two video clips (use pictures[i].as_clip(d) for stills)")
        return self._new("beside", VIDEO, {"vertical": bool(vertical)}, [self.node, other.node])

    def above(self, other: "Clip") -> "Clip":
        """This clip on top of ``other``, stacked vertically. Alias of ``beside(other, vertical=True)``."""
        return self.beside(other, vertical=True)

    def text(self, text: str, at: TimeLike = 0, for_: TimeLike | None = None, pos="bottom", size: int = 36,
             color: str = "white", font: str | None = None, box: str | None = None) -> "Clip":
        """Burn a caption. ``box`` is an optional background color like ``black@0.5``."""
        self._need(VIDEO, what="text")
        at_s = self._t(at)
        dur = None if for_ is None else self._t(for_)
        return self._new("text", VIDEO, {"text": text, "at": at_s, "duration": dur, "pos": pos, "size": size,
                                         "color": color, "font": font, "box": box}, [self.node])

    def speed(self, factor: float) -> "Clip":
        self._need(VIDEO, AUDIO, what="speed")
        if factor <= 0:
            raise EditError("speed factor must be positive")
        if factor == 1:
            return self
        return self._new("speed", self.kind, {"factor": float(factor)}, [self.node])

    def fade_in(self, duration: TimeLike = "0.5s") -> "Clip":
        return self._fade(fade_in=self._t(duration))

    def fade_out(self, duration: TimeLike = "0.5s") -> "Clip":
        return self._fade(fade_out=self._t(duration))

    def fade(self, duration: TimeLike = "0.5s") -> "Clip":
        d = self._t(duration)
        return self._fade(fade_in=d, fade_out=d)

    def _fade(self, fade_in: float = 0.0, fade_out: float = 0.0) -> "Clip":
        self._need(VIDEO, AUDIO, what="fade")
        if fade_in + fade_out > self.duration:
            raise EditError(f"fades ({fade_in + fade_out:.2f}s) longer than clip ({self.duration:.2f}s)")
        return self._new("fade", self.kind, {"fade_in": fade_in, "fade_out": fade_out}, [self.node])

    def volume(self, gain: float) -> "Clip":
        self._need(VIDEO, AUDIO, what="volume")
        return self._new("volume", self.kind, {"gain": float(gain)}, [self.node])

    def mute(self) -> "Clip":
        return self.volume(0.0)

    def normalize(self, lufs: float = -16.0) -> "Clip":
        """Loudness-normalise the audio (EBU R128, default -16 LUFS: podcast/YouTube level)."""
        self._need(VIDEO, AUDIO, what="normalize")
        return self._new("loudnorm", self.kind, {"lufs": float(lufs)}, [self.node])

    def crop(self, aspect: str | None = None, anchor="center", x: int | None = None, y: int | None = None,
             w: int | None = None, h: int | None = None) -> "Clip":
        """Crop to an aspect ratio (``"9:16"`` for vertical, ``"1:1"``) anchored at ``center``/``left``/
        ``right``/``top``/``bottom`` or an ``(fx, fy)`` pair in 0..1, or to an explicit ``x, y, w, h`` box.
        The frame size changes, so crop last (or crop every piece the same way before joining)."""
        self._need(VIDEO, what="crop")
        if aspect:
            try:
                aw, ah = (float(v) for v in str(aspect).replace("x", ":").split(":"))
            except ValueError:
                raise EditError(f"aspect must look like '9:16', got {aspect!r}") from None
            named = {"center": (0.5, 0.5), "left": (0.0, 0.5), "right": (1.0, 0.5), "top": (0.5, 0.0),
                     "bottom": (0.5, 1.0)}
            fx, fy = named[anchor] if isinstance(anchor, str) else (float(anchor[0]), float(anchor[1]))
            return self._new("crop", VIDEO, {"aspect": [aw, ah], "anchor": [fx, fy]}, [self.node])
        if None in (x, y, w, h):
            raise EditError("crop needs aspect= or all of x, y, w, h")
        return self._new("crop", VIDEO, {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}, [self.node])

    def subtitles(self, source, style: str | None = None) -> "Clip":
        """Burn subtitles from an ``.srt``/``.ass`` file or a :class:`~sceneoverflow.anchors.Transcript`.
        ``style`` is an ASS override like ``"FontSize=28,PrimaryColour=&H00FFFF00"``."""
        self._need(VIDEO, what="subtitles")
        if isinstance(source, anchors.Transcript):
            srt = source.to_srt()
            key = hashlib.sha256(srt.encode()).hexdigest()[:16]
            path = self.project.cache.scratch(f"subs-{key}.srt")
            path.write_text(srt)
            sig = key
        else:
            path = Path(source).resolve()
            if not path.exists():
                raise EditError(f"subtitle file not found: {path}")
            sig = f"{path.stat().st_size}|{int(path.stat().st_mtime)}"
        return self._new("subtitles", VIDEO, {"path": str(path), "sig": sig, "style": style}, [self.node])

    def captions(self, style: str | None = None, **transcribe_opts) -> "Clip":
        """Burn captions from the transcript (needs the ``whisper`` extra)."""
        return self.subtitles(self.words(**transcribe_opts), style=style)

    def resize(self, width: int, height: int) -> "Clip":
        self._need(VIDEO, what="resize")
        return self._new("resize", VIDEO, {"width": int(width), "height": int(height)}, [self.node])

    # ------------------------------------------------------------ anchors
    @property
    def marks(self) -> anchors.MarkerSet:
        """Named markers from ``<file>.marks.json``. Only on unmodified source clips."""
        if not self.is_source:
            raise EditError("marks live on source files; call .marks on videos[i], not on a derived clip")
        return anchors.MarkerSet(self.path)

    def _analysis_file(self) -> str:
        # Source clips are analysed on the original file (no scaling/padding artefacts);
        # derived clips on their preview render, so anchors are in the clip's own time.
        if self.is_source:
            return self.path
        return str(self.project.preview_renderer.render(self.node))

    def silences(self, min_len: TimeLike = "0.5s", threshold_db: float = -35.0) -> list[Span]:
        """Silent ranges, in this clip's own time. Feed straight into :meth:`remove`."""
        self._need(VIDEO, AUDIO, what="silences")
        if self.is_source and not self.node.params.get("has_audio") and self.kind == VIDEO:
            return [Span(0.0, self.duration, "silence")]
        return anchors.detect_silences(self._analysis_file(), self.duration, self._t(min_len), threshold_db,
                                       cache=self.project.cache)

    def scenes(self, threshold: float = 0.3) -> list[TimeRef]:
        """Scene-change points, in this clip's own time."""
        self._need(VIDEO, what="scenes")
        return anchors.detect_scenes(self._analysis_file(), threshold, cache=self.project.cache)

    def words(self, model: str = "base", language: str | None = None) -> anchors.Transcript:
        """Word-level transcript (needs the ``whisper`` extra)."""
        self._need(VIDEO, AUDIO, what="words")
        return anchors.transcribe(self._analysis_file(), model, language, cache=self.project.cache)

    # ------------------------------------------------------------- output
    def render(self, out: str = "out.mp4", **options) -> str:
        """Render at the project's current quality mode to ``out``. The extension picks the
        container: ``.mp4`` (H.264/AAC), ``.webm`` (VP9/Opus), ``.gif`` (``fps=``, ``width=``),
        ``.wav``/``.mp3``/``.m4a`` for audio, ``.png`` for images."""
        return str(self.project.renderer.export(self.node, out, **options))

    def preview(self) -> str:
        """Path of the cached preview render for this exact graph."""
        return str(self.project.preview_renderer.render(self.node))

    def frame_at(self, t: TimeLike, out: str | None = None, width: int | None = None) -> str:
        out = out or str(self.project.cache.scratch(f"frame-{self.node.hash}-{self._t(t):.3f}.jpg"))
        return str(self.project.preview_renderer.frame_at(self.node, self._t(t), out, width))

    def thumbnails(self, out: str | None = None, count: int = 8, width: int = 160) -> str:
        out = out or str(self.project.cache.scratch(f"thumbs-{self.node.hash}-{count}x{width}.png"))
        return str(self.project.preview_renderer.thumbnails(self.node, out, count, width))

    def describe(self) -> str:
        from .describe import describe
        return describe(self.node, self.project.profile)

    def to_json(self) -> dict:
        from .describe import to_json
        return to_json(self.node, self.project.profile)

    def timeline_png(self, out: str, width: int = 1200) -> str:
        from .describe import timeline_png
        return timeline_png(self.node, out, self.project.profile, width)

    def _repr_html_(self) -> str:
        from .notebook import clip_html
        return clip_html(self)


class Sequence:
    """An ordered list of clips (what :meth:`Clip.split_at` returns)."""

    def __init__(self, clips: Iterable[Clip], project: "Project"):
        self.clips = list(clips)
        self.project = project

    def __len__(self) -> int:
        return len(self.clips)

    def __iter__(self):
        return iter(self.clips)

    @overload
    def __getitem__(self, i: int) -> Clip: ...
    @overload
    def __getitem__(self, i: slice) -> "Sequence": ...

    def __getitem__(self, i):
        if isinstance(i, slice):
            return Sequence(self.clips[i], self.project)
        return self.clips[i]

    def get(self, i: int) -> Clip:
        try:
            return self.clips[i]
        except IndexError:
            raise IndexError(f"index {i} out of range; {len(self.clips)} items: {self.names}") from None

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.clips]

    @property
    def duration(self) -> float:
        return sum(c.duration for c in self.clips)

    def drop(self, *indexes: int) -> "Sequence":
        """Remove pieces by index (negative indexes allowed)."""
        n = len(self.clips)
        bad = {i % n if -n <= i < n else i for i in indexes}
        if any(i >= n or i < 0 for i in bad):
            raise IndexError(f"drop index out of range for {n} pieces: {indexes}")
        return Sequence([c for i, c in enumerate(self.clips) if i not in bad], self.project)

    def delete(self, *indexes: int) -> "Sequence":
        """Alias of :meth:`drop`."""
        return self.drop(*indexes)

    def keep(self, *indexes: int) -> "Sequence":
        n = len(self.clips)
        return Sequence([self.clips[i % n] for i in indexes], self.project)

    def map(self, fn: Callable[[Clip], Clip]) -> "Sequence":
        return Sequence([fn(c) for c in self.clips], self.project)

    def join(self, transition: str | None = None, duration: TimeLike = "0.5s") -> Clip:
        """Concatenate into one clip. With ``transition`` (``fade``, ``dissolve``, ``wipeleft``,
        ``slideright``, ``circleopen``... any ffmpeg xfade name) each cut becomes a transition of
        ``duration`` and the result is shorter by that much per cut."""
        if not self.clips:
            raise EditError("cannot join an empty sequence")
        kinds = {c.kind for c in self.clips}
        if kinds == {IMAGE}:
            raise EditError("images have no duration; use pictures.map(lambda p: p.as_clip('3s')).join()")
        if len(kinds) > 1:
            raise EditError(f"cannot join mixed kinds {sorted(kinds)}; join videos and sounds separately")
        if len(self.clips) == 1:
            return self.clips[0]
        kind = self.clips[0].kind
        if transition:
            if kind != VIDEO:
                raise EditError("transitions need video clips")
            if transition not in TRANSITIONS:
                raise EditError(f"unknown transition {transition!r}; one of {', '.join(sorted(TRANSITIONS))}")
            d = parse_time(duration, self.project.profile.fps)
            short = [c.name for c in self.clips if c.duration <= d]
            if short:
                raise EditError(f"transition of {d:.2f}s is longer than these clips: {short}")
            return Clip(make("xfade", VIDEO, {"transition": transition, "duration": d},
                             [c.node for c in self.clips]), self.project)
        return Clip(make("concat", kind, {}, [c.node for c in self.clips]), self.project)

    def concat(self) -> Clip:
        return self.join()

    def __repr__(self) -> str:
        return f"<Sequence {len(self.clips)} clips: {self.names}>"


class MediaList(Sequence):
    """``videos``, ``sounds``, ``pictures`` as passed to ``edit()``."""

    def __init__(self, clips: Iterable[Clip], project: "Project", label: str = "media"):
        super().__init__(clips, project)
        self.label = label

    def named(self, name: str) -> Clip:
        """Lookup by filename or unique substring."""
        exact = [c for c in self.clips if c.name == name]
        if exact:
            return exact[0]
        hits = [c for c in self.clips if name in c.name]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(f"{name!r} matches {len(hits)} of {self.label}: {self.names}")

    @overload
    def __getitem__(self, i: int) -> Clip: ...
    @overload
    def __getitem__(self, i: str) -> Clip: ...
    @overload
    def __getitem__(self, i: slice) -> Sequence: ...

    def __getitem__(self, i):
        if isinstance(i, str):
            return self.named(i)
        if isinstance(i, int) and not (-len(self.clips) <= i < len(self.clips)):
            raise IndexError(f"{self.label}[{i}] out of range; have {len(self.clips)}: {self.names}")
        return super().__getitem__(i)

    def __repr__(self) -> str:
        return f"<{self.label}: {self.names}>"
