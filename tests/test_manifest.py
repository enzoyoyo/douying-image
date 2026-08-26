import json
import stat
from pathlib import Path

from scraper.manifest import build_manifest, write_private_json


def test_manifest_counts_and_permissions(tmp_path: Path) -> None:
    records = [
        {"status": "downloaded", "bytes": 100},
        {"status": "reused_verified", "bytes": 100},
        {"status": "failed", "bytes": 0},
    ]
    manifest = build_manifest(
        account_ref="account_0123456789abcdef",
        capture_complete=False,
        completion_reason="stalled",
        api_response_count=2,
        captured_post_count=3,
        image_post_count=2,
        records=records,
        warnings=["cursor_repeated", "cursor_repeated"],
        limits={"max_posts": 1_000},
    )
    path = tmp_path / "manifest.json"
    write_private_json(path, manifest)

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["downloads"] == {
        "planned": 3,
        "downloaded": 1,
        "reused_verified": 1,
        "failed": 1,
        "bytes_accounted": 200,
    }
    assert loaded["warnings"] == ["cursor_repeated"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_default_manifest_contains_no_source_identity() -> None:
    raw_identity = "MS4wLjABAAAAprivate_source"
    manifest = build_manifest(
        account_ref="account_0123456789abcdef",
        capture_complete=True,
        completion_reason="api_exhausted",
        api_response_count=1,
        captured_post_count=1,
        image_post_count=1,
        records=[],
        warnings=[],
        limits={"max_posts": 1_000},
    )
    serialized = json.dumps(manifest)
    assert raw_identity not in serialized
    assert "http" not in serialized
