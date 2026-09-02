import json
import time
import urllib.error
import urllib.request

import pytest

from sceneoverflow.studio import StudioServer

pytestmark = pytest.mark.ffmpeg

SCRIPT = (
    "from sceneoverflow import edit\n\n"
    "@edit\n"
    "def edit(videos, sounds, pictures):\n"
    "    v = videos['a_intro'].head('5s').remove(('1s', '2s'))\n"
    "    return v.with_audio(sounds[0], gain=0.3)\n"
)


def _get(url, headers=None, method="GET", data=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _wait_state(url, pred, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = json.loads(_get(url + "api/state")[2])
        if pred(st):
            return st
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting; last state: {st}")


@pytest.fixture()
def studio(media_dir, tmp_path):
    script = tmp_path / "edit.py"
    script.write_text(SCRIPT)
    srv = StudioServer(script, media_dir, cache_dir=tmp_path / "cache", port=0, interval=0.2).start()
    try:
        yield srv
    finally:
        srv.stop()


def test_state_and_page(studio):
    url = studio.url
    st = _wait_state(url, lambda s: s["ok"])
    assert st["run_id"] == 1 and "edit.py:5" in st["describe"] and st["timeline"]["duration"] == pytest.approx(4.0)
    code, hdr, body = _get(url)
    assert code == 200 and b"SceneOverflow studio" in body and "text/html" in hdr["Content-Type"]
    code, _, body = _get(url + "api/script")
    assert code == 200 and body.decode() == SCRIPT
    files = json.loads(_get(url + "api/media")[2])
    assert {f["name"] for f in files} >= {"a_intro.mp4", "music.mp3", "logo.png"}


def test_preview_range_requests(studio):
    url = studio.url
    _wait_state(url, lambda s: s["ok"])
    code, hdr, body = _get(url + "media/preview.mp4", {"Range": "bytes=0-99"})
    assert code == 206 and len(body) == 100 and hdr["Content-Range"].startswith("bytes 0-99/")
    assert hdr["Accept-Ranges"] == "bytes"
    code, hdr, body = _get(url + "media/preview.mp4")
    assert code == 200 and int(hdr["Content-Length"]) == len(body) > 1000
    code, _, _ = _get(url + "media/preview.mp4", {"Range": "bytes=999999999-"})
    assert code == 416


def test_put_script_triggers_rerun_and_errors_point_at_lines(studio):
    url = studio.url
    _wait_state(url, lambda s: s["ok"])
    new = SCRIPT.replace("('1s', '2s')", "('1s', '3s')")
    code, _, body = _get(url + "api/script", method="PUT", data=new.encode())
    assert code == 200 and json.loads(body)["ok"]
    st = _wait_state(url, lambda s: s["ok"] and s["run_id"] >= 2 and not s["running"])
    assert st["timeline"]["duration"] == pytest.approx(3.0)
    assert studio.script.read_text() == new
    bad = SCRIPT.replace("('1s', '2s')", "('9s', '1s')")
    _get(url + "api/script", method="PUT", data=bad.encode())
    st = _wait_state(url, lambda s: not s["ok"] and not s["running"] and s["run_id"] >= 3)
    assert "span end must be after start" in st["error"] and 5 in st["error_lines"]


def test_mark_and_frame(studio, media_dir):
    url = studio.url
    _wait_state(url, lambda s: s["ok"])
    run = json.loads(_get(url + "api/state")[2])["run_id"]
    code, _, body = _get(url + "api/mark", method="POST",
                         data=json.dumps({"file": "a_intro.mp4", "name": "hook", "t": 2.5}).encode())
    assert code == 200 and json.loads(body)["markers"]["hook"] == 2.5
    assert (media_dir / "a_intro.mp4.marks.json").exists()
    _wait_state(url, lambda s: s["run_id"] > run and not s["running"])  # sidecar change re-triggers a run
    code, _, _ = _get(url + "api/mark", method="POST",
                      data=json.dumps({"file": "../etc/passwd", "name": "x", "t": 1}).encode())
    assert code == 400
    code, hdr, body = _get(url + "api/frame?t=1s&w=320")
    assert code == 200 and hdr["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
    code, _, _ = _get(url + "api/frame?t=nonsense")
    assert code == 400


def test_events_stream_sends_current_state(studio):
    _wait_state(studio.url, lambda s: s["ok"])
    req = urllib.request.Request(studio.url + "events")
    with urllib.request.urlopen(req, timeout=10) as r:
        line = r.readline().decode()
        assert line.startswith("event: update")
        data = r.readline().decode()
        assert data.startswith("data: ")
        assert json.loads(data[6:])["ok"]


def test_cli_studio_help():
    from sceneoverflow.cli import build_parser
    a = build_parser().parse_args(["studio", "x.py", "--port", "0", "--no-open"])
    assert a.port == 0 and a.no_open
