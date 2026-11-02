"""Rebuild frontend/assets/fish-swim.webp from fish_drawing.py.

Optional authoring tool, not used by the app, its tests or its packaging. It needs
Google Chrome (or Chromium) and two packages that are not in requirements.txt:
Pillow and numpy.

    pip install pillow numpy
    python docs/homepage-fish/render_fish.py OUTPUT.webp [CHROME_PATH]

Steps: Chrome's command-line screenshot mode draws each of the 12 frames at 800 x 400
on a transparent background (no debugging port is opened); each frame is bent slightly
along the back half of the body (a small travelling wave); all frames are cropped to
one shared box, scaled to 176 x 61 px and placed side by side in one WebP image. The
page shows each frame at 72 x 25 CSS px, so the image stays sharp on high-density
screens.
"""

import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fish_drawing import H, W, svg  # noqa: E402

FRAMES = 12
FRAME_WIDTH = 176
DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def bend(img, t):
    """Shift each column vertically by a small wave that grows toward the tail."""
    a = np.asarray(img)
    out = np.zeros_like(a)
    for x in range(a.shape[1]):
        u = max(0.0, (x - 280) / 480)
        dy = int(round(4.8 * (u ** 1.6) * math.sin(t - 2.2 * u * math.pi)))
        if dy > 0:
            out[dy:, x] = a[:-dy, x]
        elif dy < 0:
            out[:dy, x] = a[-dy:, x]
        else:
            out[:, x] = a[:, x]
    return Image.fromarray(out)


def capture(chrome, work):
    """Screenshot each frame with headless Chrome, one short-lived process per frame."""
    frames = []
    for i in range(FRAMES):
        t = 2 * math.pi * i / FRAMES
        page, shot = work / f"frame{i}.html", work / f"frame{i}.png"
        page.write_text("<style>html,body{margin:0;background:transparent}svg{display:block}</style>" + svg(t),
                        encoding="utf-8")
        subprocess.run([chrome, "--headless=new", f"--user-data-dir={work / 'profile'}", "--no-first-run",
                        "--hide-scrollbars", "--default-background-color=00000000", "--force-device-scale-factor=2",
                        f"--window-size={W},{H}", f"--screenshot={shot}", page.as_uri()],
                       check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with Image.open(shot) as im:
            frames.append(bend(im.convert("RGBA"), t))
    return frames


def main(output, chrome=DEFAULT_CHROME):
    work = Path(tempfile.mkdtemp(prefix="fish-render-"))
    try:
        frames = capture(chrome, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    box = None
    for f in frames:
        b = f.getbbox()
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    box = (box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4)
    frames = [f.crop(box) for f in frames]
    scale = FRAME_WIDTH / frames[0].size[0]
    fw, fh = round(frames[0].size[0] * scale), round(frames[0].size[1] * scale)
    sheet = Image.new("RGBA", (fw * FRAMES, fh))
    for i, f in enumerate(frames):
        sheet.paste(f.resize((fw, fh), Image.LANCZOS), (i * fw, 0))
    sheet.save(output, "WEBP", quality=90, method=6)
    print(f"{output}: {sheet.size[0]} x {sheet.size[1]} ({FRAMES} frames of {fw} x {fh})")


if __name__ == "__main__":
    main(*sys.argv[1:])
