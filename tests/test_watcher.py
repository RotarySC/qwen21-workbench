#!/usr/bin/env python3
"""`watcher.py` —— 事件归一、单一收尾路径、轮询对账。

这里守着 P1 最重要的一条设计：**收尾只有一条路径**（`_finish_from_history`）。
WS 的成功消息只是提前触发它，轮询永远在兜底。所以「WS 事件丢了会不会状态
卡住」这个问题，答案必须是「不会」—— 下面 `PollReconcile` 那组就是验这个。
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
from workbench.util import now_ms  # noqa: E402
from workbench.watcher import Watcher  # noqa: E402

WORKFLOW = {"459:458": {"class_type": "KSampler"},
            "459:451": {"class_type": "UNETLoader"},
            "461": {"class_type": "SaveImageAdvanced"}}


class WatcherBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-watch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(jobs_dir=os.path.join(self.tmp, "jobs"),
                              runs_file=os.path.join(self.tmp, "runs.jsonl"))
        self.broker = support.RecordingBroker()
        self.client = support.FakeComfy()
        self.w = Watcher(self.store, self.broker, self.client,
                         client_id="test-cid")

    def add(self, jid="job-1", **kw):
        return self.store.create(job_id=jid, mode="t2i", params={"steps": 20},
                                 prompt_text="a cat", workflow=WORKFLOW, **kw)

    def send(self, obj):
        import json
        self.w.handle_raw(json.dumps(obj))


class EventNormalisation(WatcherBase):
    def test_execution_start_marks_running_and_announces(self):
        self.add("j1")
        self.send({"type": "execution_start", "data": {"prompt_id": "j1"}})
        rec = self.store.get("j1")
        self.assertEqual(rec["status"], "running")
        self.assertIsNotNone(rec["started_at"])
        self.assertEqual(self.broker.of("job_started")[0]["job_id"], "j1")

    def test_foreign_job_is_announced_not_adopted(self):
        """从 ComfyUI 自带 UI 提交的任务不属于我们，但要告诉页面「后端正忙」，
        否则用户会以为自己的任务卡住了。"""
        self.send({"type": "execution_start", "data": {"prompt_id": "someone-else"}})
        self.assertEqual(self.broker.of("external_job")[0]["prompt_id"], "someone-else")
        self.assertIsNone(self.store.get("someone-else"))
        self.assertEqual(self.broker.of("job_started"), [])

    def test_executing_gives_node_and_class_type(self):
        self.add("j2")
        self.send({"type": "executing",
                   "data": {"node": "459:458", "prompt_id": "j2"}})
        ev = self.broker.of("node")[0]
        self.assertEqual(ev["node_id"], "459:458")
        self.assertEqual(ev["class_type"], "KSampler")
        self.assertEqual(self.store.get("j2")["node_id"], "459:458")

    def test_executing_with_null_node_is_not_a_completion(self):
        """`main.py:374` 用 node=None 表示「队列空了」，不是「这个任务完成了」。
        把它当完成会提前收尾。"""
        self.add("j3")
        self.send({"type": "executing", "data": {"node": None, "prompt_id": "j3"}})
        self.assertEqual(self.broker.events, [])
        self.assertEqual(self.store.get("j3")["status"], "queued")

    def test_progress_updates_store_and_stream(self):
        self.add("j4")
        self.send({"type": "progress",
                   "data": {"value": 7, "max": 20, "node": "459:458",
                            "prompt_id": "j4"}})
        ev = self.broker.of("progress")[0]
        self.assertEqual((ev["value"], ev["max"]), (7, 20))
        self.assertEqual(self.store.get("j4")["progress"], {"value": 7, "max": 20})

    def test_cached_marks_warm_start(self):
        self.add("j5")
        self.send({"type": "execution_cached",
                   "data": {"nodes": ["459:451", "459:453"], "prompt_id": "j5"}})
        self.assertEqual(self.store.get("j5")["cached_nodes"],
                         ["459:451", "459:453"])
        self.assertEqual(self.broker.of("cached")[0]["nodes"],
                         ["459:451", "459:453"])

    def test_executed_collects_output_without_duplicates(self):
        self.add("j6")
        payload = {"type": "executed", "data": {
            "node": "461", "prompt_id": "j6",
            "output": {"images": [{"filename": "a.png", "subfolder": "",
                                   "type": "output"}]}}}
        self.send(payload)
        self.send(payload)
        self.assertEqual([o["filename"] for o in self.store.get("j6")["outputs"]],
                         ["a.png"])

    def test_status_reports_queue_remaining(self):
        self.send({"type": "status", "data": {"status": {
            "exec_info": {"queue_remaining": 3}, "sid": "x"}}})
        self.assertEqual(self.broker.of("status")[0]["queue_remaining"], 3)

    def test_unknown_type_is_ignored(self):
        self.send({"type": "brand.new.event", "data": {"x": 1}})
        self.assertEqual(self.broker.events, [])

    def test_garbage_is_ignored(self):
        for raw in ["", "not json", "[1,2,3]", b"\x89PNG\r\n\x1a\n"]:
            self.w.handle_raw(raw)
        self.assertEqual(self.broker.events, [])


class SingleFinishPath(WatcherBase):
    def test_history_done_finishes_with_official_wall_ms(self):
        self.add("f1")
        self.client.history_map["f1"] = support.history_entry(
            "f1", start=1000, end=51000)
        self.send({"type": "execution_success", "data": {"prompt_id": "f1"}})
        rec = self.store.get("f1")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["wall_ms"], 50000)
        self.assertEqual(rec["outputs"][0]["filename"], "Qwen_image_2.1_00042.png")
        self.assertEqual(self.broker.of("job_done")[0]["wall_ms"], 50000)

    def test_history_error_finishes_with_message(self):
        self.add("f2")
        self.client.history_map["f2"] = support.history_entry(
            "f2", error={"message": "CUDA out of memory",
                         "node_type": "VAEDecode"})
        self.send({"type": "execution_error", "data": {
            "prompt_id": "f2", "node_id": "459:457",
            "exception_message": "CUDA out of memory"}})
        rec = self.store.get("f2")
        self.assertEqual(rec["status"], "error")
        self.assertEqual(rec["error"]["node_type"], "VAEDecode")
        self.assertEqual(self.broker.of("job_error")[0]["message"],
                         "CUDA out of memory")

    def test_interrupt_finishes_as_cancelled(self):
        self.add("f3")
        self.client.history_map["f3"] = support.history_entry("f3", interrupted=True)
        self.send({"type": "execution_interrupted", "data": {"prompt_id": "f3"}})
        self.assertEqual(self.store.get("f3")["status"], "cancelled")
        self.assertEqual(self.broker.of("job_cancelled")[0]["job_id"], "f3")

    def test_success_while_history_not_ready_yet_leaves_it_running(self):
        """history 条目是在 prompt_worker 里补写的，可能晚一拍。此时**不能**
        瞎收尾 —— 保持 running，让轮询接手。"""
        self.add("f4")
        self.send({"type": "execution_success", "data": {"prompt_id": "f4"}})
        self.assertEqual(self.store.get("f4")["status"], "queued")
        self.assertEqual(self.broker.of("job_done"), [])

    def test_finishing_twice_records_once(self):
        self.add("f5")
        self.client.history_map["f5"] = support.history_entry("f5")
        self.send({"type": "execution_success", "data": {"prompt_id": "f5"}})
        self.assertFalse(self.w._finish_from_history("f5"))
        self.assertEqual(len(self.broker.of("job_done")), 1)

    def test_wait_finish_returns_true_when_history_available(self):
        self.add("f6")
        self.client.history_map["f6"] = support.history_entry("f6")
        self.assertTrue(self.w.wait_finish("f6", timeout=1.0))
        self.assertEqual(self.store.get("f6")["status"], "done")

    def test_wait_finish_times_out_without_history(self):
        self.add("f7")
        self.assertFalse(self.w.wait_finish("f7", timeout=0.3, interval=0.05))


class ChainHook(WatcherBase):
    """出图后自动放大的**钩子**：只在打开时喊一声，喊坏了也不许影响收尾。

    链本身（搬产物、提交第二个作业）在 `test_chain.py` 里验；这里只守这个接口 ——
    它是 watcher 与 server 之间唯一的那条缝。
    """

    def setUp(self):
        super().setUp()
        self.calls = []
        self.w.on_done = self.calls.append

    def add_with_chain(self, jid, enabled, scale=2):
        return self.store.create(
            job_id=jid, mode="t2i",
            params={"steps": 20,
                    "auto_upscale": {"enabled": enabled, "scale": scale}},
            prompt_text="a cat", workflow=WORKFLOW)

    def finish(self, jid):
        self.client.history_map[jid] = support.history_entry(jid)
        self.w._finish_from_history(jid)
        return self.store.get(jid)

    def test_disabled_chain_is_silent(self):
        self.add_with_chain("c1", enabled=False)
        self.finish("c1")
        self.assertEqual(self.calls, [])

    def test_a_record_without_the_key_is_silent(self):
        """老记录里根本没有这个键 —— 不许把它当成开着的（键缺失 ≠ True）。"""
        self.add("c2")               # params 只有 steps
        self.finish("c2")
        self.assertEqual(self.calls, [])

    def test_enabled_chain_calls_the_hook_once_and_the_job_still_finishes(self):
        self.add_with_chain("c3", enabled=True, scale=3)
        rec = self.finish("c3")
        self.assertEqual(self.calls, ["c3"])
        self.assertEqual(rec["status"], "done")
        self.assertEqual(self.w.stats.get("chains_started"), 1)

    def test_a_broken_hook_does_not_break_the_finish(self):
        """钩子抛异常必须被吞掉：链坏了是链的事，收尾失败就变成一个永远
        收不了尾的**成功**作业 —— 那比链坏严重得多。"""
        def boom(pid):
            raise RuntimeError("搬产物炸了")

        self.w.on_done = boom
        self.add_with_chain("c4", enabled=True)
        rec = self.finish("c4")
        self.assertEqual(rec["status"], "done")
        self.assertIn("搬产物炸了", self.w.stats["last_chain_error"])
        self.assertEqual(len(self.broker.of("job_done")), 1)


class PollReconcile(WatcherBase):
    def test_job_in_queue_is_marked_running(self):
        self.add("p1")
        self.client.queue["p1"] = "running"
        self.w._poll()
        self.assertEqual(self.store.get("p1")["status"], "running")

    def test_pending_job_stays_queued(self):
        self.add("p2")
        self.client.queue["p2"] = "pending"
        self.w._poll()
        self.assertEqual(self.store.get("p2")["status"], "queued")

    def test_vanished_job_is_reaped_after_grace(self):
        """后端重启会让队列和历史一起清空 —— 这时任务不是「还在跑」，
        而是永远等不到了。必须收尾成错误，而不是永远挂着。"""
        self.add("p3")
        self.store.patch("p3", created_at=now_ms() - 60000)
        self.w._poll()
        rec = self.store.get("p3")
        self.assertEqual(rec["status"], "error")
        self.assertEqual(rec["error"]["code"], "lost")
        self.assertEqual(self.broker.of("job_error")[0]["code"], "lost")

    def test_young_missing_job_is_not_reaped(self):
        """提交到进 /queue 之间有个窗口，宽限期内不许误杀。"""
        self.add("p4")
        self.w._poll()
        self.assertEqual(self.store.get("p4")["status"], "queued")

    def test_poll_finishes_job_whose_ws_events_were_lost(self):
        """WS 被别的客户端抢走时事件全丢 —— 轮询必须独立把任务收掉。"""
        self.add("p5")
        self.client.history_map["p5"] = support.history_entry("p5", start=10, end=20)
        self.w._poll()
        rec = self.store.get("p5")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["wall_ms"], 10)

    def test_poll_counts_itself(self):
        self.add("p6")
        self.w._poll()
        self.assertEqual(self.w.stats["polls"], 1)

    def test_unreachable_backend_does_not_reap_or_crash(self):
        self.add("p7")
        self.store.patch("p7", created_at=now_ms() - 60000)
        self.client.queue_ids = lambda: None
        self.w._poll()
        self.assertEqual(self.store.get("p7")["status"], "queued")


class State(WatcherBase):
    def test_state_reports_identity_and_counters(self):
        st = self.w.state()
        self.assertEqual(st["client_id"], "test-cid")
        self.assertIn("ws_connected", st)
        self.assertIn("stats", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
