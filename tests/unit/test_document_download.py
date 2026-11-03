"""下载选档规则的离线测试，不依赖网络。"""

import importlib.util
from pathlib import Path
from urllib.error import HTTPError

import pymupdf
import pytest

SPEC = importlib.util.spec_from_file_location(
    "document_download", Path(__file__).resolve().parents[2] / "scripts/download_company_documents.py"
)
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


@pytest.mark.parametrize("title", ["中国平安2025年年度报告", "中国石油天然气股份有限公司2025年年报", "2025年度报告（修订版）", "宝钢股份2025年年度报告全文"])
def test_full_annual_report_aliases(title):
    assert download.is_annual(title)


@pytest.mark.parametrize("title", ["2025年年度报告摘要", "2025年年度报告（英文版）", "2025年年度报告（H股）", "平安银行2025年年度报告", "2026年半年度报告"])
def test_reject_incorrect_annual_documents(title):
    assert not download.is_annual(title)


def test_query_paginates(monkeypatch):
    calls = []
    def page(code, keyword, number=1):
        calls.append(number)
        return [{"page": number}], 3
    monkeypatch.setattr(download, "query_page", page)
    assert download.query("600036", "年度报告") == [{"page": 1}, {"page": 2}, {"page": 3}]
    assert calls == [1, 2, 3]


def test_reject_invalid_pdf_and_no_text():
    with pytest.raises(ValueError, match="不是PDF"):
        download.validate_pdf(b"<html>Error</html>")
    with pymupdf.open() as document:
        document.new_page()
        with pytest.raises(ValueError, match="可提取文字"):
            download.validate_pdf(document.tobytes())


def test_pdf_character_count():
    with pymupdf.open() as document:
        document.new_page().insert_text((30, 50), "Annual report financial statements " * 5)
        pages, characters = download.validate_pdf(document.tobytes())
    assert pages == 1
    assert characters >= 100


def test_404_is_not_retried(monkeypatch):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(1)
        raise HTTPError("https://www.cninfo.com.cn/", 404, "Not Found", None, None)
    monkeypatch.setattr(download.time, "sleep", lambda _: None)
    monkeypatch.setattr(download.urllib.request, "urlopen", unavailable)
    with pytest.raises(HTTPError):
        download.request_bytes("https://www.cninfo.com.cn/")
    assert len(calls) == 1
