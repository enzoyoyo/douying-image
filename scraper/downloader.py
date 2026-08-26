"""Crash-safe image downloads with verification and fallback URLs."""

from __future__ import annotations

import os
import queue
import random
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from .images import ImageInfo, ImageValidationError, inspect_image_bytes, inspect_image_file
from .security import InputValidationError, safe_output_path, validate_media_url

IMAGE_EXTENSIONS = (".jpg", ".png", ".webp", ".avif", ".heic")
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
READ_CHUNK_BYTES = 64 * 1024
_TRANSPORT_LEAK_GUARD = threading.Event()


@dataclass(frozen=True)
class DownloadResult:
    """Privacy-safe result for one planned image."""

    status: str
    file: str | None
    byte_count: int
    sha256: str | None
    width: int | None
    height: int | None
    format: str | None
    attempts: int
    error_code: str | None

    @classmethod
    def verified(
        cls,
        status: str,
        path: Path,
        info: ImageInfo,
        *,
        attempts: int,
    ) -> DownloadResult:
        return cls(
            status=status,
            file=path.name,
            byte_count=info.byte_count,
            sha256=info.sha256,
            width=info.width,
            height=info.height,
            format=info.format,
            attempts=attempts,
            error_code=None,
        )

    @classmethod
    def failed(cls, error_code: str, *, attempts: int) -> DownloadResult:
        return cls(
            status="failed",
            file=None,
            byte_count=0,
            sha256=None,
            width=None,
            height=None,
            format=None,
            attempts=attempts,
            error_code=error_code,
        )


@dataclass(frozen=True)
class _FetchedResponse:
    status: int
    headers: dict[str, str]
    data: bytes


class ImageDownloader:
    """Stream verified images without trusting redirects or response metadata."""

    def __init__(
        self,
        *,
        retries: int = 3,
        timeout_ms: int = 30_000,
        max_bytes: int = 50 * 1024 * 1024,
        redownload: bool = False,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        connection_factory=HTTPSConnection,
    ) -> None:
        self.retries = retries
        self.timeout_ms = timeout_ms
        self.max_bytes = max_bytes
        self.redownload = redownload
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self.connection_factory = connection_factory
        self._connection_lock = threading.Lock()
        self._active_connection = None
        self._poisoned = False

    def _existing_paths(self, root: Path, stem: str) -> list[Path]:
        return [safe_output_path(root, f"{stem}{extension}") for extension in IMAGE_EXTENSIONS]

    def _find_reusable(self, root: Path, stem: str) -> tuple[Path, ImageInfo] | None:
        if self.redownload:
            return None
        for path in self._existing_paths(root, stem):
            if not path.exists():
                continue
            try:
                info = inspect_image_file(path, max_bytes=self.max_bytes)
                os.chmod(path, 0o600)
                return path, info
            except (ImageValidationError, OSError):
                continue
        return None

    def _atomic_write(self, destination: Path, data: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise OSError("unsafe_destination")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.stem}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _content_length(headers: dict[str, str]) -> int | None:
        raw_value = headers.get("content-length")
        if not raw_value:
            return None
        try:
            value = int(raw_value)
        except ValueError:
            return None
        return value if value >= 0 else None

    def _backoff(self, retry_index: int) -> None:
        delay = (2**retry_index) + random.random() * 0.25
        self.sleep_fn(delay)

    def _read_bounded(self, response, *, max_bytes: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        byte_count = 0
        read_chunk = getattr(response, "read1", response.read)
        while True:
            if self.monotonic_fn() >= deadline:
                raise ImageValidationError("request_deadline_exceeded")
            chunk = read_chunk(min(READ_CHUNK_BYTES, max_bytes + 1 - byte_count))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise ImageValidationError("image_too_large")

    def _fetch_once(self, safe_url: str, *, max_bytes: int, deadline: float) -> _FetchedResponse:
        parsed = urlsplit(safe_url)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target = f"{request_target}?{parsed.query}"

        remaining_seconds = max(0.001, deadline - self.monotonic_fn())
        connection = self.connection_factory(
            parsed.hostname,
            port=parsed.port or 443,
            timeout=remaining_seconds,
        )
        response = None
        with self._connection_lock:
            self._active_connection = connection
        try:
            connection.request(
                "GET",
                request_target,
                headers={
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.1",
                    "Accept-Encoding": "identity",
                    "Referer": "https://www.douyin.com/",
                    "User-Agent": "douyin-image-downloader/1.0",
                },
            )
            response = connection.getresponse()
            status_code = int(response.status)
            headers = {str(key).lower(): str(value) for key, value in response.getheaders()}
            if status_code != 200:
                return _FetchedResponse(status=status_code, headers=headers, data=b"")
            expected_length = self._content_length(headers)
            if expected_length is not None and expected_length > max_bytes:
                raise ImageValidationError("image_too_large")
            data = self._read_bounded(response, max_bytes=max_bytes, deadline=deadline)
            if expected_length is not None and expected_length != len(data):
                raise ImageValidationError("content_length_mismatch")
            return _FetchedResponse(status=status_code, headers=headers, data=data)
        finally:
            if response is not None and hasattr(response, "close"):
                with suppress(Exception):
                    response.close()
            with suppress(Exception):
                connection.close()
            with self._connection_lock:
                if self._active_connection is connection:
                    self._active_connection = None

    def _abort_active_connection(self) -> None:
        with self._connection_lock:
            connection = self._active_connection
        if connection is not None:
            with suppress(Exception):
                connection.close()

    def _fetch_with_deadline(self, safe_url: str) -> _FetchedResponse:
        """Apply one wall-clock deadline across connect, headers, and body."""

        deadline = self.monotonic_fn() + self.timeout_ms / 1_000
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        max_bytes = self.max_bytes

        def fetch() -> None:
            try:
                result_queue.put(
                    ("ok", self._fetch_once(safe_url, max_bytes=max_bytes, deadline=deadline))
                )
            except Exception as exc:
                result_queue.put(("error", exc))

        worker = threading.Thread(target=fetch, name="image-fetch", daemon=True)
        worker.start()
        try:
            kind, payload = result_queue.get(timeout=self.timeout_ms / 1_000)
        except queue.Empty as exc:
            self._abort_active_connection()
            worker.join(timeout=0.25)
            if worker.is_alive():
                _TRANSPORT_LEAK_GUARD.set()
            self._poisoned = True
            raise ImageValidationError("request_deadline_exceeded") from exc

        if kind == "error":
            if isinstance(payload, Exception):
                raise payload
            raise RuntimeError("invalid_fetch_error")
        if not isinstance(payload, _FetchedResponse):
            raise RuntimeError("invalid_fetch_result")
        return payload

    def download(self, urls: Iterable[str], root: Path, stem: str) -> DownloadResult:
        """Download one image, falling back across already-validated candidates."""

        resolved_root = root.resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)

        if _TRANSPORT_LEAK_GUARD.is_set():
            return DownloadResult.failed("transport_unavailable_after_timeout", attempts=0)
        if self._poisoned:
            return DownloadResult.failed("request_deadline_exceeded", attempts=0)

        reusable = self._find_reusable(resolved_root, stem)
        if reusable:
            path, info = reusable
            return DownloadResult.verified("reused_verified", path, info, attempts=0)

        stale_paths = [path for path in self._existing_paths(resolved_root, stem) if path.exists()]
        attempts = 0
        last_error = "no_valid_candidate"

        for url in urls:
            try:
                safe_url = validate_media_url(url)
            except InputValidationError:
                last_error = "unsafe_media_url"
                continue

            for retry_index in range(self.retries + 1):
                attempts += 1
                try:
                    fetched = self._fetch_with_deadline(safe_url)
                    status_code = fetched.status
                    if status_code != 200:
                        last_error = f"http_{status_code}"
                        if status_code in RETRYABLE_STATUS_CODES and retry_index < self.retries:
                            self._backoff(retry_index)
                            continue
                        break

                    info = inspect_image_bytes(
                        fetched.data,
                        content_type=fetched.headers.get("content-type"),
                        max_bytes=self.max_bytes,
                    )
                    destination = safe_output_path(resolved_root, f"{stem}{info.extension}")
                    self._atomic_write(destination, fetched.data)

                    for stale_path in stale_paths:
                        is_safe_stale_file = stale_path.is_file() and not stale_path.is_symlink()
                        if stale_path != destination and is_safe_stale_file:
                            stale_path.unlink(missing_ok=True)
                    return DownloadResult.verified(
                        "downloaded",
                        destination,
                        info,
                        attempts=attempts,
                    )
                except ImageValidationError as exc:
                    last_error = str(exc)
                    if self._poisoned:
                        return DownloadResult.failed(last_error, attempts=attempts)
                    break
                except Exception:
                    last_error = "request_failed"
                    if retry_index < self.retries:
                        self._backoff(retry_index)
                        continue
                    break
        return DownloadResult.failed(last_error, attempts=attempts)
