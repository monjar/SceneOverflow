"""Immutable op graph.

A :class:`Node` is ``(op, params, inputs)``. Its content hash identifies the
rendered bytes, so any node with the same hash is served from cache. Every node
records where in the user's script it was created (:class:`Provenance`) so the
inspector can map timeline segments back to source lines.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO = "video"
AUDIO = "audio"
IMAGE = "image"


@dataclass(frozen=True)
class Provenance:
    file: str
    line: int
    function: str

    def short(self) -> str:
        return f"{os.path.basename(self.file)}:{self.line}"


def capture_provenance() -> Provenance | None:
    """First stack frame outside this package (i.e. the user's script)."""
    for fi in inspect.stack()[1:]:
        fn = os.path.abspath(fi.filename)
        if fn.startswith(_PKG_DIR):
            continue
        if "importlib" in fn or fn.startswith("<"):
            continue
        return Provenance(fn, fi.lineno, fi.function)
    return None


def _canon(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _canon(v[k]) for k in sorted(v)}
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    return str(v)


@dataclass(frozen=True)
class Node:
    op: str
    kind: str
    params: dict = field(default_factory=dict)
    inputs: tuple = ()
    provenance: Provenance | None = field(default=None, compare=False, hash=False)

    def __post_init__(self):
        object.__setattr__(self, "params", dict(self.params))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    @cached_property
    def hash(self) -> str:
        payload = json.dumps(
            {"op": self.op, "kind": self.kind, "params": _canon(self.params),
             "inputs": [n.hash for n in self.inputs]},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def duration(self) -> float:
        return duration_of(self)

    def where(self) -> str:
        return self.provenance.short() if self.provenance else "?"

    def walk(self):
        seen = set()

        def _walk(n):
            if n.hash in seen:
                return
            seen.add(n.hash)
            for i in n.inputs:
                yield from _walk(i)
            yield n

        yield from _walk(self)

    def __repr__(self) -> str:
        return f"Node({self.op}:{self.hash} {self.kind} @{self.where()})"


def duration_of(n: Node) -> float:
    """Output duration of a node, in seconds. Pure function of the graph."""
    p = n.params
    if n.op in ("source", "image_clip"):
        return float(p["duration"])
    if n.op == "trim":
        return float(p["end"]) - float(p["start"])
    if n.op == "concat":
        return sum(duration_of(i) for i in n.inputs)
    if n.op == "speed":
        return duration_of(n.inputs[0]) / float(p["factor"])
    if n.op == "with_audio":
        base = duration_of(n.inputs[0])
        if p.get("extend"):
            return max(base, float(p["at"]) + duration_of(n.inputs[1]))
        return base
    if n.op in ("overlay", "fade", "resize", "text", "volume", "mix"):
        return duration_of(n.inputs[0])
    raise ValueError(f"unknown op {n.op!r}")


def make(op: str, kind: str, params: dict | None = None, inputs=(), prov: Provenance | None = None) -> Node:
    return Node(op=op, kind=kind, params=params or {}, inputs=tuple(inputs),
                provenance=prov if prov is not None else capture_provenance())
