"""MCP server exposing the agent tools (``pip install 'sceneoverflow[mcp]'``).

Run ``sceneoverflow mcp --media ./media`` and point an MCP client at it (stdio).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from . import agent


def build_server(media: str | None = None, cache_dir: str | None = None):
    try:
        from mcp.server.fastmcp import FastMCP, Image
    except ImportError:
        raise ImportError("the MCP server needs the mcp package: pip install 'sceneoverflow[mcp]'") from None

    mcp = FastMCP("sceneoverflow", instructions=agent.API_REFERENCE)
    default_media = str(Path(media).resolve()) if media else None

    def _media(m):
        return m or default_media

    @mcp.tool()
    def analyze(target: str | None = None, words: bool = False) -> str:
        """Describe media files (durations, markers, silences, scene changes, optional transcript).
        target: a file or directory; defaults to the project's media directory."""
        return json.dumps(agent.analyze(_media(target), words=words, cache_dir=cache_dir), indent=1)

    @mcp.tool()
    def api_reference() -> str:
        """The SceneOverflow scripting API cheat sheet."""
        return agent.API_REFERENCE

    @mcp.tool()
    def run_edit(code: str | None = None, script: str | None = None, media: str | None = None,
                 out: str | None = None, mode: str = "preview") -> str:
        """Run an edit script (inline code or a path) against the media directory and return the
        resulting timeline (text + JSON). Set out to also export a file. Errors come back as text."""
        res = agent.run_edit(script=script, code=code, media=_media(media), out=out, mode=mode,
                             cache_dir=cache_dir)
        res.pop("timeline", None) if not res.get("ok") else None
        return json.dumps(res, indent=1, default=str)

    @mcp.tool()
    def frame_at(target: str, t: str, media: str | None = None, width: int = 640):
        """A JPEG of the frame at time t (e.g. "12.5s") from a media file or a script's output."""
        path = agent.frame_at(target, t, media=_media(media), width=width, cache_dir=cache_dir)
        with open(path, "rb") as f:
            return Image(data=f.read(), format="jpeg")

    @mcp.tool()
    def thumbnails(target: str, media: str | None = None, count: int = 8):
        """A strip of evenly spaced frames from a media file or a script's output."""
        path = agent.thumbnails(target, media=_media(media), count=count, cache_dir=cache_dir)
        with open(path, "rb") as f:
            return Image(data=f.read(), format="png")

    @mcp.tool()
    def set_marker(path: str, name: str, at: str) -> str:
        """Add or move a named marker on a media file (stored in <file>.marks.json)."""
        return json.dumps(agent.set_marker(path, name, at))

    return mcp


def serve(media: str | None = None, cache_dir: str | None = None, transport: str = "stdio") -> None:
    build_server(media, cache_dir).run(transport=transport)
