import json
from pathlib import Path

import pytest

import scraper.douyin_image_scraper as scraper_module
from scraper.douyin_image_scraper import DouyinImageScraper, DownloadResult, PlannedImage
from scraper.security import InputValidationError

ACCOUNT_ID = "MS4wLjABAAAAexample_account"


class FakeResponse:
    status = 200

    def __init__(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        self.url = url
        self._payload = payload
        self.headers = headers if headers is not None else {}
        self._body = (
            raw_body
            if raw_body is not None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        self.body_calls = 0

    def body(self) -> bytes:
        self.body_calls += 1
        return self._body


def account_url() -> str:
    return f"https://www.douyin.com/user/{ACCOUNT_ID}"


def api_url(account_id: str = ACCOUNT_ID) -> str:
    return f"https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id={account_id}&max_cursor=0"


def test_interceptor_rejects_foreign_and_unsafe_posts(tmp_path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    payload = {
        "has_more": 0,
        "max_cursor": 1,
        "aweme_list": [
            {
                "aweme_id": "7312345678901234567",
                "author": {"sec_uid": ACCOUNT_ID},
                "images": [],
            },
            {
                "aweme_id": "../../escape",
                "author": {"sec_uid": ACCOUNT_ID},
                "images": [],
            },
            {
                "aweme_id": "7398765432109876543",
                "author": {"sec_uid": "different_account"},
                "images": [],
            },
        ],
    }

    scraper._intercept_api(FakeResponse(api_url(), payload))

    assert list(scraper._posts) == ["7312345678901234567"]
    assert scraper._capture_complete is True
    assert "invalid_post_id_skipped" in scraper._warnings
    assert "foreign_or_unbound_post_skipped" in scraper._warnings


def test_interceptor_rejects_malicious_origin(tmp_path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    payload = {
        "has_more": 0,
        "aweme_list": [
            {
                "aweme_id": "7312345678901234567",
                "author": {"sec_uid": ACCOUNT_ID},
                "images": [],
            }
        ],
    }

    scraper._intercept_api(
        FakeResponse("https://example.invalid/aweme/v1/web/aweme/post/", payload)
    )

    assert scraper._posts == {}
    assert scraper._api_response_count == 0


def test_interceptor_does_not_claim_complete_after_post_limit(tmp_path: Path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
        max_items=1,
    )
    posts = [
        {
            "aweme_id": str(7312345678901234567 + index),
            "author": {"sec_uid": ACCOUNT_ID},
            "images": [],
        }
        for index in range(2)
    ]

    scraper._intercept_api(FakeResponse(api_url(), {"has_more": 0, "aweme_list": posts}))

    assert len(scraper._posts) == 1
    assert scraper._capture_complete is False
    assert scraper._completion_reason == "max_items"
    assert scraper._status([]) == "ok_limited"


def test_interceptor_rejects_declared_oversized_api_response(tmp_path: Path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    response = FakeResponse(
        api_url(),
        {"has_more": 0, "aweme_list": []},
        headers={"content-length": str(11 * 1024 * 1024)},
    )

    scraper._intercept_api(response)

    assert scraper._api_response_count == 0
    assert response.body_calls == 0
    assert scraper._capture_complete is False
    assert scraper._completion_reason == "api_response_too_large"
    assert scraper._status([]) == "partial_capture"
    assert "post_api_response_too_large" in scraper._warnings


@pytest.mark.parametrize("headers", [{}, {"content-length": "1"}])
def test_interceptor_bounds_actual_api_body_before_json_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    monkeypatch.setattr(scraper_module, "MAX_API_RESPONSE_BYTES", 32)
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    response = FakeResponse(
        api_url(),
        {},
        headers=headers,
        raw_body=b"{" + b" " * 32 + b"}",
    )

    scraper._intercept_api(response)

    assert response.body_calls == 1
    assert scraper._api_response_count == 0
    assert scraper._capture_complete is False
    assert scraper._completion_reason == "api_response_too_large"
    assert "post_api_response_too_large" in scraper._warnings


def test_interceptor_accepts_body_at_exact_limit_without_content_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"has_more": 0, "aweme_list": []}
    response = FakeResponse(api_url(), payload)
    monkeypatch.setattr(scraper_module, "MAX_API_RESPONSE_BYTES", len(response._body))
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )

    scraper._intercept_api(response)

    assert response.body_calls == 1
    assert scraper._api_response_count == 1
    assert scraper._capture_complete is True


def test_interceptor_rejects_invalid_json_body(tmp_path: Path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    response = FakeResponse(api_url(), {}, raw_body=b"\xff")

    scraper._intercept_api(response)

    assert response.body_calls == 1
    assert scraper._api_response_count == 0
    assert "post_api_invalid_json" in scraper._warnings


def test_interceptor_enforces_cumulative_api_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = FakeResponse(api_url(), {"has_more": 1, "aweme_list": []})
    second = FakeResponse(api_url(), {"has_more": 0, "aweme_list": []})
    third = FakeResponse(api_url(), {"has_more": 0, "aweme_list": []})
    monkeypatch.setattr(
        scraper_module,
        "MAX_API_CAPTURE_BYTES",
        len(first._body) + len(second._body) - 1,
    )
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )

    scraper._intercept_api(first)
    scraper._intercept_api(second)
    scraper._intercept_api(third)

    assert first.body_calls == 1
    assert second.body_calls == 1
    assert third.body_calls == 0
    assert scraper._api_response_count == 1
    assert scraper._capture_complete is False
    assert scraper._completion_reason == "api_capture_byte_limit"
    failed_record = scraper._public_record(
        PlannedImage("post_abc", 0, "post_abc_00", ()),
        DownloadResult.failed("not_attempted", attempts=0),
    )
    assert scraper._status([failed_record]) == "partial_capture"
    assert "post_api_capture_byte_limit_reached" in scraper._warnings


def test_interceptor_enforces_api_response_count_before_reading_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scraper_module, "MAX_API_RESPONSES", 1)
    first = FakeResponse(api_url(), {"has_more": 1, "aweme_list": []})
    second = FakeResponse(api_url(), {"has_more": 0, "aweme_list": []})
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )

    scraper._intercept_api(first)
    scraper._intercept_api(second)

    assert first.body_calls == 1
    assert second.body_calls == 0
    assert scraper._api_response_count == 1
    assert scraper._capture_complete is False
    assert scraper._completion_reason == "api_response_limit"
    assert "post_api_response_limit_reached" in scraper._warnings


def test_capture_reset_clears_api_safety_budgets(tmp_path: Path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    scraper._api_responses_seen = 7
    scraper._api_response_bytes_seen = 99
    scraper._api_capture_exhausted = True

    scraper._reset_capture_state()

    assert scraper._api_responses_seen == 0
    assert scraper._api_response_bytes_seen == 0
    assert scraper._api_capture_exhausted is False


def test_plan_uses_hashed_refs_and_safe_candidates(tmp_path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    posts = [
        (
            "7312345678901234567",
            {
                "images": [
                    {"url_list": ["https://p3-sign.douyinpic.com/path/image"]},
                    {"url_list": ["https://example.invalid/not-safe"]},
                ]
            },
        )
    ]

    plans = scraper._build_download_plan(posts)

    assert len(plans) == 2
    assert plans[0].post_ref.startswith("post_")
    assert "7312345678901234567" not in plans[0].stem
    assert plans[1].candidates == ()
    assert "image_without_safe_candidate" in scraper._warnings


def test_download_plan_has_a_hard_image_limit(tmp_path: Path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
        max_images=1,
    )
    posts = [
        (
            "7312345678901234567",
            {
                "images": [
                    {"url_list": ["https://p3-sign.douyinpic.com/path/one"]},
                    {"url_list": ["https://p3-sign.douyinpic.com/path/two"]},
                ]
            },
        )
    ]

    assert len(scraper._build_download_plan(posts)) == 1
    assert "total_image_limit_reached" in scraper._warnings


def test_download_plans_enforce_cumulative_byte_limit(tmp_path: Path) -> None:
    class FakeDownloader:
        max_bytes = 1_024
        calls = 0

        def download(self, _candidates, _root, stem: str) -> DownloadResult:
            self.calls += 1
            return DownloadResult(
                status="downloaded",
                file=f"{stem}.png",
                byte_count=1_024,
                sha256="0" * 64,
                width=1,
                height=1,
                format="PNG",
                attempts=1,
                error_code=None,
            )

    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
        max_file_bytes=1_024,
        max_total_bytes=1_024,
    )
    plans = [
        PlannedImage("post_a", 0, "post_a_00", ("https://p3-sign.douyinpic.com/a",)),
        PlannedImage("post_b", 0, "post_b_00", ("https://p3-sign.douyinpic.com/b",)),
    ]
    downloader = FakeDownloader()

    records = scraper._download_plans(plans, downloader)  # type: ignore[arg-type]

    assert downloader.calls == 1
    assert [record["status"] for record in records] == ["downloaded", "failed"]
    assert records[1]["error_code"] == "total_byte_limit"
    assert "total_byte_limit_reached" in scraper._warnings


def test_generated_download_directory_is_private(tmp_path: Path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )

    scraper._prepare_output_dirs()

    assert scraper.download_dir.stat().st_mode & 0o777 == 0o700


def test_generated_download_directory_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    output_root = tmp_path / "images"
    output_root.mkdir()
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(output_root),
        manifest_dir=str(tmp_path / "manifest"),
    )
    scraper.download_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(InputValidationError, match="download_dir_symlink_not_allowed"):
        scraper._prepare_output_dirs()


def test_existing_download_directory_permissions_are_not_mutated(tmp_path: Path) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    scraper.download_dir.mkdir(parents=True, mode=0o755)

    with pytest.raises(InputValidationError, match="download_dir_permissions_too_open"):
        scraper._prepare_output_dirs()

    assert scraper.download_dir.stat().st_mode & 0o777 == 0o755


def test_existing_manifest_directory_permissions_are_not_mutated(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir(mode=0o755)
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(manifest_dir),
    )

    with pytest.raises(InputValidationError, match="manifest_dir_permissions_too_open"):
        scraper._prepare_output_dirs()

    assert manifest_dir.stat().st_mode & 0o777 == 0o755


def test_existing_session_directory_permissions_are_not_mutated(tmp_path: Path) -> None:
    session_dir = tmp_path / "shared-session"
    session_dir.mkdir(mode=0o755)
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
        session_dir=str(session_dir),
    )

    with pytest.raises(InputValidationError, match="session_dir_permissions_too_open"):
        scraper._prepare_session_dir()

    assert session_dir.stat().st_mode & 0o777 == 0o755


def test_new_session_directory_gets_private_marker(tmp_path: Path) -> None:
    session_dir = tmp_path / "dedicated-session"
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
        session_dir=str(session_dir),
    )

    assert scraper._prepare_session_dir() == session_dir.resolve()
    marker = session_dir / ".douyin-image-session"
    assert marker.is_file()
    assert marker.stat().st_mode & 0o777 == 0o600


def test_nonempty_unmanaged_session_directory_is_rejected(tmp_path: Path) -> None:
    session_dir = tmp_path / "not-managed"
    session_dir.mkdir(mode=0o700)
    (session_dir / "Cookies").write_text("synthetic", encoding="utf-8")
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
        session_dir=str(session_dir),
    )

    with pytest.raises(InputValidationError, match="session_dir_not_tool_managed"):
        scraper._prepare_session_dir()


@pytest.mark.parametrize(
    "reason,status", [("interrupted", "interrupted"), ("runtime_error", "runtime_error")]
)
def test_aborted_run_cannot_report_success(tmp_path: Path, reason: str, status: str) -> None:
    scraper = DouyinImageScraper(
        account_url(),
        output_dir=str(tmp_path / "images"),
        manifest_dir=str(tmp_path / "manifest"),
    )
    scraper._api_response_count = 1
    scraper._capture_complete = True
    scraper._completion_reason = reason
    plan = PlannedImage("post_abc", 0, "post_abc_00", ())
    record = scraper._public_record(
        plan,
        DownloadResult.failed("not_attempted", attempts=0),
    )

    assert scraper._status([record]) == status
