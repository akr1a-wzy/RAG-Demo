"""Local credential rotation. Passwords never enter command lines or reports."""

import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as file:
        temporary = Path(file.name)
        file.write(text)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rotate_access_code(private: Path, password: str):
    if not 8 <= len(password) <= 128 or password.isspace() or any(
            ord(char) < 32 or ord(char) == 127 for char in password):
        raise ValueError("Use 8-128 characters without control characters")
    config_path, access_path = private / "config.json", private / "access.txt"
    old_config = config_path.read_text(encoding="utf-8")
    old_access = access_path.read_text(encoding="utf-8")
    config = json.loads(old_config)
    lines = old_access.splitlines()
    if (len(lines) != 2 or lines[0] != "Username: " + config["username"]
            or not lines[1].startswith("Password: ")):
        raise ValueError("Existing credential file is invalid; no changes made")
    previous_password = lines[1].removeprefix("Password: ")
    if not hmac.compare_digest(hashlib.sha256(previous_password.encode()).hexdigest(),
                               config["password_sha256"]):
        raise ValueError("Existing credential files do not match; no changes made")
    new_hash = hashlib.sha256(password.encode()).hexdigest()
    if hmac.compare_digest(config["password_sha256"], new_hash):
        raise ValueError("New access code must differ from the current code")
    config["password_sha256"] = new_hash
    try:
        atomic_write(access_path, f"Username: {config['username']}\nPassword: {password}\n")
        atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        atomic_write(access_path, old_access)
        atomic_write(config_path, old_config)
        raise
    return {"origin": config["origin"], "username": config["username"]}
