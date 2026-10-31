"""离线决策策略（mock）：不调用任何模型，用规则复现同一条决策链。

边界必须说清楚：它验证的是「循环 + 工具 + 护栏」是否跑得通，
不证明真实模型的决策质量。真实模型走 llm.py，两者输出同一个 Action 契约。
"""

from __future__ import annotations

import re
from typing import Any

from .types import Action, State

FOCUS_RE = re.compile(r"D\d{2}")
ANAPHORA = ("那", "这", "它", "呢", "还有", "呢？")


class RulePolicy:
    name = "mock-rule-policy"

    # -- 查询构造 -----------------------------------------------------

    @staticmethod
    def _history_topic(state: State) -> tuple[str, list[str]]:
        """从对话历史恢复主题与文档焦点：这是「上下文追问」能成立的前提。"""
        focus: list[str] = []
        for msg in reversed(state.history):
            focus.extend(FOCUS_RE.findall(str(msg.get("content", ""))))
            if focus:
                break
        topic = ""
        for msg in reversed(state.history):
            if msg.get("role") == "user" and msg.get("content"):
                topic = str(msg["content"])
                break
        return topic, list(dict.fromkeys(focus))

    def build_query(self, state: State) -> str:
        q = state.question.strip()
        topic, focus = self._history_topic(state)
        needs_context = len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", q)) <= 8 or any(a in q for a in ANAPHORA)
        if topic and needs_context:
            return f"{topic} {q}".strip()
        return q

    @staticmethod
    def _rewrite(state: State) -> str:
        q = state.question
        for a, b in (("多久", "期限"), ("几天", "期限"), ("多长时间", "期限"),
                     ("可以", ""), ("能", ""), ("吗", ""), ("呢", ""), ("？", " "), ("?", " ")):
            q = q.replace(a, b)
        topic, _ = RulePolicy._history_topic(state)
        if topic and topic not in q:
            q = f"{topic} {q}"
        return " ".join(q.split())

    # -- 主决策 -------------------------------------------------------

    def decide(self, state: State, **_: Any) -> Action:
        # 0) 工具失败后先谈恢复，再谈作答
        if not state.last_ok:
            search_fails = [f for f in state.failures if f.get("tool") == "search_docs"]
            if state.last_action == "search_docs":
                if len(search_fails) == 1:
                    return Action(
                        "search_docs",
                        {"query": self._rewrite(state), "top_k": 8},
                        "上次检索因超时失败，改写查询后重试一次（不重发同一请求）",
                    )
                return Action(
                    "finish", {"status": "insufficient", "reason": "检索连续失败，无可用证据"},
                    "检索连续两次失败，没有证据就不作答，明确说明工具不可用",
                )
            return Action(
                "finish", {"status": "insufficient", "reason": f"{state.last_action} 失败，证据不完整"},
                "原文读取失败，只能用已有片段作答会缺证据，选择声明缺口",
            )

        # 1) 第一轮：先检索，不凭问题直接作答
        if not state.searches:
            return Action("search_docs", {"query": self.build_query(state), "top_k": 6},
                          "先检索资料库，确认是否存在可支撑回答的原文")

        # 2) 低置信：改写查询做补充检索（最多两轮）
        if state.low_confidence and len(state.searches) < 2:
            return Action("search_docs", {"query": self._rewrite(state), "top_k": 8},
                          "上一轮检索置信度不足，换关键词再检索一次")

        # 3) 发现取值冲突：读原文查证适用范围与效力关系
        pending = state.conflicts_needing_verification()
        if pending:
            c = pending[0]
            target = c.unverified[0]
            others = "、".join(d for d in c.unverified if d != target)
            return Action("read_doc", {"doc_id": target},
                          f"「{c.key}」在 {'、'.join(e.doc_id for e in c.entries)} 上取值不一致，"
                          f"读取 {target} 全文核对适用范围与效力声明"
                          + (f"（另有 {others} 待查）" if others else ""))

        # 4) 查证后仍无结论：列出冲突并请求确认，不擅自择一
        unresolved = state.unresolved_conflicts()
        if unresolved:
            c = unresolved[0]
            return Action(
                "ask_user",
                {"status": "conflict",
                 "question": f"「{c.key}」在 {'、'.join(e.doc_id for e in c.entries)} 中规定不一致，应以哪份为准？",
                 "options": [e.doc_id for e in c.entries]},
                "两份文档都没有替代或优先级声明，随机选一个等于编造，选择追问确认",
            )

        # 5) 冲突已按声明消解：给出结论并交代依据
        if state.resolved_conflicts():
            return Action("finish", {"status": "answered"},
                          "冲突已由文档自身的取代声明消解，可给出唯一结论并说明理由")

        # 6) 单文档命中、无冲突：直接作答
        last = state.last_search or {}
        if state.snippets and last.get("confident"):
            return Action("finish", {"status": "answered"},
                          "命中单份文档且无取值冲突，可直接引用原文作答")

        # 7) 两轮都低置信：区分「库里没有」与「有但不充分」
        if last.get("content_hits", 0) == 0:
            return Action("finish", {"status": "out_of_scope"},
                          "两轮检索都没有命中任何实质关键词，判断为资料外问题")
        return Action("finish", {"status": "insufficient"},
                      "有字面命中但置信度不足，声明证据不足以支撑结论")
