#!/usr/bin/env python3
"""`wfprobe.py` 与 `sweep_tool.py` 里**不需要后端**的那些部分。

这一批用例的重点不是覆盖率，而是把这一轮真栽过的三件事钉死：

1. **`diff_stats` 的两个口径不是同义词。** 同一对图，`byte_*` 与 `pixel_*`
   可以差出几十个百分点（真产物上实测 54.839% 对 94.758%），而两张表看起来
   都「正常」。所以这里用一个**只有一个通道变了 1** 的合成图把两个数分别钉住，
   —— 而不是拿真实产物去比（那会让用例依赖"磁盘上恰好有哪张图"，
   正是这一轮在 `test_pngpix` 里刚修掉的那种脆弱）。
   顺带钉住第三个陷阱：**ComfyUI 产物的 alpha 并不恒为 255**（实测 253/254/255），
   所以「alpha 变了、颜色没变」时 `byte_pct` 有读数而 `pixel_pct` 是 0。

2. **「我传的」不一定等于「它自报的」。** 实测 `reuse_threshold=0.0` 被改写成
   0.018，而 `0.001`/`0.017` 原样保留 —— 那条对账就是靠这里这几个函数做的。

3. **接不上要当场炸。** 扫描是成批跑的，一格没接上就是一条假结论。

图全部**现场合成**，不碰 `output/` 里的真实产物。
"""
import os
import struct
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pngpix                 # noqa: E402
import support                # noqa: E402
import wfprobe                # noqa: E402

import sweep_tool             # noqa: E402  (要 httpx；预设与日志解析在这里)


# ---------------------------------------------------------------------------
# 合成图
# ---------------------------------------------------------------------------
def _chunk(ctype, body):
    return (struct.pack(">I", len(body)) + ctype + body
            + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))


def meta_png(workflow_json):
    """一张只有 tEXt + IEND 的最小 PNG —— `pngmeta` 只读文本块。"""
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"tEXt", b"prompt\x00" + workflow_json.encode("latin-1"))
            + _chunk(b"IEND", b""))


def pixel_png(rows, color_type=2):
    """8 位、非隔行、过滤类型全 0 的 PNG。`rows` = 每像素的通道元组列表。"""
    h = len(rows)
    w = len(rows[0])
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw))
            + _chunk(b"IEND", b""))


def flat(w, h, value=0, color_type=2, alpha=255):
    ch = {0: 1, 2: 3, 6: 4}[color_type]
    row = tuple([value] * (ch - 1) + [alpha]) if color_type == 6 else tuple([value] * ch)
    return [[row for _ in range(w)] for _ in range(h)]


GRAPH = {
    "459:458": {"class_type": "KSampler",
                "inputs": {"steps": 20, "seed": 1, "model": ["459:480", 0]}},
    "459:480": {"class_type": "QwenImage21Cache",
                "inputs": {"device": "auto", "model": ["459:451", 0]}},
    "459:452": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": "x"}},
}


class ReferenceGraph(support.LeakCheckedTestCase):
    """底图必须从真实产物里取 —— 这条路的入口。"""

    def test_reads_the_graph_back_out_of_a_png(self):
        import json
        wf = wfprobe.reference_graph(meta_png(json.dumps(GRAPH)))
        self.assertEqual(wf["459:458"]["inputs"]["steps"], 20)
        self.assertEqual(wf["459:458"]["inputs"]["model"], ["459:480", 0])

    def test_returns_a_fresh_object_each_time(self):
        """取回来的图要能随便改 —— 上一格改过的底图不能渗到下一格。"""
        import json
        blob = meta_png(json.dumps(GRAPH))
        a = wfprobe.reference_graph(blob)
        a["459:458"]["inputs"]["steps"] = 99
        self.assertEqual(wfprobe.reference_graph(blob)["459:458"]["inputs"]["steps"], 20)

    def test_a_png_without_metadata_says_so(self):
        with self.assertRaises(wfprobe.ProbeError) as cm:
            wfprobe.reference_graph(pixel_png(flat(4, 4)))
        self.assertIn("prompt 元数据", str(cm.exception))

    def test_a_graph_without_the_sampler_is_not_this_pipeline(self):
        import json
        with self.assertRaises(wfprobe.ProbeError) as cm:
            wfprobe.reference_graph(meta_png(json.dumps({"1": {"class_type": "X"}})))
        self.assertIn("不是这条", str(cm.exception))


class InsertModelNode(support.LeakCheckedTestCase):
    """把 `cache → KSampler` 改成 `cache → 新节点 → KSampler`。"""

    def graph(self):
        import copy
        return copy.deepcopy(GRAPH)

    def test_rewires_the_edge(self):
        wf = self.graph()
        wfprobe.insert_model_node(wf, "9900", "SomePlugin", {"p": 1})
        self.assertEqual(wf["9900"]["inputs"]["model"], ["459:480", 0])
        self.assertEqual(wf["459:458"]["inputs"]["model"], ["9900", 0])

    def test_it_overrides_whatever_model_was_passed_in(self):
        """上游由调用方决定。允许 inputs 自带 model，就等于允许"接错上游还能跑"。"""
        wf = self.graph()
        wfprobe.insert_model_node(wf, "9900", "SomePlugin",
                                  {"model": ["459:451", 0], "p": 1})
        self.assertEqual(wf["9900"]["inputs"]["model"], ["459:480", 0])

    def test_refuses_to_overwrite_an_existing_node(self):
        import copy
        wf = copy.deepcopy(GRAPH)
        wf["9900"] = {"class_type": "Already", "inputs": {}}
        with self.assertRaises(wfprobe.ProbeError) as cm:
            wfprobe.insert_model_node(wf, "9900", "SomePlugin", {})
        self.assertIn("已经在图里", str(cm.exception))

    def test_refuses_when_the_edge_moved_and_names_what_it_found(self):
        """接不上的症状是「能出图、但一秒没省」—— 没有任何症状，所以必须炸。
        报错要带上它**实际**看到的上游，不然排不出来。"""
        import copy
        wf = copy.deepcopy(GRAPH)
        wf["459:458"]["inputs"]["model"] = ["990", 0]        # 已被 LazyCache 占住
        with self.assertRaises(wfprobe.ProbeError) as cm:
            wfprobe.insert_model_node(wf, "9900", "SomePlugin", {})
        msg = str(cm.exception)
        self.assertIn("['990', 0]", msg)
        self.assertIn("459:480", msg)

    def test_every_preset_cell_fits_the_base_graph(self):
        """预设里任何一格接不上，整轮扫描都会产出一条假结论。这条用例不需要后端。"""
        import copy
        self.assertTrue(sweep_tool.TE_SPEED_PRESET)
        for cell in sweep_tool.TE_SPEED_PRESET:
            with self.subTest(cell=cell.id):
                wf = copy.deepcopy(GRAPH)
                wfprobe.insert_model_node(wf, cell.node_id, cell.class_type,
                                          cell.inputs)
                self.assertEqual(wf[cell.node_id]["class_type"], cell.class_type)
                self.assertEqual(wf["459:458"]["inputs"]["model"],
                                 [cell.node_id, 0])


class EffectiveParams(support.LeakCheckedTestCase):
    """「我传的」对「它自报的」。"""

    def test_parses_a_real_config_line(self):
        # 这一行是从真后端日志里抄的，格式变了这条就会响
        line = ("[INFO] Qwen TE predictor config: threshold=0.060000 "
                "window=0.18-0.86 max_consecutive=1 refresh_interval=4 "
                "deviation_limit=0.0800 verbose=True")
        kv = wfprobe.parse_kv(line)
        self.assertEqual(kv["threshold"], 0.06)
        self.assertEqual(kv["deviation_limit"], 0.08)
        self.assertEqual(kv["max_consecutive"], 1.0)
        # 连字符是**值的一部分**，不能当分隔符 —— 拆错了这条阅读会变成两半
        self.assertEqual(kv["window"], "0.18-0.86")
        self.assertEqual(kv["verbose"], "True")

    def test_trailing_zeros_are_the_same_number(self):
        self.assertEqual(wfprobe.effective_mismatch({"reuse_threshold": 0.2},
                                                    {"reuse_threshold": 0.200000}), [])

    def test_the_zero_means_auto_trap(self):
        """实测：传 0.0 得到的生效值是 0.018，而 0.001 会原样保留。
        把 0.0 当「关掉」是那一轮差点犯的错。"""
        drift = wfprobe.effective_mismatch({"reuse_threshold": 0.0},
                                           {"reuse_threshold": 0.018})
        self.assertEqual(drift, [("reuse_threshold", 0.0, 0.018)])
        self.assertEqual(wfprobe.effective_mismatch({"reuse_threshold": 0.001},
                                                    {"reuse_threshold": 0.001}), [])

    def test_a_param_it_never_reported_is_also_a_mismatch(self):
        """它压根没报这一项也算 —— 「没报」不等于「生效了」。"""
        self.assertEqual(wfprobe.effective_mismatch({"x": 1}, {}), [("x", 1, None)])

    def test_non_numeric_values_compare_verbatim(self):
        self.assertEqual(wfprobe.effective_mismatch({"attention": "kitchen_int8"},
                                                    {"attention": "kitchen_int8"}), [])
        self.assertEqual(len(wfprobe.effective_mismatch(
            {"attention": "default"}, {"attention": "kitchen_int8"})), 1)


class TheTwoChangedPct(support.LeakCheckedTestCase):
    """这一轮把两个都叫 changed_pct 的量混用过一次 —— 这里把它们钉死。"""

    def diff(self, a, b):
        return wfprobe.diff_stats(pngpix.pixels(a), pngpix.pixels(b))

    def test_one_channel_of_one_pixel_is_not_the_same_fraction(self):
        """2×2 RGB，只有一个像素的一个通道差 1：
        逐字节 1/12 = 8.333%，逐像素 1/4 = 25% —— **差三倍**。"""
        a = flat(2, 2, 0)
        b = flat(2, 2, 0)
        b[0][0] = (1, 0, 0)
        d = self.diff(pixel_png(a), pixel_png(b))
        self.assertEqual(d["byte_pct"], round(100.0 / 12, 3))
        self.assertEqual(d["pixel_pct"], 25.0)
        self.assertNotEqual(d["byte_pct"], d["pixel_pct"])
        self.assertEqual(d["max_abs"], 1)
        self.assertEqual(d["max_abs_color"], 1)
        self.assertFalse(d["identical"])

    def test_three_channels_of_one_pixel_makes_them_agree(self):
        """同一个像素三个通道都变 → 两个口径碰巧相等。
        正因为**有时相等有时不等**，混用才不会立刻暴露。"""
        a = flat(2, 2, 0)
        b = flat(2, 2, 0)
        b[0][0] = (1, 1, 1)
        d = self.diff(pixel_png(a), pixel_png(b))
        self.assertEqual(d["byte_pct"], 25.0)
        self.assertEqual(d["pixel_pct"], 25.0)

    def test_alpha_dilutes_the_byte_mean_by_the_channel_count(self):
        """RGBA 且 alpha 恒定：逐字节均值 = 逐像素均值 × 3/4。
        真产物的 alpha **不恒为 255**（实测 253/254/255），所以这一项必须写死。"""
        a = flat(2, 2, 0, color_type=6, alpha=255)
        b = flat(2, 2, 0, color_type=6, alpha=255)
        b[0][0] = (1, 0, 0, 255)
        d = self.diff(pixel_png(a, 6), pixel_png(b, 6))
        self.assertEqual(d["byte_mean_abs"], round(1 / 16.0, 6))
        self.assertEqual(d["pixel_mean_abs"], round(1 / 12.0, 6))
        self.assertAlmostEqual(d["byte_mean_abs"], d["pixel_mean_abs"] * 3 / 4, places=6)

    def test_alpha_only_change_has_a_byte_reading_and_no_pixel_reading(self):
        """只有 alpha 变了：逐字节说"变了 6.25%"，逐像素说"一个颜色都没变"。
        真产物的 alpha 实测会在 253/254/255 之间抖 —— 这就是那份
        「变了 77%」的历史数字里混着与画面无关的东西的原因。"""
        a = flat(2, 2, 0, color_type=6, alpha=255)
        b = flat(2, 2, 0, color_type=6, alpha=255)
        b[0][0] = (0, 0, 0, 254)
        d = self.diff(pixel_png(a, 6), pixel_png(b, 6))
        self.assertEqual(d["byte_pct"], 6.25)
        self.assertEqual(d["byte_mean_abs"], round(1 / 16.0, 6))
        self.assertEqual(d["pixel_pct"], 0.0)
        self.assertEqual(d["pixel_mean_abs"], 0.0)
        self.assertEqual(d["max_abs"], 1)
        self.assertEqual(d["max_abs_color"], 0)
        self.assertFalse(d["identical"])      # 字节上确实不同

    def test_identical_short_circuits_to_exact_zeros(self):
        blob = pixel_png(flat(3, 3, 7))
        d = self.diff(blob, blob)
        self.assertTrue(d["identical"])
        for key in ("byte_pct", "byte_mean_abs", "pixel_pct", "pixel_mean_abs",
                    "max_abs", "max_abs_color"):
            self.assertEqual(d[key], 0, key)

    def test_size_mismatch_raises_instead_of_comparing_garbage(self):
        with self.assertRaises(pngpix.PngError):
            self.diff(pixel_png(flat(2, 2)), pixel_png(flat(3, 3)))

    def test_gray_images_use_the_single_channel(self):
        a = flat(2, 2, 0, color_type=0)
        b = flat(2, 2, 0, color_type=0)
        b[0][0] = (5,)
        d = self.diff(pixel_png(a, 0), pixel_png(b, 0))
        self.assertEqual(d["color_channels"], 1)
        self.assertEqual(d["pixel_pct"], 25.0)
        self.assertEqual(d["byte_pct"], 25.0)     # 灰度：像素与字节是同一个分母


class GradientEnergy(support.LeakCheckedTestCase):
    """判「细节有没有被抹平」的那把尺。"""

    def test_a_flat_image_has_none(self):
        self.assertEqual(wfprobe.gradient_energy(pngpix.pixels(
            pixel_png(flat(5, 5, 128)))), 0.0)

    def test_a_known_synthetic_value(self):
        """3×3 灰度，只有中间是 10：
        dx = (|10-0| + |0-10|) = 20，分母 (w-1)·h = 6
        dy = 同上 20，分母 w·(h-1) = 6   →  平均 20/6 = 3.3333"""
        rows = [[(0,), (0,), (0,)], [(0,), (10,), (0,)], [(0,), (0,), (0,)]]
        self.assertEqual(
            wfprobe.gradient_energy(pngpix.pixels(pixel_png(rows, 0))), 3.3333)

    def test_sharper_detail_scores_higher(self):
        """同一张图上「有细节」一定要比「被抹平」高 —— 这就是它的全部用途。"""
        detail = [[(0,), (200,), (0,), (200,)] for _ in range(4)]
        smooth = [[(100,), (100,), (100,), (100,)] for _ in range(4)]
        self.assertGreater(wfprobe.gradient_energy(pngpix.pixels(pixel_png(detail, 0))),
                           wfprobe.gradient_energy(pngpix.pixels(pixel_png(smooth, 0))))

    def test_tiny_images_do_not_blow_up(self):
        for rows in ([[(0,)]], [[(0,), (1,)]]):
            wfprobe.gradient_energy(pngpix.pixels(pixel_png(rows, 0)))


class TeSpeedLogParsing(support.LeakCheckedTestCase):
    """日志那几行是**机制证据** —— 说清「速度来自 attention 不是步预测」靠的就是它。

    文本是从真后端日志里抄的。插件改了格式，这里会响，而不是静默返回空字典
    （静默返回空的话，「那轮到底跳了几步」就没人知道了）。
    """

    REAL = (
        "[INFO] Qwen TE predictor decision: step=3 branch=x t=0.9 change=0.040337 "
        "threshold=0.060000 result=outside_window\n"
        "[INFO] Qwen TE predictor config: threshold=0.060000 window=0.18-0.86 "
        "max_consecutive=1 refresh_interval=4 deviation_limit=0.0800 verbose=True\n"
        "[INFO] Qwen TE predictor: predicted 2/20 steps "
        "(11.1% model-call acceleration)\n"
        "[INFO] Qwen TE predictor calls: diffusion_wrapper=20 full_steps=18\n"
        "[INFO] Qwen TE predictor decision summary: {'outside_window': 6, "
        "'eligible': 2, 'consecutive_limit': 2, 'latent_change': 10}\n"
    )

    def test_extracts_every_field_it_claims_to(self):
        got = sweep_tool.parse_te_speed_log(self.REAL)
        self.assertEqual(got["reported"]["threshold"], 0.06)
        self.assertEqual(got["reported"]["window"], "0.18-0.86")
        self.assertEqual(got["predicted"], 2)
        self.assertEqual(got["steps_seen"], 20)
        self.assertEqual(got["full_steps"], 18)
        self.assertIn("latent_change", got["summary"])
        self.assertEqual(got["cooldown_hits"], 0)
        self.assertEqual(got["deviation_hits"], 0)

    def test_counts_the_gates_that_never_fired(self):
        """`predictor_error_limit` 那一轮被证明无效，靠的就是这两个计数恒为 0。"""
        text = self.REAL + ("[INFO] ... result=cooldown\n"
                            "[INFO] ... result=deviation\n")
        got = sweep_tool.parse_te_speed_log(text)
        self.assertEqual(got["cooldown_hits"], 1)
        self.assertEqual(got["deviation_hits"], 1)

    def test_empty_input_is_an_empty_dict_not_an_exception(self):
        for bad in ("", None, "完全没有相关行"):
            got = sweep_tool.parse_te_speed_log(bad)
            self.assertNotIn("reported", got)
            self.assertNotIn("predicted", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
