import shutil
import subprocess
from pathlib import Path

import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    """Synthetic media: two videos, one sound, one picture, one silent-gap video."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    d = tmp_path_factory.mktemp("media")
    # 30s test pattern with a 440Hz tone, 320x240@25 (odd size/fps to exercise conforming)
    ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=30",
       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=30",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", d / "a_intro.mp4")
    # 10s: red for 5s then blue for 5s (one scene change), no audio stream at all
    ff("-f", "lavfi", "-i", "color=c=red:size=320x240:rate=25:duration=5",
       "-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=25:duration=5",
       "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", d / "b_scenes.mp4")
    # 12s video whose audio is tone / 3s silence / tone
    ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=12",
       "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
       "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
       "-f", "lavfi", "-i", "sine=frequency=300:duration=5",
       "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]", "-map", "0:v", "-map", "[a]",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", d / "c_gap.mp4")
    # 20s music
    ff("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:duration=20", "-c:a", "libmp3lame", d / "music.mp3")
    # logo
    ff("-f", "lavfi", "-i", "color=c=yellow:size=80x40:duration=0.04", "-frames:v", "1", d / "logo.png")
    return d


@pytest.fixture()
def project(media_dir, tmp_path):
    from sceneoverflow import Project
    return Project(media_dir, cache_dir=tmp_path / "cache")


@pytest.fixture(scope="session")
def shared_project(media_dir, tmp_path_factory):
    from sceneoverflow import Project
    return Project(media_dir, cache_dir=tmp_path_factory.mktemp("cache"))


def probe_duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def probe_dims(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")
    return int(w), int(h)
