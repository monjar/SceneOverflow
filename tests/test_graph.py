from sceneoverflow.graph import AUDIO, VIDEO, Node, capture_provenance, make


def src(duration=10.0, path="a.mp4"):
    return make("source", VIDEO, {"path": path, "duration": duration, "sig": "x"})


def test_hash_is_content_addressed():
    a = make("trim", VIDEO, {"start": 1.0, "end": 3.0}, [src()])
    b = make("trim", VIDEO, {"start": 1.0000001, "end": 3.0}, [src()])
    c = make("trim", VIDEO, {"start": 1.0, "end": 3.5}, [src()])
    assert a.hash == b.hash
    assert a.hash != c.hash
    assert a.hash != make("trim", VIDEO, {"start": 1.0, "end": 3.0}, [src(path="b.mp4")]).hash


def test_provenance_points_at_caller():
    n = src()
    assert n.provenance is not None
    assert n.provenance.file.endswith("test_graph.py")
    assert n.provenance.function == "src"


def test_durations():
    s = src(10.0)
    t = make("trim", VIDEO, {"start": 2.0, "end": 5.0}, [s])
    c = make("concat", VIDEO, {}, [t, s])
    assert t.duration == 3.0
    assert c.duration == 13.0
    assert make("speed", VIDEO, {"factor": 2.0}, [c]).duration == 6.5
    a = make("source", AUDIO, {"path": "m.wav", "duration": 30.0, "sig": "y"})
    assert make("with_audio", VIDEO, {"at": 0.0, "mode": "mix"}, [c, a]).duration == 13.0
    assert make("with_audio", VIDEO, {"at": 0.0, "mode": "mix", "extend": True}, [c, a]).duration == 30.0


def test_walk_dedups_shared_inputs():
    s = src()
    c = make("concat", VIDEO, {}, [s, s])
    ops = [n.op for n in c.walk()]
    assert ops == ["source", "concat"]
