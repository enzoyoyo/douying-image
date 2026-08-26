"""Input and path validation for the downloader."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

ACCOUNT_HOSTS = frozenset({"douyin.com", "www.douyin.com"})
API_HOSTS = frozenset({"www.douyin.com"})
MEDIA_HOST_SUFFIXES = (
    "douyinpic.com",
    "douyinstatic.com",
    "byteimg.com",
)
POST_API_PATH = "/aweme/v1/web/aweme/post/"

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{8,256}$")
_POST_ID_RE = re.compile(r"^[0-9]{6,32}$")
_FILE_STEM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class InputValidationError(ValueError):
    """Raised when untrusted input is outside the supported scope."""


@dataclass(frozen=True)
class AccountTarget:
    """Canonical, validated account target."""

    url: str
    account_id: str
    account_ref: str


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _normalized_host(value: str) -> str:
    try:
        host = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise InputValidationError("invalid_host") from exc
    if not host:
        raise InputValidationError("missing_host")
    return host


def _validate_https_url_parts(value: str):
    if not isinstance(value, str) or not value.strip() or _has_control_characters(value):
        raise InputValidationError("invalid_url")

    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "https":
        raise InputValidationError("https_required")
    if parsed.username or parsed.password:
        raise InputValidationError("url_credentials_not_allowed")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InputValidationError("invalid_port") from exc
    if port not in (None, 443):
        raise InputValidationError("nonstandard_port_not_allowed")

    host = _normalized_host(parsed.hostname or "")
    return parsed, host


def validate_account_url(value: str) -> AccountTarget:
    """Accept only canonical Douyin account pages over HTTPS."""

    parsed, host = _validate_https_url_parts(value)
    if host not in ACCOUNT_HOSTS:
        raise InputValidationError("unsupported_account_host")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "user" or not _ACCOUNT_ID_RE.fullmatch(parts[1]):
        raise InputValidationError("unsupported_account_path")

    account_id = parts[1]
    canonical_url = f"https://www.douyin.com/user/{quote(account_id, safe='._~-')}"
    return AccountTarget(
        url=canonical_url,
        account_id=account_id,
        account_ref=stable_ref("account", account_id),
    )


def validate_post_id(value: object) -> str:
    """Return a safe platform post ID or raise."""

    post_id = str(value or "")
    if not _POST_ID_RE.fullmatch(post_id):
        raise InputValidationError("invalid_post_id")
    return post_id


def stable_ref(namespace: str, value: str, length: int = 16) -> str:
    """Create a stable pseudonymous identifier that omits the source value."""

    digest = hashlib.sha256(f"{namespace}\0{value}".encode()).hexdigest()
    return f"{namespace}_{digest[:length]}"


def is_expected_api_response(value: str, account_id: str) -> bool:
    """Bind intercepted API responses to the expected origin and account."""

    try:
        parsed, host = _validate_https_url_parts(value)
    except InputValidationError:
        return False
    if host not in API_HOSTS or parsed.path != POST_API_PATH:
        return False

    account_ids = parse_qs(parsed.query).get("sec_user_id", [])
    return account_ids == [account_id]


def request_is_bound_to_account(value: str, account_id: str) -> bool:
    """Return whether the request query explicitly names the target account."""

    try:
        parsed, _ = _validate_https_url_parts(value)
    except InputValidationError:
        return False
    return parse_qs(parsed.query).get("sec_user_id", []) == [account_id]


def validate_media_url(value: str) -> str:
    """Allow only HTTPS image CDN URLs owned by the supported platform."""

    parsed, host = _validate_https_url_parts(value)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InputValidationError("ip_media_host_not_allowed")

    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in MEDIA_HOST_SUFFIXES):
        raise InputValidationError("unsupported_media_host")
    if not parsed.path or parsed.path == "/":
        raise InputValidationError("missing_media_path")
    return value.strip()


def safe_output_path(root: Path, file_name: str) -> Path:
    """Resolve a generated file name and prove it stays under ``root``."""

    if not _FILE_STEM_RE.fullmatch(Path(file_name).stem):
        raise InputValidationError("invalid_output_name")
    if Path(file_name).name != file_name:
        raise InputValidationError("nested_output_name_not_allowed")

    resolved_root = root.resolve()
    candidate = (resolved_root / file_name).resolve(strict=False)
    if candidate.parent != resolved_root:
        raise InputValidationError("output_path_escape")
    return candidate
