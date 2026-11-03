import hashlib
import json

import pytest

from app.web import credentials

OLD = "old-synthetic-code"
NEW = "new-synthetic-code"


@pytest.fixture
def private(tmp_path):
    config = {"origin": "https://example.ngrok-free.app", "username": "demo",
              "password_sha256": hashlib.sha256(OLD.encode()).hexdigest(),
              "daily_chat_limit": 50, "writes_per_minute": 20, "state_dir": "data/public_demo"}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "access.txt").write_text(f"Username: demo\nPassword: {OLD}\n", encoding="utf-8")
    return tmp_path


def test_rotation_keeps_origin_and_limits_and_hashes_the_new_code(private):
    result = credentials.rotate_access_code(private, NEW)
    config = json.loads((private / "config.json").read_text(encoding="utf-8"))
    assert config["password_sha256"] == hashlib.sha256(NEW.encode()).hexdigest()
    assert config["origin"] == result["origin"] == "https://example.ngrok-free.app"
    assert config["daily_chat_limit"] == 50 and config["writes_per_minute"] == 20
    assert NEW not in json.dumps(result) + json.dumps(config)
    assert (private / "access.txt").read_text(encoding="utf-8") == (
        f"Username: demo\nPassword: {NEW}\n")


@pytest.mark.parametrize("bad", ["short", " " * 8, "code\nwith-line", "x" * 129, OLD])
def test_invalid_or_unchanged_code_cannot_modify_existing_files(private, bad):
    before = {p.name: p.read_bytes() for p in private.iterdir()}
    with pytest.raises(ValueError):
        credentials.rotate_access_code(private, bad)
    assert {p.name: p.read_bytes() for p in private.iterdir()} == before


def test_inconsistent_existing_files_are_not_silently_replaced(private):
    (private / "access.txt").write_text("Username: demo\nPassword: wrong\n", encoding="utf-8")
    with pytest.raises(ValueError, match="do not match"):
        credentials.rotate_access_code(private, NEW)


def test_failed_second_write_restores_both_files(private, monkeypatch):
    before = {p.name: p.read_bytes() for p in private.iterdir()}
    original = credentials.atomic_write
    writes = 0

    def fail_second(path, text):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("synthetic write failure")
        return original(path, text)

    monkeypatch.setattr(credentials, "atomic_write", fail_second)
    with pytest.raises(OSError):
        credentials.rotate_access_code(private, NEW)
    assert {p.name: p.read_bytes() for p in private.iterdir()} == before
