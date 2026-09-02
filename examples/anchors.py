"""Anchors instead of timestamps: silences, scene changes, and named markers.
Run:  sceneoverflow run examples/anchors.py -o examples/out/anchors.mp4"""
from sceneoverflow import edit


@edit
def edit(videos, sounds, pictures):
    talk, broll = videos["talk"], videos["broll"]

    # 1. named markers live in media/talk.mp4.marks.json  (set them with `sceneoverflow mark`)
    intro = talk.trim(talk.marks["intro_start"], talk.marks["intro_end"])
    outro = talk.trim(talk.marks["outro"])

    # 2. scene changes are detected, not typed: keep only the b-roll's first scene
    first_scene = broll.trim(0, broll.scenes()[0])

    # 3. cut the dead air out of it (auto-editor in one line) and speed up what is left
    body = first_scene.remove(*first_scene.silences(min_len="1s")).speed(1.5)

    # 4. glue, with a music bed that ducks under the speech
    return (intro + body + outro).with_audio(sounds["music"], mode="duck", gain=0.8)
