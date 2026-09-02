import pytest

from sceneoverflow.timing import Span, TimeError, TimeRef, fmt_time, parse_span, parse_time


@pytest.mark.parametrize("value,expected", [
    (12, 12.0), (1.5, 1.5), ("12s", 12.0), ("0.25s", 0.25), ("500ms", 0.5),
    ("1:23.5", 83.5), ("01:02:03.25", 3723.25), ("00:10", 10.0), ("7", 7.0),
    (TimeRef(3.0), 3.0), (Span(4.0, 5.0), 4.0),
])
def test_parse_time(value, expected):
    assert parse_time(value) == pytest.approx(expected)


def test_frames_need_fps():
    assert parse_time("30f", fps=30) == pytest.approx(1.0)
    with pytest.raises(TimeError):
        parse_time("30f")


@pytest.mark.parametrize("bad", ["", "abc", "-3", -1, "12x", None, True])
def test_bad_times(bad):
    with pytest.raises(TimeError):
        parse_time(bad)


def test_timeref_arithmetic():
    t = TimeRef(10.0, "intro")
    assert float(t + "0.5s") == pytest.approx(10.5)
    assert float(t - 2) == pytest.approx(8.0)
    assert (t + "1s").label == "intro"
    assert t < "11s"


def test_span():
    s = Span(2.0, 4.0, "gap")
    assert s.duration == 2.0
    assert float(s.mid) == 3.0
    assert s.shift(1.0) == Span(3.0, 5.0, "gap")
    assert s.clip_to(3.0, 10.0) == Span(3.0, 4.0, "gap")
    assert s.clip_to(5.0, 10.0) is None
    assert parse_span(("1s", "2s")) == Span(1.0, 2.0)
    with pytest.raises(TimeError):
        parse_span(("2s", "1s"))


def test_fmt_time():
    assert fmt_time(0) == "00:00.000"
    assert fmt_time(83.5) == "01:23.500"
    assert fmt_time(3723.25) == "1:02:03.250"
