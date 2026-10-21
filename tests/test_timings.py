#!/usr/bin/env python3
"""`timings.py` —— 读探针报告、算采样拆分与覆盖率。

三条最容易错的断言在这里守着：

1. **不能重复计数。** `sampling.total_ms` 已经含在 KSampler 节点的 `compute_ms`
   里面了；再加一遍覆盖率就会 >100%，而那看起来「像是好事」。
2. **采样拆分是两层，不是一层。** 节点耗时 ≠ 纯去噪循环。实测差 24.9 ms
   （45456.3 vs 45431.4），差的那点是条件准备。不给出来就答不出「每步多久」。
3. **「探针没开」不是错误。** 报告里查不到 prompt_id 要如实说没有数据，
   而不是抛异常 —— 出图完全正常，只是没有耗时。
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

from workbench import config, timings  # noqa: E402

# 照**真实报告**抄的一块（P1 冒烟那次，seed 20260929 / 20 步 / 1024²）
REAL_BLOCK = {
    "started_at": 1790687641.5,
    "ended_at": 1790687690.6,
    "wall_ms": 49080.922699999064,
    "t0": 29043.8559716,
    "node_types": {"13": "ResolutionSelector", "459:451": "UNETLoader",
                   "459:452": "TextEncodeQwenImage21", "459:453": "CLIPLoader",
                   "459:454": "VAELoader", "459:456": "EmptyLatentImage",
                   "459:457": "VAEDecode", "459:458": "KSampler",
                   "459:480": "QwenImage21Cache", "461": "SaveImageAdvanced"},
    "cached_nodes": ["13", "459:451", "459:453", "459:454", "459:480"],
    "nodes": {
        "459:458": {"class_type": "KSampler", "compute_ms": 45456.2983,
                    "calls": 1, "cached": False, "start_ms": 1680.4607,
                    "vram_used_mb": 18655.4},
        "459:452": {"class_type": "TextEncodeQwenImage21", "compute_ms": 2500.0,
                    "calls": 1, "cached": False, "start_ms": 100.0,
                    "vram_used_mb": 8900.0},
        "459:457": {"class_type": "VAEDecode", "compute_ms": 900.0, "calls": 1,
                    "cached": False, "start_ms": 47000.0,
                    "vram_used_mb": 18900.0},
        "461": {"class_type": "SaveImageAdvanced", "compute_ms": 82.0,
                "calls": 1, "cached": False, "start_ms": 48000.0,
                "vram_used_mb": 16500.0},
        "13": {"class_type": "ResolutionSelector", "compute_ms": 0.2982,
               "calls": 1, "cached": True, "start_ms": 924.67,
               "vram_used_mb": 1213.7},
    },
    "sampling": {"calls": 1, "steps": 20, "total_ms": 45431.3643},
}


def block(**over):
    merged = json.loads(json.dumps(REAL_BLOCK))
    merged.update(over)
    return merged


class SummarizeBasics(support.LeakCheckedTestCase):
    def test_none_and_garbage_return_none_or_empty(self):
        self.assertIsNone(timings.summarize(None))
        self.assertIsNone(timings.summarize("nope"))
        ok = timings.summarize({})
        self.assertTrue(ok["available"])
        self.assertEqual(ok["rows"], [])
        self.assertIsNone(ok["coverage_pct"])

    def test_rows_are_sorted_slowest_first(self):
        rows = timings.summarize(block())["rows"]
        self.assertEqual([r["node_id"] for r in rows],
                         ["459:458", "459:452", "459:457", "461", "13"])
        self.assertEqual(rows[0]["class_type"], "KSampler")

    def test_sampler_is_identified_by_class_not_by_node_id(self):
        """换个模板节点 id 就变了，硬编码 459:458 会失效。"""
        b = block()
        b["nodes"] = {"999": b["nodes"]["459:458"]}
        b["node_types"] = {"999": "KSampler"}
        rows = timings.summarize(b)["rows"]
        self.assertTrue(rows[0]["is_sampler"])
        self.assertEqual(rows[0]["node_id"], "999")

    def test_cached_flag_and_warm_start(self):
        s = timings.summarize(block())
        self.assertTrue(s["warm_start"])
        self.assertEqual(s["cached_count"], 5)
        by = {r["node_id"]: r for r in s["rows"]}
        self.assertTrue(by["13"]["cached"])
        self.assertFalse(by["459:458"]["cached"])


class NoDoubleCounting(support.LeakCheckedTestCase):
    """`sampling.total_ms` 已含在 KSampler 的 compute_ms 里，别再算一遍。"""

    def test_total_is_the_sum_of_node_compute_ms(self):
        s = timings.summarize(block())
        expected = sum(n["compute_ms"] for n in REAL_BLOCK["nodes"].values())
        self.assertAlmostEqual(s["total_ms"], round(expected, 1), places=1)

    def test_coverage_never_exceeds_100_on_a_consistent_block(self):
        s = timings.summarize(block())
        self.assertLessEqual(s["coverage_pct"], 100.0)
        self.assertAlmostEqual(s["coverage_pct"], 99.93, delta=0.5)

    def test_loop_ms_is_not_added_into_the_total(self):
        """把循环单独加进去会让总数虚增，而虚增的表现是**覆盖率 >100%** ——
        一个看起来像「好事」的症状。用差分证明 total 里没有它。"""
        s = timings.summarize(block())
        node_sum = sum(n["compute_ms"] for n in REAL_BLOCK["nodes"].values())
        self.assertAlmostEqual(s["total_ms"], round(node_sum, 1), places=1)
        self.assertNotAlmostEqual(
            s["total_ms"], round(node_sum + REAL_BLOCK["sampling"]["total_ms"], 1),
            places=1)

    def test_unaccounted_is_wall_minus_total(self):
        s = timings.summarize(block())
        self.assertAlmostEqual(s["unaccounted_ms"],
                               round(s["wall_ms"] - s["total_ms"], 1), places=1)


class SamplingSplit(support.LeakCheckedTestCase):
    def test_split_has_both_layers(self):
        sp = timings.summarize(block())["sampling"]
        self.assertAlmostEqual(sp["node_ms"], 45456.3, delta=0.5)
        self.assertAlmostEqual(sp["loop_ms"], 45431.4, delta=0.5)
        self.assertAlmostEqual(sp["node_overhead_ms"], 24.9, delta=1.0)

    def test_per_step_comes_from_the_loop_not_the_node(self):
        sp = timings.summarize(block())["sampling"]
        self.assertEqual(sp["steps"], 20)
        self.assertAlmostEqual(sp["per_step_ms"], 45431.3643 / 20, places=2)
        self.assertAlmostEqual(sp["per_step_ms"], 2271.57, delta=1.0)

    def test_no_steps_means_no_per_step_not_a_crash(self):
        sp = timings.summarize(block(sampling={"calls": 1, "total_ms": 100.0}))["sampling"]
        self.assertIsNone(sp["per_step_ms"])
        self.assertEqual(sp["loop_ms"], 100.0)

    def test_sampling_absent_is_handled(self):
        sp = timings.summarize(block(sampling={}))["sampling"]
        self.assertEqual(sp["loop_ms"], 0.0)
        self.assertIsNone(sp["per_step_ms"])
        # 没采到循环就不谈「节点内其他」—— 否则会把整个节点耗时说成开销
        self.assertIsNone(sp["node_overhead_ms"])

    def test_no_sampler_node_leaves_node_ms_none(self):
        b = block()
        b["nodes"] = {"461": b["nodes"]["461"]}
        sp = timings.summarize(b)["sampling"]
        self.assertIsNone(sp["node_ms"])
        self.assertEqual(sp["loop_ms"], 45431.4)   # 循环还是报出来


class Robustness(support.LeakCheckedTestCase):
    def test_malformed_nodes_are_skipped(self):
        s = timings.summarize({"wall_ms": 100.0,
                               "nodes": {"a": None, "b": "x", "c": 3,
                                         "d": {"class_type": "KSampler",
                                               "compute_ms": 50.0}}})
        self.assertEqual([r["node_id"] for r in s["rows"]], ["d"])

    def test_nodes_not_a_dict_is_handled(self):
        s = timings.summarize({"wall_ms": 100.0, "nodes": "nope"})
        self.assertEqual(s["rows"], [])

    def test_bool_is_not_treated_as_a_number(self):
        """`True` 是 int 的子类，不加判断会算成 1 ms 的耗时。"""
        s = timings.summarize({"wall_ms": 100.0,
                               "nodes": {"a": {"class_type": "X",
                                               "compute_ms": True}}})
        self.assertEqual(s["rows"][0]["compute_ms"], 0.0)

    def test_bad_wall_is_handled(self):
        s = timings.summarize({"wall_ms": None, "nodes": {}})
        self.assertIsNone(s["coverage_pct"])
        self.assertIsNone(s["wall_ms"])

    def test_cached_nodes_garbage(self):
        s = timings.summarize({"wall_ms": 1.0, "nodes": {},
                               "cached_nodes": [1, None, "ok"]})
        self.assertEqual(s["cached_nodes"], ["ok"])
        self.assertTrue(s["warm_start"])


class ForJob(support.LeakCheckedTestCase):
    def test_unknown_prompt_is_unavailable_not_an_error(self):
        out = timings.for_job("nope", report={"a": REAL_BLOCK})
        self.assertFalse(out["available"])
        self.assertIn("没有这次任务", out["reason"])
        self.assertEqual(out["known_prompts"], 1)

    def test_no_prompt_id(self):
        self.assertFalse(timings.for_job(None)["available"])
        self.assertFalse(timings.for_job("")["available"])

    def test_found_prompt_returns_the_summary(self):
        out = timings.for_job("p1", report={"p1": REAL_BLOCK})
        self.assertTrue(out["available"])
        self.assertAlmostEqual(out["coverage_pct"], 99.93, delta=0.5)
        self.assertIn("report_path", out)

    def test_report_that_is_not_a_dict(self):
        out = timings.for_job("p1", report=None)   # 读不到 -> 空报告
        self.assertFalse(out["available"])


class ProbeState(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-probe-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "probe_state.json")

    def write(self, payload):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_missing_file_means_not_known(self):
        st = timings.probe_state(os.path.join(self.tmp, "nope.json"))
        self.assertFalse(st["known"])
        self.assertFalse(st["alive"])
        self.assertIn("COMFY_NODE_PROBE", st["note"])

    def test_alive_pid_is_reported_alive(self):
        self.write({"probe": True, "pid": os.getpid(), "boot_ts": 1.0,
                    "report_path": "x.json"})
        st = timings.probe_state(self.path)
        self.assertTrue(st["known"])
        self.assertTrue(st["alive"])
        self.assertIsNone(st["note"])

    def test_stale_file_with_dead_pid_is_not_alive(self):
        """文件在 ≠ 探针在。进程死掉后状态文件会留下来，必须判活。"""
        self.write({"probe": True, "pid": 999999, "boot_ts": 1.0,
                    "report_path": "x.json"})
        st = timings.probe_state(self.path)
        self.assertTrue(st["known"])
        self.assertFalse(st["alive"])
        self.assertIn("已经不在了", st["note"])

    def test_not_a_probe_file(self):
        self.write({"something": "else"})
        self.assertFalse(timings.probe_state(self.path)["known"])

    def test_broken_json_is_not_known(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(timings.probe_state(self.path)["known"])


class ReportOverview(support.LeakCheckedTestCase):
    def test_missing_report_is_zero_not_an_error(self):
        ov = timings.report_overview(os.path.join(HERE, "definitely-missing.json"))
        self.assertEqual(ov["captured"], 0)
        self.assertEqual(ov["prompt_ids"], [])
        self.assertIsNone(ov["report_mtime"])

    def test_counts_prompts(self):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-ov-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "r.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"a": REAL_BLOCK, "b": REAL_BLOCK}, f)
        ov = timings.report_overview(path)
        self.assertEqual(ov["captured"], 2)
        self.assertEqual(sorted(ov["prompt_ids"]), ["a", "b"])
        self.assertIsNotNone(ov["report_mtime"])


class RealReport(support.LeakCheckedTestCase):
    """对这台机器上真实的 node_probe_report.json 做一次只读冒烟。"""

    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.path = config.PROBE_REPORT
        if not os.path.exists(self.path):
            self.skipTest("没有真实的探针报告")

    def test_real_blocks_summarize_consistently(self):
        report = timings.load_report(self.path)
        if not report:
            self.skipTest("探针报告是空的")
        checked = 0
        asserted = 0
        for pid in report:
            out = timings.for_job(pid, report=report)
            self.assertTrue(out["available"], pid)
            wall = out["wall_ms"]
            if not wall:
                continue
            checked += 1
            # 覆盖率这条**只在墙钟被节点计算主导的块上**才成立。墙钟太短的块里，
            # 时间花在队列、进程与网络开销上，覆盖率对它没有意义。实测三种这样的块
            # （全都是**正确的读数**，不是「漏了节点」）：
            #   ① 节点缓存全中：Σcompute 精确为 0，覆盖率就是 0%；
            #   ② 2026-10-02（P20g）起的新形态：上游全在缓存里、**只有收尾节点重跑**
            #      （它的前缀每次都不一样，见 `qwen21_build.unique_save_prefix`），
            #      墙钟约 1 秒、Σcompute 一两百毫秒；
            #   ③ 探针自己那两张极简的图（EmptyImage → SaveImageAdvanced）。
            # 真跑一次的块远远超过这个门槛：1024² 出图 60~110 秒、VOSR2 14~30 秒。
            # 把短块当成「漏了节点」，这条用例就会长期报假警，而假警的下场是被加 skip
            # 绕过去（见 support.real_output_pngs 那段）。
            if wall < 5000:
                continue
            asserted += 1
            # 覆盖率必须落在合理区间：>100% 说明重复计数，<50% 说明漏了节点
            self.assertGreater(out["coverage_pct"], 50.0, pid)
            self.assertLessEqual(out["coverage_pct"], 100.5, pid)
            # 采样循环不可能比 KSampler 节点还长
            if out["sampling"]["node_ms"] is not None:
                self.assertLessEqual(out["sampling"]["loop_ms"],
                                     out["sampling"]["node_ms"] + 0.5, pid)
        if not checked:
            self.skipTest("报告里没有带 wall_ms 的块")
        self.assertGreater(asserted, 0,
                           "有 wall_ms 的块里没有一块真的算了非收尾节点 "
                           "—— 这条断言等于没跑")


if __name__ == "__main__":
    unittest.main(verbosity=2)
