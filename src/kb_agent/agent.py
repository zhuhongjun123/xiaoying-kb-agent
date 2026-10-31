"""决策循环：模型选动作 → 执行工具 → 观察结果 → 再决策或结束。

循环本身不认识业务：它只保证四件事——有上限、有记录、有护栏、有未完成态。
"""

from __future__ import annotations

from typing import Any

from . import guards
from .policy import RulePolicy
from .retrieval import Index
from .tools import Toolbox
from .types import Action, Answer, Citation, Snippet, State

TERMINAL = ("finish", "ask_user")


class Agent:
    def __init__(self, index: Index, decider: Any | None = None):
        self.index = index
        self.decider = decider or RulePolicy()

    # -- 状态更新 -----------------------------------------------------

    def _merge_hits(self, state: State, hits: list[dict[str, Any]]) -> None:
        known = {(s.doc_id, s.heading) for s in state.snippets}
        for h in hits:
            if (h["doc_id"], h["heading"]) in known:
                continue
            state.snippets.append(Snippet(**{k: h[k] for k in
                                             ("doc_id", "title", "version", "effective_date",
                                              "heading", "text", "score")}))
        state.snippets.sort(key=lambda s: -s.score)

    def _ingest(self, state: State, action: Action, obs) -> None:
        state.last_action = action.name
        state.last_ok = obs.ok
        if not obs.ok:
            state.failures.append({"tool": action.name, "error": obs.error, "summary": obs.summary})
            state.notes.append(f"{action.name} 失败：{obs.summary}")
            return
        if action.name == "search_docs":
            state.searches.append({
                "query": obs.data["query"],
                "expanded_query": obs.data["expanded_query"],
                "top_score": obs.data["top_score"],
                "confident": obs.data["confident"],
                "content_hits": obs.data["content_hits"],
                "doc_ids": list(dict.fromkeys(h["doc_id"] for h in obs.data["hits"])),
            })
            self._merge_hits(state, obs.data["hits"])
        elif action.name == "read_doc":
            doc = self.index.docs.get(obs.data["doc_id"])
            if doc:
                state.opened[doc.doc_id] = doc

    # -- 收尾 ---------------------------------------------------------

    def _finalize(self, action: Action, state: State) -> tuple[Answer, list[str]]:
        status = str(action.args.get("status") or "answered")
        docs = self.index.docs

        raw_answer = action.args.get("answer")
        if raw_answer:
            citations: list[Citation] = []
            for c in action.args.get("citations") or []:
                doc = docs.get(str(c.get("doc_id", "")))
                if doc:
                    citations.append(Citation(doc_id=doc.doc_id, version=doc.version,
                                              quote=str(c.get("quote", ""))))
            answer = Answer(
                status=status, answer=str(raw_answer), citations=citations,
                gaps=[str(g) for g in action.args.get("gaps") or []],
                next_actions=[str(n) for n in action.args.get("next_actions") or []],
                basis=str(action.args.get("basis") or ""),
            )
        else:
            answer = self._compose(status, action, state)

        problems = guards.verify_answer(answer, docs)
        if problems and answer.status in ("answered", "conflict"):
            # 关键缺陷防线：引用对不上原文或数字无支撑，降级而不是放行
            answer = Answer(
                status="insufficient",
                answer="已生成草稿，但引用无法与原文核对，故不作为答案输出："
                       + "；".join(problems[:3]) + "。",
                gaps=["回答未通过引用可核验性检查"] + problems,
                next_actions=["缩小问题范围或指定文档 ID 后重试"],
            )
        return answer, problems

    def _compose(self, status: str, action: Action, state: State) -> Answer:
        if status == "answered":
            resolved = state.resolved_conflicts()
            if resolved:
                return guards.compose_resolved(resolved[0], state.snippets, self.index.docs)
            return guards.compose_answered(state.snippets, self.index.docs)
        if status == "conflict":
            unresolved = state.unresolved_conflicts()
            if not unresolved:
                return guards.compose_insufficient(["未找到可核对的冲突证据"], self.index.docs,
                                                   state.snippets)
            answer = guards.compose_conflict(unresolved[0], self.index.docs)
            extra = action.args.get("question")
            if extra:
                answer.answer += f"\n\n需要你确认：{extra}"
            return answer
        if status == "out_of_scope":
            return guards.compose_out_of_scope(state.question, self.index.docs)
        reason = str(action.args.get("reason") or "证据不足")
        return guards.compose_insufficient([reason], self.index.docs, state.snippets)

    def _unfinished(self, state: State, why: str) -> Answer:
        seen = "、".join(state.seen_doc_ids) or "无"
        return Answer(
            status="unfinished",
            answer=f"任务未完成：{why}（已用 {state.steps} 步 / {state.tool_calls} 次工具调用，"
                   f"已检索到 {seen}）。为避免在证据不完整时给出结论，这里不输出答案。",
            gaps=[why, "证据链尚未闭合"],
            next_actions=["提高上限后重试", "或缩小问题范围"],
        )

    # -- 主循环 -------------------------------------------------------

    def run(self, question: str, history: list[dict[str, Any]] | None = None,
            faults: dict[str, list[Any]] | None = None, max_steps: int = 8,
            max_tool_calls: int = 6, case_id: str = "", title: str = "") -> dict[str, Any]:
        state = State(question=question, history=list(history or []))
        toolbox = Toolbox(self.index, faults)
        trace: dict[str, Any] = {
            "case_id": case_id,
            "title": title,
            "provider": getattr(self.decider, "name", "unknown"),
            "question": question,
            "history": state.history,
            "budget": {"max_steps": max_steps, "max_tool_calls": max_tool_calls},
            "steps": [],
            "result": None,
            "citation_check": {"ok": True, "problems": []},
        }

        while True:
            state.conflicts = guards.detect_conflicts(state.snippets, state.opened, self.index.docs)

            if state.steps >= max_steps:
                answer = self._unfinished(state, f"达到步数上限 {max_steps}")
                break

            action = self.decider.decide(state, max_steps=max_steps, max_tool_calls=max_tool_calls)
            state.steps += 1
            step: dict[str, Any] = {
                "step": state.steps,
                "action": action.name,
                "args": action.args,
                "reason": action.reason,
            }

            if action.name in TERMINAL:
                answer, problems = self._finalize(action, state)
                step["observation"] = {"ok": True, "summary": "终止动作：不调用工具"}
                trace["citation_check"] = {"ok": not problems, "problems": problems}
                trace["steps"].append(step)
                break

            if state.tool_calls >= max_tool_calls:
                answer = self._unfinished(state, f"达到工具调用上限 {max_tool_calls}")
                step["observation"] = {"ok": False, "summary": "预算已耗尽，动作未执行",
                                       "error": "BUDGET_EXCEEDED", "data": {}}
                trace["steps"].append(step)
                break

            obs = toolbox.call(action)
            state.tool_calls += 1  # 失败调用同样计入额度：它消耗的是真实配额
            step["observation"] = {
                "ok": obs.ok,
                "summary": obs.summary,
                "error": obs.error,
                "data": obs.data,
            }
            trace["steps"].append(step)
            self._ingest(state, action, obs)

        trace["budget"]["steps_used"] = state.steps
        trace["budget"]["tool_calls_used"] = state.tool_calls
        trace["result"] = answer.as_dict()
        return trace
