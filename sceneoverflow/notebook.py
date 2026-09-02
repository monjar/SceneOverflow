"""Jupyter integration: a clip shows a player, a thumbnail strip, and its timeline."""
from __future__ import annotations

import base64
import html


def _data_uri(path: str, mime: str) -> str:
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def clip_html(clip, max_video_mb: float = 40.0) -> str:
    from .graph import AUDIO, IMAGE
    parts = [f"<div style='font-family:monospace;font-size:12px'>"]
    parts.append(f"<b>{html.escape(repr(clip))}</b><br>")
    if clip.kind == IMAGE:
        parts.append(f"<img src='{_data_uri(clip.path, 'image/png')}' style='max-width:480px'>")
    elif clip.kind == AUDIO:
        path = clip.preview()
        parts.append(f"<audio controls src='{_data_uri(path, 'audio/wav')}'></audio>")
    else:
        path = clip.preview()
        import os
        if os.path.getsize(path) <= max_video_mb * 1024 * 1024:
            parts.append(f"<video controls width='640' src='{_data_uri(path, 'video/mp4')}'></video><br>")
        else:
            parts.append(f"<i>preview too large to inline: {html.escape(path)}</i><br>")
        try:
            thumbs = clip.thumbnails()
            parts.append(f"<img src='{_data_uri(thumbs, 'image/png')}' style='max-width:100%'><br>")
        except Exception as e:  # pragma: no cover
            parts.append(f"<i>thumbnails failed: {html.escape(str(e))}</i><br>")
    if clip.kind != IMAGE:
        parts.append(f"<pre style='margin-top:6px'>{html.escape(clip.describe())}</pre>")
    parts.append("</div>")
    return "".join(parts)
