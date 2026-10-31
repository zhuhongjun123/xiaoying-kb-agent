"""端到端：验收用例全跑一遍，外加「换文档内容，答案与动作必须跟着变」的数据驱动验证。"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kb_agent.agent import Agent  # noqa: E402
from kb_agent.policy import RulePolicy  # noqa: E402
from kb_agent.retrieval import Index, load_corpus, load_synonyms  # noqa: E402

DATA = ROOT / "data"


def build_agent(data_dir: Path = DATA) -> Agent:
    docs = load_corpus(data_dir / "docs")
    return Agent(Index(docs, load_synonyms(data_dir / "synonyms.json")), RulePolicy())


def load_cases() -> list[dict]:
    return [json.loads(line) for line in
            (DATA / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def run(agent: Agent, question: str, **kw) -> dict:
    return agent.run(question=question, **kw)


class TestAcceptanceCases(unittest.TestCase):
    """覆盖题目六个必验点 + 预算耗尽。"""

    @classmethod
    def setUpClass(cls):
        cls.agent = build_agent()
        cls.traces = {}
        for case in load_cases():
            budget = case.get("budget") or {}
            cls.traces[case["case_id"]] = run(
                cls.agent, case["question"], history=case.get("history") or [],
                faults=case.get("tool_faults") or {},
                max_steps=budget.get("max_steps", 8),
                max_tool_calls=budget.get("max_tool_calls", 6),
                case_id=case["case_id"], title=case["title"],
            )
        cls.cases = {c["case_id"]: c for c in load_cases()}

    def test_every_case_matches_expectation(self):
        for cid, case in self.cases.items():
            trace, expect = self.traces[cid], case.get("expect") or {}
            with self.subTest(case=cid):
                self.assertEqual(trace["result"]["status"], expect["status"])
                cited = [c["doc_id"] for c in trace["result"]["citations"]]
                for doc_id in expect.get("must_cite", []):
                    self.assertIn(doc_id, cited)
                for text in expect.get("must_answer_contains", []):
                    self.assertIn(text, trace["result"]["answer"])
                for text in expect.get("must_not_contain", []):
                    self.assertNotIn(text, trace["result"]["answer"])

    def test_all_citations_verifiable(self):
        for cid, trace in self.traces.items():
            with self.subTest(case=cid):
                self.assertEqual(trace["citation_check"]["problems"], [])

    def test_conflict_case_never_picks_one_side(self):
        """C2 的答案必须同时出现两个数值，且状态是 conflict。"""
        t = self.traces["C2"]["result"]
        self.assertEqual(t["status"], "conflict")
        self.assertIn("30 天", t["answer"])
        self.assertIn("15 天", t["answer"])
        self.assertTrue(t["next_actions"])

    def test_paraphrase_conclusion_matches_original(self):
        """C4 换了问法，结论类型必须与 C2 一致。"""
        self.assertEqual(self.traces["C4"]["result"]["status"],
                         self.traces["C2"]["result"]["status"])

    def test_tool_failure_path_has_retry_then_gives_up(self):
        steps = [s["action"] for s in self.traces["C7"]["steps"]]
        self.assertEqual(steps[:2], ["search_docs", "search_docs"])
        self.assertEqual(self.traces["C7"]["result"]["status"], "insufficient")

    def test_budget_exhaustion_returns_unfinished(self):
        t = self.traces["C9"]
        self.assertEqual(t["result"]["status"], "unfinished")
        self.assertEqual(t["budget"]["steps_used"], 1)

    def test_trace_shape_is_complete(self):
        """每条 trace 都必须有步骤、工具参数、返回摘要、决策依据。"""
        for cid, trace in self.traces.items():
            with self.subTest(case=cid):
                self.assertTrue(trace["steps"])
                for s in trace["steps"]:
                    self.assertIn("action", s)
                    self.assertIn("args", s)
                    self.assertIn("reason", s)
                    self.assertIn("observation", s)
                    self.assertIn("summary", s["observation"])


class TestDataDriven(unittest.TestCase):
    """审核要点：替换文档内容，答案与动作要跟着变 —— 说明结论不是写死的。"""

    def _copy_data(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(DATA, tmp / "data")
        return tmp / "data"

    def test_answer_follows_modified_value(self):
        data = self._copy_data()
        doc = data / "docs" / "D01_travel_reimbursement.md"
        doc.write_text(doc.read_text(encoding="utf-8").replace(
            "差旅费用报销期限：30 天", "差旅费用报销期限：45 天"), encoding="utf-8")
        trace = run(build_agent(data), "报销期限是多久？")
        self.assertIn("45 天", trace["result"]["answer"])
        self.assertNotIn("30 天", trace["result"]["answer"])

    def test_conflict_disappears_when_values_become_equal(self):
        """把 D02 改成 30 天，两文一致后状态应从 conflict 变成 answered。"""
        data = self._copy_data()
        doc = data / "docs" / "D02_expense_reimbursement_rules.md"
        doc.write_text(doc.read_text(encoding="utf-8").replace(
            "差旅费用报销期限：15 天", "差旅费用报销期限：30 天"), encoding="utf-8")
        trace = run(build_agent(data), "报销期限是多久？")
        self.assertEqual(trace["result"]["status"], "answered")
        self.assertIn("30 天", trace["result"]["answer"])

    def test_supersede_removed_falls_back_to_priority(self):
        """删掉 D08 的取代声明后，消解应降级到第二级规则：显式优先级。"""
        data = self._copy_data()
        doc = data / "docs" / "D08_overtime_comp_time_v2.md"
        doc.write_text(doc.read_text(encoding="utf-8").replace(
            "supersedes: D07", "supersedes: none"), encoding="utf-8")
        trace = run(build_agent(data), "调休必须在多久内用完？")
        self.assertEqual(trace["result"]["status"], "answered")
        self.assertIn("优先级", trace["result"]["basis"])

    def test_no_supersede_no_priority_becomes_conflict(self):
        """取代声明与优先级都去掉，就只剩冲突，必须追问。"""
        data = self._copy_data()
        doc = data / "docs" / "D08_overtime_comp_time_v2.md"
        doc.write_text(doc.read_text(encoding="utf-8")
                       .replace("supersedes: D07", "supersedes: none")
                       .replace("priority: 2", "priority: 1"), encoding="utf-8")
        trace = run(build_agent(data), "调休必须在多久内用完？")
        self.assertEqual(trace["result"]["status"], "conflict")

    def test_partial_resolution_still_reports_conflict(self):
        """新增一份只取代 D01 的文档：D01 退出，D02 仍在，结论必须是部分消解后的冲突。"""
        data = self._copy_data()
        (data / "docs" / "D10_travel_update.md").write_text(
            "---\ndoc_id: D10\ntopic: 报销\ntitle: 差旅报销管理办法（2026 修订）\n"
            "version: v2.0\neffective_date: 2026-08-01\npriority: 2\n"
            "scope: 公司全体在职员工\nstatus: active\nowner: 财务部\nsupersedes: D01\n---\n\n"
            "## 3.1 报销期限\n\n- 差旅费用报销期限：10 天\n- 起算时点：出差行程结束之日\n",
            encoding="utf-8")
        trace = run(build_agent(data), "报销期限是多久？")
        self.assertEqual(trace["result"]["status"], "conflict")
        self.assertIn("10 天", trace["result"]["answer"])

    def test_future_effective_date_breaks_resolution(self):
        """把 D08 的生效日期推到未来，取代关系不能生效，必须退回冲突。"""
        data = self._copy_data()
        doc = data / "docs" / "D08_overtime_comp_time_v2.md"
        doc.write_text(doc.read_text(encoding="utf-8").replace(
            "effective_date: 2026-06-01", "effective_date: 2099-01-01"), encoding="utf-8")
        trace = run(build_agent(data), "调休必须在多久内用完？")
        self.assertEqual(trace["result"]["status"], "conflict")

    def test_new_topic_document_is_retrievable(self):
        """新增一个此前不存在的主题文档，不改代码即可被检索并作答。"""
        data = self._copy_data()
        (data / "docs" / "D11_access_card.md").write_text(
            "---\ndoc_id: D11\ntopic: 门禁\ntitle: 办公区门禁卡管理规定\n"
            "version: v1.0\neffective_date: 2026-09-01\npriority: 1\n"
            "scope: 全体在职员工\nstatus: active\nowner: 行政部\nsupersedes: none\n---\n\n"
            "## 2 补办\n\n- 门禁卡补办工本费：20 元/张\n- 补办领取时点：提交申请后 2 个工作日\n",
            encoding="utf-8")
        trace = run(build_agent(data), "门禁卡丢了补办要多少钱？")
        self.assertEqual(trace["result"]["status"], "answered")
        self.assertIn("20 元", trace["result"]["answer"])


class TestBudget(unittest.TestCase):
    def test_tool_call_limit(self):
        trace = run(build_agent(), "报销期限是多久？", max_tool_calls=1)
        self.assertEqual(trace["result"]["status"], "unfinished")
        self.assertEqual(trace["budget"]["tool_calls_used"], 1)


if __name__ == "__main__":
    unittest.main()
