import pytest

from scraper.cli import build_parser


def test_cli_defaults_are_bounded() -> None:
    args = build_parser().parse_args(["https://www.douyin.com/user/MS4wLjABAAAAexample_account"])
    assert args.max_scroll == 50
    assert args.retries == 3
    assert args.max_file_mb == 50
    assert args.max_items == 1_000
    assert args.max_images == 5_000
    assert args.max_total_mb == 5_120
    assert args.session_dir is None


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_cli_rejects_nonfinite_scroll_pause(value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(
            [
                "https://www.douyin.com/user/MS4wLjABAAAAexample_account",
                "--scroll-pause",
                value,
            ]
        )

    assert exc_info.value.code == 2
