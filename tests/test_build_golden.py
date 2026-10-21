#!/usr/bin/env python3
"""P0 硬门槛：统一构建器必须与四个旧脚本的产物**逐字相同**。

测试分三层：
  1. 结构等价 —— `qwen21_fashion_api.json` ≡ `qwen21_api_base.json` + `on_false`
  2. 黄金对比 —— 新构建器的输出 vs 旧脚本跑出来的快照（tests/fixtures/）
  3. 不变量   —— 断边修复、参数校验、describe() 反解

快照是在重构**之前**用旧脚本跑出来的（见 tests/fixtures/README.md），
所以这些断言是真正的回归保护，不是"自己对自己"。

    python tests/test_build_golden.py
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))          # …/qwen21-workbench/tests
REPO = os.path.dirname(HERE)                               # …/qwen21-workbench
WORKSPACE = os.path.dirname(REPO)                          # …/ComfyUI 运维现场
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import qwen21_build as qb  # noqa: E402
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # support.py 就在 tests/ 里
import support  # noqa: E402  (测试基类 / 临时目录)

FIXTURES = os.path.join(HERE, "fixtures")

T2I_PROMPT = "a small brown fox sitting in tall grass"
EDIT_PROMPT = "make the apple bright blue"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def canonical(obj):
    """规范化为可逐字比较的字符串（键排序，忽略无关空白）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def fashion_tree():
    """成品工作流 `qwen21_fashion_api.json` —— 它是**工作区**里的产物，不在本仓库。

    克隆下来的仓库里没有它（那是作者那台机器上的成品图），所以那几条「结构等价」
    的判据**跳过**，而不是变红：「我这台机器上有没有那张成品图」不是这个仓库
    能自证的事。
    """
    path = os.path.join(WORKSPACE, "qwen21_fashion_api.json")
    if not os.path.exists(path):
        raise unittest.SkipTest("工作区里没有 %s（克隆下来的仓库里不会有）" % path)
    return load_json(path)


def fashion_prompt():
    """build_refine.py / build_2k.py 的提示词是从 fashion 图里带出来的。"""
    return fashion_tree()["459:474"]["inputs"]["on_false"]


def fixture(name):
    return load_json(os.path.join(FIXTURES, name))


class GoldenRegression(support.LeakCheckedTestCase):
    """新构建器 vs 旧脚本快照 —— 逐字相同才算守住行为。

    快照是**重构之前**的旧脚本产物，那时还没有「TE-Speed 默认接」这回事 ——
    所以比之前先把这个**已知的差**补上（`expected()`），让这一组继续只钉
    「其余接线一字未变」。TE-Speed 自己的接线与参数由 `TeSpeedWiring` 那一组守。
    """

    def expected(self, name):
        """历史快照 + 新默认（TE-Speed 那个节点）。见 `support.with_default_te_speed`。"""
        return support.with_default_te_speed(fixture(name))

    def assertSame(self, built, expected, label):
        if canonical(built) == canonical(expected):
            return
        # 失败时把差异说清楚，而不是甩一个巨大的字符串
        diffs = []
        for nid in sorted(set(built) | set(expected)):
            if canonical(built.get(nid)) != canonical(expected.get(nid)):
                diffs.append(nid)
        self.fail(f"{label} 与快照不一致；有差异的节点: {diffs}")

    def test_t2i_golden(self):
        wf = qb.build("t2i", prompt=T2I_PROMPT, steps=30, seed=111)
        self.assertSame(wf, self.expected("golden_t2i.json"), "t2i")

    def test_t2i_sized_golden(self):
        wf = qb.build("t2i", prompt=T2I_PROMPT, steps=30, seed=111,
                      width=768, height=512)
        self.assertSame(wf, self.expected("golden_t2i_sized.json"), "t2i+尺寸")

    def test_rewrite_golden(self):
        """改写从**模式** `refine` 降级成开关 `rewrite=True`（2026-10-01）。

        这一条是那次重构的**核心判据**：说法变了，图必须**一个字节都不变**。
        快照是重构之前用旧脚本跑出来的，所以它真的在钉行为，不是自己对自己。
        """
        wf = qb.build("t2i", prompt=fashion_prompt(), rewrite=True)
        self.assertSame(wf, self.expected("golden_refine.json"), "t2i+rewrite")

    def test_2048_size_golden(self):
        """`2k` 模式撤销：它就是「文生图 + 显式 2048²」，图同样一个字节不变。"""
        wf = qb.build("t2i", prompt=fashion_prompt(), width=2048, height=2048)
        self.assertSame(wf, self.expected("golden_2k.json"), "t2i+2048")

    def test_edit_golden(self):
        wf = qb.build("edit", prompt=EDIT_PROMPT, source_image="ref.png",
                      resolution=1024, seed=7, steps=25)
        self.assertSame(wf, self.expected("golden_edit.json"), "edit")


class StructuralEquivalence(support.LeakCheckedTestCase):
    """把"两个基准到底差在哪"钉成可执行的断言。"""

    def test_fashion_equals_base_plus_on_false(self):
        base = qb.load_base()
        fashion = fashion_tree()
        base["459:474"]["inputs"]["on_false"] = fashion["459:474"]["inputs"]["on_false"]
        base["459:471"]["inputs"]["prompt"] = fashion["459:471"]["inputs"]["prompt"]
        self.assertEqual(canonical(base), canonical(fashion))

    def test_base_itself_has_the_broken_edge(self):
        """基准图确实带断边 —— 这是 build() 必须无条件修复的原因。"""
        base = qb.load_base()
        self.assertNotIn("on_false", base["459:474"]["inputs"])
        self.assertEqual(sorted(base["459:474"]["inputs"]), ["on_true", "switch"])

    def test_build_always_repairs_the_edge(self):
        # 三个「形状 + 配置」的组合都要修断边（曾经是三种模式，现在是一个模式
        # 加两个开关 —— 修法一个字没变，所以这一条也照旧横扫）。
        for mode, kw in (("t2i", {}), ("t2i", {"rewrite": True}),
                         ("t2i", {"width": 2048, "height": 2048})):
            with self.subTest(mode=mode, kw=kw):
                wf = qb.build(mode, prompt="边缘修复检查", **kw)
                self.assertEqual(wf["459:474"]["inputs"]["on_false"], "边缘修复检查")
                self.assertEqual(wf["459:471"]["inputs"]["prompt"], "边缘修复检查")

    def test_build_does_not_mutate_the_base_file(self):
        before = canonical(qb.load_base())
        qb.build("t2i", prompt="不该写回基准", steps=5, seed=1)
        qb.build("edit", prompt="也不该", source_image="ref.png")
        self.assertEqual(canonical(qb.load_base()), before)


class ModeWiring(support.LeakCheckedTestCase):
    def test_switch_follows_the_rewrite_flag(self):
        """改写器开关只跟 `rewrite` 走（2026-10-01 之前它跟的是模式 `refine`）。"""
        self.assertFalse(qb.build("t2i", prompt="x")["459:474"]["inputs"]["switch"])
        self.assertFalse(qb.build("t2i", prompt="x", width=2048,
                                  height=2048)["459:474"]["inputs"]["switch"])
        self.assertTrue(qb.build("t2i", prompt="x",
                                 rewrite=True)["459:474"]["inputs"]["switch"])

    def test_t2i_keeps_resolution_selector_links_by_default(self):
        lat = qb.build("t2i", prompt="x")["459:456"]["inputs"]
        self.assertEqual(lat["width"], ["13", 0])
        self.assertEqual(lat["height"], ["13", 1])

    def test_2048_comes_only_from_the_explicit_size(self):
        """2048² 现在只有一个来源：显式尺寸（那个「没给就补 2048」的模式撤销了）。

        撤销的**代价**正是这条判据在守的：以前 `2k` 会在没给尺寸时补上 2048，
        现在没人补了 —— 想要 2K 就得在「尺寸」里选它（那一档还在），
        而这一条钉住「给了就照给、没给就不动」。
        """
        lat = qb.build("t2i", prompt="x", width=2048,
                       height=2048)["459:456"]["inputs"]
        self.assertEqual((lat["width"], lat["height"]), (2048, 2048))
        untouched = qb.build("t2i", prompt="x")["459:456"]["inputs"]
        self.assertEqual(untouched["width"], ["13", 0])
        self.assertEqual(untouched["height"], ["13", 1])

    def test_edit_drops_rewriter_branch_and_relinks(self):
        wf = qb.build("edit", prompt="x", source_image="ref.png")
        for nid in qb.EDIT_DROP:
            self.assertNotIn(nid, wf)
        self.assertEqual(wf[qb.LOAD_IMAGE],
                         {"class_type": "LoadImage", "inputs": {"image": "ref.png"}})
        te = wf[qb.TEXT_ENCODE]["inputs"]
        self.assertEqual(te["images.image_1"], [qb.LOAD_IMAGE, 0])
        self.assertEqual(te["vae"], [qb.VAE_LOADER, 0])
        self.assertEqual(te["resolution"], qb.DEFAULT_EDIT_RESOLUTION)
        # 必须用参考图尺寸的 latent，不能是 EmptyLatentImage
        self.assertEqual(wf[qb.KSAMPLER]["inputs"]["latent_image"],
                         [qb.TEXT_ENCODE, 2])

    def test_none_means_leave_alone(self):
        """参数为 None = 保留基准原值，而不是写成 None。"""
        base = qb.load_base()
        wf = qb.build("t2i", prompt="x")
        self.assertEqual(wf["459:458"]["inputs"]["steps"],
                         base["459:458"]["inputs"]["steps"])
        self.assertEqual(wf["459:458"]["inputs"]["seed"],
                         base["459:458"]["inputs"]["seed"])


class Validation(support.LeakCheckedTestCase):
    def test_rejects_non_multiple_of_32(self):
        for bad in (100, 1, 33):
            with self.subTest(width=bad):
                with self.assertRaises(ValueError):
                    qb.build("t2i", prompt="x", width=bad)

    def test_rejects_steps_out_of_range(self):
        for bad in (0, 201, -5):
            with self.subTest(steps=bad):
                with self.assertRaises(ValueError):
                    qb.build("t2i", prompt="x", steps=bad)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            qb.build("txt2img", prompt="x")

    def test_removed_modes_are_rejected_with_a_migration_hint(self):
        """`2k` / `refine` 撤掉了，而且报错里必须**说清怎么改**。

        老脚本（`build_refine.py` / `build_2k.py` 这种）撞上来只会看到一句
        「未知模式」时，人得自己去猜；而这两个值的替代写法是确定的。
        **不做静默映射**：把 `2k` 当成 t2i+2048 会藏起「我用的还是老写法」。
        """
        for mode, needle in (("2k", "width=height=2048"),
                             ("refine", "rewrite=True")):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as ctx:
                    qb.build(mode, prompt="x")
                self.assertIn(needle, str(ctx.exception))
                self.assertIn("未知模式", str(ctx.exception))

    def test_rewrite_is_rejected_on_edit(self):
        """改写器只在文生图那条链路上存在 —— 图生图必须**当场拒**，不静默忽略。

        静默忽略的症状是「勾了没反应」：出图正常、没有报错、而那次跑的提示词
        根本没经过改写器。`_wire_edit` 会把 `SWITCH` 与 `PE_PROMPT` 整段删掉，
        所以不是「暂时不支持」，是**接不上**。
        """
        with self.assertRaises(ValueError) as ctx:
            qb.build("edit", prompt="x", source_image="ref.png", rewrite=True)
        self.assertIn("文生图", str(ctx.exception))

    def test_rewrite_must_be_a_bool(self):
        for bad in ("true", 1, None):
            with self.subTest(rewrite=bad):
                with self.assertRaises(ValueError):
                    qb.build("t2i", prompt="x", rewrite=bad)

    def test_rejects_empty_prompt(self):
        for bad in ("", "   ", None, 123):
            with self.subTest(prompt=bad):
                with self.assertRaises(ValueError):
                    qb.build("t2i", prompt=bad)

    def test_edit_requires_source_image(self):
        with self.assertRaises(ValueError):
            qb.build("edit", prompt="x")

    def test_edit_accepts_zero_resolution(self):
        """0 = 保持参考图原尺寸，是合法值（见 build_edit.py 的帮助文本）。"""
        wf = qb.build("edit", prompt="x", source_image="ref.png", resolution=0)
        self.assertEqual(wf[qb.TEXT_ENCODE]["inputs"]["resolution"], 0)

    def test_rejects_bad_resolution(self):
        with self.assertRaises(ValueError):
            qb.build("edit", prompt="x", source_image="ref.png", resolution=100)


class Describe(support.LeakCheckedTestCase):
    def test_t2i(self):
        info = qb.describe(qb.build("t2i", prompt="一只狐狸", steps=20, seed=42))
        self.assertEqual(info["mode"], "t2i")
        self.assertFalse(info["is_2k"])
        self.assertEqual(info["prompt"], "一只狐狸")
        self.assertEqual((info["steps"], info["seed"]), (20, 42))
        self.assertIsNone(info["width"])           # 链接到 ResolutionSelector
        self.assertEqual(info["size_source"], "ResolutionSelector(节点 13)")

    def test_2k_size(self):
        info = qb.describe(qb.build("t2i", prompt="x", width=2048, height=2048))
        self.assertTrue(info["is_2k"])
        self.assertEqual((info["width"], info["height"]), (2048, 2048))
        self.assertEqual(info["size_source"], "literal")
        self.assertEqual(info["mode"], "t2i")     # 2K 不是模式，只是尺寸

    def test_rewrite(self):
        info = qb.describe(qb.build("t2i", prompt="x", rewrite=True))
        self.assertEqual(info["mode"], "t2i")     # 改写不是模式
        self.assertTrue(info["rewrite"])

    def test_rewrite_is_false_everywhere_else(self):
        """反解出的 `rewrite` 在别的形状上必须是 False，而不是缺键。

        键**必须在**：前端拿到「键缺失」和拿到 False 是两种待遇，而两条链
        （出图 / 放大）的字段形状分家过一次就不容易再合上。
        """
        self.assertFalse(qb.describe(qb.build("t2i", prompt="x"))["rewrite"])
        self.assertFalse(qb.describe(qb.build("edit", prompt="x",
                                              source_image="ref.png"))["rewrite"])
        up = qb.describe(qb.build_upscale("ref.png", scale=2))
        self.assertIn("rewrite", up)
        self.assertFalse(up["rewrite"])

    def test_edit(self):
        info = qb.describe(qb.build("edit", prompt="改成蓝色",
                                    source_image="ref.png", resolution=1024))
        self.assertEqual(info["mode"], "edit")
        self.assertEqual(info["prompt"], "改成蓝色")
        self.assertEqual(info["source_image"], "ref.png")
        self.assertEqual(info["resolution"], 1024)
        # 10 = 基准那 9 个 + TE-Speed（2026-10-06 起默认接上）
        self.assertEqual(info["nodes"], 10)

    def test_describe_survives_real_output(self):
        """对真实产出过的图反解 —— 逐字段核对，防手滑。"""
        # 由 support 按**候选目录**找（落点搬过家，写死安装目录会静默跳过）
        by_name = support.real_output_png_by_name()
        for name, want in (("Qwen_image_2.1_00013.png", ("t2i", 2025, 20)),
                           ("Qwen_image_2.1_00011.png", ("t2i", 12345, 40))):
            path = by_name.get(name)
            if not path:
                self.skipTest(f"找不到 {name}（不在本机）")
            import struct
            with open(path, "rb") as f:
                f.read(8)
                meta = {}
                while True:
                    head = f.read(8)
                    if len(head) < 8:
                        break
                    ln, typ = struct.unpack(">I4s", head)
                    data = f.read(ln)
                    f.read(4)
                    if typ == b"tEXt":
                        k, _, v = data.partition(b"\x00")
                        meta[k.decode("latin-1")] = v.decode("latin-1")
                    elif typ == b"IEND":
                        break
            info = qb.describe(json.loads(meta["prompt"]))
            self.assertEqual((info["mode"], info["seed"], info["steps"]), want)


class LazyCacheWiring(support.LeakCheckedTestCase):
    """缓存加速（LazyCache）。默认关是主要契约，接的位置和「接不上就炸」是其次。"""

    MODE_KW = (("t2i", {}), ("t2i", {"rewrite": True}),
               ("edit", {"source_image": "ref.png"}))

    def test_default_is_off(self):
        """LazyCache **默认关** —— 默认接的是 TE-Speed（见 `TeSpeedWiring`）。
        所以这里显式 `te_speed=False`，顺手把「两个都没接」那条基线也钉住。"""
        for mode, kw in self.MODE_KW:
            with self.subTest(mode=mode):
                wf = qb.build(mode, prompt="x", te_speed=False, **kw)
                self.assertNotIn(qb.LAZY_CACHE, wf)
                self.assertNotIn(qb.TE_SPEED, wf)
                self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"], [qb.CACHE, 0])
                self.assertFalse(qb.describe(wf)["lazy_cache"])

    def test_explicit_false_is_the_same_as_omitting_it(self):
        a = qb.build("t2i", prompt="x", steps=20, seed=1)
        b = qb.build("t2i", prompt="x", steps=20, seed=1, lazy_cache=False)
        self.assertEqual(canonical(a), canonical(b))

    def test_every_mode_can_carry_it(self):
        for mode, kw in self.MODE_KW:
            with self.subTest(mode=mode):
                wf = qb.build(mode, prompt="x", lazy_cache=True, te_speed=False,
                              **kw)
                self.assertIn(qb.LAZY_CACHE, wf)
                self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"],
                                 [qb.LAZY_CACHE, 0])
                self.assertTrue(qb.describe(wf)["lazy_cache"])

    def test_it_sits_between_the_kv_cache_and_the_sampler(self):
        """顺序有讲究：步复用必须包住 KSampler 真正吃到的那份 MODEL，
        而 QwenImage21Cache 是给 Qwen 设 KV 缓存设备/量化的，得在它之后。"""
        node = qb.build("t2i", prompt="x", lazy_cache=True,
                        te_speed=False)[qb.LAZY_CACHE]
        self.assertEqual(node["class_type"], "LazyCache")
        self.assertEqual(node["inputs"]["model"], [qb.CACHE, 0])

    def test_parameters_come_from_the_measured_constants(self):
        node = qb.build("t2i", prompt="x", lazy_cache=True,
                        te_speed=False)[qb.LAZY_CACHE]
        self.assertEqual(node["inputs"]["reuse_threshold"], qb.LAZY_CACHE_THRESHOLD)
        self.assertEqual(node["inputs"]["start_percent"], qb.LAZY_CACHE_START_PERCENT)
        self.assertEqual(node["inputs"]["end_percent"], qb.LAZY_CACHE_END_PERCENT)
        # verbose 会把每个采样步刷进后端日志，实测那批就是这么被刷满的
        self.assertFalse(node["inputs"]["verbose"])

    def test_wiring_twice_does_not_stack_two_nodes(self):
        wf = qb.build("t2i", prompt="x", lazy_cache=True, te_speed=False)
        qb._wire_lazy_cache(wf)
        self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"], [qb.LAZY_CACHE, 0])
        self.assertEqual(len([k for k, v in wf.items()
                              if v.get("class_type") == "LazyCache"]), 1)

    def test_refuses_when_the_base_is_rewired_underneath(self):
        """基准图换了接法时必须**当场炸**：静默接不上会变成「能跑，但一秒没省」，
        而那种错没有任何症状。"""
        base = qb.load_base()
        base[qb.KSAMPLER]["inputs"]["model"] = [qb.UNET, 0]     # 绕过 QwenImage21Cache
        with mock.patch.object(qb, "load_base",
                               lambda path=None: json.loads(canonical(base))):
            with self.assertRaises(ValueError) as cm:
                qb.build("t2i", prompt="x", lazy_cache=True, te_speed=False)
        self.assertIn(qb.CACHE, str(cm.exception))

    def test_rejects_non_boolean(self):
        for bad in (1, 0, "true", "yes", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    qb.build("t2i", prompt="x", lazy_cache=bad)


class UpscaleWiring(support.LeakCheckedTestCase):
    """放大模式（VOSR2）。它**不是**出图的变体，所以这一组守的是「两边别串味」。"""

    def test_builds_the_five_node_graph(self):
        wf = qb.build_upscale("src.png", scale=2, seed=7)
        self.assertEqual(len(wf), 5)
        self.assertEqual(
            sorted(n["class_type"] for n in wf.values()),
            ["LoadImage", "SaveImage", "TESpeedVOSR2Image",
             "TESpeedVOSR2Loader", "TESpeedVOSR2Settings"])

    def test_the_wiring_is_the_one_that_was_measured(self):
        """节点 id 与那批实测用的图一致 —— 换 id 不会报错，只会让
        「实测过的那份」和「代码生成的那份」悄悄变成两张图。"""
        wf = qb.build_upscale("src.png")
        self.assertEqual(wf[qb.UP_IMAGE]["inputs"]["model"], [qb.UP_LOADER, 0])
        self.assertEqual(wf[qb.UP_IMAGE]["inputs"]["images"], [qb.UP_LOAD, 0])
        self.assertEqual(wf[qb.UP_IMAGE]["inputs"]["settings"], [qb.UP_SETTINGS, 0])
        self.assertEqual(wf[qb.UP_SAVE]["inputs"]["images"], [qb.UP_IMAGE, 0])
        self.assertEqual(wf[qb.UP_LOAD]["inputs"]["image"], "src.png")
        self.assertEqual(wf[qb.UP_LOADER]["inputs"]["model_bundle"],
                         qb.UPSCALE_BUNDLE)

    def test_it_is_not_a_qwen_graph(self):
        """一张放大图里**不该有** Qwen 那套东西：它连 KSampler 都没有。"""
        wf = qb.build_upscale("src.png")
        classes = {n["class_type"] for n in wf.values()}
        for absent in ("KSampler", "UNETLoader", "CLIPLoader",
                       "TextEncodeQwenImage21", "VAEDecode"):
            self.assertNotIn(absent, classes)

    def test_scale_and_seed_reach_the_graph(self):
        wf = qb.build_upscale("src.png", scale=4, seed=99)
        self.assertEqual(wf[qb.UP_IMAGE]["inputs"]["scale"], 4)
        self.assertEqual(wf[qb.UP_IMAGE]["inputs"]["seed"], 99)

    def test_defaults_are_applied(self):
        wf = qb.build_upscale("src.png")
        self.assertEqual(wf[qb.UP_IMAGE]["inputs"]["scale"], qb.UPSCALE_DEFAULT_SCALE)
        self.assertEqual(wf[qb.UP_IMAGE]["inputs"]["seed"], qb.UPSCALE_DEFAULT_SEED)

    def test_the_settings_block_is_written_out_in_full(self):
        """settings **显式写全**，不靠插件默认值：哪天插件改了默认档，
        这份图仍然表达同一个意思。三个关键值各有出处（实测那批用的就是它们）。"""
        wf = qb.build_upscale("src.png")
        s = wf[qb.UP_SETTINGS]["inputs"]
        self.assertEqual(sorted(s), sorted([
            "quality_profile", "tile_strategy", "tile_size", "tile_overlap",
            "vae_tile_size", "vae_tile_overlap", "image_batch", "frame_batch",
            "dino_batch", "temporal_cache", "cache_threshold", "cache_refresh",
            "memory_policy", "color_alignment"]))
        self.assertEqual(s["tile_size"], 512)
        self.assertEqual(s["vae_tile_size"], 1024)
        self.assertEqual(s["quality_profile"], "speed")
        # torch_compile 在 **Loader** 上，不在 Settings 上。没实测过的东西
        # （要 triton、首次调用要编译）不默认打开。
        self.assertFalse(wf[qb.UP_LOADER]["inputs"]["torch_compile"])

    def test_validation(self):
        for bad, kw in (("", {}), (None, {}), ("  ", {}),
                        ("a.png", {"scale": 0}), ("a.png", {"scale": 17}),
                        ("a.png", {"scale": 2.5}), ("a.png", {"scale": True}),
                        ("a.png", {"seed": -1}), ("a.png", {"seed": True})):
            with self.subTest(bad=bad, kw=kw):
                with self.assertRaises(ValueError):
                    qb.build_upscale(bad, **kw)

    def test_upscale_is_not_a_build_mode(self):
        """`build()` 的前提是「基准图 + 提示词」，放大两样都没有 ——
        所以它必须继续拒绝这个模式，而不是被悄悄塞进 MODES。"""
        self.assertNotIn(qb.UPSCALE_MODE, qb.MODES)
        self.assertIn(qb.UPSCALE_MODE, qb.ALL_MODES)
        with self.assertRaises(ValueError):
            qb.build(qb.UPSCALE_MODE, prompt="x")

    def test_the_two_describe_shapes_have_the_same_keys(self):
        """两种图的反解**键集必须一致** —— 前端拿到「键缺失」和拿到 null
        是两种待遇，分家之后 `p.scale` 会变成 undefined 而不是 null。"""
        up = qb.describe(qb.build_upscale("src.png"))
        qwen = qb.describe(qb.build("t2i", prompt="x"))
        self.assertEqual(sorted(up), sorted(qwen))
        self.assertEqual(up["mode"], "upscale")
        self.assertEqual(qwen["scale"], None)
        self.assertEqual(up["scale"], 2)

    def test_describe_reports_no_size_instead_of_guessing(self):
        """输出尺寸 = 源图尺寸 × scale，而源图尺寸**不在图里**。
        所以如实报 None，不拿 1024 冒充。"""
        d = qb.describe(qb.build_upscale("src.png", scale=3, seed=5))
        self.assertIsNone(d["width"])
        self.assertIsNone(d["height"])
        self.assertIsNone(d["steps"])          # one-step，没有步数
        self.assertEqual(d["scale"], 3)
        self.assertEqual(d["seed"], 5)
        self.assertEqual(d["source_image"], "src.png")

    def test_is_upscale_recognizes_by_class_not_by_id(self):
        """按**类名**认，不按 id —— id 是我们自己铸的，换个版本就会变。"""
        wf = qb.build_upscale("src.png")
        self.assertTrue(qb.is_upscale(wf))
        self.assertFalse(qb.is_upscale(qb.build("t2i", prompt="x")))
        # 就算把节点 id 全换掉，也照样认得出来
        renumbered = {("n%d" % i): node for i, node in enumerate(wf.values())}
        self.assertTrue(qb.is_upscale(renumbered))

    def test_it_does_not_read_the_base_graph(self):
        """放大**不碰基准图** —— 基准图缺了它也该能构建出来。"""
        with mock.patch.object(qb, "load_base",
                               side_effect=AssertionError("不该读基准图")):
            wf = qb.build_upscale("src.png")
        self.assertEqual(len(wf), 5)


class TeSpeedWiring(support.LeakCheckedTestCase):
    """TE-Speed（第三方插件）。**默认接**、接的位置、与 LazyCache 互斥、参数来自实测常量。

    互斥那一条是这里最值钱的：两个开关插在**同一条 model 连线**上，接错了会
    变成「能跑、但其中一个一秒没省」—— 那种错没有任何症状。
    """

    MODE_KW = (("t2i", {}), ("t2i", {"rewrite": True}),
               ("edit", {"source_image": "ref.png"}))

    def test_default_is_on(self):
        """**默认就接**（2026-10-06 起）：「每次出图都手动勾一下」是白费的手脚
        —— 实测它最快也最不伤画质（数字见上面 TE_SPEED_* 那段）。要关得明确写
        `te_speed=False`。"""
        for mode, kw in self.MODE_KW:
            with self.subTest(mode=mode):
                wf = qb.build(mode, prompt="x", **kw)
                self.assertIn(qb.TE_SPEED, wf)
                self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"],
                                 [qb.TE_SPEED, 0])
                self.assertTrue(qb.describe(wf)["te_speed"])

    def test_explicit_false_is_the_only_way_off(self):
        """「没说」与「说不要」现在是**两件事**：省略 = 默认（接），写 False 才不接。"""
        on = qb.build("t2i", prompt="x", steps=20, seed=1)
        off = qb.build("t2i", prompt="x", steps=20, seed=1, te_speed=False)
        self.assertIn(qb.TE_SPEED, on)
        self.assertNotIn(qb.TE_SPEED, off)
        self.assertEqual(off[qb.KSAMPLER]["inputs"]["model"], [qb.CACHE, 0])
        self.assertFalse(qb.describe(off)["te_speed"])

    def test_every_mode_can_carry_it(self):
        for mode, kw in self.MODE_KW:
            with self.subTest(mode=mode):
                wf = qb.build(mode, prompt="x", te_speed=True, **kw)
                self.assertIn(qb.TE_SPEED, wf)
                self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"],
                                 [qb.TE_SPEED, 0])
                self.assertTrue(qb.describe(wf)["te_speed"])

    def test_it_sits_between_the_kv_cache_and_the_sampler(self):
        node = qb.build("t2i", prompt="x", te_speed=True)[qb.TE_SPEED]
        self.assertEqual(node["class_type"], "TESpeedQwenImage21")
        self.assertEqual(node["inputs"]["model"], [qb.CACHE, 0])

    def test_parameters_come_from_the_measured_constants(self):
        node = qb.build("t2i", prompt="x", te_speed=True)[qb.TE_SPEED]
        self.assertEqual(node["inputs"]["attention"], qb.TE_SPEED_ATTENTION)
        self.assertEqual(node["inputs"]["step_cache"], qb.TE_SPEED_STEP_CACHE)
        self.assertEqual(node["inputs"]["reuse_threshold"],
                         qb.TE_SPEED_REUSE_THRESHOLD)
        self.assertEqual(node["inputs"]["predictor_error_limit"],
                         qb.TE_SPEED_ERROR_LIMIT)
        self.assertFalse(node["inputs"]["verbose"])

    def test_zero_means_auto_in_this_plugin_so_it_stays_zero(self):
        """`0.0` 在这个插件里是「用它的默认窗口」，实测生效为 0.18-0.86。
        谁要是"顺手写清楚"改成 0.18/0.86，就换成了另一条没验过的路径 ——
        这条用例存在的唯一目的就是把 0.0 钉住。"""
        node = qb.build("t2i", prompt="x", te_speed=True)[qb.TE_SPEED]
        self.assertEqual(node["inputs"]["start_percent"], 0.0)
        self.assertEqual(node["inputs"]["end_percent"], 0.0)
        self.assertNotEqual(node["inputs"]["reuse_threshold"], 0.0)

    def test_wiring_twice_does_not_stack_two_nodes(self):
        wf = qb.build("t2i", prompt="x", te_speed=True)
        qb._wire_te_speed(wf)
        self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"], [qb.TE_SPEED, 0])
        self.assertEqual(len([k for k, v in wf.items()
                              if v.get("class_type") == "TESpeedQwenImage21"]), 1)

    def test_refuses_when_the_base_is_rewired_underneath(self):
        base = qb.load_base()
        base[qb.KSAMPLER]["inputs"]["model"] = [qb.UNET, 0]
        with mock.patch.object(qb, "load_base",
                               lambda path=None: json.loads(canonical(base))):
            with self.assertRaises(ValueError) as cm:
                qb.build("t2i", prompt="x", te_speed=True)
        self.assertIn(qb.CACHE, str(cm.exception))

    def test_rejects_non_boolean(self):
        for bad in (1, 0, "true", "yes", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    qb.build("t2i", prompt="x", te_speed=bad)

    def test_the_two_accelerators_are_mutually_exclusive(self):
        with self.assertRaises(ValueError) as cm:
            qb.build("t2i", prompt="x", lazy_cache=True, te_speed=True)
        msg = str(cm.exception)
        self.assertIn("LazyCache", msg)
        self.assertIn("TE-Speed", msg)
        # 报错要自带「为什么推荐 TE-Speed」的实测数字，否则用户还得去翻文档
        self.assertIn("0.953", msg)

    def test_the_exclusion_check_runs_before_the_graph_is_touched(self):
        """拒掉时必须**什么都没做**：不能先加载基准图、更不能留下半张接错的图。
        顺手也证明了这条检查不依赖基准图存在（否则会报成 FileNotFoundError）。"""
        with mock.patch.object(qb, "load_base") as lb:
            with self.assertRaises(ValueError):
                qb.build("t2i", prompt="x", lazy_cache=True, te_speed=True)
            lb.assert_not_called()

    def test_wiring_te_speed_onto_an_already_lazy_wired_graph_says_so(self):
        """绕过 build() 直接接（老脚本/手工拼图会这么干）时，报错要点出可能是
        LazyCache 先占住了位置 —— 只说「指向不是 459:480」帮不上忙。

        这里必须显式 `te_speed=False`：默认已经是接了，不关掉连图都建不出来。"""
        wf = qb.build("t2i", prompt="x", lazy_cache=True, te_speed=False)
        with self.assertRaises(ValueError) as cm:
            qb._wire_te_speed(wf)
        self.assertIn("LazyCache", str(cm.exception))


class TurboWiring(support.LeakCheckedTestCase):
    """Turbo LoRA（官方蒸馏权重）。**默认不挂**、挂在哪、与两个加速**不互斥**。

    这一组里最值钱的是**链顺序**那两条：LoRA 必须接在两个加速节点**之前**。
    实测顺序只影响耗时不影响画面（LoRA 在前 7.7 秒、反过来 10.4 秒，出图逐位
    相同）—— 也就是说接反了**没有症状**，只是每张图白花约 3 秒。而且写这组之前
    我正是先接反了一次：局部断言全过（上游对、下游对），只有整条链的顺序是错的，
    所以这里按**数据流方向**断言整条链，不只看 `inputs.model` 两个点。
    """

    MODE_KW = (("t2i", {}), ("t2i", {"rewrite": True}),
               ("edit", {"source_image": "ref.png"}))

    def chain(self, wf):
        """从 KSampler 反向走到 QwenImage21Cache，再倒过来 = **数据流方向**。"""
        ids, nid = [], (wf[qb.KSAMPLER]["inputs"]["model"])[0]
        while nid is not None:
            ids.append(nid)
            if nid == qb.CACHE:
                break
            m = wf[nid].get("inputs", {}).get("model")
            nid = m[0] if isinstance(m, list) else None
        self.assertEqual(ids[-1], qb.CACHE, "链没走到 QwenImage21Cache：%r" % (ids,))
        return [wf[i]["class_type"] for i in ids[::-1]]

    def test_default_is_off(self):
        """**默认不挂**（与 TE-Speed 那个默认值相反）。

        理由不是「保守」，是**证据只有一条提示词一个种子** —— 那种强度还不够格
        当默认值。所以这条用例钉的是「谁都不勾时图里没有这个节点」，而不是
        「它不好用」。
        """
        for mode, kw in self.MODE_KW:
            with self.subTest(mode=mode):
                wf = qb.build(mode, prompt="x", **kw)
                self.assertNotIn(qb.TURBO_LORA, wf)
                self.assertFalse(qb.describe(wf)["turbo"])

    def test_explicit_true_puts_the_lora_before_the_accelerator(self):
        """**顺序就是契约**：`Cache → LoRA → TE-Speed → KSampler`。"""
        for mode, kw in self.MODE_KW:
            with self.subTest(mode=mode):
                wf = qb.build(mode, prompt="x", turbo=True, **kw)
                self.assertEqual(self.chain(wf),
                                 ["QwenImage21Cache", "LoraLoaderModelOnly",
                                  "TESpeedQwenImage21"])
                self.assertTrue(qb.describe(wf)["turbo"])

    def test_it_also_goes_before_lazy_cache(self):
        """与 LazyCache 那条**没测过画质**，但结构上不冲突（LoRA 不抢那条线），
        所以构建器不拦。这条用例钉的只是「接在它前面」，不是「推荐这么用」。"""
        wf = qb.build("t2i", prompt="x", turbo=True, lazy_cache=True,
                      te_speed=False)
        self.assertEqual(self.chain(wf),
                         ["QwenImage21Cache", "LoraLoaderModelOnly", "LazyCache"])

    def test_it_is_not_mutually_exclusive_with_the_other_two(self):
        """`lazy_cache` 与 `te_speed` 互斥（抢同一条线），Turbo **不在那一对里** ——
        它是权重补丁。所以这两个组合都得能建出来，而且默认那组合还得是生产默认。"""
        for kw, want in ((dict(), ["QwenImage21Cache", "LoraLoaderModelOnly",
                                   "TESpeedQwenImage21"]),
                         (dict(te_speed=False), ["QwenImage21Cache",
                                                 "LoraLoaderModelOnly"])):
            with self.subTest(kw=kw):
                wf = qb.build("t2i", prompt="x", turbo=True, **kw)
                self.assertEqual(self.chain(wf), want)

    def test_the_lora_node_comes_from_the_measured_constants(self):
        node = qb.build("t2i", prompt="x", turbo=True)[qb.TURBO_LORA]
        self.assertEqual(node["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(node["inputs"]["lora_name"], qb.TURBO_LORA_FILE)
        self.assertEqual(node["inputs"]["strength_model"], qb.TURBO_STRENGTH)
        self.assertEqual(node["inputs"]["model"], [qb.CACHE, 0])

    def test_it_fills_steps_and_scheduler_only_when_they_are_unspecified(self):
        """一个开关要做完整件事：只挂 LoRA 不动步数，等于拿 25 步的配方喂一个
        按大步蒸馏过的模型。但**显式给了就以显式的为准** —— 否则用户没法做对照。"""
        auto = qb.build("t2i", prompt="x", turbo=True)[qb.KSAMPLER]["inputs"]
        self.assertEqual(auto["steps"], qb.TURBO_DEFAULT_STEPS)
        self.assertEqual(auto["scheduler"], qb.TURBO_DEFAULT_SCHEDULER)

        explicit = qb.build("t2i", prompt="x", turbo=True, steps=25,
                            scheduler="simple")[qb.KSAMPLER]["inputs"]
        self.assertEqual(explicit["steps"], 25)
        self.assertEqual(explicit["scheduler"], "simple")

    def test_it_does_not_touch_steps_when_off(self):
        """默认关的时候，基准图里那两格**一个字节都不许动** —— 黄金快照就是靠
        这条活着的。"""
        off = qb.build("t2i", prompt="x")[qb.KSAMPLER]["inputs"]
        self.assertEqual(off["scheduler"], "simple")
        self.assertEqual(off["steps"], 25)

    def test_wiring_twice_does_not_stack_two_nodes(self):
        """幂等。这里显式 `te_speed=False` —— 否则 tail 是 TE-Speed 不是 LoRA，
        断言会写成「LoRA 该在链尾」，那是在验另一件事。"""
        wf = qb.build("t2i", prompt="x", turbo=True, te_speed=False)
        qb._wire_turbo(wf)
        self.assertEqual(len([k for k, v in wf.items()
                              if v.get("class_type") == "LoraLoaderModelOnly"]), 1)
        self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"], [qb.TURBO_LORA, 0])
        self.assertEqual(wf[qb.TURBO_LORA]["inputs"]["model"], [qb.CACHE, 0])

    def test_wiring_twice_leaves_the_default_chain_alone(self):
        """默认组合（挂着 TE-Speed）下也一样幂等：tail 仍是 TE-Speed，
        而 TE-Speed 的上游仍是那个 LoRA。"""
        wf = qb.build("t2i", prompt="x", turbo=True)
        qb._wire_turbo(wf)
        self.assertEqual(len([k for k, v in wf.items()
                              if v.get("class_type") == "LoraLoaderModelOnly"]), 1)
        self.assertEqual(wf[qb.KSAMPLER]["inputs"]["model"], [qb.TE_SPEED, 0])
        self.assertEqual(wf[qb.TE_SPEED]["inputs"]["model"], [qb.TURBO_LORA, 0])

    def test_refuses_when_the_base_is_rewired_underneath(self):
        base = qb.load_base()
        base[qb.KSAMPLER]["inputs"]["model"] = [qb.UNET, 0]
        with mock.patch.object(qb, "load_base",
                               lambda path=None: json.loads(canonical(base))):
            with self.assertRaises(ValueError) as cm:
                qb.build("t2i", prompt="x", turbo=True)
        self.assertIn(qb.CACHE, str(cm.exception))

    def test_wiring_turbo_onto_an_already_accelerated_graph_says_so(self):
        """绕过 build() 直接接（老脚本 / 手工拼图会这么干）时，报错要点出
        「它必须先接」—— 只说「指向不是 459:480」帮不上忙。"""
        wf = qb.build("t2i", prompt="x")           # 已经接了 TE-Speed
        with self.assertRaises(ValueError) as cm:
            qb._wire_turbo(wf)
        self.assertIn("TE-Speed", str(cm.exception))

    def test_rejects_non_boolean(self):
        for bad in (1, 0, "true", "yes", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    qb.build("t2i", prompt="x", turbo=bad)

    def test_acceleration_line_names_both_layers(self):
        """加速那一行要能把**两层**都说出来（LoRA 是一层，加速节点是另一层），
        否则「到底挂了没挂」只能靠翻 JSON 猜。"""
        both = qb.acceleration_line(qb.build("t2i", prompt="x", turbo=True))
        self.assertIn("Turbo", both)
        self.assertIn("TE-Speed", both)
        only = qb.acceleration_line(qb.build("t2i", prompt="x", turbo=True,
                                             te_speed=False))
        self.assertIn("Turbo", only)
        self.assertNotIn("TE-Speed", only)


class NegativePromptWiring(support.LeakCheckedTestCase):
    """反向词：接线本来就是通的，**但默认的 CFG=1.0 下它不被求值**。

    这一组只管「写进去了没有、写对地方没有」。生不生效是**策略**，由
    `workbench/server.py::_warnings_for` 说破（守它的是 `test_api.py` 里那组）。
    分开是有意的：构建器负责如实表达，页面负责不让用户踩空。
    """

    def test_it_lands_on_the_text_encoder(self):
        wf = qb.build("t2i", prompt="x", negative_prompt="blurry, text")
        self.assertEqual(wf[qb.TEXT_ENCODE]["inputs"]["negative_prompt"],
                         "blurry, text")

    def test_edit_also_carries_it(self):
        """图生图那条路原先把 `negative_prompt` **写死成空串**。

        那会让这个模式静默丢掉反向词 —— 用户在页面上填了、运行记录里也有，
        而图里是空的。这正是本模块最想消灭的那种「没有症状的错」。
        """
        wf = qb.build("edit", prompt="make it blue", source_image="a.png",
                      negative_prompt="watermark")
        self.assertEqual(wf[qb.TEXT_ENCODE]["inputs"]["negative_prompt"],
                         "watermark")

    def test_none_leaves_the_base_value_untouched(self):
        """默认 None 必须让现有 golden 图**一个字节都不变**。"""
        base = qb.load_base()
        wf = qb.build("t2i", prompt="x")
        self.assertEqual(wf[qb.TEXT_ENCODE]["inputs"]["negative_prompt"],
                         base[qb.TEXT_ENCODE]["inputs"]["negative_prompt"])
        self.assertEqual(wf[qb.TEXT_ENCODE]["inputs"]["negative_prompt"], "")

    def test_an_explicit_empty_string_is_a_real_value(self):
        """前端在放大模式那边递的就是 `""` —— 这条也得能跑通，不能当成 None。"""
        wf = qb.build("t2i", prompt="x", negative_prompt="")
        self.assertEqual(wf[qb.TEXT_ENCODE]["inputs"]["negative_prompt"], "")

    def test_rejects_non_strings(self):
        for bad in (True, 0, ["a"], {"a": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    qb.build("t2i", prompt="x", negative_prompt=bad)
                self.assertIn("negative_prompt", str(cm.exception))

    def test_describe_reports_it_and_upscale_keeps_the_key(self):
        """放大的图**没有**文本编码器，所以它只能是空串 —— 但键必须在。

        「键缺失」和「键是 null」在前端是两种待遇，两张图的键集得一样。
        """
        qwen = qb.describe(qb.build("t2i", prompt="x",
                                    negative_prompt="jpeg artifacts"))
        up = qb.describe(qb.build_upscale("a.png"))
        self.assertEqual(qwen["negative_prompt"], "jpeg artifacts")
        self.assertIn("negative_prompt", up)
        self.assertEqual(up["negative_prompt"], "")


class MultiReferenceImages(support.LeakCheckedTestCase):
    """多张参考图：每张一个 `LoadImage`，连到 `images.image_N`（N 从 1 数）。

    **这不是我们发明的形状** —— `TextEncodeQwenImage21` 的 `images` 是 autogrow，
    源码里写死的候选名就是 `image_1`..`image_16`（`comfy_extras/nodes_qwen.py`）。
    所以这一组守的是「我们拼出来的连线与那个 schema 一致」，以及几件**很容易
    悄悄错**的事：顺序（第一张决定输出尺寸）、上限的来处、两份真源的一致性。
    """

    def test_two_images_get_two_nodes_and_two_wires(self):
        wf = qb.build("edit", prompt="x", source_images=["a.png", "b.png"])
        self.assertEqual(wf["900"]["inputs"]["image"], "a.png")
        self.assertEqual(wf["901"]["inputs"]["image"], "b.png")
        te = wf[qb.TEXT_ENCODE]["inputs"]
        self.assertEqual(te["images.image_1"], ["900", 0])
        self.assertEqual(te["images.image_2"], ["901", 0])
        # 第二条走的是**同一个**文本编码器与同一张 latent —— 编辑链路没有变
        self.assertEqual(wf[qb.KSAMPLER]["inputs"]["latent_image"],
                         [qb.TEXT_ENCODE, 2])

    def test_the_first_image_decides_the_output_size(self):
        """`nodes_qwen.py` 里 latent 是按**第一张**参考图的尺寸建的，而顺序来自
        `image_N` 的编号。所以「谁在第一位」是有后果的 —— 改写顺序或去重都会
        悄悄改掉输出尺寸。"""
        a = qb.build("edit", prompt="x", source_images=["a.png", "b.png"])
        b = qb.build("edit", prompt="x", source_images=["b.png", "a.png"])
        self.assertEqual(a[qb.TEXT_ENCODE]["inputs"]["images.image_1"], ["900", 0])
        self.assertEqual(a["900"]["inputs"]["image"], "a.png")
        self.assertEqual(b["900"]["inputs"]["image"], "b.png")
        self.assertEqual(qb.describe(a)["source_images"], ["a.png", "b.png"])
        self.assertEqual(qb.describe(b)["source_images"], ["b.png", "a.png"])

    def test_order_is_kept_and_duplicates_are_not_quietly_dropped(self):
        """构建器**不替用户改主意**：不去重、不排序。

        去重看着像帮忙，但它会改掉「第几张是哪张」，而那是一条没有症状的错。
        """
        wf = qb.build("edit", prompt="x", source_images=["b.png", "a.png", "b.png"])
        self.assertEqual(qb.describe(wf)["source_images"],
                         ["b.png", "a.png", "b.png"])

    def test_a_single_image_is_the_same_graph_either_way(self):
        """单张：`source_image=` 与 `source_images=[…]` 必须给出**同一个字节**。
        两条写法要是能分岔，那「老客户端不受影响」就是句空话。"""
        one = qb.build("edit", prompt="x", source_image="a.png")
        two = qb.build("edit", prompt="x", source_images=["a.png"])
        self.assertEqual(canonical(one), canonical(two))

    def test_the_cap_comes_from_the_node_schema(self):
        """超过 16 张要**当场**拒，并且说出这个数的来处 ——
        真让它跑到后端，等到的是一句 value_not_in_list（那时已经提交完了）。"""
        ok = ["r%d.png" % i for i in range(qb.MAX_REFERENCE_IMAGES)]
        wf = qb.build("edit", prompt="x", source_images=ok)
        self.assertEqual(len([k for k, v in wf.items()
                              if v.get("class_type") == "LoadImage"]),
                         qb.MAX_REFERENCE_IMAGES)
        with self.assertRaises(ValueError) as cm:
            qb.build("edit", prompt="x", source_images=ok + ["r16.png"])
        msg = str(cm.exception)
        self.assertIn("image_%d" % qb.MAX_REFERENCE_IMAGES, msg)
        self.assertIn("nodes_qwen.py", msg)

    def test_edit_still_needs_at_least_one(self):
        for bad in ([], (), [""], ["   "]):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as cm:
                    qb.build("edit", prompt="x", source_images=bad)
                self.assertIn("source_image", str(cm.exception))

    def test_two_sources_that_disagree_are_rejected(self):
        """同一件事给两份真源，且两份不一样 —— 谁也不知道该信哪个。当场炸。"""
        with self.assertRaises(ValueError) as cm:
            qb.build("edit", prompt="x", source_image="a.png",
                     source_images=["b.png"])
        self.assertIn("不一致", str(cm.exception))

    def test_the_same_source_given_twice_ways_is_fine(self):
        wf = qb.build("edit", prompt="x", source_image="a.png",
                      source_images=["a.png", "b.png"])
        self.assertEqual(qb.describe(wf)["source_images"], ["a.png", "b.png"])

    def test_a_bare_string_for_the_list_is_rejected_not_split(self):
        with self.assertRaises(ValueError) as cm:
            qb.build("edit", prompt="x", source_images="a.png")
        self.assertIn("source_images", str(cm.exception))

    def test_non_string_entries_are_rejected(self):
        for bad in (["a.png", 7], ["a.png", None], ["a.png", ["b.png"]]):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    qb.build("edit", prompt="x", source_images=bad)

    def test_stale_autogrow_wires_are_cleared_not_left_behind(self):
        """基准图（或别人拼的图）里若已经挂着 `images.image_2`，而这次只给一张，
        那条旧连线**必须被清掉**。留着它会指到一个已经被删的节点上 ——
        而那种错能跑，只是参考图不是你以为的那几张。"""
        base = qb.load_base()
        base[qb.TEXT_ENCODE]["inputs"]["images.image_1"] = ["999", 0]
        base[qb.TEXT_ENCODE]["inputs"]["images.image_2"] = ["998", 0]
        with mock.patch.object(qb, "load_base",
                               lambda path=None: json.loads(canonical(base))):
            wf = qb.build("edit", prompt="x", source_image="a.png")
        te = wf[qb.TEXT_ENCODE]["inputs"]
        self.assertEqual(sorted(k for k in te if k.startswith("images.")),
                         ["images.image_1"])
        self.assertEqual(te["images.image_1"], ["900", 0])
        self.assertNotIn("998", wf)
        self.assertNotIn("999", wf)

    def test_describe_reports_both_keys_and_upscale_keeps_pace(self):
        qwen = qb.describe(qb.build("edit", prompt="x",
                                    source_images=["a.png", "b.png"]))
        self.assertEqual(qwen["source_images"], ["a.png", "b.png"])
        self.assertEqual(qwen["source_image"], "a.png")
        up = qb.describe(qb.build_upscale("a.png"))
        self.assertEqual(up["source_images"], ["a.png"])
        self.assertEqual(up["source_image"], "a.png")
        # 键集一致：两张图的 describe 形状不能分家（前端按同一份字段渲染）
        self.assertEqual(sorted(qwen), sorted(up))

    def test_t2i_reports_an_empty_list_not_a_missing_key(self):
        d = qb.describe(qb.build("t2i", prompt="x"))
        self.assertEqual(d["source_images"], [])
        self.assertIsNone(d["source_image"])


class SavePrefixPerSubmission(support.LeakCheckedTestCase):
    """「同一份参数再提一遍也必须写出一个新文件」的地基（2026-10-02，P20g）。

    ComfyUI 的缓存认的是**节点输入**，不是「这是一次新的提交」：一字不改地再提一遍，
    它**一个节点都不跑**，只把上一次的产物名回放给 history（`execution.py` 里命中
    缓存的节点走 `emit_cached_output()` 就返回）。于是「重复生图」既不写文件、也没有
    新图 —— 上一次那张要是已经被删（P20f 的删除是进回收站），连那张也回不来。

    所以要动的正是**收尾节点唯一那个输入**：前缀。
    """

    def t2i(self):
        return qb.build("t2i", prompt=T2I_PROMPT, steps=30, seed=111)

    def test_only_the_save_prefix_changes(self):
        wf = self.t2i()
        before = json.loads(canonical(wf))
        base = before[qb.SAVE]["inputs"]["filename_prefix"]
        got = qb.unique_save_prefix(wf, "abc12345")
        self.assertEqual(got, base + "_abc12345")
        self.assertEqual(wf[qb.SAVE]["inputs"]["filename_prefix"], got)
        changed = [nid for nid in wf if canonical(wf[nid]) != canonical(before[nid])]
        self.assertEqual(changed, [qb.SAVE],
                         "除了收尾节点的前缀，一个字节都不该动")

    def test_two_submissions_never_share_a_prefix(self):
        a, b = self.t2i(), self.t2i()
        self.assertNotEqual(qb.unique_save_prefix(a, "aaaaaaaa"),
                            qb.unique_save_prefix(b, "bbbbbbbb"))

    def test_the_upscale_graph_too(self):
        """放大是另一条链路（`SaveImage`，节点 id 也是另一套），但同一条规矩。"""
        wf = qb.build_upscale("src.png")
        prefix = qb.unique_save_prefix(wf, "abc12345")
        self.assertEqual(prefix, qb.UPSCALE_PREFIX + "_abc12345")
        self.assertEqual(wf[qb.UP_SAVE]["inputs"]["filename_prefix"], prefix)

    def test_it_reads_the_class_not_the_id(self):
        """按**节点类**认收尾节点 —— id 是我们自己铸的，换个构建器就不一样了。"""
        wf = {"7": {"class_type": "SaveImageAdvanced",
                    "inputs": {"filename_prefix": "x", "images": ["1", 0]}}}
        self.assertEqual(qb.unique_save_prefix(wf, "tok"), "x_tok")

    def test_no_save_node_is_loud(self):
        """没有收尾节点 = 写不出文件。那就当场炸，别静默地「出图成功」。"""
        with self.assertRaises(ValueError) as cm:
            qb.unique_save_prefix({"1": {"class_type": "KSampler",
                                         "inputs": {"seed": 1}}}, "tok")
        self.assertIn("收尾节点", str(cm.exception))

    def test_the_base_graph_is_untouched(self):
        """`build()` 自己不塞前缀 —— 黄金快照逐字节相同这条不变量不能被这件事碰到。"""
        wf = self.t2i()
        base = qb.load_base()[qb.SAVE]["inputs"]["filename_prefix"]
        self.assertEqual(wf[qb.SAVE]["inputs"]["filename_prefix"], base)
        self.assertFalse(base.endswith("_"),
                         "基准里的名字不该以 `_` 结尾：拼接那件事只写在 unique_save_prefix 里")


if __name__ == "__main__":
    unittest.main(verbosity=2)
