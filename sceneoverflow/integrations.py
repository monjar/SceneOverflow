"""Files that connect a project to Claude Code (and any other MCP client).

``sceneoverflow init`` writes them. Claude Code reads ``.mcp.json`` for project-scoped MCP
servers and ``CLAUDE.md`` for project instructions, so after ``init`` a Claude Code session
started in the project directory can analyse the media, edit the script, run it, and look
at frames without any further setup.
"""
from __future__ import annotations

import json
from pathlib import Path

from .agent import API_REFERENCE

MCP_SERVER_NAME = "sceneoverflow"


def mcp_config(media: str = "media") -> dict:
    return {"mcpServers": {MCP_SERVER_NAME: {"command": "sceneoverflow", "args": ["mcp", "--media", media]}}}


def claude_md(media: str = "media", script: str = "edit.py") -> str:
    return f"""# SceneOverflow project

This directory is a SceneOverflow video edit: the edit is the Python script `{script}`,
the footage lives in `{media}/`, and rendering is done by the `sceneoverflow` CLI on top
of ffmpeg. You (Claude Code) control the whole edit by editing the script and using the
`sceneoverflow` MCP tools declared in `.mcp.json`.

## Workflow

1. `analyze` (MCP) or `sceneoverflow describe {media}` to learn what footage exists:
   durations, markers, silences, scene changes, transcript words if available.
2. Edit `{script}`. Prefer anchors (`v.marks["name"]`, `v.silences()`, `v.scenes()`,
   `v.words().find("...")`) over typed seconds.
3. `run_edit` (MCP) or `sceneoverflow run {script}` to get the resulting timeline as
   text. Errors come back with the failing script line.
4. Look before you claim: `frame_at` / `thumbnails` (MCP) return images of the result.
5. If the user has `sceneoverflow studio {script}` open, every save of `{script}`
   re-runs it there automatically; `studio_state` (MCP) reports what the studio sees.
6. Export with `render` (MCP) or `sceneoverflow run {script} --mode final -o out.mp4`.

Do not hand-edit files under `.sceneoverflow/` (render cache) or `{media}/*.marks.json`
except through `set_marker` / `sceneoverflow mark`.

## Scripting API

```
{API_REFERENCE.rstrip()}
```
"""


def write_project_files(directory: str | Path, media: str = "media", starter: str = "", script: str = "edit.py",
                        force: bool = False) -> list[tuple[str, str]]:
    """Create the project skeleton. Returns ``[(path, "created"|"kept"|"updated"), ...]``."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out: list[tuple[str, str]] = []

    def put(rel: str, content: str) -> None:
        p = d / rel
        if p.exists() and not force:
            out.append((str(p), "kept"))
            return
        status = "updated" if p.exists() else "created"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        out.append((str(p), status))

    (d / media).mkdir(exist_ok=True)
    out.append((str(d / media), "dir"))
    put(script, starter)
    put(".mcp.json", json.dumps(mcp_config(media), indent=2) + "\n")
    cm = d / "CLAUDE.md"
    if cm.exists() and "SceneOverflow" not in cm.read_text() and not force:
        cm.write_text(cm.read_text().rstrip() + "\n\n" + claude_md(media, script))
        out.append((str(cm), "appended"))
    else:
        put("CLAUDE.md", claude_md(media, script))
    gi = d / ".gitignore"
    line = ".sceneoverflow/"
    if not gi.exists() or line not in gi.read_text():
        with open(gi, "a") as f:
            f.write(("" if not gi.exists() or gi.read_text().endswith("\n") else "\n") + line + "\n")
        out.append((str(gi), "updated"))
    return out
