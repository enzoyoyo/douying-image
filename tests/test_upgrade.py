"""Tests for API pagination, concurrency, and download UA options."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from scraper.douyin_image_scraper import (
    CALLER_OWNED_PARAMS,
    PAGE_OWNED_PARAMS,
    DouyinImageScraper,
    PlannedImage,
)
from scraper.downloader import DEFAULT_DOWNLOAD_USER_AGENT, ImageDownloader

from .conftest import make_png

ACCOUNT_URL = "https://www.douyin.com/user/MS4wLjABAAAA_test"


def make_scraper(**kwargs) -> DouyinImageScraper:
    defaults = {"output_dir": str(kwargs.pop("tmp_path") / "out")}
    defaults.update(kwargs)
    return DouyinImageScraper(ACCOUNT_URL, **defaults)


class FakePage:
    """Playwright page double: returns canned API payloads from evaluate()."""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []
        self.waits: list[int] = []

    def evaluate(self, script: str, url: str) -> dict:
        self.calls.append(url)
        index = len(self.calls) - 1
        payload = self.payloads[index] if index < len(self.payloads) else {}
        return {"status": 200, "text": json.dumps(payload)}

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def post_payload(post_id: str, has_more: bool, cursor: int) -> dict:
    return {
        "has_more": has_more,
        "max_cursor": cursor,
        "aweme_list": [
            {
                "aweme_id": post_id,
                "author": {"sec_uid": "MS4wLjABAAAA_test"},
                "images": [{"url_list": [f"https://p3-sign.douyinpic.com/{post_id}"]}],
            }
        ],
    }


def test_business_query_drops_page_and_caller_params() -> None:
    query = (
        "device_platform=webapp&aid=6383&msToken=abc&a_bogus=xyz&uifid=u1"
        "&sec_user_id=SEC&max_cursor=0&count=20&version_code=170400"
    )
    kept = DouyinImageScraper._business_query(query)
    names = {pair.split("=", 1)[0] for pair in kept.split("&")}
    assert names == {"device_platform", "aid", "version_code"}
    for name in PAGE_OWNED_PARAMS | CALLER_OWNED_PARAMS:
        assert name not in names


def test_sample_request_query_uses_only_post_api(tmp_path: Path) -> None:
    scraper = make_scraper(tmp_path=tmp_path)
    scraper._sample_request_query("https://www.douyin.com/aweme/v1/web/other/?aid=6383")
    assert scraper._sampled_api_query is None
    scraper._sample_request_query(
        "https://www.douyin.com/aweme/v1/web/aweme/post/?aid=6383&msToken=t"
    )
    assert scraper._sampled_api_query == "aid=6383"


def test_api_paginate_merges_pages_until_has_more_false(tmp_path: Path) -> None:
    scraper = make_scraper(tmp_path=tmp_path, page_interval_ms=0)
    scraper._sampled_api_query = "aid=6383"
    page = FakePage(
        [
            post_payload("7300000000000000001", True, 10),
            post_payload("7300000000000000002", False, 0),
        ]
    )

    assert scraper._api_paginate(page) is True
    assert sorted(scraper._posts) == ["7300000000000000001", "7300000000000000002"]
    assert scraper._capture_complete is True
    assert scraper._completion_reason == "api_exhausted"
    assert len(page.calls) == 2
    assert "sec_user_id=MS4wLjABAAAA_test" in page.calls[0]
    assert "max_cursor=10" in page.calls[1]


def test_api_paginate_stops_on_repeated_cursor(tmp_path: Path) -> None:
    scraper = make_scraper(tmp_path=tmp_path, page_interval_ms=0)
    scraper._sampled_api_query = "aid=6383"
    page = FakePage(
        [
            post_payload("7300000000000000001", True, 10),
            post_payload("7300000000000000002", True, 10),
        ]
    )

    assert scraper._api_paginate(page) is True
    assert len(page.calls) == 2
    assert scraper._completion_reason == "cursor_stalled"


def test_api_paginate_falls_back_without_sampled_request(tmp_path: Path) -> None:
    scraper = make_scraper(tmp_path=tmp_path)
    page = FakePage([])

    assert scraper._api_paginate(page) is False
    assert "api_pagination_no_sampled_request" in scraper._warnings


def test_concurrent_downloads_preserve_order_and_budget(tmp_path: Path) -> None:
    import threading

    image = make_png((5, 5))
    lock = threading.Lock()

    class threaded_factory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, host: str, *, port: int, timeout: float):
            with lock:
                self.calls += 1
            return _single_connection(image)

    scraper = make_scraper(tmp_path=tmp_path, concurrency=3)
    factory = threaded_factory()
    downloader = ImageDownloader(connection_factory=factory, sleep_fn=lambda _d: None)
    plans = [
        PlannedImage(
            post_ref=f"post_{index:04d}",
            image_index=0,
            stem=f"post_{index:04d}_00",
            candidates=("https://p3-sign.douyinpic.com/img",),
        )
        for index in range(9)
    ]

    records = scraper._download_plans(plans, downloader)

    assert len(records) == 9
    assert [record["post_ref"] for record in records] == [p.post_ref for p in plans]
    assert all(record["status"] == "downloaded" for record in records)
    assert factory.calls == 9


def _single_connection(image: bytes):
    class Resp:
        def __init__(self) -> None:
            self.status = 200
            self.headers = {"content-type": "image/png", "content-length": str(len(image))}
            self.offset = 0

        def read(self, size: int) -> bytes:
            chunk = image[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def getheaders(self) -> list[tuple[str, str]]:
            return list(self.headers.items())

        def close(self) -> None:
            return None

    class Conn:
        def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
            assert headers["User-Agent"] == DEFAULT_DOWNLOAD_USER_AGENT

        def getresponse(self) -> Resp:
            return Resp()

        def close(self) -> None:
            return None

    return Conn()


def test_downloader_default_user_agent_is_desktop_chrome(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class Conn:
        def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
            seen.update(headers)

        def getresponse(self):
            raise RuntimeError("stop before body")

        def close(self) -> None:
            return None

    def factory(host: str, *, port: int, timeout: float) -> Conn:
        return Conn()

    downloader = ImageDownloader(connection_factory=factory, retries=0)
    with contextlib.suppress(Exception):
        downloader.download(["https://p3-sign.douyinpic.com/img"], tmp_path, "x_00")
    assert seen["User-Agent"] == DEFAULT_DOWNLOAD_USER_AGENT

    custom = ImageDownloader(
        connection_factory=factory, retries=0, user_agent="custom-agent/2.0"
    )
    with contextlib.suppress(Exception):
        custom.download(["https://p3-sign.douyinpic.com/img"], tmp_path, "y_00")
    assert seen["User-Agent"] == "custom-agent/2.0"
