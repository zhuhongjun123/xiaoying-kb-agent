"""检索与置信度闸门的测试。纯标准库 unittest。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kb_agent.retrieval import (  # noqa: E402
    Index, expand_query, load_corpus, load_synonyms, tokenize,
)

DATA = ROOT / "data"


def build_index() -> Index:
    return Index(load_corpus(DATA / "docs"), load_synonyms(DATA / "synonyms.json"))


class TestTokenize(unittest.TestCase):
    def test_chinese_bigram_and_ascii(self):
        toks = tokenize("D01 报销期限 30 天")
        self.assertIn("d01", toks)
        self.assertIn("报销", toks)
        self.assertIn("30", toks)

    def test_single_char_run_kept(self):
        self.assertIn("天", tokenize("天"))


class TestExpand(unittest.TestCase):
    def test_key_to_values(self):
        out = expand_query("报销期限", load_synonyms(DATA / "synonyms.json"))
        self.assertIn("时限", out)

    def test_value_to_key(self):
        out = expand_query("多久内用完", load_synonyms(DATA / "synonyms.json"))
        self.assertIn("期限", out)


class TestRetrieval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = build_index()

    def test_clear_question_hits_expected_doc(self):
        hits = self.index.search("设备借用一次最长可以借多久？")
        self.assertEqual(hits[0].doc_id, "D03")
        self.assertTrue(self.index.is_confident("设备借用一次最长可以借多久？", hits))

    def test_paraphrase_same_docs_as_formal_wording(self):
        a = {h.doc_id for h in self.index.search("报销期限是多久？")}
        b = {h.doc_id for h in self.index.search("出差回来报销单能拖多久才交？")}
        self.assertIn("D01", a & b)
        self.assertIn("D02", a & b)

    def test_out_of_scope_is_not_confident(self):
        q = "公司年会的抽奖规则是什么？中奖率多少？"
        hits = self.index.search(q)
        self.assertEqual(self.index.content_hits(q, hits), 0)
        self.assertFalse(self.index.is_confident(q, hits))

    def test_generic_word_alone_is_not_enough(self):
        """只命中「公司」这类泛词不算有内容，必须挡住。"""
        q = "公司"
        hits = self.index.search(q)
        self.assertFalse(self.index.is_confident(q, hits))

    def test_heading_boost_prefers_specific_section(self):
        """「住宿」问题下，标题就叫「住宿上限」的小节要压过泛泛的「目的与适用范围」。"""
        hits = self.index.search("出差住宿一晚最多能报多少钱？")
        self.assertEqual(hits[0].doc_id, "D10")
        self.assertIn("住宿", hits[0].heading)

    def test_scores_are_descending(self):
        scores = [h.score for h in self.index.search("调休 期限")]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
