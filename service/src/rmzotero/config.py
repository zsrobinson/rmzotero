"""Configuration: a TOML file for connections, plus runtime settings the plugin can change."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .state import State


@dataclass
class EpubDefaults:
    """Rendition typography, mirroring the tablet's own ebook options (assumption A5)."""

    font: str = "EB Garamond"  # Noto Sans | Noto Sans UI | EB Garamond | Noto Mono | Noto Serif
    text_scale: float = 1.0  # 0.7 0.8 1 1.2 1.5 2
    line_height: int = 150  # 100 150 200
    margins: int = 125  # 50 125 200 (tablet pixels)
    alignment: str = "justify"  # justify | left

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class Settings:
    """Settings the user can change from the plugin (persisted in the state DB)."""

    paused: bool = False  # keep the service and plugin API running, but leave the tablet alone
    sync_root: str = "Zotero"
    excluded_collections: list[str] = field(default_factory=list)
    pointers: str = "unrelated"  # all | none | unrelated
    ocr: bool = True
    archive_after_days: int = 0  # 0 = never archive
    tablet_capacity_gb: float = 8.0
    filename_template: str = ""
    epub: EpubDefaults = field(default_factory=EpubDefaults)

    def as_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "epub"}
        d["epub"] = self.epub.as_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict, base: "Settings | None" = None) -> "Settings":
        s = base or cls()
        out = cls(**{f.name: getattr(s, f.name) for f in fields(cls)})
        for f in fields(cls):
            if f.name == "epub" and isinstance(d.get("epub"), dict):
                e = out.epub.as_dict()
                e.update({k: v for k, v in d["epub"].items() if k in e})
                out.epub = EpubDefaults(**e)
            elif f.name in d and f.name != "epub":
                setattr(out, f.name, d[f.name])
        if out.pointers not in ("all", "none", "unrelated"):
            raise ValueError("pointers must be all, none or unrelated")
        out.sync_root = "/".join(p for p in str(out.sync_root).split("/") if p.strip())
        return out


@dataclass
class AccessConfig:
    team_domain: str = ""
    audience: str = ""
    hostnames: list[str] = field(default_factory=list)
    allowed_emails: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.team_domain and self.audience and self.hostnames)

    def __post_init__(self):
        import re

        if any((self.team_domain, self.audience, self.hostnames)):
            self.team_domain = self.team_domain.rstrip("/")
            if not self.team_domain.startswith("https://"):
                self.team_domain = "https://" + self.team_domain
            if not re.fullmatch(r"https://[a-z0-9-]+\.cloudflareaccess\.com", self.team_domain):
                raise ValueError("access.team_domain must be a Cloudflare Access team domain")
            if not self.enabled:
                raise ValueError("access requires team_domain, audience and hostnames")
        self.hostnames = [h.lower() for h in self.hostnames]
        if any(not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", h) for h in self.hostnames):
            raise ValueError("access.hostnames must contain exact DNS hostnames")


@dataclass
class Config:
    data_dir: Path
    zotero_user_id: int
    zotero_api_key: str
    webdav_url: str
    webdav_username: str
    webdav_password: str
    rmfakecloud_url: str
    api_listen: str = "0.0.0.0:8787"
    api_token: str = ""
    quiet_seconds: float = 120.0
    poll_seconds: float = 900.0
    defaults: Settings = field(default_factory=Settings)
    # Claude connector (ADR 0015); disabled unless a secret is set
    mcp_secret: str = ""
    mcp_listen: str = "0.0.0.0:8788"
    mcp_legacy_enabled: bool = True
    mcp_legacy_hostnames: list[str] = field(default_factory=lambda: ["rmzoteroclaude.zsrobinson.com", "127.0.0.1", "localhost"])
    access: AccessConfig = field(default_factory=AccessConfig)
    contact_email: str = ""  # sent to Unpaywall and OpenAlex when looking for open-access PDFs
    translation_server_url: str = "http://translation-server:1969"

    @classmethod
    def load(cls, path: Path | str) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        env = os.environ.get
        z, w, r, a, s = (raw.get(k, {}) for k in ("zotero", "webdav", "rmfakecloud", "api", "sync"))
        c, t = raw.get("claude", {}), raw.get("translation_server", {})
        access = raw.get("access", {})

        def items(name, section, key, default):
            value = env(name)
            return [s.strip() for s in value.split(",") if s.strip()] if value is not None else section.get(key, default)

        legacy = env("RMZOTERO_MCP_LEGACY_ENABLED")
        if legacy is not None and legacy.lower() not in ("true", "false"):
            raise ValueError("RMZOTERO_MCP_LEGACY_ENABLED must be true or false")
        defaults = Settings.from_dict({**s, "epub": raw.get("epub", {})})
        return cls(
            data_dir=Path(env("RMZOTERO_DATA_DIR") or raw.get("data_dir", "/data")),
            zotero_user_id=int(env("ZOTERO_USER_ID") or z.get("user_id", 0)),
            zotero_api_key=env("ZOTERO_API_KEY") or z.get("api_key", ""),
            webdav_url=env("WEBDAV_URL") or w.get("url", ""),
            webdav_username=env("WEBDAV_USERNAME") or w.get("username", ""),
            webdav_password=env("WEBDAV_PASSWORD") or w.get("password", ""),
            rmfakecloud_url=env("RMFAKECLOUD_URL") or r.get("url", ""),
            api_listen=a.get("listen", "0.0.0.0:8787"),
            api_token=env("RMZOTERO_API_TOKEN") or a.get("token", ""),
            quiet_seconds=float(s.get("quiet_seconds", 120)),
            poll_seconds=float(s.get("poll_seconds", 900)),
            defaults=defaults,
            mcp_secret=env("RMZOTERO_MCP_SECRET") or c.get("secret", ""),
            mcp_listen=c.get("listen", "0.0.0.0:8788"),
            mcp_legacy_enabled=legacy.lower() == "true" if legacy is not None else c.get("legacy_enabled", True),
            mcp_legacy_hostnames=items("RMZOTERO_MCP_LEGACY_HOSTNAMES", c, "legacy_hostnames",
                                       ["rmzoteroclaude.zsrobinson.com", "127.0.0.1", "localhost"]),
            access=AccessConfig(
                team_domain=env("RMZOTERO_ACCESS_TEAM_DOMAIN") or access.get("team_domain", ""),
                audience=env("RMZOTERO_ACCESS_AUDIENCE") or access.get("audience", ""),
                hostnames=items("RMZOTERO_ACCESS_HOSTNAMES", access, "hostnames", []),
                allowed_emails=items("RMZOTERO_ACCESS_ALLOWED_EMAILS", access, "allowed_emails", []),
            ),
            contact_email=env("RMZOTERO_CONTACT_EMAIL") or c.get("contact_email", ""),
            translation_server_url=env("TRANSLATION_SERVER_URL") or t.get("url", "http://translation-server:1969"),
        )


def load_settings(state: State, defaults: Settings) -> Settings:
    return Settings.from_dict(state.get("settings", {}), defaults)


def save_settings(state: State, settings: Settings, defaults: Settings) -> None:
    state.set("settings", settings.as_dict())
