"""Image candidate selection and content validation."""

from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .security import InputValidationError, validate_media_url

URL_FIELDS = (
    "watermark_free_download_url_list",
    "url_list",
    "download_url_list",
)
FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "AVIF": ".avif",
    "HEIF": ".heic",
}
MAX_CANDIDATES_PER_IMAGE = 12


class ImageValidationError(ValueError):
    """Raised when a response is not a complete supported image."""


@dataclass(frozen=True)
class ImageInfo:
    """Verified properties derived from downloaded bytes."""

    width: int
    height: int
    format: str
    extension: str
    byte_count: int
    sha256: str


def _url_lists(image: dict):
    for field in URL_FIELDS:
        yield field, image.get(field)

    download_address = image.get("download_addr")
    if isinstance(download_address, dict):
        yield "download_addr.url_list", download_address.get("url_list")


def image_candidates(image: object) -> list[str]:
    """Return unique, validated URLs in quality fallback order."""

    if not isinstance(image, dict):
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for _, values in _url_lists(image):
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str) or value in seen:
                continue
            try:
                safe_url = validate_media_url(value)
            except InputValidationError:
                continue
            seen.add(safe_url)
            candidates.append(safe_url)
            if len(candidates) >= MAX_CANDIDATES_PER_IMAGE:
                return candidates
    return candidates


def inspect_image_bytes(
    data: bytes,
    *,
    content_type: str | None = None,
    max_bytes: int = 50 * 1024 * 1024,
) -> ImageInfo:
    """Decode an image completely and derive trustworthy metadata."""

    if not data:
        raise ImageValidationError("empty_body")
    if len(data) > max_bytes:
        raise ImageValidationError("image_too_large")
    if content_type and not content_type.lower().split(";", 1)[0].startswith("image/"):
        raise ImageValidationError("unexpected_content_type")

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - packaging guarantees this dependency
        raise ImageValidationError("pillow_not_installed") from exc

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                detected_format = (probe.format or "").upper()
                probe.verify()
            with Image.open(BytesIO(data)) as decoded:
                decoded.load()
                width, height = decoded.size
    except Exception as exc:
        raise ImageValidationError("invalid_image_data") from exc

    extension = FORMAT_EXTENSIONS.get(detected_format)
    if not extension:
        raise ImageValidationError("unsupported_image_format")
    if width <= 0 or height <= 0:
        raise ImageValidationError("invalid_image_dimensions")

    return ImageInfo(
        width=width,
        height=height,
        format=detected_format,
        extension=extension,
        byte_count=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def inspect_image_file(path: Path, *, max_bytes: int = 50 * 1024 * 1024) -> ImageInfo:
    """Validate an existing regular file before treating it as resumable state."""

    if path.is_symlink() or not path.is_file():
        raise ImageValidationError("unsafe_existing_file")
    if path.stat().st_size > max_bytes:
        raise ImageValidationError("image_too_large")
    return inspect_image_bytes(path.read_bytes(), max_bytes=max_bytes)
