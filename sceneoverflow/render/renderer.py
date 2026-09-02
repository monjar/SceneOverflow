"""Turns a node graph into files.

Rules that keep this simple and fast:

* every intermediate conforms to the renderer's :class:`Profile` and is
  all-intra, so ``trim`` and ``concat`` are stream copies;
* only ops that need pixels (overlay, fade, speed, text, resize) re-encode,
  and only their own span;
* every node's output is cached by its content hash.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..graph import AUDIO, IMAGE, VIDEO, Node
from ..media import Profile, Source, ffprobe_json
from . import ffmpeg
from .cache import Cache

_MARGIN = 16
_POSITIONS = {
    "top-left": ("{m}", "{m}"),
    "top-right": ("W-w-{m}", "{m}"),
    "bottom-left": ("{m}", "H-h-{m}"),
    "bottom-right": ("W-w-{m}", "H-h-{m}"),
    "center": ("(W-w)/2", "(H-h)/2"),
    "top": ("(W-w)/2", "{m}"),
    "bottom": ("(W-w)/2", "H-h-{m}"),
}
_TEXT_POSITIONS = {k: (x.replace("w", "tw").replace("h", "th") if k != "center" else "(w-tw)/2", y.replace("h", "th") if k != "center" else "(h-th)/2") for k, (x, y) in _POSITIONS.items()}


def position_xy(pos, margin: int = _MARGIN, for_text: bool = False) -> tuple[str, str]:
    if isinstance(pos, (tuple, list)) and len(pos) == 2:
        return str(pos[0]), str(pos[1])
    table = _TEXT_POSITIONS if for_text else _POSITIONS
    if pos not in table:
        raise ValueError(f"unknown position {pos!r}; use one of {sorted(table)} or (x, y)")
    x, y = table[pos]
    if for_text:
        # drawtext uses w/h for the frame and tw/th for the text box
        x = x.replace("W", "w").replace("H", "h")
        y = y.replace("W", "w").replace("H", "h")
    return x.format(m=margin), y.format(m=margin)


def atempo_chain(factor: float) -> str:
    parts = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f *= 2.0
    parts.append(f"atempo={f:.6f}")
    return ",".join(parts)


@dataclass
class RenderStats:
    rendered: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        n = len(self.rendered) + len(self.cached)
        return len(self.cached) / n if n else 1.0

    def reset(self) -> None:
        self.rendered.clear()
        self.cached.clear()


class Renderer:
    def __init__(self, cache: Cache, profile: Profile, log=None):
        self.cache = cache
        self.profile = profile
        self.stats = RenderStats()
        self.log = log or (lambda msg: None)

    # ------------------------------------------------------------------ api
    def render(self, node: Node) -> Path:
        ext = self._ext(node)
        out = self.cache.render_path(self.profile.key, node.hash, ext)
        if out.exists() and out.stat().st_size > 0:
            self.stats.cached.append(node.hash)
            return out
        inputs = [self.render(i) for i in node.inputs]
        tmp = out.with_name(out.stem + ".part" + ext)
        self.log(f"render {node.op}:{node.hash} @{node.where()}")
        handler = getattr(self, f"_op_{node.op}")
        handler(node, inputs, tmp)
        os.replace(tmp, out)
        self.stats.rendered.append(node.hash)
        return out

    def export(self, node: Node, out_path: str | Path) -> Path:
        """Render then encode a delivery file (normal GOP, faststart)."""
        src = self.render(node)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if node.kind == AUDIO:
            args = ["-i", src]
            if out_path.suffix.lower() == ".wav":
                args += self.profile.wav_args()
            elif out_path.suffix.lower() == ".mp3":
                args += ["-c:a", "libmp3lame", "-q:a", "2"]
            else:
                args += ["-c:a", "aac", "-b:a", "192k"]
            ffmpeg.run(args + [out_path])
            return out_path
        if node.kind == IMAGE:
            shutil.copyfile(src, out_path)
            return out_path
        ffmpeg.run([
            "-i", src, "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path,
        ])
        return out_path

    def frame_at(self, node: Node, t: float, out_path: str | Path, width: int | None = None) -> Path:
        src = self.render(node)
        vf = f"scale={width}:-2" if width else "null"
        ffmpeg.run(["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-vf", vf, "-q:v", "3", out_path])
        return Path(out_path)

    def thumbnails(self, node: Node, out_path: str | Path, count: int = 8, width: int = 160) -> Path:
        """One PNG strip with ``count`` evenly spaced frames."""
        src = self.render(node)
        dur = max(node.duration, 0.001)
        step = dur / count
        vf = (f"fps=1/{step:.6f},scale={width}:-2,tile={count}x1")
        ffmpeg.run(["-i", src, "-vf", vf, "-frames:v", "1", "-t", f"{dur:.3f}", out_path])
        return Path(out_path)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _ext(node: Node) -> str:
        return {VIDEO: ".mp4", AUDIO: ".wav", IMAGE: ".png"}[node.kind]

    def _venc(self) -> list[str]:
        return self.profile.video_encode_args()

    def _aenc(self) -> list[str]:
        return self.profile.audio_encode_args()

    def _dims(self, path: Path) -> tuple[int, int]:
        info = ffprobe_json(path)
        v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
        return (int(v["width"]), int(v["height"])) if v else (0, 0)

    # ----------------------------------------------------------------- ops
    def _op_source(self, node: Node, inputs, out: Path) -> None:
        p = node.params
        path = p["path"]
        if node.kind == VIDEO:
            args = ["-i", path]
            if not p.get("has_audio"):
                args += ["-f", "lavfi", "-i", f"anullsrc=r={self.profile.sample_rate}:cl=stereo"]
            args += ["-map", "0:v:0", "-map", "1:a:0" if not p.get("has_audio") else "0:a:0",
                     "-vf", self.profile.conform_vf(), *self._venc(), *self._aenc(), "-shortest", out]
            ffmpeg.run(args)
        elif node.kind == AUDIO:
            ffmpeg.run(["-i", path, "-vn", *self.profile.wav_args(), out])
        else:  # image: copy as png, keep native size
            ffmpeg.run(["-i", path, "-frames:v", "1", out])

    def _op_image_clip(self, node: Node, inputs, out: Path) -> None:
        d = float(node.params["duration"])
        ffmpeg.run([
            "-loop", "1", "-framerate", str(self.profile.fps), "-t", f"{d:.3f}", "-i", inputs[0],
            "-f", "lavfi", "-t", f"{d:.3f}", "-i", f"anullsrc=r={self.profile.sample_rate}:cl=stereo",
            "-map", "0:v:0", "-map", "1:a:0", "-vf", self.profile.conform_vf(),
            *self._venc(), *self._aenc(), "-shortest", out,
        ])

    def _op_audio_of(self, node: Node, inputs, out: Path) -> None:
        ffmpeg.run(["-i", inputs[0], "-vn", *self.profile.wav_args(), out])

    def _op_trim(self, node: Node, inputs, out: Path) -> None:
        s, e = float(node.params["start"]), float(node.params["end"])
        args = ["-ss", f"{s:.6f}", "-i", inputs[0], "-t", f"{e - s:.6f}"]
        if node.kind == AUDIO:
            args += self.profile.wav_args()
        else:
            # video is all-intra so copying is frame-exact; audio is re-encoded
            # so the cut is sample-exact instead of AAC-frame-aligned.
            args += ["-c:v", "copy", *self._aenc(), "-avoid_negative_ts", "make_zero"]
        ffmpeg.run(args + [out])

    def _op_concat(self, node: Node, inputs, out: Path) -> None:
        if node.kind == VIDEO:
            dims = {self._dims(p) for p in inputs}
            if len(dims) > 1:
                return self._concat_filter(node, inputs, out)
        listing = self.cache.scratch(f"concat-{node.hash}.txt")
        with open(listing, "w") as f:
            for p in inputs:
                f.write("file '" + str(Path(p).resolve()).replace("'", "'\\''") + "'\n")
        args = ["-f", "concat", "-safe", "0", "-i", listing]
        args += ["-c", "copy"] if node.kind == VIDEO else self.profile.wav_args()
        ffmpeg.run(args + [out])
        listing.unlink(missing_ok=True)

    def _concat_filter(self, node: Node, inputs, out: Path) -> None:
        w, h = self._dims(inputs[0])
        vf = self.profile.conform_vf(w, h)
        parts, labels = [], ""
        for i in range(len(inputs)):
            parts.append(f"[{i}:v]{vf}[v{i}]")
            labels += f"[v{i}][{i}:a]"
        parts.append(f"{labels}concat=n={len(inputs)}:v=1:a=1[v][a]")
        args = []
        for p in inputs:
            args += ["-i", p]
        args += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", *self._venc(), *self._aenc(), out]
        ffmpeg.run(args)

    def _op_with_audio(self, node: Node, inputs, out: Path) -> None:
        p = node.params
        at_ms = int(round(float(p["at"]) * 1000))
        gain = float(p.get("gain", 1.0))
        mode = p["mode"]
        dur = node.duration
        a_in = f"[1:a]adelay={at_ms}|{at_ms},volume={gain:.4f}"
        if mode == "replace":
            fc = f"{a_in},apad[aout]"
        elif mode == "mix":
            fc = f"{a_in}[a1];[0:a][a1]amix=inputs=2:duration=first:normalize=0[aout]"
        elif mode == "duck":
            fc = (f"{a_in},asplit=2[a1][a2];"
                  f"[0:a][a1]sidechaincompress=threshold={p.get('duck_threshold', 0.03)}:ratio={p.get('duck_ratio', 8)}"
                  f":attack=20:release=400:makeup=1[base];"
                  f"[base][a2]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            raise ValueError(f"unknown with_audio mode {mode!r}")
        ffmpeg.run(["-i", inputs[0], "-i", inputs[1], "-filter_complex", fc,
                    "-map", "0:v:0", "-map", "[aout]", "-t", f"{dur:.6f}", "-c:v", "copy", *self._aenc(), out])

    def _op_volume(self, node: Node, inputs, out: Path) -> None:
        g = float(node.params["gain"])
        args = ["-i", inputs[0], "-af", f"volume={g:.4f}"]
        args += self.profile.wav_args() if node.kind == AUDIO else ["-c:v", "copy", *self._aenc()]
        ffmpeg.run(args + [out])

    def _op_overlay(self, node: Node, inputs, out: Path) -> None:
        p = node.params
        at = float(p["at"])
        end = at + float(p["duration"]) if p.get("duration") is not None else node.duration
        x, y = position_xy(p["pos"], p.get("margin", _MARGIN))
        pre = f"[1:v]scale={int(p['width'])}:-1[ov];" if p.get("width") else "[1:v]null[ov];"
        fc = f"{pre}[0:v][ov]overlay=x={x}:y={y}:enable='between(t,{at:.3f},{end:.3f})'[v]"
        ffmpeg.run(["-i", inputs[0], "-i", inputs[1], "-filter_complex", fc,
                    "-map", "[v]", "-map", "0:a:0", *self._venc(), "-c:a", "copy", out])

    def _op_speed(self, node: Node, inputs, out: Path) -> None:
        f = float(node.params["factor"])
        if node.kind == AUDIO:
            ffmpeg.run(["-i", inputs[0], "-af", atempo_chain(f), *self.profile.wav_args(), out])
            return
        fc = f"[0:v]setpts=PTS/{f:.6f}[v];[0:a]{atempo_chain(f)}[a]"
        ffmpeg.run(["-i", inputs[0], "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                    *self._venc(), *self._aenc(), out])

    def _op_fade(self, node: Node, inputs, out: Path) -> None:
        p = node.params
        dur = node.duration
        fi, fo = float(p.get("fade_in", 0)), float(p.get("fade_out", 0))
        vf, af = [], []
        if fi > 0:
            vf.append(f"fade=t=in:st=0:d={fi:.3f}")
            af.append(f"afade=t=in:st=0:d={fi:.3f}")
        if fo > 0:
            st = max(0.0, dur - fo)
            vf.append(f"fade=t=out:st={st:.3f}:d={fo:.3f}")
            af.append(f"afade=t=out:st={st:.3f}:d={fo:.3f}")
        if node.kind == AUDIO:
            ffmpeg.run(["-i", inputs[0], "-af", ",".join(af) or "anull", *self.profile.wav_args(), out])
            return
        ffmpeg.run(["-i", inputs[0], "-vf", ",".join(vf) or "null", "-af", ",".join(af) or "anull",
                    *self._venc(), *self._aenc(), out])

    def _op_resize(self, node: Node, inputs, out: Path) -> None:
        p = node.params
        ffmpeg.run(["-i", inputs[0], "-vf", self.profile.conform_vf(int(p["width"]), int(p["height"])),
                    *self._venc(), "-c:a", "copy", out])

    def _op_text(self, node: Node, inputs, out: Path) -> None:
        p = node.params
        if not ffmpeg.has_filter("drawtext"):
            raise ffmpeg.RenderError(["ffmpeg"], "this ffmpeg build lacks the drawtext filter (needs libfreetype)")
        at = float(p["at"])
        end = at + float(p["duration"]) if p.get("duration") is not None else node.duration
        x, y = position_xy(p["pos"], p.get("margin", _MARGIN), for_text=True)
        font = f"fontfile='{p['font']}'" if p.get("font") else "font=Sans"
        vf = (f"drawtext=text='{ffmpeg.escape_filter_text(p['text'])}':{font}:fontsize={int(p['size'])}"
              f":fontcolor={p['color']}:x={x}:y={y}:enable='between(t,{at:.3f},{end:.3f})'")
        if p.get("box"):
            vf += f":box=1:boxcolor={p['box']}:boxborderw=8"
        ffmpeg.run(["-i", inputs[0], "-vf", vf, *self._venc(), "-c:a", "copy", out])
