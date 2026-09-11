"""Create and verify an immutable GitHub Release DB snapshot.

This command deliberately does not install its output manifest.  The workflow
may install the returned candidate only after this command has succeeded.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from restore_db_snapshot import sha256_and_size, restore_snapshot


def snapshot_names(trade_date: str, run_id: str, attempt: str) -> tuple[str, str]:
    suffix = f"{trade_date}-run-{run_id}-attempt-{attempt}"
    return f"db-snapshot-{suffix}", f"cb_history-{suffix}.db.gz"


def deterministic_gzip(source: Path, destination: Path) -> None:
    with source.open("rb") as input_handle, destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as output:
            shutil.copyfileobj(input_handle, output, length=1024 * 1024)


def strategy_metadata(database: Path, previous: dict) -> dict:
    value = dict(previous.get("strategy_a", {}))
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """SELECT series.definition_id, series.baseline_run_id, definition.strategy_code, definition.strategy_version
               FROM strategy_published_series AS series
               JOIN strategy_definition AS definition ON definition.definition_id=series.definition_id
               WHERE series.strategy_code='A'"""
        ).fetchone()
        if row is None:
            raise RuntimeError("published Strategy A series is missing")
        value.update({"strategy_code": row[2], "strategy_version": row[3],
                      "definition_id": row[0], "baseline_run_id": row[1]})
        counts = connection.execute(
            "SELECT COUNT(*), (SELECT COUNT(*) FROM strategy_run_signals WHERE run_id=?) FROM strategy_run_evaluations WHERE run_id=?",
            (row[1], row[1]),
        ).fetchone()
        value.update({"evaluations": counts[0], "signals": counts[1]})
        date_row = connection.execute(
            """SELECT MAX(trade_date) FROM (
                   SELECT end_date AS trade_date FROM strategy_run WHERE run_id=?
                   UNION ALL SELECT trade_date FROM strategy_published_date WHERE definition_id=?
               )""", (row[1], row[0])
        ).fetchone()
        value["published_through_date"] = date_row[0]
        return value
    finally:
        connection.close()


def run_gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def release_exists(tag: str) -> bool:
    return subprocess.run(["gh", "release", "view", tag], capture_output=True, text=True).returncode == 0


def publish_snapshot(candidate_db: Path, trade_date: str, manifest_path: Path, manifest_output: Path,
                     run_id: str, attempt: str) -> dict:
    previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    tag, asset_name = snapshot_names(trade_date, run_id, attempt)
    if release_exists(tag):
        raise RuntimeError(f"snapshot release tag already exists: {tag}")
    with tempfile.TemporaryDirectory(prefix="cb-snapshot-") as directory:
        temp = Path(directory)
        asset = temp / asset_name
        deterministic_gzip(candidate_db, asset)
        compressed_hash, compressed_size = sha256_and_size(asset)
        uncompressed_hash, uncompressed_size = sha256_and_size(candidate_db)
        # A draft is intentionally retained if an upload or verification fails.
        run_gh("release", "create", tag, "--draft", "--title", tag, "--notes", "Automated DB snapshot candidate")
        run_gh("release", "upload", tag, str(asset))
        downloaded = temp / "redownload"
        downloaded.mkdir()
        run_gh("release", "download", tag, "--pattern", asset_name, "--dir", str(downloaded))
        candidate_manifest = {
            "schema_version": 1,
            "snapshot": {
                "release_tag": tag, "asset_name": asset_name, "trade_date": trade_date,
                "created_at": "PENDING_RELEASE_PUBLISH",
                "sha256_compressed": compressed_hash, "sha256_uncompressed": uncompressed_hash,
                "compressed_size": compressed_size, "uncompressed_size": uncompressed_size,
            },
            "provenance": dict(previous.get("provenance", {})),
            "strategy_a": strategy_metadata(candidate_db, previous),
        }
        verifier_manifest = temp / "verify-manifest.json"
        verifier_manifest.write_text(json.dumps(candidate_manifest), encoding="utf-8")
        restore_snapshot(verifier_manifest, downloaded / asset_name, temp / "verified.db")
        run_gh("release", "edit", tag, "--draft=false")
        release = json.loads(run_gh("release", "view", tag, "--json", "publishedAt"))
        candidate_manifest["snapshot"]["created_at"] = release["publishedAt"] or datetime.now(timezone.utc).isoformat()
        manifest_output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = manifest_output.with_suffix(manifest_output.suffix + ".tmp")
        temporary_output.write_text(json.dumps(candidate_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_output.replace(manifest_output)
        return candidate_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a verified DB snapshot release")
    parser.add_argument("--candidate-db", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args(argv)
    result = publish_snapshot(args.candidate_db, args.trade_date, args.manifest, args.manifest_output, args.run_id, args.attempt)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
