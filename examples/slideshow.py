"""Stills to video: every picture for 2s, with a cross-scene music bed and captions.
Run:  sceneoverflow run examples/slideshow.py -o examples/out/slideshow.mp4"""
from sceneoverflow import edit


@edit
def edit(videos, sounds, pictures):
    slides = pictures.map(lambda p: p.as_clip("2s").fade("0.3s"))
    show = slides.join()
    for i, p in enumerate(pictures):
        show = show.text(p.name, at=i * 2, for_="2s", pos="bottom", size=28)
    return show.with_audio(sounds.join(), mode="replace", gain=0.5)
