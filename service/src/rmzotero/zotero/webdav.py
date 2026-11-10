"""Zotero attachment files on a WebDAV share.

Zotero stores each attachment as ``zotero/<KEY>.zip`` (the file zipped under
its own name) plus ``zotero/<KEY>.prop``::

    <properties version="1"><mtime>MS</mtime><hash>MD5</hash></properties>

Uploads follow Zotero's order: delete the .prop, put the .zip, put the .prop.
After an upload the attachment item's ``md5``/``mtime`` are set through the web
API, which Zotero allows for WebDAV-backed personal libraries.
"""

from __future__ import annotations

from .. import telemetry

import hashlib
import io
import re
import time
import zipfile
from typing import Protocol

import httpx


class FileStore(Protocol):
    def get(self, key: str) -> tuple[str, bytes]: ...

    def put(self, key: str, filename: str, data: bytes, mtime_ms: int | None = None) -> tuple[str, int]: ...


class FileMissing(Exception):
    pass


def zip_file(filename: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(filename, data)
    return buf.getvalue()


def unzip_file(blob: bytes) -> tuple[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not names:
            raise FileMissing("empty zip")
        name = names[0]
        return name, z.read(name)


def prop_xml(mtime_ms: int, md5: str) -> str:
    return f'<properties version="1"><mtime>{mtime_ms}</mtime><hash>{md5}</hash></properties>'


def parse_prop(text: str) -> tuple[int, str]:
    m = re.search(r"<mtime>(\d+)</mtime>", text)
    h = re.search(r"<hash>([0-9a-f]+)</hash>", text)
    mtime = int(m.group(1)) if m else 0
    if mtime and mtime < 10**11:  # legacy seconds
        mtime *= 1000
    return mtime, h.group(1) if h else ""


class WebDAVFiles:
    def __init__(self, url: str, username: str = "", password: str = "", *, client: httpx.Client | None = None):
        base = url.rstrip("/")
        if not base.endswith("/zotero"):
            base += "/zotero"
        self.base = base
        auth = (username, password) if username else None
        self.http = client or httpx.Client(timeout=120.0, auth=auth)

    @telemetry.timed("upstream")
    def get(self, key: str) -> tuple[str, bytes]:
        r = self.http.get(f"{self.base}/{key}.zip")
        if r.status_code == 404:
            raise FileMissing(key)
        r.raise_for_status()
        return unzip_file(r.content)

    @telemetry.timed("upstream")
    def stat(self, key: str) -> tuple[int, str] | None:
        r = self.http.get(f"{self.base}/{key}.prop")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return parse_prop(r.text)

    @telemetry.timed("upstream")
    def put(self, key: str, filename: str, data: bytes, mtime_ms: int | None = None) -> tuple[str, int]:
        md5 = hashlib.md5(data).hexdigest()
        mtime = int(mtime_ms or time.time() * 1000)
        r = self.http.delete(f"{self.base}/{key}.prop")
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()
        r = self.http.put(f"{self.base}/{key}.zip", content=zip_file(filename, data), headers={"Content-Type": "application/zip"})
        r.raise_for_status()
        r = self.http.put(f"{self.base}/{key}.prop", content=prop_xml(mtime, md5).encode(), headers={"Content-Type": "text/xml"})
        r.raise_for_status()
        return md5, mtime


class MemoryFiles:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}  # key.zip / key.prop

    @telemetry.timed("upstream")
    def get(self, key: str) -> tuple[str, bytes]:
        if f"{key}.zip" not in self.files:
            raise FileMissing(key)
        return unzip_file(self.files[f"{key}.zip"])

    @telemetry.timed("upstream")
    def put(self, key: str, filename: str, data: bytes, mtime_ms: int | None = None) -> tuple[str, int]:
        md5 = hashlib.md5(data).hexdigest()
        mtime = int(mtime_ms or time.time() * 1000)
        self.files[f"{key}.zip"] = zip_file(filename, data)
        self.files[f"{key}.prop"] = prop_xml(mtime, md5).encode()
        return md5, mtime
