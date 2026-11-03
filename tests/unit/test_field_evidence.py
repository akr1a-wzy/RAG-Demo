from app.retrieval.field_evidence import field_evidence_kind, is_direct_field_evidence


def test_employees_require_total_count_not_payroll_or_subsidiary_count() -> None:
    assert is_direct_field_evidence(
        "报告期末在职员工的数量合计（人） 120,000", "员工情况", "employees", "annual_report"
    )
    assert not is_direct_field_evidence(
        "主要子公司在职员工的数量（人） 100,000", "员工情况", "employees", "annual_report"
    )
    assert not is_direct_field_evidence(
        "员工薪酬合计 123,456", "应付职工薪酬", "employees", "annual_report"
    )


def test_dates_require_requested_label_and_actual_date() -> None:
    assert is_direct_field_evidence(
        "股权登记日 现金红利发放日\nＡ股 2026/7/16 2026/7/17",
        "三、相关日期", "payment_date", "announcement",
    )
    assert not is_direct_field_evidence(
        "股权登记日后按相关规定领取红利", "实施办法", "record_date", "announcement"
    )
    assert not is_direct_field_evidence(
        "公告日期2026/7/17", "", "payment_date", "announcement"
    )
    assert not is_direct_field_evidence(
        "于2025年12月31日可分配利润为100亿元，拟以股权登记日的总股本为基数",
        "利润分配方案", "record_date", "announcement",
    )
    assert is_direct_field_evidence(
        "股权登记日即2026年6月16日（星期二）", "分配方案", "record_date", "announcement"
    )


def test_assets_reject_subsidiary_sections_and_non_numeric_cross_references() -> None:
    assert is_direct_field_evidence(
        "总资产 123,456 110,000", "资产负债及权益", "assets", "annual_report"
    )
    assert not is_direct_field_evidence(
        "总资产 123,456", "财务公司主要财务指标", "assets", "annual_report"
    )
    assert not is_direct_field_evidence("总资产详见附注", "", "assets", "annual_report")


def test_policy_does_not_apply_to_explanations_or_other_employee_scopes() -> None:
    assert field_evidence_kind("在职员工数量合计多少") == "employees"
    assert field_evidence_kind("母公司员工数量多少") is None
    assert field_evidence_kind("总资产增长的原因") is None
