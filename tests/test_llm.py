#!/usr/bin/env python3
"""LLM 提示词增强（P20）—— 设置、请求形状、以及**每一种失败的说话方式**。

网络那一层全部用 `httpx.MockTransport` 打桩：**一个真请求都不发**。
真连一家厂商的接口，会把「测试跑绿」建立在网络、额度与对方接口的稳定性上，
而这三样都不属于这个仓库。

判据落在这四件事上：

1. **密钥不回显**（页面拿到的是掩码；空着保存 = 保持原样，不是清空）；
2. **失败说得清是哪一种**（401 / 404 / 429 / 超时 / 连不上 / 不是 JSON / 空内容
   各有各的话 —— 都报「增强失败」等于让用户自己猜）；
3. **密钥不会跟着报错跑出去**（服务端常把收到的密钥回显一段）；
4. **增强这条路一个文件都不写**（写盘的只有保存设置那一个端点）；
5. **「加一个动作」是后端加一行**（`ACTIONS_INFO`）—— 页面里连动作的名字都没有；
6. **「翻译」与「增强」是两件事**：规则分开（忠实 vs 发挥）、温度分开（冷 vs 热），
   而且 `action` 不传时发出去的东西与从前**逐字节相同**。
"""
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import httpx  # noqa: E402
import support  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from workbench import llm, logbuf, server, upload  # noqa: E402

KEY = "sk-test-0123456789abcdef"

# 一张真的、能解开的 16×12 PNG（就是这个红块）。用常量而不是现画，是为了让
# 这个文件**不依赖 Pillow 也能跑** —— 缩图那一层有自己的 `test_vision.py`。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAMCAIAAADkharWAAAAGklEQVR4nGM8ISfHQApgIkk1w6gG"
    "4sBwCFYAccwBHJbhmaEAAAAASUVORK5CYII=")


def ok_body(text="一段增强稿", model="test-model"):
    return {"id": "x", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": text}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34,
                      "total_tokens": 46}}


class TargetsTest(unittest.TestCase):
    """目标表是**唯一真源**（`llm.TARGETS_INFO`）。页面从 `/api/llm` 拿它去建
    「要增强哪一段」那个下拉框 —— 所以「加一个可增强的东西」= 这里加一行，
    不必去页面里加第二颗按钮。这就是用户说的「扩展性」。"""

    def test_every_target_has_a_label_and_a_form_field(self):
        ids = [t["id"] for t in llm.TARGETS_INFO]
        self.assertEqual(sorted(ids), sorted(llm.TARGETS),
                         "TARGETS 与 TARGETS_INFO 分家了")
        labels = set()
        for info in llm.TARGETS_INFO:
            self.assertTrue(info["label"], info)
            self.assertNotIn(info["label"], labels, "两个目标重名了：%s" % info)
            labels.add(info["label"])
            # `field` 是「采用时写回表单里哪一个元素」—— 空着的话采用会静默地
            # 什么都不做，而那看起来只是「点了没反应」
            self.assertTrue(info["field"], info)

    def test_every_target_has_its_own_builtin_rule(self):
        for tid in llm.TARGETS:
            self.assertTrue(llm.SYSTEM_BASE.get(tid), "目标 %s 没有内置规则" % tid)


class ActionsTest(unittest.TestCase):
    """动作表（`llm.ACTIONS_INFO`）—— 与目标表**同一套规矩**：唯一真源在服务端。

    用户要的是「为 LLM 增强加上中英翻译」。加它的方式**不是**在页面里再放一颗
    按钮，而是加一行动作：页面按 `/api/llm` 的 `actions` 建顶栏那个「做什么」
    下拉框，所以「加一个动作」= 后端加一行 + 配一条规则与温度，页面一个字都不用改。
    """

    def test_every_action_has_a_label_and_a_noun(self):
        ids = [a["id"] for a in llm.ACTIONS_INFO]
        self.assertEqual(sorted(ids), sorted(llm.ACTIONS),
                         "ACTIONS 与 ACTIONS_INFO 分家了")
        labels = set()
        for info in llm.ACTIONS_INFO:
            self.assertTrue(info["label"], info)
            # `noun` 是这一支的产物叫什么：右栏标题、「采用<noun>」、空稿占位
            # 都从它来 —— 少了它页面就得按动作 id 分支，那就是把表抄成第二份。
            self.assertTrue(info["noun"], info)
            self.assertNotIn(info["label"], labels, "两个动作重名了：%s" % info)
            labels.add(info["label"])

    def test_every_action_has_a_rule_and_a_temperature(self):
        for aid in llm.ACTIONS:
            self.assertIn(aid, llm.ACTION_TEMPERATURE, "%s 没有温度" % aid)
            for tid in llm.TARGETS:
                self.assertTrue(llm.rule_for(tid, aid),
                                "%s / %s 没有规则" % (aid, tid))

    def test_the_labels_come_from_the_table(self):
        """日志台与页面上的说法都从这张表取 —— 页面里没有第二份。"""
        for info in llm.ACTIONS_INFO:
            self.assertEqual(llm.action_label(info["id"]), info["label"])
        for info in llm.TARGETS_INFO:
            self.assertEqual(llm.target_label(info["id"]), info["label"])

    def test_enhance_is_the_default_and_did_not_change(self):
        """`action=None` 与 `action="enhance"` 必须**逐字节相同**。

        老调用方发出去的就是这一份。翻译是从这里长出去的 —— 长出去的时候顺手
        改了「增强」的行为才是最危险的事：判据全绿，产出变了。
        """
        for tid in llm.TARGETS:
            self.assertEqual(llm.build_messages("x", tid),
                             llm.build_messages("x", tid, "enhance"))
            self.assertEqual(llm.build_messages("x", tid, "enhance", "再短一点"),
                             llm.build_messages("x", tid, None, "再短一点"))

    def test_translation_does_not_depend_on_the_target(self):
        """翻译那两条**不看目标**：一条规则靠「保持原文的形式」同时覆盖正向与
        反向词（原本是逗号分隔的元素表就还是元素表），所以再加一个目标不必再配
        一份翻译规则 —— 这也是那条「加一行」的承诺撑得住的原因。"""
        for aid in (llm.ACTION_TO_EN, llm.ACTION_TO_ZH):
            self.assertEqual(llm.build_messages("猫", "prompt", aid),
                             llm.build_messages("猫", "negative", aid))

    def test_translation_is_told_to_be_faithful(self):
        """翻译那一支的**全部要求**就是「忠实」。

        判据落在**规则文本**上：规则就是产品，改措辞就是在改产出。「忠实」写丢了，
        模型就会顺手替你润色一遍 —— 而用户点的是「译成英文」，他只是想看懂。
        """
        for aid, lang in ((llm.ACTION_TO_EN, "英文"), (llm.ACTION_TO_ZH, "中文")):
            rule = llm.build_messages("猫", "prompt", aid)[0]["content"]
            self.assertIn("忠实", rule)
            self.assertIn("不改写", rule)
            self.assertIn(lang, rule)
            # 增强那一支才允许它发挥（那些词是那一边的）
            self.assertNotIn("更具体", rule)

    def test_translation_runs_colder_than_enhancement(self):
        """翻译是有唯一正确答案的活，温度高就是在让它自由发挥。

        另一条判据（`EnhanceTest`）验这个数**真的进了请求体**。
        """
        self.assertLess(llm.ACTION_TEMPERATURE[llm.ACTION_TO_EN],
                        llm.ACTION_TEMPERATURE[llm.ACTION_ENHANCE])
        self.assertLess(llm.ACTION_TEMPERATURE[llm.ACTION_TO_ZH],
                        llm.ACTION_TEMPERATURE[llm.ACTION_ENHANCE])

    def test_an_unknown_action_is_refused(self):
        for bad in ("翻译", "to_jp", "ENHANCE"):
            with self.subTest(action=bad):
                with self.assertRaises(ValueError):
                    llm.build_messages("x", "prompt", bad)

    def test_an_empty_action_means_the_default_one(self):
        """`""` 与 `None` 一样算「没给」＝增强 —— 与 `target` 的规矩一致。

        别把这条改成报错：页面在动作表还没答上时给的就是空串，那时候弹一个
        「不认识的动作」等于把一次正常的首屏变成一次失败。
        """
        for blank in (None, ""):
            self.assertEqual(llm.build_messages("x", "prompt", blank),
                             llm.build_messages("x", "prompt", llm.ACTION_ENHANCE))


class SettingsTest(support.LeakCheckedTestCase):
    def make(self, data=None):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-llm-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "llm.json")
        if data is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        return llm.LLMSettings(path=path)

    def configured(self):
        settings = self.make()
        settings.save({"base_url": "https://api.example.com/v1",
                       "model": "test-model", "api_key": KEY})
        return settings

    def test_a_blank_key_keeps_the_stored_one(self):
        """**空串 = 保持原样**，不是清空。

        页面拿到的是掩码，所以用户改个模型名再保存时那个密钥框是空的。
        当成「清掉」的话，他下一次增强才失败 —— 而当时他什么都没删，
        也就无从判断自己删掉了什么。
        """
        settings = self.configured()
        settings.save({"model": "另一个模型", "api_key": ""})
        self.assertEqual(settings.raw()["api_key"], KEY)
        self.assertEqual(settings.raw()["model"], "另一个模型")

    def test_an_explicit_null_clears_the_key(self):
        """真要清就传 null —— 空串与 null 是两件事，这是**刻意的**。"""
        settings = self.configured()
        settings.save({"api_key": None})
        self.assertEqual(settings.raw()["api_key"], "")

    def test_a_new_key_replaces_the_old(self):
        settings = self.configured()
        settings.save({"api_key": "sk-another-9999"})
        self.assertEqual(settings.raw()["api_key"], "sk-another-9999")

    def test_the_public_view_never_carries_the_key(self):
        settings = self.configured()
        public = settings.public()
        self.assertNotIn("api_key", public)
        self.assertTrue(public["has_key"])
        # 连**整串**都不许出现在给页面看的那份里
        self.assertNotIn(KEY, json.dumps(public, ensure_ascii=False))
        self.assertIn("…", public["key_masked"])

    def test_the_mask_never_reveals_the_middle(self):
        self.assertEqual(llm.mask_key(KEY), "sk-…cdef")
        self.assertEqual(llm.mask_key("短"), "•")

    def test_it_says_exactly_what_is_missing(self):
        public = self.make().public()
        self.assertFalse(public["configured"])
        self.assertEqual(sorted(public["missing"]), ["api_key", "base_url", "model"])
        # 三项齐全才算配好 —— 页面靠这个决定那两颗按钮亮不亮
        self.assertTrue(self.configured().public()["configured"])

    def test_a_bad_timeout_is_refused(self):
        settings = self.make()
        with self.assertRaises(ValueError):
            settings.save({"timeout_s": "一会儿"})

    def test_the_timeout_is_clamped_to_something_sane(self):
        settings = self.make()
        settings.save({"timeout_s": 1})
        self.assertGreaterEqual(settings.raw()["timeout_s"], 5.0)
        settings.save({"timeout_s": 10 ** 6})
        self.assertLessEqual(settings.raw()["timeout_s"], 600.0)


class EndpointUrlTest(unittest.TestCase):
    """很多人是从文档里**整条 URL** 复制的，再拼一次会得到一个 404 ——
    而那个 404 看起来像「模型名不对」，把人指到错的方向。"""

    def test_it_appends_the_path(self):
        self.assertEqual(llm.endpoint_url("https://api.example.com/v1"),
                         "https://api.example.com/v1/chat/completions")

    def test_a_trailing_slash_does_not_double_up(self):
        self.assertEqual(llm.endpoint_url("https://api.example.com/v1/"),
                         "https://api.example.com/v1/chat/completions")

    def test_an_already_complete_url_is_left_alone(self):
        full = "https://api.example.com/v1/chat/completions"
        self.assertEqual(llm.endpoint_url(full), full)

    def test_an_empty_base_url_stays_empty(self):
        self.assertEqual(llm.endpoint_url(""), "")
        self.assertEqual(llm.endpoint_url(None), "")


class MessagesTest(unittest.TestCase):
    """发出去的是什么 —— 抽出来就是为了能这么验。"""

    def test_each_target_gets_its_own_rule(self):
        """**反向词不是正向提示词的一种**。

        正向那套「八步结构」用在反向词上会得到一段散文，而反向词要的是逗号
        分隔的元素表 —— 同一个模型、同一个接口，差的就是这句规则。
        """
        prompt = llm.build_messages("a cat", "prompt")[0]["content"]
        negative = llm.build_messages("blurry", "negative")[0]["content"]
        self.assertNotEqual(prompt, negative)
        self.assertIn("逗号分隔", negative)
        self.assertIn("不希望出现", negative)
        self.assertIn("提示词", prompt)
        self.assertNotIn("逗号分隔", prompt)

    def test_the_extra_instruction_is_appended_not_substituted(self):
        """用户那句是**追加**在内置规则后面。

        替换的话，「逗号分隔的元素表」这条形状约束会一起没了 —— 而它正是
        反向词好不好用的关键。
        """
        msgs = llm.build_messages("x", "negative", system="再短一点")
        self.assertIn("逗号分隔", msgs[0]["content"])
        self.assertIn("再短一点", msgs[0]["content"])
        self.assertEqual(msgs[1], {"role": "user", "content": "x"})

    def test_an_unknown_target_is_refused(self):
        with self.assertRaises(ValueError):
            llm.build_messages("x", "别的")


def shot(name="a.png", mime="image/jpeg", b64="QUJD"):
    """一张**已经准备好**的参考图（`vision.prepare()` 的产出形状）。"""
    return {"name": name, "mime": mime, "b64": b64, "bytes": 3,
            "width": 8, "height": 8, "src_width": 8, "src_height": 8,
            "src_bytes": 9}


class ImageMessagesTest(unittest.TestCase):
    """带参考图时发出去的是什么（P20e）。

    两条都不能松：**有图**时要变成 OpenAI 的 content 数组（实测这条端点收得下，
    见 `logs/llm_vision_probe.log`：同一张图答对了背景、形状、颜色、方位），
    **没图**时一个字都不许变（老路径的逐字节判据钉着）。
    """

    def test_without_images_the_user_content_is_still_a_plain_string(self):
        msgs = llm.build_messages("a cat", "prompt")
        self.assertEqual(msgs[1], {"role": "user", "content": "a cat"})
        self.assertNotIn("随附", msgs[0]["content"])

    def test_images_turn_the_user_content_into_a_content_array(self):
        msgs = llm.build_messages("a cat", "prompt", images=[shot()])
        self.assertIsInstance(msgs[1]["content"], list)
        self.assertEqual(msgs[1]["content"][0],
                         {"type": "text", "text": "a cat"})
        self.assertEqual(msgs[1]["content"][1],
                         {"type": "image_url",
                          "image_url": {"url": "data:image/jpeg;base64,QUJD"}})

    def test_the_order_of_the_images_is_the_order_that_was_given(self):
        """顺序就是意义 —— 「第一张决定输出尺寸」，错位了模型描述的就是别的图。"""
        msgs = llm.build_messages("a cat", "prompt",
                                  images=[shot("1.png", b64="QQ=="),
                                          shot("2.png", b64="Qg=="),
                                          shot("3.png", b64="Qw==")])
        urls = [p["image_url"]["url"] for p in msgs[1]["content"][1:]]
        self.assertEqual(urls, ["data:image/jpeg;base64,QQ==",
                                "data:image/jpeg;base64,Qg==",
                                "data:image/jpeg;base64,Qw=="])

    def test_the_rule_tells_the_model_how_many_came_and_not_to_use_ordinals(self):
        """「不要写『第一张』」不是措辞偏好：本链路的提示词里**没有序号**这回事
        （指令要按内容描述，见 AGENTS.md 的多参考图那条）。"""
        msgs = llm.build_messages("a cat", "prompt", images=[shot(), shot()])
        rule = msgs[0]["content"]
        self.assertIn("随附 2 张参考图", rule)
        self.assertIn("不要写", rule)
        self.assertIn("序号", rule)
        # 老规则一条不少（是追加，不是替换）
        self.assertIn("直接输出提示词本身", rule)

    def test_the_system_message_stays_a_string(self):
        msgs = llm.build_messages("a cat", "prompt", images=[shot()])
        self.assertIsInstance(msgs[0]["content"], str)


class EnhanceTest(support.LeakCheckedTestCase):
    """一次调用：成功与**每一种失败**。全程 `MockTransport`，不发真请求。"""

    def settings(self, **over):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-llm-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = llm.LLMSettings(path=os.path.join(tmp, "llm.json"))
        body = {"base_url": "https://api.example.com/v1", "model": "test-model",
                "api_key": KEY, "timeout_s": 30}
        body.update(over)
        s.save(body)
        return s

    def run_enhance(self, handler, settings=None, text="a cat", target="prompt",
                    action=None, images=None):
        async def go():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       trust_env=False)
            try:
                return await llm.enhance(text, target=target, action=action,
                                         settings=settings or self.settings(),
                                         client=client, images=images)
            finally:
                await client.aclose()
        return asyncio.run(go())

    def kind_of(self, handler, **kw):
        with self.assertRaises(llm.LLMError) as cm:
            self.run_enhance(handler, **kw)
        return cm.exception

    def test_a_good_answer_comes_back_with_the_model_and_usage(self):
        def handler(request):
            return httpx.Response(200, json=ok_body("一段增强稿", "real-model"))

        out = self.run_enhance(handler)
        self.assertEqual(out["text"], "一段增强稿")
        self.assertEqual(out["model"], "real-model")
        self.assertEqual(out["usage"]["total_tokens"], 46)
        self.assertEqual(out["target"], "prompt")
        self.assertEqual(out["action"], "enhance")
        self.assertIsInstance(out["elapsed_ms"], int)

    def test_a_translation_goes_out_cold_and_comes_back_tagged(self):
        """翻译那一支：**发出去的是那条忠实规则、温度是冷的**，回执里带着 action。

        温度不是随手填的数：忠实翻译在一个有唯一正确答案的活上把温度调高，
        就是在让它自由发挥 —— 而「别发挥」正是这一支的全部要求。
        """
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ok_body("A cat"))

        out = self.run_enhance(handler, action=llm.ACTION_TO_EN)
        self.assertIn("忠实", seen["body"]["messages"][0]["content"])
        self.assertEqual(seen["body"]["temperature"],
                         llm.ACTION_TEMPERATURE[llm.ACTION_TO_EN])
        self.assertEqual(out["action"], llm.ACTION_TO_EN)
        self.assertEqual(out["target"], "prompt")
        self.assertEqual(out["text"], "A cat")

    def test_the_images_ride_along_on_the_wire(self):
        """参考图真的进了请求体，而且回执里带着**发出去几张**。

        回执那个数不是装饰：页面拿它写「· 2 张图」，而服务端那一步失败时
        页面自己数出来的数会是错的（它会以为带上了）。
        """
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ok_body("x"))

        out = self.run_enhance(handler, images=[shot("ref.png")])
        content = seen["body"]["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "a cat"})
        self.assertEqual(content[1]["image_url"]["url"],
                         "data:image/jpeg;base64,QUJD")
        self.assertIn("随附 1 张参考图", seen["body"]["messages"][0]["content"])
        self.assertEqual(out["images"], 1)

    def test_the_receipt_counts_zero_when_nothing_went(self):
        def handler(request):
            return httpx.Response(200, json=ok_body("x"))

        out = self.run_enhance(handler)
        self.assertEqual(out["images"], 0)

    def test_too_many_images_never_reach_the_network(self):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, json=ok_body("x"))

        err = self.kind_of(handler,
                           images=[shot() for _ in range(llm.MAX_IMAGES + 1)])
        self.assertEqual(err.kind, "input")
        self.assertEqual(called, [])

    def test_a_half_prepared_image_never_reaches_the_network(self):
        """拼 data URI 时才会炸的那种残缺，在这里挡掉 —— 不挡的话它以一个
        `KeyError` 的形式出来，而那个堆栈长得像「接口返回的形状不对」。"""
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, json=ok_body("x"))

        err = self.kind_of(handler, images=[{"name": "a.png", "mime": "image/jpeg"}])
        self.assertEqual(err.kind, "input")
        self.assertEqual(called, [])

    def test_an_unknown_action_never_reaches_the_network(self):
        """不认识的动作在本地停住。漏过去的话 `rule_for()` 会 KeyError ——
        用户收到一个 500，而它看起来像「服务端坏了」。"""
        called = []
        with self.assertRaises(llm.LLMError) as cm:
            self.run_enhance(lambda r: called.append(r) or httpx.Response(200),
                             action="翻译")
        self.assertEqual(cm.exception.kind, "input")
        self.assertEqual(called, [])

    def test_it_sends_the_key_as_a_bearer_header_and_the_model_in_the_body(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ok_body())

        self.run_enhance(handler)
        self.assertEqual(seen["auth"], "Bearer %s" % KEY)
        self.assertEqual(seen["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(seen["body"]["model"], "test-model")
        self.assertFalse(seen["body"]["stream"])

    def test_a_bad_key_says_so(self):
        def handler(request):
            return httpx.Response(401, json={"error": {"message": "Invalid key"}})

        exc = self.kind_of(handler)
        self.assertEqual(exc.kind, "auth")
        self.assertIn("密钥", str(exc))

    def test_a_wrong_model_name_says_so_and_mentions_v1(self):
        def handler(request):
            return httpx.Response(404, text="model not found")

        exc = self.kind_of(handler)
        self.assertEqual(exc.kind, "model")
        self.assertIn("/v1", str(exc))

    def test_every_failure_kind_is_named(self):
        """每种失败各有一句**能照着修**的话。都报「增强失败」等于没说。

        「返回体里没有 choices」与「正文不是 JSON」都归 `shape` —— 它们要做的事
        是同一件（去看这个端点到底回了什么），分成两个 kind 只是多一个要记的名字。
        """
        cases = {
            "rate": lambda r: httpx.Response(429, text="slow down"),
            "api": lambda r: httpx.Response(500, text="内部错误"),
            "shape": lambda r: httpx.Response(200, text="<html>不是 JSON</html>"),
            "empty": lambda r: httpx.Response(200, json=ok_body("   ")),
            "shape_no_choices": lambda r: httpx.Response(200, json={"id": "x"}),
        }
        for want, make in cases.items():
            with self.subTest(kind=want):
                self.assertEqual(self.kind_of(make).kind, want.split("_")[0])

    def test_a_timeout_is_told_apart_from_being_unreachable(self):
        def timeout(request):
            raise httpx.ReadTimeout("太慢了", request=request)

        def refused(request):
            raise httpx.ConnectError("拒绝连接", request=request)

        self.assertEqual(self.kind_of(timeout).kind, "timeout")
        self.assertEqual(self.kind_of(refused).kind, "network")

    def test_a_server_error_that_echoes_the_key_is_scrubbed(self):
        """服务端常把收到的密钥回显一段。这条消息会进页面、还会进日志台 ——
        不抹掉就等于把密钥抄到了两个更松的地方，而且**没有症状**。"""
        def handler(request):
            return httpx.Response(401, text="Invalid API key: %s (see docs)" % KEY)

        exc = self.kind_of(handler)
        self.assertNotIn(KEY, str(exc))
        self.assertNotIn(KEY, exc.detail or "")
        self.assertNotIn(KEY[-8:], str(exc))
        self.assertIn("…", str(exc))

    def test_an_unconfigured_settings_never_reaches_the_network(self):
        """没配好就在本地停住 —— 报「缺 base_url / 模型 / 密钥」，而不是发一个
        必然失败的请求出去（那既慢又看不着原因）。"""
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, json=ok_body())

        settings = self.settings(base_url="", model="", api_key="")
        with self.assertRaises(llm.LLMError) as cm:
            self.run_enhance(handler, settings=settings)
        self.assertEqual(cm.exception.kind, "config")
        self.assertEqual(called, [])

    def test_an_empty_text_never_reaches_the_network(self):
        called = []
        with self.assertRaises(llm.LLMError) as cm:
            self.run_enhance(lambda r: called.append(r) or httpx.Response(200),
                             text="   ")
        self.assertEqual(cm.exception.kind, "input")
        self.assertEqual(called, [])

    def test_enhance_itself_does_not_touch_the_settings_file(self):
        """增强这条路**一个文件都不写**。写盘的只有保存设置那一个端点。"""
        settings = self.settings()
        with open(settings.path, "rb") as f:
            before = f.read()
        self.run_enhance(lambda r: httpx.Response(200, json=ok_body()),
                         settings=settings)
        with open(settings.path, "rb") as f:
            self.assertEqual(f.read(), before)


class LlmEndpointTest(support.LeakCheckedTestCase):
    """两个 HTTP 端点。`length` 这类东西都在设置那一层，这里只验边界。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-llm-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wb = server.Workbench(state_dir=self.tmp, client=support.FakeComfy())
        self.tc = TestClient(server.create_app(self.wb))
        self.buf = logbuf.LogBuffer()
        patcher = mock.patch.object(logbuf, "BUFFER", self.buf)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_endpoint_serves_the_target_table(self):
        """页面建那两个下拉框靠的就是这一份 —— 它必须是**后端那张表**，不是页面抄的。"""
        data = self.tc.get("/api/llm").json()
        self.assertEqual([t["id"] for t in data["targets"]],
                         [t["id"] for t in llm.TARGETS_INFO])
        for info in data["targets"]:
            self.assertEqual(sorted(info), ["field", "id", "label"])

    def test_the_endpoint_serves_the_action_table_too(self):
        """「做什么」那个下拉框同理。**页面里连「译成英文」这几个字都没有**
        （有源码判据钉着），所以这张表是它唯一的来源。"""
        data = self.tc.get("/api/llm").json()
        self.assertEqual([a["id"] for a in data["actions"]],
                         [a["id"] for a in llm.ACTIONS_INFO])
        for info in data["actions"]:
            # `noun` 也要给：右栏标题与「采用<noun>」都从它拼
            self.assertEqual(sorted(info), ["id", "label", "noun"])

    def test_the_settings_round_trip_without_echoing_the_key(self):
        body = {"base_url": "https://api.example.com/v1", "model": "m",
                "api_key": KEY, "timeout_s": 45}
        saved = self.tc.post("/api/llm", json=body).json()["settings"]
        self.assertTrue(saved["has_key"])
        self.assertNotIn(KEY, json.dumps(saved, ensure_ascii=False))
        # 再存一次，**不带密钥**（页面就是这样：那一栏是空的）
        again = self.tc.post("/api/llm", json={"model": "m2"}).json()["settings"]
        self.assertTrue(again["has_key"], "空着的密钥栏把密钥清掉了")
        self.assertEqual(again["model"], "m2")

    def test_the_raw_key_is_never_in_the_settings_file_response(self):
        self.tc.post("/api/llm", json={"base_url": "https://x/v1", "model": "m",
                                       "api_key": KEY})
        text = self.tc.get("/api/llm").text
        self.assertNotIn(KEY, text)
        self.assertIn("key_masked", text)

    def test_a_bad_body_is_a_400_not_a_500(self):
        self.assertEqual(self.tc.post("/api/llm", json={"timeout_s": "x"}).status_code, 400)
        self.assertEqual(self.tc.post("/api/llm", json=[1, 2]).status_code, 400)
        self.assertEqual(self.tc.post("/api/llm/enhance", json={"text": "  "}).status_code,
                         400)
        self.assertEqual(self.tc.post("/api/llm/enhance",
                                      json={"text": "x", "target": "别的"}).status_code,
                         400)
        # 不认识的动作必须是 400：漏过去会 KeyError，而 500 看起来像服务端坏了
        self.assertEqual(self.tc.post("/api/llm/enhance",
                                      json={"text": "x", "action": "翻译"}).status_code,
                         400)

    def test_the_action_is_passed_through_and_logged_as_one(self):
        """日志台上要分得清「增强」与「译成英文」。

        都写「LLM 增强」的话，翻了一半的那条提醒不了任何人 —— 而这个功能存在的
        意义就是让人看得出这一段文本经过了什么。
        """
        seen = {}

        async def fake(text, target=None, action=None, settings=None, client=None,
                       images=None):
            seen["action"] = action
            return {"text": "A cat", "model": "m", "elapsed_ms": 12,
                    "target": target, "action": action,
                    "images": len(images or []), "usage": None}

        with mock.patch.object(llm, "enhance", side_effect=fake):
            r = self.tc.post("/api/llm/enhance",
                             json={"text": "一只猫", "action": "to_en"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen["action"], "to_en")
        notes = [x["text"] for x in self.buf.rows() if x["source"] == "llm"]
        self.assertEqual(len(notes), 1)
        self.assertIn("译成英文", notes[0])
        self.assertIn("提示词", notes[0])

    def test_the_default_action_is_still_enhance(self):
        """不传 `action` 的老调用方走的还是增强 —— 加上翻译不该改动老行为。"""
        seen = {}

        async def fake(text, target=None, action=None, settings=None, client=None,
                       images=None):
            seen["action"] = action
            return {"text": "x", "model": "m", "elapsed_ms": 1,
                    "target": target, "action": action,
                    "images": len(images or []), "usage": None}

        with mock.patch.object(llm, "enhance", side_effect=fake):
            self.tc.post("/api/llm/enhance", json={"text": "x"})
        self.assertEqual(seen["action"], llm.ACTION_ENHANCE)

    def test_a_failed_enhance_lands_in_the_timeline_as_an_error(self):
        """增强失败**不在任何作业记录里**，所以它唯一能被看见的地方就是时间线。"""
        with mock.patch.object(llm, "enhance",
                               side_effect=llm.LLMError("密钥不对", kind="auth")):
            r = self.tc.post("/api/llm/enhance", json={"text": "a cat"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["kind"], "auth")
        rows = [x for x in self.buf.rows() if x["level"] == "error"]
        self.assertEqual(len(rows), 1)
        self.assertIn("增强", rows[0]["text"])
        self.assertIn("提示词", rows[0]["text"])
        self.assertIn("密钥不对", rows[0]["text"])

    def test_the_action_of_a_failure_is_named_too(self):
        """失败的**那一句话**同样要说是哪一道 —— 否则「LLM 增强失败」在只做了
        翻译的这一轮里会把人的注意力引到错的地方。"""
        with mock.patch.object(llm, "enhance",
                               side_effect=llm.LLMError("超时", kind="timeout")):
            self.tc.post("/api/llm/enhance",
                         json={"text": "cat", "action": "to_zh"})
        rows = [x for x in self.buf.rows() if x["level"] == "error"]
        self.assertIn("译成中文", rows[0]["text"])

    def test_the_two_targets_are_logged_apart(self):
        async def fake(text, target=None, action=None, settings=None, client=None,
                       images=None):
            return {"text": text + "!", "model": "m", "elapsed_ms": 12,
                    "target": target, "action": action,
                    "images": len(images or []), "usage": None}

        with mock.patch.object(llm, "enhance", side_effect=fake):
            self.tc.post("/api/llm/enhance", json={"text": "a cat"})
            self.tc.post("/api/llm/enhance",
                         json={"text": "blurry", "target": "negative"})
        notes = [r["text"] for r in self.buf.rows() if r["source"] == "llm"]
        self.assertIn("提示词", notes[0])
        self.assertIn("反向词", notes[1])

    # --- 参考图（P20e）----------------------------------------------------

    def ref_client(self, names=("a.png",)):
        """`upload.read_reference` 的替身 —— 读盘那件事本身在 `test_upload.py`
        里整支验过（对账、唯一命中、双胞胎、找不到、太大），这里只验接线。"""
        return mock.patch.object(upload, "read_reference",
                                 side_effect=lambda name, client=None: {
                                     "name": name, "data": TINY_PNG, "path": "x",
                                     "root": "d", "verified": True, "how": "对账过"})

    def recording_enhance(self, seen):
        async def fake(text, target=None, action=None, settings=None, client=None,
                       images=None):
            seen["images"] = images
            return {"text": "稿子", "model": "m", "elapsed_ms": 5, "target": target,
                    "action": action, "images": len(images or []), "usage": None}
        return fake

    def test_refs_are_read_prepared_and_sent(self):
        """三件事连起来：读字节 → 缩成给模型看的那一份 → 进请求体。"""
        seen = {}
        with self.ref_client(), \
                mock.patch.object(llm, "enhance",
                                  side_effect=self.recording_enhance(seen)):
            r = self.tc.post("/api/llm/enhance",
                             json={"text": "一只猫", "refs": ["a.png"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["images"], 1)
        self.assertEqual(len(seen["images"]), 1)
        self.assertEqual(seen["images"][0]["name"], "a.png")
        self.assertEqual(seen["images"][0]["mime"], "image/jpeg")
        self.assertTrue(seen["images"][0]["b64"])
        notes = [x["text"] for x in self.buf.rows() if x["source"] == "llm"]
        self.assertIn("随附 1 张参考图", notes[0])

    def test_an_unreadable_ref_is_a_400_and_the_model_is_never_asked(self):
        """**这是这个功能最要紧的一条判据。**

        图取不到就整次不做。退化成纯文本的话，模型会照着**看不见的图**编一张
        出来（实测两次，见 `logs/llm_vision_probe.log`），而那段编出来的描述会
        被「采用」写进提示词 —— 出图能出、耗时正常、日志干净，没有症状。
        """
        called = []

        async def fake(text, target=None, action=None, settings=None, client=None,
                       images=None):
            called.append(text)
            return {"text": "稿子", "model": "m", "elapsed_ms": 5, "target": target,
                    "action": action, "images": len(images or []), "usage": None}

        with mock.patch.object(upload, "read_reference",
                               side_effect=upload.UploadError("找不到『a.png』")), \
                mock.patch.object(llm, "enhance", side_effect=fake):
            r = self.tc.post("/api/llm/enhance",
                             json={"text": "一只猫", "refs": ["a.png"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["kind"], "image")
        self.assertIn("a.png", r.json()["error"])
        self.assertEqual(called, [], "图取不到，却还是去问了模型")
        rows = [x for x in self.buf.rows() if x["level"] == "error"]
        self.assertEqual(len(rows), 1)
        self.assertIn("参考图", rows[0]["text"])

    def test_refs_must_be_a_list_of_names(self):
        for bad in ("a.png", [1], {"a": 1}, 3):
            r = self.tc.post("/api/llm/enhance", json={"text": "x", "refs": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.json()["kind"], "input")

    def test_too_many_refs_is_a_400(self):
        r = self.tc.post("/api/llm/enhance",
                         json={"text": "x",
                               "refs": ["a.png"] * (llm.MAX_IMAGES + 1)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["kind"], "input")

    def test_without_refs_the_call_carries_no_images(self):
        """老调用方（不带 `refs`）走的还是纯文本那条路。"""
        seen = {}
        with mock.patch.object(llm, "enhance",
                               side_effect=self.recording_enhance(seen)):
            self.tc.post("/api/llm/enhance", json={"text": "a cat"})
        self.assertEqual(seen["images"], [])

    def test_the_endpoint_does_not_write_any_file(self):
        """**这条路不写盘。** 把 state 目录整棵树的前后快照比一遍 ——
        只看「有没有多出 llm.json」会漏掉别的文件。"""
        async def fake(text, target=None, action=None, settings=None, client=None,
                       images=None):
            return {"text": "增强稿", "model": "m", "elapsed_ms": 5,
                    "target": target or "prompt", "action": action,
                    "images": len(images or []), "usage": None}

        def tree():
            out = {}
            for base, _dirs, files in os.walk(self.tmp):
                for name in files:
                    path = os.path.join(base, name)
                    out[os.path.relpath(path, self.tmp)] = os.path.getsize(path)
            return out

        before = tree()
        with mock.patch.object(llm, "enhance", side_effect=fake):
            r = self.tc.post("/api/llm/enhance", json={"text": "a cat"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "增强稿")
        self.assertEqual(tree(), before, "增强写了盘")


if __name__ == "__main__":
    unittest.main(verbosity=2)
