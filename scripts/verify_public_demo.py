"""External public-entry smoke checks. Uses structured facts; no LLM calls."""

import argparse
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot():
    profile = json.loads((ROOT / "data/retrieval/active_profile.json").read_text(encoding="utf-8"))
    proof = json.loads((ROOT / profile["proof"]["path"]).read_text(encoding="utf-8"))
    runtime = {path: sha(ROOT / path) for path in proof["runtime_hashes"]}
    qa = {p.relative_to(ROOT).as_posix(): sha(p) for p in (ROOT / "data/qa").glob("*.json")}
    with sqlite3.connect((ROOT / "data/qa_sessions.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        turns = list(db.execute("SELECT * FROM turns ORDER BY id"))
        first = db.execute("SELECT id FROM sessions LIMIT 1").fetchone()
    return {"runtime": runtime, "runtime_matches_release": runtime == proof["runtime_hashes"],
            "qa": qa, "personal_turns": len(turns),
            "personal_turns_sha256": hashlib.sha256(json.dumps(
                turns, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}, (
                    first[0] if first else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new report path")
    private = ROOT / "tmp/public-demo"
    config = json.loads((private / "config.json").read_text(encoding="utf-8"))
    auth_required = config.get("auth_required", True)
    auth = None
    if auth_required:
        access = (private / "access.txt").read_text(encoding="utf-8").splitlines()
        auth = httpx.BasicAuth(access[0].removeprefix("Username: "),
                              access[1].removeprefix("Password: "))
    checks, requests = {}, []
    before, personal_sid = snapshot()
    started = time.perf_counter()
    report = {"date": datetime.now(UTC).isoformat(), "version": "ngrok-public-entry-v1",
              "origin": config["origin"],
              "auth_required": auth_required,
              "role": "external_HTTP_known_structured_smoke_not_accuracy",
              "model_calls": 0, "before": before, "checks": checks, "requests": requests,
              "browser_verified": False, "phone_network_verified": False}
    try:
        with httpx.Client(base_url=config["origin"], timeout=35, trust_env=False,
                          follow_redirects=False, auth=auth,
                          headers={"ngrok-skip-browser-warning": "1"}) as client:
            def request(method, path, **kwargs):
                request_started = time.perf_counter()
                response = client.request(method, path, **kwargs)
                requests.append({"method": method, "path": path,
                                 "status": response.status_code,
                                 "seconds": round(time.perf_counter() - request_started, 4)})
                return response

            r = request("GET", "/", auth=None)
            checks["anonymous_home_matches_auth_mode"] = (
                r.status_code == (401 if auth_required else 200))
            checks["login_challenge_matches_auth_mode"] = (
                ("www-authenticate" in r.headers) == auth_required)
            r = request("GET", "/api/health", auth=httpx.BasicAuth("demo", "invalid"))
            checks["invalid_credentials_match_auth_mode"] = (
                r.status_code == (401 if auth_required else 200))
            r = request("GET", "/")
            checks["authenticated_home_200"] = r.status_code == 200 and "有据" in r.text
            checks["authenticated_private_no_store"] = "no-store" in r.headers.get(
                "cache-control", "")
            r = request("GET", "/api/health")
            health = r.json()
            checks["gpu_health_ready"] = (r.status_code == 200 and health.get("status") == "ready"
                                          and health.get("deployment", {}).get("device") == "cuda")
            checks["corpus_20_60_32668"] = (health.get("companies") == 20
                                           and health.get("documents") == 60
                                           and health.get("deployment", {}).get(
                                               "collection_count") == 32668)
            r = request("POST", "/api/sessions", headers={"Origin": "https://evil.invalid"})
            checks["cross_origin_write_403"] = r.status_code == 403
            r = request("POST", "/api/sessions", headers={"Origin": config["origin"]})
            checks["same_origin_session_created"] = r.status_code == 201
            sid = r.json()["session_id"]
            question = "宁德时代2025年营业收入是多少？"
            r = request("POST", "/api/chat", json={"session_id": sid, "question": question},
                        headers={"Origin": config["origin"]})
            answer = r.json()
            checks["structured_answer_with_citations"] = (
                r.status_code == 200 and answer.get("mode") == "structured_answer"
                and bool(answer.get("claims")) and bool(answer.get("sources"))
                and any(c.get("citations") for c in answer.get("claims", [])))
            checks["structured_query_no_usage"] = not answer.get("usage")
            report["structured_answer"] = answer
            r = request("POST", "/api/chat", json={"session_id": sid,
                        "question": "那2025年总资产是多少？"}, headers={"Origin": config["origin"]})
            followup = r.json()
            checks["followup_structured_answer"] = (r.status_code == 200
                                                     and followup.get("mode") == "structured_answer"
                                                     and not followup.get("usage"))
            report["followup_answer"] = followup
            r = request("GET", f"/api/sessions/{sid}")
            checks["history_two_turns"] = (r.status_code == 200
                                           and len(r.json().get("turns", [])) == 2)
            sources = answer.get("sources", [])
            if sources:
                doc_id = sources[0].get("document_id")
                if not doc_id:
                    doc_id = sources[0].get("metadata", {}).get("document_id")
                if doc_id:
                    r = request("GET", f"/documents/{doc_id}.pdf",
                                headers={"Range": "bytes=0-1023"})
                    checks["pdf_range_206"] = (r.status_code == 206
                                               and r.content.startswith(b"%PDF")
                                               and len(r.content) == 1024)
            checks.setdefault("pdf_range_206", False)
            if personal_sid:
                r = request("GET", f"/api/sessions/{personal_sid}")
                checks["personal_session_unavailable"] = r.status_code == 404
            r = request("GET", "/.env")
            checks["env_unavailable"] = r.status_code == 404
            r = request("GET", "/openapi.json")
            checks["internal_api_docs_hidden"] = r.status_code == 404
            r = request("DELETE", f"/api/sessions/{sid}/messages",
                        headers={"Origin": config["origin"]})
            checks["public_session_reset"] = r.status_code == 200
            r = request("GET", f"/api/sessions/{sid}")
            checks["public_history_empty_after_reset"] = (r.status_code == 200
                                                         and not r.json()["turns"])
    except Exception as error:
        # Do not persist credential-bearing requests or provider response bodies.
        report["error_type"] = type(error).__name__
        checks["completed_all_requests"] = False
    finally:
        after, _ = snapshot()
        report["after"] = after
        checks["released_runtime_files_unchanged"] = before["runtime"] == after["runtime"]
        checks["released_runtime_matches_proof"] = after["runtime_matches_release"]
        checks["qa_data_unchanged"] = before["qa"] == after["qa"]
        checks["personal_history_unchanged"] = (before["personal_turns_sha256"]
                                                 == after["personal_turns_sha256"])
        report.update(seconds=round(time.perf_counter() - started, 4),
                      denominator=len(checks), passed=sum(checks.values()),
                      all_passed=all(checks.values()), top_k_catalog=None,
                      timing_scope="external HTTPS serial smoke incl. network and local GPU "
                                   "readiness; excludes startup; structured facts, zero LLM calls")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
        print(json.dumps({"report": str(args.output), "passed": report["passed"],
                          "denominator": report["denominator"], "seconds": report["seconds"],
                          "failed": [key for key, passed in checks.items() if not passed]},
                         ensure_ascii=False))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
