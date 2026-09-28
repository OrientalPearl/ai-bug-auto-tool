"""Pixel size of a downloaded attachment, read straight from its header.

Why this exists
---------------
Zentao hands out placeholder images that are literally 1x1 pixels. Feeding one to
a vision model is not a warning, it is a hard rejection:

    {"error": {"code": "invalid_parameter_error",
     "message": "The image length and width do not meet the model restrictions.
                 [height:1 or width:1 must be larger than 10]"}}

and an unattended run that reads the screenshot first -- which gate G1 demands --
dies right there. So the size is measured once, at download time, and the picture
is either handed to the AI as a real screenshot or marked unreadable with a
reason, never silently fed to the model.

No Pillow: this runs on a machine where the only guaranteed dependency is the
standard library, and four header formats cover everything Zentao serves.
"""
from __future__ import annotations

import struct
from pathlib import Path

# Anything below this on either side is rejected by the vision models we drive.
MIN_SIDE = 10

# Only these are worth measuring; a PDF has no pixel size to speak of.
MEASURABLE = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


def _png(b: bytes):
    w, h = struct.unpack(">II", b[16:24])
    return w, h


def _gif(b: bytes):
    w, h = struct.unpack("<HH", b[6:10])
    return w, h


def _bmp(b: bytes):
    w, h = struct.unpack("<ii", b[18:26])
    return w, abs(h)


def _jpeg(b: bytes):
    i, n = 2, len(b)
    while i < n - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        segment = struct.unpack(">H", b[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + segment
    return None


def _webp(b: bytes):
    kind = b[12:16]
    if kind == b"VP8X":
        return (1 + int.from_bytes(b[24:27], "little"),
                1 + int.from_bytes(b[27:30], "little"))
    if kind == b"VP8L":
        bits = int.from_bytes(b[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if kind == b"VP8 ":
        return (struct.unpack("<H", b[26:28])[0] & 0x3FFF,
                struct.unpack("<H", b[28:30])[0] & 0x3FFF)
    return None


def measure(path: Path):
    """Return ``(width, height)``, or None when the pixels cannot be determined."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < 30:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _gif(data)
    if data[:2] == b"BM":
        return _bmp(data)
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    return None


def gate(path, ext: str = ""):
    """Judge one attachment. Returns ``(pixels, unreadable_reason)``.

    A non-picture is not "unreadable": it simply has no pixel gate, so both
    values come back empty and the caller keeps listing it as a document.
    """
    ext = str(ext or Path(str(path)).suffix).lower().lstrip(".")
    if ext and ext not in MEASURABLE:
        return "", ""
    size = measure(Path(path))
    if size is None:
        return "", "读不出图片尺寸（文件损坏或不是图片），视觉模型会直接报错"
    w, h = int(size[0]), int(size[1])
    if w < MIN_SIDE or h < MIN_SIDE:
        return f"{w}x{h}", f"图片只有 {w}x{h}，短边不足 {MIN_SIDE}px，喂给模型会被拒"
    return f"{w}x{h}", ""
