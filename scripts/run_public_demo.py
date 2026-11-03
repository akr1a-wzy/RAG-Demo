"""Prepare private public-entry configuration, or serve the public entry."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--origin", required=True)
    prepare.add_argument("--auth", action=argparse.BooleanOptionalAction, default=None,
                         help="Require login by default; --no-auth disables it")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    private = ROOT / "tmp/public-demo"
    private.mkdir(parents=True, exist_ok=True)
    config_path = private / "config.json"
    if args.command == "prepare":
        from app.web.public import PublicConfig

        existing = (json.loads(config_path.read_text(encoding="utf-8"))
                    if config_path.exists() else {})
        if existing and not (private / "access.txt").is_file():
            raise RuntimeError("Existing config has no access file; recover it before proceeding")
        password = secrets.token_urlsafe(18) if not existing else None
        data = {
            "origin": args.origin.rstrip("/"), "username": "demo",
            "password_sha256": existing.get("password_sha256") or hashlib.sha256(
                password.encode()).hexdigest(),
            "state_dir": "data/public_demo", "daily_chat_limit": 50,
            "writes_per_minute": 20, "max_body_bytes": 16384,
            "auth_required": (args.auth if args.auth is not None
                              else existing.get("auth_required", True)),
        }
        PublicConfig(**{**data, "state_dir": ROOT / data["state_dir"]})
        if password:
            with (private / "access.txt").open("x", encoding="utf-8") as file:
                file.write(f"Username: demo\nPassword: {password}\n")
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        print(json.dumps({"origin": data["origin"], "config": str(config_path),
                          "auth_required": data["auth_required"],
                          "access_file": str(private / "access.txt")}, ensure_ascii=False))
        return
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    import uvicorn

    from app.web.public import create_public_app, load_config

    config = load_config(config_path, ROOT)
    application = create_public_app(ROOT, config)
    uvicorn.run(application, host="127.0.0.1", port=args.port, workers=1,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
