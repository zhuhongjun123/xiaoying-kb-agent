"""命令行入口。三个子命令：ask（单问）、run-cases（批量跑验收用例）、dump（打印库概览）。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .agent import Agent
from .policy import RulePolicy
from .retrieval import Index, load_corpus, load_synonyms

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "data"


def load_dotenv(path: Path) -> None:
    """极简 .env 读取：只认 KEY=VALUE，不覆盖已有的环境变量。够用就不引依赖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_agent(data_dir: Path, provider: str = "mock") -> Agent:
    docs = load_corpus(data_dir / "docs")
    index = Index(docs, load_synonyms(data_dir / "synonyms.json"))
    if provider == "llm":
        from .llm import LLMDecider, LLMError  # 延迟导入：离线路径不该依赖任何网络配置

        try:
            decider: Any = LLMDecider()
        except LLMError as exc:
            print(f"[模型通道不可用] {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    else:
        decider = RulePolicy()
    return Agent(index, decider)


def parse_faults(specs: list[str] | None) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for spec in specs or []:
        tool, _, seq = spec.partition(":")
        nums: list[Any] = []
        for part in seq.split(",") if seq else ["1"]:
            part = part.strip()
            if part in ("*", "all"):
                nums.append("*")
            elif part.isdigit():
                nums.append(int(part))
        out.setdefault(tool, []).extend(nums or [1])
    return out


def print_answer(trace: dict[str, Any]) -> None:
    r = trace["result"]
    b = trace["budget"]
    print(f"\n=== 状态：{r['status']} ===")
    print(r["answer"])
    if r["citations"]:
        print("\n-- 引用 --")
        for c in r["citations"]:
            print(f"[{c['doc_id']} {c['version']}] {c['quote'].strip()[:200]}")
    if r["gaps"]:
        print("\n-- 资料缺口 --")
        for g in r["gaps"]:
            print(f"- {g}")
    if r["next_actions"]:
        print("\n-- 下一步 --")
        for n in r["next_actions"]:
            print(f"- {n}")
    print(f"\n-- 预算 -- {b['steps_used']}/{b['max_steps']} 步，"
          f"{b['tool_calls_used']}/{b['max_tool_calls']} 次工具调用")
    print(f"-- 引用核验 -- {'通过' if trace['citation_check']['ok'] else '未通过'}")
    for p in trace["citation_check"]["problems"]:
        print(f"   ! {p}")


def result_payload(trace: dict[str, Any]) -> dict[str, Any]:
    """对外输出契约：不含逐步 trace，只留结论与判卷结果，便于 diff 与自动评分。"""
    return {
        "case_id": trace["case_id"],
        "title": trace["title"],
        "question": trace["question"],
        **trace["result"],
        "budget": trace["budget"],
        "citation_check": trace["citation_check"],
    }


def write_text_lf(path: Path, text: str) -> None:
    """固定 LF 换行：仓库按 .gitattributes 签出 LF，若这里写 CRLF，重跑就会产生无意义 diff。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_json(path: Path, payload: Any) -> None:
    write_text_lf(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def cmd_ask(args: argparse.Namespace) -> int:
    spec: dict[str, Any] = {}
    if args.input:
        spec = json.loads(Path(args.input).read_text(encoding="utf-8"))
    question = str(spec.get("question") or args.question or "").strip()
    if not question:
        print("缺少问题：给位置参数，或在 --input 文件里提供 question 字段", file=sys.stderr)
        return 2

    history = spec.get("history") or (json.loads(args.history_json) if args.history_json else [])
    faults = spec.get("tool_faults") or parse_faults(args.fail)
    budget = spec.get("budget") or {}

    agent = build_agent(Path(args.data), args.provider)
    trace = agent.run(
        question=question,
        history=history,
        faults=faults,
        max_steps=budget.get("max_steps", args.max_steps),
        max_tool_calls=budget.get("max_tool_calls", args.max_tool_calls),
        case_id=str(spec.get("case_id") or ""),
        title=str(spec.get("title") or ""),
    )
    if args.json:
        print(json.dumps(trace, ensure_ascii=False, indent=2))
    else:
        print_answer(trace)
    if args.save_trace:
        write_json(Path(args.save_trace), trace)
    if args.out:
        write_json(Path(args.out), result_payload(trace))
    return 0


# ---------------------------------------------------------------- 批量

def check_expect(trace: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """断言只看语义，不卡空白：模型写「60天」与原文「60 天」等价。"""
    r = trace["result"]
    errs: list[str] = []
    answer = re.sub(r"\s+", "", r["answer"])
    if expect.get("status") and r["status"] != expect["status"]:
        errs.append(f"状态期望 {expect['status']}，实际 {r['status']}")
    cited = [c["doc_id"] for c in r["citations"]]
    for doc_id in expect.get("must_cite", []):
        if doc_id not in cited:
            errs.append(f"未引用必需的 {doc_id}（实际 {cited}）")
    for text in expect.get("must_answer_contains", []):
        if re.sub(r"\s+", "", text) not in answer:
            errs.append(f"答案缺少「{text}」")
    for text in expect.get("must_not_contain", []):
        if re.sub(r"\s+", "", text) in answer:
            errs.append(f"答案不应出现「{text}」")
    if not trace["citation_check"]["ok"]:
        errs.extend(trace["citation_check"]["problems"])
    return errs


def render_transcript(trace: dict[str, Any], errs: list[str]) -> str:
    r = trace["result"]
    b = trace["budget"]
    lines = [f"# {trace['case_id']} {trace['title']}", "",
             f"- 问题：{trace['question']}"]
    if trace["history"]:
        lines.append("- 对话历史：" + "；".join(
            f"{m.get('role')}「{str(m.get('content'))[:60]}」" for m in trace["history"]))
    else:
        lines.append("- 对话历史：无")
    lines += [f"- 决策提供方：{trace['provider']}",
              f"- 预算：步数上限 {b['max_steps']}、工具调用上限 {b['max_tool_calls']}；"
              f"实际消耗 {b['steps_used']} 步 / {b['tool_calls_used']} 次", "",
              "## 执行记录", ""]
    steps = trace["steps"]
    for i, s in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        obs = s["observation"]
        lines += [f"### Step {s['step']} · `{s['action']}`",
                  f"- 参数：`{json.dumps(s['args'], ensure_ascii=False)}`",
                  f"- 决策依据：{s['reason']}",
                  f"- 返回（{'ok' if obs.get('ok') else '失败 ' + str(obs.get('error'))}）："
                  f"{obs.get('summary')}",
                  f"- 后续动作：{('`' + nxt['action'] + '` — ' + nxt['reason']) if nxt else '无（已结束）'}",
                  ""]
        if s["action"] == "search_docs" and obs.get("data", {}).get("hits"):
            for h in obs["data"]["hits"][:4]:
                lines.append(f"  - [{h['doc_id']} {h['version']} / {h['score']}] "
                             f"{h['heading']}：{h['text'].replace(chr(10), ' / ')[:110]}")
            lines.append("")
    lines += ["## 最终结果", "", f"- 状态：**{r['status']}**", "", "```text", r["answer"].strip(), "```", ""]
    if r["citations"]:
        lines.append("### 引用原文")
        for c in r["citations"]:
            lines.append(f"- `{c['doc_id']}` {c['version']}：{c['quote'].strip().replace(chr(10), ' / ')}")
        lines.append("")
    if r["gaps"]:
        lines.append("### 资料缺口")
        lines += [f"- {g}" for g in r["gaps"]]
        lines.append("")
    if r["next_actions"]:
        lines.append("### 下一步动作")
        lines += [f"- {n}" for n in r["next_actions"]]
        lines.append("")
    lines.append(f"### 引用可核验性：{'通过' if not trace['citation_check']['problems'] else '未通过'}")
    lines += [f"- {p}" for p in trace["citation_check"]["problems"]]
    lines += ["", f"### 断言结果：{'通过' if not errs else '未通过'}"]
    lines += [f"- {e}" for e in errs]
    return "\n".join(lines) + "\n"


def cmd_run_cases(args: argparse.Namespace) -> int:
    data_dir = Path(args.data)
    cases = [json.loads(line) for line in
             (data_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    agent = build_agent(data_dir, args.provider)
    out = Path(args.out)
    for sub in ("inputs", "outputs", "traces", "transcripts"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, str, str, str, str]] = []
    failures = 0
    for case in cases:
        budget = case.get("budget") or {}
        trace = agent.run(
            question=case["question"],
            history=case.get("history") or [],
            faults=case.get("tool_faults") or {},
            max_steps=budget.get("max_steps", args.max_steps),
            max_tool_calls=budget.get("max_tool_calls", args.max_tool_calls),
            case_id=case["case_id"],
            title=case["title"],
        )
        errs = check_expect(trace, case.get("expect") or {})
        failures += bool(errs)

        # 输入/输出成对落盘：输入可直接 `--input` 复跑，输出可 diff 判确定性
        name = f"{case['case_id']}-{case.get('slug', case['case_id'])}"
        write_json(out / "inputs" / f"{name}.input.json", {
            "case_id": case["case_id"], "title": case["title"],
            "question": case["question"],
            "history": case.get("history") or [],
            "tool_faults": case.get("tool_faults") or {},
            "budget": budget,
        })
        write_json(out / "outputs" / f"{name}.output.json", result_payload(trace))
        write_json(out / "traces" / f"{name}.json", trace)
        write_text_lf(out / "transcripts" / f"{case['case_id']}.md",
                      render_transcript(trace, errs))
        rows.append((case["case_id"], case["title"], trace["result"]["status"],
                     f"{trace['budget']['steps_used']}/{trace['budget']['tool_calls_used']}",
                     "PASS" if not errs else "FAIL"))

    width = max(len(r[1]) for r in rows)
    print(f"{'用例':<5}{'场景':<{width + 2}}{'状态':<20}{'步/工具':<9}断言")
    for cid, title, status, used, verdict in rows:
        print(f"{cid:<5}{title:<{width + 2}}{status:<20}{used:<9}{verdict}")
    shown = os.path.relpath(out.resolve(), ROOT) if out.resolve().is_relative_to(ROOT) else str(out)
    print(f"\n共 {len(rows)} 个用例，失败 {failures} 个。inputs/outputs/traces/transcripts 已写入 {shown}")
    return 1 if failures and args.strict else 0


def cmd_dump(args: argparse.Namespace) -> int:
    docs = load_corpus(Path(args.data) / "docs")
    for d in docs:
        print(f"{d.doc_id}  {d.version:<6} 生效 {d.effective_date}  "
              f"priority={d.priority}  替代={d.supersedes}  claims={len(d.claims)}  《{d.title}》")
    print(f"\n共 {len(docs)} 份文档，{sum(len(d.claims) for d in docs)} 条可比对断言")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kb-agent", description="企业知识问答 Agent")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="data 目录")
    p.add_argument("--provider", choices=["mock", "llm"], default="mock",
                   help="mock=离线规则策略；llm=OpenAI 兼容模型")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="单条提问（可直接吃 examples/inputs/*.input.json）")
    a.add_argument("question", nargs="?", default="")
    a.add_argument("--input", default="", help="从 JSON 文件读取 question/history/tool_faults/budget")
    a.add_argument("--out", default="", help="把结论写成 JSON（不含逐步 trace）")
    a.add_argument("--history-json", default="", help='如 [{"role":"user","content":"..."}]')
    a.add_argument("--fail", action="append", help="注入工具故障，如 search_docs:1 或 read_doc:*")
    a.add_argument("--max-steps", type=int, default=8)
    a.add_argument("--max-tool-calls", type=int, default=6)
    a.add_argument("--json", action="store_true", help="输出完整 trace")
    a.add_argument("--save-trace", default="")
    # 允许 --provider/--data 写在 ask 之后（SUPPRESS 保证不覆盖顶层已解析的值）
    a.add_argument("--provider", choices=["mock", "llm"], default=argparse.SUPPRESS)
    a.add_argument("--data", default=argparse.SUPPRESS)
    a.set_defaults(func=cmd_ask)

    r = sub.add_parser("run-cases", help="批量跑 data/cases.jsonl")
    r.add_argument("--out", default=str(ROOT / "examples"))
    r.add_argument("--max-steps", type=int, default=8)
    r.add_argument("--max-tool-calls", type=int, default=6)
    r.add_argument("--strict", action="store_true", help="有断言失败则返回非 0")
    r.set_defaults(func=cmd_run_cases)

    d = sub.add_parser("dump", help="打印文档库概览")
    d.set_defaults(func=cmd_dump)

    # 没有子命令时默认 ask：让 `python -m src.main --input xxx.input.json`
    # 与 `python run.py "报销期限是多久？"` 都能直接跑
    argv = list(sys.argv[1:] if argv is None else argv)
    if not {"ask", "run-cases", "dump"} & set(argv):
        argv.insert(0, "ask")

    args = p.parse_args(argv)
    load_dotenv(ROOT / ".env")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
