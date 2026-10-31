"""确定性护栏：冲突判定与消解、引用可核验性、答案组装。

设计原则：凡是「能否算数」的判断都交给代码，模型只负责选动作和写叙述。
这样替换文档内容时结论自动跟着变，不依赖模型自由心证。
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .types import Answer, Citation, Claim, Conflict, Document, Snippet

WS_RE = re.compile(r"\s+")
BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# 这些形态里的数字是指称而非断言，不参与「数字必须有引用支撑」的校验
NUM_EXEMPT = [
    re.compile(r"第\s*\d+(?:\.\d+)*\s*条"),
    re.compile(r"[vV]\d+(?:\.\d+)*"),
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"[Dd]\d{2,}"),
]


def norm(text: str) -> str:
    """比对用归一化：去掉行首列表符号（格式不是内容）与全部空白。"""
    return WS_RE.sub("", BULLET_RE.sub("", text or ""))


# ------------------------------------------------------------------ 冲突

def _claims_of(doc: Document, snippets: list[Snippet], opened: dict[str, Document]) -> list[Claim]:
    if doc.doc_id in opened:
        return list(doc.claims)
    headings = {s.heading for s in snippets if s.doc_id == doc.doc_id}
    return [c for c in doc.claims if c.heading in headings]


def detect_conflicts(snippets: list[Snippet], opened: dict[str, Document],
                     docs: dict[str, Document], min_ratio: float = 0.5,
                     today: date | None = None) -> list[Conflict]:
    """在同一「证据集」内、同一主题下、同一 claim key 出现不同取值 → 候选冲突。

    两个必要的限定：
    1. 只在证据集内比对（分数不低于最高分的 min_ratio），否则检索出来的无关片段会冒充冲突；
    2. 只在「最强命中所在主题」内比对：跨主题的同一 key（如「起算时点」在报销与调休里都有）
       不是冲突；同理，问住宿时不该把报销主题的内部矛盾翻出来。
    """
    if not snippets:
        return []
    top = max(snippets, key=lambda s: s.score)
    top_doc = docs.get(top.doc_id)
    focus_topic = top_doc.topic if top_doc else None
    evidence = [s for s in snippets
                if s.score >= top.score * min_ratio
                and (focus_topic is None or (docs.get(s.doc_id) and docs[s.doc_id].topic == focus_topic))]

    groups: dict[tuple[str, str], list[Claim]] = defaultdict(list)
    for doc_id in dict.fromkeys(s.doc_id for s in evidence):
        doc = docs.get(doc_id)
        if not doc:
            continue
        for c in _claims_of(doc, evidence, opened):
            groups[(doc.topic, c.key)].append(c)

    conflicts: list[Conflict] = []
    for (_, key), entries in groups.items():
        values = {e.doc_id: e.value for e in entries}
        if len(set(values.values())) < 2:
            continue
        verified = [d for d in values if d in opened]
        unverified = [d for d in values if d not in opened]
        conflict = Conflict(key=key, entries=entries, verified=verified, unverified=unverified)
        if not unverified:
            resolved, winner, basis = _resolve(values, opened, today or date.today())
            conflict.resolved, conflict.winner, conflict.basis = resolved, winner, basis
        else:
            conflict.basis = f"已检索到 {'、'.join(values)} 的取值不一致，尚未读取全文查证效力关系"
        conflicts.append(conflict)
    return conflicts


def _is_effective(doc: Document, today: date) -> bool:
    """生效日期未到 = 还不算数。日期解析失败时按已生效处理（不因格式问题阻塞判定）。"""
    try:
        return date.fromisoformat(doc.effective_date) <= today
    except (ValueError, TypeError):
        return True


def _resolve(values: dict[str, str], opened: dict[str, Document],
             today: date) -> tuple[bool, str, str]:
    """消解顺序：声明取代（且已生效）> 显式优先级 > 无法消解。"""
    upper = {d.upper(): d for d in values}

    for doc_id, doc in opened.items():
        if doc_id not in values:
            continue
        target = (doc.supersedes or "").strip().upper()
        if target and target != "NONE" and target in upper and upper[target] != doc_id:
            if not _is_effective(doc, today):
                return False, "", (
                    f"{doc_id} 虽然声明取代 {upper[target]}，但其生效日期 {doc.effective_date} "
                    f"尚未到达（今天 {today.isoformat()}），现在还不能据此消解"
                )
            remaining = {d: v for d, v in values.items() if d != upper[target]}
            if len(set(remaining.values())) == 1:
                return True, next(iter(remaining)), (
                    f"{doc_id}（{doc.version}，{doc.effective_date} 生效）声明取代 "
                    f"{upper[target]}，被取代文档的对应条款不再适用"
                )

    prios = {d: opened[d].priority for d in values
             if d in opened and opened[d].priority and _is_effective(opened[d], today)}
    if len(prios) == len(values) and len(set(prios.values())) == len(prios) and len(prios) > 1:
        top = max(prios, key=lambda d: prios[d])
        return True, top, f"参与文档均显式声明了效力优先级，取优先级最高者（{top}，priority={prios[top]}）"

    scopes = {d: opened[d].scope for d in values if d in opened}
    note = ""
    if len(set(scopes.values())) > 1:
        note = "；适用范围表述不同（" + "；".join(f"{d}：{s}" for d, s in scopes.items()) + "），但均覆盖本次提问人群"
    docs = "、".join(values)
    return False, "", (
        f"{docs} 有效期重叠、均未声明替代关系，且未给出可用于区分的适用范围或优先级" + note
    )


# ------------------------------------------------------------------ 引用核验

def _numbers(text: str) -> list[str]:
    for pat in NUM_EXEMPT:
        text = pat.sub(" ", text)
    return NUM_RE.findall(text)


def verify_answer(answer: Answer, docs: dict[str, Document]) -> list[str]:
    """引用必须是原文子串；答案里每个数字必须能在引用原文里找到。"""
    problems: list[str] = []
    quoted = ""
    for c in answer.citations:
        doc = docs.get(c.doc_id)
        if not doc:
            problems.append(f"引用了不存在的文档 {c.doc_id}")
            continue
        if norm(c.quote) not in norm(doc.text):
            problems.append(f"引用原文在 {c.doc_id} 中找不到：{c.quote[:40]}…")
        # 文档标题/版本/生效日期属于同一份文档的可核验元数据，允许在叙述里出现
        quoted += norm(c.quote) + norm(doc.title) + norm(doc.version) + norm(doc.effective_date)
    if not answer.citations and answer.status == "answered":
        problems.append("answered 状态缺少引用")
    for num in _numbers(answer.answer):
        if num not in quoted:
            problems.append(f"答案中的数字「{num}」没有任何引用原文支撑")
    return problems


# ------------------------------------------------------------------ 答案组装

def _citation(doc: Document, quote: str) -> Citation:
    return Citation(doc_id=doc.doc_id, version=doc.version, quote=quote.strip())


def _section_of(doc: Document, heading: str):
    for s in doc.sections:
        if s.heading == heading:
            return s
    return None


def _claim_lines(doc: Document, heading: str) -> list[str]:
    sec = _section_of(doc, heading)
    if not sec:
        return []
    return [f"- {c.key}：{c.value}" for c in sec.claims] or [sec.text.strip()]


def _supersede_section(doc: Document, target: str):
    for s in doc.sections:
        if target and target.upper() in s.text.upper() and ("替代" in s.text or "取代" in s.text or "修订" in s.text):
            return s
    return None


def compose_answered(snippets: list[Snippet], docs: dict[str, Document]) -> Answer:
    """抽取式作答：直接引用命中片段所属小节，答案条目从原文 claim 复制，天然不会编造。"""
    if not snippets:
        return compose_insufficient(["检索无命中片段"], docs)
    top = snippets[0]
    doc = docs[top.doc_id]
    sec = _section_of(doc, top.heading)
    lines = _claim_lines(doc, top.heading)
    citations = [_citation(doc, sec.text if sec else top.text)]

    facts = "\n".join(lines)
    answer = (f"依据《{doc.title}》（{doc.doc_id}，{doc.version}，{doc.effective_date} 生效）：\n"
              + (facts or (sec.text.strip() if sec else top.text.strip())))
    gaps: list[str] = []
    if doc.scope:
        gaps.append(f"本条仅适用于「{doc.scope}」，若提问者不属于该范围需另行确认")
    return Answer(status="answered", answer=answer, citations=citations, gaps=gaps)


def compose_resolved(conflict: Conflict, snippets: list[Snippet], docs: dict[str, Document]) -> Answer:
    winner = docs[conflict.winner]
    line = next((e for e in conflict.entries if e.doc_id == conflict.winner), None)
    heading = line.heading if line else winner.sections[0].heading
    sec = _section_of(winner, heading)
    citations = [_citation(winner, sec.text if sec else winner.text)]

    basis_sec = _supersede_section(winner, winner.supersedes)
    if basis_sec:
        citations.append(_citation(winner, basis_sec.text))

    losers = [e.doc_id for e in conflict.entries if e.doc_id != conflict.winner]
    answer = (f"依据《{winner.title}》（{winner.doc_id}，{winner.version}，{winner.effective_date} 生效）：\n"
              + "\n".join(_claim_lines(winner, heading))
              + f"\n\n为何以此为准：{conflict.basis}。"
              + (f"检索同时命中了 {'、'.join(losers)}，但其中对应条款已被取代，故不作为答案依据。"
                 if losers else ""))
    return Answer(status="answered", answer=answer, citations=citations,
                  gaps=[f"'{conflict.key}' 存在历史取值（" + "、".join(
                      f"{e.doc_id}：{e.value}" for e in conflict.entries if e.doc_id != conflict.winner
                  ) + "），已按文档声明消解"],
                  basis=conflict.basis)


def compose_conflict(conflict: Conflict, docs: dict[str, Document]) -> Answer:
    parts: list[str] = []
    citations: list[Citation] = []
    for entry in conflict.entries:
        doc = docs[entry.doc_id]
        sec = _section_of(doc, entry.heading)
        if sec:
            citations.append(_citation(doc, sec.text))
        parts.append(f"《{doc.title}》（{doc.doc_id}，{doc.version}，生效 {doc.effective_date}）"
                     f"规定「{entry.key}」为 {entry.value}")

    answer = ("资料库中关于「" + conflict.key + "」存在互相冲突的规定：\n"
              + "\n".join(f"- {p}" for p in parts)
              + f"\n\n查证结论：{conflict.basis}。\n"
              + f"因此这条问题暂时无法给出唯一答案，也不能从中擅自挑一个数值回答。")
    ids = "、".join(e.doc_id for e in conflict.entries)
    gaps = [
        f"{ids} 均未声明替代或废止关系，也没有可比较的效力优先级",
        "适用范围表述虽有差异，但都覆盖本次提问人群，不构成区分依据",
        "需要你确认应以哪份文档为准",
    ]
    return Answer(status="conflict", answer=answer, citations=citations, gaps=gaps,
                  next_actions=["确认适用制度（如指定以 D01 或 D02 为准）",
                                "或提供你的组织归属/岗位信息以判定适用范围"],
                  basis=conflict.basis)


def compose_insufficient(reasons: list[str], docs: dict[str, Document],
                         partial: list[Snippet] | None = None) -> Answer:
    answer = "无法给出可靠答案：" + "；".join(reasons) + "。"
    if partial:
        ids = "、".join(dict.fromkeys(p.doc_id for p in partial))
        answer += f"\n已检索到最接近的内容（{ids}），但不足以支撑结论。"
    return Answer(status="insufficient", answer=answer, gaps=reasons,
                  next_actions=["补充问题关键词或指定文档 ID 后重试"])


def compose_out_of_scope(question: str, docs: dict[str, Document]) -> Answer:
    return Answer(
        status="out_of_scope",
        answer="资料库里没有检索到与该问题相关的制度或产品说明，因此不回答。"
               "现有资料覆盖差旅与费用报销、设备借用、考勤请假、加班调休、产品导出、采购准入等主题。",
        gaps=["知识库内无对应文档", "该问题可能属于未入库的业务领域"],
        next_actions=["确认问题所属业务域，或提供对应制度文档后再问"],
    )
