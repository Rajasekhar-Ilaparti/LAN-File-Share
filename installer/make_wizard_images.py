"""Draw the setup wizard's side banner and corner logo from the app's
pixel-art icon, at 100% and 200% so they stay crisp on high-DPI screens."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from file_share_app import pixel_logo_image  # noqa: E402
from PIL import Image  # noqa: E402

TOP, BOTTOM = (88, 80, 236), (52, 44, 170)  # same indigo as the app


def banner(scale):
    w, h = 164 * scale, 314 * scale
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        row = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
        for x in range(w):
            px[x, y] = row
    # faint pixel "stars", like an old game title screen
    dot = 2 * scale
    for i, (sx, sy) in enumerate(((18, 30), (132, 52), (30, 250), (140, 210), (96, 284), (60, 70), (120, 150))):
        shade = (150, 146, 255) if i % 2 else (124, 118, 250)
        for yy in range(sy * scale, sy * scale + dot):
            for xx in range(sx * scale, sx * scale + dot):
                px[xx, yy] = shade
    logo = pixel_logo_image(6 * scale)
    img.paste(logo, ((w - logo.width) // 2, int(h * 0.30)), logo)
    return img


def corner(scale):
    size = 58 * scale
    img = Image.new("RGB", (size, size), (255, 255, 255))
    logo = pixel_logo_image(3 * scale)
    img.paste(logo, ((size - logo.width) // 2, (size - logo.height) // 2), logo)
    return img


if __name__ == "__main__":
    for scale in (1, 2):
        banner(scale).save(os.path.join(HERE, f"wizard_{scale}x.bmp"))
        corner(scale).save(os.path.join(HERE, f"wizard_small_{scale}x.bmp"))
    print("wizard images written")
