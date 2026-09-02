import json
from pathlib import Path

import pytest

from sceneoverflow import EditError, Span, edit, run_script
from tests.conftest import probe_dims, probe_duration

pytestmark = pytest.mark.ffmpeg


def test_media_lists(shared_project):
    p = shared_project
    assert p.videos.names == ["a_intro.mp4", "b_scenes.mp4", "c_gap.mp4"]
    assert p.sounds.names == ["music.mp3"]
    assert p.pictures.names == ["logo.png"]
    assert p.videos[0].duration == pytest.approx(30.0, abs=0.1)
    assert p.videos["b_scenes"].duration == pytest.approx(10.0, abs=0.1)
    assert p.profile.fps == 25.0


def test_user_example_cut_delete_join(shared_project, tmp_path):
    v = shared_project.videos.get(0)
    parts = v.cut("12s", "14s")
    assert len(parts) == 3
    assert [round(c.duration, 3) for c in parts] == [12.0, 2.0, pytest.approx(v.duration - 14.0, abs=0.01)]
    joined = parts.delete(1).join()
    assert joined.duration == pytest.approx(v.duration - 2.0, abs=0.01)
    assert joined.node.hash == v.remove(("12s", "14s")).node.hash  # same graph, same hash
    out = joined.render(tmp_path / "out.mp4")
    assert probe_duration(out) == pytest.approx(v.duration - 2.0, abs=0.15)
    assert probe_dims(out) == (640, 360)


def test_cache_hits_and_dirty_rerender(shared_project, tmp_path):
    r = shared_project.renderer
    v = shared_project.videos[0]
    a = v.remove(("12s", "14s"))
    r.render(a.node)
    r.stats.reset()
    r.render(a.node)
    assert r.stats.hit_rate == 1.0
    r.stats.reset()
    b = v.remove(("12s", "15s"))  # move one cut point
    r.render(b.node)
    # first trim [0,12) is shared and cached; second trim and the concat re-render
    assert len(r.stats.rendered) == 2
    assert len(r.stats.cached) >= 1


def test_with_audio_overlay_fade_text(shared_project, tmp_path):
    p = shared_project
    v = p.videos[0].head("6s")
    music = p.sounds.join()
    v = v.with_audio(music, at="1s", mode="mix", gain=0.5)
    v = v.overlay(p.pictures[0], at="1s", for_="2s", pos="top-right")
    v = v.text("hello", at="0.5s", for_="1s", pos="bottom", box="black@0.5")
    v = v.fade_in("0.5s").fade_out("0.5s")
    assert v.duration == pytest.approx(6.0)
    out = v.render(tmp_path / "fx.mp4")
    assert probe_duration(out) == pytest.approx(6.0, abs=0.15)
    desc = v.describe()
    assert "overlay" in desc and "logo.png" in desc and "music.mp3" in desc and "'hello'" in desc
    assert "fade in" in desc and "fade out" in desc
    js = v.to_json()
    assert js["duration"] == pytest.approx(6.0)
    assert {s["track"] for s in js["segments"]} == {"video", "audio", "overlay", "text", "fx"}
    json.dumps(js)  # serializable


def test_replace_and_duck_modes(shared_project, tmp_path):
    p = shared_project
    v = p.videos[0].head("3s")
    for mode in ("replace", "duck"):
        out = v.with_audio(p.sounds[0], mode=mode).render(tmp_path / f"{mode}.mp4")
        assert probe_duration(out) == pytest.approx(3.0, abs=0.15)


def test_video_without_audio_gets_silence_and_concats(shared_project, tmp_path):
    p = shared_project
    v = p.videos["b_scenes"].head("2s") + p.videos["a_intro"].head("2s")
    out = v.render(tmp_path / "mixed.mp4")
    assert probe_duration(out) == pytest.approx(4.0, abs=0.15)


def test_speed_and_image_clip_and_audio_extract(shared_project, tmp_path):
    p = shared_project
    fast = p.videos[0].head("4s").speed(2)
    assert fast.duration == pytest.approx(2.0)
    assert probe_duration(fast.render(tmp_path / "fast.mp4")) == pytest.approx(2.0, abs=0.15)
    still = p.pictures[0].as_clip("1.5s")
    assert probe_duration(still.render(tmp_path / "still.mp4")) == pytest.approx(1.5, abs=0.15)
    a = p.videos[0].head("2s").audio
    assert a.kind == "audio"
    assert probe_duration(a.render(tmp_path / "a.wav")) == pytest.approx(2.0, abs=0.05)
    seq = p.pictures.map(lambda i: i.as_clip("1s")).join()
    assert seq.duration == 1.0


def test_silences_and_scenes(shared_project):
    p = shared_project
    sil = p.videos["c_gap"].silences(min_len="1s")
    assert len(sil) == 1
    assert sil[0].start == pytest.approx(4.0, abs=0.3)
    assert sil[0].end == pytest.approx(7.0, abs=0.3)
    tight = p.videos["c_gap"].remove(*sil)
    assert tight.duration == pytest.approx(9.0, abs=0.5)
    scenes = p.videos["b_scenes"].scenes()
    assert len(scenes) == 1
    assert float(scenes[0]) == pytest.approx(5.0, abs=0.1)
    # anchors on a derived clip are in the derived clip's own time
    later = p.videos["c_gap"].trim("2s")
    s2 = later.silences(min_len="1s")
    assert s2[0].start == pytest.approx(2.0, abs=0.3)


def test_markers(shared_project, media_dir):
    v = shared_project.videos[0]
    v.marks.set("intro_end", "3s")
    v.marks.set("outro", "10s")
    assert (media_dir / "a_intro.mp4.marks.json").exists()
    assert float(v.marks["intro_end"]) == 3.0
    assert v.marks.span("intro_end", "outro") == Span(3.0, 10.0, "intro_end..outro")
    with pytest.raises(KeyError, match="sceneoverflow mark"):
        v.marks["nope"]
    clip = v.trim(v.marks["intro_end"], v.marks["outro"] + "1s")
    assert clip.duration == pytest.approx(8.0)
    with pytest.raises(EditError):
        clip.marks


def test_errors(shared_project):
    p = shared_project
    with pytest.raises(EditError, match="past the end"):
        p.videos[0].trim("5s", "999s")
    with pytest.raises(EditError, match="mixed"):
        from sceneoverflow import Sequence
        Sequence([p.videos[0], p.sounds[0]], p).join()
    with pytest.raises(EditError, match="delete all"):
        p.videos[0].remove((0, "30s"))
    with pytest.raises(IndexError, match="out of range"):
        p.videos[7]
    with pytest.raises(EditError, match="as_clip"):
        p.pictures.join()


def test_run_script_end_to_end(media_dir, tmp_path):
    script = tmp_path / "edit.py"
    script.write_text(
        "from sceneoverflow import edit\n\n"
        "@edit\n"
        "def edit(videos, sounds, pictures):\n"
        "    v = videos.get(0).cut('12s', '14s').delete(1).join()\n"
        "    fullsound = sounds.join()\n"
        "    return v.dub(fullsound).overlay(pictures[0], at='1s', for_='2s')\n"
    )
    res = run_script(script, media=media_dir, out=tmp_path / "out.mp4", cache_dir=tmp_path / "cache")
    assert res["out"] and Path(res["out"]).exists()
    assert probe_duration(res["out"]) == pytest.approx(28.0, abs=0.15)
    desc = res["clip"].describe()
    assert "edit.py:5" in desc and "edit.py:7" in desc
    # second run is all cache hits
    res2 = run_script(script, media=media_dir, out=tmp_path / "out2.mp4", cache_dir=tmp_path / "cache")
    assert res2["stats"].hit_rate == 1.0


def test_final_mode_uses_source_resolution(media_dir, tmp_path):
    from sceneoverflow import Project
    p = Project(media_dir, cache_dir=tmp_path / "cache", mode="final")
    out = p.videos[0].head("1s").render(tmp_path / "final.mp4")
    assert probe_dims(out) == (320, 240)


def test_timeline_png(shared_project, tmp_path):
    pytest.importorskip("PIL")
    v = shared_project.videos[0].remove(("12s", "14s")).with_audio(shared_project.sounds[0])
    out = v.timeline_png(str(tmp_path / "tl.png"))
    assert Path(out).stat().st_size > 1000


def test_notebook_html(shared_project):
    html = shared_project.videos[0].head("1s")._repr_html_()
    assert "<video" in html and "data:video/mp4;base64" in html and "timeline" in html


def test_overlay_video_pip_and_beside(shared_project, tmp_path):
    p = shared_project
    base = p.videos["a_intro"].head("6s")
    top = p.videos["b_scenes"].head("3s")
    v = base.pip(top, at="1s", audio=True)                       # video on video, bottom-right, 30% width
    assert v.duration == pytest.approx(6.0)
    out = v.render(tmp_path / "pip.mp4")
    assert probe_duration(out) == pytest.approx(6.0, abs=0.15)
    desc = v.describe()
    assert "overlay" in desc and "b_scenes.mp4" in desc and "x0.3" in desc and "+audio" in desc
    seg = [s for s in v.to_json()["segments"] if s["track"] == "overlay"][0]
    assert seg["out_start"] == pytest.approx(1.0) and seg["out_end"] == pytest.approx(4.0)  # for_ defaults to top length
    # the b-roll is red for its first 5s: the pip region must be reddish at t=2s and not at t=0.5s
    from PIL import Image
    red = Image.open(v.frame_at("2s", str(tmp_path / "f2.png"))).convert("RGB")
    before = Image.open(v.frame_at("0.5s", str(tmp_path / "f0.png"))).convert("RGB")
    # pip is 30% wide at bottom-right with a 16px margin; sample its centre (the 4:3 clip is pillarboxed)
    pw, ph = int(red.width * 0.3), int(red.width * 0.3) * 9 // 16
    px = (red.width - 16 - pw // 2, red.height - 16 - ph // 2)
    r, g, b = red.getpixel(px)
    assert r > 150 and g < 90 and b < 90, (r, g, b)
    assert before.getpixel(px) != red.getpixel(px)
    half = v.overlay(p.pictures[0], opacity=0.5, scale=0.2, pos="center")
    assert "@0.5" in half.describe()
    assert probe_duration(half.render(tmp_path / "half.mp4")) == pytest.approx(6.0, abs=0.15)
    side = base.head("2s").beside(top)
    assert side.duration == pytest.approx(3.0)
    assert probe_dims(side.render(tmp_path / "side.mp4")) == (640, 360)
    assert "right" in side.describe()
    stacked = base.head("2s").above(top.head("1s"))
    assert probe_duration(stacked.render(tmp_path / "stack.mp4")) == pytest.approx(2.0, abs=0.15)


def test_picture_on_picture(shared_project, tmp_path):
    p = shared_project
    logo = p.pictures[0]
    card = logo.overlay(logo, pos="center", scale=0.5, opacity=0.8)
    assert card.kind == "image"
    out = card.render(tmp_path / "card.png")
    from PIL import Image
    assert Image.open(out).size == (80, 40)
    still = card.as_clip("1s")
    assert probe_duration(still.render(tmp_path / "card.mp4")) == pytest.approx(1.0, abs=0.15)
    with pytest.raises(EditError, match="as_clip"):
        logo.overlay(p.videos[0])


def test_transitions(shared_project, tmp_path):
    p = shared_project
    a, b, c = p.videos[0].head("3s"), p.videos["b_scenes"].head("3s"), p.videos["c_gap"].head("2s")
    from sceneoverflow import Sequence
    v = Sequence([a, b, c], p).join(transition="wipeleft", duration="0.5s")
    assert v.duration == pytest.approx(3 + 3 + 2 - 1.0)
    out = v.render(tmp_path / "xf.mp4")
    assert probe_duration(out) == pytest.approx(7.0, abs=0.15)
    desc = v.describe()
    assert "wipeleft transition" in desc and desc.count("wipeleft") == 2
    segs = v.to_json()["segments"]
    second = [s for s in segs if s["track"] == "video"][1]
    assert second["out_start"] == pytest.approx(2.5)
    assert a.crossfade(b).duration == pytest.approx(5.5)
    with pytest.raises(EditError, match="longer than"):
        a.crossfade(b, duration="4s")
    with pytest.raises(EditError, match="unknown transition"):
        a.crossfade(b, transition="explode")


def test_title_blank_freeze_loop_normalize_mute(shared_project, tmp_path):
    p = shared_project
    card = p.title("Chapter 1", "1.5s", bg="0x223344")
    assert card.duration == pytest.approx(1.5) and "color:0x223344" in card.describe() and "'Chapter 1'" in card.describe()
    v = card + p.videos[0].head("2s")
    assert probe_duration(v.render(tmp_path / "t.mp4")) == pytest.approx(3.5, abs=0.15)
    fz = p.videos[0].head("2s").freeze("1s", "1s")
    assert fz.duration == pytest.approx(3.0)
    assert probe_duration(fz.render(tmp_path / "fz.mp4")) == pytest.approx(3.0, abs=0.15)
    desc = fz.describe()
    assert "(still)" in desc and "a_intro.mp4 [00:01.000-00:01.000]" in desc
    lp = p.videos[0].head("1s").loop(3)
    assert lp.duration == pytest.approx(3.0)
    nz = p.videos[0].head("2s").normalize().mute()
    assert "normalize -16 LUFS" in nz.describe() and "volume x0.00" in nz.describe()
    assert probe_duration(nz.render(tmp_path / "nz.mp4")) == pytest.approx(2.0, abs=0.15)
    assert probe_duration(p.sounds[0].head("2s").normalize().render(tmp_path / "nz.wav")) == pytest.approx(2.0, abs=0.1)
    blank = p.blank("0.5s")
    assert probe_dims(blank.render(tmp_path / "b.mp4")) == (640, 360)


def test_crop_aspect_and_box(shared_project, tmp_path):
    p = shared_project
    v = p.videos[0].head("1s")
    vert = v.crop("9:16")
    assert probe_dims(vert.render(tmp_path / "v.mp4")) == (202, 360)
    sq = v.crop("1:1", anchor="left")
    assert probe_dims(sq.render(tmp_path / "sq.mp4")) == (360, 360)
    box = v.crop(x=10, y=10, w=100, h=50)
    assert probe_dims(box.render(tmp_path / "box.mp4")) == (100, 50)
    assert "crop 9:16" in vert.describe()
    with pytest.raises(EditError):
        v.crop("nine-sixteen")
    # cropped pieces still concat (filter fallback conforms to the first input)
    both = vert + v.crop("9:16", anchor="right")
    assert probe_dims(both.render(tmp_path / "both.mp4")) == (202, 360)


def test_subtitles_from_srt_and_transcript(shared_project, tmp_path):
    from sceneoverflow import Transcript, Word
    p = shared_project
    words = [Word("hello", 0.2, 0.5), Word("there", 0.6, 0.9), Word("world.", 1.0, 1.4), Word("bye", 2.0, 2.3)]
    t = Transcript(words)
    cues = t.cues()
    assert [c.label for c in cues] == ["hello there world.", "bye"]
    srt = t.to_srt(tmp_path / "t.srt")
    assert "00:00:00,200 --> 00:00:01,400" in srt and srt.count("-->") == 2
    v = p.videos[0].head("3s")
    a = v.subtitles(tmp_path / "t.srt", style="FontSize=30")
    b = v.subtitles(t)
    for clip, name in ((a, "a"), (b, "b")):
        assert probe_duration(clip.render(tmp_path / f"{name}.mp4")) == pytest.approx(3.0, abs=0.15)
    assert "(subtitles)" in a.describe() and "t.srt" in a.describe()
    with pytest.raises(EditError, match="not found"):
        v.subtitles("/nope.srt")


def test_gif_and_webm_export(shared_project, tmp_path):
    v = shared_project.videos[0].head("1s")
    gif = v.render(tmp_path / "o.gif", fps=8, width=160)
    assert Path(gif).read_bytes()[:6] in (b"GIF89a", b"GIF87a")
    webm = v.render(tmp_path / "o.webm")
    assert probe_duration(webm) == pytest.approx(1.0, abs=0.15)
