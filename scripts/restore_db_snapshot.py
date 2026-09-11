"""Offline verification and atomic restoration of a DB snapshot artifact."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path


class SnapshotVerificationError(RuntimeError):
    pass


def sha256_and_size(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest().upper(), size


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    snapshot = data.get("snapshot", {})
    required = {"release_tag", "asset_name", "trade_date", "created_at", "sha256_compressed",
                "sha256_uncompressed", "compressed_size", "uncompressed_size"}
    if data.get("schema_version") != 1 or required - snapshot.keys():
        raise SnapshotVerificationError("invalid snapshot manifest")
    return data


def verify_sqlite(path: Path, required_tables: tuple[str, ...] = ("cb_daily",)) -> None:
    connection = sqlite3.connect(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SnapshotVerificationError(f"SQLite integrity_check failed: {integrity}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise SnapshotVerificationError(f"SQLite foreign_key_check failed: {foreign_keys}")
        found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = set(required_tables) - found
        if missing:
            raise SnapshotVerificationError("missing required tables: " + ", ".join(sorted(missing)))
    finally:
        connection.close()


def restore_snapshot(manifest_path: Path, asset_path: Path, destination: Path) -> None:
    """Verify ``asset_path`` then atomically replace ``destination`` only on success."""
    manifest = load_manifest(manifest_path)
    snapshot = manifest["snapshot"]
    if asset_path.name != snapshot["asset_name"]:
        raise SnapshotVerificationError("asset filename does not match manifest")
    compressed_hash, compressed_size = sha256_and_size(asset_path)
    if compressed_size != snapshot["compressed_size"]:
        raise SnapshotVerificationError("compressed size mismatch")
    if compressed_hash != snapshot["sha256_compressed"].upper():
        raise SnapshotVerificationError("compressed SHA-256 mismatch")

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output, gzip.open(asset_path, "rb") as source:
            digest, size = hashlib.sha256(), 0
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size != snapshot["uncompressed_size"]:
            raise SnapshotVerificationError("uncompressed size mismatch")
        if digest.hexdigest().upper() != snapshot["sha256_uncompressed"].upper():
            raise SnapshotVerificationError("uncompressed SHA-256 mismatch")
        verify_sqlite(temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and atomically restore a CB DB snapshot")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    restore_snapshot(args.manifest, args.asset, args.destination)
    print(f"restored: {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
