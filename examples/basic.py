"""The README example. Run:  sceneoverflow run examples/basic.py -o examples/out/basic.mp4"""
from sceneoverflow import edit


@edit
def edit(videos, sounds, pictures):
    talk = videos["talk"]
    # two cuts, drop the middle piece, join what is left  (== talk.remove(("12s", "14s")))
    talk = talk.cut("12s", "14s").delete(1).join()
    # every sound file in ./media, back to back, mixed under the talk at 40% volume
    music = sounds.join()
    talk = talk.with_audio(music, at=0, mode="mix", gain=0.4)
    # logo in the corner for the first three seconds, then a caption
    talk = talk.overlay(pictures["logo"], at="0.5s", for_="3s", pos="top-right")
    talk = talk.text("cut by a script", at="4s", for_="2s", pos="bottom", size=40, box="black@0.5")
    return talk.fade_in("0.5s").fade_out("1s")
