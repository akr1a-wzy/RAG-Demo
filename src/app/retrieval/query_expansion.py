"""可审计的确定性财务查询扩展。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class RuleScope(BaseModel):
    document_types: list[str] = Field(default_factory=list)
    query_terms_any: list[str] = Field(default_factory=list)
    query_terms_excluded: list[str] = Field(default_factory=list)
    target_sections: list[str] = Field(default_factory=list)
    metric_kind: str


class RuleProvenance(BaseModel):
    source_type: str
    reference: str
    note: str


class QueryExpansionRule(BaseModel):
    rule_id: str
    canonical: str
    aliases: list[str]
    scope: RuleScope
    excluded: list[str] = Field(default_factory=list)
    provenance: list[RuleProvenance]
    review_status: Literal["candidate", "confirmed", "rejected"]
    version: int


class ExpansionResult(BaseModel):
    original_query: str
    expanded_query: str
    added_terms: list[str]
    matched_rule_ids: list[str]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


class QueryExpander:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise ValueError("查询扩展词典schema_version不受支持")
        self.rules = [QueryExpansionRule.model_validate(item) for item in raw["rules"]]
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("查询扩展规则ID重复")
        for rule in self.rules:
            terms = [rule.canonical, *rule.aliases]
            if rule.version < 1 or not rule.aliases or len(terms) != len(set(terms)):
                raise ValueError(f"查询扩展规则无效: {rule.rule_id}")
            if set(terms) & set(rule.excluded):
                raise ValueError(f"查询扩展包含与排除项冲突: {rule.rule_id}")
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()

    @staticmethod
    def _scope_matches(rule: QueryExpansionRule, query: str, document_type: str | None) -> bool:
        compact = _compact(query)
        scope = rule.scope
        if document_type and scope.document_types and document_type not in scope.document_types:
            return False
        if scope.query_terms_excluded and any(
            _compact(term) in compact for term in scope.query_terms_excluded
        ):
            return False
        return not scope.query_terms_any or any(
            _compact(term) in compact for term in scope.query_terms_any
        )

    def expand(self, query: str, document_type: str | None = None) -> ExpansionResult:
        compact = _compact(query)
        added: list[str] = []
        matched: list[str] = []
        for rule in self.rules:
            if rule.review_status != "confirmed" or not self._scope_matches(
                rule, query, document_type
            ):
                continue
            if any(_compact(term) in compact for term in rule.excluded):
                continue
            triggers = sorted([rule.canonical, *rule.aliases], key=len, reverse=True)
            if not any(_compact(term) in compact for term in triggers):
                continue
            matched.append(rule.rule_id)
            for term in triggers:
                if _compact(term) not in compact and term not in added:
                    added.append(term)
        expanded = query if not added else f"{query} {' '.join(added)}"
        return ExpansionResult(
            original_query=query,
            expanded_query=expanded,
            added_terms=added,
            matched_rule_ids=matched,
        )
