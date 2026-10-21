#!/usr/bin/env python3
"""表单**行为** —— 用 node + 一个假 DOM 跑 `tests/form_probe.js`。

为什么值得单独一层：这个 agent 没有浏览器驱动（见 AGENTS.md「页面渲染没被真正
看过」），所以「切到放大模式以后提示词框到底藏没藏」「`collectBody()` 发出去的
body 长什么样」这件事**没有任何地方在守**。而那个 body 正是前后端之间的契约 ——
它发错了只有到浏览器里才会暴露，而这里根本看不见浏览器。

`test_api.py::FrontendContract` 守的是**一致性**（id / SSE 名字 / 路由 / 语法），
它不知道行为；`node --check` 只证明「能解析」。这一层补的就是那一段。

**这不是浏览器**：只喂 `document.getElementById` 和一个 `classList`，
不渲染、不算样式、不看布局。所以它验的是逻辑，不是长相。
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import support  # noqa: E402

APP_JS = os.path.join(REPO, "workbench", "static", "app.js")
PROBE_JS = os.path.join(HERE, "form_probe.js")


@unittest.skipUnless(shutil.which("node") or shutil.which("node.exe"),
                     "没有 node —— 前端行为验不了（不是失败，是没工具）")
class FormBehaviour(support.LeakCheckedTestCase):
    """四种模式各自该露出什么、该发什么。"""

    @classmethod
    def setUpClass(cls):
        node = shutil.which("node") or shutil.which("node.exe")
        try:
            r = subprocess.run([node, PROBE_JS, APP_JS], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=60)
        except subprocess.TimeoutExpired:
            raise AssertionError("form_probe.js 超时（60s）")
        if r.returncode != 0:
            raise AssertionError("form_probe.js 挂了：\n%s\n%s" % (r.stdout, r.stderr))
        try:
            cls.data = json.loads(r.stdout)
        except ValueError:
            raise AssertionError("form_probe.js 的输出不是 JSON：\n%s" % r.stdout)

    def probe(self, name):
        self.assertIn(name, self.data, "探针没给出 %r" % name)
        return self.data[name]

    # --- 放大：藏掉出图那一套，露出源图与倍数 -----------------------------
    def test_upscale_hides_the_generation_only_controls(self):
        """提示词/反向词/词库/步数/cfg/高级 —— 在放大模式里**一个都不该出现**。

        留着不只是没用：会让用户以为调了有用，而那张图里根本没有那些旋钮。
        """
        h = self.probe("upscale")["hidden"]
        for key in ("prompt", "negative", "library", "gen", "cfg", "advanced"):
            self.assertTrue(h[key], "%s 应当藏起来" % key)
        self.assertFalse(h["src"])
        self.assertFalse(h["upscale"])

    def test_upscale_shows_the_source_and_scale(self):
        d = self.probe("upscale")
        self.assertFalse(d["hidden"]["src"])
        self.assertNotIn("hidden", d["srcWrapClasses"])
        self.assertIn("源图", d["srcLabel"])

    def test_upscale_body_carries_only_what_upscale_has(self):
        body = self.probe("upscale")["body"]
        self.assertEqual(body["mode"], "upscale")
        self.assertEqual(body["prompt"], "")       # 不是 null，也不是提示词
        self.assertEqual(body["source_image"], "a.png")
        self.assertEqual(body["scale"], 2)
        self.assertIsNone(body["steps"])
        self.assertIsNone(body["cfg"])
        self.assertIsNone(body["sampler"])
        self.assertIsNone(body["scheduler"])
        self.assertIsNone(body["width"])
        self.assertIsNone(body["height"])

    def test_upscale_body_has_the_same_keys_as_t2i(self):
        """键集**恒定** —— 任务记录、画廊项、表单回显三处共用这一份形状。
        「键缺失」和「键是 null」在前端是两种待遇（undefined 会传染）。"""
        up = self.probe("upscale")["body"]
        t2i = self.probe("t2i")["body"]
        self.assertEqual(sorted(up), sorted(t2i))

    def test_upscale_never_leaks_a_generation_accelerator(self):
        """先勾上 Turbo / TE-Speed / LazyCache 再切到放大：那几个框在
        `advanced-wrap` 里，跟着一起**藏起来了**，值就绝不能被带进请求 ——
        否则记录里会写着「这次开了 TE-Speed」，而那张图根本没经过 KSampler。
        那是一条假的参数记录。

        Turbo 尤其要一起判：它还会**顺带改步数与调度器**，带走的话那条假记录
        就不只是多了个开关，而是一整套对不上的参数。
        """
        body = self.probe("upscale_with_accels")["body"]
        self.assertIs(body["te_speed"], False)
        self.assertIs(body["lazy_cache"], False)
        self.assertIs(body["turbo"], False)

    # --- 别的模式不许被这次改动带跑 ---------------------------------------
    def test_t2i_is_unchanged(self):
        d = self.probe("t2i")
        self.assertFalse(d["hidden"]["prompt"])
        self.assertFalse(d["hidden"]["gen"])
        self.assertFalse(d["hidden"]["advanced"])
        self.assertTrue(d["hidden"]["src"])        # 文生图不需要源图
        self.assertTrue(d["hidden"]["upscale"])
        self.assertEqual(d["body"]["prompt"], "a green apple on a table")
        self.assertEqual((d["body"]["width"], d["body"]["height"]), (1024, 1024))
        self.assertEqual(d["body"]["steps"], 20)
        self.assertIsNone(d["body"]["scale"])

    def test_edit_still_shows_one_shared_source_field(self):
        """图生图与放大**共用**同一个源图字段（同一个上传通道），
        不是各来一份 —— 两份会飘。"""
        d = self.probe("edit")
        self.assertFalse(d["hidden"]["src"])
        self.assertTrue(d["hidden"]["upscale"])    # 但倍数框不该出现
        self.assertFalse(d["hidden"]["prompt"])    # 编辑还要提示词
        self.assertEqual(d["body"]["resolution"], 1024)
        self.assertIsNone(d["body"]["scale"])
        self.assertIn("参考图", d["srcLabel"])

    # --- 反向词：前端只负责**如实发出去** ---------------------------------
    def test_the_negative_prompt_is_sent_verbatim(self):
        """填了就原样进 body。

        它生不生效是**后端**的判断（CFG≤1 时负向分支根本不被求值，见
        `server.py::negative_prompt_state`）。前端要是把它吞掉，那种错在页面上
        没有任何症状 —— 用户填了词、记录里也有，而像素一个没变。
        """
        body = self.probe("negative_filled")["body"]
        self.assertEqual(body["negative_prompt"], "blurry, watermark")

    def test_t2i_sends_an_empty_negative_prompt_by_default(self):
        self.assertEqual(self.probe("t2i")["body"]["negative_prompt"], "")

    def test_upscale_clears_the_negative_prompt(self):
        """放大图里没有文本编码器 —— 反向词必须被清成空串，与 prompt 同理。

        否则记录里会写着「这次用了反向词」，而那张 VOSR2 图根本没经过文本编码器。
        """
        d = self.probe("upscale_clears_negative")
        self.assertEqual(d["body"]["negative_prompt"], "")
        self.assertTrue(d["hidden"]["negative"])   # 字段本身也藏起来

    def test_the_negative_field_shows_wherever_it_can_work(self):
        for name in ("t2i", "edit"):
            with self.subTest(mode=name):
                self.assertFalse(self.probe(name)["hidden"]["negative"])

    # --- 提示词改写器：**额外功能**，不是模式（2026-10-01 重构）-------------
    def test_the_rewrite_control_shows_on_t2i(self):
        self.assertFalse(self.probe("t2i")["hidden"]["rewrite"])

    def test_rewrite_is_sent_only_on_t2i(self):
        """改写器只在文生图那条链路上存在（图生图会把它整段删掉）。

        所以图生图 / 放大下它必须**藏起来**，而且 body 里**必发 false** ——
        否则记录里会写着「这次用了改写器」，而那张图根本没经过它。
        后端也会拒（`qb.build` 当场抛），前端先说真话是为了不让假参数进记录。
        """
        self.assertTrue(self.probe("rewrite_on_t2i")["body"]["rewrite"])
        for name in ("rewrite_on_edit", "rewrite_on_upscale"):
            with self.subTest(case=name):
                d = self.probe(name)
                self.assertFalse(d["body"]["rewrite"])
                self.assertTrue(d["hidden"]["rewrite"])

    # --- 出图后自动放大：另一个额外功能（2026-10-01 新增）------------------
    def test_the_chain_carries_its_own_scale(self):
        """链的倍数走**自己的字段**，不与放大模式那个 `scale` 混用。

        两者含义不同：「这次提交就是一次超分」对「出完图之后再接一段」。
        混成一个字段，「t2i + scale=4 但链关着」就成了一个说不清的状态。
        """
        body = self.probe("chain_on_t2i")["body"]
        self.assertTrue(body["auto_upscale"]["enabled"])
        self.assertEqual(body["auto_upscale"]["scale"], 4)
        self.assertIsNone(body["scale"])          # 放大模式那个键这次不用

    def test_the_chain_is_off_by_default(self):
        body = self.probe("t2i")["body"]
        self.assertFalse(body["auto_upscale"]["enabled"])

    def test_the_chain_cannot_be_chained_on_upscale(self):
        """放大模式自己就是那条链：控件藏起来，body 里也必发 false。"""
        d = self.probe("chain_on_upscale")
        self.assertFalse(d["body"]["auto_upscale"]["enabled"])
        self.assertTrue(d["hidden"]["chain"])

    def test_the_chain_is_available_on_edit(self):
        """图生图也能接（放大是另一条链路，与形状无关）。"""
        d = self.probe("chain_on_edit")
        self.assertTrue(d["body"]["auto_upscale"]["enabled"])
        self.assertFalse(d["hidden"]["chain"])

    # --- 多张参考图：两个键恒等，而第一张就是单数那个 ----------------------
    def test_one_reference_fills_both_keys(self):
        """一张时，`source_images` 是单元素表，`source_image` 是它那一项。

        两个键**必须一起来**：只发复数，老记录/老后端读不到；只发单数，
        多张就丢了。后端也会核这两个键一致（不一致当场 400）。
        """
        for name in ("edit", "upscale"):
            with self.subTest(mode=name):
                body = self.probe(name)["body"]
                self.assertEqual(body["source_images"], ["a.png"])
                self.assertEqual(body["source_image"], "a.png")

    def test_an_empty_source_is_null_and_an_empty_list(self):
        """没选图时是 `null` 与 `[]`，不是 `""`。空串到了后端会被当成
        「给了一个叫空字符串的文件名」—— 而那种错没有症状。"""
        body = self.probe("no_refs")["body"]
        self.assertEqual(body["source_images"], [])
        self.assertIsNone(body["source_image"])

    def test_comma_separated_names_become_several_references(self):
        body = self.probe("multi_refs")["body"]
        self.assertEqual(body["source_images"], ["a.png", "b.png"])
        self.assertEqual(body["source_image"], "a.png")

    def test_the_reference_parser_dedupes_and_keeps_order(self):
        body = self.probe("messy_refs")["body"]
        self.assertEqual(body["source_images"], ["a.png", "b.png"])

    def test_a_filename_with_a_space_is_not_split(self):
        """分隔符**只认逗号**：按空格切会把 `my photo.png` 劈成两张不存在的图，
        而那种错要到提交时才以一句「后端 input/ 里没有 `my`」冒出来。"""
        body = self.probe("spaced_name_refs")["body"]
        self.assertEqual(body["source_images"], ["my photo.png", "b.png"])

    def test_upscale_only_ever_sends_one_reference(self):
        body = self.probe("upscale_truncates_refs")["body"]
        self.assertEqual(body["source_images"], ["a.png"])
        self.assertEqual(body["source_image"], "a.png")

    # --- 上传：**累积**，不是替换（用户这一轮要求改的就是它）---------------
    def test_a_second_upload_keeps_the_first_batch(self):
        """第二次选文件必须**追加**到已有的那批后面。

        旧行为是「设定这一批」（与浏览器原生 file input 一样），于是想凑三张
        素材的人每选一次就被清一次 —— 而页面看不出哪里不对。
        """
        first = self.probe("upload_first_batch")
        self.assertEqual(first["refs"], ["one.png", "two.png"])
        second = self.probe("upload_second_batch")
        self.assertEqual(second["refs"], ["one.png", "two.png", "three.png"],
                         "第一批被第二批顶掉了")
        self.assertEqual(self.probe("upload_accumulated"),
                         ["one.png", "two.png", "three.png"])

    def test_the_upload_message_says_how_many_are_held_now(self):
        """回话里要有「现有几张」—— 累积行为不说出来，用户会以为第一张丢了。"""
        self.assertIn("现有 3 张", self.probe("upload_second_batch")["msg"])

    def test_an_over_limit_batch_is_refused_whole(self):
        """上限按**并集**判：装不下就整批不传，而且说清楚还能放几张。

        按这一批判的话，会先写进去几张再报错 —— 留下一个半截的状态，而那种
        状态在页面上看不出是半截的。
        """
        over = self.probe("upload_over_limit")
        self.assertEqual(over["refs"], ["one.png", "two.png", "three.png"],
                         "超上限的那一批动了已选中的集合")
        self.assertIn("最多 3 张", over["msg"])
        self.assertIn("这一批没上传", over["msg"])
        # 网络那一侧也要证明：那一批**一个字节都没发出去**
        self.assertEqual(self.probe("upload_calls"),
                         ["one.png", "two.png", "three.png"])

    def test_an_empty_pick_changes_nothing(self):
        empty = self.probe("upload_empty")
        self.assertEqual(empty["refs"], ["one.png", "two.png", "three.png"])
        self.assertIn("先选一个文件", empty["msg"])

    # --- LLM 增强那**一颗**入口（P20；形状在 2026-10-01 改过）----------------
    def test_the_enhance_button_explains_why_it_is_disabled(self):
        """禁用**要说出理由**，理由指向去填设置的那一块。

        与放大模式下那两颗「载入」同一条规矩：一个点不动的按钮不解释自己，
        用户只会以为坏了。
        """
        unset = self.probe("enhance_unconfigured")["enhance"]
        self.assertTrue(unset["disabled"])
        self.assertIn("LLM 增强设置", unset["title"])

    def test_the_enhance_button_works_even_with_an_empty_prompt(self):
        """**框空着也叫得动** —— 这是新形状最要紧的一处差别。

        输入已经搬进面板了：空着进去再写正是它的用法。旧形状那颗按钮属于
        提示词框（框空就没东西可增强），所以「空就禁用」在那个形状下是对的 ——
        **这两条不是一回事，别把旧判据照搬过来**。
        """
        ready = self.probe("enhance_ready")["enhance"]
        self.assertFalse(ready["disabled"])
        # 可用时那句提示**不是**「为什么点不动」，而是「点进去能做什么」——
        # 面板里现在有两条道（增强 / 中英翻译），而「翻译」在这颗按钮上没有任何
        # 别的落点：不写在这里，页面上就没有第二处提到它了。
        self.assertNotIn("LLM 增强设置", ready["title"])
        self.assertIn("翻译", ready["title"])
        blank = self.probe("enhance_empty_box")["enhance"]
        self.assertFalse(blank["disabled"],
                         "提示词框空着就把入口禁掉了 —— 输入已经搬进面板了")

    def test_the_prompt_enhance_row_hides_in_upscale(self):
        """放大模式下**没有提示词输入框**，所以那一行入口也得跟着藏。

        那颗按钮在 `<label>` **外面**（在里面点它会被 label 连带激活 textarea），
        所以它不跟着 `prompt-wrap` 一起藏 —— 这件事必须单独说一句，否则就会
        留着一颗亮着的按钮去增强一个不在页面上的框。
        """
        d = self.probe("enhance_on_upscale")
        self.assertTrue(d["hidden"]["promptActions"])
        self.assertFalse(self.probe("enhance_ready")["hidden"]["promptActions"])


class NewChains(FormBehaviour):
    """P21c：H3 与 See-through 是**与 Qwen-Image-2.1 平级**的两条链路。

    页面是两级的（先选链路，再在 Qwen 那一档里选形状），但**线路上只有一个
    `mode` 字段**（五个合法值）。这一层守的就是这两件事：
      · 每一档该露什么、该藏什么（**穷尽的**矩阵，见下）
      · `collectBody()` 发出去的 `mode` 到底是哪一个
    """

    #: ★ 五档 × 全部字段块的**期望表**。
    #:
    #: 只测「H3 藏了模式下拉」是不够的 —— 少一格就是「一个控件留在页面上、看着
    #: 能调、实际不生效」，而那正是本工作区最忌讳的那种错（不报错、没有症状）。
    #: 所以这里把每一格都写出来，Python 逐格比。
    #:
    #: 列：prompt negative library gen cfg advanced rewrite chain src upscale
    #:     promptActions modeSelect steps size h3 layers sweepSteps sizeCustom
    MATRIX = {
        # Qwen 三种形状。`edit` 多一个源图、`upscale` 只有源图与倍数。
        "t2i": dict(prompt=False, negative=False, library=False, gen=False,
                    cfg=False, advanced=False, rewrite=False, chain=False,
                    src=True, upscale=True, promptActions=False,
                    modeSelect=False, steps=False, size=False, h3=True,
                    layers=True, sweepSteps=False, sizeCustom=True),
        "edit": dict(prompt=False, negative=False, library=False, gen=False,
                     cfg=False, advanced=False, rewrite=True, chain=False,
                     src=False, upscale=True, promptActions=False,
                     modeSelect=False, steps=False, size=False, h3=True,
                     layers=True, sweepSteps=False, sizeCustom=True),
        "upscale": dict(prompt=True, negative=True, library=True, gen=True,
                        cfg=True, advanced=True, rewrite=True, chain=True,
                        src=False, upscale=False, promptActions=True,
                        modeSelect=False, steps=True, size=True, h3=True,
                        layers=True, sweepSteps=False, sizeCustom=True),
        # H3：要提示词与首帧、要时长；不要尺寸 / CFG / 采样器 / 高级设置。
        # 步数也藏（它是蒸馏 LoRA 的 4 步固定档，页面上没有这个旋钮）。
        "h3": dict(prompt=False, negative=True, library=True, gen=True,
                   cfg=True, advanced=True, rewrite=True, chain=True,
                   src=False, upscale=True, promptActions=False,
                   modeSelect=True, steps=True, size=True, h3=False,
                   layers=True, sweepSteps=True, sizeCustom=True),
        # 拆层：要源图、分辨率、**步数**；不要提示词、尺寸、CFG、高级设置。
        # `steps` 是这一档最容易写错的一格：它复用出图那一格，而 `gen-row`
        # 那一行里还坐着尺寸 —— 整行一起藏就会把步数也带走（不报错）。
        "layers": dict(prompt=True, negative=True, library=True, gen=False,
                       cfg=True, advanced=True, rewrite=True, chain=True,
                       src=False, upscale=True, promptActions=True,
                       modeSelect=True, steps=False, size=True, h3=True,
                       layers=False, sweepSteps=False, sizeCustom=True),
    }

    def row(self, combo):
        for item in self.probe("matrix"):
            if item["mode"] == combo:
                return item
        raise AssertionError("矩阵里没有 %r" % combo)

    def test_the_whole_visibility_matrix(self):
        """★ 五档 × 18 个字段块，**逐格**断言。"""
        for combo, want in self.MATRIX.items():
            got = self.row(combo)["hidden"]
            for key, expected in want.items():
                with self.subTest(combo=combo, field=key):
                    self.assertEqual(
                        got[key], expected,
                        "%s 下 %s 应当%s，实际%s"
                        % (combo, key, "藏起来" if expected else "露出来",
                           "藏起来了" if got[key] else "露着"))
            # 表里也不许多出没断言的键 —— 将来加了字段块就要一起补进来
            with self.subTest(combo=combo, extra=True):
                self.assertEqual(sorted(got), sorted(want))

    def test_mode_select_only_exists_on_the_qwen_tab(self):
        """另两条链路不在那个下拉框的选项里，所以它必须整格藏掉。"""
        self.assertFalse(self.row("t2i")["hidden"]["modeSelect"])
        self.assertTrue(self.row("h3")["hidden"]["modeSelect"])
        self.assertTrue(self.row("layers")["hidden"]["modeSelect"])

    # --- 「有效模式」：链路盖过下拉框 --------------------------------------
    def test_the_chain_wins_over_the_select(self):
        """★ 这一格是「线路上只有一个 mode」那条规矩的**唯一入口**。

        写错了会让 H3 那一档发出 `t2i`（跑的是 Qwen 的图），而页面看着一切正常 ——
        正是那种没有症状的错。
        """
        rows = {r["chain"] + "/" + r["select"]: r["body_mode"]
                for r in self.probe("effective_mode")}
        self.assertEqual(rows["qwen/t2i"], "t2i")
        self.assertEqual(rows["qwen/upscale"], "upscale")
        # 下拉框停在哪都不算数
        self.assertEqual(rows["h3/t2i"], "h3")
        self.assertEqual(rows["layers/upscale"], "layers")

    def test_set_chain_for_mode_is_the_only_reverse_mapping(self):
        got = {r["mode"]: r["chain"] for r in self.probe("chain_for_mode")}
        # Qwen 那三档都落到 qwen
        for mode in ("t2i", "edit", "upscale"):
            with self.subTest(mode=mode):
                self.assertEqual(got[mode], "qwen")
        self.assertEqual(got["h3"], "h3")
        self.assertEqual(got["layers"], "layers")
        # 老值（`2k` / `refine`）与不认识的值一律当 qwen —— 与 `LEGACY_FORM_MODE`
        # 同一个结论，而这里不抛错：它是个**归一**函数，不是校验器。
        for mode in ("2k", "refine", "nonsense"):
            with self.subTest(mode=mode):
                self.assertEqual(got[mode], "qwen")

    # --- 各档自己的字段 ---------------------------------------------------
    def test_h3_sends_its_duration_and_clears_everything_else(self):
        d = self.probe("h3")
        self.assertEqual(d["chain"], "h3")
        self.assertEqual(d["body"]["mode"], "h3")
        self.assertEqual(d["body"]["seconds"], 5)
        self.assertTrue(d["body"]["prompt"])
        self.assertEqual(d["body"]["source_image"], "a.png")
        # 出图那一套一个都不许带上（后端会忽略，但记录里会留下假参数）
        for key in ("cfg", "sampler", "scheduler", "width", "height", "scale",
                    "resolution", "resolution_depth"):
            with self.subTest(key=key):
                self.assertIsNone(d["body"][key])
        # 步数：H3 的 4 步是冻结在基准图里的，页面上没有这个旋钮 → 发 null
        self.assertIsNone(d["body"]["steps"])

    def test_layers_sends_resolution_depth_and_keeps_steps(self):
        d = self.probe("layers")
        self.assertEqual(d["body"]["mode"], "layers")
        self.assertEqual(d["body"]["resolution"], 1024)
        self.assertEqual(d["body"]["resolution_depth"], -1)
        self.assertEqual(d["body"]["steps"], 20)      # 复用出图那一格
        self.assertEqual(d["body"]["prompt"], "")     # 这条链路没有文本输入
        self.assertIsNone(d["body"]["seconds"])
        self.assertIsNone(d["body"]["cfg"])

    def test_both_new_chains_truncate_refs_to_one(self):
        """那两条图里各只有一个 `LoadImage` —— 多发的那些会被静默丢掉。"""
        self.assertEqual(self.probe("h3_truncates_refs")["body"]["source_images"],
                         ["a.png"])
        self.assertEqual(self.probe("layers_truncates_refs")["body"]["source_images"],
                         ["a.png"])

    def test_turning_seconds_and_depth_actually_reaches_the_body(self):
        self.assertEqual(self.probe("h3_seconds_10")["body"]["seconds"], 10)
        body = self.probe("layers_depth_512")["body"]
        self.assertEqual(body["resolution"], 1280)
        self.assertEqual(body["resolution_depth"], 512)

    def test_the_three_accelerators_are_false_on_both_new_chains(self):
        for name in ("h3_clears_accels", "layers_clears_accels"):
            for key in ("te_speed", "lazy_cache", "turbo"):
                with self.subTest(probe=name, key=key):
                    self.assertFalse(self.probe(name)["body"][key])
            # 串联的放大同理（后端会 400）
            with self.subTest(probe=name, key="auto_upscale"):
                self.assertFalse(self.probe(name)["body"]["auto_upscale"]["enabled"])

    # --- 键集恒定 ---------------------------------------------------------
    def test_params_keyset_is_identical_across_all_five_modes(self):
        """★ 记录 / 画廊项 / 表单回显三处共用同一份形状。

        少一个键，前端就**静默少画一格** —— 不报错、不崩溃，只是某一栏永远空着。
        所以这条要横着比五档，而不是各自断言自己的键。
        """
        bodies = {combo: self.row(combo)["body"] for combo in self.MATRIX}
        first = set(bodies["t2i"])
        for combo, body in bodies.items():
            with self.subTest(combo=combo):
                self.assertEqual(set(body), first,
                                 "%s 的 body 键集与 t2i 不同：多 %s 少 %s"
                                 % (combo, sorted(set(body) - first),
                                    sorted(first - set(body))))

    def test_the_chain_badge_follows_the_chain(self):
        """角标随链路变：写着 QWEN-IMAGE-2.1 而跑出来是 H3 的视频，就是一句假话。"""
        self.assertEqual(self.probe("qwen_tag")["chainTag"], "QWEN-IMAGE-2.1")
        self.assertEqual(self.probe("h3")["chainTag"], "MINIMAX-H3")
        self.assertEqual(self.probe("layers")["chainTag"], "SEE-THROUGH")


class IdHygiene(support.LeakCheckedTestCase):
    """`index.html` 里那些 id 的**唯一性**。

    为什么值得一层：`$("id")` 撞上重复 id 时 `getElementById` 只给你第一个，
    而页面上两个元素长得一模一样 —— 于是「改了没反应」的那一个看起来像产品坏了。
    少一个则 `$()` 返回 null，`applyModeUI()` 一上来就炸。

    这两件事**假 DOM 都发现不了**：`form_probe.js` 的 id 清单是自己写的，
    它只证明「app.js 要的那些 id 在清单里」，不证明「index.html 里真有」。
    """

    CHECKS = os.path.join(HERE, "id_check.js")

    @unittest.skipUnless(shutil.which("node") or shutil.which("node.exe"),
                         "没有 node —— 验不了（不是失败，是没工具）")
    def test_every_id_we_toggle_exists_exactly_once(self):
        node = shutil.which("node") or shutil.which("node.exe")
        r = subprocess.run([node, self.CHECKS], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(r.returncode, 0,
                         "index.html 的 id 有问题：\n%s\n%s" % (r.stdout, r.stderr))
        self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
