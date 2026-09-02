"""The studio's pure JS (highlighter, completion logic) exercised in node, when node is available."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "sceneoverflow" / "studio" / "index.html"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _js(script: str) -> dict:
    src = PAGE.read_text()
    a, b = src.index("// @pure-start"), src.index("// @pure-end")
    hl_a, hl_b = src.index("  const hl = $('hl');"), src.index("  function renderHighlight")
    pre = "const $ = () => ({});\n" + src[hl_a:hl_b].replace("const hl = $('hl');", "") + src[a:b]
    out = subprocess.run(["node", "-e", pre + "\n" + script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


CAT = {
    "clip": [{"name": "trim", "kind": "method", "sig": "(start=0, end=None)", "doc": "Keep a range.",
              "params": [{"name": "start", "default": "0"}, {"name": "end", "default": "None"}]},
             {"name": "duration", "kind": "property", "sig": "", "doc": "", "params": []},
             {"name": "overlay", "kind": "method", "sig": "(top, at=0, pos='top-right')", "doc": "",
              "params": [{"name": "top"}, {"name": "at", "default": "0"}, {"name": "pos", "default": "'top-right'"}]}],
    "sequence": [{"name": "join", "kind": "method", "sig": "(transition=None, duration='0.5s')", "doc": "",
                  "params": [{"name": "transition", "default": "None"}, {"name": "duration", "default": "'0.5s'"}]},
                 {"name": "drop", "kind": "method", "sig": "(*indexes)", "doc": "", "params": [{"name": "*indexes"}]}],
    "medialist": [{"name": "named", "kind": "method", "sig": "(name)", "doc": "", "params": [{"name": "name"}]}],
    "project": [{"name": "title", "kind": "method", "sig": "(text, duration='3s')", "doc": "",
                 "params": [{"name": "text"}, {"name": "duration", "default": "'3s'"}]}],
    "markers": [], "transcript": [{"name": "find", "kind": "method", "sig": "(phrase)", "doc": "", "params": [{"name": "phrase"}]}],
    "transitions": ["fade", "dissolve", "wipeleft"], "positions": ["top-left", "top-right", "center"],
    "audio_modes": ["mix", "replace", "duck"],
    "media": {"videos": ["talk.mp4", "broll.mp4"], "sounds": ["music.mp3"], "pictures": ["logo.png"]},
    "marker_names": {"talk.mp4": ["intro_end", "outro"]},
}


def run(text: str, pos: int | None = None):
    pos = len(text) if pos is None else pos
    return _js(f"const cat = {json.dumps(CAT)}; const t = {json.dumps(text)}; const ctx = completionContext(t, {pos});"
               "console.log(JSON.stringify({ctx, items: completions(cat, ctx).map(i => i.label)}));")


def test_method_completion_by_receiver_kind():
    r = run("v = videos[0].tr")
    assert r["ctx"]["cls"] == "clip" and r["items"] == ["trim"]
    r = run("videos[0].cut('1s').")
    assert r["ctx"]["cls"] == "sequence" and set(r["items"]) == {"join", "drop"}
    r = run("videos[0].cut('1s').join().")
    assert r["ctx"]["cls"] == "clip" and "trim" in r["items"] and "join" not in r["items"]
    r = run("videos.")
    assert r["ctx"]["cls"] == "medialist" and set(r["items"]) == {"named", "join", "drop"}
    r = run("    card = project.ti")
    assert r["items"] == ["title"]
    r = run("talk.words().fi")
    assert r["items"] == ["find"]
    assert run("x = 3.")["items"] == []            # a float literal is not a receiver
    assert run("talk.marks[\"a\"].")["ctx"]["cls"] == "clip"


def test_string_and_param_completion():
    assert run('v = videos["t')["items"] == ["talk.mp4"]
    assert run("s = sounds['")["items"] == ["music.mp3"]
    assert run('talk.trim(talk.marks["o')["items"] == ["outro"]
    assert run('seq.join(transition="w')["items"] == ["wipeleft"]
    assert run("v.overlay(logo, pos='")["items"] == ["top-left", "top-right", "center"]
    r = run("v.overlay(logo, ")
    assert r["ctx"]["kind"] == "param" and r["items"] == ["top=", "at=", "pos="]
    assert run("v.overlay(logo, p")["items"] == ["pos="]
    assert run("v.overlay(logo, at=1, po")["items"] == ["pos="]
    assert run("v.trim(")["items"] == ["start=", "end="]
    assert run("v.trim('1s')")["ctx"] is None


def test_apply_completion_closes_quotes_and_opens_calls():
    r = _js(f"const cat = {json.dumps(CAT)};"
            "const t = 'x = videos[\"ta'; const ctx = completionContext(t, t.length);"
            "const it = completions(cat, ctx)[0]; console.log(JSON.stringify(applyCompletion(t, t.length, ctx, it)));")
    assert r["text"] == 'x = videos["talk.mp4"' and r["cursor"] == len('x = videos["talk.mp4"')
    r = _js(f"const cat = {json.dumps(CAT)};"
            "const t = 'x = videos[\"ta\"]'; const ctx = completionContext(t, 14);"
            "const it = completions(cat, ctx)[0]; console.log(JSON.stringify(applyCompletion(t, 14, ctx, it)));")
    assert r["text"] == 'x = videos["talk.mp4"]'
    r = _js(f"const cat = {json.dumps(CAT)};"
            "const t = 'v.tr'; const ctx = completionContext(t, 4);"
            "const it = completions(cat, ctx)[0]; console.log(JSON.stringify(applyCompletion(t, 4, ctx, it)));")
    assert r["text"] == "v.trim(" and r["cursor"] == 7


def test_highlighter_tokens():
    r = _js("console.log(JSON.stringify({h: highlight('talk = talk.cut(\"12s\").fade_in(\"0.5s\")  # c')}));")
    h = r["h"]
    assert '<span class="m">cut</span>' in h and '<span class="t">12s</span>' in h and '<span class="c"># c</span>' in h
