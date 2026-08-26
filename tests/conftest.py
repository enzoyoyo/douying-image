"""Shared test helpers."""

from __future__ import annotations

from io import BytesIO

from PIL import Image


def make_png(size: tuple[int, int] = (8, 6)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(buffer, format="PNG")
    return buffer.getvalue()
