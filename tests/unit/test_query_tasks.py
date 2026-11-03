from app.retrieval.query_tasks import is_financial_fact, plan_query_tasks


def test_comparison_tasks_do_not_include_other_company_names() -> None:
    tasks = plan_query_tasks(
        "按2025年营业收入排列美的集团、比亚迪、宁德时代，统一为亿元",
        ["000333", "002594", "300750"], "2025",
    )
    assert len(tasks) == 3
    assert tasks[0].stock_code == "000333"
    assert "美的集团" in tasks[0].query
    assert "比亚迪" not in tasks[0].query
    assert "宁德时代" not in tasks[0].query
    assert all(task.metric_terms == ("营业收入",) for task in tasks)


def test_multisource_tasks_split_financial_and_dividend_evidence() -> None:
    tasks = plan_query_tasks(
        "隆基绿能2025年营业收入和同比变化是多少，利润分配方案是什么？",
        ["601012"], "2025",
    )
    assert [task.kind for task in tasks] == ["financial", "dividend"]
    assert "利润分配" not in tasks[0].query
    assert "营业收入" not in tasks[1].query


def test_single_announcement_preserves_important_qualifiers() -> None:
    query = "美的集团2025年度A股实施时实际参与分配股份每10股含税派多少？"
    task = plan_query_tasks(query, ["000333"], "2025")[0]
    assert task.query == query
    assert not task.prefer_primary_financial


def test_analysis_and_quarterly_questions_do_not_prioritize_annual_summary() -> None:
    assert not is_financial_fact("营业收入下降的原因是什么？")
    assert not is_financial_fact("2025年第三季度营业收入是多少？")
    assert not is_financial_fact("母公司净利润是多少？")
    assert not is_financial_fact("比较各公司研发投入占营业收入的比例")
    assert is_financial_fact("2025年营业收入与归母净利润同比变化多少？")


def test_employee_comparison_is_separated_by_company() -> None:
    tasks = plan_query_tasks(
        "按在职员工数量排列美的集团、比亚迪、宁德时代",
        ["000333", "002594", "300750"], "2025",
    )
    assert len(tasks) == 3
    assert all(task.kind == "employees" for task in tasks)
    assert "比亚迪" not in tasks[0].query
    assert "员工" in tasks[0].query


def test_assets_and_dividend_payment_keep_two_topics_and_date_qualifier() -> None:
    tasks = plan_query_tasks(
        "总资产规模多少，A股每10股现金红利发放日是哪天？", ["600030"], "2025"
    )
    assert [task.kind for task in tasks] == ["financial", "dividend"]
    assert "总资产" in tasks[0].query
    assert "现金红利发放日" in tasks[1].query
    assert "A股" in tasks[1].query
    assert "每10股" in tasks[1].query


def test_employee_and_cash_dividend_are_separate_tasks() -> None:
    tasks = plan_query_tasks(
        "在职员工合计多少，全年每10股拟派发现金股利多少？", ["600050"], "2025"
    )
    assert [task.kind for task in tasks] == ["employees", "dividend"]
    assert "合计" in tasks[0].query
    assert "全年" in tasks[1].query
