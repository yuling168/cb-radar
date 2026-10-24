"""Upload export files to a Google Drive folder using a service account.

Environment:
    GDRIVE_SA_KEY      service-account JSON key (contents, not a path)
    GDRIVE_FOLDER_ID   target Drive folder id

Service accounts have no storage quota of their own, so files must be OWNED by a
human (or live in a Shared Drive).  This script therefore UPDATES files that already
exist in the folder (matched by name) and only tries to create missing ones as a
fallback, failing with a clear message if Drive refuses.

Usage: python scripts/upload_to_drive.py exports/all_cb_daily.csv exports/_index.md
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _service():
    info = json.loads(os.environ["GDRIVE_SA_KEY"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload(service, folder_id: str, path: Path) -> None:
    mime = {
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(path.suffix) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
    escaped = path.name.replace("'", "\\'")
    found = service.files().list(
        q=f"'{folder_id}' in parents and name = '{escaped}' and trashed = false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()["files"]
    if found:
        service.files().update(
            fileId=found[0]["id"], media_body=media, supportsAllDrives=True
        ).execute()
        print(f"updated: {path.name}")
        return
    try:
        service.files().create(
            body={"name": path.name, "parents": [folder_id]},
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        print(f"created: {path.name}")
    except HttpError as exc:
        raise SystemExit(
            f"Cannot create {path.name} ({exc.resp.status}). Service accounts have no Drive "
            f"quota: create an empty file named '{path.name}' in the shared folder yourself, "
            "then rerun so the script can update it."
        ) from exc


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    service = _service()
    folder_id = os.environ["GDRIVE_FOLDER_ID"]
    for name in argv:
        path = Path(name)
        if not path.is_file():
            raise SystemExit(f"missing file: {path}")
        upload(service, folder_id, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
