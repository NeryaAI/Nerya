"""Bounded raster avatar values, kept in the existing profile JSON."""
from __future__ import annotations

import base64
import binascii
import struct
import zlib

PNG_PREFIX = "data:image/png;base64,"
MAX_AVATAR_BYTES = 96 * 1024
PRESET_ROLES = frozenset({
    "lead", "researcher", "analyst", "reviewer", "risk", "coder", "macro", "quant",
    "execution", "data", "sentiment", "onchain", "portfolio", "backtest", "security",
    "connector", "prediction", "futures", "equities", "evolution",
})


def valid_avatar(value: str) -> bool:
    if value.startswith("role:"):
        return value[5:] in PRESET_ROLES
    if not value.startswith("data:"):
        return len(value) <= 16
    if not value.startswith(PNG_PREFIX) or len(value) > len(PNG_PREFIX) + 4 * ((MAX_AVATAR_BYTES + 2) // 3):
        return False
    try:
        image = base64.b64decode(value[len(PNG_PREFIX):], validate=True)
        if len(image) > MAX_AVATAR_BYTES or image[:8] != b"\x89PNG\r\n\x1a\n":
            return False
        position, header, ended, compressed = 8, None, False, bytearray()
        while position < len(image):
            if position + 12 > len(image):
                return False
            length = struct.unpack_from(">I", image, position)[0]
            kind = image[position + 4:position + 8]
            end = position + 8 + length
            if end + 4 > len(image):
                return False
            payload = image[position + 8:end]
            if zlib.crc32(kind + payload) & 0xFFFFFFFF != struct.unpack_from(">I", image, end)[0]:
                return False
            if header is None:
                if kind != b"IHDR" or length != 13:
                    return False
                header = struct.unpack(">IIBBBBB", payload)
                width, height, depth, color, compression, filtering, interlace = header
                if not (0 < width <= 256 and 0 < height <= 256) or depth != 8 or color not in (2, 6) or any((compression, filtering, interlace)):
                    return False
            elif kind == b"IHDR":
                return False
            if kind == b"IDAT":
                compressed.extend(payload)
            if kind == b"IEND":
                if length or end + 4 != len(image):
                    return False
                ended = True
                break
            position = end + 4
        if not ended or header is None or not compressed:
            return False
        width, height, _, color, *_ = header
        stride = 1 + width * (4 if color == 6 else 3)
        expected = height * stride
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(bytes(compressed), expected + 1)
        return len(pixels) == expected and decoder.eof and not decoder.unused_data and all(pixels[row * stride] <= 4 for row in range(height))
    except (ValueError, binascii.Error, struct.error, zlib.error):
        return False
