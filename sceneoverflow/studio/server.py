"""The studio: a local web page that re-runs the script on save and shows the result.

Stdlib only. ``ThreadingHTTPServer`` serves one HTML page, a JSON API, the preview
file with HTTP Range support (browsers need it to seek), and a Server-Sent Events
stream that pushes every new run to the page. The watch loop is the same subprocess
runner as ``sceneoverflow watch``.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..anchors import MarkerSet
from ..media import MediaError, kind_of, scan_dir
from ..project import Project, run_script
from ..timing import TimeError, parse_time
from ..watch import _snapshot, run_once

_HERE = Path(__file__).parent
_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')


class StudioServer:
    def __init__(self, script: str | Path, media: str | Path, cache_dir: str | Path | None = None,
                 host: str = "127.0.0.1", port: int = 8765, mode: str = "preview", interval: float = 0.5,
                 timeout: float = 120.0):
        self.script = Path(script).resolve()
        self.media = Path(media).resolve()
        if not self.script.exists():
            raise FileNotFoundError(self.script)
        if not self.media.is_dir():
            raise MediaError(f"media directory not found: {self.media}")
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else self.media.parent / ".sceneoverflow"
        self.studio_dir = self.cache_dir / "studio"
        self.studio_dir.mkdir(parents=True, exist_ok=True)
        self.preview_path = self.studio_dir / "preview.mp4"
        self.mode, self.interval, self.timeout = mode, interval, timeout
        self.host, self.port = host, port
        self.state: dict = {"ok": False, "run_id": 0, "running": True, "describe": "", "timeline": None,
                            "error": None, "error_lines": [], "log": "", "seconds": 0.0,
                            "script": self.script.name, "media": str(self.media)}
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._stop = threading.Event()
        self._force = threading.Event()
        self._graph = None  # (script mtime, snapshot key, clip) for frame grabs
        self._httpd: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ lifecycle
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> "StudioServer":
        handler = type("StudioHandler", (_Handler,), {"studio": self})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        t1 = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="studio-http")
        t2 = threading.Thread(target=self._watch_loop, daemon=True, name="studio-watch")
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        return self

    def wait(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        for q in list(self._subs):
            q.put(None)

    def rerun(self) -> None:
        self._force.set()

    # ------------------------------------------------------------ watching
    def _watch_loop(self) -> None:
        last = None
        while not self._stop.is_set():
            try:
                snap = _snapshot(self.script, self.media)
            except FileNotFoundError:
                snap = last
            if snap != last or self._force.is_set():
                self._force.clear()
                last = snap
                self._set(running=True)
                res = run_once(self.script, self.media, self.preview_path, None, self.mode, self.cache_dir,
                               self.timeout)
                upd = {"ok": res["ok"], "running": False, "describe": res["describe"], "seconds": res["seconds"],
                       "log": res["log"], "run_id": self.state["run_id"] + 1, "finished": time.time()}
                if res["ok"]:
                    upd.update(timeline=res["timeline"], error=None, error_lines=[],
                               preview=f"/media/preview.mp4?r={upd['run_id']}")
                else:
                    lines = [int(n) for f, n in _LINE_RE.findall(res["log"]) if Path(f).name == self.script.name]
                    tail = [ln for ln in res["log"].splitlines() if ln.strip()]
                    upd.update(error=tail[-1] if tail else "failed", error_lines=lines)
                self._set(**upd)
            self._stop.wait(self.interval)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)
            payload = json.dumps(self.state)
            subs = list(self._subs)
        for q in subs:
            q.put(payload)

    def snapshot_json(self) -> str:
        with self._lock:
            return json.dumps(self.state)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    # ------------------------------------------------------------ helpers
    def clip(self):
        """The current graph, imported in-process (for frame grabs). Cached per script version."""
        key = (self.script.stat().st_mtime, tuple(sorted(_snapshot(self.script, self.media).items())))
        if self._graph and self._graph[0] == key:
            return self._graph[1]
        clip = run_script(self.script, media=self.media, mode="preview", cache_dir=self.cache_dir)["clip"]
        self._graph = (key, clip)
        return clip

    def media_info(self) -> list[dict]:
        project = Project(self.media, cache_dir=self.cache_dir)
        out = []
        for c in list(project.videos) + list(project.sounds) + list(project.pictures):
            out.append({"name": c.name, "path": c.path, "kind": c.kind, "duration": c.duration,
                        "markers": MarkerSet(c.path).to_dict()})
        return out

    def resolve_media(self, name: str) -> Path:
        p = (self.media / Path(name).name).resolve()
        if p.parent != self.media or not p.is_file():
            raise MediaError(f"not a media file in {self.media}: {name}")
        kind_of(p)
        return p


class _Handler(BaseHTTPRequestHandler):
    studio: StudioServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode())

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send_file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            return self._error(404, "not found")
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:  # suffix range
                    start = max(0, size - int(b))
            except ValueError:
                return self._error(416, "bad range")
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # ---- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        s = self.studio
        try:
            if u.path == "/":
                html = (_HERE / "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._send(200, s.snapshot_json().encode())
            if u.path == "/api/script":
                return self._send(200, s.script.read_bytes(), "text/plain; charset=utf-8")
            if u.path == "/api/media":
                return self._json(s.media_info())
            if u.path == "/api/frame":
                t = parse_time(q.get("t", ["0"])[0])
                width = int(q.get("w", ["640"])[0])
                clip = s.clip()
                out = s.studio_dir / f"frame-{clip.node.hash}-{t:.3f}-{width}.jpg"
                if not out.exists():
                    clip.frame_at(min(t, max(0.0, clip.duration - 0.05)), str(out), width)
                return self._send_file(out, "image/jpeg")
            if u.path == "/api/catalog":
                from .catalog import catalog
                return self._json(catalog(Project(s.media, cache_dir=s.cache_dir)))
            if u.path == "/api/thumbs":
                count = max(4, min(60, int(q.get("n", ["24"])[0])))
                clip = s.clip()
                out = s.studio_dir / f"thumbs-{clip.node.hash}-{count}.png"
                if not out.exists():
                    clip.thumbnails(str(out), count=count, width=96)
                return self._send_file(out, "image/png")
            if u.path == "/media/preview.mp4":
                return self._send_file(s.preview_path, "video/mp4")
            if u.path == "/events":
                return self._events()
            return self._error(404, "not found")
        except (TimeError, MediaError, ValueError) as e:
            return self._error(400, str(e))
        except Exception as e:  # keep the server alive; show the error to the page
            return self._error(500, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

    def do_PUT(self):
        if urlparse(self.path).path != "/api/script":
            return self._error(404, "not found")
        text = self._body().decode("utf-8")
        tmp = self.studio.script.with_suffix(".py.tmp")
        tmp.write_text(text)
        os.replace(tmp, self.studio.script)
        self.studio.rerun()
        return self._json({"ok": True, "bytes": len(text)})

    def do_POST(self):
        u = urlparse(self.path)
        s = self.studio
        try:
            data = json.loads(self._body() or b"{}")
            if u.path == "/api/mark":
                path = s.resolve_media(data["file"])
                ms = MarkerSet(path)
                if data.get("remove"):
                    ms.remove(data["name"])
                else:
                    ms.set(data["name"], data["t"])
                s.rerun()
                return self._json({"ok": True, "file": path.name, "markers": ms.to_dict()})
            if u.path == "/api/rerun":
                s.rerun()
                return self._json({"ok": True})
            return self._error(404, "not found")
        except (KeyError, TimeError, MediaError, ValueError) as e:
            return self._error(400, str(e))

    def _events(self):
        s = self.studio
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = s.subscribe()
        try:
            self.wfile.write(f"event: update\ndata: {s.snapshot_json()}\n\n".encode())
            self.wfile.flush()
            while not s._stop.is_set():
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if msg is None:
                    break
                self.wfile.write(f"event: update\ndata: {msg}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            s.unsubscribe(q)
