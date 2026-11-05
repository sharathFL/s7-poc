"""YAML configuration loading. Secrets come from the environment, never the YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from .models import TagConfig


@dataclass
class S7Config:
    enabled: bool = True
    rack: int = 0
    slot: int = 1
    port: int = 102
    polling_ms: int = 1000
    timeout_ms: int = 3000


@dataclass
class OpcUaConfig:
    enabled: bool = True
    endpoint: str = ""
    polling_ms: int = 1000
    timeout_ms: int = 4000
    mode: str = "poll"
    security_mode: str = "None"
    security_policy: str = "None"
    client_cert: str | None = None
    client_key: str | None = None
    server_cert: str | None = None
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)


@dataclass
class DeviceConfig:
    id: str
    ip: str
    mock: bool = False
    s7: S7Config = field(default_factory=S7Config)
    opcua: OpcUaConfig = field(default_factory=OpcUaConfig)
    tags: list[TagConfig] = field(default_factory=list)


@dataclass
class AppConfig:
    devices: list[DeviceConfig]
    stale_after_factor: float = 3.0
    history_size: int = 500


def _env_creds(dev_id: str) -> tuple[str | None, str | None]:
    """OPCUA_USERNAME_<DEVICE_ID> wins over the global OPCUA_USERNAME."""
    suffix = "_" + dev_id.upper().replace("-", "_")
    user = os.environ.get("OPCUA_USERNAME" + suffix) or os.environ.get("OPCUA_USERNAME")
    pw = os.environ.get("OPCUA_PASSWORD" + suffix) or os.environ.get("OPCUA_PASSWORD")
    if not pw and (f := os.environ.get("OPCUA_PASSWORD_FILE")):
        with open(f, encoding="utf-8") as fh:
            pw = fh.read().strip()
    return user or None, pw or None


def parse_config(raw: dict[str, Any], force_mock: bool = False) -> AppConfig:
    api = raw.get("api") or {}
    devices: list[DeviceConfig] = []
    for d in raw.get("devices") or []:
        s7 = S7Config(**(d.get("s7comm") or {}))
        oc = d.get("opcua") or {}
        opcua = OpcUaConfig(**oc)
        opcua.username, opcua.password = _env_creds(d["id"])
        tags = [TagConfig(**t) for t in d.get("tags") or []]
        if not tags:
            raise ValueError(f"device {d['id']}: no tags configured")
        devices.append(DeviceConfig(id=d["id"], ip=d.get("ip", ""), mock=bool(d.get("mock")) or force_mock,
                                    s7=s7, opcua=opcua, tags=tags))
    if not devices:
        raise ValueError("no devices configured")
    return AppConfig(devices=devices,
                     stale_after_factor=float(api.get("stale_after_factor", 3)),
                     history_size=int(api.get("history_size", 500)))


def load_config(path: str | None = None) -> AppConfig:
    path = path or os.environ.get("CONFIG_PATH", "/config/devices.yaml")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    force_mock = os.environ.get("S7POC_MOCK", "").lower() in ("1", "true", "yes")
    return parse_config(raw, force_mock)
