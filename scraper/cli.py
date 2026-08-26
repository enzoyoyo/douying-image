"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Sequence

from .douyin_image_scraper import DependencyUnavailableError, DouyinImageScraper
from .security import InputValidationError

EXIT_CODES = {
    "ok": 0,
    "ok_limited": 0,
    "no_image_posts": 0,
    "capture_failed": 3,
    "partial_capture": 4,
    "partial_download": 5,
    "runtime_error": 7,
    "interrupted": 130,
}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _pause_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.1:
        raise argparse.ArgumentTypeError("must be a finite value of at least 0.1 seconds")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="douyin-image",
        description="Download verified original images from a Douyin account's photo posts.",
    )
    parser.add_argument("url", help="HTTPS Douyin account URL")
    parser.add_argument(
        "--output-dir",
        default="downloads/account_images",
        help="image output root",
    )
    parser.add_argument(
        "--manifest-dir",
        default="output",
        help="privacy-minimal manifest output root",
    )
    parser.add_argument("--max-scroll", type=_positive_int, default=50)
    parser.add_argument("--stall-rounds", type=_positive_int, default=5)
    parser.add_argument(
        "--max-items",
        type=_positive_int,
        default=1_000,
        help="maximum captured posts",
    )
    parser.add_argument("--max-images", type=_positive_int, default=5_000)
    parser.add_argument("--scroll-pause", type=_pause_seconds, default=2.0)
    parser.add_argument("--retries", type=_nonnegative_int, default=3)
    parser.add_argument("--request-timeout", type=_positive_int, default=30)
    parser.add_argument("--max-file-mb", type=_positive_int, default=50)
    parser.add_argument("--max-total-mb", type=_positive_int, default=5_120)
    parser.add_argument("--redownload", action="store_true")
    parser.add_argument(
        "--session-dir",
        default=None,
        help=(
            "dedicated Playwright session directory; never point this at a personal browser profile"
        ),
    )
    parser.add_argument(
        "--login-wait",
        type=_nonnegative_int,
        default=0,
        help="headed-mode seconds reserved for manual login",
    )
    parser.add_argument("--no-headless", action="store_true", help="show the browser")
    parser.add_argument("--json", action="store_true", help="print only the result JSON to stdout")
    parser.add_argument(
        "--log-level",
        choices=("error", "warning", "info"),
        default="info",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        scraper = DouyinImageScraper(
            args.url,
            output_dir=args.output_dir,
            manifest_dir=args.manifest_dir,
            max_scroll=args.max_scroll,
            stall_rounds=args.stall_rounds,
            max_items=args.max_items,
            max_images=args.max_images,
            scroll_pause_ms=round(args.scroll_pause * 1_000),
            headless=not args.no_headless,
            session_dir=args.session_dir,
            login_wait_seconds=args.login_wait,
            retries=args.retries,
            request_timeout_ms=args.request_timeout * 1_000,
            max_file_bytes=args.max_file_mb * 1024 * 1024,
            max_total_bytes=args.max_total_mb * 1024 * 1024,
            redownload=args.redownload,
        )
        result = scraper.run()
    except InputValidationError as exc:
        print(f"input_error: {exc}", file=sys.stderr)
        return 2
    except DependencyUnavailableError as exc:
        print(f"dependency_error: {exc}", file=sys.stderr)
        return 6

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"status: {result['status']}")
        print(f"captured posts: {result['captured_posts']}")
        print(f"photo posts: {result['image_posts']}")
        print(
            "images: "
            f"{result['downloaded']} downloaded, "
            f"{result['reused_verified']} reused, "
            f"{result['failed']} failed"
        )
        print(f"manifest: {result['manifest_file']}")

    return EXIT_CODES.get(result["status"], 1)
