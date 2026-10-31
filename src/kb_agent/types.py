"""领域数据结构。刻意保持扁平：没有基类、没有插件协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------- 文档与检索

@dataclass
class Claim:
    """文档正文里的一行「键：值」断言，是冲突判定的最小单位。"""

    doc_id: str
    key: str
    value: str
    line: str
    heading: str


@dataclass
class Section:
    """一个标题小节，检索与引用的最小单位（引用原文从这里整段取）。"""

    doc_id: str
    heading: str
    text: str
    claims: list[Claim] = field(default_factory=list)


@dataclass
class Document:
    doc_id: str
    topic: str
    title: str
    version: str
    effective_date: str
    priority: int
    scope: str
    status: str
    owner: str
    supersedes: str
    text: str
    sections: list[Section] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)


@dataclass
class Snippet:
    """检索命中的片段。注意：携带 doc_id/version/生效日期，但不携带 priority/scope/supersedes。"""

    doc_id: str
    title: str
    version: str
    effective_date: str
    heading: str
    text: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "version": self.version,
            "effective_date": self.effective_date,
            "heading": self.heading,
            "score": round(self.score, 3),
            "text": self.text,
        }


@dataclass
class Conflict:
    """同一 claim key 下出现多个不一致取值时的处理结果。"""

    key: str
    entries: list[Claim]
    resolved: bool = False
    winner: str = ""
    basis: str = ""
    verified: list[str] = field(default_factory=list)  # 已 read_doc 全文查证过的 doc_id
    unverified: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "values": {c.doc_id: c.value for c in self.entries},
            "resolved": self.resolved,
            "winner": self.winner,
            "basis": self.basis,
            "verified": self.verified,
            "unverified": self.unverified,
        }


# ---------------------------------------------------------------- 动作与观察

@dataclass
class Action:
    name: str  # search_docs | read_doc | ask_user | finish
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class Observation:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class Citation:
    doc_id: str
    version: str
    quote: str

    def as_dict(self) -> dict[str, str]:
        return {"doc_id": self.doc_id, "version": self.version, "quote": self.quote}


@dataclass
class Answer:
    """一次问答的交付结果：回答 + 引用 + 资料缺口 + 任务状态。"""

    status: str  # answered | conflict | insufficient | out_of_scope | needs_clarification | unfinished
    answer: str
    citations: list[Citation] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    basis: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "citations": [c.as_dict() for c in self.citations],
            "gaps": self.gaps,
            "next_actions": self.next_actions,
            "basis": self.basis,
        }


@dataclass
class State:
    """循环里流转的全部上下文。决策器只看这个对象，不碰工具。"""

    question: str
    history: list[dict[str, Any]] = field(default_factory=list)
    snippets: list[Snippet] = field(default_factory=list)
    searches: list[dict[str, Any]] = field(default_factory=list)
    opened: dict[str, Document] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    last_action: str = ""
    last_ok: bool = True
    retried: bool = False
    focus_doc_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- 派生判断，决策器与护栏共用 -------------------------------------

    @property
    def last_search(self) -> dict[str, Any] | None:
        return self.searches[-1] if self.searches else None

    @property
    def low_confidence(self) -> bool:
        last = self.last_search
        return bool(last) and not last.get("confident", False)

    @property
    def seen_doc_ids(self) -> list[str]:
        ids: list[str] = []
        for s in self.snippets:
            if s.doc_id not in ids:
                ids.append(s.doc_id)
        return ids

    def conflicts_needing_verification(self) -> list[Conflict]:
        """未消解、且参与文档还没被 read_doc 查证过的冲突。"""
        return [c for c in self.conflicts if not c.resolved and c.unverified]

    def unresolved_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if not c.resolved]

    def resolved_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.resolved]
