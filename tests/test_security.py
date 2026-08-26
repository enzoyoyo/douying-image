from pathlib import Path

import pytest

from scraper.security import (
    InputValidationError,
    is_expected_api_response,
    safe_output_path,
    validate_account_url,
    validate_media_url,
    validate_post_id,
)

ACCOUNT_ID = "MS4wLjABAAAAexample_account"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.douyin.com/user/MS4wLjABAAAAexample_account",
        "https://example.invalid/user/MS4wLjABAAAAexample_account",
        "https://www.douyin.com/video/1234567890123456789",
        "https://user:pass@www.douyin.com/user/MS4wLjABAAAAexample_account",
        "https://www.douyin.com:8443/user/MS4wLjABAAAAexample_account",
        "https://www.douyin.com/user/../private",
    ],
)
def test_account_url_rejects_unsupported_inputs(url: str) -> None:
    with pytest.raises(InputValidationError):
        validate_account_url(url)


def test_account_url_is_canonical_and_pseudonymous() -> None:
    target = validate_account_url(f"https://douyin.com/user/{ACCOUNT_ID}?modal_id=123#fragment")
    assert target.url == f"https://www.douyin.com/user/{ACCOUNT_ID}"
    assert target.account_id == ACCOUNT_ID
    assert target.account_ref.startswith("account_")
    assert ACCOUNT_ID not in target.account_ref


def test_api_response_is_origin_and_account_bound() -> None:
    valid = f"https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id={ACCOUNT_ID}&max_cursor=0"
    assert is_expected_api_response(valid, ACCOUNT_ID)
    assert not is_expected_api_response(
        "https://www.douyin.com/aweme/v1/web/aweme/post/?max_cursor=0", ACCOUNT_ID
    )
    assert not is_expected_api_response(valid.replace(ACCOUNT_ID, "different_account"), ACCOUNT_ID)
    foreign_origin = valid.replace("www.douyin.com", "example.invalid")
    assert not is_expected_api_response(foreign_origin, ACCOUNT_ID)


@pytest.mark.parametrize(
    "url",
    [
        "http://p3-sign.douyinpic.com/image/example",
        "https://127.0.0.1/image/example",
        "https://169.254.169.254/latest/meta-data",
        "https://example.invalid/image/example",
        "file:///tmp/example.png",
    ],
)
def test_media_url_rejects_ssrf_targets(url: str) -> None:
    with pytest.raises(InputValidationError):
        validate_media_url(url)


def test_media_url_accepts_known_image_cdn() -> None:
    url = "https://p3-sign.douyinpic.com/tos-cn-i-example/image.webp?signature=temporary"
    assert validate_media_url(url) == url


@pytest.mark.parametrize("value", ["../../escape", "/tmp/escape", "abc", "123/456", ""])
def test_post_id_is_strict(value: str) -> None:
    with pytest.raises(InputValidationError):
        validate_post_id(value)


def test_safe_output_path_stays_in_root(tmp_path: Path) -> None:
    output = safe_output_path(tmp_path, "post_0123456789abcdef_00.webp")
    assert output.parent == tmp_path.resolve()
    with pytest.raises(InputValidationError):
        safe_output_path(tmp_path, "../../escape.webp")
