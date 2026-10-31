"""业务工具：文档检索、片段读取，外加可控的故障注入。

工具签名固定为 (Toolbox.call(action) -> Observation)，决策器不直接碰它。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .retrieval import Index, expand_query
from .types import Action, Observation


class Toolbox:
    def __init__(self, index: Index, fault_plan: dict[str, list[Any]] | None = None):
        self.index = index
        self.fault_plan = dict(fault_plan or {})
        self.calls: Counter = Counter()

    # -- 故障注入 -----------------------------------------------------

    def _fails(self, name: str) -> bool:
        plan = self.fault_plan.get(name)
        if not plan:
            return False
        n = self.calls[name]
        return "*" in plan or n in plan

    # -- 分发 ---------------------------------------------------------

    def call(self, action: Action) -> Observation:
        self.calls[action.name] += 1
        if self._fails(action.name):
            return self._fault(action.name)
        if action.name == "search_docs":
            return self.search_docs(**action.args)
        if action.name == "read_doc":
            return self.read_doc(**action.args)
        return Observation(ok=False, summary=f"未知工具：{action.name}", error="unknown_tool")

    def _fault(self, name: str) -> Observation:
        # 故障是「注入」的，但对外表现得像真实基础设施故障：调用方只能看到失败与错误码。
        if name == "search_docs":
            return Observation(
                ok=False,
                summary="检索服务超时：上游 3000ms 未返回",
                error="TIMEOUT",
                data={"tool": name, "attempt": self.calls[name], "injected_fault": True},
            )
        return Observation(
            ok=False,
            summary="文档服务不可用：读取原文返回 503",
            error="UNAVAILABLE",
            data={"tool": name, "attempt": self.calls[name], "injected_fault": True},
        )

    # -- 工具实现 -----------------------------------------------------

    def search_docs(self, query: str = "", top_k: int = 5,
                    doc_ids: list[str] | None = None) -> Observation:
        if not query.strip():
            return Observation(ok=False, summary="缺少检索词", error="BAD_ARGS")
        expanded = expand_query(query, self.index.synonyms)
        hits = self.index.search(query, top_k=int(top_k or 5), doc_ids=doc_ids,
                                 expanded=expanded)
        confident = self.index.is_confident(query, hits)
        content_hits = self.index.content_hits(query, hits)
        if not hits:
            summary = f"检索「{query}」无命中片段"
        else:
            ids = "、".join(dict.fromkeys(h.doc_id for h in hits))
            summary = (f"检索「{query}」命中 {len(hits)} 个片段，涉及 {ids}；"
                       f"最高分 {hits[0].score:.2f}，置信={'是' if confident else '否'}")
        return Observation(
            ok=True,
            summary=summary,
            data={
                "query": query,
                "expanded_query": expanded,
                "top_score": round(hits[0].score, 3) if hits else 0.0,
                "confident": confident,
                "content_hits": content_hits,
                "hits": [h.as_dict() for h in hits],
            },
        )

    def read_doc(self, doc_id: str = "") -> Observation:
        doc = self.index.docs.get(doc_id)
        if not doc:
            return Observation(ok=False, summary=f"文档不存在：{doc_id}", error="NOT_FOUND")
        sections = [
            {"heading": s.heading, "text": s.text,
             "claims": [{"key": c.key, "value": c.value} for c in s.claims]}
            for s in doc.sections if s.text.strip()
        ]
        return Observation(
            ok=True,
            summary=(f"读取 {doc.doc_id}《{doc.title}》{doc.version}（生效 {doc.effective_date}，"
                     f"优先级 {doc.priority}，适用范围「{doc.scope}」，替代 {doc.supersedes}）"
                     f"，共 {len(sections)} 个小节"),
            data={
                "doc_id": doc.doc_id,
                "title": doc.title,
                "version": doc.version,
                "effective_date": doc.effective_date,
                "priority": doc.priority,
                "scope": doc.scope,
                "supersedes": doc.supersedes,
                "owner": doc.owner,
                "sections": sections,
                "claims": [{"key": c.key, "value": c.value, "heading": c.heading} for c in doc.claims],
            },
        )
