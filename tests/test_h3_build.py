#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax H3 构建器（`h3_build.py`）的判据。

分四层：

  1. **基准图良构** —— `h3_api_base.json` 必须是那次实测 `success / 316.9 秒` 的
     那份 20 节点图（`generated/minimaxh3_browser_api.json` 的逐字副本）
  2. **冻结值** —— `115` ↔ `340` 一致（见 `h3_build` 模块文档）、4 步、LoRA 0.75、
     模型文件名、`SaveVideo` 的前缀
  3. **`build()` 的射程** —— 只动我传的那几处，其余逐字不变
  4. **反解与换算** —— `describe()` 往返、17 帧网格

    python tests/test_h3_build.py
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))          # …/qwen21-workbench/tests
REPO = os.path.dirname(HERE)                               # …/qwen21-workbench
WORKSPACE = os.path.dirname(REPO)                          # …/ComfyUI 运维现场
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # support.py 就在 tests/ 里

import h3_build as h3b          # noqa: E402
import project_paths            # noqa: E402
import qwen21_build as qb       # noqa: E402
import layers_build as lb       # noqa: E402
import support                  # noqa: E402

#: 那次跑通的图里出现的**全部**节点类。多一个少一个都说明基准图被换过了。
EXPECTED_CLASSES = {
    "LoadImage", "ResolutionSelector", "ImageResizeKJv2",
    "MiniMaxH3ImageToVideo", "UNETLoader", "CLIPLoader",
    "LoraLoaderModelOnly", "BasicScheduler", "KSamplerSelect",
    "BasicGuider", "SamplerCustomAdvanced", "RandomNoise",
    "ComfyMathExpression", "PrimitiveFloat",
    "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo",
    "VAELoader",
}

#: 基准图里应有的节点 id（不含 `_meta`）—— 刻意与生成脚本里那份表重合。
EXPECTED_IDS = {
    "114", "115", "121", "122", "123", "124", "125", "126", "127", "128",
    "129", "130", "131", "133", "134", "135", "140", "188", "189", "340",
}


def base():
    return h3b.load_base()


def _flatten(obj, prefix=""):
    """把嵌套结构压成 `路径 -> 标量`，用来精确断言「改了哪几处」。"""
    out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            out.update(_flatten(value, path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.update(_flatten(value, "%s[%d]" % (prefix, index)))
    else:
        out[prefix] = obj
    return out


_MISSING = object()


def changed_paths(before, after):
    a, b = _flatten(before), _flatten(after)
    keys = set(a) | set(b)
    return sorted(k for k in keys if a.get(k, _MISSING) != b.get(k, _MISSING))


class BaseGraph(support.LeakCheckedTestCase):
    """第 1 层：基准图就是那份被验证过的图。"""

    def test_base_file_is_where_the_builder_looks(self):
        """构建器用的那份 = **落点规则**算出来的那份（不是写死的某个路径）。

        规则只有一处（`project_paths.base_file()`）：环境变量 → 仓库自带的
        `workflows/` → 工作区。这样两种布局都对：克隆下来就能用，以及
        「图放别处、仓库只放代码」的部署。写死成工作区路径的话，**克隆下来的
        仓库里这条判据必红** —— 而那看起来像「仓库缺文件」。
        """
        self.assertEqual(
            h3b.BASE_FILE,
            project_paths.base_file("QWEN21_H3_BASE", "h3_api_base.json",
                                    repo=REPO, workspace=WORKSPACE))
        self.assertTrue(os.path.exists(h3b.BASE_FILE),
                        "缺少基准图：%s" % h3b.BASE_FILE)

    def test_node_count_and_classes(self):
        wf = base()
        self.assertEqual(len(wf), 20)
        self.assertEqual({n["class_type"] for n in wf.values()},
                         EXPECTED_CLASSES)

    def test_node_ids_are_the_verified_ones(self):
        # id **刻意不重排**：重排之后这份图就没法再与那次已验证的产物对 diff 了。
        self.assertEqual(set(base()), EXPECTED_IDS)

    def test_first_frame_is_a_placeholder_not_a_real_file(self):
        # 留一个看起来能跑的真实文件名，会让人以为不传首帧也能出图。
        name = base()["114"]["inputs"]["image"]
        self.assertEqual(name, "__first_frame__.png")


class FrozenValues(support.LeakCheckedTestCase):
    """第 2 层：本次不放开的那几项，改了要当场炸。"""

    def test_scheduler_steps_and_lora_strength(self):
        wf = base()
        # 4 步是 `lightx2v_turbo_4step` 蒸馏 LoRA 的固定档。
        self.assertEqual(wf["126"]["inputs"]["steps"], 4)
        self.assertEqual(wf["140"]["inputs"]["strength_model"], 0.75)

    def test_models_are_the_ones_that_ran(self):
        wf = base()
        self.assertEqual(wf["129"]["inputs"]["unet_name"],
                         "Minimax_H3\\minimax_h3_fl2va_pruned_int8_convrot.safetensors")
        self.assertEqual(wf["130"]["inputs"]["clip_name"],
                         "qwen3vl_32b_minimax_h3_int8_convrot.safetensors")
        self.assertEqual(wf["130"]["inputs"]["type"], "minimax")
        self.assertEqual(wf["140"]["inputs"]["lora_name"],
                         "minimax_h3\\minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors")

    def test_save_video_prefix_sits_in_the_video_subfolder(self):
        # 画廊只扫每个输出根目录的**顶层**，所以这个 `video/` 前缀是
        # 「H3 的视频不会进画廊」这条不变量的实现方式，不是随手起的名。
        self.assertEqual(base()["189"]["inputs"]["filename_prefix"],
                         "video/MiniMax_H3")

    def test_math_expression_is_verbatim(self):
        # 表达式在基准图里，帧数换算在 Python 里 —— 两处必须逐字一致，
        # 否则页面上显示的「实际多少帧」与服务端真正算出来的会分家。
        self.assertEqual(base()["134"]["inputs"]["expression"],
                         h3b.MATH_EXPRESSION)

    def test_resolution_selector_agrees_with_the_first_frame_resize(self):
        """★ 本次最重要的一条：`115` 与 `340` 是同一个尺寸的两处真源。

        不一致时画面会被裁两次，而**没有症状**（照样出一个 480×864 的视频，
        只是构图不是你以为的那个）。这里用 `ResolutionSelector` 的**真实算法**
        算一遍，再与 `ImageResizeKJv2` 的宽度和高度比。
        """
        wf = base()
        res = wf["115"]["inputs"]
        got = h3b.resolution_for(res["aspect_ratio"], res["megapixels"],
                                 res["multiple"])
        want = (wf["340"]["inputs"]["width"], wf["340"]["inputs"]["height"])
        self.assertEqual(got, want,
                         "ResolutionSelector 算出 %r，而 ImageResizeKJv2 裁成 %r"
                         % (got, want))
        # 顺手把那个具体数钉住：480×864 是 2026-10-10 那次跑的产物尺寸
        # （README 里记的「h264 480×864」），不是推导出来的。
        self.assertEqual(want, (480, 864))

    def test_resolution_for_uses_1024_squared_not_one_million(self):
        """算法里那个常数错了只会差一档，而差一档看不出来。

        `comfy_extras/nodes_resolution.py:82` 是 `megapixels * 1024 * 1024`。
        按 `* 1e6` 算会得到 480×832（`logs/wf_analyze3_out.txt` 里那个读数就是
        这么来的），于是这条判据就是区分两者的唯一东西。
        """
        self.assertEqual(h3b.resolution_for("9:16 (Portrait Widescreen)", 0.4, 32),
                         (480, 864))
        self.assertEqual(h3b.resolution_for("1:1 (Square)", 1.0, 8), (1024, 1024))

    def test_resolution_for_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            h3b.resolution_for("7:5 (Nonsense)", 1.0, 32)
        with self.assertRaises(ValueError):
            h3b.resolution_for("1:1 (Square)", 1.0, 0)
        with self.assertRaises(ValueError):
            h3b.resolution_for("1:1 (Square)", 1.0, True)


class DurationGrid(support.LeakCheckedTestCase):
    """17 帧网格：用户给的那个「秒」是近似值，产物里的那个才是真的。"""

    def test_five_seconds(self):
        self.assertEqual(h3b.duration_frames(5), 124)
        self.assertAlmostEqual(h3b.duration_seconds(5), 124 / 24.0, places=6)

    def test_ten_seconds(self):
        # ★ 10 秒这一档**从没跑过**（只实测过 5 秒）。这条判据钉的是**公式**，
        # 不是「它跑得动」—— 页面上的「未实测」标注要到真有读数才能去掉。
        self.assertEqual(h3b.duration_frames(10), 243)
        self.assertAlmostEqual(h3b.duration_seconds(10), 243 / 24.0, places=6)

    def test_none_falls_back_to_the_default(self):
        self.assertEqual(h3b.duration_frames(None), h3b.duration_frames(5))

    def test_frames_always_land_on_the_grid(self):
        for seconds in range(1, 31):
            with self.subTest(seconds=seconds):
                frames = h3b.duration_frames(seconds)
                self.assertGreaterEqual(frames, 5)
                self.assertEqual(frames % 17, 5 % 17)

    def test_out_of_range_rejected(self):
        for bad in (0, -1, h3b.MAX_SECONDS + 1):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    h3b.duration_frames(bad)
        with self.assertRaises(ValueError):
            h3b.duration_frames(True)


class BuildScope(support.LeakCheckedTestCase):
    """第 3 层：`build()` 的射程。"""

    def test_only_the_named_inputs_change(self):
        before = base()
        after = h3b.build(prompt="精灵女子抬头", first_frame="ref.png",
                          seconds=10, seed=777)
        self.assertEqual(
            changed_paths(before, after),
            ["114.inputs.image", "131.inputs.noise_seed", "133.inputs.prompt",
             "135.inputs.value"])

    def test_omitted_arguments_leave_the_base_alone(self):
        """三个可选项都为 None 时，只该动提示词与首帧。

        `seconds=None` 那个写死 5 秒的写法就是被这条抓住的：它会让「没表态」变成
        「把基准里那次实测用的值改掉」。
        """
        before = base()
        after = h3b.build(prompt="x", first_frame="y.png")
        self.assertEqual(changed_paths(before, after),
                         ["114.inputs.image", "133.inputs.prompt"])

    def test_first_frame_is_stripped(self):
        got = h3b.build(prompt="x", first_frame="  ref.png  ")
        self.assertEqual(got["114"]["inputs"]["image"], "ref.png")

    def test_prompt_is_not_stripped(self):
        # 提示词前后空白由用户决定，与 `qwen21_build.build` 同一条规矩。
        got = h3b.build(prompt="  x  ", first_frame="ref.png")
        self.assertEqual(got["133"]["inputs"]["prompt"], "  x  ")

    def test_each_call_gets_a_fresh_graph(self):
        first = h3b.build(prompt="a", first_frame="1.png")
        second = h3b.build(prompt="b", first_frame="2.png")
        self.assertEqual(first["133"]["inputs"]["prompt"], "a")
        self.assertEqual(second["133"]["inputs"]["prompt"], "b")
        self.assertEqual(base()["114"]["inputs"]["image"], "__first_frame__.png")

    def test_validation(self):
        cases = [
            (dict(prompt="", first_frame="a.png"), "空提示词"),
            (dict(prompt="   ", first_frame="a.png"), "全空白提示词"),
            (dict(prompt="a", first_frame=""), "空首帧"),
            (dict(prompt="a", first_frame="a.png", seed=-1), "负种子"),
            (dict(prompt="a", first_frame="a.png", seed=True), "布尔种子"),
            (dict(prompt="a", first_frame="a.png", seconds=99), "越界秒数"),
        ]
        for kwargs, why in cases:
            with self.subTest(why=why):
                with self.assertRaises(ValueError):
                    h3b.build(**kwargs)

    def test_a_foreign_base_is_rejected(self):
        """拿 See-through 的基准图来建 H3 的图，必须当场说，不能跑成一次白等。"""
        with self.assertRaises(ValueError) as ctx:
            h3b.build(prompt="a", first_frame="a.png", base=lb.BASE_FILE)
        self.assertIn("MiniMaxH3ImageToVideo", str(ctx.exception))


class Describe(support.LeakCheckedTestCase):
    """第 4 层：反解往返。"""

    def test_round_trip(self):
        wf = h3b.build(prompt="精灵女子抬头", first_frame="ref.png",
                       seconds=5, seed=847181417704469)
        info = h3b.describe(wf)
        self.assertEqual(info["mode"], "h3")
        self.assertEqual(info["prompt"], "精灵女子抬头")
        self.assertEqual(info["first_frame"], "ref.png")
        self.assertEqual(info["seconds"], 5)
        self.assertEqual(info["frames"], 124)
        self.assertEqual(info["seed"], 847181417704469)
        self.assertEqual(info["steps"], 4)
        self.assertEqual((info["width"], info["height"]), (480, 864))
        self.assertEqual((info["resize_width"], info["resize_height"]), (480, 864))
        self.assertTrue(info["resize_matches"])
        self.assertEqual(info["lora_strength"], 0.75)
        self.assertEqual(info["save_prefix"], "video/MiniMax_H3")

    def test_resize_matches_reports_false_when_the_pair_disagrees(self):
        """那条「两处真源」的判据要能**报出**不一致，而不是只在测试里成立。"""
        wf = h3b.build(prompt="a", first_frame="a.png")
        wf["340"]["inputs"]["height"] = 832
        self.assertFalse(h3b.describe(wf)["resize_matches"])

    def test_describe_survives_a_broken_resolution_node(self):
        # 反解的是**外部数据**（可能来自磁盘上的老记录）—— 一个坏字段不该让
        # 整个记录渲染崩掉，与 `comfy.outputs_from_history` 同一条规矩。
        wf = h3b.build(prompt="a", first_frame="a.png")
        wf["115"]["inputs"]["aspect_ratio"] = "7:5 (Nonsense)"
        info = h3b.describe(wf)
        self.assertIsNone(info["width"])
        self.assertFalse(info["resize_matches"])
        self.assertEqual(info["prompt"], "a")


class Recognition(support.LeakCheckedTestCase):
    """按**节点类**认，不按 id。"""

    def test_is_h3(self):
        self.assertTrue(h3b.is_h3(base()))
        self.assertTrue(h3b.is_h3(h3b.build(prompt="a", first_frame="a.png")))

    def test_is_h3_is_false_for_the_other_chains(self):
        self.assertFalse(h3b.is_h3(qb.load_base()))
        self.assertFalse(h3b.is_h3(lb.load_base()))
        self.assertFalse(h3b.is_h3({}))
        self.assertFalse(h3b.is_h3(None))

    def test_is_h3_does_not_care_about_ids(self):
        wf = base()
        renamed = {(str(int(k) + 1000) if k.isdigit() else k): v
                   for k, v in wf.items()}
        self.assertTrue(h3b.is_h3(renamed))


class SavePrefix(support.LeakCheckedTestCase):
    """★ 收尾前缀：不改就是「秒完成 + 没有产物」。"""

    def test_unique_save_prefix_recognizes_save_video(self):
        wf = h3b.build(prompt="a", first_frame="a.png")
        got = qb.unique_save_prefix(wf, "a1b2c3d4")
        self.assertEqual(got, "video/MiniMax_H3_a1b2c3d4")
        self.assertEqual(wf["189"]["inputs"]["filename_prefix"], got)

    def test_the_token_ends_up_in_the_filename(self):
        """这条是 `artifacts.py` 能靠文件名找回 H3 产物的**全部依据**。

        `SaveVideo` 把它写成 `<prefix>_00001.mp4`，所以真实文件名是
        `MiniMax_H3_a1b2c3d4_00001.mp4` —— 那个 token 是「作业 ↔ 产物」之间
        唯一的键。拼法改了，产物就全部找不回来。
        """
        wf = h3b.build(prompt="a", first_frame="a.png")
        prefix = qb.unique_save_prefix(wf, "a1b2c3d4")
        self.assertTrue(prefix.endswith("_a1b2c3d4"))
        self.assertTrue(os.path.basename(prefix).startswith("MiniMax_H3_"))

    def test_a_graph_without_a_save_node_is_rejected(self):
        wf = h3b.build(prompt="a", first_frame="a.png")
        del wf["189"]
        with self.assertRaises(ValueError) as ctx:
            qb.unique_save_prefix(wf, "deadbeef")
        self.assertIn("SaveVideo", str(ctx.exception))


class QwenUntouched(support.LeakCheckedTestCase):
    """这次改动**不许**碰 Qwen 那两条链路的模式表。"""

    def test_mode_lists_are_unchanged(self):
        self.assertEqual(qb.MODES, ("t2i", "edit"))
        self.assertEqual(qb.ALL_MODES, ("t2i", "edit", "upscale"))

    def test_save_classes_is_now_four_but_still_starts_with_the_old_two(self):
        # 前两项的顺序是别人依赖的（报错文案里那一串），新增只能往后加。
        self.assertEqual(qb.SAVE_CLASSES[:2], ("SaveImageAdvanced", "SaveImage"))
        self.assertIn("SaveVideo", qb.SAVE_CLASSES)
        self.assertIn("SeeThrough_SavePSD", qb.SAVE_CLASSES)

    def test_unknown_mode_message_does_not_mention_h3(self):
        # H3 不是 Qwen 那三个形状里的一个；它出现在这句提示里会把人指错方向。
        text = qb.unknown_mode_message("nonsense")
        self.assertNotIn("h3", text.lower())


if __name__ == "__main__":
    unittest.main()
