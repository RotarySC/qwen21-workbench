#!/usr/bin/env python3
"""`prompts.py` —— 提示词库。

它守的是**「调好的提示词不能丢」**：本工作区的提示词常常几百字，丢了就得重写。
所以断言都在盘上：写进去、重启后还在、坏文件不让整个库读不出来。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import support  # noqa: E402  (测试基类 / 临时目录)

from workbench.prompts import PromptLibrary  # noqa: E402


class LibraryBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-lib-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "prompts.json")
        self.lib = PromptLibrary(path=self.path)


class Create(LibraryBase):
    def test_creates_and_lists(self):
        row = self.lib.create("a green apple on marble")
        self.assertEqual(row["text"], "a green apple on marble")
        self.assertEqual(row["title"], "a green apple on marble")
        self.assertEqual(row["uses"], 0)
        self.assertTrue(row["id"])
        self.assertEqual(len(self.lib.list()), 1)

    def test_title_defaults_to_first_line(self):
        row = self.lib.create("first line\nsecond line")
        self.assertEqual(row["title"], "first line")

    def test_explicit_title_wins(self):
        self.assertEqual(self.lib.create("body", title="我的提示词")["title"],
                         "我的提示词")

    def test_empty_text_rejected(self):
        for bad in ("", "   ", "\n\n", None):
            with self.assertRaises(ValueError):
                self.lib.create(bad)

    def test_overlong_text_rejected(self):
        with self.assertRaises(ValueError):
            self.lib.create("x" * 20001)

    def test_text_is_stripped(self):
        self.assertEqual(self.lib.create("  hi  ")["text"], "hi")


class Tags(LibraryBase):
    def test_list_and_comma_string_both_work(self):
        self.assertEqual(self.lib.create("a", tags=["x", "y"])["tags"], ["x", "y"])
        self.assertEqual(self.lib.create("b", tags="x, y")["tags"], ["x", "y"])

    def test_full_width_comma(self):
        self.assertEqual(self.lib.create("a", tags="苹果，静物")["tags"],
                         ["苹果", "静物"])

    def test_dedupes_and_strips(self):
        self.assertEqual(self.lib.create("a", tags=[" x ", "x", "", "y"])["tags"],
                         ["x", "y"])

    def test_caps_the_count(self):
        row = self.lib.create("a", tags=[str(i) for i in range(50)])
        self.assertEqual(len(row["tags"]), 12)

    def test_tag_counts(self):
        self.lib.create("a", tags=["fruit", "still"])
        self.lib.create("b", tags=["fruit"])
        tags = {t["tag"]: t["count"] for t in self.lib.tags()}
        self.assertEqual(tags, {"fruit": 2, "still": 1})


class Persistence(LibraryBase):
    def test_survives_a_reload(self):
        row = self.lib.create("keep me", title="t", tags=["a"])
        again = PromptLibrary(path=self.path)
        got = again.get(row["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["text"], "keep me")
        self.assertEqual(got["tags"], ["a"])

    def test_broken_file_does_not_kill_the_library(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        lib = PromptLibrary(path=self.path)
        self.assertEqual(lib.count(), 0)
        lib.create("still works")
        self.assertEqual(lib.count(), 1)

    def test_rows_without_text_are_dropped_on_load(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([{"id": "a", "text": "ok"},
                       {"id": "b"},                 # 没有 text
                       "not a dict"], f)
        self.assertEqual(PromptLibrary(path=self.path).count(), 1)

    def test_accepts_wrapped_shape(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"prompts": [{"id": "a", "text": "ok"}]}, f)
        self.assertEqual(PromptLibrary(path=self.path).count(), 1)


class UpdateDelete(LibraryBase):
    def test_update_changes_only_what_is_given(self):
        row = self.lib.create("old text", title="old", tags=["a", "b"])
        got = self.lib.update(row["id"], text="new text")
        self.assertEqual(got["text"], "new text")
        self.assertEqual(got["title"], "old")        # 没给就不动
        self.assertEqual(got["tags"], ["a", "b"])

    def test_update_title_and_tags(self):
        row = self.lib.create("text", title="old", tags=["a"])
        got = self.lib.update(row["id"], title="new", tags=["z"])
        self.assertEqual((got["title"], got["tags"]), ("new", ["z"]))

    def test_update_can_clear_tags_with_empty_list(self):
        row = self.lib.create("text", tags=["a"])
        self.assertEqual(self.lib.update(row["id"], tags=[])["tags"], [])

    def test_update_empty_text_rejected(self):
        row = self.lib.create("text")
        with self.assertRaises(ValueError):
            self.lib.update(row["id"], text="   ")
        self.assertEqual(self.lib.get(row["id"])["text"], "text")

    def test_update_unknown_is_none(self):
        self.assertIsNone(self.lib.update("nope", text="x"))

    def test_delete(self):
        row = self.lib.create("text")
        self.assertTrue(self.lib.delete(row["id"]))
        self.assertIsNone(self.lib.get(row["id"]))
        self.assertFalse(self.lib.delete(row["id"]))     # 第二次没有可删的

    def test_touch_counts_uses(self):
        row = self.lib.create("text")
        self.lib.touch(row["id"])
        self.assertEqual(self.lib.get(row["id"])["uses"], 1)
        self.assertIsNone(self.lib.touch("nope"))


class Ordering(LibraryBase):
    def test_most_used_first(self):
        a = self.lib.create("a")
        b = self.lib.create("b")
        self.lib.touch(b["id"])
        self.lib.touch(b["id"])
        self.assertEqual([r["id"] for r in self.lib.list()][0], b["id"])

    def test_search_covers_title_text_and_tags(self):
        self.lib.create("a green apple", title="fruit", tags=["still life"])
        self.lib.create("a red fox", title="animal")
        self.assertEqual(len(self.lib.list(query="green")), 1)
        self.assertEqual(len(self.lib.list(query="animal")), 1)
        self.assertEqual(len(self.lib.list(query="still")), 1)
        self.assertEqual(len(self.lib.list(query="nothing")), 0)

    def test_filter_by_tag(self):
        self.lib.create("a", tags=["x"])
        self.lib.create("b", tags=["y"])
        self.assertEqual(len(self.lib.list(tag="x")), 1)


class Transfer(LibraryBase):
    def test_export_import_roundtrip(self):
        self.lib.create("one", title="t1")
        self.lib.create("two", title="t2")
        payload = self.lib.export()
        self.assertEqual(payload["version"], 1)

        other = PromptLibrary(path=os.path.join(self.tmp, "other.json"))
        added, skipped = other.import_rows(payload)
        self.assertEqual((added, skipped), (2, 0))
        self.assertEqual(other.count(), 2)

    def test_import_skips_existing_ids(self):
        row = self.lib.create("one")
        added, skipped = self.lib.import_rows({"prompts": [row, row]})
        self.assertEqual((added, skipped), (0, 2))
        self.assertEqual(self.lib.count(), 1)

    def test_import_survives_garbage_rows(self):
        added, skipped = self.lib.import_rows({
            "prompts": [None, "x", {"id": "a"}, {"id": "b", "text": "  "},
                        {"id": "c", "text": "good"}]})
        self.assertEqual((added, skipped), (1, 4))

    def test_import_rejects_non_list(self):
        with self.assertRaises(ValueError):
            self.lib.import_rows({"prompts": "nope"})


class Prune(support.LeakCheckedTestCase):
    def test_prunes_least_used_and_keeps_the_rest(self):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-lib-prune-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lib = PromptLibrary(path=os.path.join(tmp, "p.json"), max_rows=3)
        keep = lib.create("keep", title="keep")
        for _ in range(5):
            lib.touch(keep["id"])
        for i in range(6):
            lib.create("filler %d" % i, title="f%d" % i)
        self.assertLessEqual(lib.count(), 3)
        self.assertIsNotNone(lib.get(keep["id"]), "常用的那条不该被裁掉")

    def test_prune_is_a_noop_under_the_limit(self):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-lib-prune2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lib = PromptLibrary(path=os.path.join(tmp, "p.json"), max_rows=100)
        for i in range(5):
            lib.create("p%d" % i)
        self.assertEqual(lib.count(), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
