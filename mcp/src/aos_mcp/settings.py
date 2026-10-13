"""Loopback-only MCP configuration and bounded, uncached credential reads."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MAX_TOKEN_BYTES = 4096


@dataclass(frozen=True)
class Credential:
    value: str | None = field(default=None, repr=False)
    path: Path | None = None

    def read(self) -> str | None:
        """Reopen files each time; unreadable/empty/invalid files revoke access."""
        value: str | None
        try:
            if self.path is not None:
                with self.path.open("rb") as handle:
                    raw = handle.read(MAX_TOKEN_BYTES + 1)
                if len(raw) > MAX_TOKEN_BYTES:
                    return None
                value = raw.decode("ascii").rstrip("\r\n")
            else:
                value = self.value
            if value is None or not re.fullmatch(r"[\x21-\x7e]{1,4096}", value):
                return None
            return value
        except (OSError, UnicodeError, ValueError):
            return None

    @classmethod
    def from_env(cls, name: str) -> Credential:
        value = os.environ.get(f"AOS_{name}_TOKEN")
        path = os.environ.get(f"AOS_{name}_TOKEN_FILE")
        if path is not None:
            return cls(path=Path(path))
        if value is not None:
            return cls(value=value)
        return cls(path=Path(f"/etc/aos/{name.lower()}.token"))


@dataclass(frozen=True)
class Settings:
    mcp_token: Credential = field(
        default_factory=lambda: Credential(path=Path("/etc/aos/mcp.token"))
    )
    bridge_token: Credential = field(
        default_factory=lambda: Credential(path=Path("/etc/aos/bridge.token"))
    )
    bridge_url: str = "http://127.0.0.1:8080"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "127.0.0.1:8090", "localhost:8090")
    request_limit: int = 256 * 1024
    response_limit: int = 512 * 1024
    timeout_s: float = 26.0

    def __post_init__(self) -> None:
        url = urlsplit(self.bridge_url)
        try:
            port = url.port
        except ValueError:
            raise ValueError("bridge URL must be a plain loopback HTTP origin") from None
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or port == 0
        ):
            raise ValueError("bridge URL must be a plain loopback HTTP origin")
        if not 1 <= self.request_limit <= 256 * 1024:
            raise ValueError("MCP request limit out of bounds")
        if not 1 <= self.response_limit <= 512 * 1024 or not 0 < self.timeout_s <= 30:
            raise ValueError("MCP response limit or timeout out of bounds")
        if not self.allowed_hosts or any(
            not re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)*[a-zA-Z0-9-]+(?::[0-9]{1,5})?", host)
            for host in self.allowed_hosts
        ):
            raise ValueError("MCP allowed hosts must be exact host headers, without wildcards")

    @classmethod
    def from_env(cls) -> Settings:
        hosts = os.environ.get("AOS_MCP_ALLOWED_HOSTS")
        allowed_hosts = (
            cls().allowed_hosts if hosts is None else tuple(h.strip() for h in hosts.split(","))
        )
        return cls(
            mcp_token=Credential.from_env("MCP"),
            bridge_token=Credential.from_env("BRIDGE"),
            bridge_url=os.environ.get("AOS_BRIDGE_URL", "http://127.0.0.1:8080"),
            allowed_hosts=allowed_hosts,
            timeout_s=float(os.environ.get("AOS_MCP_TIMEOUT_S", "26")),
        )
