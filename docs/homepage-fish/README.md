# Homepage fish

Source of `frontend/assets/fish-swim.webp`, the small fish that swims down the right margin
of the product homepage (`web/home.html`). It was drawn for this project; there is no
outside artwork, photograph or model in it.

- `fish_drawing.py`: the drawing. `svg(t)` returns one frame of the swim cycle as SVG.
  Standard library only.
- `render_fish.py`: an optional authoring tool that turns 12 frames into the sprite. The app,
  its tests and its packaging never use it.

## Rebuilding the sprite

Needs Google Chrome (or Chromium) and two packages that are not in `requirements.txt`:

```
pip install pillow numpy
python docs/homepage-fish/render_fish.py rebuilt.webp
```

On Windows the script looks for Chrome in its default location; elsewhere pass the path
as a second argument.

## How exact a rebuild is

A rebuild is visually the same as the committed sprite but not byte-identical: the frames
captured from Chrome vary slightly in fine detail from run to run. Checked on Windows with
Chrome: the committed sprite and two rebuilds were compared on both homepage backgrounds,
and each pair differed in about 0.1 % of pixels, by at most 14 of 255.
