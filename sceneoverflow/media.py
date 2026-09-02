"""Source media: probing, profiles, and conformance rules.

Every source is conformed on import into a *proxy* that matches a
:class:`Profile`: fixed resolution (scale + pad), constant frame rate,
yuv420p, exactly one video stream and one 48 kHz stereo audio stream
(silence is injected when the source has none), and **all-intra** H.264.
All-intra is what makes every later cut a lossless stream copy.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path

from .graph import AUDIO, IMAGE, VIDEO

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


class MediaError(RuntimeError):
    pass


def kind_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXT:
        return VIDEO
    if ext in AUDIO_EXT:
        return AUDIO
    if ext in IMAGE_EXT:
        return IMAGE
    raise MediaError(f"unsupported media type: {path}")


def file_sig(path: str | Path) -> str:
    """Cheap identity for a file: path + size + mtime. Changes when the file does."""
    st = os.stat(path)
    return f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"


def ffprobe_json(path: str | Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise MediaError("ffprobe not found on PATH; install ffmpeg") from None
    except subprocess.CalledProcessError as e:
        raise MediaError(f"ffprobe failed for {path}: {e.stderr.strip()}") from None
    return json.loads(out)


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = stream.get(key)
        if v and v not in ("0/0", "0"):
            try:
                f = float(Fraction(v))
                if f > 0:
                    return f
            except (ValueError, ZeroDivisionError):
                pass
    return 30.0


@dataclass(frozen=True)
class Source:
    path: str
    kind: str
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    sig: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_probe(path: str | Path, info: dict) -> "Source":
        kind = kind_of(path)
        streams = info.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        dur = float(info.get("format", {}).get("duration") or 0.0)
        if kind == IMAGE:
            dur = 0.0
        if dur == 0.0 and kind != IMAGE:
            for s in (v, a):
                if s and s.get("duration"):
                    dur = max(dur, float(s["duration"]))
        # A still image inside an mp3 (cover art) is not video.
        is_video = bool(v) and kind == VIDEO
        return Source(
            path=os.path.abspath(str(path)), kind=kind, duration=dur,
            width=int(v.get("width", 0)) if v else 0,
            height=int(v.get("height", 0)) if v else 0,
            fps=_fps(v) if is_video else 0.0,
            has_audio=bool(a), has_video=is_video or kind == IMAGE,
            sig=file_sig(path),
        )


@dataclass(frozen=True)
class Profile:
    """Encoding target every intermediate conforms to."""

    name: str
    width: int
    height: int
    fps: float
    crf: int
    sample_rate: int = 48000
    preset: str = "veryfast"

    @property
    def key(self) -> str:
        return f"{self.name}-{self.width}x{self.height}@{round(self.fps, 3)}-crf{self.crf}"

    def conform_vf(self, width: int | None = None, height: int | None = None) -> str:
        w, h = width or self.width, height or self.height
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={self.fps},format=yuv420p,setsar=1"
        )

    def video_encode_args(self) -> list[str]:
        # All-intra: every frame is a keyframe, so any cut is a stream copy.
        return ["-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
                "-g", "1", "-keyint_min", "1", "-sc_threshold", "0", "-pix_fmt", "yuv420p"]

    def audio_encode_args(self) -> list[str]:
        return ["-c:a", "aac", "-b:a", "160k", "-ar", str(self.sample_rate), "-ac", "2"]

    def wav_args(self) -> list[str]:
        return ["-c:a", "pcm_s16le", "-ar", str(self.sample_rate), "-ac", "2"]


def preview_profile(fps: float = 30.0) -> Profile:
    return Profile("preview", 640, 360, fps, crf=23, preset="veryfast")


def final_profile(width: int, height: int, fps: float) -> Profile:
    width, height = max(2, width // 2 * 2), max(2, height // 2 * 2)
    return Profile("final", width, height, fps, crf=16, preset="medium")


def scan_dir(directory: str | Path) -> dict[str, list[str]]:
    """Group files in a directory by kind, sorted by name."""
    out: dict[str, list[str]] = {VIDEO: [], AUDIO: [], IMAGE: []}
    for p in sorted(Path(directory).iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            out[kind_of(p)].append(str(p.resolve()))
        except MediaError:
            continue
    return out
