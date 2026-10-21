#!/usr/bin/env python3
"""网格扫描的纯逻辑 —— 展开、去重、上限、每步耗时的来源。

这里**不碰后端、不碰 GPU**：扫描最贵的错是「排了 12 张才发现该是 3 张」，
而那一类错全在纯函数里，用毫秒级单测钉死就够了。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from workbench import sweep  # noqa: E402
import support  # noqa: E402  (测试基类 / 临时目录)


class ParseGrid(support.LeakCheckedTestCase):
    def test_three_seeds_one_axis(self):
        """判据 5 要的正是这一种：3 个 seed 排一排。"""
        plan = sweep.parse_grid({"seed": [11, 22, 33]})
        self.assertEqual(plan["count"], 3)
        self.assertEqual([c["seed"] for c in plan["cells"]], [11, 22, 33])
        # 没给 steps 轴 → 该格不带 steps，表示「沿用基准里的原值」
        self.assertEqual([c["steps"] for c in plan["cells"]], [None, None, None])

    def test_cartesian_product(self):
        plan = sweep.parse_grid({"seed": [1, 2], "steps": [10, 20]})
        self.assertEqual(plan["count"], 4)
        # steps 外层、seed 内层：同一组 steps 的几个 seed 挨在一起，像一组对照
        self.assertEqual([(c["steps"], c["seed"]) for c in plan["cells"]],
                         [(10, 1), (10, 2), (20, 1), (20, 2)])

    def test_single_number_is_accepted_as_a_one_axis(self):
        """别逼用户为了一元写方括号；表单里那一格顺手填个数字是常事。"""
        plan = sweep.parse_grid({"seed": 12345})
        self.assertEqual(plan["cells"], [{"seed": 12345, "steps": None}])

    def test_strings_are_coerced(self):
        """表单来的东西天生是字符串。"""
        plan = sweep.parse_grid({"seed": ["7", "8"], "steps": ["5"]})
        self.assertEqual([(c["seed"], c["steps"]) for c in plan["cells"]],
                         [(7, 5), (8, 5)])

    def test_duplicates_are_dropped_but_reported(self):
        """重复项不是错误，是**浪费** —— 2048² 一张 4~13 分钟。

        去掉，但要说出来：静默改用户给的东西是另一种撒谎。
        """
        plan = sweep.parse_grid({"seed": [1, 1, 2, 2, 2]})
        self.assertEqual([c["seed"] for c in plan["cells"]], [1, 2])
        self.assertEqual(plan["dropped_dupes"], 3)

    def test_two_axes_with_missing_key(self):
        plan = sweep.parse_grid({"steps": [20]})
        self.assertEqual(plan["cells"], [{"seed": None, "steps": 20}])


class ParseGridRejects(support.LeakCheckedTestCase):
    def bad(self, grid, max_jobs=None):
        with self.assertRaises(sweep.GridError) as ctx:
            sweep.parse_grid(grid, max_jobs)
        return str(ctx.exception)

    def test_not_a_dict(self):
        self.assertIn("grid 必须是对象", self.bad([1, 2, 3]))

    def test_empty_grid(self):
        self.assertIn("至少要给一条轴", self.bad({}))

    def test_empty_axis_alone_is_not_enough(self):
        """`{"seed": []}` 是「什么都没说」，不是「用默认 seed」。"""
        self.assertIn("至少要给一条轴", self.bad({"seed": []}))

    def test_unknown_keys_do_not_count_as_an_axis(self):
        self.assertIn("至少要给一条轴", self.bad({"cfg": [1.0, 2.0]}))

    def test_non_integer(self):
        self.assertIn("必须是整数", self.bad({"seed": ["abc"]}))
        self.assertIn("必须是整数", self.bad({"seed": [1.5]}))
        self.assertIn("必须是整数", self.bad({"seed": [None]}))
        self.assertIn("必须是整数", self.bad({"seed": [True]}))

    def test_empty_string(self):
        self.assertIn("不能是空字符串", self.bad({"seed": [""]}))

    def test_steps_bounds(self):
        self.assertIn("steps 要在 1..", self.bad({"steps": [0]}))
        self.assertIn("steps 要在 1..", self.bad({"steps": [-1]}))
        self.assertIn("steps 要在 1..", self.bad({"steps": [sweep.MAX_STEPS + 1]}))

    def test_seed_bounds(self):
        self.assertIn("seed 要在 0..", self.bad({"seed": [-1]}))
        self.assertIn("seed 要在 0..",
                      self.bad({"seed": [sweep.MAX_SEED + 1]}))

    def test_steps_one_and_max_are_allowed(self):
        self.assertEqual(sweep.parse_grid({"steps": [1]})["count"], 1)
        self.assertEqual(
            sweep.parse_grid({"steps": [sweep.MAX_STEPS]})["count"], 1)

    def test_over_the_cap_says_how_many_and_what_to_do(self):
        msg = self.bad({"seed": list(range(20))})
        self.assertIn("20 张", msg)
        self.assertIn("上限 12", msg)
        self.assertIn("4~13 分钟", msg)     # 顺带提醒代价，别只报个数

    def test_cap_is_configurable(self):
        self.assertEqual(
            sweep.parse_grid({"seed": [1, 2, 3]}, max_jobs=3)["count"], 3)
        self.assertIn("上限 2", self.bad({"seed": [1, 2, 3]}, max_jobs=2))

    def test_a_big_max_jobs_is_clamped_not_rejected(self):
        """上限**只收紧不放开**：要 1000 张时答复是「最多 64」而不是报错。

        夹住比报错更贴合意图 —— 调用方想「尽量多排」，我们能给的最多就这么多。
        但夹住必须**在错误信息里看得见**，否则就成了静默改行为。
        """
        # 2 张远没到硬上限，所以夹到 64 之后照样通过
        plan = sweep.parse_grid({"seed": [1, 2]}, max_jobs=1000)
        self.assertEqual(plan["count"], 2)
        # 真的超过硬上限时，报出来的数字是 64（夹住之后的），不是用户要的 1000
        msg = self.bad({"seed": list(range(100))}, max_jobs=1000)
        self.assertIn("上限 %d" % sweep.HARD_MAX_JOBS, msg)
        self.assertNotIn("上限 1000", msg)

    def test_default_cap_is_the_spec_value(self):
        self.assertEqual(sweep.MAX_JOBS, 12)     # spec §6.2 的 max_jobs 默认值
        self.assertEqual(
            sweep.parse_grid({"seed": list(range(12))})["count"], 12)

    def test_max_jobs_must_be_positive(self):
        self.assertIn("max_jobs 必须 ≥ 1", self.bad({"seed": [1]}, max_jobs=0))

    def test_dedupe_happens_before_the_cap_check(self):
        """`[1,1,1,1]` 是 1 张不是 4 张 —— 上限不该被重复项撑爆。"""
        plan = sweep.parse_grid({"seed": [7, 7, 7, 7]}, max_jobs=1)
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["dropped_dupes"], 3)


class Expand(support.LeakCheckedTestCase):
    def base(self, **over):
        body = {"mode": "t2i", "prompt": "a green apple", "steps": 20,
                "width": 1024, "height": 1024, "seed": 1, "cfg": 1.0,
                "grid": {"seed": [1, 2]}, "max_jobs": 12}
        body.update(over)
        return body

    def test_grid_and_sweep_fields_are_stripped(self):
        """`grid` / `max_jobs` 不是构建器的参数，也不该进 params。

        params 会被写进运行记录、被画廊读回来填表单 —— 里面混进分组信息
        会让「把这次出图填回表单」多出几个没人认识的字段。
        """
        bodies = sweep.expand(self.base(), [{"seed": 5, "steps": None}])
        self.assertEqual(len(bodies), 1)
        for gone in ("grid", "max_jobs", "sweep_id"):
            self.assertNotIn(gone, bodies[0])

    def test_upscale_refuses_a_steps_axis(self):
        """放大没有步数。放过一条 steps 轴，构建器会**默默忽略**它 ——
        N 格全得到差不多的图，而页面把它们并排摆成「参数对比」。
        这种没有症状的错必须在展开之前就炸掉。"""
        body = self.base(mode="upscale", prompt=None,
                         source_image="a.png", scale=2)
        with self.assertRaises(sweep.GridError) as ctx:
            sweep.expand(body, [{"seed": 1, "steps": 10}])
        self.assertIn("步数", str(ctx.exception))

    def test_h3_refuses_a_steps_axis(self):
        """★ H3 的步数是**固定档**（4 步蒸馏 LoRA），页面上根本没有那个旋钮。

        放过一条 steps 轴的话后端会把它丢掉 —— N 格跑的是**同一份参数**，
        而每格要 310 秒、5 分钟一轮，用户排 4 格白等 20 分钟只得到 4 段一样的视频。
        所以要在展开之前就炸，并说清该改用什么。
        """
        body = self.base(mode="h3", seconds=5, source_image="ref.png")
        with self.assertRaises(sweep.GridError) as ctx:
            sweep.expand(body, [{"seed": 1, "steps": 10}])
        self.assertIn("步数", str(ctx.exception))
        self.assertIn("seed", str(ctx.exception))

    def test_h3_allows_a_seed_axis(self):
        body = self.base(mode="h3", seconds=5, source_image="ref.png")
        bodies = sweep.expand(body, [{"seed": 7, "steps": None}])
        self.assertEqual(bodies[0]["seed"], 7)

    def test_layers_allows_both_axes(self):
        """拆层**有**步数（`num_inference_steps`），所以两条轴都该放行 ——
        照抄放大那条守卫会把它误伤成「这也不能扫」。"""
        body = self.base(mode="layers", prompt=None, source_image="a.png")
        bodies = sweep.expand(body, [{"seed": 7, "steps": 20}])
        self.assertEqual(bodies[0]["seed"], 7)
        self.assertEqual(bodies[0]["steps"], 20)

    def test_upscale_allows_a_seed_axis(self):
        """但扫 seed 是**有意义的** —— 实测换 seed 会改 73% 的像素。"""
        body = self.base(mode="upscale", prompt=None,
                         source_image="a.png", scale=2)
        bodies = sweep.expand(body, [{"seed": 1, "steps": None},
                                     {"seed": 2, "steps": None}])
        self.assertEqual([b["seed"] for b in bodies], [1, 2])
        self.assertEqual(bodies[0]["scale"], 2)

    def test_each_cell_overrides_only_what_it_names(self):
        bodies = sweep.expand(self.base(),
                              [{"seed": 5, "steps": None},
                               {"seed": 6, "steps": 40},
                               {"seed": None, "steps": 8}])
        self.assertEqual(bodies[0], {"mode": "t2i", "prompt": "a green apple",
                                     "steps": 20, "width": 1024, "height": 1024,
                                     "seed": 5, "cfg": 1.0})
        # 给了 steps 就覆盖；只给 seed 的格子保留原来那 20 步
        self.assertEqual(bodies[1]["seed"], 6)
        self.assertEqual(bodies[1]["steps"], 40)
        # 只给 steps 的格子保留原来那个 seed
        self.assertEqual(bodies[2]["seed"], 1)
        self.assertEqual(bodies[2]["steps"], 8)

    def test_the_original_body_is_not_mutated(self):
        """扫描会展开成 N 份，改到原件上会让第 2 格拿到第 1 格的参数。"""
        body = self.base()
        sweep.expand(body, [{"seed": 99, "steps": None}])
        self.assertEqual(body["seed"], 1)
        self.assertIn("grid", body)

    def test_edit_mode_keeps_its_extra_fields(self):
        bodies = sweep.expand(
            self.base(mode="edit", source_image="ref.png", resolution=1024),
            [{"seed": 3, "steps": None}])
        self.assertEqual(bodies[0]["source_image"], "ref.png")
        self.assertEqual(bodies[0]["resolution"], 1024)
        self.assertEqual(bodies[0]["mode"], "edit")


class PerStep(support.LeakCheckedTestCase):
    """`s/步` 必须有**来源**，因为两种算法给出的不是同一个量。"""

    def test_prefers_the_probe_sampling_loop(self):
        timing = {"sampling": {"per_step_ms": 2449.38, "steps": 20}}
        got = sweep.per_step(59448, 20, timing)
        self.assertEqual(got["ms"], 2449.4)
        self.assertEqual(got["source"], "probe")

    def test_falls_back_to_wall_over_steps_and_says_so(self):
        got = sweep.per_step(59448, 20, None)
        self.assertEqual(got["ms"], 2972.4)
        # 关键：**不能**冒充探针读数。这个数含加载与解码，比真实步耗时大
        self.assertEqual(got["source"], "wall_over_steps")
        self.assertIn("偏大", sweep.label(got["source"]))

    def test_probe_without_sampling_falls_back(self):
        got = sweep.per_step(59448, 20, {"available": False})
        self.assertEqual(got["source"], "wall_over_steps")

    def test_mismatched_step_count_refuses_to_use_the_probe(self):
        """步数对不上说明这条 timing 不是这一格的 —— 宁可不给，也别给错的。"""
        timing = {"sampling": {"per_step_ms": 2449.38, "steps": 40}}
        got = sweep.per_step(59448, 20, timing)
        self.assertEqual(got["source"], "wall_over_steps")

    def test_no_wall_ms_yet(self):
        got = sweep.per_step(None, 20, None)
        self.assertIsNone(got["ms"])
        self.assertIsNone(got["source"])

    def test_zero_steps_does_not_divide_by_zero(self):
        got = sweep.per_step(1000, 0, None)
        self.assertIsNone(got["ms"])

    def test_labels_are_human_readable(self):
        self.assertIn("探针", sweep.label("probe"))
        self.assertIn("偏大", sweep.label("wall_over_steps"))
        self.assertIn("未知", sweep.label("something-else"))


if __name__ == "__main__":
    unittest.main()
