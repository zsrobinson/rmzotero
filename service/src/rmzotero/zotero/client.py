"""Zotero web API v3 client, plus an in-memory stand-in with the same semantics."""

from __future__ import annotations

from .. import telemetry

import logging
import random
import time
from typing import Iterable, Protocol

import httpx

log = logging.getLogger(__name__)

KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"


def new_key() -> str:
    """A Zotero object key; Zotero accepts client-generated keys."""
    return "".join(random.choice(KEY_ALPHABET) for _ in range(8))


class VersionConflict(Exception):
    """An object or the library changed since the version we sent (HTTP 412)."""


class WriteResult:
    def __init__(self) -> None:
        self.versions: dict[str, int] = {}  # key -> new version
        self.failed: dict[str, str] = {}  # key -> error message
        self.conflicts: set[str] = set()
        self.library_version = 0
        self.data: dict[str, dict] = {}  # key -> the object as the server stored it (when it says)


class ZoteroAPI(Protocol):
    user_id: int

    def changes_since(self, version: int) -> tuple[list[dict], list[dict], dict, int]:
        """(collections, items, deleted, new library version); objects are Zotero ``data`` dicts."""
        ...

    def write_items(self, objects: list[dict]) -> WriteResult: ...

    def delete_items(self, keys_versions: list[tuple[str, int]]) -> WriteResult: ...

    def write_collections(self, objects: list[dict]) -> WriteResult: ...


class ZoteroClient:
    BASE = "https://api.zotero.org"

    def __init__(self, user_id: int, api_key: str, *, base_url: str | None = None, client: httpx.Client | None = None):
        self.user_id = int(user_id)
        self.base = (base_url or self.BASE).rstrip("/")
        self.http = client or httpx.Client(timeout=60.0)
        self.headers = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}

    def _url(self, path: str) -> str:
        return f"{self.base}/users/{self.user_id}{path}"

    @telemetry.timed("upstream")
    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        for attempt in range(5):
            r = self.http.get(self._url(path), params=params, headers=self.headers)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or r.headers.get("Backoff") or 2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    def _paged(self, path: str, params: dict) -> tuple[list[dict], int]:
        out: list[dict] = []
        start = 0
        version = 0
        while True:
            r = self._get(path, {**params, "start": start, "limit": 100, "format": "json"})
            version = int(r.headers.get("Last-Modified-Version", version))
            batch = r.json()
            out.extend(obj["data"] for obj in batch)
            total = int(r.headers.get("Total-Results", len(out)))
            start += len(batch)
            if not batch or start >= total:
                return out, version

    def changes_since(self, version: int) -> tuple[list[dict], list[dict], dict, int]:
        collections, v1 = self._paged("/collections", {"since": version})
        items, v2 = self._paged("/items", {"since": version, "includeTrashed": 1})
        deleted = self._get("/deleted", {"since": version}).json() if version else {}
        return collections, items, deleted, max(v1, v2, version)

    def write_items(self, objects: list[dict]) -> WriteResult:
        return self._write("/items", objects)

    @telemetry.timed("upstream")
    def schema(self) -> dict:
        """Zotero's item schema (item types, their fields and creator types)."""
        r = self.http.get(f"{self.base}/schema", headers={"Zotero-API-Version": "3"})
        r.raise_for_status()
        return r.json()

    def write_collections(self, objects: list[dict]) -> WriteResult:
        return self._write("/collections", objects)

    @telemetry.timed("upstream")
    def _write(self, path: str, objects: list[dict]) -> WriteResult:
        res = WriteResult()
        for i in range(0, len(objects), 50):
            chunk = objects[i : i + 50]
            r = self.http.post(self._url(path), json=chunk, headers=self.headers)
            if r.status_code == 412:
                res.conflicts.update(o["key"] for o in chunk if "key" in o)
                continue
            r.raise_for_status()
            body = r.json()
            res.library_version = int(r.headers.get("Last-Modified-Version", res.library_version))
            for idx, ok in (body.get("successful") or {}).items():
                res.versions[ok["key"]] = int(ok.get("version") or ok["data"]["version"])
                if isinstance(ok.get("data"), dict):
                    res.data[ok["key"]] = ok["data"]
            for idx in body.get("unchanged") or {}:
                key = chunk[int(idx)].get("key")
                if key:
                    res.versions[key] = int(chunk[int(idx)].get("version", 0))
            for idx, err in (body.get("failed") or {}).items():
                key = chunk[int(idx)].get("key", f"#{idx}")
                if int(err.get("code", 0)) == 412:
                    res.conflicts.add(key)
                else:
                    res.failed[key] = f"{err.get('code')}: {err.get('message')}"
        return res

    @telemetry.timed("upstream")
    def delete_items(self, keys_versions: list[tuple[str, int]]) -> WriteResult:
        res = WriteResult()
        for key, version in keys_versions:
            r = self.http.delete(self._url(f"/items/{key}"), headers={**self.headers, "If-Unmodified-Since-Version": str(version)})
            if r.status_code == 412:
                res.conflicts.add(key)
            elif r.status_code == 404:
                continue
            else:
                r.raise_for_status()
                res.library_version = int(r.headers.get("Last-Modified-Version", res.library_version))
        return res


class MemoryZotero:
    """An in-memory Zotero library with version semantics (tests and dry runs)."""

    def __init__(self, user_id: int = 1):
        self.user_id = user_id
        self.version = 0
        self.objects: dict[str, dict] = {}  # key -> data (with "_kind": item|collection)
        self.deleted: dict[str, tuple[str, int]] = {}  # key -> (kind, version)

    def _bump(self) -> int:
        self.version += 1
        return self.version

    # helpers for tests
    def add_collection(self, name: str, parent: str | None = None, key: str | None = None) -> str:
        key = key or new_key()
        self.objects[key] = {"_kind": "collection", "key": key, "name": name, "parentCollection": parent or False, "version": self._bump()}
        return key

    def add_item(self, data: dict, key: str | None = None) -> str:
        key = key or data.get("key") or new_key()
        self.objects[key] = {"_kind": "item", "tags": [], "relations": {}, "dateModified": _now_iso(), **data, "key": key, "version": self._bump()}
        return key

    def update(self, key: str, **fields) -> None:
        self.objects[key].update(fields, version=self._bump(), dateModified=_now_iso())

    def remove(self, key: str) -> None:
        obj = self.objects.pop(key)
        self.deleted[key] = (obj["_kind"], self._bump())

    def items(self) -> Iterable[dict]:
        return (o for o in self.objects.values() if o["_kind"] == "item")

    @telemetry.timed("upstream")
    def schema(self) -> dict:
        from .schema import MINI

        return MINI

    # ZoteroAPI
    def changes_since(self, version: int):
        cols = [_strip(o) for o in self.objects.values() if o["_kind"] == "collection" and o["version"] > version]
        items = [_strip(o) for o in self.objects.values() if o["_kind"] == "item" and o["version"] > version]
        deleted = {"collections": [k for k, (kind, v) in self.deleted.items() if kind == "collection" and v > version],
                   "items": [k for k, (kind, v) in self.deleted.items() if kind == "item" and v > version]}
        return cols, items, deleted, self.version

    def write_items(self, objects: list[dict]) -> WriteResult:
        res = WriteResult()
        for o in objects:
            key = o.get("key") or new_key()
            cur = self.objects.get(key)
            # like api.zotero.org: writes that name a key must say which version they expect (0 = new)
            if "key" in o and "version" not in o:
                res.failed[key] = "428: Either If-Unmodified-Since-Version or 'version' property must be provided for 'key'-based writes"
                continue
            expected = int(o.get("version", 0))
            if (cur is None and expected != 0) or (cur is not None and expected != cur["version"]):
                res.conflicts.add(key)
                continue
            if o.get("itemType") == "annotation":
                parent = self.objects.get(o.get("parentItem") or (cur or {}).get("parentItem"))
                if not parent or parent.get("itemType") != "attachment":
                    res.failed[key] = "400: parent must be a file attachment"
                    continue
            base = cur or {"_kind": "item", "tags": [], "relations": {}}
            if cur is not None and o.get("itemType") and o["itemType"] != cur.get("itemType"):
                # like the server: a type change moves base-mapped fields and drops the rest
                from .schema import MINI, Schema

                sch = Schema(MINI)
                if sch.has_type(o["itemType"]):
                    base = {**sch.apply_type_change(cur, o["itemType"]), "_kind": "item"}
            data = {**base, **{k: v for k, v in o.items() if k != "version"}, "key": key}
            data["version"] = self._bump()
            data["dateModified"] = _now_iso()
            self.objects[key] = data
            res.versions[key] = data["version"]
            res.data[key] = _strip(data)
        res.library_version = self.version
        return res

    def write_collections(self, objects: list[dict]) -> WriteResult:
        res = WriteResult()
        for o in objects:
            key = o.get("key") or new_key()
            cur = self.objects.get(key)
            if "key" in o and "version" not in o:
                res.failed[key] = "428: Either If-Unmodified-Since-Version or 'version' property must be provided for 'key'-based writes"
                continue
            expected = int(o.get("version", 0))
            if (cur is None and expected != 0) or (cur is not None and expected != cur["version"]):
                res.conflicts.add(key)
                continue
            data = {**(cur or {"_kind": "collection", "parentCollection": False}), **{k: v for k, v in o.items() if k != "version"}, "key": key}
            data["version"] = self._bump()
            self.objects[key] = data
            res.versions[key] = data["version"]
        res.library_version = self.version
        return res

    @telemetry.timed("upstream")
    def delete_items(self, keys_versions: list[tuple[str, int]]) -> WriteResult:
        res = WriteResult()
        for key, version in keys_versions:
            cur = self.objects.get(key)
            if cur is None:
                continue
            if cur["version"] != version:
                res.conflicts.add(key)
                continue
            self.remove(key)
        res.library_version = self.version
        return res


def _strip(o: dict) -> dict:
    return {k: v for k, v in o.items() if k != "_kind"}


def _now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"
