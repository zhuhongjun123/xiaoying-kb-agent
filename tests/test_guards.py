"""护栏测试：冲突判定/消解、引用可核验性。这两处是「关键缺陷」的防线。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kb_agent import guards  # noqa: E402
from kb_agent.retrieval import Index, load_corpus, load_synonyms  # noqa: E402
from kb_agent.types import Answer, Citation, Snippet  # noqa: E402

DATA = ROOT / "data"
DOCS = {d.doc_id: d for d in load_corpus(DATA / "docs")}
INDEX = Index(list(DOCS.values()), load_synonyms(DATA / "synonyms.json"))


def snippets_for(query: str) -> list[Snippet]:
    return INDEX.search(query)


class TestConflictDetection(unittest.TestCase):
    def test_unresolvable_conflict(self):
        """D01 30 天 vs D02 15 天：只看到片段时不可消解。"""
        snips = snippets_for("报销期限是多久？")
        conflicts = guards.detect_conflicts(snips, {}, DOCS)
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]
        self.assertEqual(c.key, "差旅费用报销期限")
        self.assertFalse(c.resolved)
        self.assertEqual(set(c.unverified), {"D01", "D02"})

    def test_conflict_still_unresolvable_after_reading_both(self):
        snips = snippets_for("报销期限是多久？")
        opened = {"D01": DOCS["D01"], "D02": DOCS["D02"]}
        c = guards.detect_conflicts(snips, opened, DOCS)[0]
        self.assertFalse(c.resolved)
        self.assertIn("未声明替代关系", c.basis)

    def test_resolvable_by_supersede(self):
        """D08 显式声明取代 D07，读完全文后应可消解。"""
        snips = snippets_for("调休必须在多久内用完？")
        opened = {"D07": DOCS["D07"], "D08": DOCS["D08"]}
        c = guards.detect_conflicts(snips, opened, DOCS)[0]
        self.assertTrue(c.resolved)
        self.assertEqual(c.winner, "D08")
        self.assertIn("取代", c.basis)

    def test_no_conflict_across_topics(self):
        """「起算时点」在报销与调休里都有，但主题不同，不得算冲突。"""
        snips = snippets_for("起算时点是什么？")
        for c in guards.detect_conflicts(snips, {"D01": DOCS["D01"], "D07": DOCS["D07"]}, DOCS):
            topics = {DOCS[e.doc_id].topic for e in c.entries}
            self.assertEqual(len(topics), 1, f"{c.key} 跨主题被误判为冲突：{topics}")

    def test_irrelevant_docs_excluded_from_evidence(self):
        """借用问题不应把调休文档拉进冲突比对。"""
        snips = snippets_for("设备借用一次最长可以借多久？")
        conflicts = guards.detect_conflicts(snips, {}, DOCS)
        self.assertEqual(conflicts, [])


class TestEffectiveDate(unittest.TestCase):
    """生效日期未到的修订版不能拿来消解冲突 —— 否则等于用未来的制度管今天。"""

    def _conflict(self, today):
        snips = snippets_for("调休必须在多久内用完？")
        opened = {"D07": DOCS["D07"], "D08": DOCS["D08"]}
        return guards.detect_conflicts(snips, opened, DOCS, today=today)[0]

    def test_before_effective_date_does_not_resolve(self):
        from datetime import date

        c = self._conflict(date(2026, 5, 1))
        self.assertFalse(c.resolved)
        self.assertIn("尚未到达", c.basis)

    def test_after_effective_date_resolves(self):
        from datetime import date

        c = self._conflict(date(2026, 10, 28))
        self.assertTrue(c.resolved)
        self.assertEqual(c.winner, "D08")


class TestCitationCheck(unittest.TestCase):
    def _answer(self, quote: str, text: str = "报销期限为 30 天。") -> Answer:
        return Answer(status="answered", answer=text,
                      citations=[Citation(doc_id="D01", version="v1.2", quote=quote)])

    def test_good_quote_passes(self):
        a = self._answer("差旅费用报销期限：30 天")
        self.assertEqual(guards.verify_answer(a, DOCS), [])

    def test_fabricated_quote_is_caught(self):
        a = self._answer("差旅费用报销期限：45 天")
        problems = guards.verify_answer(a, DOCS)
        self.assertTrue(any("找不到" in p for p in problems))

    def test_number_without_support_is_caught(self):
        a = self._answer("差旅费用报销期限：30 天", text="报销期限为 45 天。")
        problems = guards.verify_answer(a, DOCS)
        self.assertTrue(any("45" in p for p in problems))

    def test_clause_reference_numbers_are_exempt(self):
        """「第 3.1 条」「v1.2」「2026-06-01」这类指称不算断言数字。"""
        a = self._answer("差旅费用报销期限：30 天",
                         text="按第 3.1 条（D01 v1.2，2025-03-01 生效）为 30 天。")
        self.assertEqual(guards.verify_answer(a, DOCS), [])

    def test_answered_without_citation_is_caught(self):
        a = Answer(status="answered", answer="30 天。")
        self.assertTrue(any("缺少引用" in p for p in guards.verify_answer(a, DOCS)))


if __name__ == "__main__":
    unittest.main()
