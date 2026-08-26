#!/usr/bin/env python3
"""Capture and download original images from a Douyin account's photo posts."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # Support direct execution without duplicating the CLI.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper.downloader import DownloadResult, ImageDownloader
from scraper.images import image_candidates
from scraper.manifest import build_manifest, write_private_json
from scraper.security import (
    AccountTarget,
    InputValidationError,
    is_expected_api_response,
    request_is_bound_to_account,
    stable_ref,
    validate_account_url,
    validate_post_id,
)

LOGGER = logging.getLogger(__name__)
SESSION_MARKER = ".douyin-image-session"
MAX_API_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_API_CAPTURE_BYTES = 100 * 1024 * 1024
MAX_API_RESPONSES = 128
MAX_IMAGES_PER_POST = 100
MAX_ALLOWED_POSTS = 10_000
MAX_ALLOWED_IMAGES = 20_000
MAX_ALLOWED_TOTAL_BYTES = 100 * 1024 * 1024 * 1024


class DependencyUnavailableError(RuntimeError):
    """Raised when a runtime dependency is missing."""


@dataclass(frozen=True)
class PlannedImage:
    """Internal download plan. Source identifiers never enter the public manifest."""

    post_ref: str
    image_index: int
    stem: str
    candidates: tuple[str, ...]


class DouyinImageScraper:
    """A narrow, privacy-minimal downloader for account photo posts."""

    def __init__(
        self,
        account_url: str,
        *,
        output_dir: str = "downloads/account_images",
        manifest_dir: str = "output",
        max_scroll: int = 50,
        stall_rounds: int = 5,
        max_items: int = 1_000,
        max_images: int = 5_000,
        scroll_pause_ms: int = 2_000,
        headless: bool = True,
        session_dir: str | None = None,
        login_wait_seconds: int = 0,
        retries: int = 3,
        request_timeout_ms: int = 30_000,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_total_bytes: int = 5 * 1024 * 1024 * 1024,
        redownload: bool = False,
    ) -> None:
        self.target: AccountTarget = validate_account_url(account_url)
        if max_scroll <= 0:
            raise InputValidationError("max_scroll_must_be_positive")
        if stall_rounds <= 0:
            raise InputValidationError("stall_rounds_must_be_positive")
        if max_items <= 0 or max_items > MAX_ALLOWED_POSTS:
            raise InputValidationError("max_items_out_of_range")
        if max_images <= 0 or max_images > MAX_ALLOWED_IMAGES:
            raise InputValidationError("max_images_out_of_range")
        if scroll_pause_ms < 100:
            raise InputValidationError("scroll_pause_too_short")
        if login_wait_seconds < 0:
            raise InputValidationError("login_wait_must_be_nonnegative")
        if retries < 0 or retries > 10:
            raise InputValidationError("retries_out_of_range")
        if request_timeout_ms < 1_000:
            raise InputValidationError("request_timeout_too_short")
        if max_file_bytes < 1_024:
            raise InputValidationError("max_file_size_too_small")
        if max_total_bytes < 1_024 or max_total_bytes > MAX_ALLOWED_TOTAL_BYTES:
            raise InputValidationError("max_total_size_out_of_range")

        self.output_base = Path(output_dir).expanduser()
        self.manifest_base = Path(manifest_dir).expanduser()
        self.download_dir = self.output_base / self.target.account_ref
        self.manifest_path = self.manifest_base / f"{self.target.account_ref}_manifest.json"
        self.max_scroll = max_scroll
        self.stall_rounds = stall_rounds
        self.max_items = max_items
        self.max_images = max_images
        self.scroll_pause_ms = scroll_pause_ms
        self.headless = headless
        self.session_dir = Path(session_dir).expanduser() if session_dir else None
        self.login_wait_seconds = login_wait_seconds
        self.retries = retries
        self.request_timeout_ms = request_timeout_ms
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.redownload = redownload

        self._reset_capture_state()

    def _reset_capture_state(self) -> None:
        self._posts: dict[str, dict] = {}
        self._seen_cursors: set[str] = set()
        self._api_response_count = 0
        self._api_responses_seen = 0
        self._api_response_bytes_seen = 0
        self._api_capture_exhausted = False
        self._last_has_more: bool | None = None
        self._capture_complete = False
        self._completion_reason = "not_started"
        self._warnings: set[str] = set()
        self._limit_reasons: set[str] = set()

    @staticmethod
    def _as_bool(value: object) -> bool:
        if isinstance(value, str):
            return value.lower() not in {"", "0", "false", "none"}
        return bool(value)

    def _post_belongs_to_target(self, post: dict, response_url: str) -> bool:
        author = post.get("author")
        author_id = author.get("sec_uid") if isinstance(author, dict) else None
        if author_id:
            return author_id == self.target.account_id
        return request_is_bound_to_account(response_url, self.target.account_id)

    def _merge_post(self, post_id: str, post: dict) -> None:
        existing = self._posts.get(post_id)
        if existing is None:
            if len(self._posts) < self.max_items:
                self._posts[post_id] = post
            else:
                self._limit_reasons.add("max_items")
                self._completion_reason = "max_items"
            return

        old_images = existing.get("images") if isinstance(existing.get("images"), list) else []
        new_images = post.get("images") if isinstance(post.get("images"), list) else []
        if len(new_images) > len(old_images):
            self._posts[post_id] = post

    def _stop_for_api_limit(self, reason: str, warning: str) -> None:
        """Mark capture partial after an API response safety budget is exhausted."""

        self._api_capture_exhausted = True
        self._capture_complete = False
        self._completion_reason = reason
        self._limit_reasons.add(reason)
        self._warnings.add(warning)

    def _intercept_api(self, response) -> None:
        """Accept only target-bound post responses from the canonical API origin."""

        response_url = str(getattr(response, "url", ""))
        if not is_expected_api_response(response_url, self.target.account_id):
            return
        if int(getattr(response, "status", 0)) != 200:
            self._warnings.add("post_api_http_error")
            return
        if self._api_capture_exhausted:
            return
        if self._api_responses_seen >= MAX_API_RESPONSES:
            self._stop_for_api_limit("api_response_limit", "post_api_response_limit_reached")
            return
        self._api_responses_seen += 1

        try:
            headers = {
                str(key).lower(): str(value)
                for key, value in getattr(response, "headers", {}).items()
            }
            raw_content_length = headers.get("content-length")
            if raw_content_length is not None:
                content_length = int(raw_content_length)
                if content_length < 0:
                    raise ValueError
                if content_length > MAX_API_RESPONSE_BYTES:
                    self._stop_for_api_limit(
                        "api_response_too_large",
                        "post_api_response_too_large",
                    )
                    return
        except (TypeError, ValueError):
            self._warnings.add("post_api_invalid_content_length")
            return

        try:
            body = response.body()
        except Exception:
            self._warnings.add("post_api_body_unavailable")
            return
        if not isinstance(body, bytes):
            self._warnings.add("post_api_invalid_body")
            return
        if len(body) > MAX_API_RESPONSE_BYTES:
            self._stop_for_api_limit(
                "api_response_too_large",
                "post_api_response_too_large",
            )
            return
        if self._api_response_bytes_seen + len(body) > MAX_API_CAPTURE_BYTES:
            self._stop_for_api_limit(
                "api_capture_byte_limit",
                "post_api_capture_byte_limit_reached",
            )
            return
        self._api_response_bytes_seen += len(body)

        try:
            payload = json.loads(body.decode())
        except Exception:
            self._warnings.add("post_api_invalid_json")
            return
        if not isinstance(payload, dict):
            self._warnings.add("post_api_invalid_payload")
            return

        self._api_response_count += 1
        has_more = self._as_bool(payload.get("has_more", False))
        self._last_has_more = has_more

        cursor = payload.get("max_cursor")
        if cursor is not None:
            cursor_text = str(cursor)
            if cursor_text in self._seen_cursors and has_more:
                self._warnings.add("cursor_repeated")
            self._seen_cursors.add(cursor_text)

        raw_posts = payload.get("aweme_list", [])
        if not isinstance(raw_posts, list):
            self._warnings.add("post_list_invalid")
            return

        for post in raw_posts:
            if not isinstance(post, dict) or not self._post_belongs_to_target(post, response_url):
                self._warnings.add("foreign_or_unbound_post_skipped")
                continue
            try:
                post_id = validate_post_id(post.get("aweme_id"))
            except InputValidationError:
                self._warnings.add("invalid_post_id_skipped")
                continue
            self._merge_post(post_id, post)

        if not has_more:
            if "max_items" in self._limit_reasons:
                self._capture_complete = False
                self._completion_reason = "max_items"
            else:
                self._capture_complete = True
                self._completion_reason = "api_exhausted"

    def _scroll_to_load(self, page) -> None:
        """Let the first-party page generate signed pagination requests."""

        if self._capture_complete or self._api_capture_exhausted:
            return

        previous_count = len(self._posts)
        stale_rounds = 0
        for round_index in range(self.max_scroll):
            if len(self._posts) >= self.max_items:
                self._limit_reasons.add("max_items")
                self._completion_reason = "max_items"
                return

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(self.scroll_pause_ms)
            current_count = len(self._posts)

            if self._capture_complete or self._api_capture_exhausted:
                return
            if current_count == previous_count:
                stale_rounds += 1
            else:
                stale_rounds = 0
            previous_count = current_count

            LOGGER.info("capture round %s: %s posts", round_index + 1, current_count)
            if stale_rounds >= self.stall_rounds:
                self._completion_reason = (
                    "cursor_stalled" if "cursor_repeated" in self._warnings else "stalled"
                )
                return

        self._completion_reason = "max_scroll"

    def _image_posts(self) -> list[tuple[str, dict]]:
        return [
            (post_id, post)
            for post_id, post in self._posts.items()
            if isinstance(post.get("images"), list) and post["images"]
        ]

    def _build_download_plan(self, image_posts: list[tuple[str, dict]]) -> list[PlannedImage]:
        plans: list[PlannedImage] = []
        for post_id, post in image_posts:
            post_ref = stable_ref("post", post_id)
            images = post.get("images", [])
            if len(images) > MAX_IMAGES_PER_POST:
                self._warnings.add("per_post_image_limit_reached")
            for index, image in enumerate(images[:MAX_IMAGES_PER_POST]):
                if len(plans) >= self.max_images:
                    self._warnings.add("total_image_limit_reached")
                    return plans
                candidates = tuple(image_candidates(image))
                if not candidates:
                    self._warnings.add("image_without_safe_candidate")
                stem = f"{post_ref}_{index:02d}"
                plans.append(
                    PlannedImage(
                        post_ref=post_ref,
                        image_index=index,
                        stem=stem,
                        candidates=candidates,
                    )
                )
        return plans

    @staticmethod
    def _public_record(plan: PlannedImage, result: DownloadResult) -> dict:
        return {
            "post_ref": plan.post_ref,
            "image_index": plan.image_index,
            "candidate_count": len(plan.candidates),
            "status": result.status,
            "file": result.file,
            "bytes": result.byte_count,
            "sha256": result.sha256,
            "width": result.width,
            "height": result.height,
            "format": result.format,
            "attempts": result.attempts,
            "error_code": result.error_code,
        }

    def _prepare_session_dir(self) -> Path | None:
        if self.session_dir is None:
            return None
        if self.session_dir.is_symlink():
            raise InputValidationError("session_dir_symlink_not_allowed")
        existed = self.session_dir.exists()
        if existed and not self.session_dir.is_dir():
            raise InputValidationError("session_dir_must_be_directory")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if not existed:
            os.chmod(self.session_dir, 0o700)
        elif os.name != "nt" and self.session_dir.stat().st_mode & 0o077:
            raise InputValidationError("session_dir_permissions_too_open")

        marker = self.session_dir / SESSION_MARKER
        if marker.is_symlink() or (marker.exists() and not marker.is_file()):
            raise InputValidationError("session_marker_invalid")
        if not marker.exists():
            if existed and any(self.session_dir.iterdir()):
                raise InputValidationError("session_dir_not_tool_managed")
            marker.touch(mode=0o600, exist_ok=False)
        if os.name != "nt":
            os.chmod(marker, 0o600)
        return self.session_dir.resolve()

    def _prepare_output_dirs(self) -> None:
        """Create generated directories without following a pre-created account symlink."""

        if self.download_dir.is_symlink():
            raise InputValidationError("download_dir_symlink_not_allowed")
        existed = self.download_dir.exists()
        if existed and not self.download_dir.is_dir():
            raise InputValidationError("download_dir_must_be_directory")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if not existed:
            os.chmod(self.download_dir, 0o700)
        elif os.name != "nt" and self.download_dir.stat().st_mode & 0o077:
            raise InputValidationError("download_dir_permissions_too_open")

        if self.manifest_base.is_symlink():
            raise InputValidationError("manifest_dir_symlink_not_allowed")
        manifest_existed = self.manifest_base.exists()
        if manifest_existed and not self.manifest_base.is_dir():
            raise InputValidationError("manifest_dir_must_be_directory")
        self.manifest_base.mkdir(parents=True, exist_ok=True)
        if not manifest_existed:
            os.chmod(self.manifest_base, 0o700)
        elif os.name != "nt" and self.manifest_base.stat().st_mode & 0o077:
            raise InputValidationError("manifest_dir_permissions_too_open")

    def _download_plans(
        self,
        plans: list[PlannedImage],
        downloader: ImageDownloader,
    ) -> list[dict]:
        records: list[dict] = []
        accounted_bytes = 0
        for position, plan in enumerate(plans, start=1):
            LOGGER.info("download %s/%s", position, len(plans))
            remaining_bytes = self.max_total_bytes - accounted_bytes
            if remaining_bytes < 1_024:
                self._warnings.add("total_byte_limit_reached")
                for pending_plan in plans[position - 1 :]:
                    records.append(
                        self._public_record(
                            pending_plan,
                            DownloadResult.failed("total_byte_limit", attempts=0),
                        )
                    )
                break

            downloader.max_bytes = min(self.max_file_bytes, remaining_bytes)
            result = downloader.download(plan.candidates, self.download_dir, plan.stem)
            if result.error_code == "image_too_large" and remaining_bytes < self.max_file_bytes:
                result = DownloadResult.failed("total_byte_limit", attempts=result.attempts)
                self._warnings.add("total_byte_limit_reached")
            records.append(self._public_record(plan, result))
            accounted_bytes += result.byte_count
        return records

    def _open_browser(self, playwright):
        session_dir = self._prepare_session_dir()
        if session_dir:
            context = playwright.chromium.launch_persistent_context(
                str(session_dir),
                headless=self.headless,
                locale="zh-CN",
                viewport={"width": 1440, "height": 960},
            )
            page = context.pages[0] if context.pages else context.new_page()
            return None, context, page

        browser = playwright.chromium.launch(headless=self.headless)
        context = browser.new_context(
            locale="zh-CN",
            viewport={"width": 1440, "height": 960},
        )
        return browser, context, context.new_page()

    def _status(self, records: list[dict]) -> str:
        if self._completion_reason == "interrupted":
            return "interrupted"
        if self._completion_reason == "runtime_error":
            return "runtime_error"
        if self._api_capture_exhausted:
            return "partial_capture"
        failed = sum(record["status"] == "failed" for record in records)
        if self._api_response_count == 0:
            return "capture_failed"
        if failed:
            return "partial_download"
        if not self._capture_complete and self._completion_reason != "max_items":
            return "partial_capture"
        if (
            self._completion_reason == "max_items"
            or {
                "per_post_image_limit_reached",
                "total_image_limit_reached",
            }
            & self._warnings
        ):
            return "ok_limited"
        if not records:
            return "no_image_posts"
        return "ok"

    def run(self) -> dict:
        """Execute capture, verified downloads, and a privacy-minimal manifest."""

        self._reset_capture_state()
        self._prepare_output_dirs()
        records: list[dict] = []
        image_posts: list[tuple[str, dict]] = []
        plans: list[PlannedImage] = []

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DependencyUnavailableError("playwright_not_installed") from exc

        browser = None
        context = None
        try:
            with sync_playwright() as playwright:
                try:
                    try:
                        browser, context, page = self._open_browser(playwright)
                    except PlaywrightError as exc:
                        message = str(exc)
                        if "Executable doesn't exist" in message or "playwright install" in message:
                            raise DependencyUnavailableError("chromium_not_installed") from exc
                        raise
                    page.on("response", self._intercept_api)

                    try:
                        page.goto(self.target.url, wait_until="domcontentloaded", timeout=60_000)
                    except Exception:
                        self._warnings.add("navigation_failed")

                    initial_wait_ms = 3_000 + self.login_wait_seconds * 1_000
                    page.wait_for_timeout(initial_wait_ms)
                    self._scroll_to_load(page)

                    if self._completion_reason == "not_started":
                        self._completion_reason = "initial_page_complete"

                    image_posts = self._image_posts()
                    plans = self._build_download_plan(image_posts)
                    downloader = ImageDownloader(
                        retries=self.retries,
                        timeout_ms=self.request_timeout_ms,
                        max_bytes=self.max_file_bytes,
                        redownload=self.redownload,
                    )

                    records = self._download_plans(plans, downloader)
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            self._warnings.add("context_close_failed")
                        context = None
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            self._warnings.add("browser_close_failed")
                        browser = None
        except (DependencyUnavailableError, InputValidationError):
            raise
        except KeyboardInterrupt:
            self._completion_reason = "interrupted"
            self._warnings.add("run_interrupted")
        except Exception:
            self._completion_reason = "runtime_error"
            self._warnings.add("runtime_error")

        for plan in plans[len(records) :]:
            records.append(
                self._public_record(
                    plan,
                    DownloadResult.failed("not_attempted", attempts=0),
                )
            )

        manifest = build_manifest(
            account_ref=self.target.account_ref,
            capture_complete=self._capture_complete,
            completion_reason=self._completion_reason,
            api_response_count=self._api_response_count,
            captured_post_count=len(self._posts),
            image_post_count=len(image_posts),
            records=records,
            warnings=sorted(self._warnings),
            limits={
                "max_posts": self.max_items,
                "max_images": self.max_images,
                "max_api_responses": MAX_API_RESPONSES,
                "max_api_response_bytes": MAX_API_RESPONSE_BYTES,
                "max_api_capture_bytes": MAX_API_CAPTURE_BYTES,
                "max_image_bytes": self.max_file_bytes,
                "max_total_bytes": self.max_total_bytes,
            },
        )
        write_private_json(self.manifest_path, manifest)

        status = self._status(records)
        return {
            "status": status,
            "account_ref": self.target.account_ref,
            "captured_posts": len(self._posts),
            "image_posts": len(image_posts),
            "planned_images": len(records),
            "downloaded": manifest["downloads"]["downloaded"],
            "reused_verified": manifest["downloads"]["reused_verified"],
            "failed": manifest["downloads"]["failed"],
            "bytes_accounted": manifest["downloads"]["bytes_accounted"],
            "capture_complete": self._capture_complete,
            "completion_reason": self._completion_reason,
            "output_folder": self.download_dir.name,
            "manifest_file": self.manifest_path.name,
            "warnings": sorted(self._warnings),
        }


if __name__ == "__main__":
    from scraper.cli import main

    raise SystemExit(main())
