"""Local bridge configuration; credentials are resolved per request, not cached."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path("/var/lib/aos")
    bridge_token: str | None = None
    bridge_token_file: Path | None = None
    display_token: str | None = None
    display_token_file: Path | None = None
    muse_token: str | None = None
    muse_token_file: Path | None = None
    cec_socket: Path = Path("/run/aos/cec.sock")
    cec_timeout_s: float = 22.0
    inbox_dir: Path | None = None
    inbox_interval_s: float = 1.0
    inbox_retention: int = 100
    queue_limit: int = 1000
    history_limit: int = 1000
    heartbeat_s: float = 15.0
    stream_limit: int = 32
    contracts_dir: Path | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.stream_limit <= 32:
            raise ValueError("stream_limit must be between 1 and 32")
        if not 1 <= self.queue_limit <= 1000 or not 1 <= self.history_limit <= 10000:
            raise ValueError("queue/history limits out of range")
        if not 0 < self.cec_timeout_s <= 30 or not 0 < self.heartbeat_s <= 15:
            raise ValueError("timeouts out of range")
        if not 0 < self.inbox_interval_s <= 60 or not 1 <= self.inbox_retention <= 1000:
            raise ValueError("inbox limits out of range")

    @classmethod
    def from_env(cls) -> Settings:
        def token(scope: str) -> tuple[str | None, Path | None]:
            value = os.environ.get(f"AOS_{scope}_TOKEN")
            filename = os.environ.get(f"AOS_{scope}_TOKEN_FILE")
            if filename is not None:
                return value, Path(filename)
            return value, None if value is not None else Path(f"/etc/aos/{scope.lower()}.token")

        bridge, bridge_file = token("BRIDGE")
        display, display_file = token("DISPLAY")
        muse, muse_file = token("MUSE")
        inbox = os.environ.get("AOS_INBOX_DIR")
        contracts = os.environ.get("AOS_CONTRACTS_DIR")
        return cls(
            state_dir=Path(os.environ.get("AOS_STATE_DIR", "/var/lib/aos")),
            bridge_token=bridge,
            bridge_token_file=bridge_file,
            display_token=display,
            display_token_file=display_file,
            muse_token=muse,
            muse_token_file=muse_file,
            cec_socket=Path(os.environ.get("AOS_CEC_SOCKET", "/run/aos/cec.sock")),
            cec_timeout_s=float(os.environ.get("AOS_CEC_TIMEOUT_S", "22")),
            inbox_dir=Path(inbox) if inbox else None,
            contracts_dir=Path(contracts) if contracts else None,
        )
