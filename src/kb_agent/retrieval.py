"""文档解析 + 中文分词 + BM25 检索。

零第三方依赖：中文用字 bigram，英文/数字用整词；同义词做双向词面扩展。
取舍见 docs/02-solution.md「检索为什么不上向量库」。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from .types import Claim, Document, Section, Snippet

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
HEAD_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$")
KV_RE = re.compile(r"^\s*(?:[-*]\s*)?([^：:\n|#]{2,26})[：:]\s*(.+?)\s*$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*|\d+")

# 只用于「置信度」判断的泛词 bigram：它们单独命中不能算「库里有相关内容」
STOP_BIGRAMS = {
    "公司", "我们", "你们", "什么", "怎么", "可以", "需要", "一下", "请问",
    "是不", "不是", "有时", "有的", "这个", "那个", "多少", "如何", "是否",
    "现在", "然后", "以及", "还有", "一个", "哪些", "为什", "么样",
}

MIN_TOP_SCORE = 0.9  # 低于此分视为「库里没有相关内容」，触发补充检索/拒答


# ------------------------------------------------------------------ 解析

def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = ASCII_RE.findall(text)
    for run in CJK_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def load_synonyms(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def expand_query(query: str, synonyms: dict[str, list[str]]) -> str:
    """词面双向扩展：命中 key 补 values，命中 value 补 key。"""
    extra: list[str] = []
    for key, values in synonyms.items():
        if key in query:
            extra.extend(values)
        elif any(v in query for v in values):
            extra.append(key)
    return " ".join([query, *extra])


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, m.group(2)


def parse_document(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(raw)

    sections: list[Section] = []
    claims: list[Claim] = []
    heading = ""
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text or heading:
            sections.append(Section(doc_id=meta.get("doc_id", ""), heading=heading, text=text,
                                    claims=[c for c in claims if c.heading == heading]))

    for line in body.splitlines():
        h = HEAD_RE.match(line)
        if h:
            flush()
            heading = h.group(2)
            buf = []
            continue
        buf.append(line)
        kv = KV_RE.match(line)
        if kv and not line.strip().startswith("|"):
            key, value = kv.group(1).strip(), kv.group(2).strip()
            if len(value) <= 80 and "http" not in key:
                claims.append(Claim(doc_id=meta.get("doc_id", ""), key=key,
                                    value=value, line=line.strip(), heading=heading))
    flush()

    return Document(
        doc_id=meta.get("doc_id", path.stem),
        topic=meta.get("topic", "未分类"),
        title=meta.get("title", path.stem),
        version=meta.get("version", "unknown"),
        effective_date=meta.get("effective_date", "unknown"),
        priority=int(meta.get("priority", "0") or 0),
        scope=meta.get("scope", ""),
        status=meta.get("status", "active"),
        owner=meta.get("owner", ""),
        supersedes=meta.get("supersedes", "none"),
        text=body,
        sections=[s for s in sections if s.text or s.heading],
        claims=claims,
    )


def load_corpus(docs_dir: Path) -> list[Document]:
    docs = [parse_document(p) for p in sorted(docs_dir.glob("*.md"))]
    return [d for d in docs if d.status != "archived"]


# ------------------------------------------------------------------ 检索

class Index:
    """整库 BM25。规模是几十到几千个片段，暴力打分足够；上向量库属于过度设计。"""

    def __init__(self, docs: list[Document], synonyms: dict[str, list[str]] | None = None):
        self.docs = {d.doc_id: d for d in docs}
        self.synonyms = synonyms or {}
        self.units: list[tuple[Document, Section]] = []
        self.tokens: list[list[str]] = []
        self.tf: list[Counter] = []
        self.head_tf: list[Counter] = []
        for doc in docs:
            for sec in doc.sections:
                toks = tokenize(f"{sec.heading} {sec.text}")
                if not toks:
                    continue
                self.units.append((doc, sec))
                self.tokens.append(toks)
                self.tf.append(Counter(toks))
                # 标题单独计一份：相同词面下，标题命中比正文命中更能说明「这个小节就是讲这个的」
                self.head_tf.append(Counter(tokenize(sec.heading)))
        self.df: Counter = Counter()
        for toks in self.tokens:
            self.df.update(set(toks))
        self.n = len(self.units)
        self.avgdl = (sum(len(t) for t in self.tokens) / self.n) if self.n else 0.0

    # -- 打分 ---------------------------------------------------------

    def _score(self, q_tokens: list[str], i: int, k1: float = 1.5, b: float = 0.75,
               head_boost: float = 1.5) -> float:
        tf, dl = self.tf[i], len(self.tokens[i])
        head = self.head_tf[i]
        s = 0.0
        for t in q_tokens:
            f = tf.get(t, 0)
            if not f:
                continue
            df = self.df.get(t, 1)
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / self.avgdl))
            if t in head:
                s += head_boost * idf
        return s

    def search(self, query: str, top_k: int = 5, doc_ids: list[str] | None = None,
               expanded: str | None = None) -> list[Snippet]:
        q = expanded if expanded is not None else expand_query(query, self.synonyms)
        q_tokens = tokenize(q)
        if not q_tokens:
            return []
        scored: list[tuple[float, Document, Section]] = []
        for i, (doc, sec) in enumerate(self.units):
            if doc_ids and doc.doc_id not in doc_ids:
                continue
            s = self._score(q_tokens, i)
            if s > 0:
                scored.append((s, doc, sec))
        scored.sort(key=lambda x: -x[0])
        return [
            Snippet(doc_id=d.doc_id, title=d.title, version=d.version,
                    effective_date=d.effective_date, heading=sec.heading,
                    text=sec.text, score=s)
            for s, d, sec in scored[:top_k]
        ]

    # -- 置信度 -------------------------------------------------------

    @staticmethod
    def content_hits(query: str, snippets: list[Snippet]) -> int:
        """查询里「有内容」的 bigram 在命中片段里出现了几个（已剔除泛词）。"""
        q = {t for t in tokenize(query) if len(t) >= 2 and t not in STOP_BIGRAMS}
        if not q:
            return 0
        hit: set[str] = set()
        for s in snippets:
            hit |= q & set(tokenize(f"{s.heading} {s.text}"))
        return len(hit)

    def is_confident(self, query: str, snippets: list[Snippet]) -> bool:
        if not snippets:
            return False
        return snippets[0].score >= MIN_TOP_SCORE and self.content_hits(query, snippets) >= 1
