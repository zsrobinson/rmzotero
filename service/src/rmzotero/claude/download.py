"""Short-lived PDF capabilities. Tokens and document bytes must never be logged."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import time
from urllib.parse import urlsplit

import pikepdf


class Downloads:
    def __init__(self, ops):
        self.ops = ops
        self.secret = os.environ.get("RMZOTERO_PDF_SECRET", "").encode()
        self.base = os.environ.get("RMZOTERO_PDF_BASE_URL", "").rstrip("/")
        self.limit = int(os.environ.get("RMZOTERO_PDF_MAX_BYTES", str(50 * 1024 * 1024)))
        if self.limit <= 0:
            raise ValueError("RMZOTERO_PDF_MAX_BYTES must be positive")

    def pdf(self, key, pages):
        attachment, filename, data = self.ops.original_pdf(key)
        if len(data) > self.limit:
            raise ValueError("PDF exceeds download size limit")
        with pikepdf.Pdf.open(io.BytesIO(data)) as source:
            count = len(source.pages)
            if pages is not None:
                if len(pages) > 200 or not re.fullmatch(r"[1-9]\d*(?:-[1-9]\d*)?(?:,[1-9]\d*(?:-[1-9]\d*)?)*", pages):
                    raise ValueError("Use page ranges such as 1-3,5")
                indexes = []
                seen = set()
                for part in pages.split(","):
                    bounds = list(map(int, part.split("-")))
                    lo, hi = bounds[0], bounds[-1]
                    if not 1 <= lo <= hi <= count:
                        raise ValueError("Page range outside PDF")
                    for i in range(lo - 1, hi):
                        if i not in seen:
                            indexes.append(i)
                            seen.add(i)
                with pikepdf.Pdf.new() as subset:
                    subset.pages.extend(source.pages[i] for i in indexes)
                    output = io.BytesIO()
                    subset.save(output)
                    data = output.getvalue()
                count = len(indexes)
        if len(data) > self.limit:
            raise ValueError("PDF exceeds download size limit")
        return attachment, filename, data, count

    def link(self, key, pages=None):
        origin = urlsplit(self.base)
        if len(self.secret) < 32 or origin.scheme not in ("http", "https") or not origin.netloc or origin.query or origin.fragment or origin.username or origin.password:
            raise ValueError("PDF downloads require RMZOTERO_PDF_SECRET (32+ bytes) and RMZOTERO_PDF_BASE_URL")
        attachment, filename, data, count = self.pdf(key, pages)
        expires = int(time.time()) + 600
        payload = base64.urlsafe_b64encode(json.dumps([attachment, pages, expires], separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return {"url": f"{self.base}/pdf-download/{payload}.{signature}", "filename": filename,
                "page_count": count, "byte_size": len(data), "expires_at": expires}

    def fetch(self, token):
        if len(self.secret) < 32 or len(token) > 2048:
            raise ValueError("Invalid capability")
        payload, signature = token.split(".")
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("Invalid capability")
        key, pages, expires = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if type(expires) is not int or expires <= time.time():
            raise ValueError("Expired capability")
        return self.pdf(key, pages)[2]
