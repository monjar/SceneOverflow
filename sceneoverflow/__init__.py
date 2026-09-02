"""SceneOverflow: code-first video editing on top of ffmpeg.

    from sceneoverflow import edit

    @edit
    def edit(videos, sounds, pictures):
        v = videos[0].remove(("12s", "14s"))
        return v.with_audio(sounds.join())

Run with ``sceneoverflow run script.py --media ./media -o out.mp4``.
"""
from .anchors import MarkerSet, Transcript, Word
from .clip import Clip, EditError, MediaList, Sequence
from .graph import Node
from .media import MediaError, Profile, Source
from .project import Project, edit, load_script, run_script
from .render.ffmpeg import RenderError
from .timing import Span, TimeError, TimeRef, fmt_time, parse_time

__version__ = "0.1.0"
__all__ = [
    "Clip", "Sequence", "MediaList", "Project", "edit", "run_script", "load_script",
    "Span", "TimeRef", "MarkerSet", "Transcript", "Word", "Node", "Profile", "Source",
    "EditError", "MediaError", "RenderError", "TimeError", "parse_time", "fmt_time",
]
