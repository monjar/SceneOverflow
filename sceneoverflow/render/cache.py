"""Content-addressed cache on disk.

Layout::

    <root>/probe/<sig>.json          ffprobe output per source file
    <root>/analysis/<key>.json       silences, scenes, transcripts
    <root>/render/<profile>/<hash>.<ext>   rendered intermediates
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DEFAULT_DIRNAME = ".sceneoverflow"


def _key(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:24]


class Cache:
    def __init__(self, root: str | Path | None = None):
        root = root or os.environ.get("SCENEOVERFLOW_CACHE") or os.path.join(os.getcwd(), DEFAULT_DIRNAME)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, *parts: str) -> Path:
        d = self.root.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- json ---------------------------------------------------------------
    def json_get(self, bucket: str, key: str) -> dict | list | None:
        p = self._dir(bucket) / f"{_key(key)}.json"
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return None

    def json_set(self, bucket: str, key: str, value) -> None:
        p = self._dir(bucket) / f"{_key(key)}.json"
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(value, f)
        os.replace(tmp, p)

    # -- rendered files -----------------------------------------------------
    def render_path(self, profile_key: str, node_hash: str, ext: str) -> Path:
        return self._dir("render", profile_key) / f"{node_hash}{ext}"

    def scratch(self, name: str) -> Path:
        return self._dir("tmp") / name

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def clear(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
