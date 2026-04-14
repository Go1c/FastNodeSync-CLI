from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServerConfig:
    api: str = ""
    token: str = ""
    vault: str = "defaultVault"


@dataclass
class SyncConfig:
    watch_path: str = "./vault"
    sync_notes: bool = True
    sync_files: bool = True
    sync_config: bool = True
    upload_concurrency: int = 2
    exclude_patterns: list[str] = field(
        default_factory=lambda: [".git/**", ".trash/**", "*.tmp"]
    )
    file_chunk_size: int = 524288


@dataclass
class ClientConfig:
    reconnect_max_retries: int = 15
    reconnect_base_delay: int = 3
    heartbeat_interval: int = 30


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    client: ClientConfig = field(default_factory=ClientConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def ws_api(self) -> str:
        url = self.server.api.rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url

    @property
    def vault_path(self) -> Path:
        return Path(self.sync.watch_path).resolve()


def load_config(path: str) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = AppConfig()

    if "server" in raw:
        s = raw["server"]
        cfg.server = ServerConfig(
            api=s.get("api", ""),
            token=s.get("token", ""),
            vault=s.get("vault", "defaultVault"),
        )

    if "sync" in raw:
        s = raw["sync"]
        cfg.sync = SyncConfig(
            watch_path=s.get("watch_path", "./vault"),
            sync_notes=s.get("sync_notes", True),
            sync_files=s.get("sync_files", True),
            sync_config=s.get("sync_config", True),
            upload_concurrency=max(1, s.get("upload_concurrency", 2)),
            exclude_patterns=s.get(
                "exclude_patterns", [".git/**", ".trash/**", "*.tmp"]
            ),
            file_chunk_size=s.get("file_chunk_size", 524288),
        )

    if "client" in raw:
        c = raw["client"]
        cfg.client = ClientConfig(
            reconnect_max_retries=c.get("reconnect_max_retries", 15),
            reconnect_base_delay=c.get("reconnect_base_delay", 3),
            heartbeat_interval=c.get("heartbeat_interval", 30),
        )

    if "logging" in raw:
        lg = raw["logging"]
        cfg.logging = LoggingConfig(
            level=lg.get("level", "INFO"),
            file=lg.get("file", ""),
        )

    api = cfg.server.api
    if not api:
        api = os.environ.get("FNS_API", "")
        cfg.server.api = api
    token = cfg.server.token
    if not token:
        token = os.environ.get("FNS_TOKEN", "")
        cfg.server.token = token

    if not cfg.server.api or not cfg.server.token:
        raise ValueError(
            "server.api and server.token must be set in config or via "
            "FNS_API / FNS_TOKEN environment variables"
        )

    return cfg
