"""Protocol-driver interface. Drivers are READ-ONLY and know nothing about each other.

Adding Modbus TCP later = one new subclass + a factory entry; the API does not change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from ..config import DeviceConfig
from ..models import Reading


class DriverError(Exception):
    """Connection/transport level failure (whole read failed)."""


ReadingCallback = Callable[[Reading], Awaitable[None] | None]


class Driver(ABC):
    protocol: str = "unknown"
    polling_ms: int = 1000
    supports_subscribe: bool = False

    def __init__(self, device: DeviceConfig):
        self.device = device
        self.connected = False

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def read(self) -> list[Reading]:
        """Read all configured tags once. Raise DriverError on transport failure;
        mark individual bad items with Quality.INVALID instead of raising."""

    async def subscribe(self, callback: ReadingCallback) -> None:  # pragma: no cover
        raise NotImplementedError

    async def healthy(self) -> bool:
        return self.connected

    async def browse(self) -> list[dict[str, Any]]:
        raise NotImplementedError(f"{self.protocol} does not support browsing")
