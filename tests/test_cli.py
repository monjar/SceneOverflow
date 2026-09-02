import json
import subprocess
import sys
from pathlib import Path

import pytest

from sceneoverflow import agent
from sceneoverflow.cli import main

pytestmark = pytest.mark.ffmpeg

SCRIPT = (
    "from sceneoverflow import edit\n\n"
    "@edit\n"
    "def edit(videos, sounds, pictures):\n"
    "    v = videos['a_intro'].head('5s').remove(('1s', '2s'))\n"
    "    return v.with_audio(sounds[0], mode='duck')\n"
)


@pytest.fixture()
def script(tmp_path):
    p = tmp_path / "edit.py"
    p.write_text(SCRIPT)
    return p


def test_run_and_describe(script, media_dir, tmp_path, capsys):
    out = tmp_path / "o.mp4"
    png = tmp_path / "tl.png"
    js = tmp_path / "tl.json"
    assert main(["run", str(script), "--media", str(media_dir), "--cache", str(tmp_path / "c"), "-o", str(out),
                 "--png", str(png), "--json", str(js)]) == 0
    text = capsys.readouterr().out
    assert "timeline  00:04.000" in text and "edit.py:5" in text and "duck" in text
    assert out.exists() and png.exists()
    assert json.load(open(js))["timeline"]["duration"] == pytest.approx(4.0)
    assert main(["describe", str(script), "--media", str(media_dir), "--cache", str(tmp_path / "c"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["duration"] == pytest.approx(4.0)


def test_describe_media(media_dir, tmp_path, capsys):
    assert main(["describe", str(media_dir), "--cache", str(tmp_path / "c")]) == 0
    text = capsys.readouterr().out
    assert "c_gap.mp4" in text and "silences:" in text and "scenes: 00:05.000" in text and "NO AUDIO" in text


def test_mark_cli(media_dir, capsys):
    f = str(media_dir / "c_gap.mp4")
    assert main(["mark", f, "--at", "2.5s", "--name", "hook"]) == 0
    assert main(["mark", f, "--list"]) == 0
    assert "00:02.500  hook" in capsys.readouterr().out
    assert main(["mark", f, "--remove", "hook"]) == 0


def test_frame_and_thumbs(script, media_dir, tmp_path, capsys):
    out = tmp_path / "f.jpg"
    assert main(["frame", str(script), "--at", "1s", "--media", str(media_dir), "--cache", str(tmp_path / "c"),
                 "-o", str(out)]) == 0
    assert out.stat().st_size > 500
    out2 = tmp_path / "t.png"
    assert main(["thumbs", str(media_dir / "a_intro.mp4"), "-o", str(out2), "--cache", str(tmp_path / "c")]) == 0
    assert out2.stat().st_size > 500


def test_bad_script_reports_error(media_dir, tmp_path, capsys):
    p = tmp_path / "bad.py"
    p.write_text("from sceneoverflow import edit\n@edit\ndef edit(videos):\n    return videos[0].trim('5s', '999s')\n")
    assert main(["run", str(p), "--media", str(media_dir), "--cache", str(tmp_path / "c")]) == 1
    assert "past the end" in capsys.readouterr().err


def test_watch_once_via_subprocess(script, media_dir, tmp_path):
    from sceneoverflow.watch import run_once, watch
    res = run_once(script, media_dir, tmp_path / "p.mp4", None, "preview", tmp_path / "c")
    assert res["ok"], res["log"]
    assert "timeline  00:04.000" in res["describe"]
    lines = []
    watch(script, media_dir, out=tmp_path / "p2.mp4", cache_dir=tmp_path / "c", once=True, echo=lines.append)
    assert any("ok in" in ln for ln in lines) and any("timeline" in ln for ln in lines)


def test_agent_run_edit_inline_and_errors(media_dir, tmp_path):
    res = agent.run_edit(code=SCRIPT, media=media_dir, cache_dir=tmp_path / "c")
    assert res["ok"] and res["timeline"]["duration"] == pytest.approx(4.0) and Path(res["preview"]).exists()
    bad = agent.run_edit(code="from sceneoverflow import edit\n@edit\ndef edit(v):\n    return v[0].trim('9s','1s')\n",
                         media=media_dir, cache_dir=tmp_path / "c")
    assert not bad["ok"] and "empty range" in bad["error"] and bad["script_lines"]
    info = agent.analyze(media_dir / "b_scenes.mp4", cache_dir=tmp_path / "c")
    assert info["files"][0]["scenes"] == [pytest.approx(5.0, abs=0.1)]


def test_module_entrypoint():
    out = subprocess.run([sys.executable, "-m", "sceneoverflow", "api"], capture_output=True, text=True)
    assert out.returncode == 0 and ".with_audio" in out.stdout
