"""Timeline as text, JSON, or PNG. This is what humans and LLM agents read back."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .graph import AUDIO, IMAGE, VIDEO, Node
from .media import Profile
from .timing import fmt_time


@dataclass
class Segment:
    track: str          # video | audio | overlay | text | fx
    out_start: float
    out_end: float
    source: str         # file name or label
    src_start: float | None
    src_end: float | None
    rate: float         # source seconds per output second (1.0 = normal speed)
    where: str          # script file:line that created this segment
    note: str = ""

    @property
    def duration(self) -> float:
        return self.out_end - self.out_start

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d


def _shift(segs: list[Segment], by: float) -> list[Segment]:
    return [Segment(s.track, s.out_start + by, s.out_end + by, s.source, s.src_start, s.src_end, s.rate,
                    s.where, s.note) for s in segs]


def _slice(segs: list[Segment], start: float, end: float, where: str) -> list[Segment]:
    out = []
    for s in segs:
        a, b = max(s.out_start, start), min(s.out_end, end)
        if b - a <= 1e-9:
            continue
        ss = se = None
        if s.src_start is not None:
            ss = s.src_start + (a - s.out_start) * s.rate
            se = s.src_start + (b - s.out_start) * s.rate
        w = where if s.track in ("video", "audio") and (a != s.out_start or b != s.out_end) else s.where
        out.append(Segment(s.track, a - start, b - start, s.source, ss, se, s.rate, w, s.note))
    return out


def segments(node: Node) -> list[Segment]:
    p, w = node.params, node.where()
    op = node.op
    if op == "source":
        track = "audio" if node.kind == AUDIO else "video"
        return [Segment(track, 0.0, node.duration, os.path.basename(p["path"]), 0.0, node.duration, 1.0, w)]
    if op == "image_clip":
        return [Segment("video", 0.0, node.duration, os.path.basename(node.inputs[0].params["path"]), None, None,
                        1.0, w, "still")]
    if op == "audio_of":
        return [Segment("audio", s.out_start, s.out_end, s.source, s.src_start, s.src_end, s.rate, s.where, s.note)
                for s in segments(node.inputs[0])]
    if op == "trim":
        return _slice(segments(node.inputs[0]), float(p["start"]), float(p["end"]), w)
    if op == "concat":
        out, t = [], 0.0
        for i in node.inputs:
            out.extend(_shift(segments(i), t))
            t += i.duration
        return out
    if op == "speed":
        f = float(p["factor"])
        return [Segment(s.track, s.out_start / f, s.out_end / f, s.source, s.src_start, s.src_end, s.rate * f,
                        s.where, s.note) for s in segments(node.inputs[0])]
    if op == "with_audio":
        base = segments(node.inputs[0])
        added = _shift(segments(node.inputs[1]), float(p["at"]))
        added = _slice(added, 0.0, node.duration, w)
        added = [Segment("audio", s.out_start, s.out_end, s.source, s.src_start, s.src_end, s.rate, w,
                         p["mode"] + (f" x{p['gain']:.2f}" if p.get("gain", 1.0) != 1.0 else "")) for s in added]
        return base + added
    if op == "overlay":
        base = segments(node.inputs[0])
        end = float(p["at"]) + float(p["duration"]) if p.get("duration") is not None else node.duration
        top = node.inputs[1]
        label = os.path.basename(top.params["path"]) if top.op == "source" else f"{top.op}:{top.hash}"
        return base + [Segment("overlay", float(p["at"]), min(end, node.duration), label, None, None, 1.0, w,
                               str(p["pos"]))]
    if op == "text":
        base = segments(node.inputs[0])
        end = float(p["at"]) + float(p["duration"]) if p.get("duration") is not None else node.duration
        return base + [Segment("text", float(p["at"]), min(end, node.duration), repr(p["text"]), None, None, 1.0, w,
                               str(p["pos"]))]
    if op == "fade":
        base = segments(node.inputs[0])
        fx = []
        if p.get("fade_in"):
            fx.append(Segment("fx", 0.0, float(p["fade_in"]), "fade in", None, None, 1.0, w))
        if p.get("fade_out"):
            fx.append(Segment("fx", node.duration - float(p["fade_out"]), node.duration, "fade out", None, None, 1.0, w))
        return base + fx
    if op == "volume":
        return segments(node.inputs[0]) + [Segment("fx", 0.0, node.duration, f"volume x{p['gain']:.2f}", None, None,
                                                   1.0, w)]
    if op == "resize":
        return segments(node.inputs[0]) + [Segment("fx", 0.0, node.duration, f"resize {p['width']}x{p['height']}",
                                                   None, None, 1.0, w)]
    raise ValueError(f"unknown op {op!r}")


_TRACK_ORDER = {"video": 0, "overlay": 1, "text": 2, "audio": 3, "fx": 4}


def describe(node: Node, profile: Profile | None = None) -> str:
    segs = sorted(segments(node), key=lambda s: (_TRACK_ORDER.get(s.track, 9), s.out_start))
    head = f"timeline  {fmt_time(node.duration)}  {node.kind}"
    if profile:
        head += f"  {profile.width}x{profile.height}@{profile.fps:g} ({profile.name})"
    rows = [head, f"{'track':8} {'out':23} {'source':40} {'where'}"]
    for s in segs:
        out = f"{fmt_time(s.out_start)}-{fmt_time(s.out_end)}"
        if s.src_start is not None:
            src = f"{s.source} [{fmt_time(s.src_start)}-{fmt_time(s.src_end)}]"
            if abs(s.rate - 1.0) > 1e-6:
                src += f" x{s.rate:g}"
        else:
            src = s.source
        if s.note:
            src += f" ({s.note})"
        rows.append(f"{s.track:8} {out:23} {src:40} {s.where}")
    return "\n".join(rows)


def to_json(node: Node, profile: Profile | None = None) -> dict:
    return {
        "duration": node.duration,
        "kind": node.kind,
        "profile": {"name": profile.name, "width": profile.width, "height": profile.height, "fps": profile.fps}
        if profile else None,
        "segments": [s.to_dict() for s in sorted(segments(node), key=lambda s: (_TRACK_ORDER.get(s.track, 9),
                                                                                  s.out_start))],
        "nodes": [{"hash": n.hash, "op": n.op, "kind": n.kind, "duration": n.duration, "where": n.where(),
                   "params": {k: v for k, v in n.params.items() if k not in ("sig",)},
                   "inputs": [i.hash for i in n.inputs]} for n in node.walk()],
    }


_COLORS = {"video": (76, 132, 255), "audio": (46, 184, 120), "overlay": (255, 170, 60), "text": (200, 120, 255),
           "fx": (150, 150, 150)}


def timeline_png(node: Node, out: str, profile: Profile | None = None, width: int = 1200) -> str:
    """Draw the timeline to a PNG (needs pillow)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ImportError("timeline_png needs pillow: pip install 'sceneoverflow[png]'") from None
    segs = segments(node)
    tracks = sorted({s.track for s in segs}, key=lambda t: _TRACK_ORDER.get(t, 9))
    lanes: dict[str, list[list[Segment]]] = {}
    for t in tracks:
        rows: list[list[Segment]] = []
        for s in sorted((x for x in segs if x.track == t), key=lambda x: x.out_start):
            for row in rows:
                if row[-1].out_end <= s.out_start + 1e-6:
                    row.append(s)
                    break
            else:
                rows.append([s])
        lanes[t] = rows
    left, top, row_h, gap = 90, 34, 30, 6
    total_rows = sum(len(r) for r in lanes.values())
    height = top + total_rows * (row_h + gap) + 30
    img = Image.new("RGB", (width, height), (24, 26, 32))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    except OSError:
        font = small = ImageFont.load_default()
    dur = max(node.duration, 1e-6)
    scale = (width - left - 20) / dur

    def x(t):
        return left + t * scale

    title = f"{fmt_time(node.duration)}"
    if profile:
        title += f"   {profile.width}x{profile.height}@{profile.fps:g} {profile.name}"
    d.text((left, 8), title, fill=(220, 220, 220), font=font)
    step = 1.0
    while dur / step > 24:
        step *= 2 if step < 10 else 1.5
    t = 0.0
    while t <= dur + 1e-6:
        d.line([(x(t), top - 6), (x(t), height - 24)], fill=(50, 52, 60))
        d.text((x(t) + 2, height - 20), fmt_time(t)[:-4] if step >= 1 else fmt_time(t), fill=(140, 140, 150),
               font=small)
        t += step
    y = top
    for track in tracks:
        for row in lanes[track]:
            d.text((8, y + 8), track, fill=(200, 200, 210), font=font)
            for s in row:
                x0, x1 = x(s.out_start), max(x(s.out_end), x(s.out_start) + 2)
                col = _COLORS.get(track, (120, 120, 120))
                d.rectangle([x0, y, x1, y + row_h], fill=col, outline=(24, 26, 32))
                label = s.source
                if s.src_start is not None:
                    label += f" {fmt_time(s.src_start)[:-4]}-{fmt_time(s.src_end)[:-4]}"
                label += f"  ·{s.where}"
                if x1 - x0 > 30:
                    d.text((x0 + 4, y + 3), label[: max(1, int((x1 - x0) / 6.5))], fill=(10, 10, 10), font=small)
                    if s.note:
                        d.text((x0 + 4, y + 16), s.note[: max(1, int((x1 - x0) / 6.5))], fill=(30, 30, 30), font=small)
            y += row_h + gap
    img.save(out)
    return out
