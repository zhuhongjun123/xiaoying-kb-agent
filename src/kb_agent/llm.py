"""真实模型通道：OpenAI 兼容 /chat/completions，只用标准库。

输出契约与 mock 完全一致（Action JSON），因此循环、护栏、trace 格式完全复用。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date
from typing import Any

from .policy import RulePolicy
from .types import Action, State

ALLOWED = {"search_docs", "read_doc", "ask_user", "finish"}

SYSTEM = """你是企业知识库问答 Agent。你只能依据工具返回的原文回答，不能使用先验常识。

可用动作（每次只输出一个 JSON）：
1. {"action":"search_docs","args":{"query":"...","top_k":6},"reason":"..."}
   在资料库里按关键词检索片段。返回 doc_id、版本、生效日期、片段原文与分数。
2. {"action":"read_doc","args":{"doc_id":"D01"},"reason":"..."}
   读取某份文档全文，返回适用范围(scope)、优先级(priority)、替代关系(supersedes)与全部条款。
3. {"action":"ask_user","args":{"status":"conflict|needs_clarification","question":"...","options":["D01","D02"]},"reason":"..."}
   资料自相矛盾且无法判定时，向用户追问，不得自行择一。
4. {"action":"finish","args":{"status":"answered|conflict|insufficient|out_of_scope","answer":"...","citations":[{"doc_id":"D01","quote":"原文片段"}],"gaps":["..."],"next_actions":["..."],"basis":"..."},"reason":"..."}
   结束并交付答案。

硬性规则：
- 回答必须来自工具返回的原文。quote 必须是**某一段原文的连续片段**：不能跨条目拼接，
  不能用「/」「；」等分隔符把多行连成一条。需要多行支撑就写多条 citations，每条对应一段连续原文。
- answer 里出现的每个数字，都必须能在 citations 的 quote 里找到。
  不要在答案里写引用原文没有的数字，包括由生效日期推算出的日期、由数值算出的比例。
  需要说明时间范围时，直接引用文档的生效日期（它属于可核验元数据），不要自己换算。
- 同一问题在不同文档中取值不一致时：先 read_doc 查证适用范围、优先级、替代关系；能消解就说明依据后作答，不能消解就 ask_user 并列出双方原文。
- 检索置信度低（confident=false）时，先改写查询补充检索一次；两轮仍无实质命中，判定 out_of_scope。
- 工具报错时不要重发同一请求，改写查询重试一次；仍失败则 finish(insufficient)，说明工具不可用。
- 不要编造文档号、条款号或数值。宁可不答。

只输出 JSON，不要解释、不要 markdown 代码块。"""


class LLMError(RuntimeError):
    pass


class LLMDecider:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 60.0, temperature: float = 0.0):
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        self.timeout = timeout
        self.temperature = temperature
        self.name = f"llm:{self.model}"
        self.calls = 0
        self.fallbacks = 0
        if not self.api_key:
            raise LLMError("缺少 OPENAI_API_KEY；离线请用 --provider mock")

    # -- HTTP ---------------------------------------------------------

    def _chat(self, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # 错误信息里不回显 key
            raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"网络不可达: {e.reason}") from e
        try:
            return payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"响应结构异常: {str(payload)[:200]}") from e

    # -- 状态摘要 -----------------------------------------------------

    @staticmethod
    def _render(state: State, max_steps: int, max_tool_calls: int) -> str:
        lines: list[str] = [f"用户问题：{state.question}"]
        lines.append(f"当前日期：{date.today().isoformat()}（判断文档是否已生效时以此为准）")
        if state.history:
            hist = "\n".join(f"- {m.get('role')}: {str(m.get('content'))[:120]}"
                             for m in state.history[-6:])
            lines.append(f"对话历史：\n{hist}")
        lines.append(f"预算：已用 {state.steps}/{max_steps} 步，{state.tool_calls}/{max_tool_calls} 次工具调用")

        if state.searches:
            lines.append("已执行的检索：")
            for s in state.searches:
                lines.append(f"- 「{s['query']}」→ 最高分 {s['top_score']}，置信 {s['confident']}，"
                             f"命中 {s['doc_ids']}")
        if state.snippets:
            lines.append("当前可用片段（quote 只能逐条取自下面这些原文行）：")
            for s in state.snippets[:8]:
                lines.append(f"- [{s.doc_id} {s.version} 生效{s.effective_date} 分数{s.score:.2f}] "
                             f"小节「{s.heading}」原文：")
                for ln in [x for x in s.text.splitlines() if x.strip()][:12]:
                    lines.append(f"    {ln.strip()}")
        if state.opened:
            lines.append("已读取全文的文档（quote 可取自下列原文行）：")
            for doc in state.opened.values():
                lines.append(f"- {doc.doc_id}《{doc.title}》{doc.version} 生效{doc.effective_date} "
                             f"优先级{doc.priority} 适用范围「{doc.scope}」替代「{doc.supersedes}」")
                for sec in doc.sections[:6]:
                    body = [x.strip() for x in sec.text.splitlines() if x.strip()]
                    if not body:
                        continue
                    lines.append(f"  §{sec.heading}")
                    for ln in body[:8]:
                        lines.append(f"    {ln}")
        if state.conflicts:
            lines.append("已检出的取值冲突：")
            for c in state.conflicts:
                vals = "、".join(f"{e.doc_id}={e.value}" for e in c.entries)
                lines.append(f"- 「{c.key}」{vals}；已查证 {c.verified or '无'}；"
                             f"待查证 {c.unverified or '无'}；"
                             f"{'已消解：' + c.basis if c.resolved else '未消解'}")
        if state.failures:
            lines.append("已发生的工具失败：")
            for f in state.failures:
                lines.append(f"- {f['tool']} → {f['error']} {f['summary']}")
        lines.append("请输出下一个动作的 JSON。")
        return "\n".join(lines)

    # -- 解析 ---------------------------------------------------------

    @staticmethod
    def _parse(raw: str) -> Action | None:
        text = raw.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        name = obj.get("action")
        if name not in ALLOWED:
            return None
        args = obj.get("args") or {}
        if not isinstance(args, dict):
            return None
        return Action(name=name, args=args, reason=str(obj.get("reason") or ""))

    # -- 决策 ---------------------------------------------------------

    def decide(self, state: State, max_steps: int = 8, max_tool_calls: int = 6) -> Action:
        prompt = self._render(state, max_steps, max_tool_calls)
        last_err = ""
        for attempt in range(2):
            try:
                raw = self._chat(prompt if attempt == 0 else
                                 prompt + f"\n\n上一次输出不是合法的动作 JSON（{last_err}），只输出一个 JSON 对象。")
            except LLMError as e:
                last_err = str(e)
                break
            action = self._parse(raw)
            if action:
                return action
            last_err = "无法解析为动作 JSON"
        # 模型不可用时回落规则策略，并在 reason 里留痕，trace 可查
        self.fallbacks += 1
        fallback = RulePolicy().decide(state)
        fallback.reason = f"[模型不可用，回落规则策略：{last_err}] " + fallback.reason
        return fallback
