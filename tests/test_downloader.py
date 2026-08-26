import threading
import time
from pathlib import Path

from scraper.downloader import ImageDownloader

from .conftest import make_png


class FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        self.status = status
        self._body = body
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(body)),
        }
        self.closed = False
        self.offset = 0

    def read(self, size: int) -> bytes:
        chunk = self._body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers.items())

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.request_target: str | None = None

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        assert method == "GET"
        assert headers["Accept-Encoding"] == "identity"
        self.request_target = target

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.response.close()


class FakeConnectionFactory:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, host: str, *, port: int, timeout: float) -> FakeConnection:
        assert host.endswith("douyinpic.com")
        assert port == 443
        assert 0 < timeout <= 30
        response = self.responses[self.calls]
        self.calls += 1
        return FakeConnection(response)


def test_download_is_verified_and_atomic(tmp_path: Path) -> None:
    image = make_png((11, 9))
    context = FakeConnectionFactory([FakeResponse(200, image, "image/png")])
    downloader = ImageDownloader(connection_factory=context, sleep_fn=lambda _delay: None)

    result = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_00")

    assert result.status == "downloaded"
    assert result.file == "post_abc_00.png"
    assert (tmp_path / result.file).read_bytes() == image
    assert not list(tmp_path.glob("*.part"))


def test_html_200_is_a_failure(tmp_path: Path) -> None:
    context = FakeConnectionFactory([FakeResponse(200, b"<html>blocked</html>", "text/html")])
    downloader = ImageDownloader(
        connection_factory=context, retries=0, sleep_fn=lambda _delay: None
    )

    result = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_00")

    assert result.status == "failed"
    assert result.error_code == "unexpected_content_type"
    assert not list(tmp_path.iterdir())


def test_retry_then_candidate_success(tmp_path: Path) -> None:
    image = make_png()
    context = FakeConnectionFactory(
        [
            FakeResponse(503, b"", "text/plain"),
            FakeResponse(200, image, "image/png"),
        ]
    )
    downloader = ImageDownloader(
        connection_factory=context, retries=1, sleep_fn=lambda _delay: None
    )

    result = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_00")

    assert result.status == "downloaded"
    assert result.attempts == 2


def test_corrupt_existing_file_is_not_reused(tmp_path: Path) -> None:
    corrupt = tmp_path / "post_abc_00.webp"
    corrupt.write_bytes(b"not an image")
    image = make_png()
    context = FakeConnectionFactory([FakeResponse(200, image, "image/png")])
    downloader = ImageDownloader(connection_factory=context, sleep_fn=lambda _delay: None)

    result = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_00")

    assert result.status == "downloaded"
    assert result.file == "post_abc_00.png"
    assert not corrupt.exists()


def test_valid_existing_file_is_verified_before_reuse(tmp_path: Path) -> None:
    existing = tmp_path / "post_abc_00.png"
    existing.write_bytes(make_png())
    existing.chmod(0o644)
    context = FakeConnectionFactory([])
    downloader = ImageDownloader(connection_factory=context, sleep_fn=lambda _delay: None)

    result = downloader.download([], tmp_path, "post_abc_00")

    assert result.status == "reused_verified"
    assert result.attempts == 0
    assert context.calls == 0
    assert existing.stat().st_mode & 0o777 == 0o600


def test_download_revalidates_url_at_request_boundary(tmp_path: Path) -> None:
    context = FakeConnectionFactory([])
    downloader = ImageDownloader(
        connection_factory=context, retries=0, sleep_fn=lambda _delay: None
    )

    result = downloader.download(["https://127.0.0.1/private"], tmp_path, "post_abc_00")

    assert result.status == "failed"
    assert result.error_code == "unsafe_media_url"
    assert result.attempts == 0
    assert context.calls == 0


def test_content_length_limit_is_checked_before_body_read(tmp_path: Path) -> None:
    response = FakeResponse(200, make_png(), "image/png")
    response.headers["content-length"] = "1048577"

    def unexpected_body(_size: int) -> bytes:
        raise AssertionError("body should not be buffered after an oversized length header")

    response.read = unexpected_body  # type: ignore[method-assign]
    context = FakeConnectionFactory([response])
    downloader = ImageDownloader(
        connection_factory=context,
        retries=0,
        max_bytes=1_048_576,
        sleep_fn=lambda _delay: None,
    )

    result = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_00")

    assert result.status == "failed"
    assert result.error_code == "image_too_large"
    assert result.attempts == 1


def test_chunked_body_is_stopped_at_size_limit(tmp_path: Path) -> None:
    response = FakeResponse(200, b"x" * 100, "image/png")
    response.headers.pop("content-length")
    context = FakeConnectionFactory([response])
    downloader = ImageDownloader(
        connection_factory=context,
        retries=0,
        max_bytes=32,
        sleep_fn=lambda _delay: None,
    )

    result = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_00")

    assert result.status == "failed"
    assert result.error_code == "image_too_large"
    assert response.offset == 33


def test_total_request_deadline_covers_headers_and_stops_future_fetches(tmp_path: Path) -> None:
    class SlowConnection:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def request(self, _method: str, _target: str, *, headers: dict[str, str]) -> None:
            assert headers["Accept-Encoding"] == "identity"

        def getresponse(self) -> FakeResponse:
            self.closed.wait(timeout=1)
            raise OSError("connection closed")

        def close(self) -> None:
            self.closed.set()

    class SlowConnectionFactory:
        calls = 0

        def __call__(self, _host: str, *, port: int, timeout: float) -> SlowConnection:
            assert port == 443
            assert timeout > 0
            self.calls += 1
            return SlowConnection()

    connection_factory = SlowConnectionFactory()
    downloader = ImageDownloader(
        connection_factory=connection_factory,
        retries=3,
        timeout_ms=20,
        sleep_fn=lambda _delay: None,
    )

    started = time.monotonic()
    first = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_00")
    elapsed = time.monotonic() - started
    second = downloader.download(["https://p3-sign.douyinpic.com/image"], tmp_path, "post_abc_01")

    assert first.error_code == "request_deadline_exceeded"
    assert second.error_code == "request_deadline_exceeded"
    assert second.attempts == 0
    assert connection_factory.calls == 1
    assert elapsed < 0.1
