"""S7 address parser and big-endian data type decoders (pure functions, no I/O)."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any

# Sizes in bytes of the supported types
SIZES = {"BOOL": 1, "BYTE": 1, "SINT": 1, "USINT": 1, "CHAR": 1,
         "INT": 2, "UINT": 2, "WORD": 2,
         "DINT": 4, "UDINT": 4, "DWORD": 4, "REAL": 4, "TIME": 4,
         "LREAL": 8, "LINT": 8, "ULINT": 8, "LWORD": 8}

_ADDR = re.compile(r"^%?DB(?P<db>\d+)\.DB(?P<kind>[XBWDS])(?P<byte>\d+)(?:\.(?P<bit>[0-7]))?$", re.I)


class AddressError(ValueError):
    pass


@dataclass(frozen=True)
class S7Address:
    db: int
    byte: int
    bit: int | None
    kind: str  # X B W D S

    def __str__(self) -> str:
        return f"DB{self.db}.DB{self.kind}{self.byte}" + (f".{self.bit}" if self.bit is not None else "")


def parse_address(text: str) -> S7Address:
    """Parse 'DB10.DBD0', 'DB10.DBX12.0', '%DB1.DBW20'. Only data-block addressing is supported."""
    m = _ADDR.match(text.strip())
    if not m:
        raise AddressError(f"unsupported S7 address: {text!r} (expected e.g. DB10.DBD0 or DB10.DBX12.0)")
    kind = m["kind"].upper()
    bit = int(m["bit"]) if m["bit"] is not None else None
    if kind == "X" and bit is None:
        raise AddressError(f"{text!r}: bit address needs a bit number, e.g. DB10.DBX12.0")
    if kind != "X" and bit is not None:
        raise AddressError(f"{text!r}: only DBX addresses take a bit number")
    return S7Address(int(m["db"]), int(m["byte"]), bit, kind)


def _split_type(dtype: str) -> tuple[str, int | None]:
    m = re.fullmatch(r"(W?STRING)(?:\[(\d+)\])?", dtype.strip().upper())
    if m:
        return m[1], int(m[2]) if m[2] else 254
    return dtype.strip().upper(), None


def byte_length(dtype: str) -> int:
    base, n = _split_type(dtype)
    if base == "STRING":
        return 2 + (n or 254)
    if base == "WSTRING":
        return 4 + 2 * (n or 254)
    try:
        return SIZES[base]
    except KeyError:
        raise AddressError(f"unsupported datatype {dtype!r}") from None


def decode(dtype: str, data: bytes | bytearray, bit: int | None = None) -> Any:
    """Decode `data` (exactly byte_length(dtype) bytes, S7 big-endian) to a Python value."""
    base, _ = _split_type(dtype)
    need = byte_length(dtype)
    if len(data) < need:
        raise ValueError(f"{dtype}: need {need} bytes, got {len(data)}")
    d = bytes(data[:need])
    if base == "BOOL":
        if bit is None:
            raise ValueError("BOOL requires a bit offset")
        return bool((d[0] >> bit) & 1)
    fmt = {"BYTE": ">B", "USINT": ">B", "SINT": ">b", "INT": ">h", "UINT": ">H", "WORD": ">H",
           "DINT": ">i", "UDINT": ">I", "DWORD": ">I", "TIME": ">i", "REAL": ">f", "LREAL": ">d",
           "LINT": ">q", "ULINT": ">Q", "LWORD": ">Q"}.get(base)
    if fmt:
        v = struct.unpack(fmt, d)[0]
        if base in ("REAL", "LREAL"):
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError(f"{dtype}: NaN/Inf")
        return v
    if base == "CHAR":
        return d.decode("latin-1")
    if base == "STRING":   # [max_len][actual_len][chars]
        actual = min(d[1], d[0])
        return d[2:2 + actual].decode("latin-1")
    if base == "WSTRING":
        actual = min(struct.unpack(">H", d[2:4])[0], struct.unpack(">H", d[0:2])[0])
        return d[4:4 + 2 * actual].decode("utf-16-be")
    raise ValueError(f"unsupported datatype {dtype!r}")


def span(addr: S7Address, dtype: str) -> tuple[int, int]:
    """(start byte, length) in the DB needed to read this tag."""
    return addr.byte, byte_length(dtype)
