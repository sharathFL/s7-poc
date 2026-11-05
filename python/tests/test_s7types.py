"""Unit tests with SYNTHETIC byte arrays (no PLC, no network)."""
import struct
import pytest
from app.s7types import AddressError, byte_length, decode, parse_address


def test_parse_real():
    a = parse_address("DB10.DBD0")
    assert (a.db, a.byte, a.bit, a.kind) == (10, 0, None, "D")


def test_parse_bool_and_percent_prefix():
    a = parse_address("%DB10.DBX12.3")
    assert (a.db, a.byte, a.bit) == (10, 12, 3)


@pytest.mark.parametrize("bad", ["", "M0.0", "DB10.DBX12", "DB10.DBD0.1", "DB10.DBX12.9", "DBx.DBD0", "DB10"])
def test_parse_rejects(bad):
    with pytest.raises(AddressError):
        parse_address(bad)


def test_real():
    assert decode("REAL", struct.pack(">f", 35.5)) == 35.5


def test_real_nan_is_error():
    with pytest.raises(ValueError):
        decode("REAL", struct.pack(">f", float("nan")))


@pytest.mark.parametrize("t,fmt,v", [("INT", ">h", -1234), ("UINT", ">H", 65000), ("DINT", ">i", -70000),
                                    ("UDINT", ">I", 4_000_000_000), ("LREAL", ">d", 3.14159), ("BYTE", ">B", 200),
                                    ("SINT", ">b", -5), ("LINT", ">q", -2**40)])
def test_numeric(t, fmt, v):
    assert decode(t, struct.pack(fmt, v)) == v


def test_bool_bits():
    assert decode("BOOL", b"\x05", 0) is True
    assert decode("BOOL", b"\x05", 1) is False
    assert decode("BOOL", b"\x05", 2) is True


def test_bool_needs_bit():
    with pytest.raises(ValueError):
        decode("BOOL", b"\x01")


def test_string():
    assert decode("STRING[10]", bytes([10, 5]) + b"hello" + b"\x00" * 5) == "hello"


def test_short_buffer():
    with pytest.raises(ValueError):
        decode("DINT", b"\x00\x01")


def test_lengths():
    assert byte_length("REAL") == 4 and byte_length("BOOL") == 1 and byte_length("STRING[10]") == 12
    with pytest.raises(AddressError):
        byte_length("FOO")
