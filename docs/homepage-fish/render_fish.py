"""Rebuild frontend/assets/fish-swim.webp from fish_drawing.py.

Optional authoring tool, not used by the app, its tests or its packaging. It needs
Google Chrome (or Chromium) and two packages that are not in requirements.txt:
Pillow and numpy. websockets comes with uvicorn[standard].

    pip install pillow numpy
    python docs/homepage-fish/render_fish.py OUTPUT.webp [CHROME_PATH]

Steps: Chrome draws each of the 12 frames at 800 x 400 on a transparent background;
each frame is bent slightly along the back half of the body (a small travelling
wave); all frames are cropped to one shared box, scaled to 176 x 61 px and placed side
by side in one WebP image. The page shows each frame at 72 x 25 CSS px, so the image
stays sharp on high-density screens.
"""

import asyncio
import base64
import io
import json
import math
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np
import websockets
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fish_drawing import H, W, svg  # noqa: E402

FRAMES = 12
FRAME_WIDTH = 176
PORT = 9351
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


async def capture(ws_url):
    async with websockets.connect(ws_url, max_size=None) as ws:
        n = 0

        async def call(method, **params):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    return msg.get("result", {})

        await call("Emulation.setDefaultBackgroundColorOverride", color={"r": 0, "g": 0, "b": 0, "a": 0})
        await call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=False)
        frames = []
        for i in range(FRAMES):
            t = 2 * math.pi * i / FRAMES
            page = "<style>html,body{margin:0;background:transparent}svg{display:block}</style>" + svg(t)
            # Write the frame, then wait for two animation frames so the screenshot shows it painted.
            await call("Runtime.evaluate", awaitPromise=True, expression=(
                f"document.open(); document.write({json.dumps(page)}); document.close();"
                " new Promise(done => requestAnimationFrame(() => requestAnimationFrame(() => done(1))))"))
            shot = await call("Page.captureScreenshot", format="png")
            frames.append(bend(Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGBA"), t))
        return frames


def main(output, chrome=DEFAULT_CHROME):
    profile = tempfile.mkdtemp(prefix="fish-render-")
    proc = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                             "--no-first-run", "--hide-scrollbars", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        frames = asyncio.run(capture(ws_url))
    finally:
        proc.kill()
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
