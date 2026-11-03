"""问答层的有限消歧；不改已发布检索词表及其冻结版本。"""

from app.qa.query_understanding import resolve_entities


def resolve_document_query(question: str, history: list[dict] | None = None) -> dict:
    return resolve_entities(question, history)
