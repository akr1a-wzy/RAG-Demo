"""External GET checks of portfolio assets; no model or business requests."""

import argparse
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import httpx
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids, self.resources = set(), set(), set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "a" and attrs.get("href"):
            self.links.add(attrs["href"])
        if tag in {"script", "link"}:
            value = attrs.get("src") or attrs.get("href")
            if value:
                self.resources.add(value)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--pdf-pages-visually-reviewed", type=int, choices=[0, 4], default=0)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    config = json.loads((ROOT / "tmp/public-demo/config.json").read_text(encoding="utf-8"))
    auth_required = config.get("auth_required", True)
    auth = None
    if auth_required:
        lines = (ROOT / "tmp/public-demo/access.txt").read_text(encoding="utf-8").splitlines()
        auth = httpx.BasicAuth(lines[0].removeprefix("Username: "),
                              lines[1].removeprefix("Password: "))
    before = json.loads(args.baseline.read_text(encoding="utf-8"))
    checks, requests = {}, []
    started = time.perf_counter()
    with httpx.Client(base_url=config["origin"], auth=auth, trust_env=False,
                      timeout=30, follow_redirects=False,
                      headers={"ngrok-skip-browser-warning": "1"}) as client:
        def get(path, **kwargs):
            response = client.get(path, **kwargs)
            requests.append({"path": path, "status": response.status_code})
            return response

        page = get("/static/project.html")
        checks["project_html_200"] = page.status_code == 200 and "让回答" in page.text
        links = Links()
        links.feed(page.text)
        checks["exactly_three_example_links"] = {s for s in links.links if "?example=" in s} == {
            "/?example=financial", "/?example=employee", "/?example=market"}
        checks["no_empty_placeholder_links"] = "#" not in links.links and "" not in links.links
        checks["all_section_anchors_exist"] = all(s[1:] in links.ids for s in links.links
                                                  if s.startswith("#"))
        checks["no_external_scripts_or_styles"] = all(s.startswith("/") for s in links.resources)
        for path in sorted(links.resources | {s for s in links.links if s.startswith("/")}):
            if path.startswith("/documents/"):
                response = get(path, headers={"Range": "bytes=0-1023"})
                checks["source_pdf_range"] = response.status_code == 206
            else:
                response = get(path)
                checks["route:" + path] = response.status_code == 200
        home = get("/")
        checks["home_project_materials_links"] = all(s in home.text for s in [
            "/static/project.html", "youju-project.pdf", "youju-source-review.zip"])
        checks["public_dead_api_docs_link_removed"] = 'href="/docs"' not in home.text
        js = get("/static/app.js")
        checks["workbench_three_categories"] = all(s in js.text for s in [
            "财务概览", "在职员工数量合计", "最新股价", "URLSearchParams", "typeof preset"])
        pdf = get("/static/downloads/youju-project.pdf")
        checks["published_pdf_matches_file"] = digest(pdf.content) == digest(
            (ROOT / "output/pdf/youju-project.pdf").read_bytes())
        reader = PdfReader(io.BytesIO(pdf.content))
        checks["pdf_four_pages_with_text"] = len(reader.pages) == 4 and all(
            len(p.extract_text()) > 300 for p in reader.pages)
        pdf_text = "\n".join(p.extract_text() for p in reader.pages)
        checks["pdf_metrics_and_boundaries"] = all(s in pdf_text for s in [
            "93.33%", "85/85", "403", "不含密钥", "607", "不是答案准确率",
            "50", "60", "20", "无需登录", "4,251", "294,916"])
        source = get("/static/downloads/youju-source-review.zip")
        checks["published_zip_matches_file"] = digest(source.content) == digest(
            (ROOT / "output/source/youju-source-review.zip").read_bytes())
        with zipfile.ZipFile(io.BytesIO(source.content)) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read("SOURCE_MANIFEST.json"))
            checks["zip_crc_and_manifest"] = archive.testzip() is None and all(
                digest(archive.read(name)) == record["sha256"]
                for name, record in manifest["files"].items())
            checks["zip_no_runtime_or_secrets_dirs"] = all(
                name != ".env" and Path(name).parts[0] not in {
                    "data", "eval", "tmp", ".tmp", ".cloud", ".git", ".venv", "output"}
                for name in names)
            checks["zip_review_scope_stated"] = "不能直接启动完整Demo" in archive.read(
                "README.md").decode()
        for path in ["/static/project.html", "/static/downloads/youju-project.pdf",
                     "/static/downloads/youju-source-review.zip"]:
            checks["access_mode:" + path] = get(path, auth=None).status_code == (
                401 if auth_required else 200)
    after_runtime = {name: digest((ROOT / name).read_bytes()) for name in before["runtime"]}
    after_qa = {name: digest((ROOT / name).read_bytes()) for name in before["qa"]}
    backend = {name: digest((ROOT / name).read_bytes()) for name in before["backend_hashes"]}
    checks["runtime_50_unchanged"] = after_runtime == before["runtime"]
    checks["qa_19_unchanged"] = after_qa == before["qa"]
    checks["all_backend_python_unchanged"] = backend == before["backend_hashes"]
    with sqlite3.connect((ROOT / "data/qa_sessions.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        turns = list(db.execute("SELECT * FROM turns ORDER BY id"))
    personal_hash = digest(json.dumps(turns, ensure_ascii=False, separators=(",", ":")).encode())
    checks["personal_turns_unchanged"] = personal_hash == before["personal_turns_sha256"]
    with sqlite3.connect((ROOT / "data/public_demo/sessions.sqlite").as_uri() + "?mode=ro",
                         uri=True) as db:
        rows = list(db.execute("SELECT * FROM turns ORDER BY id"))
    current = {str(row[0]): digest(json.dumps(row, ensure_ascii=False).encode()) for row in rows}
    checks["existing_public_turns_unchanged"] = all(
        current.get(key) == value for key, value in before["existing_public_turn_hashes"].items())
    report = {"version": "portfolio-sync-v2", "date": "2026-11-03",
              "role": "external_static_download_integrity_and_preservation_not_answer_accuracy",
              "origin": config["origin"], "checks": checks, "requests": requests,
              "passed": sum(checks.values()), "denominator": len(checks),
              "all_passed": all(checks.values()),
              "seconds": round(time.perf_counter() - started, 4),
              "model_calls": 0, "top_k": None, "browser_visual_verified": False,
              "browser_blocker": "Not part of this GET-only verifier",
              "pdf_pages_visually_reviewed": args.pdf_pages_visually_reviewed}
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps({"report": str(args.output), "passed": report["passed"],
                      "denominator": report["denominator"], "seconds": report["seconds"],
                      "failed": [k for k, v in checks.items() if not v]}, ensure_ascii=False))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
