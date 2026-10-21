"""Small Google Drive client for the worker.

Signs in with an OAuth refresh token (the person's own account), so it works
with a personal Gmail Drive and, later, a church Workspace or shared drive.
Downloads stream to disk and resume if the connection drops; uploads use
Drive's resumable upload in chunks, so 10 GB files are fine.

Needs: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN.
"""

from __future__ import annotations

import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 64 * 1024 * 1024          # upload chunk; must be a multiple of 256 KiB
READ_CHUNK = 8 * 1024 * 1024
RETRIES = 5
COMMON = {"supportsAllDrives": "true"}


class DriveError(Exception):
    """A Drive problem the person can fix (missing folder, bad sign-in...)."""


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    size: int = 0

    @property
    def is_folder(self) -> bool:
        return self.mime == FOLDER_MIME


def _q(text: str) -> str:
    """Quote a value for a Drive search query."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Drive:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._creds = (client_id, client_secret, refresh_token)
        self._token = ""
        self._expires = 0.0
        self.http = requests.Session()

    @classmethod
    def from_env(cls) -> "Drive":
        missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
                   if not os.environ.get(k)]
        if missing:
            raise DriveError("Missing GitHub secrets: " + ", ".join(missing))
        return cls(os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"],
                   os.environ["GOOGLE_REFRESH_TOKEN"])

    # ------------------------------------------------------------ auth + http

    def _auth(self) -> dict:
        if time.time() > self._expires - 60:
            cid, secret, refresh = self._creds
            r = requests.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret,
                                               "refresh_token": refresh, "grant_type": "refresh_token"},
                              timeout=30)
            if r.status_code != 200:
                try:
                    body = r.json()
                    detail = body.get("error_description") or body.get("error") or r.status_code
                except ValueError:
                    detail = r.status_code
                raise DriveError(f"Google sign-in failed ({detail}). Check the three GOOGLE_ secrets; "
                                 "if the app was still in Testing, the token may have expired after 7 days.")
            data = r.json()
            self._token = data["access_token"]
            self._expires = time.time() + int(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, url: str, *, ok=(200,), headers: dict | None = None,
                 timeout: int = 120, retry: bool = True, **kwargs) -> requests.Response:
        """Send a request, retrying rate limits, server errors and dropped connections."""
        attempts = RETRIES if retry else 1
        for attempt in range(attempts):
            try:
                r = self.http.request(method, url, headers={**(headers or {}), **self._auth()},
                                      timeout=timeout, **kwargs)
            except requests.RequestException:
                if attempt == attempts - 1:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code in ok:
                return r
            retryable = r.status_code in (429, 500, 502, 503, 504) or (
                r.status_code == 403 and "rateLimit" in r.text)
            if retryable and attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                raise DriveError("Drive couldn't find that folder or file. Check that DRIVE_ROOT_FOLDER_ID "
                                 "is just the folder ID, and that the signed-in account can see it.")
            raise DriveError(f"Drive said {r.status_code}: {r.text[:300]}")
        raise DriveError(f"Drive kept failing on {method} {url.split('?')[0]}")

    # ------------------------------------------------------------ browsing

    def get(self, file_id: str) -> DriveFile:
        r = self._request("GET", f"{API}/files/{file_id}",
                          params={**COMMON, "fields": "id,name,mimeType,size"})
        d = r.json()
        return DriveFile(d["id"], d["name"], d["mimeType"], int(d.get("size", 0)))

    def children(self, folder_id: str) -> list[DriveFile]:
        files: list[DriveFile] = []
        token = None
        while True:
            params = {**COMMON, "includeItemsFromAllDrives": "true", "pageSize": "1000",
                      "q": f"{_q(folder_id)} in parents and trashed = false",
                      "fields": "nextPageToken,files(id,name,mimeType,size)", "orderBy": "name"}
            if token:
                params["pageToken"] = token
            d = self._request("GET", f"{API}/files", params=params).json()
            files += [DriveFile(f["id"], f["name"], f["mimeType"], int(f.get("size", 0)))
                      for f in d.get("files", [])]
            token = d.get("nextPageToken")
            if not token:
                return files

    def child(self, folder_id: str, name: str, folder: bool | None = None) -> DriveFile | None:
        """Find an item by name in a folder (case-insensitive)."""
        matches = [f for f in self.children(folder_id) if f.name.lower() == name.lower()
                   and (folder is None or f.is_folder == folder)]
        return matches[0] if matches else None

    def resolve(self, root_id: str, rel_path: str) -> DriveFile:
        """Find 'Incoming/2026/Romans/file.mp4' under the root folder."""
        current = self.get(root_id)
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
        walked = current.name
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            nxt = self.child(current.id, part, folder=None if last else True)
            if nxt is None and last:
                # Allow the name without its extension: "service" finds "service.mp4".
                stem = [f for f in self.children(current.id)
                        if not f.is_folder and f.name.rsplit(".", 1)[0].lower() == part.lower()]
                if len(stem) > 1:
                    names = ", ".join(f.name for f in stem)
                    raise DriveError(f"'{part}' matches more than one file in '{walked}' ({names}); add the extension")
                nxt = stem[0] if stem else None
            if nxt is None:
                raise DriveError(f"'{part}' wasn't found in '{walked}'")
            current, walked = nxt, f"{walked}/{part}"
        return current

    def walk(self, folder_id: str, prefix: str = ""):
        """Yield (relative path, file) for every file under a folder, any depth."""
        for f in self.children(folder_id):
            path = f"{prefix}{f.name}"
            if f.is_folder:
                yield from self.walk(f.id, path + "/")
            else:
                yield path, f

    def ensure_folder(self, parent_id: str, rel_path: str) -> str:
        """Create (or reuse) nested folders like '2026/Romans'; return the last folder's ID."""
        current = parent_id
        for part in [p for p in rel_path.split("/") if p]:
            existing = self.child(current, part, folder=True)
            if existing:
                current = existing.id
            else:
                r = self._request("POST", f"{API}/files", params={**COMMON, "fields": "id"},
                                  json={"name": part, "mimeType": FOLDER_MIME, "parents": [current]})
                current = r.json()["id"]
        return current

    # ------------------------------------------------------------ transfer

    def download(self, f: DriveFile, dest: Path, progress=print) -> Path:
        """Stream a file to disk, resuming from where it stopped if the connection drops."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        have = 0
        dest.write_bytes(b"")
        next_mark, started = 10, time.monotonic()
        for attempt in range(RETRIES):
            try:
                headers = {"Range": f"bytes={have}-"} if have else {}
                r = self._request("GET", f"{API}/files/{f.id}", ok=(200, 206), stream=True,
                                  params={**COMMON, "alt": "media"}, headers=headers, timeout=300)
                with dest.open("ab") as out:
                    for chunk in r.iter_content(READ_CHUNK):
                        out.write(chunk)
                        have += len(chunk)
                        if f.size:
                            pct = have * 100 // f.size
                            if pct >= next_mark:
                                mb = have / 1048576
                                rate = mb / max(time.monotonic() - started, 0.1)
                                progress(f"    download {f.name}: {pct}% ({mb:,.0f} MB, {rate:,.0f} MB/s)")
                                next_mark = pct - pct % 10 + 10
                if not f.size or have >= f.size:
                    return dest
            except requests.RequestException:
                if attempt == RETRIES - 1:
                    raise
            time.sleep(2 ** attempt)
        raise DriveError(f"Download of {f.name} stopped at {have} of {f.size} bytes")

    def upload(self, src: Path, folder_id: str, progress=print) -> str:
        """Upload in chunks; replaces a same-name file in the folder instead of making a duplicate."""
        size = src.stat().st_size
        mime = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        existing = self.child(folder_id, src.name, folder=False)
        if existing:
            r = self._request("PATCH", f"{UPLOAD_API}/files/{existing.id}",
                              params={**COMMON, "uploadType": "resumable"},
                              headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                              json={})
        else:
            r = self._request("POST", f"{UPLOAD_API}/files",
                              params={**COMMON, "uploadType": "resumable"},
                              headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                              json={"name": src.name, "parents": [folder_id]})
        session = r.headers["Location"]

        sent, next_mark, failures = 0, 25, 0
        with src.open("rb") as fh:
            while True:
                fh.seek(sent)
                data = fh.read(CHUNK)
                header = {"Content-Range": f"bytes {sent}-{sent + len(data) - 1}/{size}" if data
                          else f"bytes */{size}"}
                try:
                    r = self._request("PUT", session, ok=(200, 201, 308), data=data, headers=header,
                                      timeout=600, retry=False)
                except (requests.RequestException, DriveError):
                    failures += 1
                    if failures > RETRIES:
                        raise
                    time.sleep(2 ** failures)
                    # Ask Google how much it actually received, then carry on from there.
                    r = self._request("PUT", session, ok=(200, 201, 308),
                                      headers={"Content-Range": f"bytes */{size}"})
                if r.status_code in (200, 201):
                    progress(f"    upload {src.name}: 100%")
                    return r.json()["id"]
                rng = r.headers.get("Range")          # e.g. "bytes=0-67108863"
                sent = int(rng.split("-")[1]) + 1 if rng else 0
                if size and sent * 100 // size >= next_mark:
                    progress(f"    upload {src.name}: {sent * 100 // size}%")
                    next_mark = sent * 100 // size // 25 * 25 + 25
        raise DriveError(f"Upload of {src.name} didn't finish")
