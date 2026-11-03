"""Interactive, local-only access-code rotation followed by restart and auth checks."""

import getpass
import hashlib
import json
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.web.credentials import rotate_access_code

ROOT = Path(__file__).resolve().parents[1]


def public_state_hash():
    result = {}
    for name in ["sessions.sqlite", "budget.sqlite"]:
        path = ROOT / "data/public_demo" / name
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            dump = "\n".join(db.iterdump())
        result[name] = hashlib.sha256(dump.encode()).hexdigest()
    return result


def main():
    private = ROOT / "tmp/public-demo"
    config = json.loads((private / "config.json").read_text(encoding="utf-8"))
    old_access = (private / "access.txt").read_text(encoding="utf-8").splitlines()
    old_password = old_access[1].removeprefix("Password: ")
    print("Change the DEMO access code. Do not enter your ngrok token or API key.")
    for _ in range(3):
        password = getpass.getpass("New demo access code (8-128 characters; hidden): ")
        confirm = getpass.getpass("Confirm new access code (hidden): ")
        if password != confirm:
            print("The two entries differ. Please retry.")
            continue
        try:
            before = public_state_hash()
            rotate_access_code(private, password)
            break
        except ValueError as error:
            print(str(error))
    else:
        raise SystemExit("Not changed. Run the command again when ready.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = ROOT / f"eval/results/public-access-change-{stamp}.json"
    report = {"date": datetime.now(UTC).isoformat(), "version": "public-access-rotation-v1",
              "origin": config["origin"], "checks": {}, "model_calls": 0,
              "timing_scope": ("interactive entry excluded; stop/start and HTTP "
                               "auth probes included")}
    started = time.perf_counter()
    try:
        for script in ["Stop-PublicDemo.ps1", "Start-PublicDemo.ps1"]:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / script)], cwd=ROOT, capture_output=True, timeout=150)
            (private / f"rotation-{stamp}-{script}.log").write_bytes(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError("Public entry restart failed; see private rotation logs")
        with httpx.Client(base_url=config["origin"], timeout=30, trust_env=False,
                          follow_redirects=False,
                          headers={"ngrok-skip-browser-warning": "1"}) as client:
            old = client.get("/api/health", auth=httpx.BasicAuth(config["username"], old_password))
            new = client.get("/api/health", auth=httpx.BasicAuth(config["username"], password))
            anon = client.get("/api/health")
        state = json.loads((private / "state.json").read_text(encoding="utf-8-sig"))
        report["checks"] = {
            "old_code_rejected": old.status_code == 401,
            "new_code_ready": new.status_code == 200 and new.json().get("status") == "ready",
            "anonymous_rejected": anon.status_code == 401,
            "fixed_origin_preserved": state["origin"] == config["origin"],
            "public_sessions_and_budget_preserved": public_state_hash() == before,
        }
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["checks"]["restart_and_auth_verified"] = False
    report.update(seconds=round(time.perf_counter() - started, 4),
                  denominator=len(report["checks"]), passed=sum(report["checks"].values()),
                  all_passed=all(report["checks"].values()))
    with report_path.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    result = {"completed": report["all_passed"], "report": str(report_path),
              "origin": config["origin"], "passed": report["passed"],
              "denominator": report["denominator"]}
    (private / "last-rotation.json").write_text(json.dumps(result, indent=2) + "\n",
                                               encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if not report["all_passed"]:
        print("Code files were updated, but external verification needs attention. "
              "Keep this report.")
        raise SystemExit(1)
    print("Access code changed. Reopen the demo and sign in with the new code.")


if __name__ == "__main__":
    main()
