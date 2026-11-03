from types import SimpleNamespace

import pytest

from app.qa.workflow import DocumentQA


def hit(text, page, code="601088"):
    return SimpleNamespace(text=text, same_page_supporting_chunks=[], document_type="annual_report",
                           document_id=code + "-2025-annual", stock_code=code,
                           stock_name="中国神华",
                           report_period="2025", title="年报", page_start=page, source_url="local",
                           section="员工情况", table_layout_review_required=False)


class Runtime:
    def __init__(self, broken=False, complete=False):
        self.calls, self.broken, self.complete = [], broken, complete

    def metadata(self):
        return {"device": "cuda"}

    def retrieve_resolved(self, resolved):
        self.calls.append(resolved)
        if len(self.calls) == 1 and not self.complete:
            return [hit("无目标员工事实", 12)], []
        if self.broken:
            raise RuntimeError("reranker-failed")
        return [hit("截至报告期末，本集团共有员工100人。", 84)], []


class NoGeneration:
    configured = False


def qa(runtime):
    return DocumentQA(lambda: runtime, NoGeneration(), supported_codes={"601088"},
                      supported_periods={"2025"})


def test_missing_employee_fact_uses_generic_query_and_same_runtime():
    runtime = Runtime()
    result = qa(runtime).ask("中国神华2025年在职员工数量合计多少人？")
    assert len(runtime.calls) == 2
    assert "本集团共有员工" in runtime.calls[1]["query"]
    assert runtime.calls[1]["stock_codes"] == ["601088"]
    assert result["sources"][0]["physical_page"] == 84
    assert result["fact_selection"]["status"] == "ready"
    ids = [s["source_id"] for s in result["sources"]]
    assert len(ids) == len(set(ids))
    assert all(f["source_id"] == s["source_id"] for s in result["sources"] for f in s["facts"])


def test_complete_primary_answer_does_not_repeat_retrieval():
    runtime = Runtime(complete=True)
    assert qa(runtime).ask("中国神华2025年员工人数多少？")["fact_selection"]["status"] == "ready"
    assert len(runtime.calls) == 1


def test_supplement_failure_does_not_silently_fall_back_to_incomplete_evidence():
    runtime = Runtime(broken=True)
    with pytest.raises(RuntimeError, match="reranker-failed"):
        qa(runtime).ask("中国神华2025年员工人数多少？")
