import gzip
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from db import connect, upsert_daily

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from publish_db_snapshot import deterministic_gzip, snapshot_names
from restore_db_snapshot import SnapshotVerificationError, restore_snapshot, sha256_and_size
from validate_db import validate_database


def _database(path):
    with connect(path) as connection:
        upsert_daily(connection, [{"trade_date": "2026-09-09", "cb_code": "11111", "cb_name": "test",
                                    "close_price": 100, "volume_lots": 1, "source": "test", "collected_at": "x"}])


def _snapshot(tmp_path, *, corrupt=False):
    database, asset, manifest = tmp_path / "source.db", tmp_path / "asset.db.gz", tmp_path / "manifest.json"
    _database(database)
    deterministic_gzip(database, asset)
    compressed_hash, compressed_size = sha256_and_size(asset)
    uncompressed_hash, uncompressed_size = sha256_and_size(database)
    payload = {"schema_version": 1, "snapshot": {"release_tag": "exact-tag", "asset_name": asset.name,
        "trade_date": "2026-09-09", "created_at": "x", "sha256_compressed": compressed_hash,
        "sha256_uncompressed": uncompressed_hash, "compressed_size": compressed_size,
        "uncompressed_size": uncompressed_size}}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return database, asset, manifest, payload


def test_restore_good_snapshot_and_atomic_failure_protection(tmp_path):
    _, asset, manifest, _ = _snapshot(tmp_path)
    destination = tmp_path / "destination.db"
    destination.write_bytes(b"old database")
    restore_snapshot(manifest, asset, destination)
    assert destination.read_bytes() != b"old database"
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cb_daily").fetchone()[0] == 1
    destination.write_bytes(b"sentinel")
    payload = json.loads(manifest.read_text())
    payload["snapshot"]["sha256_compressed"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotVerificationError, match="compressed SHA"):
        restore_snapshot(manifest, asset, destination)
    assert destination.read_bytes() == b"sentinel"


@pytest.mark.parametrize("field", ["compressed_size", "sha256_uncompressed"])
def test_restore_rejects_manifest_mismatches(tmp_path, field):
    _, asset, manifest, payload = _snapshot(tmp_path)
    payload["snapshot"][field] = 0 if field.endswith("size") else "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotVerificationError):
        restore_snapshot(manifest, asset, tmp_path / "out.db")


def test_restore_rejects_corrupt_gzip_and_missing_required_table(tmp_path):
    _, asset, manifest, _ = _snapshot(tmp_path)
    asset.write_bytes(b"not gzip")
    with pytest.raises((SnapshotVerificationError, OSError)):
        restore_snapshot(manifest, asset, tmp_path / "out.db")
    bare, asset, manifest = tmp_path / "bare.db", tmp_path / "bare.db.gz", tmp_path / "bare.json"
    sqlite3.connect(bare).close()
    deterministic_gzip(bare, asset)
    ch, cs = sha256_and_size(asset); uh, us = sha256_and_size(bare)
    manifest.write_text(json.dumps({"schema_version": 1, "snapshot": {"release_tag":"x","asset_name":asset.name,"trade_date":"x","created_at":"x","sha256_compressed":ch,"sha256_uncompressed":uh,"compressed_size":cs,"uncompressed_size":us}}), encoding="utf-8")
    with pytest.raises(SnapshotVerificationError, match="required tables"):
        restore_snapshot(manifest, asset, tmp_path / "bare-out.db")


def test_deterministic_gzip_and_unique_names(tmp_path):
    source = tmp_path / "source"; source.write_bytes(b"same bytes")
    first, second = tmp_path / "a.gz", tmp_path / "b.gz"
    deterministic_gzip(source, first); deterministic_gzip(source, second)
    assert first.read_bytes() == second.read_bytes()
    assert snapshot_names("2026-09-09", "1", "2") == (
        "db-snapshot-2026-09-09-run-1-attempt-2", "cb_history-2026-09-09-run-1-attempt-2.db.gz"
    )


def test_validation_requires_schema_and_detects_future_balance(tmp_path):
    database = tmp_path / "valid.db"; _database(database)
    validate_database(database, run_date="2026-09-09")
    with connect(database) as connection:
        connection.execute("""INSERT OR REPLACE INTO cb_master
            (cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date, issue_amount,
             balance_date, source, source_url, collected_at)
            VALUES ('11111','x','1111','x','2020-01-01','2030-01-01',1,'2026-09-10','test','test','x')""")
    with pytest.raises(RuntimeError, match="Future balance"):
        validate_database(database, run_date="2026-09-09")
