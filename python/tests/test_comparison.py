from datetime import timedelta
from app.models import Quality, Reading, utcnow
from app.services.comparison import compare_tag


def mk(proto, v, dt="REAL", q=Quality.GOOD, off=0.0):
    return Reading("d", proto, "T", dt, v, utcnow() + timedelta(seconds=off), q)


def test_match_within_tolerance_and_skew():
    r = compare_tag(mk("s7comm", 35.50), mk("opcua", 35.52, "Float", off=0.2))
    assert r["match"] and r["type_match"] and 150 < r["skew_ms"] < 250


def test_different():
    assert compare_tag(mk("s7comm", 35.0), mk("opcua", 50.0, "Float"))["status"] == "DIFFERENT"


def test_type_mismatch():
    assert compare_tag(mk("s7comm", 1, "DINT"), mk("opcua", 1, "Int16"))["type_match"] is False


def test_unusable_when_bad_quality():
    r = compare_tag(mk("s7comm", 1.0, q=Quality.COMM_FAILURE), mk("opcua", 1.0, "Float"))
    assert r["status"] == "UNUSABLE" and r["match"] is None


def test_incomplete():
    assert compare_tag(mk("s7comm", 1.0), None)["status"] == "INCOMPLETE"


def test_bool():
    assert compare_tag(mk("s7comm", True, "BOOL"), mk("opcua", True, "Boolean"))["match"]
