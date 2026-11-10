"""HTTP client for rmfakecloud's device sync API (the same API the tablet uses)."""

from __future__ import annotations

from .. import telemetry

import json
import logging
import uuid
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class GenerationConflict(Exception):
    """The root changed since we read it (HTTP 412)."""


class BlobNotFound(Exception):
    pass


class BlobStore(Protocol):
    """The minimal sync15 storage interface."""

    def get_root(self) -> tuple[str, int]: ...

    def put_root(self, root_hash: str, generation: int) -> int: ...

    def get_blob(self, blob_hash: str, filename: str = "") -> bytes: ...

    def put_blob(self, blob_hash: str, filename: str, data: bytes) -> None: ...


class RmCloudClient:
    """Talks to rmfakecloud as a registered device.

    Registration exchanges a one-time code (from rmfakecloud's web UI) for a
    long-lived *device token*, stored in ``token_path``. Each session exchanges
    the device token for a short-lived *user token*.
    """

    DEVICE_DESC = "desktop-linux"

    def __init__(self, base_url: str, token_path: Path, *, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.token_path = token_path
        self.http = client or httpx.Client(timeout=60.0)
        self._user_token: str | None = None

    # -- auth ---------------------------------------------------------------

    def register(self, code: str) -> None:
        body = {"code": code, "deviceDesc": self.DEVICE_DESC, "deviceID": str(uuid.uuid4())}
        r = self.http.post(f"{self.base_url}/token/json/2/device/new", json=body)
        r.raise_for_status()
        token = r.text.strip()
        if not token:
            raise RuntimeError("rmfakecloud returned an empty device token")
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(token)
        self._user_token = None

    def _device_token(self) -> str:
        if not self.token_path.exists():
            raise RuntimeError(
                "Not registered with rmfakecloud. Run `rmzotero register --code <code>` "
                "with a one-time code from rmfakecloud's web UI."
            )
        return self.token_path.read_text().strip()

    @telemetry.timed("upstream")
    def _refresh_user_token(self) -> str:
        r = self.http.post(
            f"{self.base_url}/token/json/2/user/new",
            headers={"Authorization": f"Bearer {self._device_token()}"},
        )
        r.raise_for_status()
        self._user_token = r.text.strip()
        return self._user_token

    def user_token(self) -> str:
        return self._user_token or self._refresh_user_token()

    @telemetry.timed("upstream")
    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (1, 2):
            headers = dict(kw.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self.user_token()}"
            r = self.http.request(method, f"{self.base_url}{path}", headers=headers, **kw)
            if r.status_code == 401 and attempt == 1:
                self._refresh_user_token()
                kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            return r
        raise AssertionError("unreachable")

    # -- sync15 -------------------------------------------------------------

    def get_root(self) -> tuple[str, int]:
        r = self._request("GET", "/sync/v4/root")
        if r.status_code == 404:
            return "", 0
        r.raise_for_status()
        data = r.json()
        return data.get("hash", "") or "", int(data.get("generation", 0) or 0)

    def put_root(self, root_hash: str, generation: int) -> int:
        body = {"hash": root_hash, "generation": generation, "broadcast": True}
        r = self._request("PUT", "/sync/v3/root", json=body, headers={"rm-filename": "roothash"})
        if r.status_code == 412:
            raise GenerationConflict()
        r.raise_for_status()
        data = r.json()
        if data.get("hash") != root_hash:
            raise RuntimeError("rmfakecloud acknowledged a different root hash")
        return int(data["generation"])

    def get_blob(self, blob_hash: str, filename: str = "") -> bytes:
        headers = {"rm-filename": filename} if filename else {}
        r = self._request("GET", f"/sync/v3/files/{blob_hash}", headers=headers)
        if r.status_code == 404:
            raise BlobNotFound(blob_hash)
        r.raise_for_status()
        return r.content

    def put_blob(self, blob_hash: str, filename: str, data: bytes) -> None:
        headers = {"rm-filename": filename}
        if filename == "root.docSchema":
            headers["content-type"] = "text/plain; charset=UTF-8"
        r = self._request("PUT", f"/sync/v3/files/{blob_hash}", content=data, headers=headers)
        r.raise_for_status()

    # -- notifications --------------------------------------------------------

    def notifications_url(self) -> str:
        ws = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{ws}/notifications/ws/json/1"


def parse_notification(raw: str | bytes) -> dict | None:
    """Return the attributes of a websocket notification, or None if unrecognised."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    attrs = (msg.get("message") or {}).get("attributes")
    return attrs if isinstance(attrs, dict) else None


class MemoryCloud:
    """An in-memory BlobStore with rmfakecloud's generation semantics (tests, dry runs)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.root_hash = ""
        self.generation = 0
        self.root_writes = 0

    def get_root(self) -> tuple[str, int]:
        return self.root_hash, self.generation

    def put_root(self, root_hash: str, generation: int) -> int:
        if generation != self.generation:
            raise GenerationConflict()
        if root_hash and root_hash not in self.blobs:
            raise RuntimeError("root index blob missing")
        self.root_hash = root_hash
        self.generation += 1
        self.root_writes += 1
        return self.generation

    def get_blob(self, blob_hash: str, filename: str = "") -> bytes:
        try:
            return self.blobs[blob_hash]
        except KeyError:
            raise BlobNotFound(blob_hash) from None

    def put_blob(self, blob_hash: str, filename: str, data: bytes) -> None:
        self.blobs[blob_hash] = data
