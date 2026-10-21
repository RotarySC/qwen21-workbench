#!/usr/bin/env python3
"""`pngmeta.py` —— 纯标准库的 PNG tEXt 解析。

它存在的理由是「画廊不需要数据库」：ComfyUI 把整份提交的 API 图塞进 PNG，
读元数据就知道模式/seed/步数/尺寸/提示词，**后端没起也能用**。
所以这里要守的是**对畸形输入的耐受** —— 磁盘上那堆历史图里什么都可能有。
"""
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench import pngmeta  # noqa: E402


def make_png(chunks):
    """拼一个最小合法 PNG（签名 + 若干块 + IEND）。"""
    out = [b"\x89PNG\r\n\x1a\n"]
    for ctype, body in chunks:
        out.append(struct.pack(">I", len(body)) + ctype + body)
        out.append(struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))
    return b"".join(out)


def text_chunk(key, value):
    return (b"tEXt", key.encode("latin-1") + b"\x00" + value.encode("latin-1"))


class TextChunks(support.LeakCheckedTestCase):
    def test_reads_text_chunk(self):
        blob = make_png([text_chunk("prompt", '{"a": 1}'), (b"IEND", b"")])
        self.assertEqual(pngmeta.png_text_from_bytes(blob), {"prompt": '{"a": 1}'})

    def test_reads_itxt_utf8(self):
        # keyword \0 compflag \0 compmethod \0 lang \0 translated \0 text
        body = "note".encode("latin-1") + b"\x00\x00\x00\x00\x00"
        body += "中文".encode("utf-8")
        blob = make_png([(b"iTXt", body), (b"IEND", b"")])
        self.assertEqual(pngmeta.png_text_from_bytes(blob), {"note": "中文"})

    def test_ignores_other_chunks(self):
        blob = make_png([(b"IHDR", b"\x00" * 13),
                         text_chunk("k", "v"), (b"IEND", b"")])
        self.assertEqual(pngmeta.png_text_from_bytes(blob), {"k": "v"})

    def test_stops_at_iend(self):
        blob = make_png([text_chunk("a", "1"), (b"IEND", b"")]) + \
            struct.pack(">I", 3) + b"tEXt" + b"b\x00x" + b"\x00\x00\x00\x00"
        self.assertEqual(pngmeta.png_text_from_bytes(blob), {"a": "1"})

    def test_non_png_is_empty_not_an_exception(self):
        self.assertEqual(pngmeta.png_text_from_bytes(b"not a png at all"), {})
        self.assertEqual(pngmeta.png_text_from_bytes(b""), {})
        self.assertEqual(pngmeta.png_text_from_bytes(None), {})

    def test_truncated_file_does_not_raise(self):
        blob = make_png([text_chunk("prompt", '{"x":1}'), (b"IEND", b"")])
        for cut in (4, 12, 20, len(blob) - 3):
            pngmeta.png_text_from_bytes(blob[:cut])     # 只要不抛就算过


class PromptWorkflow(support.LeakCheckedTestCase):
    def test_roundtrip(self):
        import json
        wf = {"459:474": {"class_type": "ComfySwitchNode",
                          "inputs": {"switch": False, "on_false": "a cat"}}}
        blob = make_png([text_chunk("prompt", json.dumps(wf)), (b"IEND", b"")])
        self.assertEqual(pngmeta.prompt_workflow(blob), wf)

    def test_missing_or_broken_prompt_returns_none(self):
        self.assertIsNone(pngmeta.prompt_workflow(
            make_png([text_chunk("other", "x"), (b"IEND", b"")])))
        self.assertIsNone(pngmeta.prompt_workflow(
            make_png([text_chunk("prompt", "{not json"), (b"IEND", b"")])))

    def test_reads_from_a_path(self):
        import json
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-png-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "x.png")
        with open(path, "wb") as f:
            f.write(make_png([text_chunk("prompt", json.dumps({"k": 1})),
                              (b"IEND", b"")]))
        self.assertEqual(pngmeta.prompt_workflow(path), {"k": 1})


class Summarize(support.LeakCheckedTestCase):
    def build(self, **over):
        wf = {
            "459:474": {"class_type": "ComfySwitchNode",
                        "inputs": {"switch": False, "on_false": "a cat"}},
            "459:471": {"class_type": "TextGenerate",
                        "inputs": {"prompt": "a cat"}},
            "459:458": {"class_type": "KSampler",
                        "inputs": {"steps": 20, "seed": 7, "cfg": 1.0,
                                   "sampler_name": "euler",
                                   "scheduler": "simple"}},
            "459:452": {"class_type": "TextEncodeQwenImage21",
                        "inputs": {"negative_prompt": "", "prompt": "a cat"}},
            "459:456": {"class_type": "EmptyLatentImage",
                        "inputs": {"width": 1024, "height": 1024}},
        }
        wf.update(over)
        return wf

    def test_reads_the_basics(self):
        info = pngmeta.summarize(self.build())
        self.assertEqual(info["mode"], "t2i")
        self.assertEqual(info["steps"], 20)
        self.assertEqual(info["seed"], 7)
        self.assertEqual((info["width"], info["height"]), (1024, 1024))
        self.assertEqual(info["prompt"], "a cat")
        self.assertFalse(info["is_2k"])

    def test_switch_true_is_rewrite_not_a_mode(self):
        """改写是**额外功能**（2026-10-01 起）：`switch=True` 只让 `rewrite` 为真。

        老图（历史 refine 产物）走的正是这条路 —— 所以它们一张都不会丢，
        只是标签从「模式 Refine」变成「改写过」。
        """
        wf = self.build()
        wf["459:474"]["inputs"]["switch"] = True
        info = pngmeta.summarize(wf)
        self.assertEqual(info["mode"], "t2i")
        self.assertTrue(info["rewrite"])
        # 没开关器的那张必须是 False，而不是缺键（两条链的字段形状不许分家）
        self.assertFalse(pngmeta.summarize(self.build())["rewrite"])

    def test_a_link_shaped_prompt_is_empty_not_a_list(self):
        """老图（断边年代）的 `on_false` / `prompt` 是一条 **link**，不是字符串。

        `prompt` 的契约是字符串：画廊搜索是 `needle in x["prompt"].lower()`，放过一个
        list 的后果不是「少显示一句话」，而是**在搜索框里输入任何字都 500** —— 而且只在
        扫到那一张时才炸（本机 `Qwen_image_2.1_00001/00002` 两张真图就是这样）。

        收成 `""` 是如实的：那两张图身上根本没有「生效过的提示词」。
        """
        wf = self.build()
        wf["459:474"]["inputs"]["on_false"] = ["459:472", 0]
        wf["459:452"]["inputs"]["prompt"] = ["459:472", 0]
        wf["459:452"]["inputs"]["negative_prompt"] = ["459:473", 0]
        info = pngmeta.summarize(wf)
        self.assertIsInstance(info["prompt"], str)
        self.assertIsInstance(info["negative_prompt"], str)
        self.assertEqual(info["prompt"], "")
        self.assertEqual(info["negative_prompt"], "")

    def test_a_missing_negative_prompt_is_a_string_not_none(self):
        wf = self.build()
        wf["459:452"]["inputs"]["negative_prompt"] = None
        self.assertEqual(pngmeta.summarize(wf)["negative_prompt"], "")

    def test_2048_is_flagged(self):
        wf = self.build()
        wf["459:456"]["inputs"] = {"width": 2048, "height": 2048}
        self.assertTrue(pngmeta.summarize(wf)["is_2k"])
        self.assertEqual(pngmeta.summarize(wf)["width"], 2048)

    def test_load_image_means_edit(self):
        wf = self.build()
        wf["900"] = {"class_type": "LoadImage", "inputs": {"image": "ref.png"}}
        info = pngmeta.summarize(wf)
        self.assertEqual(info["mode"], "edit")
        self.assertEqual(info["source_image"], "ref.png")
        self.assertEqual(info["source_images"], ["ref.png"])

        self.assertIsNone(info["width"])

    def test_several_references_are_read_in_image_number_order(self):
        """顺序**按 `image_N` 的编号**，不按字典的插入顺序 ——
        第一张决定输出尺寸（`nodes_qwen.py` 里 latent 就是照它建的），
        顺序读错会让画廊回填出另一张图。
        """
        wf = self.build()
        wf["900"] = {"class_type": "LoadImage", "inputs": {"image": "first.png"}}
        wf["901"] = {"class_type": "LoadImage", "inputs": {"image": "second.png"}}
        wf["459:452"]["inputs"]["images.image_2"] = ["901", 0]
        wf["459:452"]["inputs"]["images.image_1"] = ["900", 0]
        info = pngmeta.summarize(wf)
        self.assertEqual(info["source_images"], ["first.png", "second.png"])
        self.assertEqual(info["source_image"], "first.png")
        # 键集与放大那张图一致（两种 summarize 不能分家）
        self.assertEqual(sorted(info),
                         sorted(pngmeta.summarize(
                             {"1": {"class_type": "LoadImage",
                                    "inputs": {"image": "s.png"}},
                              "4": {"class_type": "TESpeedVOSR2Image",
                                    "inputs": {"seed": 1, "scale": 2}}})))

    def test_an_unwired_loadimage_is_not_a_reference(self):
        """图里多挂一个**没接到文本编码器**的 LoadImage（手搭的图会有），
        它不该被算成参考图 —— 连线才代表真的喂进去了。"""
        wf = self.build()
        wf["900"] = {"class_type": "LoadImage", "inputs": {"image": "used.png"}}
        wf["950"] = {"class_type": "LoadImage", "inputs": {"image": "dangling.png"}}
        wf["459:452"]["inputs"]["images.image_1"] = ["900", 0]
        self.assertEqual(pngmeta.summarize(wf)["source_images"], ["used.png"])

    def test_old_graphs_without_autogrow_keys_still_read(self):
        """构建器之前那些手搭的图没有 `images.image_N` 键，只有 LoadImage 节点。
        画廊要能读**任何**一份历史 PNG，所以那条兜底路径必须留着。"""
        wf = self.build()
        wf["900"] = {"class_type": "LoadImage", "inputs": {"image": "old.png"}}
        self.assertEqual(pngmeta.summarize(wf)["source_images"], ["old.png"])

    def test_reports_the_cache_acceleration_node(self):
        """画廊要能看出哪张图开了缓存加速 —— 否则从画廊回填表单时开关会静默丢掉，
        用户以为自己在复现那张图，其实参数已经变了。"""
        self.assertFalse(pngmeta.summarize(self.build())["lazy_cache"])
        wf = self.build(**{"990": {"class_type": "LazyCache",
                                   "inputs": {"reuse_threshold": 0.2}}})
        self.assertTrue(pngmeta.summarize(wf)["lazy_cache"])

    def test_reports_the_te_speed_node(self):
        """与缓存加速同理：画廊看不出这张图开了 TE-Speed 的话，回填表单时那个
        开关会静默变成关，用户以为自己在复现那张图，其实参数已经变了 ——
        而且更慢的那个方向（关掉加速）不会让人起疑，于是错得更久。"""
        self.assertFalse(pngmeta.summarize(self.build())["te_speed"])
        wf = self.build(**{"9900": {"class_type": "TESpeedQwenImage21",
                                    "inputs": {"reuse_threshold": 0.2}}})
        self.assertTrue(pngmeta.summarize(wf)["te_speed"])

    def test_reports_the_turbo_lora(self):
        """同一条规矩：画廊看不出这张图挂了 Turbo 的话，回填表单时那个开关会
        静默变成关 —— 而 Turbo 不只是「接一个节点」，它**顺带把步数与调度器
        换了配方**，所以回填错了等于换了另一套参数，比少一个加速难察觉得多。

        判据是 **id + 类名一起**：`LoraLoaderModelOnly` 是个**通用类**，任何
        LoRA 都是它；光看类名会把「用户自己挂了别的 LoRA」也认成 turbo。
        """
        self.assertFalse(pngmeta.summarize(self.build())["turbo"])
        wf = self.build(**{"9800": {"class_type": "LoraLoaderModelOnly",
                                    "inputs": {"lora_name": "x.safetensors"}}})
        self.assertTrue(pngmeta.summarize(wf)["turbo"])
        # id 撞上、类名不对 —— 不算
        other = self.build(**{"9800": {"class_type": "LoraLoader",
                                       "inputs": {"lora_name": "x.safetensors"}}})
        self.assertFalse(pngmeta.summarize(other)["turbo"])

    def test_linked_dimensions_report_none_not_a_number(self):
        """基准图里 width/height 是指向 ResolutionSelector 的链接，
        不是字面量。硬把列表当数字回显会骗人。"""
        wf = self.build()
        wf["459:456"]["inputs"] = {"width": ["13", 0], "height": ["13", 1]}
        info = pngmeta.summarize(wf)
        self.assertIsNone(info["width"])
        self.assertIsNone(info["height"])

    def test_garbage_input(self):
        self.assertEqual(pngmeta.summarize(None), {})
        self.assertEqual(pngmeta.summarize("nope"), {})
        # 空图不该抛异常，也不该硬套成 t2i —— 那是 P2 改掉的旧行为，
        # 会让画廊把一张别人模型的图显示成一条假的 Qwen 记录。
        empty = pngmeta.summarize({})
        self.assertIsNone(empty["mode"])
        self.assertFalse(empty["recognized"])
        self.assertIsNone(empty["steps"])
        self.assertIsNone(empty["width"])
        self.assertEqual(empty["prompt"], "")

    def test_unrelated_graph_is_not_labelled_t2i(self):
        """MINIMAX 那条链路的图也有 KSampler，但不在 459:458 上 ——
        如实说「不认识」，别让它冒充本链路的记录。"""
        foreign = {"1": {"class_type": "UNETLoader",
                         "inputs": {"unet_name": "minimax_h3.safetensors"}},
                   "2": {"class_type": "KSampler", "inputs": {"steps": 8}}}
        info = pngmeta.summarize(foreign)
        self.assertIsNone(info["mode"])
        self.assertFalse(info["recognized"])
        self.assertIsNone(info["unet"])          # unet 只在 459:451 上取
        self.assertIsNone(info["steps"])


def qwen_graph():
    """一份最小但能被认成 Qwen 链路的图（键集比对用）。"""
    return {
        "459:452": {"class_type": "TextEncodeQwenImage21",
                    "inputs": {"prompt": "a green apple", "negative_prompt": ""}},
        "459:458": {"class_type": "KSampler",
                    "inputs": {"steps": 20, "seed": 12345, "cfg": 1.0}},
        "459:456": {"class_type": "EmptyLatentImage",
                    "inputs": {"width": 1024, "height": 1024}},
    }


def vosr2_graph(scale=2, seed=42, source="a.png"):
    """一份最小但能被认出来的 VOSR2 放大图。

    刻意**没有 KSampler** —— 那正是它最容易掉进出图那套逻辑、
    然后被判成「不认识」（mode=None）的原因，而它偏偏是我们自己的产物。
    """
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": source}},
        "4": {"class_type": "TESpeedVOSR2Image",
              "inputs": {"scale": scale, "seed": seed}},
    }


class UpscaleSummary(support.LeakCheckedTestCase):
    """放大图在画廊里必须**认得出来**，而且不能被当成出图来展示。"""

    def test_it_is_recognized_as_upscale(self):
        info = pngmeta.summarize(vosr2_graph())
        self.assertEqual(info["mode"], "upscale")
        self.assertTrue(info["recognized"])       # 是本链路的产物，只是模型不同
        self.assertEqual(info["scale"], 2)
        self.assertEqual(info["seed"], 42)
        self.assertEqual(info["source_image"], "a.png")

    def test_it_reports_none_for_what_it_does_not_have(self):
        """提示词/步数/cfg/采样器/尺寸 —— 放大图里一个都没有，必须如实报 None，
        而不是掉进出图那套被填上一堆假的。"""
        info = pngmeta.summarize(vosr2_graph())
        for key in ("steps", "cfg", "sampler", "scheduler", "width", "height"):
            self.assertIsNone(info[key], key)
        self.assertEqual(info["prompt"], "")
        self.assertFalse(info["lazy_cache"])
        self.assertFalse(info["te_speed"])
        self.assertFalse(info["is_2k"])           # 读不到真实输出尺寸就不打这个标

    def test_recognized_by_class_not_by_node_id(self):
        wf = {"whatever": {"class_type": "TESpeedVOSR2Image",
                           "inputs": {"scale": 8}}}
        self.assertEqual(pngmeta.summarize(wf)["mode"], "upscale")

    def test_both_summaries_have_the_same_keys(self):
        """两种图的 summarize **键集必须一致** —— 前端拿到「键缺失」
        和拿到 null 是两种待遇。"""
        up = pngmeta.summarize(vosr2_graph())
        qwen = pngmeta.summarize(qwen_graph())
        self.assertEqual(sorted(up), sorted(qwen))
        self.assertIsNone(qwen["scale"])

    def test_a_foreign_graph_stays_foreign(self):
        """能认出放大图，不该顺手把 MINIMAX 那种也认成自家人。"""
        info = pngmeta.summarize({"2": {"class_type": "KSampler",
                                        "inputs": {"steps": 8}}})
        self.assertIsNone(info["mode"])
        self.assertFalse(info["recognized"])


class PngSize(support.LeakCheckedTestCase):
    """只读 IHDR 的尺寸读取 —— 放大的「输出多大」全靠它。"""

    def blob(self, w, h):
        return make_png([(b"IHDR", struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"),
                         (b"IEND", b"")])

    def test_reads_the_real_header(self):
        self.assertEqual(pngmeta.png_size(self.blob(1024, 768)), (1024, 768))
        self.assertEqual(pngmeta.png_size(self.blob(4096, 2048)), (4096, 2048))

    def test_reads_from_a_path(self):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-png-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, "a.png")
        with open(p, "wb") as f:
            f.write(self.blob(2048, 512))
        self.assertEqual(pngmeta.png_size(p), (2048, 512))

    def test_returns_none_instead_of_faking_a_number(self):
        """读不到就 None，**不是 (0,0)** —— 调用方必须能区分
        「读不到」与「读到了但是零」，否则会把「不知道」当成「很小」。"""
        for blob in (b"", b"not a png at all", b"\x89PNG\r\n\x1a\n",
                     b"\x89PNG\r\n\x1a\n" + b"z" * 40):     # 签名对，但没有 IHDR
            with self.subTest(blob=blob[:17]):
                self.assertIsNone(pngmeta.png_size(blob))

    def test_a_missing_file_is_none_not_an_exception(self):
        self.assertIsNone(pngmeta.png_size(os.path.join("Z:\\nope", "a.png")))


class RealOutputs(support.LeakCheckedTestCase):
    """如果磁盘上已有真实出图，顺手断言真家伙也解析得动。

    找不到就跳过 —— 这条不该因为「output 目录被清过」而变红。
    """

    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        # 候选目录由 support 统一找（工作区 `output/` 优先，安装目录兜底）——
        # 写死安装目录的版本在落点搬家后会静默跳过，套件还是绿的。
        self.files = support.real_output_pngs()[-4:]

    def test_real_pngs_yield_a_prompt_workflow(self):
        if not self.files:
            self.skipTest("output/ 里没有 PNG")
        for path in self.files:
            wf = pngmeta.prompt_workflow(path)
            self.assertIsInstance(wf, dict, path)
            info = pngmeta.summarize(wf)
            self.assertIn(info["mode"], ("t2i", "edit", "upscale"), path)
            # `rewrite` 是**额外功能**，必须每个反解结果里都有这个键（老图、
            # 新图、放大图一视同仁）—— 缺键与 False 在前端是两种待遇。
            self.assertIsInstance(info["rewrite"], bool, path)
            self.assertIsInstance(info["seed"], int, path)
            # 放大是 one-step 模型，**没有步数这个旋钮**；它的必填项是源图。
            # 分开断言比「都在预期内」更有用 —— 放大图若报出一个 int 步数，
            # 那说明反解又把它当成了出图。
            if info["mode"] == "upscale":
                self.assertIsNone(info["steps"], path)
                self.assertTrue(info["source_image"], path)
                self.assertIsInstance(info["scale"], int, path)
            else:
                self.assertIsInstance(info["steps"], int, path)
                self.assertIsNone(info["scale"], path)

    def test_real_pngs_carry_a_nonempty_prompt(self):
        """断边修复的证据也在这里：真实产物里 `on_false` 必须是句人话，
        而不是空串。空串就说明当时提交的图还是坏的。

        放大图**按设计没有提示词**（VOSR2 只吃一张图），所以跳过它 ——
        这不是给它开后门，是因为它根本没有「提示词进没进编码器」这个问题。
        """
        if not self.files:
            self.skipTest("output/ 里没有 PNG")
        for path in self.files:
            info = pngmeta.summarize(pngmeta.prompt_workflow(path))
            if info["mode"] in ("edit", "upscale"):
                continue
            self.assertTrue(info["prompt"].strip(), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
