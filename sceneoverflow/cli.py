"""``sceneoverflow`` command line."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, agent
from .anchors import MarkerSet
from .media import MediaError, scan_dir
from .project import Project, run_script
from .render import ffmpeg
from .timing import TimeError, fmt_time, parse_time


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _media_arg(script: str | None, media: str | None) -> Path:
    if media:
        return Path(media)
    if script:
        guess = Path(script).resolve().parent / "media"
        if guess.is_dir():
            return guess
    if Path("media").is_dir():
        return Path("media")
    raise MediaError("no media directory found; pass --media DIR")


# ------------------------------------------------------------------ commands
def cmd_run(a) -> int:
    log = (lambda m: print("  " + m, file=sys.stderr)) if a.verbose else None
    media = _media_arg(a.script, a.media)
    t0 = time.time()
    res = run_script(a.script, media=media, out=a.out, mode=a.mode, cache_dir=a.cache, log=log)
    clip = res["clip"]
    desc = clip.describe()
    if a.png:
        clip.timeline_png(a.png)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"describe": desc, "timeline": clip.to_json(), "out": res["out"]}, f, indent=1)
    if not a.quiet:
        print(desc)
        stats = res["stats"]
        print(f"\n{len(stats.rendered)} rendered, {len(stats.cached)} cached, {time.time() - t0:.1f}s"
              + (f" -> {res['out']}" if res["out"] else ""))
        if a.png:
            print(f"timeline image -> {a.png}")
    return 0


def cmd_describe(a) -> int:
    target = Path(a.target)
    if target.suffix == ".py":
        media = _media_arg(str(target), a.media)
        res = run_script(target, media=media, cache_dir=a.cache)
        clip = res["clip"]
        if a.png:
            clip.timeline_png(a.png)
        print(json.dumps(clip.to_json(), indent=1) if a.json else clip.describe())
        return 0
    info = agent.analyze(target, words=a.words, cache_dir=a.cache)
    if a.json:
        print(json.dumps(info, indent=1))
        return 0
    for f in info["files"]:
        head = f"{f['name']}  {f['kind']}"
        if f["kind"] != "image":
            head += f"  {fmt_time(f['duration'])}"
        if f["width"]:
            head += f"  {f['width']}x{f['height']}"
        if f["fps"]:
            head += f"@{f['fps']:g}"
        if f["kind"] == "video":
            head += "  audio" if f["has_audio"] else "  NO AUDIO"
        print(head)
        if f["markers"]:
            print("  markers: " + ", ".join(f"{k}={fmt_time(v)}" for k, v in sorted(f["markers"].items(),
                                                                                    key=lambda kv: kv[1])))
        if f.get("silences"):
            print("  silences: " + ", ".join(f"{fmt_time(s['start'])}-{fmt_time(s['end'])}" for s in f["silences"]))
        if f.get("scenes"):
            print("  scenes: " + ", ".join(fmt_time(t) for t in f["scenes"]))
        if f.get("words"):
            print("  words: " + " ".join(w["text"] for w in f["words"])[:200])
        if f.get("words_error"):
            print("  words: " + f["words_error"])
    return 0


def _mpv_get(sock: socket.socket, prop: str):
    sock.sendall((json.dumps({"command": ["get_property", prop]}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf += chunk
    for line in buf.decode().splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "data" in msg:
            return msg["data"]
    return None


def _mpv_cmd(sock: socket.socket, *cmd) -> None:
    sock.sendall((json.dumps({"command": list(cmd)}) + "\n").encode())


def cmd_mark(a) -> int:
    ms = MarkerSet(a.file)
    if a.remove:
        ms.remove(a.remove)
        print(f"removed {a.remove}")
    if a.name and a.at is not None:
        t = ms.set(a.name, a.at)
        print(f"{a.name} = {fmt_time(float(t))}")
    if a.interactive:
        return _mark_interactive(a.file, ms)
    if a.list or not (a.remove or (a.name and a.at is not None)):
        if not len(ms):
            print(f"no markers on {os.path.basename(a.file)}")
        for name, t in ms.items():
            print(f"{fmt_time(float(t))}  {name}")
    return 0


def _mark_interactive(path: str, ms: MarkerSet) -> int:
    if not shutil.which("mpv"):
        return _err("interactive marking needs mpv on PATH (https://mpv.io)")
    sock_path = os.path.join(tempfile.mkdtemp(prefix="sceneoverflow-"), "mpv.sock")
    proc = subprocess.Popen(["mpv", "--really-quiet", "--keep-open=yes", f"--input-ipc-server={sock_path}", path])
    sock = socket.socket(socket.AF_UNIX)
    for _ in range(50):
        try:
            sock.connect(sock_path)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        return _err("could not connect to mpv")
    print("mpv is open. Commands here:  m <name>  mark now   |  space  pause  |  <  >  seek 1s  |  q  quit")
    try:
        while proc.poll() is None:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if line in ("q", "quit"):
                break
            if line in ("", " ", "space", "p"):
                _mpv_cmd(sock, "cycle", "pause")
            elif line in ("<", ","):
                _mpv_cmd(sock, "seek", -1, "relative")
            elif line in (">", "."):
                _mpv_cmd(sock, "seek", 1, "relative")
            elif line.startswith("m"):
                name = line[1:].strip() or f"m{len(ms) + 1}"
                t = _mpv_get(sock, "time-pos")
                if t is None:
                    print("  no position yet")
                    continue
                ms.set(name, float(t))
                print(f"  {name} = {fmt_time(float(t))}  (saved to {ms.path.name})")
            else:
                print("  ?")
    finally:
        try:
            _mpv_cmd(sock, "quit")
        except OSError:
            pass
        sock.close()
        proc.wait(timeout=5)
    return 0


def cmd_proxy(a) -> int:
    target = Path(a.target)
    project = Project(target if target.is_dir() else None, cache_dir=a.cache, mode=a.mode)
    clips = list(project.videos) + list(project.sounds) + list(project.pictures) if target.is_dir() \
        else [project.load(target)]
    for c in clips:
        t0 = time.time()
        p = project.renderer.render(c.node)
        print(f"{c.name:40} {time.time() - t0:5.1f}s  {p}")
    return 0


def cmd_frame(a) -> int:
    media = a.media if a.media else (str(_media_arg(a.target, None)) if a.target.endswith(".py") else None)
    out = a.out or f"frame-{parse_time(a.at):.3f}.jpg"
    print(agent.frame_at(a.target, a.at, media=media, out=out, width=a.width, cache_dir=a.cache))
    return 0


def cmd_thumbs(a) -> int:
    media = a.media if a.media else (str(_media_arg(a.target, None)) if a.target.endswith(".py") else None)
    out = a.out or "thumbs.png"
    print(agent.thumbnails(a.target, media=media, out=out, count=a.count, cache_dir=a.cache))
    return 0


def cmd_watch(a) -> int:
    from .watch import watch
    media = _media_arg(a.script, a.media)
    try:
        watch(a.script, media, out=a.out, png=a.png, mode=a.mode, cache_dir=a.cache, interval=a.interval,
              timeout=a.timeout, once=a.once)
    except KeyboardInterrupt:
        print()
    return 0


def cmd_studio(a) -> int:
    from .studio import StudioServer
    media = _media_arg(a.script, a.media)
    srv = StudioServer(a.script, media, cache_dir=a.cache, host=a.host, port=a.port, mode=a.mode)
    srv.start()
    print(f"studio at {srv.url}   (ctrl-c to stop)")
    if not a.no_open:
        import webbrowser
        webbrowser.open(srv.url)
    try:
        srv.wait()
    except KeyboardInterrupt:
        print()
    finally:
        srv.stop()
    return 0


_STARTER = '''"""Edit script. Run:  sceneoverflow studio edit.py   (or: sceneoverflow run edit.py -o out.mp4)"""
from sceneoverflow import Clip, MediaList, Project, edit


@edit
def edit(videos: MediaList, sounds: MediaList, pictures: MediaList, project: Project) -> Clip:
    v = videos[0]
    # v = v.remove(*v.silences())             # drop dead air
    # v = v.with_audio(sounds[0], gain=0.3)   # music bed
    return v
'''


def cmd_init(a) -> int:
    from .integrations import write_project_files
    written = write_project_files(a.dir, media=a.media, starter=_STARTER, force=a.force)
    for path, status in written:
        print(f"{status:8} {path}")
    print("\nnext: put media files in", Path(a.dir, a.media), "then  sceneoverflow studio edit.py")
    print("Claude Code in this directory now has the sceneoverflow MCP tools (see CLAUDE.md).")
    return 0


def cmd_mcp(a) -> int:
    from .mcp_server import serve
    serve(media=a.media, cache_dir=a.cache)
    return 0


def cmd_cache(a) -> int:
    from .render.cache import Cache
    c = Cache(a.cache)
    if a.clear:
        c.clear()
        print(f"cleared {c.root}")
    else:
        print(f"{c.root}  {c.size_bytes() / 1e6:.1f} MB")
    return 0


def cmd_api(a) -> int:
    if a.json:
        from .studio.catalog import catalog
        print(json.dumps(catalog(), indent=1))
        return 0
    print(agent.API_REFERENCE)
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sceneoverflow", description="Code-first video editing on ffmpeg.")
    p.add_argument("--version", action="version", version=f"sceneoverflow {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, script=True):
        sp.add_argument("--media", help="media directory (default: ./media next to the script)")
        sp.add_argument("--cache", help="cache directory (default: .sceneoverflow next to media)")

    s = sub.add_parser("run", help="run an edit script and export the result")
    s.add_argument("script")
    common(s)
    s.add_argument("-o", "--out", help="output file; omit to only render the preview graph")
    s.add_argument("--mode", choices=("preview", "final"), default="preview")
    s.add_argument("--png", help="also draw the timeline to this PNG")
    s.add_argument("--json", help="also write describe()+to_json() to this file")
    s.add_argument("-q", "--quiet", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="log every ffmpeg render")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("describe", help="describe a script's timeline, or analyse media files")
    s.add_argument("target", help="edit script (.py), a media file, or a media directory")
    common(s)
    s.add_argument("--json", action="store_true")
    s.add_argument("--png", help="(scripts) draw the timeline to this PNG")
    s.add_argument("--words", action="store_true", help="(media) transcribe with faster-whisper")
    s.set_defaults(fn=cmd_describe)

    s = sub.add_parser("mark", help="list, add, or interactively set named markers on a media file")
    s.add_argument("file")
    s.add_argument("--at", help="time, e.g. 12.5s or 1:02")
    s.add_argument("--name", help="marker name")
    s.add_argument("--remove", metavar="NAME")
    s.add_argument("--list", action="store_true")
    s.add_argument("-i", "--interactive", action="store_true", help="open mpv and mark while watching")
    s.set_defaults(fn=cmd_mark)

    s = sub.add_parser("proxy", help="pre-generate proxies for a media directory or file")
    s.add_argument("target")
    s.add_argument("--mode", choices=("preview", "final"), default="preview")
    s.add_argument("--cache")
    s.set_defaults(fn=cmd_proxy)

    s = sub.add_parser("frame", help="grab one frame as JPEG from a media file or a script's output")
    s.add_argument("target")
    s.add_argument("--at", required=True)
    s.add_argument("-o", "--out")
    s.add_argument("--width", type=int, default=640)
    common(s)
    s.set_defaults(fn=cmd_frame)

    s = sub.add_parser("thumbs", help="thumbnail strip of a media file or a script's output")
    s.add_argument("target")
    s.add_argument("-o", "--out")
    s.add_argument("--count", type=int, default=8)
    common(s)
    s.set_defaults(fn=cmd_thumbs)

    s = sub.add_parser("watch", help="re-run the script on every change and show the timeline diff")
    s.add_argument("script")
    common(s)
    s.add_argument("-o", "--out", help="preview file to rewrite on each run (default: preview.mp4)",
                   default="preview.mp4")
    s.add_argument("--png", help="timeline PNG to rewrite on each run")
    s.add_argument("--mode", choices=("preview", "final"), default="preview")
    s.add_argument("--interval", type=float, default=0.5)
    s.add_argument("--timeout", type=float, default=120.0)
    s.add_argument("--once", action="store_true", help="run once and exit (for scripting/tests)")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("studio", help="open the browser studio: editor, preview and timeline, live on save")
    s.add_argument("script")
    common(s)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--mode", choices=("preview", "final"), default="preview")
    s.add_argument("--no-open", action="store_true", help="do not open a browser")
    s.set_defaults(fn=cmd_studio)

    s = sub.add_parser("init", help="set up a project: media/, edit.py, .mcp.json and CLAUDE.md for Claude Code")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--media", default="media", help="media directory name (default: media)")
    s.add_argument("--force", action="store_true", help="overwrite existing files")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("mcp", help="serve the agent tools over MCP (stdio)")
    s.add_argument("--media")
    s.add_argument("--cache")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("cache", help="show or clear the render cache")
    s.add_argument("--cache")
    s.add_argument("--clear", action="store_true")
    s.set_defaults(fn=cmd_cache)

    s = sub.add_parser("api", help="print the scripting API cheat sheet")
    s.add_argument("--json", action="store_true", help="machine-readable catalog of methods and signatures")
    s.set_defaults(fn=cmd_api)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd not in ("api", "cache", "mark", "init") and not ffmpeg.available():
        return _err("ffmpeg and ffprobe must be on PATH")
    try:
        return a.fn(a)
    except (MediaError, TimeError, ffmpeg.RenderError, FileNotFoundError, KeyError, ValueError) as e:
        if os.environ.get("SCENEOVERFLOW_TRACEBACK"):  # watch/studio want the script line numbers
            import traceback
            traceback.print_exc()
        return _err(str(e))
