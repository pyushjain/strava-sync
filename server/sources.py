import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol


@dataclass
class ExportFile:
    path: Path
    file_id_or_name: str
    version: str | None = None


class Source(Protocol):
    def files(self, seen_versions: set[str], download_dir: Path) -> Iterator[ExportFile]: ...


class LocalFolderSource:
    def __init__(self, path: Path):
        self.path = Path(path)

    def files(self, seen_versions: set[str], download_dir: Path) -> Iterator[ExportFile]:
        if not self.path.is_dir():
            raise ValueError("Local source path must be an existing directory")
        for path in sorted(self.path.iterdir()):
            if path.is_file() and path.suffix.lower() == ".zip":
                yield ExportFile(path, path.name)


class DriveSource:
    """Downloads one newest unseen ZIP; service account has Drive read-only scope."""

    @staticmethod
    def credentials():
        from google.oauth2 import service_account

        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if raw:
            try:
                info = json.loads(raw)
                if not isinstance(info, dict):
                    raise ValueError("Expected a JSON object")
                return service_account.Credentials.from_service_account_info(info, scopes=scopes)
            except (ValueError, TypeError, KeyError):
                raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be valid service-account JSON") from None
        path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if not path:
            raise ValueError("Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS for Drive access")
        return service_account.Credentials.from_service_account_file(path, scopes=scopes)

    def files(self, seen_versions: set[str], download_dir: Path) -> Iterator[ExportFile]:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload

        folder = os.environ["DRIVE_FOLDER_ID"]
        credentials = self.credentials()
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp

        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=60))
        service = build("drive", "v3", http=http, cache_discovery=False)
        escaped_folder = folder.replace("\\", "\\\\").replace("'", "\\'")
        token = None
        try:
            while True:
                page = service.files().list(
                    q=f"'{escaped_folder}' in parents and trashed = false",
                    fields="nextPageToken,files(id,name,modifiedTime,md5Checksum)",
                    orderBy="modifiedTime desc", pageSize=100, pageToken=token,
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                ).execute()
                for item in page.get("files", []):
                    if not item["name"].lower().endswith(".zip"):
                        continue
                    version = json.dumps([item["id"], item["modifiedTime"], item.get("md5Checksum")])
                    if version in seen_versions:
                        continue
                    path = download_dir / "drive-export.zip"
                    with io.FileIO(path, "wb") as target:
                        downloader = MediaIoBaseDownload(target, service.files().get_media(
                            fileId=item["id"], supportsAllDrives=True
                        ))
                        done = False
                        while not done:
                            _, done = downloader.next_chunk()
                    yield ExportFile(path, item["id"], version)
                    return
                token = page.get("nextPageToken")
                if not token:
                    return
        finally:
            service.close()
