"""Thin ffmpeg subprocess wrapper."""
from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache


class RenderError(RuntimeError):
    def __init__(self, cmd: list[str], stderr: str):
        self.cmd = cmd
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-12:])
        super().__init__(f"ffmpeg failed:\n  {' '.join(cmd)}\n{tail}")


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(args: list[str], *, quiet: bool = True) -> str:
    """Run ``ffmpeg <args>``; returns stderr (ffmpeg logs there)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    if quiet:
        cmd += ["-loglevel", "error"]
    cmd += [str(a) for a in args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RenderError(cmd, "ffmpeg not found on PATH; install ffmpeg") from None
    if p.returncode != 0:
        raise RenderError(cmd, p.stderr)
    return p.stderr


def run_capture(args: list[str], loglevel: str = "info") -> str:
    """Run ffmpeg for analysis and return the full stderr log."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", loglevel] + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RenderError(cmd, p.stderr)
    return p.stderr


@lru_cache(maxsize=1)
def filters() -> set[str]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    names = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] and set(parts[0]) <= set("TSC."):
            names.add(parts[1])
    return names


def has_filter(name: str) -> bool:
    return name in filters()


def escape_filter_text(text: str) -> str:
    """Escape text for use inside a filter option value (drawtext etc.)."""
    out = []
    for ch in text:
        if ch in "\\':,[];":
            out.append("\\" + ch)
        elif ch == "%":
            out.append("\\%")
        else:
            out.append(ch)
    return "".join(out)
