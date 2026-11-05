import pytest
from app.config import parse_config


def test_secrets_from_env_not_yaml(monkeypatch):
    monkeypatch.setenv("OPCUA_USERNAME", "u"); monkeypatch.setenv("OPCUA_PASSWORD", "s3cret")
    c = parse_config({"devices": [{"id": "a-1", "ip": "1.1.1.1", "tags": [{"name": "t", "type": "REAL"}]}]})
    assert c.devices[0].opcua.username == "u" and c.devices[0].opcua.password == "s3cret"
    assert "s3cret" not in repr(c.devices[0].opcua)  # password hidden from repr


def test_requires_tags():
    with pytest.raises(ValueError):
        parse_config({"devices": [{"id": "a", "ip": "x", "tags": []}]})
