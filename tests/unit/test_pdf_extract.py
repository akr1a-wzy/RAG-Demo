from app.retrieval.pdf_extract import clean_page_text


def test_clean_page_text_keeps_line_structure() -> None:
    raw = " 标题  \n\n\n 第一列\t第二列 \n数值  100\u00a0元\x00 "

    assert clean_page_text(raw) == "标题\n\n第一列 第二列\n数值 100 元"


def test_clean_page_text_handles_empty_page() -> None:
    assert clean_page_text(" \n\t\n") == ""

