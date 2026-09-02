"""Time expressions.

Every public API accepts a *time-like*: a number of seconds, a string literal
(``"12s"``, ``"500ms"``, ``"1:23.5"``, ``"01:02:03.25"``, ``"120f"``), a
:class:`TimeRef` or a :class:`Span` (its start). Frame literals need an fps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

_NUM = r"[0-9]+(?:\.[0-9]+)?"
_RE_S = re.compile(rf"^({_NUM})\s*s$")
_RE_MS = re.compile(rf"^({_NUM})\s*ms$")
_RE_F = re.compile(r"^([0-9]+)\s*f$")
_RE_CLOCK = re.compile(rf"^(?:(\d+):)?(\d+):({_NUM})$")


class TimeError(ValueError):
    pass


@dataclass(frozen=True)
class TimeRef:
    """A resolved point in time, in seconds, with an optional label.

    Supports ``+``/``-`` with any time-like so anchors compose:
    ``v.marks["intro"] + "0.5s"``.
    """

    seconds: float
    label: str | None = None

    def __float__(self) -> float:
        return float(self.seconds)

    def __add__(self, other: TimeLike) -> "TimeRef":
        return TimeRef(self.seconds + parse_time(other), self.label)

    def __radd__(self, other: TimeLike) -> "TimeRef":
        return self.__add__(other)

    def __sub__(self, other: TimeLike) -> "TimeRef":
        return TimeRef(self.seconds - parse_time(other), self.label)

    def __lt__(self, other: TimeLike) -> bool:
        return self.seconds < parse_time(other)

    def __repr__(self) -> str:
        if self.label:
            return f"TimeRef({fmt_time(self.seconds)}, {self.label!r})"
        return f"TimeRef({fmt_time(self.seconds)})"


@dataclass(frozen=True)
class Span:
    """A ``[start, end)`` range in seconds. ``remove()``/``trim()`` accept it directly."""

    start: float
    end: float
    label: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def mid(self) -> TimeRef:
        return TimeRef((self.start + self.end) / 2, self.label)

    @property
    def start_ref(self) -> TimeRef:
        return TimeRef(self.start, self.label)

    @property
    def end_ref(self) -> TimeRef:
        return TimeRef(self.end, self.label)

    def shift(self, by: float) -> "Span":
        return Span(self.start + by, self.end + by, self.label)

    def clip_to(self, lo: float, hi: float) -> "Span | None":
        s, e = max(self.start, lo), min(self.end, hi)
        if e <= s:
            return None
        return Span(s, e, self.label)

    def __repr__(self) -> str:
        tag = f", {self.label!r}" if self.label else ""
        return f"Span({fmt_time(self.start)}-{fmt_time(self.end)}{tag})"


TimeLike = Union[int, float, str, TimeRef, Span]


def parse_time(value: TimeLike, fps: float | None = None) -> float:
    """Convert a time-like to seconds. Raises :class:`TimeError` on bad input."""
    if isinstance(value, bool):
        raise TimeError(f"not a time: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise TimeError(f"negative time: {value}")
        return float(value)
    if isinstance(value, TimeRef):
        return float(value.seconds)
    if isinstance(value, Span):
        return float(value.start)
    if not isinstance(value, str):
        raise TimeError(f"not a time: {value!r}")
    s = value.strip()
    if m := _RE_S.match(s):
        return float(m.group(1))
    if m := _RE_MS.match(s):
        return float(m.group(1)) / 1000.0
    if m := _RE_F.match(s):
        if not fps:
            raise TimeError(f"frame literal {s!r} needs an fps")
        return int(m.group(1)) / float(fps)
    if m := _RE_CLOCK.match(s):
        h = int(m.group(1) or 0)
        return h * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    try:
        v = float(s)
    except ValueError:
        raise TimeError(
            f"cannot parse time {value!r}; use seconds, '12s', '500ms', '1:23.5' or '120f'"
        ) from None
    if v < 0:
        raise TimeError(f"negative time: {value!r}")
    return v


def parse_span(value, fps: float | None = None) -> Span:
    """A Span, or a ``(start, end)`` pair of time-likes."""
    if isinstance(value, Span):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        a, b = parse_time(value[0], fps), parse_time(value[1], fps)
        if b <= a:
            raise TimeError(f"span end must be after start: {value!r}")
        return Span(a, b)
    raise TimeError(f"expected a Span or (start, end) pair, got {value!r}")


def fmt_time(seconds: float) -> str:
    """``mm:ss.mmm`` (or ``h:mm:ss.mmm`` above one hour)."""
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h >= 1:
        return f"{int(h)}:{int(m):02d}:{s:06.3f}"
    return f"{int(m):02d}:{s:06.3f}"
