"""Pure-Python BlurHash encode (no C extension → builds in python:3.13-slim).

Operates on a PIL image, downscaled to a small grid first so the O(pixels*components)
DCT stays cheap. Implements the reference algorithm (github.com/woltapp/blurhash).
"""

import math

from PIL import Image

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"


def _b83(value: int, length: int) -> str:
    out = ""
    for i in range(1, length + 1):
        out += _CHARS[(value // (83 ** (length - i))) % 83]
    return out


def _srgb_to_linear(c: int) -> float:
    x = c / 255.0
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(v: float) -> int:
    v = max(0.0, min(1.0, v))
    s = v * 12.92 if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055
    return round(s * 255)


def _sign_pow(value: float, exp: float) -> float:
    return math.copysign(abs(value) ** exp, value)


def encode(image: Image.Image, components_x: int = 4, components_y: int = 3) -> str:
    # Downscale: BlurHash only needs a coarse representation, and this bounds the cost.
    small = image.convert("RGB")
    small.thumbnail((64, 64))
    width, height = small.size
    pixels = list(small.getdata())  # type: ignore[arg-type]  # Pillow stub types this as ImagingCore
    lin = [(_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)) for r, g, b in pixels]

    components = []
    for j in range(components_y):
        for i in range(components_x):
            norm = 1.0 if (i == 0 and j == 0) else 2.0
            r = g = b = 0.0
            for y in range(height):
                cos_y = math.cos(math.pi * j * y / height)
                for x in range(width):
                    basis = norm * math.cos(math.pi * i * x / width) * cos_y
                    pr, pg, pb = lin[y * width + x]
                    r += basis * pr
                    g += basis * pg
                    b += basis * pb
            scale = 1.0 / (width * height)
            components.append((r * scale, g * scale, b * scale))

    dc = components[0]
    ac = components[1:]

    dc_value = (
        (_linear_to_srgb(dc[0]) << 16) + (_linear_to_srgb(dc[1]) << 8) + _linear_to_srgb(dc[2])
    )

    if ac:
        actual_max = max(max(abs(v) for v in c) for c in ac)
        quant_max = max(0, min(82, int(actual_max * 166 - 0.5)))
        max_value = (quant_max + 1) / 166
    else:
        quant_max = 0
        max_value = 1.0

    out = _b83((components_x - 1) + (components_y - 1) * 9, 1)
    out += _b83(quant_max, 1)
    out += _b83(dc_value, 4)
    for r, g, b in ac:
        qr = max(0, min(18, int(_sign_pow(r / max_value, 0.5) * 9 + 9.5)))
        qg = max(0, min(18, int(_sign_pow(g / max_value, 0.5) * 9 + 9.5)))
        qb = max(0, min(18, int(_sign_pow(b / max_value, 0.5) * 9 + 9.5)))
        out += _b83(qr * 19 * 19 + qg * 19 + qb, 2)
    return out
