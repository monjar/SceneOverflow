"""Public editing API: :class:`Clip`, :class:`Sequence`, :class:`MediaList`.

Every method returns a new object; nothing is rendered until you ask.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable, Iterable

from . import anchors
from .graph import AUDIO, IMAGE, VIDEO, Node, make
from .timing import Span, TimeLike, TimeRef, fmt_time, parse_span, parse_time

if TYPE_CHECKING:
    from .project import Project


class EditError(ValueError):
    pass


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
                width: int | None = None, margin: int = 16) -> "Clip":
        """Composite an image (or video) on top of this clip. ``pos`` is a name
        (top-left, top-right, bottom-left, bottom-right, center, top, bottom) or ``(x, y)``."""
        self._need(VIDEO, what="overlay")
        if top.kind not in (IMAGE, VIDEO):
            raise EditError("overlay needs an image or video clip")
        at_s = self._t(at)
        dur = None if for_ is None else self._t(for_)
        if at_s >= self.duration:
            raise EditError(f"overlay at {fmt_time(at_s)} is past the end of {self.name}")
        return self._new("overlay", VIDEO, {"at": at_s, "duration": dur, "pos": pos, "width": width,
                                            "margin": margin}, [self.node, top.node])

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
    def render(self, out: str = "out.mp4") -> str:
        """Render at the project's current quality mode to ``out``."""
        return str(self.project.renderer.export(self.node, out))

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

    def join(self) -> Clip:
        """Concatenate into one clip."""
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

    def __getitem__(self, i):
        if isinstance(i, str):
            return self.named(i)
        if isinstance(i, int) and not (-len(self.clips) <= i < len(self.clips)):
            raise IndexError(f"{self.label}[{i}] out of range; have {len(self.clips)}: {self.names}")
        return super().__getitem__(i)

    def __repr__(self) -> str:
        return f"<{self.label}: {self.names}>"
