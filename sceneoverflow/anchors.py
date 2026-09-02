"""Anchors: named, detected, or transcribed points in time.

Scripts should reference these instead of raw seconds wherever possible.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .render import ffmpeg
from .render.cache import Cache
from .timing import Span, TimeRef, fmt_time, parse_time

_SIL_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")
_PTS = re.compile(r"pts_time:\s*([0-9.]+)")


# ------------------------------------------------------------------ markers
class MarkerSet:
    """Named markers stored in a sidecar ``<media>.marks.json`` next to the file.

    ``v.marks["intro_end"]`` returns a :class:`TimeRef`. The file is plain JSON
    so it diffs well in git.
    """

    def __init__(self, media_path: str | Path):
        self.media_path = str(media_path)
        self.path = Path(self.media_path + ".marks.json")
        self._data: dict[str, float] = {}
        self.reload()

    def reload(self) -> None:
        if self.path.exists():
            with open(self.path) as f:
                raw = json.load(f)
            self._data = {str(k): float(v) for k, v in raw.get("markers", raw).items()}
        else:
            self._data = {}

    def __getitem__(self, name: str) -> TimeRef:
        if name not in self._data:
            known = ", ".join(sorted(self._data)) or "none"
            raise KeyError(f"no marker {name!r} on {os.path.basename(self.media_path)} (known: {known}); "
                           f"add one with: sceneoverflow mark {self.media_path} --at <time> --name {name}")
        return TimeRef(self._data[name], name)

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def __iter__(self):
        return iter(sorted(self._data, key=self._data.get))

    def __len__(self) -> int:
        return len(self._data)

    def items(self) -> list[tuple[str, TimeRef]]:
        return [(k, TimeRef(v, k)) for k, v in sorted(self._data.items(), key=lambda kv: kv[1])]

    def get(self, name: str, default=None):
        return TimeRef(self._data[name], name) if name in self._data else default

    def set(self, name: str, t, save: bool = True) -> TimeRef:
        self._data[name] = parse_time(t)
        if save:
            self.save()
        return TimeRef(self._data[name], name)

    def remove(self, name: str, save: bool = True) -> None:
        self._data.pop(name, None)
        if save:
            self.save()

    def span(self, start_name: str, end_name: str) -> Span:
        return Span(self._data[start_name], self._data[end_name], f"{start_name}..{end_name}")

    def save(self) -> None:
        with open(self.path, "w") as f:
            json.dump({"markers": dict(sorted(self._data.items(), key=lambda kv: kv[1]))}, f, indent=2)
            f.write("\n")

    def to_dict(self) -> dict:
        return dict(self._data)


# ----------------------------------------------------------------- detection
def detect_silences(path: str | Path, duration: float, min_len: float = 0.5, threshold_db: float = -35.0,
                    cache: Cache | None = None) -> list[Span]:
    key = f"silences|{path}|{os.path.getmtime(path)}|{min_len}|{threshold_db}"
    if cache and (hit := cache.json_get("analysis", key)) is not None:
        return [Span(a, b, "silence") for a, b in hit]
    log = ffmpeg.run_capture(["-i", str(path), "-vn", "-af", f"silencedetect=n={threshold_db}dB:d={min_len}",
                              "-f", "null", "-"])
    starts = [float(m.group(1)) for m in _SIL_START.finditer(log)]
    ends = [float(m.group(1)) for m in _SIL_END.finditer(log)]
    spans = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        s, e = max(0.0, s), min(duration, e)
        if e > s:
            spans.append(Span(s, e, "silence"))
    if cache:
        cache.json_set("analysis", key, [[s.start, s.end] for s in spans])
    return spans


def detect_scenes(path: str | Path, threshold: float = 0.3, cache: Cache | None = None) -> list[TimeRef]:
    key = f"scenes|{path}|{os.path.getmtime(path)}|{threshold}"
    if cache and (hit := cache.json_get("analysis", key)) is not None:
        return [TimeRef(t, f"scene{i + 1}") for i, t in enumerate(hit)]
    log = ffmpeg.run_capture(["-i", str(path), "-an", "-vf", f"select='gt(scene,{threshold})',showinfo",
                              "-fps_mode", "vfr", "-f", "null", "-"])
    times = sorted({round(float(m.group(1)), 3) for m in _PTS.finditer(log)})
    if cache:
        cache.json_set("analysis", key, times)
    return [TimeRef(t, f"scene{i + 1}") for i, t in enumerate(times)]


# ---------------------------------------------------------------- transcript
@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float

    @property
    def span(self) -> Span:
        return Span(self.start, self.end, self.text)


class Transcript:
    """Word-level transcript. ``find("thanks for watching")`` returns the Span of that phrase."""

    def __init__(self, words: list[Word]):
        self.words = list(words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def __len__(self) -> int:
        return len(self.words)

    def __iter__(self):
        return iter(self.words)

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9']+", " ", s.lower()).strip()

    def find(self, phrase: str, nth: int = 0) -> Span:
        target = self._norm(phrase).split()
        if not target:
            raise ValueError("empty phrase")
        n, hits = len(target), []
        toks = [self._norm(w.text) for w in self.words]
        for i in range(len(toks) - n + 1):
            if toks[i:i + n] == target:
                hits.append(Span(self.words[i].start, self.words[i + n - 1].end, phrase))
        if len(hits) <= nth:
            raise KeyError(f"phrase {phrase!r} not found in transcript (matches: {len(hits)})")
        return hits[nth]

    def find_all(self, phrase: str) -> list[Span]:
        out, i = [], 0
        while True:
            try:
                out.append(self.find(phrase, i))
                i += 1
            except KeyError:
                return out

    def between(self, start_phrase: str, end_phrase: str) -> Span:
        a, b = self.find(start_phrase), self.find(end_phrase)
        return Span(a.start, b.end, f"{start_phrase}..{end_phrase}")

    def to_list(self) -> list[dict]:
        return [{"text": w.text, "start": w.start, "end": w.end} for w in self.words]

    @staticmethod
    def from_list(items: list[dict]) -> "Transcript":
        return Transcript([Word(d["text"], float(d["start"]), float(d["end"])) for d in items])

    def __repr__(self) -> str:
        head = self.text[:60] + ("..." if len(self.text) > 60 else "")
        return f"Transcript({len(self.words)} words: {head!r})"


def transcribe(path: str | Path, model: str = "base", language: str | None = None,
               cache: Cache | None = None) -> Transcript:
    """Word-level transcription via faster-whisper (``pip install sceneoverflow[whisper]``)."""
    key = f"words|{path}|{os.path.getmtime(path)}|{model}|{language}"
    if cache and (hit := cache.json_get("analysis", key)) is not None:
        return Transcript.from_list(hit)
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        raise ImportError("transcription needs faster-whisper: pip install 'sceneoverflow[whisper]'") from None
    wm = WhisperModel(model, compute_type="int8")
    segments, _ = wm.transcribe(str(path), word_timestamps=True, language=language)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append(Word(w.word.strip(), float(w.start), float(w.end)))
    t = Transcript(words)
    if cache:
        cache.json_set("analysis", key, t.to_list())
    return t


def describe_spans(spans: list[Span]) -> str:
    return "\n".join(f"  {fmt_time(s.start)} - {fmt_time(s.end)}  ({s.duration:.2f}s) {s.label or ''}" for s in spans)
