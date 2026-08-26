import pytest

from scraper.images import (
    MAX_CANDIDATES_PER_IMAGE,
    ImageValidationError,
    image_candidates,
    inspect_image_bytes,
)

from .conftest import make_png


def test_candidates_preserve_quality_fallback_order_and_deduplicate() -> None:
    preferred = "https://p3-sign.douyinpic.com/original/a"
    fallback = "https://p9.douyinpic.com/original/b"
    image = {
        "watermark_free_download_url_list": [preferred],
        "url_list": [preferred, fallback, "https://example.invalid/not-allowed"],
    }
    assert image_candidates(image) == [preferred, fallback]


def test_candidate_count_is_hard_bounded() -> None:
    image = {
        "url_list": [
            f"https://p3-sign.douyinpic.com/path/image-{index}"
            for index in range(MAX_CANDIDATES_PER_IMAGE + 10)
        ]
    }

    assert len(image_candidates(image)) == MAX_CANDIDATES_PER_IMAGE


def test_image_validation_derives_real_metadata() -> None:
    info = inspect_image_bytes(make_png((13, 7)), content_type="image/png")
    assert (info.width, info.height, info.format, info.extension) == (13, 7, "PNG", ".png")
    assert len(info.sha256) == 64


@pytest.mark.parametrize(
    ("data", "content_type", "error_code"),
    [
        (b"<html>challenge</html>", "text/html", "unexpected_content_type"),
        (b"not an image", "image/webp", "invalid_image_data"),
        (b"", "image/png", "empty_body"),
    ],
)
def test_invalid_responses_are_not_images(data: bytes, content_type: str, error_code: str) -> None:
    with pytest.raises(ImageValidationError, match=error_code):
        inspect_image_bytes(data, content_type=content_type)


def test_size_limit_is_enforced() -> None:
    with pytest.raises(ImageValidationError, match="image_too_large"):
        inspect_image_bytes(make_png(), max_bytes=5)
