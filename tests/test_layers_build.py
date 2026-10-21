#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""See-through 构建器（`layers_build.py`）的判据。

分四层：

  1. **基准图良构** —— 接线照官方 `workflows/seethrough-basic.json`
  2. **冻结值** —— `auto_download=false` / `quant_mode=nf4` / 两个本地目录名 /
     `cache_tag_embeds` / `group_offload` / `tblr_split` / `use_lama`
  3. **`build()` 的射程** —— 只动我传的那几处，其余逐字不变
  4. **命名与反解** —— `LAYER_RE`、`describe()` 往返、收尾前缀

    python tests/test_layers_build.py
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
import layers_build as lb       # noqa: E402
import project_paths            # noqa: E402
import qwen21_build as qb       # noqa: E402
import support                  # noqa: E402

EXPECTED_LINKS = {
    "4.image": ["1", 0],
    "4.layerdiff_model": ["2", 0],
    "5.layers": ["4", 0],
    "5.depth_model": ["3", 0],
    "6.layers_depth": ["5", 0],
    "7.parts": ["6", 0],
}


def base():
    return lb.load_base()


def _flatten(obj, prefix=""):
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
    def test_base_file_is_where_the_builder_looks(self):
        """构建器用的那份 = **落点规则**算出来的那份（不是写死的某个路径）。

        规则只有一处（`project_paths.base_file()`）：环境变量 → 仓库自带的
        `workflows/` → 工作区。写死成工作区路径的话，**克隆下来的仓库里这条判据
        必红** —— 而那看起来像「仓库缺文件」。
        """
        self.assertEqual(
            lb.BASE_FILE,
            project_paths.base_file("QWEN21_LAYERS_BASE",
                                    "seethrough_api_base.json",
                                    repo=REPO, workspace=WORKSPACE))
        self.assertTrue(os.path.exists(lb.BASE_FILE),
                        "缺少基准图：%s" % lb.BASE_FILE)

    def test_six_processing_nodes_plus_the_loader(self):
        wf = base()
        self.assertEqual(sorted(wf), ["1", "2", "3", "4", "5", "6", "7"])
        self.assertEqual(
            {nid: n["class_type"] for nid, n in wf.items()},
            {"1": "LoadImage",
             "2": "SeeThrough_LoadLayerDiffModel",
             "3": "SeeThrough_LoadDepthModel",
             "4": "SeeThrough_GenerateLayers",
             "5": "SeeThrough_GenerateDepth",
             "6": "SeeThrough_PostProcess",
             "7": "SeeThrough_SavePSD"})

    def test_wiring_matches_the_official_workflow(self):
        """接线照官方 `seethrough-basic.json`：7 条边一条不多一条不少。

        官方那份是 8 节点 7 条边（多一个 `PreviewImage`）；这里去掉那个纯预览节点，
        因为层的本体已经逐张落盘了，多一个预览只会在输出目录里多一个 `_temp`。
        """
        wf = base()
        for path, want in EXPECTED_LINKS.items():
            nid, _, field = path.partition(".")
            with self.subTest(edge=path):
                self.assertEqual(wf[nid]["inputs"][field], want)

    def test_save_node_is_the_only_terminal(self):
        # 没有谁把 `7` 的输出接走 —— 它是收尾（`OUTPUT_NODE = True`）。
        consumed = set()
        for node in base().values():
            for value in node["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    consumed.add(value[0])
        self.assertNotIn("7", consumed)

    def test_source_image_is_a_placeholder(self):
        self.assertEqual(base()["1"]["inputs"]["image"], "__source__.png")


class FrozenValues(support.LeakCheckedTestCase):
    """★ 这一节每一个都对应一处**没有症状的错**：勾了、跑得通、结果不对。"""

    def test_auto_download_is_off(self):
        # 留 true 就会去够 huggingface.co —— 那台机器上那是 15 秒超时黑洞，
        # 退避 5 次后整次跑死。
        for nid in ("2", "3"):
            with self.subTest(node=nid):
                self.assertIs(base()[nid]["inputs"]["auto_download"], False)

    def test_quant_mode_is_nf4_and_matches_the_local_dirs(self):
        # `quant_mode=none` 会拿 `.to(bfloat16)` 去怼 bnb 的 4-bit 参数 ——
        # 真正的 4-bit 加载来自目录里 `unet/config.json` 的 `quantization_config`。
        # 所以这个开关是「告诉它别乱转型」，不是「告诉它去加载 4-bit」。
        for nid in ("2", "3"):
            with self.subTest(node=nid):
                self.assertEqual(base()[nid]["inputs"]["quant_mode"], "nf4")
                self.assertEqual(base()[nid]["inputs"]["quant_mode"], lb.QUANT_MODE)

    def test_models_are_local_directory_names(self):
        # 本地目录名 ⇒ 插件 `is_local=True` ⇒ `local_files_only=True` ⇒ 全程不碰网络。
        # 换成 repo id 就会走网络。
        self.assertEqual(base()["2"]["inputs"]["model"], lb.DIR_LAYERDIFF)
        self.assertEqual(base()["3"]["inputs"]["model"], lb.DIR_DEPTH)
        for nid in ("2", "3"):
            with self.subTest(node=nid):
                self.assertNotIn("/", base()[nid]["inputs"]["model"])

    def test_cache_tag_embeds_on_and_group_offload_off(self):
        # 前者省约 2 GB 显存且零速度代价；后者在 22 GB 卡上不需要，开了慢 2.8×。
        for nid in ("2", "3"):
            with self.subTest(node=nid):
                self.assertIs(base()[nid]["inputs"]["cache_tag_embeds"], True)
                self.assertIs(base()[nid]["inputs"]["group_offload"], False)

    def test_postprocess_toggles(self):
        # `use_lama=true` 缺检查点时会**静默退化成不拆前后发**，所以它不进页面、
        # 由体检警告（`config.LAMA_CKPT_*`）。
        self.assertIs(base()["6"]["inputs"]["tblr_split"], True)
        self.assertIs(base()["6"]["inputs"]["use_lama"], True)

    def test_defaults_match_the_verified_run(self):
        self.assertEqual(base()["4"]["inputs"]["resolution"], 1024)
        self.assertEqual(base()["4"]["inputs"]["num_inference_steps"], 30)
        # `-1` = 跟随 resolution。bf16 下它必 OOM，换成 fp16 之后 1024/1280 都跑得动。
        self.assertEqual(base()["5"]["inputs"]["resolution_depth"], lb.DEPTH_FOLLOW)

    def test_lama_checkpoint_constants(self):
        self.assertTrue(lb.LAMA_CKPT_REL.endswith("lama_large_512px.ckpt"))
        self.assertEqual(lb.LAMA_CKPT_BYTES, 204544673)


class LayerNaming(support.LeakCheckedTestCase):
    """★ `LAYER_RE` —— 画廊的排除规则与 `artifacts.py` 共用它。"""

    def test_matches_a_real_run(self):
        # 部署清单里那次真跑出来的名字（1024 档 23 层那次）。
        self.assertTrue(lb.LAYER_RE.match("stfp16_20261011_232021_b50ca98d_face.png"))
        self.assertTrue(lb.LAYER_RE.match(
            "stfp16_20261011_232021_b50ca98d_front_hair_depth.png"))

    def test_matches_after_the_job_token_is_inserted(self):
        # `unique_save_prefix` 会把 `job_id[:8]` 插进前缀 ⇒ 文件名里有两组八位十六进制。
        self.assertTrue(lb.LAYER_RE.match(
            "seethrough_a1b2c3d4_20261012_101530_ab12cd34_tail.png"))
        self.assertTrue(lb.LAYER_RE.match(
            "seethrough_a1b2c3d4_20261012_101530_ab12cd34_irides-l_depth.png"))

    def test_does_not_match_a_qwen_image(self):
        for name in ("Qwen_image_2.1_00001.png", "qwen21_apple_00007.png",
                     "MINIMAX_00003.png"):
            with self.subTest(name=name):
                self.assertIsNone(lb.LAYER_RE.match(name))

    def test_does_not_match_other_extensions(self):
        self.assertIsNone(lb.LAYER_RE.match(
            "seethrough_20261012_101530_ab12cd34_face.jpg"))

    def test_can_match_a_lookalike_which_is_why_the_gallery_needs_two_conditions(self):
        """这条**如实记下正则的边界**，免得有人以为它已经够严了。

        `SeeThrough_SavePSD` 的命名形状并不唯一属于 See-through —— 用户自己丢进
        `output/` 的一张图完全可能长这样。所以画廊的排除规则**必须**再加一个条件：
        同根目录里有配套的 `*_layers.json`。那条判据在 `test_artifacts.py` 里。
        """
        self.assertTrue(lb.LAYER_RE.match("photo_20260101_120000_deadbeef_cat.png"))


class BuildScope(support.LeakCheckedTestCase):
    def test_only_the_named_inputs_change(self):
        before = base()
        after = lb.build(source_image="in.png", seed=777, resolution=1280,
                         steps=20, resolution_depth=512)
        self.assertEqual(changed_paths(before, after),
                         ["1.inputs.image", "4.inputs.num_inference_steps",
                          "4.inputs.resolution", "4.inputs.seed",
                          "5.inputs.resolution_depth", "5.inputs.seed"])

    def test_omitted_arguments_leave_the_base_alone(self):
        before = base()
        self.assertEqual(changed_paths(before, lb.build(source_image="in.png")),
                         ["1.inputs.image"])

    def test_only_one_argument_at_a_time(self):
        # 逐项单独传，确保每一处都真的接上了（只看「全传」那一格会漏掉「少接一根线」）。
        cases = [
            ({"seed": 9}, ["4.inputs.seed", "5.inputs.seed"]),
            ({"resolution": 1280}, ["4.inputs.resolution"]),
            ({"steps": 15}, ["4.inputs.num_inference_steps"]),
            ({"resolution_depth": 512}, ["5.inputs.resolution_depth"]),
            ({"resolution_depth": lb.DEPTH_FOLLOW},
             [] if base()["5"]["inputs"]["resolution_depth"] == lb.DEPTH_FOLLOW
             else ["5.inputs.resolution_depth"]),
        ]
        for extra, want in cases:
            with self.subTest(extra=extra):
                kwargs = {"source_image": "in.png"}
                kwargs.update(extra)
                self.assertEqual(changed_paths(base(), lb.build(**kwargs)),
                                 ["1.inputs.image"] + want)

    def test_both_seeds_move_together(self):
        """★ 两个种子必须同步写。

        各自独立会让「同一个 seed 重跑」这件事对用户不成立 —— 他会以为没换种子
        就没变，而深度那一段其实换了。
        """
        wf = lb.build(source_image="in.png", seed=12345)
        self.assertEqual(wf["4"]["inputs"]["seed"], 12345)
        self.assertEqual(wf["5"]["inputs"]["seed"], 12345)

    def test_source_image_is_stripped(self):
        self.assertEqual(lb.build(source_image="  in.png  ")["1"]["inputs"]["image"],
                         "in.png")

    def test_each_call_gets_a_fresh_graph(self):
        lb.build(source_image="a.png", seed=1)
        lb.build(source_image="b.png", seed=2)
        self.assertEqual(base()["1"]["inputs"]["image"], "__source__.png")
        self.assertEqual(base()["4"]["inputs"]["seed"], 42)

    def test_validation(self):
        cases = [
            (dict(source_image=""), "空源图"),
            (dict(source_image="   "), "全空白源图"),
            (dict(source_image="a.png", seed=-1), "负种子"),
            (dict(source_image="a.png", seed=True), "布尔种子"),
            (dict(source_image="a.png", steps=0), "步数 0"),
            (dict(source_image="a.png", steps=101), "步数超上限"),
            (dict(source_image="a.png", steps=1.5), "小数步数"),
            (dict(source_image="a.png", resolution=1000), "分辨率不是 64 的倍数"),
            (dict(source_image="a.png", resolution=256), "分辨率太小"),
            (dict(source_image="a.png", resolution=4096), "分辨率太大"),
            (dict(source_image="a.png", resolution_depth=0), "深度分辨率 0"),
            (dict(source_image="a.png", resolution_depth=700), "深度分辨率不是 64 的倍数"),
        ]
        for kwargs, why in cases:
            with self.subTest(why=why):
                with self.assertRaises(ValueError):
                    lb.build(**kwargs)

    def test_depth_follow_is_the_only_negative_allowed(self):
        self.assertEqual(lb.build(source_image="a.png",
                                  resolution_depth=-1)["5"]["inputs"]["resolution_depth"],
                         -1)
        with self.assertRaises(ValueError):
            lb.build(source_image="a.png", resolution_depth=-64)

    def test_a_foreign_base_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            lb.build(source_image="a.png", base=h3b.BASE_FILE)
        self.assertIn("SeeThrough_GenerateLayers", str(ctx.exception))


class Describe(support.LeakCheckedTestCase):
    def test_round_trip(self):
        wf = lb.build(source_image="in.png", seed=42, resolution=1280,
                      steps=30, resolution_depth=-1)
        info = lb.describe(wf)
        self.assertEqual(info["mode"], "layers")
        self.assertEqual(info["source_image"], "in.png")
        self.assertEqual(info["source_images"], ["in.png"])
        self.assertEqual(info["seed"], 42)
        self.assertEqual(info["steps"], 30)
        self.assertEqual(info["resolution"], 1280)
        self.assertEqual(info["layerdiff_model"], lb.DIR_LAYERDIFF)
        self.assertEqual(info["depth_model"], lb.DIR_DEPTH)
        self.assertEqual(info["quant_mode"], "nf4")
        self.assertIs(info["auto_download"], False)
        self.assertIs(info["tblr_split"], True)
        self.assertIs(info["use_lama"], True)
        self.assertEqual(info["save_prefix"], lb.SAVE_PREFIX)

    def test_follow_reports_the_effective_value(self):
        """`-1` 时页面要能直接说出「深度实际跑在多少」,而不是自己再推一遍。"""
        info = lb.describe(lb.build(source_image="in.png", resolution=1280,
                                    resolution_depth=-1))
        self.assertTrue(info["depth_follows"])
        self.assertEqual(info["resolution_depth"], -1)
        self.assertEqual(info["resolution_depth_effective"], 1280)

    def test_explicit_depth_is_not_reported_as_following(self):
        info = lb.describe(lb.build(source_image="in.png", resolution=1280,
                                    resolution_depth=512))
        self.assertFalse(info["depth_follows"])
        self.assertEqual(info["resolution_depth_effective"], 512)

    def test_prompt_keys_are_present_but_empty(self):
        # 这条链路没有文本输入。三个键**必须写出来**：任务记录 / 画廊项 / 表单回显
        # 三处共用同一份字段形状，「键缺失」与「键是空串」在前端是两种待遇。
        info = lb.describe(base())
        for key in ("prompt", "negative_prompt"):
            with self.subTest(key=key):
                self.assertIn(key, info)
                self.assertEqual(info[key], "")


class Recognition(support.LeakCheckedTestCase):
    def test_is_layers(self):
        self.assertTrue(lb.is_layers(base()))
        self.assertTrue(lb.is_layers(lb.build(source_image="a.png")))

    def test_is_layers_is_false_for_the_other_chains(self):
        self.assertFalse(lb.is_layers(qb.load_base()))
        self.assertFalse(lb.is_layers(h3b.load_base()))
        self.assertFalse(lb.is_layers({}))
        self.assertFalse(lb.is_layers(None))


class SavePrefix(support.LeakCheckedTestCase):
    def test_unique_save_prefix_recognizes_save_psd(self):
        wf = lb.build(source_image="a.png")
        got = qb.unique_save_prefix(wf, "a1b2c3d4")
        self.assertEqual(got, "seethrough_a1b2c3d4")
        self.assertEqual(wf["7"]["inputs"]["filename_prefix"], got)

    def test_the_token_ends_up_in_every_layer_filename(self):
        """这条是 `artifacts.py` 能从几十个层文件里认出「这一批」的**全部依据**：
        `SeeThrough_SavePSD` 把前缀拼成 `<prefix>_<ts>_<uid>_<tag>.png`。"""
        wf = lb.build(source_image="a.png")
        prefix = qb.unique_save_prefix(wf, "a1b2c3d4")
        ts, uid = "20261012", "ab12cd34"
        name = "%s_%s_101530_%s_tail.png" % (prefix, ts, uid)
        self.assertTrue(lb.LAYER_RE.match(name), name)


class QwenUntouched(support.LeakCheckedTestCase):
    def test_mode_lists_are_unchanged(self):
        self.assertEqual(qb.MODES, ("t2i", "edit"))
        self.assertEqual(qb.ALL_MODES, ("t2i", "edit", "upscale"))


if __name__ == "__main__":
    unittest.main()
