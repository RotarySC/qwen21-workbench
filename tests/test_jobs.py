#!/usr/bin/env python3
"""`jobs.py` —— 状态落盘与运行记录。

判据 2（刷新不丢）和判据 3（画廊）都压在这个模块上，所以这里的断言都在
「盘上到底有什么」，而不是「内存里返回了什么」。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench.jobs import JobStore  # noqa: E402
from workbench.util import read_json, read_jsonl  # noqa: E402

WORKFLOW = {"459:458": {"class_type": "KSampler"},
            "459:451": {"class_type": "UNETLoader"},
            "461": {"class_type": "SaveImageAdvanced"}}


class StoreBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-jobs-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.jobs_dir = os.path.join(self.tmp, "jobs")
        self.runs_file = os.path.join(self.tmp, "runs.jsonl")
        self.store = JobStore(jobs_dir=self.jobs_dir, runs_file=self.runs_file)

    def add(self, jid="job-1", mode="t2i", prompt="a cat", **kw):
        return self.store.create(job_id=jid, mode=mode, params={"steps": 20},
                                 prompt_text=prompt, workflow=WORKFLOW, **kw)


class NewJob(StoreBase):
    def test_job_id_is_prompt_id(self):
        """刻意的同值：PNG 元数据 / /history / 探针报告靠它共用一个键。"""
        rec = self.add("abc-123")
        self.assertEqual(rec["prompt_id"], "abc-123")
        self.assertEqual(rec["status"], "queued")
        self.assertIsNone(rec["ended_at"])

    def test_node_classes_captured_for_ui_labels(self):
        rec = self.add()
        self.assertEqual(rec["node_classes"]["459:458"], "KSampler")
        self.assertEqual(rec["node_count"], 3)

    def test_lands_on_disk_immediately(self):
        self.add("disk-1")
        on_disk = read_json(os.path.join(self.jobs_dir, "disk-1.json"))
        self.assertEqual(on_disk["job_id"], "disk-1")


class Persistence(StoreBase):
    def test_reload_sees_the_same_records(self):
        self.add("r-1")
        self.store.patch("r-1", progress={"value": 5, "max": 20})
        again = JobStore(jobs_dir=self.jobs_dir, runs_file=self.runs_file)
        rec = again.get("r-1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["progress"], {"value": 5, "max": 20})

    def test_load_does_not_rewrite_status(self):
        """上次进程死掉留下的 running 可能是真的还在跑，也可能是后端一起没了。
        猜错会撒谎，所以载入时一个字都不改，对账交给 Watcher。"""
        self.add("r-2")
        self.store.mark_running("r-2")
        again = JobStore(jobs_dir=self.jobs_dir, runs_file=self.runs_file)
        self.assertEqual(again.get("r-2")["status"], "running")

    def test_corrupt_file_is_skipped(self):
        self.add("good")
        with open(os.path.join(self.jobs_dir, "broken.json"), "w",
                  encoding="utf-8") as f:
            f.write("{ not json")
        again = JobStore(jobs_dir=self.jobs_dir, runs_file=self.runs_file)
        self.assertEqual(again.count(), 1)
        self.assertIsNotNone(again.get("good"))


class ProgressThrottle(StoreBase):
    def test_memory_always_updated_disk_throttled(self):
        jid = "thr-1"
        self.add(jid)
        self.store.patch_progress(jid, "n1", "KSampler", 1, 20)
        first = read_json(self.store.path(jid))
        self.store.patch_progress(jid, "n2", "KSampler", 2, 20)
        self.assertEqual(self.store.get(jid)["progress"]["value"], 2)   # 内存
        second = read_json(self.store.path(jid))
        self.assertEqual(second["progress"]["value"], 1)                # 盘，被节流
        self.assertEqual(first["progress"]["value"], 1)

    def test_progress_flips_queued_to_running(self):
        jid = "thr-2"
        self.add(jid)
        self.store.patch_progress(jid, "n1", "KSampler", 1, 20)
        self.assertEqual(self.store.get(jid)["status"], "running")


class Finish(StoreBase):
    def test_appends_one_run_line_with_wall_ms(self):
        jid = "fin-1"
        self.add(jid)
        self.store.mark_running(jid)
        rec = self.store.finish(jid, "done", outputs=[
            {"filename": "a.png", "subfolder": "", "type": "output"}])
        self.assertEqual(rec["status"], "done")
        self.assertGreater(rec["wall_ms"], 0)
        rows = read_jsonl(self.runs_file)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], jid)
        self.assertEqual(rows[0]["prompt_id"], jid)
        self.assertEqual(rows[0]["outputs"][0]["filename"], "a.png")

    def test_uses_official_wall_ms_when_given(self):
        jid = "fin-2"
        self.add(jid)
        self.store.finish(jid, "done", wall_ms=50000)
        self.assertEqual(self.store.get(jid)["wall_ms"], 50000)

    def test_is_idempotent_no_double_run_line(self):
        """WS 的成功消息和轮询会几乎同时想收尾 —— 只能记一次。"""
        jid = "fin-3"
        self.add(jid)
        self.store.finish(jid, "done")
        self.store.finish(jid, "error")
        self.store.finish(jid, "done")
        rows = read_jsonl(self.runs_file)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.get(jid)["status"], "done")

    def test_error_keeps_message(self):
        jid = "fin-4"
        self.add(jid)
        self.store.finish(jid, "error", error={"message": "CUDA out of memory"})
        row = read_jsonl(self.runs_file)[0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"]["message"], "CUDA out of memory")

    def test_unknown_job_returns_none(self):
        self.assertIsNone(self.store.finish("nope", "done"))

    def test_finished_job_leaves_active(self):
        self.add("fin-5")
        self.assertEqual(len(self.store.active()), 1)
        self.store.finish("fin-5", "cancelled")
        self.assertEqual(self.store.active(), [])


class Listing(StoreBase):
    def test_newest_first(self):
        self.add("a", prompt="first")
        self.store.patch("a", created_at=1000)
        self.add("b", prompt="second")
        self.store.patch("b", created_at=2000)
        self.add("c", prompt="third")
        self.store.patch("c", created_at=3000)
        self.assertEqual([r["job_id"] for r in self.store.list()], ["c", "b", "a"])

    def test_mode_filter_and_paging(self):
        self.add("t1", mode="t2i")
        self.add("t2", mode="t2i")
        # 记录里的 mode 只是个字符串，挑一个**现在真的存在**的模式当第二个值 ——
        # 以前这里用 "refine"，而它 2026-10-01 已经不是模式了（会让人以为这条
        # 判据在验一个不存在的取值）。
        self.add("r1", mode="upscale")
        self.assertEqual([r["job_id"] for r in self.store.list(mode="upscale")], ["r1"])
        self.assertEqual(len(self.store.list(limit=1)), 1)

    def test_runs_reads_jsonl_newest_first(self):
        for i, jid in enumerate(["x", "y"]):
            self.add(jid)
            self.store.patch(jid, created_at=i)
            self.store.finish(jid, "done", wall_ms=(i + 1) * 1000)
        rows = self.store.runs()
        self.assertEqual([r["job_id"] for r in rows], ["y", "x"])


class Prune(StoreBase):
    def test_prunes_oldest_terminal_only(self):
        store = JobStore(jobs_dir=self.jobs_dir, runs_file=self.runs_file,
                         max_jobs=3)
        for i in range(5):
            store.create(job_id="done-%d" % i, mode="t2i", params={},
                         prompt_text="p", workflow=WORKFLOW)
            store.patch("done-%d" % i, created_at=i)
            store.finish("done-%d" % i, "done")
        store.create(job_id="still-running", mode="t2i", params={},
                     prompt_text="p", workflow=WORKFLOW)
        self.assertLessEqual(store.count(), 4)
        self.assertIsNotNone(store.get("still-running"))
        # 最旧的那批终态任务被裁掉了
        self.assertIsNone(store.get("done-0"))


class Forget(StoreBase):
    """P20j 的「清理」：把**挑好的**记录从账里去掉。

    与上面的 `Prune` 分工不同：那一条按条数裁最旧的（自动），这一条按名单删
    （用户点的那一下）。两者都会让记录**再也回不来**，所以判据都落在磁盘上。
    """

    def test_removes_the_record_from_memory_and_disk(self):
        self.add("keep-1")
        self.add("drop-1")
        self.store.finish("drop-1", "done")

        removed = self.store.forget(["drop-1"])

        self.assertEqual(removed, ["drop-1"])
        self.assertIsNone(self.store.get("drop-1"))
        self.assertFalse(os.path.exists(os.path.join(self.jobs_dir, "drop-1.json")))
        self.assertIsNotNone(self.store.get("keep-1"))
        # 重启一次也不能再看见它（文件真的没了，不是只在内存里藏起来）
        again = JobStore(jobs_dir=self.jobs_dir, runs_file=self.runs_file)
        self.assertIsNone(again.get("drop-1"))
        self.assertIsNotNone(again.get("keep-1"))

    def test_never_touches_a_running_job(self):
        """在跑的绝不动 —— 调用方已经挡过一道，这里再挡一道。"""
        self.add("run-1")
        self.store.mark_running("run-1")
        self.assertEqual(self.store.forget(["run-1"]), [])
        self.assertIsNotNone(self.store.get("run-1"))
        self.assertTrue(os.path.exists(os.path.join(self.jobs_dir, "run-1.json")))

    def test_unknown_and_empty_lists_are_counted_honestly(self):
        """回执是给用户看的账：没删掉的不算数，重复的只算一次。

        「还没终态的不算」这条在这里也要成立 —— `new_job` 造出来的记录默认是
        `queued`，它正是「在跑的绝不动」那种形态的另一种写法。
        """
        self.add("queued-1")
        self.add("done-1")
        self.store.finish("done-1", "done")
        self.assertEqual(self.store.forget(["nope"]), [])
        self.assertEqual(self.store.forget([]), [])
        self.assertEqual(self.store.forget(None), [])
        self.assertEqual(self.store.forget(["queued-1"]), [])
        self.assertEqual(self.store.forget(["done-1", "done-1"]), ["done-1"])
        self.assertEqual(self.store.count(), 1)

    def test_the_run_ledger_is_not_rewritten(self):
        """`runs.jsonl` 是历史账（画廊按文件名跟它对账）—— 清理**不动它**。

        划掉它等于让「这张图是哪次跑出来的」永远没有答案，而档案不该由
        「列表清理」来改写。
        """
        self.add("drop-2")
        self.store.finish("drop-2", "done")
        before = read_jsonl(self.runs_file)
        self.assertEqual(len(before), 1)
        self.store.forget(["drop-2"])
        self.assertEqual(read_jsonl(self.runs_file), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
