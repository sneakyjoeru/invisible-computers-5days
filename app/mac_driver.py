"""Image "driver" for the headless Mac instance.

There is no e-ink panel or SPI on this machine — the deliverable is the final
image file. This module takes the supersampled PIL image from render.py, scales
it down to the panel's native resolution (OUTPUT_W x OUTPUT_H), and thresholds
it to a true 1-bit black/white PNG that the 800x480 device fetches from /image.

Downscaling is done in grayscale with LANCZOS, then thresholded — so 1px lines
drawn at 3x survive as clean ~1px lines at native resolution (a single black
row within a 3-row block averages to ~170 and stays below the threshold).
"""
import logging
import os
import tempfile

from PIL import Image

from . import config

logger = logging.getLogger("eink.macdriver")

# Threshold cutoff (0-255): pixels below become black. 200 keeps thin/averaged
# grid lines (which land around 170 after downscaling) as black.
_THRESHOLD = int(os.environ.get("BW_THRESHOLD", "200"))


def render_to_file(pil_image: Image.Image, out_path=None) -> str:
    """Downscale + threshold the rendered image to 1-bit and save it atomically.

    Returns the path written. `pil_image` is expected at SCREEN_W x SCREEN_H
    (supersampled); output is OUTPUT_W x OUTPUT_H, mode "1".
    """
    out_path = str(out_path or config.RENDER_IMAGE)
    target = (config.OUTPUT_W, config.OUTPUT_H)

    gray = pil_image.convert("L")
    if gray.size != target:
        gray = gray.resize(target, Image.LANCZOS)

    # Threshold to true 1-bit (mode "1"): black where dark.
    bw = gray.point(lambda x: 0 if x < _THRESHOLD else 255, "L").convert("1")

    # Atomic write: temp in the same dir, then rename, so /image never serves a
    # half-written file.
    d = os.path.dirname(out_path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".png")
    os.close(fd)
    try:
        bw.save(tmp, "PNG", optimize=True)
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    logger.info("Wrote 1-bit image %s (%dx%d)", out_path, target[0], target[1])
    return out_path


def has_image() -> bool:
    return os.path.exists(str(config.RENDER_IMAGE))
