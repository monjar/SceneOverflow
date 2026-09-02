"""``sceneoverflow watch``: re-run the script whenever it or the media changes.

Runs the script in a subprocess so a crash or hang in user code never takes the
watcher down. The cache makes each re-run cost only the nodes that changed.
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _snapshot(script: Path, media: Path) -> dict[str, float]:
    snap = {str(script): script.stat().st_mtime}
    for p in media.iterdir():
        if p.is_file() and not p.name.startswith("."):
            snap[str(p)] = p.stat().st_mtime
    return snap


def run_once(script: Path, media: Path, out: Path | None, png: Path | None, mode: str,
             cache_dir: Path | None, timeout: float | None = None) -> dict:
    """One run in a subprocess. Returns ``{"ok", "describe", "seconds", "log"}``."""
    with_json = Path(str(out or script) + ".timeline.json")
    cmd = [sys.executable, "-m", "sceneoverflow", "run", str(script), "--media", str(media),
           "--mode", mode, "--json", str(with_json), "--quiet"]
    if out:
        cmd += ["-o", str(out)]
    if png:
        cmd += ["--png", str(png)]
    if cache_dir:
        cmd += ["--cache", str(cache_dir)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "describe": "", "seconds": time.time() - t0,
                "log": f"timed out after {timeout}s (infinite loop in the script?)"}
    dt = time.time() - t0
    if p.returncode != 0:
        return {"ok": False, "describe": "", "seconds": dt, "log": (p.stderr or p.stdout).strip()}
    desc = ""
    if with_json.exists():
        with open(with_json) as f:
            desc = json.load(f).get("describe", "")
        with_json.unlink(missing_ok=True)
    return {"ok": True, "describe": desc, "seconds": dt, "log": p.stdout.strip()}


def watch(script: str | Path, media: str | Path, out: str | Path | None = None, png: str | Path | None = None,
          mode: str = "preview", cache_dir: str | Path | None = None, interval: float = 0.5,
          timeout: float | None = 120.0, once: bool = False, echo=print) -> None:
    script, media = Path(script).resolve(), Path(media).resolve()
    out = Path(out).resolve() if out else None
    png = Path(png).resolve() if png else None
    cache_dir = Path(cache_dir).resolve() if cache_dir else None
    prev_desc = None
    last = None
    echo(f"watching {script.name} + {media}  (ctrl-c to stop)")
    while True:
        snap = _snapshot(script, media)
        if snap != last:
            last = snap
            res = run_once(script, media, out, png, mode, cache_dir, timeout)
            stamp = time.strftime("%H:%M:%S")
            if not res["ok"]:
                echo(f"[{stamp}] FAILED in {res['seconds']:.1f}s\n{res['log']}")
            else:
                echo(f"[{stamp}] ok in {res['seconds']:.1f}s" + (f" -> {out}" if out else ""))
                if prev_desc is None:
                    echo(res["describe"])
                elif res["describe"] != prev_desc:
                    diff = difflib.unified_diff(prev_desc.splitlines(), res["describe"].splitlines(),
                                                "before", "after", lineterm="", n=1)
                    echo("\n".join(diff))
                else:
                    echo("  (timeline unchanged)")
                prev_desc = res["describe"]
            if once:
                return
        time.sleep(interval)
