"""Atomic, privacy-minimal run manifests."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def build_manifest(
    *,
    account_ref: str,
    capture_complete: bool,
    completion_reason: str,
    api_response_count: int,
    captured_post_count: int,
    image_post_count: int,
    records: list[dict],
    warnings: list[str],
    limits: dict[str, int],
) -> dict:
    """Build a schema whose default fields contain no source identities or signed URLs."""

    downloaded = sum(record["status"] == "downloaded" for record in records)
    reused = sum(record["status"] == "reused_verified" for record in records)
    failed = sum(record["status"] == "failed" for record in records)

    return {
        "schema_version": 1,
        "run_id": uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "account_ref": account_ref,
        "capture": {
            "complete": capture_complete,
            "completion_reason": completion_reason,
            "api_response_count": api_response_count,
            "captured_post_count": captured_post_count,
            "image_post_count": image_post_count,
        },
        "downloads": {
            "planned": len(records),
            "downloaded": downloaded,
            "reused_verified": reused,
            "failed": failed,
            "bytes_accounted": sum(record["bytes"] for record in records),
        },
        "limits": limits,
        "warnings": sorted(set(warnings)),
        "records": records,
    }


def write_private_json(path: Path, payload: dict) -> None:
    """Write JSON atomically with owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
