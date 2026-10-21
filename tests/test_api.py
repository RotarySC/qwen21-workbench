#!/usr/bin/env python3
"""HTTP 层 —— `TestClient` 打真实路由，ComfyUI 换成内存替身。

特意**不进** `with TestClient(app)`：那样会跑 lifespan、起 Watcher 线程去连
真实 8188。这里要测的是路由与校验，不是采集线程（那是 `test_watcher.py` 的事）。

判据 6（后端没起时给出可复制的命令）和判据 1 的提交路径都在这里守。
"""
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import layers_build as lb  # noqa: E402  (LaMa 检查点那两段常量只在它那里有一份)
import qwen21_build as qb  # noqa: E402  (参考图上限那类常量只在构建器里有一份)
from workbench import (config, cost, gallery, server, thumbs, timings, trash,  # noqa: E402
                       upload)
from workbench.jobs import JobStore  # noqa: E402

PROMPT = "Photorealistic studio photograph of a single green apple."


def make_png_blob(workflow=None, extra=None):
    """造一张带 tEXt 的最小 PNG —— 路由测试要的是「磁盘上有张能读的图」。"""
    chunks = []
    if workflow is not None:
        chunks.append((b"tEXt", b"prompt\x00" +
                       json.dumps(workflow).encode("utf-8")))
    for key, value in (extra or {}).items():
        chunks.append((b"tEXt", key.encode("latin-1") + b"\x00" +
                       value.encode("latin-1")))
    out = [b"\x89PNG\r\n\x1a\n"]
    for ctype, body in chunks:
        out.append(struct.pack(">I", len(body)) + ctype + body)
        out.append(struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))
    out.append(struct.pack(">I", 0) + b"IEND" + struct.pack(">I", 0))
    return b"".join(out)


def real_png_blob(workflow=None, size=(1000, 800), seed=7):
    """一张**真能解码**的 PNG（带**噪声**像素，所以 PNG 压不小）。

    与 `make_png_blob` 分开是刻意的：那一张只有 tEXt + IEND，**没有 IDAT** ——
    路由测试要的只是「磁盘上有张能读的图」，而缩略图那条路要的是真像素。
    拿纯色图当夹具还会得出反的结论（纯色 PNG 比同尺寸 JPEG 还小，
    「缩了之后更小」这条断言会假红）。

    纯标准库（zlib + struct）造，所以 `test_api` 不必装 Pillow。
    """
    rnd = random.Random(seed)
    w, h = size
    rows = b"".join(b"\x00" + rnd.randbytes(w * 3) for _ in range(h))

    def chunk(ctype, body):
        return (struct.pack(">I", len(body)) + ctype + body +
                struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    out = [b"\x89PNG\r\n\x1a\n",
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))]
    if workflow is not None:
        out.append(chunk(b"tEXt", b"prompt\x00" +
                         json.dumps(workflow).encode("utf-8")))
    out.append(chunk(b"IDAT", zlib.compress(rows)))
    out.append(chunk(b"IEND", b""))
    return b"".join(out)


def qwen_graph(steps=20, seed=12345, width=1024, height=1024, switch=False,
               prompt="a green apple"):
    """一份最小但**可被 pngmeta 认出是本链路**的 API 图。"""
    return {
        "459:474": {"class_type": "ComfySwitchNode",
                    "inputs": {"switch": switch, "on_false": prompt}},
        "459:452": {"class_type": "TextEncodeQwenImage21",
                    "inputs": {"prompt": prompt, "negative_prompt": ""}},
        "459:458": {"class_type": "KSampler",
                    "inputs": {"steps": steps, "seed": seed, "cfg": 1.0,
                               "sampler_name": "euler", "scheduler": "simple"}},
        "459:456": {"class_type": "EmptyLatentImage",
                    "inputs": {"width": width, "height": height}},
    }


class ApiBase(support.LeakCheckedTestCase):
    def make(self, running=True, models=None, submit_error=None,
             input_dir=None, input_options=None, argv=None, node_classes=(),
             combos=None):
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-api-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        client = support.FakeComfy(running=running, models=models,
                                   submit_error=submit_error,
                                   input_dir=input_dir, input_options=input_options,
                                   argv=argv, node_classes=node_classes,
                                   combos=combos)
        wb = server.Workbench(state_dir=self.tmp, client=client)
        app = server.create_app(wb)
        self.tc = TestClient(app)
        self.wb = wb
        self.client = client
        return self.tc

    def build_body(self, **over):
        body = {"mode": "t2i", "prompt": PROMPT, "steps": 20,
                "width": 1024, "height": 1024, "seed": 12345, "cfg": 1.0}
        body.update(over)
        return body


class AutoUpscale(ApiBase):
    """出图后自动放大（串联）。2026-10-01 起：**额外功能，不是模式**。

    链本身（搬产物 + 提交第二个作业）在 `test_chain.py` 里针对性验；
    这里守的是请求契约与提交侧的两件事：预检要算它、子作业要记父作业。
    """

    def test_upscale_mode_cannot_carry_the_chain(self):
        """放大不能再挂放大（VOSR2 是 one-step，叠一次等于换个更大的倍数）。"""
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body(
            mode="upscale", source_image="ref.png",
            auto_upscale={"enabled": True, "scale": 2}))
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能再挂", r.json()["error"])

    def test_bad_scale_is_rejected(self):
        tc = self.make()
        for bad in (0, 17, "2", 2.5, True):
            with self.subTest(scale=bad):
                r = tc.post("/api/build", json=self.build_body(
                    auto_upscale={"enabled": True, "scale": bad}))
                self.assertEqual(r.status_code, 400)

    def test_the_estimate_covers_both_steps_of_the_chain(self):
        """点一次「确认」覆盖的是**整条链** —— 所以预检必须把两段都算进去。"""
        tc = self.make()
        plain = tc.post("/api/build", json=self.build_body()).json()
        data = tc.post("/api/build", json=self.build_body(
            auto_upscale={"enabled": True, "scale": 4})).json()
        parts = data["estimate"]["parts"]
        self.assertGreater(parts["chain_upscale_s"], 0)
        self.assertEqual(parts["chain_output"], [4096, 4096])
        self.assertEqual(parts["chain_scale"], 4)
        self.assertGreater(data["estimate"]["seconds"],
                           plain["estimate"]["seconds"])
        self.assertIsNone(plain["estimate"]["parts"].get("chain_upscale_s"))

    def test_a_huge_chained_output_needs_confirmation(self):
        """×8（1024²→8192²）那道门槛走的是**放大自己**的判据（8192 起）。"""
        tc = self.make()
        data = tc.post("/api/build", json=self.build_body(
            auto_upscale={"enabled": True, "scale": 8})).json()
        self.assertTrue(data["estimate"]["confirm"]["required"])
        self.assertIn("4 倍于实测过的最大档",
                      data["estimate"]["confirm"]["text"])

    def test_the_child_job_records_its_parent(self):
        """子作业带 `parent_job_id`，页面才说得出「这张是谁放大出来的」。"""
        tc = self.make()
        out = asyncio.run(server._submit_one(
            self.wb, {"mode": "upscale", "source_image": "ref.png", "scale": 2,
                      "confirm": True}, parent_job_id="parent-1"))
        self.assertTrue(out["ok"], out)
        rec = self.wb.store.get(out["job_id"])
        self.assertEqual(rec["parent_job_id"], "parent-1")
        # 普通提交没有父作业 —— 键在、值是 None（记录形状一致）
        plain = asyncio.run(server._submit_one(
            self.wb, self.build_body()))
        self.assertIsNone(self.wb.store.get(plain["job_id"])["parent_job_id"])


class Health(ApiBase):
    def test_health(self):
        tc = self.make()
        r = tc.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


class BackendDetection(ApiBase):
    """判据 6。"""

    def test_down_says_so_and_hands_over_a_command(self):
        tc = self.make(running=False)
        data = tc.get("/api/backend").json()
        self.assertFalse(data["running"])
        self.assertFalse(data["models_ok"])
        self.assertIn("后端未运行", data["hint"])
        cmd = data["start_command"]["display"]
        self.assertIn("--extra-model-paths-config", cmd)
        self.assertIn("--enable-manager", cmd)
        self.assertIn("--port 8188", cmd)
        self.assertIn("main.py", cmd)
        # 四个模型都得出现在体检表里，只是状态为「未知」
        self.assertEqual(len(data["models"]), 4)
        self.assertTrue(all(v["present"] is None for v in data["models"].values()))

    def test_up_and_complete(self):
        tc = self.make()
        data = tc.get("/api/backend").json()
        self.assertTrue(data["running"])
        self.assertTrue(data["models_ok"])
        self.assertEqual(data["comfyui_version"], "0.37.4")
        self.assertEqual(data["missing_models"], [])
        self.assertIsNotNone(data["device"])

    def test_up_but_missing_a_model_is_called_out(self):
        models = support.full_models()
        models["vae"]["present"] = False
        tc = self.make(models=models)
        data = tc.get("/api/backend").json()
        self.assertTrue(data["running"])
        self.assertFalse(data["models_ok"])
        self.assertEqual(data["missing_models"], ["vae"])
        self.assertIn("vae", data["hint"])
        self.assertIn("白跑", data["hint"])

    def test_carries_cost_model(self):
        data = self.make().get("/api/backend").json()
        self.assertEqual(data["cost_model"]["sec_per_step_1mp"], 2.5)
        # 断言**有哪几个锚点**而不是数数 —— 加一条实测锚点不该把测试弄红
        cases = [a["case"] for a in data["cost_model"]["anchors"]]
        for want in ("1024² / 20 步", "1024² / 40 步", "1024² / 25 步 + 改写器",
                     "2048² / 25 步"):
            self.assertIn(want, cases)


class BootScriptDrift(ApiBase):
    """开机脚本那条路**不在本仓库里** —— 体检必须能把「没有」与「有但不同源」
    分开说，而且**读不到不许抛**（那个文件在某些机器上根本不存在）。

    它是三处环境里唯一没有版本保护的一处：工作台那颗开关与页面那条可复制命令
    都从 `config.BACKEND_ENV` 现拼，而开机任务跑的是安装目录里那个 `.bat`。
    """

    def _root_with(self, text=None):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-boot-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        if text is not None:
            with open(os.path.join(tmp, config.BOOT_SCRIPT_NAME), "w",
                      encoding="utf-8", newline="") as handle:
                handle.write(text)
        p = mock.patch.object(config, "COMFY_ROOT", tmp)
        p.start()
        self.addCleanup(p.stop)
        return tmp

    def _lines(self, skip=()):
        out = ["@echo off", "cd /d \"C:\\somewhere\""]
        for key, value in sorted(config.BACKEND_ENV.items()):
            if key not in skip:
                out.append('set "%s=%s"' % (key, value))
        out.append('".venv\\Scripts\\python.exe" -s main.py --port 8188')
        return "\r\n".join(out) + "\r\n"

    def test_no_boot_script_is_reported_not_fatal(self):
        """★ 文件不在**不是错误**，但也不能算「同源」—— 这时页面那一行不画。"""
        self._root_with(None)
        boot = self.make().get("/api/backend").json()["boot_script"]
        self.assertFalse(boot["present"])
        self.assertFalse(boot["ok"])
        self.assertIsNone(boot["summary"])
        self.assertTrue(boot["path"].endswith(config.BOOT_SCRIPT_NAME))

    def test_a_matching_boot_script_says_so(self):
        self._root_with(self._lines())
        boot = self.make().get("/api/backend").json()["boot_script"]
        self.assertTrue(boot["present"])
        self.assertTrue(boot["ok"], boot)
        self.assertIn("同源", boot["summary"])

    def test_a_drifting_boot_script_is_called_out(self):
        """★ 2026-10-12 那台机器的形状：少了缓存那两个变量。"""
        self._root_with(self._lines(skip=("HF_HOME", "TORCH_HOME")))
        boot = self.make().get("/api/backend").json()["boot_script"]
        self.assertTrue(boot["present"])
        self.assertFalse(boot["ok"])
        self.assertEqual(boot["missing"], ["HF_HOME", "TORCH_HOME"])
        self.assertIn("另一套环境", boot["summary"])
        self.assertIn("HF_HOME", boot["summary"])


class LamaCheckpointFollowsTorchHome(unittest.TestCase):
    """LaMa 检查点必须按 **`TORCH_HOME`** 找，不能按 `~`。

    默认的 `~/.cache/torch` 是**账户相对**的：生产后端由开机脚本以 SYSTEM 拉起，
    而检查点是我们预放进共享缓存的那一份 ⇒ 按 `~` 找会报「检查点不在」，
    而它就在那儿。实测 2026-10-12 那条警告就是这么凭空冒出来的 —— 属于
    「误报会让人去重下 195 MB、或者以为前后发本来就拆不开」那一类。

    **三个用例都把 `USERPROFILE` 钉到一个空目录**：不钉的话，跑测试的这台机器
    自己的 `~` 里正好有那份检查点时，「按 `~` 找」这个错误实现也能蒙对。
    """

    def _empty_home(self):
        home = tempfile.mkdtemp(prefix="wbfakehome-")
        self.addCleanup(shutil.rmtree, home, True)
        return home

    def _with_env(self, torch_home):
        p = mock.patch.dict(os.environ, {"TORCH_HOME": torch_home,
                                         "USERPROFILE": self._empty_home()})
        p.start()
        self.addCleanup(p.stop)

    def _checkpoint_of(self, size):
        tmp = tempfile.mkdtemp(prefix="wbtorch-")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, lb.LAMA_CKPT_REL)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.truncate(size)      # 稀疏文件：报到 204,544,673 字节但不占盘
        self._with_env(tmp)
        return target

    def test_the_right_size_under_torch_home_is_ok(self):
        self._checkpoint_of(lb.LAMA_CKPT_BYTES)
        self.assertEqual(server._lama_checkpoint(), (True, lb.LAMA_CKPT_BYTES))

    def test_a_wrong_size_is_not_ok(self):
        self._checkpoint_of(lb.LAMA_CKPT_BYTES - 1)
        self.assertEqual(server._lama_checkpoint(),
                         (False, lb.LAMA_CKPT_BYTES - 1))

    def test_nothing_under_torch_home_is_reported_as_absent(self):
        empty = tempfile.mkdtemp(prefix="wbtorch-")
        self.addCleanup(shutil.rmtree, empty, True)
        self._with_env(empty)
        self.assertEqual(server._lama_checkpoint(), (False, None))

    def test_it_falls_back_to_where_the_backend_would_look(self):
        """★ 工作台自己没设 `TORCH_HOME` 时，要去**后端会拿到的那个位置**找。

        生产上就是这个形状：工作台与后端**都是开机任务以 SYSTEM 起的**，两个进程
        的环境里都没有 `TORCH_HOME`（那个变量写在开机脚本里、只进后端那个进程），
        而缓存位置由 `config.BACKEND_ENV` 定死在共享目录。只认进程环境或 `~`，
        这条体检在真机上就会一直误报「检查点不在」。
        """
        tmp = tempfile.mkdtemp(prefix="wbtorch-")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, lb.LAMA_CKPT_REL)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.truncate(lb.LAMA_CKPT_BYTES)
        # 空串就是「进程里没有这个变量」那一支 —— `or` 链上 falsy 就往下走
        env = mock.patch.dict(os.environ, {"TORCH_HOME": "",
                                           "USERPROFILE": self._empty_home()})
        env.start()
        self.addCleanup(env.stop)
        be = mock.patch.dict(config.BACKEND_ENV, {"TORCH_HOME": tmp})
        be.start()
        self.addCleanup(be.stop)
        self.assertEqual(server._lama_checkpoint(), (True, lb.LAMA_CKPT_BYTES))


class Build(ApiBase):
    def test_valid_t2i(self):
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body())
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["describe"]["mode"], "t2i")
        self.assertEqual(data["describe"]["steps"], 20)
        self.assertEqual(data["describe"]["seed"], 12345)
        self.assertIn("KSampler", data["node_classes"].values())
        self.assertGreater(len(data["node_classes"]), 10)
        self.assertIn("秒", data["estimate"]["human"])

    def test_empty_prompt_rejected(self):
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body(prompt="   "))
        self.assertEqual(r.status_code, 400)
        self.assertIn("提示词", r.json()["error"])

    def test_steps_out_of_range(self):
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body(steps=0))
        self.assertEqual(r.status_code, 400)
        self.assertIn("steps", r.json()["error"])

    def test_width_must_be_multiple_of_32(self):
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body(width=1000))
        self.assertEqual(r.status_code, 400)
        self.assertIn("32", r.json()["error"])

    def test_edit_requires_source_image(self):
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body(mode="edit"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("source_image", r.json()["error"])

    def test_non_json_body(self):
        tc = self.make()
        r = tc.post("/api/build", content=b"not json",
                    headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)

    def test_cfg_other_than_one_warns_about_doubling(self):
        tc = self.make()
        data = tc.post("/api/build", json=self.build_body(cfg=7.0)).json()
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("翻倍", texts)

    def test_a_negative_prompt_at_cfg_1_is_declared_dead(self):
        """填了反向词却在 CFG=1.0 —— 这是一个**没有症状**的错，必须提前说破。

        判据是那句话必须给出两件事：**不生效这个事实**、以及**怎么才能生效**。
        少任何一条，用户只知道"有点问题"却说不出下一步做什么。

        2026-10-01 起还有第三条：它必须**是一句话**。这句话原来是四行论文
        （实测证据 0/4194304 字节、逐字节相同、代价秒数），而用户要的是功能与
        下一步 —— 证据现在只在 `config.py` 与 `details.md` 里。所以这里反过来判：
        不许再出现「实测」「逐字节相同」这类论证。
        """
        tc = self.make()
        data = tc.post("/api/build",
                       json=self.build_body(cfg=1.0,
                                            negative_prompt="blurry, text")).json()
        hits = [w for w in data["warnings"] if "反向词" in w["text"]]
        self.assertEqual(len(hits), 1, data["warnings"])
        self.assertEqual(hits[0]["level"], "warn")
        text = hits[0]["text"]
        self.assertIn("不会生效", text)
        # 「怎么才能生效」必须带**那个数**：只说「把 CFG 提上去」用户还得回来问。
        self.assertIn("%.1f" % config.NEGATIVE_PROMPT_CFG_SUGGEST, text)
        for gone in ("实测", "逐字节相同", "4194304"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, text)

    def test_a_negative_prompt_at_cfg_above_1_is_declared_live(self):
        tc = self.make()
        data = tc.post("/api/build",
                       json=self.build_body(
                           cfg=config.NEGATIVE_PROMPT_CFG_SUGGEST,
                           negative_prompt="blurry")).json()
        hits = [w for w in data["warnings"] if "反向词" in w["text"]]
        self.assertEqual(len(hits), 1, data["warnings"])
        self.assertEqual(hits[0]["level"], "info")
        self.assertIn("已生效", hits[0]["text"])
        # 代价必须**连数字一起**说，否则那个「让它生效」的按钮看着像白送的。
        self.assertIn("%.1f/%.1f" % config.NEGATIVE_PROMPT_SECONDS_AT_SUGGEST,
                      hits[0]["text"])

    def test_cfg_above_1_without_a_negative_prompt_never_claims_it_is_used(self):
        """抬了 CFG 但没填反向词。

        这一条是**防止我自己写错**的：很容易顺手写成「没填反向词，所以白花两倍
        时间」。那是错的 —— 实测两版都空反向词时出图 74.3% 的字节不同，变的是
        cfg 本身。所以这里只准说「那一遍算的是空字符串」。
        """
        tc = self.make()
        data = tc.post("/api/build", json=self.build_body(cfg=2.0)).json()
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("翻倍", texts)                  # 既有的成本预警不能被顶掉
        self.assertIn("空字符串", texts)
        self.assertNotIn("已生效", texts)

    def test_the_dead_and_live_warnings_never_both_fire(self):
        """两条互斥分支不能同时出现 —— 同一次提交里说两遍 CFG，用户就得自己猜。"""
        for cfg, expect in ((1.0, "不会生效"), (2.0, "已生效")):
            with self.subTest(cfg=cfg):
                tc = self.make()
                data = tc.post("/api/build",
                               json=self.build_body(cfg=cfg,
                                                    negative_prompt="blurry")).json()
                texts = [w["text"] for w in data["warnings"]]
                self.assertEqual(sum(expect in t for t in texts), 1, texts)
                self.assertEqual(sum("不是 1.0" in t for t in texts), 0, texts)

    def test_the_negative_prompt_reaches_the_graph(self):
        tc = self.make()
        data = tc.post("/api/build",
                       json=self.build_body(
                           cfg=2.0,
                           negative_prompt="blurry, watermark")).json()
        # describe 是**从图里反解**出来的，所以这条断言证明它真的写进了那张图。
        self.assertEqual(data["describe"]["negative_prompt"], "blurry, watermark")
        self.assertEqual(data["node_classes"]["459:452"], "TextEncodeQwenImage21")

    def test_omitting_the_negative_prompt_leaves_the_base_alone(self):
        """缺字段 = 一个字都不动 —— 现有 golden 图就靠这条不变。"""
        tc = self.make()
        data = tc.post("/api/build", json=self.build_body()).json()
        self.assertEqual(data["describe"]["negative_prompt"], "")
        self.assertNotIn("反向词", " ".join(w["text"] for w in data["warnings"]))

    def test_a_blank_negative_prompt_is_not_treated_as_filled_in(self):
        """只打了空格不算填了 —— 否则用户会收到一句莫名其妙的「不会生效」。"""
        tc = self.make()
        data = tc.post("/api/build",
                       json=self.build_body(cfg=1.0,
                                            negative_prompt="   \n  ")).json()
        self.assertNotIn("反向词", " ".join(w["text"] for w in data["warnings"]))

    def test_a_non_string_negative_prompt_is_rejected_by_field_name(self):
        for bad in (True, 7, ["blurry"]):
            with self.subTest(bad=bad):
                tc = self.make()
                r = tc.post("/api/build", json=self.build_body(negative_prompt=bad))
                self.assertEqual(r.status_code, 400)
                self.assertIn("negative_prompt", r.json()["error"])

    def test_2k_warns_about_the_eleven_minutes(self):
        """2048² 那条警告的判据是**尺寸**（`2k` 模式 2026-10-01 撤销之后更清楚了）。"""
        tc = self.make()
        data = tc.post("/api/build",
                       json=self.build_body(mode="t2i", width=2048,
                                            height=2048)).json()
        self.assertTrue(data["describe"]["is_2k"])
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("11 分钟", texts)

    def test_the_removed_2k_mode_is_rejected_with_a_migration_hint(self):
        """`mode="2k"` 现在**当场 400**，而且错误里说清该怎么改。

        不做静默映射（把它当成 t2i+2048）：那会把「我用的还是老写法」藏起来。
        """
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body(mode="2k"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("width=height=2048", r.json()["error"])

    def test_the_removed_refine_mode_is_rejected_with_a_migration_hint(self):
        tc = self.make()
        r = tc.post("/api/build", json=self.build_body(mode="refine"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("rewrite=True", r.json()["error"])

    def test_without_an_explicit_size_nothing_gets_inflated(self):
        """没人再替用户补 2048 了 —— 没给尺寸就是「由节点 13 决定」。

        这一条是**反向**守的：以前 `mode="2k"` 会在缺尺寸时补上 2048，
        那个便捷写法撤销后，若有人手滑把那个默认值加回来，这里会立刻红。
        """
        tc = self.make()
        body = self.build_body(mode="t2i")
        body.pop("width")
        body.pop("height")
        data = tc.post("/api/build", json=body).json()
        self.assertIsNone(data["describe"]["width"])
        self.assertFalse(data["describe"]["is_2k"])

    def test_warns_when_backend_cannot_see_models(self):
        models = support.full_models()
        models["dit"]["present"] = False
        tc = self.make(models=models)
        data = tc.post("/api/build", json=self.build_body()).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(errs)
        self.assertIn("dit", errs[0]["text"])

    def test_default_1024_estimate_is_near_the_measured_60s(self):
        tc = self.make()
        data = tc.post("/api/build", json=self.build_body()).json()
        # 实测 1024²/20 步：冷 65 s、温 53.4 s。估算不该离得太远。
        self.assertGreater(data["estimate"]["seconds"], 45)
        self.assertLess(data["estimate"]["seconds"], 85)


class CacheAcceleration(ApiBase):
    """缓存加速开关。**默认关闭**是这次改动的主要契约，所以缺字段那条最先测。"""

    def test_off_unless_asked(self):
        data = self.make().post("/api/build", json=self.build_body()).json()
        self.assertFalse(data["describe"]["lazy_cache"])
        self.assertNotIn("LazyCache", data["node_classes"].values())

    def test_adds_the_node_when_asked(self):
        data = self.make().post("/api/build",
                                json=self.build_body(lazy_cache=True)).json()
        self.assertTrue(data["describe"]["lazy_cache"])
        self.assertIn("LazyCache", data["node_classes"].values())

    def test_false_and_missing_are_the_same(self):
        a = self.make().post("/api/build", json=self.build_body()).json()
        b = self.make().post("/api/build",
                             json=self.build_body(lazy_cache=False)).json()
        self.assertEqual(a["describe"]["lazy_cache"], b["describe"]["lazy_cache"])
        self.assertEqual({k: v for k, v in a["node_classes"].items() if v == "LazyCache"},
                         {k: v for k, v in b["node_classes"].items() if v == "LazyCache"})

    def test_a_string_from_curl_counts(self):
        """表单会给布尔，但 curl 多半递 "true" —— 那时也该算开。"""
        for value in ("true", "1", "on", True):
            with self.subTest(value=value):
                data = self.make().post(
                    "/api/build", json=self.build_body(lazy_cache=value)).json()
                self.assertTrue(data["describe"]["lazy_cache"])

    def test_says_what_it_costs_and_that_the_estimate_ignores_it(self):
        """估算刻意不把加速算进去（宁可偏保守），那就得说出来 —— 否则用户看到
        预计 65 秒、实际 28 秒，只会以为工作台算错了。"""
        data = self.make().post("/api/build",
                                json=self.build_body(lazy_cache=True)).json()
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("缓存加速", texts)
        self.assertIn("偏保守", texts)


class TeSpeedAcceleration(ApiBase):
    """TE-Speed 开关。三条主要契约：默认关、与 LazyCache 互斥、插件不在时是 error。"""

    NODE = "TESpeedQwenImage21"

    def test_off_unless_asked(self):
        data = self.make().post("/api/build", json=self.build_body()).json()
        self.assertFalse(data["describe"]["te_speed"])
        self.assertNotIn(self.NODE, data["node_classes"].values())

    def test_adds_the_node_when_asked(self):
        data = self.make(node_classes=(self.NODE,)).post(
            "/api/build", json=self.build_body(te_speed=True)).json()
        self.assertTrue(data["describe"]["te_speed"])
        self.assertIn(self.NODE, data["node_classes"].values())

    def test_false_and_missing_are_the_same(self):
        a = self.make(node_classes=(self.NODE,)).post(
            "/api/build", json=self.build_body()).json()
        b = self.make(node_classes=(self.NODE,)).post(
            "/api/build", json=self.build_body(te_speed=False)).json()
        self.assertEqual(a["describe"]["te_speed"], b["describe"]["te_speed"])
        self.assertEqual({k: v for k, v in a["node_classes"].items()
                          if v == self.NODE},
                         {k: v for k, v in b["node_classes"].items()
                          if v == self.NODE})

    def test_a_string_from_curl_counts(self):
        """表单会给布尔，但 curl 多半递 "true" —— 那时也该算开。"""
        for value in ("true", "1", "on", True):
            with self.subTest(value=value):
                data = self.make(node_classes=(self.NODE,)).post(
                    "/api/build", json=self.build_body(te_speed=value)).json()
                self.assertTrue(data["describe"]["te_speed"])

    def test_says_what_it_costs_and_that_the_estimate_ignores_it(self):
        data = self.make(node_classes=(self.NODE,)).post(
            "/api/build", json=self.build_body(te_speed=True)).json()
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("TE-Speed", texts)
        self.assertIn("偏保守", texts)

    def test_missing_plugin_is_an_error_not_a_warn(self):
        """插件没装时提交**必然失败**（ComfyUI 校验阶段就打回），所以与缺模型
        文件同级 —— 是 error，不是「提醒一下」。"""
        data = self.make(node_classes=()).post(
            "/api/build", json=self.build_body(te_speed=True)).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(any(self.NODE in w["text"] for w in errs),
                        "缺插件没被报成 error：%r" % data["warnings"])
        # 光说"没有"没用 —— 得说清楚去哪装
        self.assertTrue(any("custom_nodes" in w["text"] for w in errs))

    def test_plugin_is_not_probed_when_the_switch_is_off(self):
        """不勾它的人不该为一次节点体检多付一次往返。"""
        tc = self.make(node_classes=(self.NODE,))
        tc.post("/api/build", json=self.build_body())
        self.assertNotIn(self.NODE, self.client.object_info_calls)
        tc.post("/api/build", json=self.build_body(te_speed=True))
        self.assertIn(self.NODE, self.client.object_info_calls)

    def test_backend_down_says_it_could_not_check_not_that_it_is_missing(self):
        """「没起」与「后端没有这个类」是两种病：一个去拉后端，一个去装插件。
        后端没起时**不能**报成缺插件。"""
        data = self.make(running=False).post(
            "/api/build", json=self.build_body(te_speed=True)).json()
        errs = [w["text"] for w in data["warnings"] if w["level"] == "error"]
        self.assertFalse(any(self.NODE in t for t in errs),
                         "后端没起被误报成缺插件：%r" % data["warnings"])
        self.assertTrue(any("读不到" in t for t in
                            [w["text"] for w in data["warnings"]]))

    def test_both_accelerators_together_is_a_400(self):
        """两个开关插在同一条 model 连线上。与其自动挑一个、让用户以为两个都生效，
        不如当场拒掉，并在话里带上"为什么推荐 TE-Speed"的实测数字。"""
        r = self.make().post("/api/build",
                             json=self.build_body(lazy_cache=True, te_speed=True))
        self.assertEqual(r.status_code, 400)
        self.assertIn("二选一", r.json()["error"])


class TurboAcceleration(ApiBase):
    """Turbo 开关走端点那一路。三条主要契约：**默认关**、与两个加速**不互斥**、
    权重不在时是 error。

    与 `TeSpeedAcceleration` 那一组刻意分开：Turbo 的失败模式不一样 —— 它缺的是
    **权重文件**（不是节点类），而且它**不抢**那条 model 连线，所以「二选一」
    那套对它一个字都不适用。混在一组里最容易把两条契约写串。
    """

    NODE = "LoraLoaderModelOnly"
    FILE = qb.TURBO_LORA_FILE

    def with_lora(self, **kw):
        """把替身配成「turbo 权重下好了」的样子。"""
        combos = {"LoraLoaderModelOnly": {"lora_name": [self.FILE]}}
        combos.update(kw.pop("combos", {}))
        return self.make(node_classes=(self.NODE,), combos=combos, **kw)

    def test_off_unless_asked(self):
        data = self.make().post("/api/build", json=self.build_body()).json()
        self.assertFalse(data["describe"]["turbo"])
        self.assertNotIn(self.NODE, data["node_classes"].values())

    def test_adds_the_node_when_asked(self):
        data = self.with_lora().post(
            "/api/build", json=self.build_body(turbo=True)).json()
        self.assertTrue(data["describe"]["turbo"])
        self.assertIn(self.NODE, data["node_classes"].values())

    def test_false_and_missing_are_the_same(self):
        a = self.with_lora().post("/api/build", json=self.build_body()).json()
        b = self.with_lora().post("/api/build",
                                  json=self.build_body(turbo=False)).json()
        self.assertEqual(a["describe"]["turbo"], b["describe"]["turbo"])
        self.assertFalse(a["describe"]["turbo"])

    def test_a_string_from_curl_counts(self):
        """表单会给布尔，但 curl 多半递 "true" —— 那时也该算开。"""
        for value in ("true", "1", "on", True):
            with self.subTest(value=value):
                data = self.with_lora().post(
                    "/api/build", json=self.build_body(turbo=value)).json()
                self.assertTrue(data["describe"]["turbo"])

    def test_it_sits_before_the_accelerator(self):
        """**顺序就是契约**：`Cache → LoRA → TE-Speed → KSampler`。

        接反了没有症状（出图逐位相同，只是每张白花约 3 秒），所以只能断图。
        `/api/build` 的回执**故意只回摘要**（见 `post_build` 里那句注释），
        所以这里走 `/api/run` 去读**真正提交的那一份** —— 比读回执更强。

        另外注意：端点上的 `te_speed` 默认是 false（请求体说了算），与
        `qb.build()` 那个「不勾也接」的默认**不是**一回事，所以要显式传。
        """
        tc = self.with_lora()
        r = tc.post("/api/run", json=self.build_body(turbo=True, te_speed=True))
        self.assertEqual(r.status_code, 200, r.text)
        graph = self.client.submitted[0]["workflow"]
        self.assertEqual(graph["9800"]["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(graph["9800"]["inputs"]["model"], [qb.CACHE, 0])
        self.assertEqual(graph["9900"]["inputs"]["model"], ["9800", 0])
        self.assertEqual(graph[qb.KSAMPLER]["inputs"]["model"], ["9900", 0])

    def test_it_also_goes_before_lazy_but_the_two_are_still_exclusive(self):
        """Turbo 与 LazyCache 也不互斥：`Cache → LoRA → LazyCache → KSampler`。
        但 LazyCache 与 TE-Speed 那对**仍然**互斥 —— 别把 turbo 的加入读成
        「那个二选一取消了」。"""
        tc = self.with_lora()
        r = tc.post("/api/run", json=self.build_body(turbo=True, lazy_cache=True,
                                                     te_speed=False))
        self.assertEqual(r.status_code, 200, r.text)
        graph = self.client.submitted[0]["workflow"]
        self.assertEqual(graph["9800"]["inputs"]["model"], [qb.CACHE, 0])
        self.assertEqual(graph[qb.LAZY_CACHE]["inputs"]["model"], ["9800", 0])
        self.assertEqual(graph[qb.KSAMPLER]["inputs"]["model"], [qb.LAZY_CACHE, 0])

        bad = tc.post("/api/build", json=self.build_body(lazy_cache=True,
                                                         te_speed=True))
        self.assertEqual(bad.status_code, 400)
        self.assertIn("二选一", bad.json()["error"])

    def test_it_neither_needs_nor_triggers_the_two_way_exclusion(self):
        """Turbo 与那两个**都不互斥**：三种组合都得是 200，而且都真的接上了。"""
        for kw in (dict(turbo=True),
                   dict(turbo=True, lazy_cache=True, te_speed=False),
                   dict(turbo=True, te_speed=True)):
            with self.subTest(kw=kw):
                r = self.with_lora().post("/api/build",
                                          json=self.build_body(**kw))
                self.assertEqual(r.status_code, 200, r.text)
                self.assertTrue(r.json()["describe"]["turbo"])

    def test_it_fills_steps_and_scheduler_when_not_given(self):
        """一个开关要做完整件事：只挂 LoRA 不动步数，等于拿 25 步的配方喂一个按
        大步蒸馏过的模型。`build_body` 默认带 `steps=20`，所以这里得先去掉它。"""
        body = self.build_body(turbo=True)
        del body["steps"]
        data = self.with_lora().post("/api/build", json=body).json()
        self.assertEqual(data["describe"]["steps"], qb.TURBO_DEFAULT_STEPS)
        self.assertEqual(data["describe"]["scheduler"],
                         qb.TURBO_DEFAULT_SCHEDULER)

    def test_explicit_steps_win(self):
        data = self.with_lora().post(
            "/api/build", json=self.build_body(turbo=True, steps=25)).json()
        self.assertEqual(data["describe"]["steps"], 25)

    def test_missing_lora_is_an_error_not_a_warn(self):
        """权重不在 = 提交必然失败（ComfyUI 的 COMBO 校验阶段就打回），所以与
        缺模型文件同级 —— 是 error，不是「提醒一下」。而且要点名是哪个文件。"""
        combos = {"LoraLoaderModelOnly": {"lora_name": ["别的.safetensors"]}}
        data = self.make(node_classes=(self.NODE,), combos=combos).post(
            "/api/build", json=self.build_body(turbo=True)).json()
        errs = [w["text"] for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(any(self.FILE in t for t in errs),
                        "缺权重没报成 error 且点名文件：%r" % data["warnings"])

    def test_backend_down_says_it_could_not_check_not_that_it_is_missing(self):
        """「没起」与「后端看不到这个文件」是两种病：一个去拉后端，一个去下权重。"""
        data = self.make(running=False).post(
            "/api/build", json=self.build_body(turbo=True)).json()
        errs = [w["text"] for w in data["warnings"] if w["level"] == "error"]
        self.assertFalse(any(self.FILE in t for t in errs),
                         "后端没起被误报成缺权重：%r" % data["warnings"])
        self.assertTrue(any("读不到" in w["text"] for w in data["warnings"]))

    def test_the_lora_is_not_probed_when_the_switch_is_off(self):
        """不勾它的人不该为一次多出来的体检付一次往返 —— 与 TE-Speed 同理。"""
        tc = self.with_lora()
        tc.post("/api/build", json=self.build_body())
        self.assertNotIn(self.NODE, self.client.object_info_calls)
        tc.post("/api/build", json=self.build_body(turbo=True))
        self.assertIn(self.NODE, self.client.object_info_calls)

    def test_says_what_it_costs_and_that_the_estimate_ignores_it(self):
        data = self.with_lora().post(
            "/api/build", json=self.build_body(turbo=True)).json()
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("Turbo", texts)
        # 估时器**刻意**不把加速算进去 —— 不说的话，用户看到预计 60 秒、
        # 实际 8 秒，只会以为工作台算错了。
        self.assertIn("偏保守", texts)
        # 「8 步是最差的一端」是这一轮最反直觉的结论，页面上必须说出来，
        # 否则用户会顺手把步数调回 8。
        self.assertIn("最差", texts)

    def test_no_turbo_warning_when_the_switch_is_off(self):
        data = self.make().post("/api/build", json=self.build_body()).json()
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertNotIn("Turbo", texts)


class Run(ApiBase):
    def test_down_returns_503(self):
        tc = self.make(running=False)
        r = tc.post("/api/run", json=self.build_body())
        self.assertEqual(r.status_code, 503)
        self.assertIn("后端未运行", r.json()["error"])

    def test_submit_makes_job_id_equal_prompt_id(self):
        """这是整个设计的枢纽：一个键贯穿 PNG / history / 探针 / 记录。"""
        tc = self.make()
        r = tc.post("/api/run", json=self.build_body())
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["job_id"], data["prompt_id"])
        self.assertEqual(self.client.submitted[0]["job_id"], data["job_id"])

    def test_submit_uses_the_watchers_client_id(self):
        """不一致的话 ComfyUI 会把 execution_* 事件发给别人
        （`execution.py:736`），进度就永远不动了。"""
        tc = self.make()
        tc.post("/api/run", json=self.build_body())
        self.assertEqual(self.client.submitted[0]["client_id"],
                         self.wb.watcher.client_id)

    def test_submitted_workflow_has_the_edge_repair(self):
        """提交的图必须已经补好断边 —— 否则输出只由 seed 决定（空转）。"""
        tc = self.make()
        tc.post("/api/run", json=self.build_body())
        wf = self.client.submitted[0]["workflow"]
        self.assertEqual(wf["459:474"]["inputs"]["on_false"], PROMPT)
        self.assertEqual(wf["459:471"]["inputs"]["prompt"], PROMPT)
        self.assertFalse(wf["459:474"]["inputs"]["switch"])

    def test_recorded_as_queued_and_announced(self):
        tc = self.make()
        data = tc.post("/api/run", json=self.build_body()).json()
        rec = self.wb.store.get(data["job_id"])
        self.assertEqual(rec["status"], "queued")
        self.assertEqual(rec["prompt_text"], PROMPT)
        # replay_since 返回 (序号, 事件) 二元组
        last = self.wb.broker.replay_since(0)[-1][1]
        self.assertEqual(last["type"], "job_queued")
        self.assertEqual(last["prompt_id"], data["job_id"])

    def test_rejection_is_400_and_job_is_recorded_as_error(self):
        from workbench import comfy
        tc = self.make(submit_error=comfy.ComfyRejected(
            "值不在列表里", {"459:451": {"errors": [{"message": "missing"}]}}))
        r = tc.post("/api/run", json=self.build_body())
        self.assertEqual(r.status_code, 400)
        self.assertIn("459:451", r.json()["node_errors"])
        jobs = tc.get("/api/jobs").json()["jobs"]
        self.assertEqual(jobs[0]["status"], "error")

    def test_unreachable_is_503_and_recorded(self):
        from workbench import comfy
        tc = self.make(submit_error=comfy.ComfyUnreachable("连不上 ComfyUI"))
        r = tc.post("/api/run", json=self.build_body())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(tc.get("/api/jobs").json()["jobs"][0]["status"], "error")

    def test_every_submission_carries_its_own_save_prefix(self):
        """重复提交必须写得出新文件（2026-10-02 用户报的「原参数重复生图：秒完成
        且不会有任何图像产生」）。

        根因不在我们这边，但修法在我们这边：ComfyUI 的缓存认**节点输入**，
        一字不改地再提一遍，它一个节点都不跑，只把**上一次**的产物名回放给
        `history`（`execution.py` 里命中缓存的节点走 `emit_cached_output()` 就
        返回）—— 那张文件要是已经删了（P20f 的删除是进回收站），就既没有新图、
        也拿不回旧图，而页面上写着「完成」。
        所以每次提交都把收尾节点的前缀带上本次作业的短标记：收尾那个节点于是
        「输入变了」→ 自己重跑一次，把缓存里那份重新写出来（上游仍然一个都不重算）。
        """
        tc = self.make()
        first = tc.post("/api/run", json=self.build_body()).json()
        wf1 = self.client.submitted[0]["workflow"]
        self.assertEqual(wf1["461"]["inputs"]["filename_prefix"],
                         "Qwen_image_2.1_" + first["job_id"][:8])
        tc.post("/api/run", json=self.build_body())
        wf2 = self.client.submitted[1]["workflow"]
        self.assertNotEqual(wf1["461"]["inputs"]["filename_prefix"],
                            wf2["461"]["inputs"]["filename_prefix"])
        self.assertEqual(wf2["461"]["inputs"]["filename_prefix"],
                         "Qwen_image_2.1_" + self.client.submitted[1]["job_id"][:8])
        # 图上**只有**这一处不同：其余节点逐字相同（否则就不是「同一份参数」了）
        self.assertEqual(
            [nid for nid in wf1 if json.dumps(wf1[nid], sort_keys=True)
             != json.dumps(wf2[nid], sort_keys=True)], ["461"])

    def test_the_preflight_build_keeps_the_pristine_prefix(self):
        """预检（`/api/build`）与黄金快照拿到的仍是原样的名字：唯一前缀是
        **提交**这件事的属性，不是这张图的属性。"""
        workflow, _, _ = server.build_from_body(self.build_body())
        self.assertEqual(workflow["461"]["inputs"]["filename_prefix"],
                         "Qwen_image_2.1")
        # 同一个 body 交给提交路径才带上标记，而且**只动那一处**
        tc = self.make()
        tc.post("/api/run", json=self.build_body())
        submitted = self.client.submitted[0]["workflow"]
        self.assertNotEqual(submitted["461"]["inputs"]["filename_prefix"],
                            workflow["461"]["inputs"]["filename_prefix"])
        self.assertEqual(
            [nid for nid in submitted if json.dumps(submitted[nid], sort_keys=True)
             != json.dumps(workflow[nid], sort_keys=True)], ["461"])

    def test_bad_params_never_reach_the_backend(self):
        tc = self.make()
        r = tc.post("/api/run", json=self.build_body(steps=999))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.wb.store.count(), 0)


    def test_submitted_workflow_carries_the_cache_node_only_when_asked(self):
        tc = self.make()
        tc.post("/api/run", json=self.build_body())
        self.assertNotIn("990", self.client.submitted[0]["workflow"])
        tc.post("/api/run", json=self.build_body(lazy_cache=True))
        wf = self.client.submitted[1]["workflow"]
        self.assertEqual(wf["990"]["class_type"], "LazyCache")
        self.assertEqual(wf["990"]["inputs"]["model"], ["459:480", 0])
        self.assertEqual(wf["459:458"]["inputs"]["model"], ["990", 0])

    def test_the_record_remembers_the_flag(self):
        """不记下来的话，画廊里两张图看不出区别，也没法复现其中一张。"""
        tc = self.make()
        data = tc.post("/api/run", json=self.build_body(lazy_cache=True)).json()
        rec = self.wb.store.get(data["job_id"])
        self.assertTrue(rec["params"]["lazy_cache"])


class JobRoutes(ApiBase):
    def submit(self):
        return self.make().post("/api/run", json=self.build_body()).json()

    def test_list_and_get(self):
        data = self.submit()
        jid = data["job_id"]
        listing = self.tc.get("/api/jobs").json()
        self.assertEqual(listing["total"], 1)
        self.assertIn(jid, listing["active"])
        one = self.tc.get("/api/jobs/" + jid)
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["job_id"], jid)

    def test_unknown_job_is_404(self):
        self.make()
        self.assertEqual(self.tc.get("/api/jobs/nope").status_code, 404)

    def test_cancel_pending_job_marks_cancelled(self):
        old = config.CANCEL_WAIT_S
        config.CANCEL_WAIT_S = 0.2          # 测试里别真等 5 秒
        self.addCleanup(setattr, config, "CANCEL_WAIT_S", old)
        data = self.submit()
        r = self.tc.post("/api/jobs/%s/cancel" % data["job_id"], json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["job"]["status"], "cancelled")
        self.assertEqual(self.client.cancelled[0][0], data["job_id"])

    def test_cancel_finished_job_is_a_no_op(self):
        self.make()
        self.wb.store.finish("gone", "done")
        self.assertEqual(
            self.tc.post("/api/jobs/gone/cancel", json={}).status_code, 404)

    def test_cancel_unknown_job_is_404(self):
        self.make()
        self.assertEqual(
            self.tc.post("/api/jobs/nope/cancel", json={}).status_code, 404)


class Image(ApiBase):
    def test_no_output_yet_is_404(self):
        tc = self.make()
        self.assertEqual(tc.get("/api/image?job_id=nope").status_code, 404)

    def test_serves_bytes_through_the_backend(self):
        tc = self.make()
        self.client.views["Qwen_image_2.1_00042.png"] = ("image/png", b"\x89PNG\r\n\x1a\nfake")
        self.wb.store.create(job_id="img-1", mode="t2i", params={},
                             prompt_text="x", workflow={"461": {"class_type": "SaveImageAdvanced"}})
        self.wb.store.patch("img-1", outputs=[{"filename": "Qwen_image_2.1_00042.png",
                                               "subfolder": "", "type": "output"}])
        r = tc.get("/api/image?job_id=img-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(r.content.startswith(b"\x89PNG"))
        # 序号会在后端重启后从头开始，长缓存会拿旧图
        self.assertIn("no-store", r.headers["cache-control"])

    def test_backend_down_is_503(self):
        tc = self.make()
        self.wb.store.create(job_id="img-2", mode="t2i", params={},
                             prompt_text="x", workflow={})
        self.wb.store.patch("img-2", outputs=[{"filename": "missing.png",
                                               "subfolder": "", "type": "output"}])
        self.assertEqual(tc.get("/api/image?job_id=img-2").status_code, 503)


class EventStream(ApiBase):
    """判据 2 的服务端半边：重放 → 快照 → 实时，顺序不能变。"""

    def frames(self, last_id, publish_before=(), publish_after=(), count=2,
               pre=None):
        tc = self.make()
        if pre is not None:
            pre(self.wb)
        for ev in publish_before:
            self.wb.broker.publish(ev)

        async def go():
            gen = server.event_stream(self.wb, last_id)
            out = []
            for _ in range(count):
                out.append(await gen.__anext__())
            for ev in publish_after:
                self.wb.broker.publish(ev)
            out.append(await gen.__anext__())
            await gen.aclose()
            return out
        return asyncio.run(go())

    def test_retry_then_snapshot_then_live(self):
        out = self.frames(None, publish_after=[{"type": "progress", "value": 3}])
        self.assertTrue(out[0].startswith("retry:"))
        self.assertIn("event: snapshot", out[1])
        snap = json.loads(out[1].split("data: ", 1)[1])
        self.assertIn("jobs", snap)
        self.assertIn("cost_model", snap)
        self.assertIn("event: progress", out[2])
        self.assertIn('"value": 3', out[2])

    def test_replay_fills_the_gap_on_reconnect(self):
        out = self.frames(0, publish_before=[{"type": "a"}, {"type": "b"}], count=3)
        self.assertIn("event: a", out[1])
        self.assertIn("event: b", out[2])

    def test_replay_skips_what_the_client_already_saw(self):
        out = self.frames(1, publish_before=[{"type": "a"}, {"type": "b"}], count=2)
        self.assertTrue(out[0].startswith("retry:"))
        self.assertIn("event: b", out[1])
        self.assertNotIn("event: a", out[1])

    def test_snapshot_carries_live_jobs_so_a_refresh_recovers_state(self):
        def seed(wb):
            wb.store.create(job_id="live-1", mode="t2i", params={"steps": 20},
                            prompt_text="a cat", workflow={})
        out = self.frames(None, count=2, pre=seed)
        snap = json.loads(out[1].split("data: ", 1)[1])
        self.assertEqual([j["job_id"] for j in snap["jobs"]], ["live-1"])
        self.assertEqual(snap["active"], ["live-1"])

    def test_subscriber_is_removed_when_the_client_goes_away(self):
        self.make()

        async def go():
            gen = server.event_stream(self.wb, None)
            await gen.__anext__()
            self.assertEqual(self.wb.broker.subscriber_count, 1)
            await gen.aclose()
        asyncio.run(go())
        self.assertEqual(self.wb.broker.subscriber_count, 0)


class GalleryFixture(ApiBase):
    """画廊测试的公共夹具：输出目录 patch 成临时目录。

    抽出来是因为 `GalleryRoutes`（读）与 `GalleryDelete`（写）要用**同一套**
    目录与造图方式 —— 各自复制一份的话，「删的到底是哪一份」这件事就没人验了。
    """

    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp_gallery = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-galroute-")
        self.addCleanup(shutil.rmtree, self.tmp_gallery, ignore_errors=True)

    def roots(self):
        path = os.path.join(self.tmp_gallery, "output")
        os.makedirs(path, exist_ok=True)
        return [{"path": path, "label": "测试输出目录", "kind": "test",
                 "exists": True}]

    def put_png(self, name, workflow=None, blob=None):
        roots = self.roots()
        path = os.path.join(roots[0]["path"], name)
        if blob is None:
            blob = make_png_blob(workflow or qwen_graph())
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def put_file(self, name, text="do not delete me"):
        path = os.path.join(self.roots()[0]["path"], name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def make_with_roots(self, roots=None, **kw):
        tc = self.make(**kw)
        fixed = roots if roots is not None else self.roots()
        patcher = mock.patch.object(gallery, "candidate_roots",
                                    side_effect=lambda *a, **k: fixed)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tc


class GalleryRoutes(GalleryFixture):
    """`/api/gallery` 与 `/api/gallery/image`。

    另有一条 `RealOutputs` 专门对真实目录做只读冒烟（找不到就跳过）。
    """

    def test_lists_pngs_with_metadata(self):
        tc = self.make_with_roots()
        self.put_png("a.png", qwen_graph(seed=42, steps=30))
        data = tc.get("/api/gallery").json()
        self.assertEqual(data["scanned"], 1)
        item = data["items"][0]
        self.assertEqual(item["filename"], "a.png")
        self.assertEqual(item["seed"], 42)
        self.assertEqual(item["steps"], 30)
        self.assertEqual(item["mode"], "t2i")
        self.assertTrue(item["recognized"])

    def test_filter_params(self):
        tc = self.make_with_roots()
        self.put_png("a.png", qwen_graph(seed=1))
        self.put_png("b.png", qwen_graph(seed=2, switch=True))
        # 改写是**额外功能**（不是模式），所以要它自己的参数 —— 而两张图都是 t2i：
        # `switch=True` 那张不再是「另一个模式」，它只是打开过改写器。
        self.assertEqual(tc.get("/api/gallery?rewrite=1").json()["total"], 1)
        self.assertEqual(tc.get("/api/gallery?mode=t2i").json()["total"], 2)
        self.assertEqual(tc.get("/api/gallery?recognized=0").json()["total"], 0)
        self.assertEqual(tc.get("/api/gallery?q=apple").json()["total"], 2)
        self.assertEqual(tc.get("/api/gallery?q=nothing").json()["total"], 0)

    def test_reports_roots_so_the_ui_can_explain_itself(self):
        tc = self.make_with_roots()
        data = tc.get("/api/gallery").json()
        self.assertEqual(len(data["roots"]), 1)
        self.assertEqual(data["roots"][0]["label"], "测试输出目录")
        self.assertTrue(data["roots"][0]["exists"])
        self.assertIn("counts", data)

    def test_empty_gallery_is_not_an_error(self):
        tc = self.make_with_roots()
        data = tc.get("/api/gallery").json()
        self.assertEqual(data["items"], [])
        self.assertEqual(data["scanned"], 0)

    def test_serves_image_bytes_from_disk(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        r = tc.get("/api/gallery/image?root=0&name=a.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(r.content.startswith(b"\x89PNG"))
        self.assertIn("etag", {k.lower() for k in r.headers})

    def test_revalidates_with_etag_instead_of_redownloading(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        first = tc.get("/api/gallery/image?root=0&name=a.png")
        etag = first.headers["etag"]
        again = tc.get("/api/gallery/image?root=0&name=a.png",
                       headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")

    def test_path_traversal_is_blocked(self):
        tc = self.make_with_roots()
        secret = os.path.join(self.tmp_gallery, "secret.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("do not serve me")
        for bad in ("../secret.txt", "..%2Fsecret.txt", "a/../../secret.txt",
                    "/etc/hosts", "C:/windows/win.ini"):
            r = tc.get("/api/gallery/image?root=0&name=" + bad)
            self.assertIn(r.status_code, (400, 404), bad)
            self.assertNotIn(b"do not serve me", r.content)

    def test_root_out_of_range_is_404(self):
        tc = self.make_with_roots()
        self.assertEqual(
            tc.get("/api/gallery/image?root=99&name=a.png").status_code, 404)
        self.assertEqual(
            tc.get("/api/gallery/image?root=abc&name=a.png").status_code, 400)

    def test_missing_file_is_404(self):
        tc = self.make_with_roots()
        self.assertEqual(
            tc.get("/api/gallery/image?root=0&name=nope.png").status_code, 404)

    def test_works_with_backend_down(self):
        """画廊刻意不依赖后端 —— 全从磁盘读。这是它相对 Web UI 的卖点之一。"""
        tc = self.make_with_roots(running=False)
        self.put_png("a.png")
        data = tc.get("/api/gallery").json()
        self.assertEqual(data["scanned"], 1)
        self.assertEqual(
            tc.get("/api/gallery/image?root=0&name=a.png").status_code, 200)


class GalleryThumbs(GalleryFixture):
    """`/api/gallery/thumb`（P20l）—— 格子上那张小图。

    现场一页 18 格的原图实测合计 **65.4 MB**（最大一张 4096² / 18.5 MB），浏览器
    每一张都整份下、整份解码 —— 用户报的「有高分辨率的作品时页面卡顿」就是它。

    这条路的判据**分两半**，第二半更重要：

    1. 有 Pillow 时**真的变小了**（JPEG、长边 ≤ 512、字节远小于原图）；
    2. **缩不出来时必须原样发原图**（没装 Pillow / 这一张解不开）—— 它的坏法是
       没有症状的那一种：格子上空着，或者整条取图路 500，而用户只是想把图看见。
    """

    def thumb(self, tc, root=0, name="a.png", headers=None):
        return tc.get("/api/gallery/thumb",
                      params={"root": root, "name": name}, headers=headers)

    @unittest.skipUnless(thumbs.available(), "没装 Pillow，缩略图这条路不存在")
    def test_it_serves_a_small_jpeg_instead_of_the_original(self):
        tc = self.make_with_roots()
        src = self.put_png("a.png", blob=real_png_blob(qwen_graph()))
        r = self.thumb(tc)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertEqual(r.headers["x-thumb"], "jpeg")
        self.assertTrue(r.content.startswith(b"\xff\xd8\xff"), "应当是 JPEG 字节")
        src_bytes = os.path.getsize(src)
        self.assertLess(len(r.content), src_bytes / 10.0,
                        "缩略图必须**小一个量级**，不是小一点点（%d → %d 字节）"
                        % (src_bytes, len(r.content)))

    @unittest.skipUnless(thumbs.available(), "没装 Pillow，缩略图这条路不存在")
    def test_the_cache_is_written_under_the_state_directory(self):
        """缓存落在**状态目录**下面（`state/thumbs`），一个字都不许写进 output/。"""
        tc = self.make_with_roots()
        self.put_png("a.png", blob=real_png_blob(qwen_graph()))
        out_dir = self.roots()[0]["path"]
        before = sorted(os.listdir(out_dir))
        self.thumb(tc)
        self.assertEqual(sorted(os.listdir(out_dir)), before,
                         "输出目录里一张文件都不许多（那里是用户的作品）")
        cache = thumbs.cache_dir(self.wb.state_dir)
        self.assertEqual(len(os.listdir(cache)), 1)

    @unittest.skipUnless(thumbs.available(), "没装 Pillow，缩略图这条路不存在")
    def test_the_second_request_comes_from_the_cache(self):
        tc = self.make_with_roots()
        self.put_png("a.png", blob=real_png_blob(qwen_graph()))
        first = self.thumb(tc)
        again = self.thumb(tc)
        self.assertEqual(first.content, again.content)
        self.assertEqual(len(os.listdir(thumbs.cache_dir(self.wb.state_dir))), 1,
                         "同一个文件第二次不该再缩一遍")

    @unittest.skipUnless(thumbs.available(), "没装 Pillow，缩略图这条路不存在")
    def test_the_etag_comes_from_the_source_not_from_the_cache(self):
        """ETag 按**源图**算。按缩略图文件本身算的话，收尾清掉缓存之后 ETag 就变了，
        页面手里的 18 份全部作废 —— 重画画廊又会白下一遍。"""
        tc = self.make_with_roots()
        self.put_png("a.png", blob=real_png_blob(qwen_graph()))
        first = self.thumb(tc)
        self.assertEqual(first.headers["x-thumb"], "jpeg")
        shutil.rmtree(thumbs.cache_dir(self.wb.state_dir))     # 缓存被清掉
        again = self.thumb(tc, headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")
        # 缓存没了也不许报错：这一张会被重新缩一遍
        self.assertEqual(self.thumb(tc).status_code, 200)

    def test_a_broken_png_falls_back_to_the_original(self):
        """`make_png_blob` 那种没有 IDAT 的图**解不开** —— 那就发原图（PNG），
        不是 500、也不是空白。"""
        tc = self.make_with_roots()
        self.put_png("a.png")                       # 默认就是那种最小 PNG
        r = self.thumb(tc)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-thumb"], "original")
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(r.content, tc.get("/api/gallery/image?root=0&name=a.png").content)

    def test_without_pillow_the_endpoint_still_serves_the_image(self):
        """Pillow 不在 = **这条路整个消失**，不是「画廊坏了」（`vision.py` 那条纪律）。

        这一条不装 Pillow 也能跑：把模块里那个 `Image` 换成 None，走的就是那条路。
        """
        tc = self.make_with_roots()
        self.put_png("a.png", blob=real_png_blob(qwen_graph()))
        with mock.patch.object(thumbs, "Image", None):
            r = self.thumb(tc)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-thumb"], "original")
        self.assertTrue(r.content.startswith(b"\x89PNG"), "发的该是原图字节")
        self.assertFalse(os.path.isdir(thumbs.cache_dir(self.wb.state_dir)),
                         "没装 Pillow 时连缓存目录都不该建")

    def test_the_original_route_is_untouched(self):
        """`/api/gallery/image` 仍然发**原图** —— 放大视图、1:1、拖去做参考图都靠它。
        拿缩略图当原图是**没有症状**的降级（传得上去、出得来图，只是少一半像素）。"""
        tc = self.make_with_roots()
        self.put_png("a.png", blob=real_png_blob(qwen_graph()))
        r = tc.get("/api/gallery/image?root=0&name=a.png")
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(r.content.startswith(b"\x89PNG"))
        self.assertEqual(len(r.content), self.size_of("a.png"))

    @unittest.skipUnless(thumbs.available(), "没装 Pillow，缩略图这条路不存在")
    def test_the_archived_view_gets_its_own_root(self):
        """归档档取图必须带 `&archived=1`（那批图不在 `candidate_roots()` 里）。

        不带就每格 404，更坏的是**取到 `output/` 里同名的另一张** —— 而那一张还画得
        出来，没有症状。缩略图那条路是**另一个口**，所以这一条得单独钉一次。
        """
        arch = os.path.join(self.tmp_gallery, "output-archived")
        patcher = mock.patch.object(config, "ARCHIVE_DIR", arch)
        patcher.start()
        self.addCleanup(patcher.stop)
        tc = self.make_with_roots()
        os.makedirs(arch, exist_ok=True)
        with open(os.path.join(arch, "a.png"), "wb") as f:
            f.write(real_png_blob(qwen_graph()))
        self.assertEqual(self.thumb(tc).status_code, 404, "正常档看不见归档里的图")
        r = tc.get("/api/gallery/thumb",
                   params={"root": 0, "name": "a.png", "archived": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-thumb"], "jpeg")

    def test_bad_parameters_answer_like_the_image_route(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.put_file("secret.txt")
        self.assertEqual(self.thumb(tc, root="abc").status_code, 400)
        self.assertEqual(self.thumb(tc, root=99).status_code, 404)
        self.assertEqual(self.thumb(tc, name="nope.png").status_code, 404)
        for bad in ("../secret.txt", "a/../../secret.txt", "/etc/hosts"):
            with self.subTest(bad=bad):
                r = self.thumb(tc, name=bad)
                self.assertIn(r.status_code, (400, 404))
                self.assertNotIn(b"do not delete me", r.content)

    def size_of(self, name):
        return os.path.getsize(os.path.join(self.roots()[0]["path"], name))


class GalleryDelete(GalleryFixture):
    """`POST /api/gallery/delete` —— 破坏性路径，判据一律落在**磁盘**上。

    为什么把 `trash._load` 换成 `os.remove`（而不是把 `trash.trash` 整个换成
    替身）：换成后者的话，「服务端到底有没有把**解析出来的那条路径**交给删除
    这一层」就没人验了 —— 而这正是最该验的一段。现在只换最后那一跳，
    `server → gallery.deletable → trash.trash` 全程是真跑的，文件也真的从
    临时目录里消失了；同时测试**不会**往用户的回收站里丢东西。
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(trash, "_load", lambda: os.remove)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rm(self, tc, items):
        return tc.post("/api/gallery/delete", json={"items": items})

    def path_of(self, name):
        return os.path.join(self.roots()[0]["path"], name)

    def test_deletes_the_files_and_reports_what_it_freed(self):
        tc = self.make_with_roots()
        self.put_png("a.png", qwen_graph(seed=1))
        self.put_png("b.png", qwen_graph(seed=2))
        sizes = os.path.getsize(self.path_of("a.png")) + \
            os.path.getsize(self.path_of("b.png"))
        r = self.rm(tc, [{"root": 0, "name": "a.png"}, {"root": 0, "name": "b.png"}])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["failed"], [])
        self.assertEqual(data["bytes"], sizes)
        # 判据在磁盘上：两个都真的没了
        self.assertFalse(os.path.exists(self.path_of("a.png")))
        self.assertFalse(os.path.exists(self.path_of("b.png")))
        self.assertEqual(tc.get("/api/gallery").json()["scanned"], 0)

    def test_a_bad_item_does_not_stop_the_batch(self):
        """12 张里 1 张找不到，另外 11 张该删还得删 —— 逐项降级，不是整批回滚。"""
        tc = self.make_with_roots()
        self.put_png("good.png")
        # 中间产物**真的存在**（那才是它危险的形态：ComfyUI 正在往里写）
        self.put_png("_temp_00001_.png")
        r = self.rm(tc, [
            {"root": 0, "name": "good.png"},
            {"root": 99, "name": "a.png"},          # 根越界
            {"root": 0, "name": "nope.png"},        # 不在了
            {"root": 0, "name": "../escape.png"},   # 路径穿越
            {"root": 0, "name": "_temp_00001_.png"},  # 正在写的中间产物
        ])
        data = r.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual([t["name"] for t in data["trashed"]], ["good.png"])
        why = [f["why"] for f in data["failed"]]
        self.assertEqual(len(why), 4)
        self.assertIn("root 越界", why)
        self.assertIn("图片不在了", why)
        self.assertIn("非法的文件名", why)
        self.assertTrue(any("中间产物" in w for w in why), why)
        self.assertFalse(os.path.exists(self.path_of("good.png")))

    def test_only_png_is_deletable(self):
        """名单是客户端给的，所以扩展名必须自己挡 —— 同目录里还有别的文件。"""
        tc = self.make_with_roots()
        self.put_file("secret.txt", "do not delete me")
        self.put_png("a.png")
        data = self.rm(tc, [{"root": 0, "name": "secret.txt"}]).json()
        self.assertEqual(data["count"], 0)
        self.assertIn("不是 PNG", data["failed"][0]["why"])
        self.assertTrue(os.path.exists(self.path_of("secret.txt")))

    def test_the_root_index_picks_which_directory(self):
        """同名的图在两个目录里都有 —— 删的一定是点名的那一个。"""
        one = self.roots()
        two_dir = os.path.join(self.tmp_gallery, "output-2")
        os.makedirs(two_dir, exist_ok=True)
        two = [{"path": two_dir, "label": "另一个目录", "kind": "test", "exists": True}]
        roots = one + two
        for r in roots:
            with open(os.path.join(r["path"], "same.png"), "wb") as f:
                f.write(make_png_blob(qwen_graph()))
        tc = self.make_with_roots(roots=roots)
        data = self.rm(tc, [{"root": 1, "name": "same.png"}]).json()
        self.assertEqual(data["count"], 1)
        self.assertFalse(os.path.exists(os.path.join(two_dir, "same.png")))
        self.assertTrue(os.path.exists(os.path.join(one[0]["path"], "same.png")))

    def test_a_malformed_root_index_deletes_nothing(self):
        """`int(3.7)` 会悄悄截成 3 —— 那意味着畸形请求去删另一个目录的同名图。"""
        tc = self.make_with_roots()
        self.put_png("a.png")
        data = self.rm(tc, [{"root": 3.7, "name": "a.png"},
                            {"root": True, "name": "a.png"},
                            {"root": None, "name": "a.png"}]).json()
        self.assertEqual(data["count"], 0)
        self.assertEqual([f["why"] for f in data["failed"]],
                         ["root 必须是整数"] * 3)
        self.assertTrue(os.path.exists(self.path_of("a.png")))

    def test_a_missing_package_refuses_the_whole_batch(self):
        """装不上 send2trash 时**一张都不许删** —— 绝不退回硬删。"""
        tc = self.make_with_roots()
        self.put_png("a.png")

        def nope():
            raise trash.TrashError("没有装 send2trash（测试造的），一张都没删。"
                                   "装它：pip install send2trash", "unavailable")
        with mock.patch.object(trash, "_load", nope):
            data = self.rm(tc, [{"root": 0, "name": "a.png"}]).json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(len(data["failed"]), 1)
        self.assertIn("send2trash", data["failed"][0]["why"])
        self.assertTrue(os.path.exists(self.path_of("a.png")))

    def test_the_gallery_says_up_front_whether_deleting_works(self):
        tc = self.make_with_roots()
        self.assertTrue(tc.get("/api/gallery").json()["can_delete"])

        def nope():
            raise trash.TrashError("没有装 send2trash", "unavailable")
        with mock.patch.object(trash, "_load", nope):
            data = tc.get("/api/gallery").json()
        self.assertFalse(data["can_delete"])
        self.assertIn("send2trash", data["delete_hint"])

    def test_structural_errors_are_400(self):
        tc = self.make_with_roots()
        self.assertEqual(tc.post("/api/gallery/delete", json={}).status_code, 400)
        self.assertEqual(
            tc.post("/api/gallery/delete", json={"items": "a.png"}).status_code, 400)
        self.assertEqual(
            tc.post("/api/gallery/delete", json={"items": []}).status_code, 400)
        self.assertEqual(
            tc.post("/api/gallery/delete", content=b"not json",
                    headers={"content-type": "application/json"}).status_code, 400)
        many = [{"root": 0, "name": "a%d.png" % i}
                for i in range(server.MAX_DELETE_ITEMS + 1)]
        r = tc.post("/api/gallery/delete", json={"items": many})
        self.assertEqual(r.status_code, 400)
        self.assertIn("最多", r.json()["error"])

    def test_a_second_delete_reports_it_is_gone_instead_of_counting_it(self):
        """回执是给用户看的账，不许比真实发生的事多。"""
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.assertEqual(self.rm(tc, [{"root": 0, "name": "a.png"}]).json()["count"], 1)
        second = self.rm(tc, [{"root": 0, "name": "a.png"}]).json()
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["bytes"], 0)
        self.assertIn("不在了", second["failed"][0]["why"])

    def test_it_leaves_a_line_in_the_log(self):
        """破坏性动作要留痕：几天后看着少掉的图，得有个东西能解释。"""
        tc = self.make_with_roots()
        self.put_png("a.png")
        with mock.patch.object(server.logger, "info") as spy:
            self.rm(tc, [{"root": 0, "name": "a.png"}])
        self.assertTrue(spy.called)
        self.assertIn("回收站", " ".join(str(a) for a in spy.call_args[0][0:1]))


class GalleryArchive(GalleryFixture):
    """`/api/gallery/archive | restore | purge`（P20h）—— 判据一律落在**磁盘**上。

    归档只认 `config.OUTPUT_DIR` 那一个目录（见 `gallery.py` 归档那一段），所以夹具
    必须把这两个常量**都**指到临时目录里：不指的话，测试会去动真的工作区 `output/`
    —— 那是用户的作品，自动化测试不许碰它。

    `trash._load` 换成 `os.remove`（同 `GalleryDelete`）：全程真跑
    （server → gallery 的路径解析 → trash.trash），文件真的从临时目录里消失，
    但测试**不会**往用户的回收站里丢东西。
    """

    def setUp(self):
        super().setUp()
        self.out = self.roots()[0]["path"]
        self.arch = os.path.join(self.tmp_gallery, "output-archived")
        for name, value in (("OUTPUT_DIR", self.out), ("ARCHIVE_DIR", self.arch)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(trash, "_load", lambda: os.remove)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apath(self, name):
        return os.path.join(self.arch, name)

    def out_path(self, name):
        return os.path.join(self.out, name)

    def archive(self, tc, items):
        return tc.post("/api/gallery/archive", json={"items": items})

    def restore(self, tc, names):
        return tc.post("/api/gallery/restore",
                       json={"items": [{"name": n} for n in names]})

    def purge(self, tc, names):
        return tc.post("/api/gallery/purge",
                       json={"items": [{"name": n} for n in names]})

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    # --- 归档 ---------------------------------------------------------------
    def test_archiving_moves_the_file_out_of_output_and_into_the_archive(self):
        tc = self.make_with_roots()
        size = os.path.getsize(self.put_png("a.png"))
        data = self.archive(tc, [{"root": 0, "name": "a.png"}]).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["failed"], [])
        self.assertEqual(data["bytes"], size)
        self.assertFalse(os.path.exists(self.out_path("a.png")))     # 离开 output/
        self.assertTrue(os.path.isfile(self.apath("a.png")))         # 进了归档
        self.assertEqual(os.path.getsize(self.apath("a.png")), size)
        self.assertEqual(tc.get("/api/gallery").json()["scanned"], 0)
        arch = tc.get("/api/gallery?archived=1").json()
        self.assertTrue(arch["archived"])
        self.assertEqual([i["filename"] for i in arch["items"]], ["a.png"])

    def test_archiving_never_touches_the_recycle_bin(self):
        """P20h 的**核心判据**：画廊那颗「删除」不再走回收站那条路。

        这一条比「文件挪到了位置」更值钱：挪对了位置、却顺手也丢进回收站，看着
        完全一样（页面说「已归档」、归档目录里也有），只有这一个读数能分开。
        """
        tc = self.make_with_roots()
        self.put_png("a.png")
        used = []
        with mock.patch.object(trash, "_load", lambda: used.append(1)):
            self.archive(tc, [{"root": 0, "name": "a.png"}])
        self.assertEqual(used, [], "归档不该碰回收站那条路")

    def test_a_bad_item_does_not_stop_the_batch(self):
        tc = self.make_with_roots()
        self.put_png("good.png")
        self.put_png("_temp_00001_.png")          # 真实存在，才是它危险的形态
        data = self.archive(tc, [
            {"root": 0, "name": "good.png"},
            {"root": 99, "name": "a.png"},          # 根越界
            {"root": 0, "name": "nope.png"},        # 不在了
            {"root": 0, "name": "../escape.png"},   # 路径穿越
            {"root": 0, "name": "_temp_00001_.png"},  # 正在写的中间产物
        ]).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual([t["name"] for t in data["archived"]], ["good.png"])
        why = [f["why"] for f in data["failed"]]
        self.assertEqual(len(why), 4)
        self.assertIn("root 越界", why)
        self.assertIn("图片不在了", why)
        self.assertIn("非法的文件名", why)
        self.assertTrue(any("中间产物" in w for w in why), why)
        self.assertFalse(os.path.exists(self.out_path("good.png")))
        self.assertTrue(os.path.isfile(self.apath("good.png")))

    def test_a_root_outside_the_workspace_is_refused(self):
        """归档只认工作区 `output/`。判据是**路径**，不是 kind —— 见 gallery.py 那段。

        同名的图在两个目录里都有时，这条同时钉住「挪的一定是点名的那一张」。
        """
        two_dir = os.path.join(self.tmp_gallery, "output-2")
        os.makedirs(two_dir, exist_ok=True)
        roots = self.roots() + [{"path": two_dir, "label": "另一个目录",
                                 "kind": "test", "exists": True}]
        tc = self.make_with_roots(roots=roots)
        for r in roots:
            with open(os.path.join(r["path"], "same.png"), "wb") as f:
                f.write(make_png_blob(qwen_graph()))
        data = self.archive(tc, [{"root": 1, "name": "same.png"},
                                 {"root": 0, "name": "same.png"}]).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["failed"]), 1)
        self.assertIn("不在归档范围", data["failed"][0]["why"])
        self.assertTrue(os.path.exists(os.path.join(two_dir, "same.png")))
        self.assertTrue(os.path.isfile(self.apath("same.png")))

    def test_a_second_archive_reports_it_is_gone_instead_of_counting_it(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.assertEqual(self.archive(tc, [{"root": 0, "name": "a.png"}]).json()["count"], 1)
        second = self.archive(tc, [{"root": 0, "name": "a.png"}]).json()
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["bytes"], 0)
        self.assertIn("不在了", second["failed"][0]["why"])

    # --- 那一档是不是另一档 -------------------------------------------------
    def test_the_gallery_says_which_stage_is_on_screen(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        normal = tc.get("/api/gallery").json()
        self.assertFalse(normal["archived"])
        self.assertIs(normal["items"][0]["archivable"], True)
        self.assertTrue(normal["can_delete"])
        self.assertTrue(tc.get("/api/gallery?archived=1").json()["archived"])

    # --- 还原 ---------------------------------------------------------------
    def test_restore_moves_it_back_byte_for_byte(self):
        tc = self.make_with_roots()
        blob = self.read(self.put_png("a.png"))
        self.archive(tc, [{"root": 0, "name": "a.png"}])
        data = self.restore(tc, ["a.png"]).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual([r["name"] for r in data["restored"]], ["a.png"])
        self.assertEqual(self.read(self.out_path("a.png")), blob)
        self.assertFalse(os.path.exists(self.apath("a.png")))
        self.assertEqual(tc.get("/api/gallery").json()["scanned"], 1)
        self.assertEqual(tc.get("/api/gallery?archived=1").json()["scanned"], 0)

    def test_restore_refuses_a_name_that_came_back(self):
        """撞名**不是异常**：ComfyUI 的下一个序号是扫现存文件名算的，归档走一张
        `…_00088.png` 之后，CLI 那条路会再写一张同名的。

        这一条同时钉住三件事：整项失败、**新那张没被覆盖**、归档那份**没丢**。
        """
        tc = self.make_with_roots()
        self.put_png("a.png", blob=b"AAA")
        self.archive(tc, [{"root": 0, "name": "a.png"}])
        self.put_png("a.png", blob=b"BBB")
        data = self.restore(tc, ["a.png"]).json()
        self.assertEqual(data["count"], 0)
        self.assertIn("已经占着这个名字", data["failed"][0]["why"])
        self.assertEqual(self.read(self.out_path("a.png")), b"BBB")
        self.assertEqual(self.read(self.apath("a.png")), b"AAA")

    # --- 彻底删除 -----------------------------------------------------------
    def test_purge_only_looks_in_the_archive(self):
        """点名 `output/` 里那张 → 失败。彻底删除**只**在归档目录里生效。"""
        tc = self.make_with_roots()
        self.put_png("keep.png")
        self.put_png("a.png")
        self.archive(tc, [{"root": 0, "name": "a.png"}])
        data = self.purge(tc, ["keep.png", "a.png"]).json()
        self.assertEqual([p["name"] for p in data["purged"]], ["a.png"])
        self.assertIn("不在归档目录里了", data["failed"][0]["why"])
        self.assertTrue(os.path.isfile(self.out_path("keep.png")))
        self.assertFalse(os.path.exists(self.apath("a.png")))

    def test_purge_without_the_package_deletes_nothing(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.archive(tc, [{"root": 0, "name": "a.png"}])

        def nope():
            raise trash.TrashError("没有装 send2trash（测试造的）", "unavailable")
        with mock.patch.object(trash, "_load", nope):
            data = self.purge(tc, ["a.png"]).json()
        self.assertEqual(data["count"], 0)
        self.assertIn("send2trash", data["failed"][0]["why"])
        self.assertTrue(os.path.isfile(self.apath("a.png")))

    # --- 形状 ---------------------------------------------------------------
    def test_structural_errors_are_400(self):
        tc = self.make_with_roots()
        many = [{"name": "a%d.png" % i}
                for i in range(server.MAX_DELETE_ITEMS + 1)]
        for path in ("/api/gallery/archive", "/api/gallery/restore",
                     "/api/gallery/purge"):
            self.assertEqual(tc.post(path, json={}).status_code, 400, path)
            self.assertEqual(tc.post(path, json={"items": "a.png"}).status_code, 400, path)
            self.assertEqual(tc.post(path, json={"items": []}).status_code, 400, path)
            self.assertEqual(tc.post(path, json={"items": many}).status_code, 400, path)

    def test_the_endpoints_leave_a_line_in_the_log(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        calls = []
        with mock.patch.object(server.logger, "info") as spy:
            self.archive(tc, [{"root": 0, "name": "a.png"}])
            self.restore(tc, ["a.png"])
            self.archive(tc, [{"root": 0, "name": "a.png"}])
            self.purge(tc, ["a.png"])
        calls = [" ".join(str(a) for a in c.args) for c in spy.call_args_list]
        self.assertTrue(any("归档" in c for c in calls), calls)
        self.assertTrue(any("还原" in c for c in calls), calls)
        self.assertTrue(any("彻底删除" in c for c in calls), calls)

    # --- 任务账 -------------------------------------------------------------
    def test_the_jobs_list_marks_an_archived_product_and_unmarks_it(self):
        """「从最近任务里移除」是**画面上的过滤**，不是把账划掉。

        两个读数缺一不可：归档后标起来、还原后**标记要跟着回来**；而且这个标记
        不许被写进 store 自己那份记录里（写进去它就会在还原之后依然说「已归档」，
        而且没有任何东西会去纠正它）。
        """
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.wb.store.create(job_id="arch-1", mode="t2i", params={},
                             prompt_text="x", workflow={})
        self.wb.store.patch("arch-1", outputs=[{"filename": "a.png",
                                                "subfolder": "", "type": "output"}])
        self.wb.store.finish("arch-1", "done")

        def flag():
            rows = {r["job_id"]: r for r in tc.get("/api/jobs").json()["jobs"]}
            return rows["arch-1"]["archived"]
        self.assertFalse(flag())
        self.archive(tc, [{"root": 0, "name": "a.png"}])
        self.assertTrue(flag())
        self.restore(tc, ["a.png"])
        self.assertFalse(flag())
        self.assertNotIn("archived", self.wb.store.get("arch-1"))


class JobsProductGone(GalleryFixture):
    """最近任务那一行画不画 + 那颗「清理」（P20j）—— 判据全在**磁盘**上。

    为什么这族测试非得用真目录：`/api/jobs` 现在给每条记录现算四个字段
    （`on_disk` / `archived` / `restorable` / `gone`），而它们全是「此刻文件在哪儿」
    的函数 —— 替身文件系统证明不了这件事。夹具沿用画廊那一套（`GalleryFixture`）：
    `config.OUTPUT_DIR` 与 `config.ARCHIVE_DIR` **都**指进临时目录，不指的话测试会去
    动用户真的工作区 `output/` 与 `output-archived/`。

    这一族的来历：2026-10-03 用户从归档页彻底删掉一张之后，最近任务里那一行
    **重新冒出来**了 —— 旧判据是「文件在不在归档目录里」，而「归档过、又被彻底删除」
    两处都不在，于是它算出「没归档」。现在三种情况（归档 / 彻底删除 / 手工搬走）
    合成同一个答案，而清理的射程比它更窄一格：**永远回不来的才清**。
    """

    def setUp(self):
        super().setUp()
        self.out = self.roots()[0]["path"]
        self.arch = os.path.join(self.tmp_gallery, "output-archived")
        for name, value in (("OUTPUT_DIR", self.out), ("ARCHIVE_DIR", self.arch)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, jid, status="done", outputs=()):
        """造一条任务记录。产物**只写在记录里** —— 磁盘上的那份由测试自己摆。"""
        self.wb.store.create(job_id=jid, mode="t2i", params={"steps": 20},
                             prompt_text="夹具 " + jid,
                             workflow={"461": {"class_type": "SaveImage"}})
        self.wb.store.patch(jid, status=status, outputs=list(outputs))
        return self.wb.store.get(jid)

    def rows(self, tc):
        return {j["job_id"]: j for j in tc.get("/api/jobs?limit=100").json()["jobs"]}

    def archive(self, tc, names):
        return tc.post("/api/gallery/archive",
                       json={"items": [{"root": 0, "name": n} for n in names]}).json()

    def stale(self, tc):
        return tc.get("/api/jobs").json()["stale"]

    # --- 判据本身 -----------------------------------------------------------
    def test_a_product_that_is_gone_makes_the_row_disappear_and_be_counted(self):
        tc = self.make_with_roots()
        self.seed("purged", outputs=[{"filename": "gone.png"}])
        data = tc.get("/api/jobs").json()
        row = data["jobs"][0]
        self.assertIs(row["gone"], True)        # 有产物、但一张都取不到
        self.assertIs(row["on_disk"], False)
        self.assertIs(row["restorable"], False)
        self.assertEqual(data["stale"], 1)

    def test_a_product_still_in_output_is_not_gone(self):
        tc = self.make_with_roots()
        self.put_png("live.png")
        self.seed("live", outputs=[{"filename": "live.png"}])
        row = self.rows(tc)["live"]
        self.assertIs(row["gone"], False)
        self.assertIs(row["on_disk"], True)
        self.assertEqual(self.stale(tc), 0)

    def test_an_archived_product_is_hidden_but_never_cleaned(self):
        """归档里的：**不画**（取不到图），但**不清** —— 免死金牌。

        还原回 `output/` 之后那一行必须带着耗时与参数回来（P20h 第④条），
        所以「还能还原」是清理的排除条件。这一格要是不挡，用户还原时就会发现
        那一行永远回不来了。
        """
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.seed("arc", outputs=[{"filename": "a.png"}])
        self.archive(tc, ["a.png"])
        row = self.rows(tc)["arc"]
        self.assertIs(row["gone"], True)
        self.assertIs(row["archived"], True)
        self.assertIs(row["restorable"], True)
        self.assertEqual(self.stale(tc), 0)
        self.assertEqual(tc.post("/api/jobs/clean", json={}).json()["count"], 0)
        self.assertIsNotNone(self.wb.store.get("arc"))

    def test_a_failed_row_without_a_product_is_never_gone(self):
        """失败必须看得见 —— 没有产物的记录不是「无效记录」（把它藏起来最不能接受）。"""
        tc = self.make_with_roots()
        self.seed("failed", status="error", outputs=[])
        self.seed("queued", status="queued", outputs=[])
        rows = self.rows(tc)
        self.assertIs(rows["failed"]["gone"], False)
        self.assertIs(rows["queued"]["gone"], False)
        self.assertEqual(self.stale(tc), 0)

    # --- 那一下点击 ---------------------------------------------------------
    def test_cleaning_removes_only_the_unrecoverable_ones(self):
        tc = self.make_with_roots()
        self.put_png("live.png")
        self.put_png("a.png")
        self.seed("live", outputs=[{"filename": "live.png"}])
        self.seed("purged", outputs=[{"filename": "gone.png"}])
        self.seed("missing", outputs=[{"filename": "nowhere.png"}])
        self.seed("failed", status="error", outputs=[])
        self.seed("arc", outputs=[{"filename": "a.png"}])
        self.archive(tc, ["a.png"])
        self.assertEqual(self.stale(tc), 2)

        got = tc.post("/api/jobs/clean", json={}).json()

        self.assertEqual(got["count"], 2)
        self.assertEqual(sorted(got["removed"]), ["missing", "purged"])
        # 记录真的从内存与磁盘上没了；没在名单里的四条一条不少
        self.assertIsNone(self.wb.store.get("purged"))
        self.assertFalse(os.path.exists(self.wb.store.path("purged")))
        self.assertEqual(sorted(self.rows(tc)), ["arc", "failed", "live"])
        # **产物一张没动**：还在 output/ 的那张、还在归档目录里的那张
        self.assertTrue(os.path.isfile(os.path.join(self.out, "live.png")))
        self.assertTrue(os.path.isfile(os.path.join(self.arch, "a.png")))
        # 幂等：再点一次是 0，而不是把剩下的一起卷走
        self.assertEqual(tc.post("/api/jobs/clean", json={}).json()["count"], 0)
        self.assertEqual(self.stale(tc), 0)

    def test_cleaning_never_touches_a_running_job(self):
        """在跑的绝不动 —— 哪怕它的产物此刻也取不到（它还可能写完/被收尾）。"""
        tc = self.make_with_roots()
        self.seed("busy", status="running", outputs=[{"filename": "busy.png"}])
        self.assertIs(self.rows(tc)["busy"]["gone"], True)     # 判据说取不到
        self.assertEqual(self.stale(tc), 0)                    # 但不算可清理
        self.assertEqual(tc.post("/api/jobs/clean", json={}).json()["count"], 0)
        self.assertIsNotNone(self.wb.store.get("busy"))

    def test_cleaning_does_not_rewrite_the_run_ledger(self):
        """`runs.jsonl` 是历史账（画廊按文件名跟它对账）—— 清理一行都不动它。"""
        tc = self.make_with_roots()
        self.seed("gone-1", status="running", outputs=[{"filename": "x.png"}])
        self.wb.store.finish("gone-1", "done")
        runs = os.path.join(self.tmp, "runs.jsonl")
        with open(runs, encoding="utf-8") as f:
            before = f.read()
        self.assertEqual(tc.post("/api/jobs/clean", json={}).json()["count"], 1)
        with open(runs, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_restoring_the_image_brings_the_row_back_with_its_params(self):
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.seed("arc", outputs=[{"filename": "a.png"}])
        self.archive(tc, ["a.png"])
        self.assertIs(self.rows(tc)["arc"]["gone"], True)

        tc.post("/api/gallery/restore", json={"items": [{"name": "a.png"}]})

        row = self.rows(tc)["arc"]
        self.assertIs(row["gone"], False)
        self.assertEqual(row["params"]["steps"], 20)

    def test_the_verdict_is_computed_per_request_and_never_stored(self):
        """现算的字段**不许被写进 store 自己那份记录**（写进去它就永远说「已归档」）。"""
        tc = self.make_with_roots()
        self.put_png("a.png")
        self.seed("arc", outputs=[{"filename": "a.png"}])
        self.archive(tc, ["a.png"])
        self.rows(tc)
        stored = self.wb.store.get("arc")
        for key in ("gone", "on_disk", "restorable"):
            self.assertNotIn(key, stored)


class PromptRoutes(ApiBase):
    def test_starts_empty(self):
        tc = self.make()
        data = tc.get("/api/prompts").json()
        self.assertEqual(data["prompts"], [])
        self.assertEqual(data["total"], 0)

    def test_create_list_and_fetch(self):
        tc = self.make()
        r = tc.post("/api/prompts", json={"text": "a green apple",
                                          "title": "苹果", "tags": ["fruit"]})
        self.assertEqual(r.status_code, 201)
        row = r.json()
        self.assertTrue(row["id"])
        listing = tc.get("/api/prompts").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["prompts"][0]["title"], "苹果")
        self.assertEqual(listing["tags"], [{"tag": "fruit", "count": 1}])

    def test_empty_text_is_400(self):
        tc = self.make()
        self.assertEqual(tc.post("/api/prompts", json={"text": "  "}).status_code, 400)
        self.assertEqual(tc.post("/api/prompts", json={}).status_code, 400)

    def test_non_json_body_is_400(self):
        tc = self.make()
        self.assertEqual(tc.post("/api/prompts", content=b"nope",
                                 headers={"Content-Type": "application/json"}
                                 ).status_code, 400)

    def test_update(self):
        tc = self.make()
        pid = tc.post("/api/prompts", json={"text": "old"}).json()["id"]
        r = tc.put("/api/prompts/" + pid, json={"text": "new", "tags": ["x"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "new")
        self.assertEqual(r.json()["tags"], ["x"])

    def test_update_unknown_is_404(self):
        tc = self.make()
        self.assertEqual(tc.put("/api/prompts/nope", json={"text": "x"}).status_code, 404)

    def test_delete(self):
        tc = self.make()
        pid = tc.post("/api/prompts", json={"text": "x"}).json()["id"]
        self.assertEqual(tc.delete("/api/prompts/" + pid).status_code, 204)
        self.assertEqual(tc.get("/api/prompts").json()["total"], 0)
        self.assertEqual(tc.delete("/api/prompts/" + pid).status_code, 404)

    def test_touch_increments_uses(self):
        tc = self.make()
        pid = tc.post("/api/prompts", json={"text": "x"}).json()["id"]
        r = tc.post("/api/prompts/%s/use" % pid, json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["uses"], 1)
        self.assertEqual(tc.post("/api/prompts/nope/use", json={}).status_code, 404)

    def test_search_and_tag_filter(self):
        tc = self.make()
        tc.post("/api/prompts", json={"text": "green apple", "tags": ["fruit"]})
        tc.post("/api/prompts", json={"text": "red fox", "tags": ["animal"]})
        self.assertEqual(len(tc.get("/api/prompts?q=green").json()["prompts"]), 1)
        self.assertEqual(len(tc.get("/api/prompts?tag=animal").json()["prompts"]), 1)

    def test_survives_a_server_restart(self):
        """判据 3 的「重启服务后画廊仍在」—— 提示词库同理，都是落盘的。"""
        tc = self.make()
        state_dir = self.wb.state_dir
        tc.post("/api/prompts", json={"text": "persisted", "title": "t"})

        again = server.Workbench(state_dir=state_dir, client=support.FakeComfy())
        fresh = TestClient(server.create_app(again))
        rows = fresh.get("/api/prompts").json()["prompts"]
        self.assertEqual([r["text"] for r in rows], ["persisted"])


class BackendCarriesOutputRoots(ApiBase):
    def test_output_roots_are_reported(self):
        """输出目录不止一个（Desktop / 手动各一个），页面要能解释这一点。"""
        data = self.make().get("/api/backend").json()
        self.assertIn("output_roots", data)
        self.assertIsInstance(data["output_roots"], list)
        for root in data["output_roots"]:
            self.assertIn("path", root)
            self.assertIn("label", root)
            self.assertIn("exists", root)


class TimingsRoutes(ApiBase):
    """`/api/jobs/{id}/timings`（判据 4）。

    探针报告用 patch 换掉 —— 否则断言会依赖这台机器上碰巧采到过哪几次。
    **真实的**报告另有一条 `RealReport` 在 `test_timings.py` 里做只读冒烟。
    """

    def submit(self):
        return self.make().post("/api/run", json=self.build_body()).json()

    def patch_report(self, report):
        patcher = mock.patch.object(timings, "load_report", return_value=report)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_the_summary_for_a_known_prompt(self):
        from test_timings import REAL_BLOCK
        data = self.submit()
        self.patch_report({data["job_id"]: REAL_BLOCK})
        out = self.tc.get("/api/jobs/%s/timings" % data["job_id"]).json()
        self.assertTrue(out["available"])
        self.assertAlmostEqual(out["coverage_pct"], 99.93, delta=0.5)
        self.assertEqual(out["rows"][0]["class_type"], "KSampler")
        self.assertEqual(out["sampling"]["steps"], 20)
        self.assertAlmostEqual(out["sampling"]["per_step_ms"], 2271.57, delta=1.0)

    def test_missing_probe_data_is_not_a_404(self):
        """「后端没带探针」是正常状态，不是「任务不存在」—— 页面要照实说
        没有数据，而不是弹一个错误。"""
        data = self.submit()
        self.patch_report({})
        r = self.tc.get("/api/jobs/%s/timings" % data["job_id"])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["available"])
        self.assertIn("没有这次任务", r.json()["reason"])

    def test_unknown_job_is_404(self):
        self.make()
        self.assertEqual(self.tc.get("/api/jobs/nope/timings").status_code, 404)

    def test_backend_reports_probe_state(self):
        data = self.make().get("/api/backend").json()
        self.assertIn("probe", data)
        for key in ("known", "alive", "report"):
            self.assertIn(key, data["probe"])


class Static(ApiBase):
    def test_index_and_assets(self):
        tc = self.make()
        r = tc.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("工作台", r.text)
        for asset in ("/static/app.js", "/static/style.css"):
            self.assertEqual(tc.get(asset).status_code, 200, asset)

    def test_assets_may_not_be_reused_without_asking(self):
        """静态文件必须**回源校验**（P20k）—— 页面的两条腿不能来自不同的版本。

        HTML 那条路是 `no-store`，而本机 starlette 1.7 的 `StaticFiles` 已经不发
        任何缓存指令了（实测只有 ETag / Last-Modified）。两边各按各的规矩来，
        浏览器就可能拿**上一版**的 `app.js` 配**这一版**的 HTML —— 那个组合会让
        `init()` 在第一行就抛，整页像冻住一样且**没有任何提示**
        （2026-10-03 用户报的就是它，`logs/ui_hybrid_probe.py` 复现了两种坏法）。
        """
        tc = self.make()
        for asset in ("/static/app.js", "/static/style.css"):
            with self.subTest(asset=asset):
                head = tc.get(asset).headers
                self.assertEqual(head.get("cache-control"), "no-cache")
        # HTML 不许被缓存，一个字都别改回去
        self.assertEqual(tc.get("/").headers.get("cache-control"), "no-store")


UNSET = object()      # 「用目录里的文件」与「后端读不到候选表」必须能分开表达


class InputsBase(ApiBase):
    """参考图相关的路由都要一个**真的** input 目录 —— 对账是按文件名比的。"""

    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.srcdir = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-src-")
        self.addCleanup(shutil.rmtree, self.srcdir, ignore_errors=True)

    def put(self, name, body=b"\x89PNG\r\n\x1a\n"):
        with open(os.path.join(self.srcdir, name), "wb") as f:
            f.write(body)
        return name

    def make_inputs(self, files=(), options=UNSET, running=True):
        """`options` 省略 = 后端「读到」的正好是这个目录（对账应当成功）；
        传 None = 后端读不到候选表（模拟后端没起 / object_info 拿不到）。"""
        for n in files:
            self.put(n)
        return self.make(
            running=running, input_dir=self.srcdir,
            input_options=list(files) if options is UNSET else options,
            argv=["main.py", "--input-directory", self.srcdir])


class InputsRoutes(InputsBase):
    def test_lists_what_the_backend_can_read(self):
        tc = self.make_inputs(files=("a.png", "b.png"))
        r = tc.get("/api/inputs")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["files"], ["a.png", "b.png"])
        self.assertTrue(data["verified"])
        self.assertEqual(os.path.normcase(data["root"]), os.path.normcase(self.srcdir))

    def test_backend_down_still_lists_the_directory_but_unverified(self):
        tc = self.make_inputs(files=("a.png",))
        self.client.running_flag = False
        self.client.input_options = None
        data = tc.get("/api/inputs").json()
        self.assertEqual(data["files"], ["a.png"])
        self.assertFalse(data["verified"])

    def test_exposes_the_candidate_dirs_for_diagnosis(self):
        """两个 input 目录这件事必须能在页面上看出来，否则写错目录查不动。"""
        tc = self.make_inputs(files=("a.png",))
        data = tc.get("/api/inputs").json()
        self.assertGreaterEqual(len(data["candidates"]), 1)
        self.assertTrue(all("path" in c for c in data["candidates"]))


class UploadRoutes(InputsBase):
    def test_new_file_is_201_and_reported_unverified_until_the_backend_sees_it(self):
        tc = self.make_inputs(files=())
        r = tc.post("/api/upload?filename=new.png", content=b"\x89PNG\r\n\x1a\n")
        self.assertEqual(r.status_code, 201, r.text)
        data = r.json()
        self.assertEqual(data["filename"], "new.png")
        self.assertTrue(os.path.isfile(os.path.join(self.srcdir, "new.png")))
        # 后端候选表还没更新，所以**不能**说成功
        self.assertFalse(data["verified"])
        self.assertIn("候选表", data["warning"])

    def test_verified_once_the_backend_table_shows_it(self):
        tc = self.make_inputs(files=())
        self.client.input_options = ["new.png"]      # 模拟后端已经能读到
        data = tc.post("/api/upload?filename=new.png",
                       content=b"\x89PNG\r\n\x1a\n").json()
        self.assertTrue(data["verified"])
        self.assertIsNone(data["warning"])

    def test_replacing_an_existing_name_is_200_not_201(self):
        tc = self.make_inputs(files=("a.png",), options=["a.png"])
        r = tc.post("/api/upload?filename=a.png", content=b"new-bytes")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["replaced"])

    def test_overwrite_can_be_refused(self):
        tc = self.make_inputs(files=("a.png",))
        r = tc.post("/api/upload?filename=a.png&overwrite=0", content=b"x")
        self.assertEqual(r.status_code, 400)
        self.assertIn("同名", r.json()["error"])

    def test_bad_extension_is_400(self):
        tc = self.make_inputs(files=())
        r = tc.post("/api/upload?filename=notes.txt", content=b"hello")
        self.assertEqual(r.status_code, 400)
        self.assertIn("不是图片后缀", r.json()["error"])

    def test_traversal_is_400_and_writes_nothing(self):
        tc = self.make_inputs(files=())
        r = tc.post("/api/upload?filename=..%2F..%2Fevil.png", content=b"x")
        # 被剥成 evil.png 是允许的；关键是**不能**跑到上级目录
        self.assertIn(r.status_code, (201, 400))
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(self.srcdir), "evil.png")))
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(os.path.dirname(self.srcdir)), "evil.png")))

    def test_filename_from_the_header_also_works(self):
        tc = self.make_inputs(files=())
        r = tc.post("/api/upload", content=b"\x89PNG\r\n\x1a\n",
                    headers={"X-Filename": "hdr.png"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["filename"], "hdr.png")

    def test_missing_filename_is_400(self):
        tc = self.make_inputs(files=())
        r = tc.post("/api/upload", content=b"\x89PNG\r\n\x1a\n")
        self.assertEqual(r.status_code, 400)
        self.assertIn("缺少文件名", r.json()["error"])

    def test_empty_body_is_400(self):
        tc = self.make_inputs(files=())
        r = tc.post("/api/upload?filename=x.png", content=b"")
        self.assertEqual(r.status_code, 400)
        self.assertIn("空文件", r.json()["error"])

    def test_oversize_is_413_without_writing(self):
        tc = self.make_inputs(files=())
        with mock.patch.object(upload, "MAX_BYTES", 8):
            r = tc.post("/api/upload?filename=big.png", content=b"x" * 64)
        self.assertEqual(r.status_code, 413)
        self.assertIn("文件太大", r.json()["error"])
        self.assertFalse(os.path.exists(os.path.join(self.srcdir, "big.png")))

    def test_uploaded_file_becomes_a_valid_edit_source(self):
        """这一期的真正闭环：传上去 → 后端能读到 → 图生图不再报「没有这个文件」。"""
        tc = self.make_inputs(files=())
        self.client.input_options = ["ref.png"]
        tc.post("/api/upload?filename=ref.png", content=b"\x89PNG\r\n\x1a\n")
        body = self.build_body(mode="edit", source_image="ref.png", resolution=1024)
        data = tc.post("/api/build", json=body).json()
        levels = {w["level"] for w in data["warnings"] if "ref.png" in w["text"]}
        self.assertEqual(levels, {"info"})
        self.assertEqual(tc.post("/api/run", json=body).status_code, 200)


class UploadGoesThroughTheBackend(InputsBase):
    """上传的**首选路径**是请 ComfyUI 自己落盘（`POST /upload/image`）。

    为什么这件事值得单列一组：工作台跑在**工作区**解释器上，而本机那条沙箱
    规则只按**可执行文件位置**判定 —— 实测同一个目录，工作区解释器
    `PermissionError`、ComfyUI 自己的解释器写得进去。所以「自己写文件」这条路
    在工作台上天然走不通，用户看到的就是「只能填已有的文件名，选文件传不上去」。
    把写盘挪进后端进程才是修法，不是给本地写加重试。
    """

    def test_the_json_says_the_backend_did_the_write(self):
        tc = self.make_inputs(files=())
        data = tc.post("/api/upload?filename=new.png",
                       content=b"\x89PNG\r\n\x1a\n").json()
        self.assertEqual(data["via"], "backend")
        # **不报落点**是有意的：文件落在 ComfyUI 进程的目录里，我们只有一个
        # 文件名。猜一个目录填进去，就等于制造这个模块一直在消灭的假自信。
        self.assertIsNone(data["root"])
        self.assertIsNone(data["path"])
        self.assertEqual(data["filename"], "new.png")

    def test_it_asks_comfyui_rather_than_writing_itself(self):
        tc = self.make_inputs(files=())
        tc.post("/api/upload?filename=new.png", content=b"\x89PNG\r\n\x1a\n")
        # 替身的 upload_image 是「后端收到了这次上传」的唯一证据
        self.assertEqual([n for n, _ in self.client.uploaded], ["new.png"])
        # 而**没有**走本地写那条路（那条会报出落点 `root`/`root_label`）
        data = tc.post("/api/upload?filename=other.png", content=b"x").json()
        self.assertNotIn("root_label", data)
        self.assertNotIn("how", data)

    def test_a_rename_by_the_backend_is_reported_and_used(self):
        """候选表**陈旧**时（文件其实在磁盘上、但表里还没有），`overwrite=0`
        会走到后端那条路，而 ComfyUI 的处理是**改名**（`a (1).png`）而不是拒绝。

        回执里的名字与我们递进去的不一样 —— 这必须被说出来，而且必须用**后端
        那个名字**。用户以为传的是 `a.png`，而图里引用的会是 `a (1).png`。
        """
        tc = self.make_inputs(files=("a.png",), options=[])
        data = tc.post("/api/upload?filename=a.png&overwrite=0",
                       content=b"x").json()
        self.assertEqual(data["filename"], "a (1).png")
        self.assertEqual(data["renamed_from"], "a.png")
        self.assertEqual(data["via"], "backend")

    def test_inputs_expose_the_reference_cap(self):
        """一次能挂几张是**图**的约束，跟着 /api/inputs 一起交给页面，
        让它在选第 17 张之前就拦住，而不是等提交后收一句 400。"""
        tc = self.make_inputs(files=())
        data = tc.get("/api/inputs").json()
        self.assertEqual(data["max_references"], qb.MAX_REFERENCE_IMAGES)


class MultiReferenceImages(InputsBase):
    """多张参考图：表单两种写法、每张都要核、上限要有来处。"""

    def two(self, **over):
        return self.make_inputs(files=("a.png", "b.png"),
                                options=["a.png", "b.png"])

    def test_the_list_flows_into_the_graph(self):
        tc = self.two()
        body = self.build_body(mode="edit", source_images=["a.png", "b.png"],
                               resolution=1024)
        data = tc.post("/api/build", json=body).json()
        classes = data["node_classes"]
        self.assertEqual(classes["900"], "LoadImage")
        self.assertEqual(classes["901"], "LoadImage")

    def test_every_reference_is_checked_not_just_the_first(self):
        """只核第一张的话，第二张不在 input/ 里会变成一次 111 秒后的失败 ——
        而「提交前就说清」正是这一层存在的全部理由。"""
        tc = self.make_inputs(files=("a.png",), options=["a.png"])
        body = self.build_body(mode="edit", source_images=["a.png", "gone.png"],
                               resolution=1024)
        data = tc.post("/api/build", json=body).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(errs, data["warnings"])
        self.assertIn("没有 `gone.png`", errs[0]["text"])
        # 在的那张不许被说成不在（那会把一次「只缺一张」误导成「两张都没了」）
        self.assertNotIn("没有 `a.png`", errs[0]["text"])

    def test_several_references_get_a_warning_about_the_unknown_cost(self):
        """多张的耗时**没量过**。估时是按「一张参考图」那批实测锚点算的 （而且
        2026-10-01 起页面上不再显示估时了）—— 不说出来，用户就会以为那个数字
        还是准的。"""
        tc = self.two()
        body = self.build_body(mode="edit", source_images=["a.png", "b.png"],
                               resolution=1024)
        data = tc.post("/api/build", json=body).json()
        texts = [w["text"] for w in data["warnings"]]
        self.assertTrue(any("2 张参考图" in t for t in texts), texts)

    def test_one_reference_gets_no_such_warning(self):
        tc = self.two()
        body = self.build_body(mode="edit", source_image="a.png",
                               resolution=1024)
        data = tc.post("/api/build", json=body).json()
        self.assertFalse(any("张参考图" in w["text"] for w in data["warnings"]))

    def test_a_single_reference_still_works_the_old_way(self):
        """老客户端只发 `source_image` —— 那条路必须一个字节都不变。"""
        tc = self.two()
        body = self.build_body(mode="edit", source_image="a.png",
                               resolution=1024)
        data = tc.post("/api/build", json=body).json()
        loads = sorted(k for k, v in data["node_classes"].items()
                       if v == "LoadImage")
        self.assertEqual(loads, ["900"])

    def test_two_keys_that_disagree_are_a_400(self):
        tc = self.two()
        body = self.build_body(mode="edit", source_image="a.png",
                               source_images=["b.png", "a.png"],
                               resolution=1024)
        r = tc.post("/api/build", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("不一致", r.json()["error"])

    def test_a_non_list_is_a_400(self):
        tc = self.two()
        body = self.build_body(mode="edit", source_images="a.png",
                               resolution=1024)
        r = tc.post("/api/build", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("source_images", r.json()["error"])

    def test_the_cap_is_enforced_with_its_reason(self):
        tc = self.two()
        too_many = ["r%d.png" % i for i in range(qb.MAX_REFERENCE_IMAGES + 1)]
        body = self.build_body(mode="edit", source_images=too_many,
                               resolution=1024)
        r = tc.post("/api/build", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("image_%d" % qb.MAX_REFERENCE_IMAGES, r.json()["error"])

    def test_the_run_path_carries_the_list_too(self):
        """扫描与直接出图共用 `_submit_one` —— 那里也必须走同一条解析。"""
        tc = self.two()
        body = self.build_body(mode="edit", source_images=["a.png", "b.png"],
                               resolution=1024)
        r = tc.post("/api/run", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        rec = self.wb.store.get(r.json()["job_id"])
        self.assertEqual(rec["params"]["source_images"], ["a.png", "b.png"])
        self.assertEqual(rec["params"]["source_image"], "a.png")



class EditSourcePreflight(InputsBase):
    """图生图的参考图在不在 input/ 里 —— 提交前就该说清，别等 111 秒白跑。"""

    def test_missing_source_is_an_error_level_warning(self):
        tc = self.make_inputs(files=("a.png",), options=["a.png"])
        body = self.build_body(mode="edit", source_image="nope.png", resolution=1024)
        data = tc.post("/api/build", json=body).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(errs, data["warnings"])
        self.assertIn("nope.png", errs[0]["text"])
        self.assertIn("a.png", errs[0]["text"])     # 顺手告诉用户现在有哪些

    def test_present_source_is_just_information(self):
        tc = self.make_inputs(files=("ref.png",), options=["ref.png"])
        body = self.build_body(mode="edit", source_image="ref.png", resolution=1024)
        data = tc.post("/api/build", json=body).json()
        refs = [w for w in data["warnings"] if "ref.png" in w["text"]]
        self.assertEqual([w["level"] for w in refs], ["info"])

    def test_no_source_at_all_is_a_400_from_the_builder(self):
        """「没给参考图」由构建器当场打回（400），**到不了**预警那一层。

        刻意让这两件事各归各位：缺参考图是**参数错**（构建器管，硬失败），
        「参考图不在 input/ 里」是**环境错**（预警管，能提前说）。别混成一条。
        """
        tc = self.make_inputs(files=("a.png",))
        body = self.build_body(mode="edit", source_image=None, resolution=1024)
        r = tc.post("/api/build", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("source_image", r.json()["error"])

    def test_unreadable_input_list_warns_instead_of_claiming_absence(self):
        self.make_inputs(files=(), options=None)
        self.client.input_options = None
        body = self.build_body(mode="edit", source_image="x.png", resolution=1024)
        data = self.tc.post("/api/build", json=body).json()
        self.assertTrue(any("读不到后端 input/" in w["text"] for w in data["warnings"]),
                        data["warnings"])
        # 关键是**不能**报成「没有这个文件」—— 不知道就说不知道
        self.assertFalse(any("提交必然失败" in w["text"] for w in data["warnings"]))

    def test_run_also_reports_the_preflight(self):
        tc = self.make_inputs(files=(), options=[])
        body = self.build_body(mode="edit", source_image="gone.png", resolution=1024)
        data = tc.post("/api/run", json=body).json()
        self.assertTrue(any("gone.png" in w["text"] for w in data["warnings"]))

    def test_other_modes_never_mention_the_reference_image(self):
        tc = self.make_inputs(files=("a.png",))
        data = tc.post("/api/build", json=self.build_body(mode="t2i")).json()
        self.assertFalse(any("参考图" in w["text"] for w in data["warnings"]))


class UpscaleMode(InputsBase):
    """放大（VOSR2）—— 它和出图那四种**不是一回事**，所以契约另有一套：
    不要提示词、要源图、要插件、要权重。

    每一条都对应一种真实会发生的事：源图不在 input/ 里（ComfyUI 校验阶段打回）、
    插件没装（节点类不存在）、权重没下（bundle 候选表是空的）。
    """

    NODE = "TESpeedVOSR2Image"
    LOADER = "TESpeedVOSR2Loader"
    BUNDLE = "VOSR2"

    def up(self, **over):
        body = {"mode": "upscale", "source_image": "a.png", "scale": 2, "seed": 42}
        body.update(over)
        return body

    def png(self, name, w, h):
        """造一张**带真 IHDR** 的图 —— 放大得读它才知道输出多大。

        不能复用 `make_png_blob`：那个只写 tEXt + IEND，没有 IHDR，
        `pngmeta.png_size` 会（正确地）读不到尺寸。
        """
        head = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", 13, b"IHDR")
                + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
                + b"\x00\x00\x00\x00")
        return self.put(name, head)

    def ready(self, files=(("a.png", 1024, 1024),), nodes=True, bundles=True, **kw):
        """一个「插件装了、权重也在、input 里有源图」的后端。

        `bundles` 三态（这三种在真后端里是**三件不同的事**，别混）：
          True    = 候选表里有 VOSR2            → 权重在
          []      = 候选表是**空的**             → 权重没下（error）
          False   = 根本没有这个字段/问不到它    → 读不到清单（warn，不是 error）
        """
        for name, w, h in files:
            self.png(name, w, h)
        names = [f[0] for f in files]
        if bundles is True:
            combos = {self.LOADER: {"model_bundle": [self.BUNDLE]}}
        elif bundles is False:
            combos = {}
        else:
            combos = {self.LOADER: {"model_bundle": list(bundles)}}
        return self.make(
            running=kw.pop("running", True),
            input_dir=self.srcdir,
            input_options=names,
            argv=["main.py", "--input-directory", self.srcdir],
            node_classes=kw.pop("node_classes",
                                (self.NODE, self.LOADER) if nodes else ()),
            combos=kw.pop("combos", combos),
            **kw)

    # --- 提示词：放大**没有**这个东西 -------------------------------------
    def test_does_not_need_a_prompt(self):
        """放大图的输入只有一张图 —— 原来那条「提示词不能为空」在这里是错的。"""
        data = self.ready().post("/api/build", json=self.up()).json()
        self.assertEqual(data["describe"]["mode"], "upscale")
        self.assertEqual(data["describe"]["prompt"], "")
        self.assertIsNone(data["describe"]["steps"])

    def test_a_prompt_is_ignored_not_rejected(self):
        """带上提示词也不该报错 —— 只是它不起作用。"""
        data = self.ready().post("/api/build", json=self.up(prompt="随便写点")).json()
        self.assertEqual(data["describe"]["mode"], "upscale")

    def test_it_needs_a_source(self):
        tc = self.ready()
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                r = tc.post("/api/build", json=self.up(source_image=bad))
                self.assertEqual(r.status_code, 400)
                self.assertIn("源图", r.json()["error"])

    def test_scale_is_checked(self):
        tc = self.ready()
        for bad in (0, -1, 17, 2.5, True):
            with self.subTest(bad=bad):
                r = tc.post("/api/build", json=self.up(scale=bad))
                self.assertEqual(r.status_code, 400)
                self.assertIn("scale", r.json()["error"])

    # --- 输出尺寸：只能去读源图 -------------------------------------------
    def test_output_size_comes_from_the_source_png(self):
        data = self.ready(files=(("a.png", 1024, 768),)).post(
            "/api/build", json=self.up(scale=2)).json()
        self.assertEqual((data["output_size"]["width"],
                          data["output_size"]["height"]), (2048, 1536))
        self.assertIsNone(data["output_size"]["note"])
        self.assertIn("2048 × 1536", " ".join(w["text"] for w in data["warnings"]))

    def test_unreadable_source_size_is_admitted_not_guessed(self):
        """源图在 input/ 里，但不是一张读得出 IHDR 的 PNG —— 这时必须说
        「读不到尺寸」，而不是拿 1024 冒充。（估算仍会给，但要标出前提。）"""
        self.put("weird.png", b"\x89PNG\r\n\x1a\n" + b"z" * 40)
        tc = self.make(running=True, input_dir=self.srcdir,
                       input_options=["weird.png"],
                       argv=["main.py", "--input-directory", self.srcdir],
                       node_classes=(self.NODE, self.LOADER),
                       combos={self.LOADER: {"model_bundle": [self.BUNDLE]}})
        data = tc.post("/api/build", json=self.up(source_image="weird.png")).json()
        self.assertIsNone(data["output_size"]["width"])
        self.assertTrue(data["estimate"]["parts"]["assumed"])
        self.assertTrue(any("读不到" in w["text"] for w in data["warnings"]))

    def test_cost_follows_the_output_size_not_the_input(self):
        """**这条钉的是那个成本模型的核心实测结论**：512²×4 与 1024²×2 都是
        2048² 输出，耗时应当一样（实测 13.65 对 13.24~13.28 秒）。
        要是哪天有人把它改成按输入尺寸算，这条会红。"""
        a = self.ready(files=(("big.png", 1024, 1024),))
        big_in = a.post("/api/build", json=self.up(source_image="big.png",
                                                   scale=2)).json()
        b = self.ready(files=(("small.png", 512, 512),))
        small_in = b.post("/api/build", json=self.up(source_image="small.png",
                                                     scale=4)).json()
        self.assertEqual(big_in["output_size"]["width"], 2048)
        self.assertEqual(small_in["output_size"]["width"], 2048)
        self.assertEqual(big_in["estimate"]["seconds"], small_in["estimate"]["seconds"])

    def test_the_estimate_uses_the_upscale_formula(self):
        """放大的分解项是 `upscale_s`，**不是** `sampling_s`/`per_step_s` ——
        它是 one-step 模型，没有「s/步」这个量。"""
        data = self.ready().post("/api/build", json=self.up()).json()
        parts = data["estimate"]["parts"]
        self.assertIn("upscale_s", parts)
        self.assertNotIn("sampling_s", parts)
        self.assertNotIn("per_step_s", parts)
        # 2048² 输出：实测 13.24~14.71 秒，估算要点在这个量级
        self.assertGreater(data["estimate"]["seconds"], 8)
        self.assertLess(data["estimate"]["seconds"], 25)

    # --- 三类会「必然失败」的环境问题 -------------------------------------
    def test_missing_plugin_is_an_error_and_gives_the_install_path(self):
        data = self.ready(nodes=False).post("/api/build", json=self.up()).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(errs)
        self.assertIn("TE-Speed-VOSR2", " ".join(w["text"] for w in errs))

    def test_missing_weights_is_an_error_and_names_the_install_dir(self):
        """权重没下 → 插件自己那张 bundle 候选表是**空的**。报错里要写清落点，
        因为那正是最容易放错的地方（它在安装目录，不在共享模型库）。"""
        data = self.ready(bundles=[]).post("/api/build", json=self.up()).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(errs)
        text = " ".join(w["text"] for w in errs)
        self.assertIn("VOSR2", text)
        self.assertIn("models", text)
        self.assertIn("安装目录", text)

    def test_an_unreadable_bundle_table_warns_and_does_not_accuse(self):
        """**候选表读不到 ≠ 权重不在。** 前者只能说「没查到」，说成「没有」
        就会让用户去重下 6.5 GiB —— 这两件事必须分开。"""
        data = self.ready(bundles=False).post("/api/build", json=self.up()).json()
        levels = [w["level"] for w in data["warnings"] if "VOSR2" in w["text"]]
        self.assertEqual(levels, ["warn"])
        texts = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("读不到", texts)
        self.assertNotIn("提交必然失败", texts)

    def test_weights_present_is_just_information(self):
        data = self.ready().post("/api/build", json=self.up()).json()
        hits = [w for w in data["warnings"] if "VOSR2" in w["text"]]
        self.assertEqual([w["level"] for w in hits], ["info"])

    def test_a_source_outside_input_is_an_error(self):
        data = self.ready().post("/api/build",
                                 json=self.up(source_image="gone.png")).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(errs)
        self.assertIn("gone.png", " ".join(w["text"] for w in errs))

    def test_unreadable_input_list_warns_instead_of_claiming_absence(self):
        tc = self.ready()
        self.client.input_options = None      # 后端答不出候选表了
        data = tc.post("/api/build", json=self.up()).json()
        self.assertTrue(any("读不到" in w["text"] for w in data["warnings"]))
        self.assertFalse(any("提交必然失败" in w["text"] for w in data["warnings"]))

    def test_the_qwen_models_are_not_checked(self):
        """放大不用那四个 Qwen 模型 —— 它们缺了也不该拦住放大。"""
        tc = self.ready(models={"dit": {"present": False, "filename": "x"}})
        data = tc.post("/api/build", json=self.up()).json()
        self.assertFalse(any("后端看不到这些模型文件" in w["text"]
                             for w in data["warnings"]))
        self.assertFalse(any(w["level"] == "error" for w in data["warnings"]))

    def test_says_it_is_generative_not_a_magnifier(self):
        data = self.ready().post("/api/build", json=self.up()).json()
        self.assertIn("生成式", " ".join(w["text"] for w in data["warnings"]))

    # --- 提交与预警 -------------------------------------------------------
    def test_run_submits_and_records_the_mode(self):
        data = self.ready().post("/api/run", json=self.up()).json()
        # /api/run 的响应里模式在 describe 下面，不在顶层
        self.assertEqual(data["describe"]["mode"], "upscale")
        job = self.wb.store.get(data["job_id"])
        self.assertEqual(job["mode"], "upscale")
        self.assertEqual(job["params"]["scale"], 2)
        # 提交下去的必须是放大那张图，不能混进 KSampler。
        # 记录里存的是 `node_classes`（nid → 类名），不是整张图。
        classes = set(job["node_classes"].values())
        self.assertIn(self.NODE, classes)
        self.assertNotIn("KSampler", classes)
        self.assertEqual(job["node_count"], 5)
        self.assertEqual(job["prompt_text"], "")     # 放大没有提示词

    def test_build_and_run_agree_about_the_environment(self):
        """同一个请求，/api/build 与 /api/run 不能各说一套 ——
        页面说「权重不在」而提交回一句「读不到清单」，用户没法判断哪句是真的。"""
        tc = self.ready()
        built = " ".join(w["text"] for w in
                         tc.post("/api/build", json=self.up()).json()["warnings"])
        ran = " ".join(w["text"] for w in
                       tc.post("/api/run", json=self.up()).json()["warnings"])
        self.assertIn("权重 `VOSR2` 在", built)
        self.assertIn("权重 `VOSR2` 在", ran)
        # 而且两边都得报出真实输出尺寸（都去读了源图的 IHDR）
        self.assertIn("2048 × 2048", built)
        self.assertIn("2048 × 2048", ran)

    def test_the_estimate_in_the_record_matches_the_page(self):
        """页面显示的数字与写进记录的数字必须同源，否则会出现
        「页面说 14 秒、记录里写 5 秒」这种没人能解释的分家。"""
        tc = self.ready(files=(("a.png", 1024, 1024),))
        shown = tc.post("/api/build", json=self.up()).json()["estimate"]["seconds"]
        run = tc.post("/api/run", json=self.up()).json()
        self.assertEqual(run["estimate_s"], shown)

    def test_a_huge_output_asks_for_confirmation(self):
        """输出 16384² 是 256 MP、约 5 分钟 —— 这种要用户再点一次。
        （实测过的最大档是 4096² 输出 / 41 秒，那个不该被拦。）"""
        tc = self.ready()
        r = tc.post("/api/run", json=self.up(scale=16))
        self.assertEqual(r.status_code, 409)
        self.assertIn("输出", r.json()["confirm"]["text"])
        # 带上 confirm 就放行
        ok = tc.post("/api/run", json=self.up(scale=16, confirm=True))
        self.assertEqual(ok.status_code, 200)

    def test_a_normal_upscale_is_not_gated(self):
        """2048² 输出只有十几秒 —— 拦它就是训练用户闭眼点确认。"""
        r = self.ready().post("/api/run", json=self.up())
        self.assertEqual(r.status_code, 200)

    def test_sweep_refuses_a_steps_axis(self):
        """放大没有步数。放过一条 steps 轴，构建器会**默默忽略**它 ——
        于是 N 格全得到差不多的图，而页面把它们并排摆成「参数对比」。
        那正是本工作区最忌讳的错：没有症状、看着像结论。"""
        tc = self.ready()
        body = self.up()
        body["grid"] = {"seed": [1, 2], "steps": [10, 20]}
        r = tc.post("/api/sweep", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("步数", r.json()["error"])

    def test_sweep_over_seed_is_fine(self):
        """但**扫 seed 是合理的** —— 实测换 seed 会改 73% 的像素。"""
        tc = self.ready()
        body = self.up()
        body["grid"] = {"seed": [1, 2, 3]}
        r = tc.post("/api/sweep", json=body)
        self.assertEqual(r.status_code, 201)      # 建了一批任务，所以是 201
        self.assertEqual(r.json()["count"], 3)


class CostConfirm(InputsBase):
    """成本预警：贵的那档**真的**拦一道，不只是页面上写句话。"""

    # 2K 现在就是「文生图 + 显式 2048²」（那个便捷模式 2026-10-01 撤销了），
    # 所以这里只剩尺寸这一件事要写。
    def big(self, **over):
        return self.build_body(mode="t2i", width=2048, height=2048, **over)

    def test_build_reports_confirm_required_for_2k(self):
        tc = self.make()
        data = tc.post("/api/build", json=self.big()).json()
        self.assertTrue(data["estimate"]["confirm"]["required"])
        self.assertIn("确认提交", data["estimate"]["confirm"]["text"])

    def test_a_small_size_needs_no_confirm(self):
        """一次 1024² 的运行不值当拦 —— 门槛落在**实际尺寸**上。"""
        tc = self.make()
        data = tc.post("/api/build",
                       json=self.build_body(mode="t2i", width=1024,
                                            height=1024)).json()
        self.assertIsNone(data["estimate"]["confirm"])

    def test_cheap_t2i_needs_no_confirm(self):
        """判据 1 的主流程必须仍是一点就出 —— 这是那道门槛的上界。"""
        tc = self.make()
        data = tc.post("/api/build", json=self.build_body(mode="t2i")).json()
        self.assertIsNone(data["estimate"]["confirm"])

    def test_rewrite_needs_confirm_even_with_a_single_step(self):
        """改写器那条理由是**显存**（+8.82 GiB），与秒数无关。

        2026-10-01 之前它挂在这个模式上（`mode="refine"`），现在是一个开关 ——
        所以这一条同时钉住「新的字段真的接进了那套判据」。
        """
        tc = self.make()
        data = tc.post("/api/build",
                       json=self.build_body(mode="t2i", rewrite=True,
                                            steps=1)).json()
        self.assertTrue(data["estimate"]["confirm"]["required"])
        self.assertIn("8.82", data["estimate"]["confirm"]["text"])

    def test_rewrite_costs_more_than_not_rewriting(self):
        """`rewrite` 必须真的进成本模型，而不是只在页面上写着。"""
        tc = self.make()
        plain = tc.post("/api/build", json=self.build_body(mode="t2i")).json()
        rew = tc.post("/api/build",
                      json=self.build_body(mode="t2i", rewrite=True)).json()
        self.assertGreater(rew["estimate"]["parts"]["rewriter_s"], 0)
        self.assertEqual(plain["estimate"]["parts"]["rewriter_s"], 0)
        self.assertGreater(rew["estimate"]["seconds"], plain["estimate"]["seconds"])

    def test_run_without_confirm_is_409_and_submits_nothing(self):
        tc = self.make()
        r = tc.post("/api/run", json=self.big())
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["confirm"]["required"])
        self.assertEqual(self.client.submitted, [])

    def test_run_with_confirm_goes_through(self):
        tc = self.make()
        body = self.big()
        body["confirm"] = True
        r = tc.post("/api/run", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.client.submitted), 1)

    def test_confirm_false_is_still_refused(self):
        tc = self.make()
        body = self.big()
        body["confirm"] = False
        self.assertEqual(tc.post("/api/run", json=body).status_code, 409)

    def test_string_confirm_also_counts(self):
        tc = self.make()
        body = self.big()
        body["confirm"] = "true"
        self.assertEqual(tc.post("/api/run", json=body).status_code, 200)

    def test_refused_run_creates_no_job_record(self):
        """409 之后 store 里不能留下一条孤儿任务 —— 页面会显示成一次失败。"""
        tc = self.make()
        tc.post("/api/run", json=self.big())
        self.assertEqual(self.wb.store.count(), 0)

    def test_policy_keys_the_2k_caveat_on_size_not_the_mode_name(self):
        """2K 那条显存警告的判据是**实际尺寸**，与模式名无关。

        第一版把它挂在 `mode == "2k"` 上，于是 `mode=2k` + 1024²（一次 1024² 的
        运行）被误拦。2026-10-01 那个模式撤销了，这条判据的价值反而更明显：
        现在**没有任何模式**能让 2048² 出现或消失，它只看尺寸。
        """
        # 真的 2048² 才有那条理由
        p = cost.confirm_policy("t2i", 300.0, 2048, 2048)
        self.assertTrue(p["required"])
        self.assertTrue(any("步耗时受显存状态" in r for r in p["reasons"]))
        # 只有一边到 2048 也算（与 describe() 的 is_2k 同判据）
        self.assertTrue(cost.confirm_policy("t2i", 300.0, 2048, 1024)["required"])
        # 1024² 且不贵 → 不需要确认
        self.assertIsNone(cost.confirm_policy("t2i", 60.0, 1024, 1024))
        # 放大走的是另一条链路（VOSR2 没有「每步耗时」），同一个尺寸不该触发它
        self.assertFalse(any("步耗时受显存状态" in r
                             for r in (cost.confirm_policy("upscale", 300.0, 2048,
                                                           2048) or {"reasons": []}
                                       )["reasons"]))

    def test_policy_reasons_are_independent(self):
        """时间与显存是两个理由，各自成立 —— 别把它们耦合成一个。"""
        # 只要显存：打开改写器 + 1 步（秒数很低）
        p = cost.confirm_policy("t2i", 5.0, 1024, 1024, rewrite=True)
        self.assertTrue(any("8.82" in r for r in p["reasons"]))
        self.assertFalse(any("超过" in r for r in p["reasons"]))
        # 只要时间：t2i 跑很久
        p = cost.confirm_policy("t2i", 400.0, 1024, 1024)
        self.assertTrue(any("超过" in r for r in p["reasons"]))
        self.assertFalse(any("8.82" in r for r in p["reasons"]))


class BackendLiveness(ApiBase):
    """「没答好」不是「没起」。冷加载 16 GiB 时 `/system_stats` 实测能超过 2 秒不答，
    早先一律当成 False，于是正在跑任务的后端被报成未运行、提交还被自己挡 503。

    三态各有各的处置：down → 去拉后端；unclear → 等一下，别去重启；
    ok → 正常。混成一个布尔就没法给出对的那句话。
    """

    def test_unclear_is_still_reported_as_running(self):
        tc = self.make()
        self.client.unclear = True
        be = tc.get("/api/backend").json()
        self.assertTrue(be["running"])
        self.assertEqual(be["liveness"], "unclear")
        self.assertIn("没干净答完", be["hint"])

    def test_unclear_does_not_tell_the_user_to_start_the_backend(self):
        tc = self.make()
        self.client.unclear = True
        be = tc.get("/api/backend").json()
        self.assertNotIn("后端未运行", be["hint"])
        self.assertIn("不用去拉后端", be["hint"])

    def test_unclear_still_accepts_a_submit(self):
        """没答好的时候正是用户最想排队的时候 —— 不该被挡在门外。"""
        tc = self.make()
        self.client.unclear = True
        r = tc.post("/api/run", json=self.build_body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.client.submitted), 1)

    def test_down_is_still_503_and_says_so(self):
        tc = self.make(running=False)
        r = tc.post("/api/run", json=self.build_body())
        self.assertEqual(r.status_code, 503)
        be = tc.get("/api/backend").json()
        self.assertEqual(be["liveness"], "down")
        self.assertIn("后端未运行", be["hint"])

    def test_the_down_hint_points_at_where_the_command_actually_is(self):
        """告警板撤掉之后，那句话**不许再指着下面**（P20i）。

        原来它是「把**下面这条命令**贴进 PowerShell 执行」—— 那句话长在一块把
        命令摆在正下方的告警板上。板子撤了之后命令搬到「日志 → 环境体检」，
        同一句话就从「说得对」变成「指着一个不存在的东西」：用户去下面找，
        而下面什么都没有。文案的落点变了，判据得跟着落。
        """
        tc = self.make(running=False)
        hint = tc.get("/api/backend").json()["hint"]
        self.assertNotIn("下面这条命令", hint)
        self.assertIn("日志", hint)          # 它现在在哪儿，得说出来
        self.assertIn("开关", hint)          # 怎么拉起来，也得说出来
        # 503 那条也要指向同一个地方（它原来写的是「按 /api/backend 给的命令」，
        # 那是个接口名，不是用户在页面上能找到的东西）
        err = tc.post("/api/run", json=self.build_body()).json()["error"]
        self.assertNotIn("/api/backend 给的命令", err)
        self.assertIn("开关", err)

    def test_healthy_reports_ok(self):
        tc = self.make()
        be = tc.get("/api/backend").json()
        self.assertTrue(be["running"])
        self.assertEqual(be["liveness"], "ok")


class SweepRoutes(ApiBase):
    """判据 5：3 个 seed 扫描，各带 wall_ms 与 s/步。

    扫描刻意**不新造任务类型** —— 每一格就是一次普通出图，所以这里最重的
    几条是在验「它真的复用了那条路」，而不是另起了一套。
    """

    def sweep_body(self, **over):
        body = self.build_body()
        body["grid"] = {"seed": [11, 22, 33]}
        body.pop("seed", None)          # 交给 grid，别让基准里的 seed 混进来
        body.update(over)
        return body

    def test_three_seeds_makes_three_jobs(self):
        tc = self.make()
        r = tc.post("/api/sweep", json=self.sweep_body())
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["count"], 3)
        self.assertTrue(r.json()["sweep_id"])
        self.assertEqual(len(self.client.submitted), 3)

    def test_the_auto_upscale_chain_is_dropped_and_reported(self):
        """扫描里**不接**出图后自动放大，而且要把这件事**说出来**。

        否则 N 格变 2N 个作业，而「扫到一半崩了整次作废」的语义会被搅乱；
        更糟的是用户勾着它按扫描，拿到 N 张没放大的图却不知道为什么 ——
        静默降级正是这个仓库最忌讳的那类错。
        """
        tc = self.make()
        body = self.sweep_body(auto_upscale={"enabled": True, "scale": 3})
        data = tc.post("/api/sweep", json=body).json()
        self.assertEqual(data["count"], 3)
        self.assertTrue(data["auto_upscale_dropped"])
        # 每一格的记录里那条链都必须是**关着**的（留成 True 就会在收尾时被
        # `_maybe_chain` 捡起来）。键本身**要在** —— 记录形状一致是另一条契约。
        for cell in data["cells"]:
            rec = self.wb.store.get(cell["job_id"])
            self.assertFalse(rec["params"]["auto_upscale"]["enabled"])
        # 不勾的时候没什么要报的
        plain = tc.post("/api/sweep", json=self.sweep_body()).json()
        self.assertFalse(plain["auto_upscale_dropped"])

    def test_each_cell_carries_its_own_seed_into_the_workflow(self):
        """这一条才是判据 5 的实质：三格必须**真的**是三个不同的 seed。

        只验「建了 3 条记录」是不够的 —— 记录对了但提交的图是同一个 seed，
        结果就是三张一模一样的图，而页面会显示成一次成功的扫描。
        """
        tc = self.make()
        tc.post("/api/sweep", json=self.sweep_body())
        seeds = [s["workflow"]["459:458"]["inputs"]["seed"]
                 for s in self.client.submitted]
        self.assertEqual(seeds, [11, 22, 33])

    def test_cells_share_a_sweep_id_and_are_indexed(self):
        tc = self.make()
        data = tc.post("/api/sweep", json=self.sweep_body()).json()
        rows = self.wb.store.sweep(data["sweep_id"])
        self.assertEqual([r["cell_index"] for r in rows], [0, 1, 2])
        self.assertEqual([r["cell"]["seed"] for r in rows], [11, 22, 33])
        self.assertEqual({r["sweep_id"] for r in rows}, {data["sweep_id"]})

    def test_sweep_id_is_not_smuggled_into_params(self):
        """params 会被画廊读回来填表单 —— 里面不该多出分组字段。"""
        tc = self.make()
        data = tc.post("/api/sweep", json=self.sweep_body()).json()
        rec = self.wb.store.get(data["cells"][0]["job_id"])
        for gone in ("sweep_id", "grid", "cell", "cell_index"):
            self.assertNotIn(gone, rec["params"])

    def test_two_axes_expand_to_the_cartesian_product(self):
        tc = self.make()
        body = self.build_body()
        body["grid"] = {"seed": [1, 2], "steps": [10, 20]}
        self.assertEqual(tc.post("/api/sweep", json=body).json()["count"], 4)
        pairs = [(s["workflow"]["459:458"]["inputs"]["seed"],
                  s["workflow"]["459:458"]["inputs"]["steps"])
                 for s in self.client.submitted]
        self.assertEqual(pairs, [(1, 10), (2, 10), (1, 20), (2, 20)])

    def test_over_the_cap_is_400_and_queues_nothing(self):
        tc = self.make()
        body = self.build_body()
        body["grid"] = {"seed": list(range(20))}
        r = tc.post("/api/sweep", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("20 张", r.json()["error"])
        self.assertIn("上限 12", r.json()["error"])
        self.assertEqual(self.client.submitted, [])

    def test_grid_must_be_an_object(self):
        tc = self.make()
        body = self.build_body()
        body["grid"] = "seeds please"
        self.assertEqual(tc.post("/api/sweep", json=body).status_code, 400)

    def test_missing_grid_is_400(self):
        tc = self.make()
        r = tc.post("/api/sweep", json=self.build_body())
        self.assertEqual(r.status_code, 400)
        self.assertIn("至少要给一条轴", r.json()["error"])

    def test_dupes_are_reported_not_hidden(self):
        tc = self.make()
        body = self.build_body()
        body["grid"] = {"seed": [5, 5, 5]}
        data = tc.post("/api/sweep", json=body).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["dropped_dupes"], 2)

    def test_backend_down_is_503(self):
        tc = self.make(running=False)
        self.assertEqual(tc.post("/api/sweep",
                                 json=self.sweep_body()).status_code, 503)

    def test_a_refused_cell_carries_its_own_payload(self):
        """被成本预警拦住时，客户端要能看出**是预警拦的**。

        只给一句「第 1/3 格失败」是不够的 —— 页面拿不到 `confirm` 就不知道
        该提示用户「再点一次确认」，只能显示一句没用的错误。
        """
        tc = self.make()
        body = self.sweep_body(mode="t2i", width=2048, height=2048)
        r = tc.post("/api/sweep", json=body)
        self.assertEqual(r.status_code, 409)
        data = r.json()
        self.assertIn("confirm", data)
        self.assertTrue(data["confirm"]["required"])
        self.assertEqual(data["cell_error"], data["confirm"]["text"])
        self.assertIn("第 1/3 格失败", data["error"])

    def test_cost_gate_applies_to_sweeps_too(self):
        """否则「一次排 3 个 2K」就成了绕过成本预警的后门。"""
        tc = self.make()
        body = self.sweep_body(mode="t2i", width=2048, height=2048)
        r = tc.post("/api/sweep", json=body)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.wb.store.count(), 0)

    def test_confirm_lets_the_sweep_through(self):
        tc = self.make()
        body = self.sweep_body(mode="t2i", width=2048, height=2048,
                               confirm=True)
        self.assertEqual(tc.post("/api/sweep", json=body).status_code, 201)
        self.assertEqual(len(self.client.submitted), 3)

    def test_an_invalid_cell_stops_before_queueing_anything(self):
        """第一格就失败时**一张都不该排上** —— 12 张同一个错不该排 12 次。"""
        tc = self.make()
        r = tc.post("/api/sweep", json=self.sweep_body(steps=99999))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.submitted, [])


class SweepAbort(ApiBase):
    """扫到一半崩了：**整次作废**，不留半个对照。

    半个扫描是没用的对照 —— 它会在不同条件下跑出不同的失败方式，
    用户拿它比对参数影响就会得出错的结论。宁可不做。
    """

    def four(self, **over):
        body = self.build_body()
        body["grid"] = {"seed": [1, 2, 3, 4]}
        body.pop("seed", None)
        body.update(over)
        return body

    def test_midway_failure_cancels_what_was_already_queued(self):
        tc = self.make()
        self.client.fail_at = 2                      # 第 2 格提交被拒
        r = tc.post("/api/sweep", json=self.four())
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("第 2/4 格失败", r.json()["error"])
        self.assertIn("已撤销前面 1 格", r.json()["error"])
        self.assertEqual(len(self.client.submitted), 2)      # 第 3、4 格没提交
        self.assertEqual(r.json()["cancelled"],
                         [self.wb.store.list(limit=50)[-1]["job_id"]])

    def test_first_cell_failure_leaves_nothing_behind(self):
        tc = self.make()
        self.client.fail_at = 1
        r = tc.post("/api/sweep", json=self.four())
        self.assertEqual(r.status_code, 400)
        self.assertIn("已撤销前面 0 格", r.json()["error"])
        self.assertEqual(len(self.client.submitted), 1)

    def test_abort_of_queued_cells_does_not_wait_five_seconds_each(self):
        """排着的任务不会有 execution_* 事件，为它等 5 秒纯属白等。

        三格都排上、而且**确知**它们还排着时，应该立刻撤完。
        真等下去这里是 3×5=15 秒 —— 这条测试拿时间当断言。
        """
        tc = self.make()
        self.client.auto_queue = True                # 确知三格都在排队
        self.client.fail_at = 4                      # 三格都排上之后才崩
        t0 = time.time()
        r = tc.post("/api/sweep", json=self.four())
        elapsed = time.time() - t0
        self.assertEqual(r.status_code, 400)
        self.assertIn("已撤销前面 3 格", r.json()["error"])
        self.assertLess(elapsed, 3.0,
                        "撤 3 个排着的格子用了 %.1f 秒 —— 不该等满 5 秒" % elapsed)

    def test_unreadable_queue_takes_the_waiting_path_instead_of_guessing(self):
        """读不到队列时**必须等**。

        抢着标 cancelled 会把一张真出了的图记成「已取消」，而那个错误一旦
        写进记录就收不回来了。这条不测时长，直接盯着「有没有走等待那条路」。
        """
        tc = self.make()
        self.client.fail_at = 2
        self.client.queue_ids = lambda: None         # 读不到队列
        calls = []
        real = self.wb.watcher.wait_finish

        def spy(pid, timeout=5.0, interval=0.25):
            calls.append(pid)
            return real(pid, 0.05, 0.02)             # 真等，但只等 50 ms

        with mock.patch.object(self.wb.watcher, "wait_finish", spy):
            r = tc.post("/api/sweep", json=self.four())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(calls), 1, "读不到队列时应当等一次真实终态")
        self.assertIn("已撤销前面 1 格", r.json()["error"])

    def test_certainly_queued_skips_the_wait_entirely(self):
        """反向对照：确知排着时，**一次都不该等**。

        没有这条，上面那条「立刻撤完」可能只是因为 `wait_finish` 恰好很快返回，
        而不是因为我们真的走了捷径。
        """
        tc = self.make()
        self.client.auto_queue = True
        self.client.fail_at = 2
        calls = []
        with mock.patch.object(self.wb.watcher, "wait_finish",
                               lambda *a, **k: calls.append(a) or False):
            r = tc.post("/api/sweep", json=self.four())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(calls, [], "确知排着时不该等")


class SweepReport(ApiBase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        # 撤销路径会去等「真实终态」（产品里是 5 秒）。这里把等待压到毫秒级 ——
        # 被测的是**逻辑**，不是那个常量。单测为了迁就一个超时值跑几十秒，
        # 只会让人以后不敢动这块代码。
        p1 = mock.patch.object(config, "CANCEL_WAIT_S", 0.05)
        p2 = mock.patch.object(config, "CANCEL_BATCH_BUDGET_S", 0.2)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def start(self, **over):
        """在**同一个**工作台里起一次扫描。

        `ApiBase.make()` 每次都会新建一个临时 state 目录，所以调用两次拿到的是
        两个互不相干的工作台 —— 第一次扫出来的任务在第二个里当然一条也找不到。
        所以这里复用已经建好的那个 client。
        """
        body = self.build_body()
        body["grid"] = {"seed": [11, 22, 33]}
        body.pop("seed", None)
        body.update(over)
        tc = getattr(self, "tc", None) or self.make()
        return tc, tc.post("/api/sweep", json=body).json()

    def test_unknown_sweep_is_404(self):
        tc = self.make()
        self.assertEqual(tc.get("/api/sweep/nope").status_code, 404)

    def test_cells_come_back_in_grid_order(self):
        tc = self.make()
        data = tc.post("/api/sweep", json={
            "mode": "t2i", "prompt": PROMPT, "steps": 20, "width": 1024,
            "height": 1024, "cfg": 1.0,
            "grid": {"seed": [1, 2], "steps": [10, 20]}}).json()
        cells = tc.get("/api/sweep/" + data["sweep_id"]).json()["cells"]
        self.assertEqual([c["cell_index"] for c in cells], [0, 1, 2, 3])
        self.assertEqual([(c["cell"]["steps"], c["cell"]["seed"])
                          for c in cells],
                         [(10, 1), (10, 2), (20, 1), (20, 2)])

    def test_pending_cells_have_no_per_step_yet(self):
        tc, data = self.start()
        got = tc.get("/api/sweep/" + data["sweep_id"]).json()
        self.assertEqual(got["finished"], 0)
        for cell in got["cells"]:
            self.assertIsNone(cell["per_step"]["ms"])
            self.assertIsNone(cell["per_step"]["source"])

    def test_done_cell_reports_per_step_with_its_source(self):
        """判据 5 要的「s/步」就在这里 —— 而且必须带来源。"""
        tc, data = self.start()
        jid = data["cells"][0]["job_id"]
        self.wb.store.finish(jid, "done", wall_ms=59448,
                             outputs=[{"filename": "a.png", "subfolder": "",
                                       "type": "output"}])
        cells = tc.get("/api/sweep/" + data["sweep_id"]).json()["cells"]
        cell = [c for c in cells if c["job_id"] == jid][0]
        self.assertEqual(cell["wall_ms"], 59448)
        # 探针没开 → 退化成 wall/steps，但**必须**标出来源，不能冒充探针读数
        self.assertEqual(cell["per_step"]["ms"], 2972.4)
        self.assertEqual(cell["per_step"]["source"], "wall_over_steps")
        self.assertEqual(cell["per_step"]["steps"], 20)

    def test_totals_and_counts(self):
        tc, data = self.start()
        for cell, wall in zip(data["cells"], (1000, 2000)):
            self.wb.store.finish(cell["job_id"], "done", wall_ms=wall)
        got = tc.get("/api/sweep/" + data["sweep_id"]).json()
        self.assertEqual(got["done"], 2)
        self.assertEqual(got["finished"], 2)
        self.assertEqual(got["wall_ms_total"], 3000)
        self.assertEqual(got["count"], 3)

    def test_report_carries_the_per_step_provenance_note(self):
        tc, data = self.start()
        got = tc.get("/api/sweep/" + data["sweep_id"]).json()
        self.assertIn("探针", got["per_step_note"])
        self.assertIn("偏大", got["per_step_note"])

    def test_cancel_stops_the_active_cells(self):
        tc, data = self.start()
        r = tc.post("/api/sweep/%s/cancel" % data["sweep_id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 3)
        rows = self.wb.store.sweep(data["sweep_id"])
        self.assertEqual({x["status"] for x in rows}, {"cancelled"})

    def test_cancel_reports_which_cells_were_confirmed(self):
        """没等到真实终态的那几个要说出来，不能让调用方以为都是准的。"""
        tc, data = self.start()
        # 队列读得到、但里面没有这三格 → 既不是 pending 也不是 running，
        # 属于「说不清」，于是走等待那条路；等不到就记进 unconfirmed
        r = tc.post("/api/sweep/%s/cancel" % data["sweep_id"]).json()
        self.assertEqual(len(r["unconfirmed"]), 3)
        self.assertEqual(set(r["unconfirmed"]), set(r["cancelled"]))

    def test_cancel_of_certainly_queued_cells_is_confirmed(self):
        tc = self.make()
        self.client.auto_queue = True
        data = tc.post("/api/sweep", json={
            "mode": "t2i", "prompt": PROMPT, "steps": 20, "width": 1024,
            "height": 1024, "cfg": 1.0,
            "grid": {"seed": [1, 2, 3]}}).json()
        r = tc.post("/api/sweep/%s/cancel" % data["sweep_id"]).json()
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["unconfirmed"], [], "确知排着的格子不该算「没确认」")

    def test_the_whole_batch_shares_one_wait_budget(self):
        """12 格不能变成 12 × 5 秒 = 60 秒的请求。

        预算用完就按用户意图收尾，并如实标出「没确认」。

        **判据是行为，不是墙钟上界。** 这里原先写 `assertLess(elapsed, 0.35)`，
        而每格的 `queue_ids` / `cancel` 往返开销是随机器负载浮动的 —— 实测全量
        跑时它抖到过 0.41 秒，单跑 6 次又都是绿的，属于「抖动红线」：
        它红的时候你分不清是实现退化还是机器正好忙。改成查**有谁被标成没确认**，
        这个结论由预算与每格等待时长直接决定，与负载无关。
        """
        tc = self.make()
        self.client.auto_queue = False          # 队列读得到但说不清这些格
        body = self.build_body()
        body["grid"] = {"seed": list(range(8))}
        body.pop("seed", None)
        data = tc.post("/api/sweep", json=body).json()
        t0 = time.time()
        r = tc.post("/api/sweep/%s/cancel" % data["sweep_id"]).json()
        elapsed = time.time() - t0
        self.assertEqual(r["count"], 8)

        # 行为判据：每格要等 0.05 秒、整批预算 0.2 秒 → **后几格必然等不到**，
        # 它们必须被如实标成「没确认」。预算若没起作用，8 格都会等到真实终态，
        # unconfirmed 就是空的 —— 那才是我们真正要防的退化。
        self.assertTrue(
            r["unconfirmed"],
            "8 格每格等 0.05 秒、预算只有 0.2 秒，却一格都没被标成「没确认」"
            " —— 预算没起作用（整批等完了）")

        # 墙钟只留一个宽松上界：防「整批等完」的那种退化，不做微秒级断言。
        # 8 格 × 0.05 秒 = 0.4 秒，若预算失效至少是这个数；0.75 秒留给机器抖动。
        self.assertLess(
            elapsed, 0.75,
            "8 格撤了 %.2f 秒 —— 预算完全没压住（预期约 0.2~0.4 秒）" % elapsed)

    def test_cancel_on_unknown_sweep_is_404(self):
        tc = self.make()
        self.assertEqual(tc.post("/api/sweep/nope/cancel").status_code, 404)

    def test_cancel_is_idempotent(self):
        tc, data = self.start()
        tc.post("/api/sweep/%s/cancel" % data["sweep_id"])
        again = tc.post("/api/sweep/%s/cancel" % data["sweep_id"]).json()
        self.assertEqual(again["count"], 0)

    def test_parent_sweep_id_is_what_groups_them(self):
        """两次不同的扫描不该混在一起 —— 分组键就是 sweep_id。"""
        tc, first = self.start()
        _, second = self.start()
        self.assertNotEqual(first["sweep_id"], second["sweep_id"])
        rows_a = self.wb.store.sweep(first["sweep_id"])
        self.assertEqual(len(rows_a), 3)
        self.assertEqual(len(self.wb.store.sweep(second["sweep_id"])), 3)


class LaunchApiBase(ApiBase):
    """P6 的路由层。真起一次 ComfyUI 要十几秒、占内存，而且本机已经有一个在跑
    （起第二个只会撞端口），所以把「进程」与「配置现场」都换成替身。"""

    def make_launchable(self, running=False, with_yaml=True, proc=None):
        tc = self.make(running=running)
        root, exe, ypath = support.fake_comfy_install(self.tmp, with_yaml)
        argv, patchers = support.patch_launch_config(root, exe, ypath)
        for p in patchers:
            self.addCleanup(p.stop)
        patcher, holder = support.fake_popen(proc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.launch_argv = argv
        self.spawn_holder = holder
        return tc

    def wait_phase(self, tc, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = tc.get("/api/backend/launch").json()
            if st["phase"] in ("ready", "failed", "exited"):
                return st
            time.sleep(0.05)
        return tc.get("/api/backend/launch").json()


class BackendStart(LaunchApiBase):
    def test_start_returns_202_and_the_command_it_ran(self):
        tc = self.make_launchable()
        r = tc.post("/api/backend/start")
        self.assertEqual(r.status_code, 202, r.text)
        data = r.json()
        self.assertEqual(data["phase"], "starting")
        self.assertIsNotNone(data["pid"])
        self.assertEqual(data["log_path"], self.wb.launcher.log_path)
        # 逐元素拼接，yaml 路径没有被空格切开
        self.assertEqual(self.spawn_holder["argv"], self.launch_argv)

    def test_start_announces_itself_over_sse(self):
        tc = self.make_launchable()
        tc.post("/api/backend/start")
        types = [e["type"] for _, e in self.wb.broker.replay_since(0)]
        self.assertIn("backend_launching", types)

    def test_start_is_refused_when_the_port_is_occupied(self):
        tc = self.make_launchable(running=True)      # 端口上有东西
        r = tc.post("/api/backend/start")
        self.assertEqual(r.status_code, 409)
        self.assertIn("不抢它的端口", r.json()["error"])
        self.assertIsNone(self.spawn_holder["argv"], "拒绝了就**不许**起进程")

    def test_start_is_refused_when_the_model_yaml_is_missing(self):
        tc = self.make_launchable(with_yaml=False)
        r = tc.post("/api/backend/start")
        self.assertEqual(r.status_code, 409)
        self.assertIn("模型路径配置", r.json()["error"])
        self.assertIsNone(self.spawn_holder["argv"])

    def test_ready_is_reported_and_the_wiring_to_the_broker_works(self):
        """启动流程跑在**普通线程**里，所以它走的是 `broker.publish_threadsafe`。

        这里断言的是**接线**：工作台确实把启动器接到了 broker 上。
        （`backend_ready` 这类事件本身由 `test_launch.py` 验，那边不需要事件循环；
        这里换成 TestClient 之后没有运行中的 loop，`call_soon_threadsafe`
        投不进来，所以盯 broker 的入口而不是它的收件箱。）
        """
        tc = self.make_launchable()
        self.client.running_flag = False
        seen = []
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return calls["n"] > 2

        # 打桩要打在 `launcher.on_event` 上，**不是** `broker.publish_threadsafe`：
        # 后者在 Launcher 构造时就以**绑定的方法对象**存下来了，
        # 之后再换 broker 上的那个名字，启动器手里那个引用不会跟着变。
        with mock.patch.object(self.wb.launcher, "on_event",
                               seen.append), \
                mock.patch.object(self.client, "probe", probe):
            tc.post("/api/backend/start")
            st = self.wait_phase(tc)
        self.assertEqual(st["phase"], "ready")
        self.assertTrue(st["ready"])
        self.assertIn("backend_ready", [e["type"] for e in seen])

    def test_the_broker_threadsafe_publisher_is_what_gets_wired(self):
        """别改成 `publish` —— 那个要跑到事件循环上，从启动线程里调会抛。"""
        self.make_launchable()          # 建出 wb 才能看接线
        self.assertEqual(self.wb.launcher.on_event,
                         self.wb.broker.publish_threadsafe)

    def test_early_exit_reports_the_code_and_keeps_the_log(self):
        tc = self.make_launchable(proc=support.FakeProc(exit_code=3))
        with open(self.wb.launcher.log_path, "a", encoding="utf-8") as f:
            f.write("OSError: address already in use\n")
        self.client.running_flag = False
        with mock.patch.object(self.client, "probe", lambda: False):
            tc.post("/api/backend/start")
            st = self.wait_phase(tc)
        self.assertEqual(st["phase"], "exited")
        self.assertEqual(st["exit_code"], 3)
        # 认得出原因就说原因（这里日志里写的是端口占用）
        self.assertIn("端口被占", st["error"])
        self.assertTrue(st["error_short"])
        # log_tail 是**行的列表**，所以断言「某一行包含」而不是「等于」
        self.assertTrue(any("address already in use" in ln
                            for ln in st["log_tail"]), st["log_tail"])


class BackendStop(LaunchApiBase):
    """`POST /api/backend/stop`（P20i）：页面上那颗开关的「关」。

    两层闸都在这里验：**有作业没结束**（路由层判，尾队在 ComfyUI 自己进程的
    内存里）与**不是自己起的**（Launcher 判）。外加一条**留痕**：拒绝要进
    日志台的时间线 —— 那颗开关上只有颜色与一句悬停提示，刷新就没了。
    """

    def started(self, proc=None):
        """起一个「我们自己的」后端，返回 (tc, proc)。"""
        proc = proc or support.FakeProc()
        tc = self.make_launchable(proc=proc)
        r = tc.post("/api/backend/start")
        self.assertEqual(r.status_code, 202, r.text)
        return tc, proc

    def timeline(self, tc):
        return [row["text"] for row in tc.get("/api/logs").json()["timeline"]]

    def test_it_stops_the_one_it_started_and_announces_it(self):
        tc, proc = self.started()
        r = tc.post("/api/backend/stop")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["phase"], "stopped")
        self.assertTrue(proc.terminated)
        # 别的标签页也要跟上：那颗开关每个开着的页面都画着
        types = [e["type"] for _, e in self.wb.broker.replay_since(0)]
        self.assertIn("backend_stopped", types)
        # 停完再读一次状态：页面下一次轮询看到的就是它
        st = tc.get("/api/backend").json()
        self.assertFalse(st["running"])
        self.assertFalse(st["launch"]["process_alive"])

    def test_a_backend_we_did_not_start_is_refused_with_a_way_out(self):
        tc = self.make_launchable(running=True)      # 后端在跑，但工作台没起过它
        r = tc.post("/api/backend/stop")
        self.assertEqual(r.status_code, 409)
        self.assertIn("不是工作台这次起的", r.json()["error"])
        self.assertIn("任务管理器", r.json()["error"])

    def test_a_running_job_blocks_the_stop(self):
        """有作业在跑就必须拒绝 —— 停了它就是白跑（2K 一次 11 分钟）。

        这不是防错，是防**丢用户的东西**：队列在 ComfyUI 自己进程的内存里，
        后端一没，那条队列连同正在算的那一步一起消失。
        """
        tc = self.make_launchable(running=True)      # 后端在跑才提得动作业
        tc.post("/api/run", json=self.build_body())
        self.assertTrue(self.wb.store.active(), "前提：得真有一条活着的作业")
        r = tc.post("/api/backend/stop")
        self.assertEqual(r.status_code, 409)
        self.assertIn("还有作业没结束", r.json()["error"])
        # 拒绝里要有**那条作业**是谁，还得给出卡住时的出路
        self.assertIn("任务管理器", r.json()["error"])

    def test_both_refusals_reach_the_log_view(self):
        """拒绝只写在悬停提示里是不够的 —— 那是**刷新就没了**的一份。

        用户点一下没反应，去日志台翻时间线必须能找到原因原文。
        """
        tc = self.make_launchable(running=True)
        tc.post("/api/backend/stop")
        rows = self.timeline(tc)
        self.assertTrue(any("停后端被拒" in t for t in rows), rows[-3:])
        # 逐项细节：是「不是自己起的」那一条，不是别的
        self.assertTrue(any("不是工作台这次起的" in t for t in rows), rows[-3:])

        job_tc = self.make_launchable(running=True)
        job_tc.post("/api/run", json=self.build_body())
        job_tc.post("/api/backend/stop")
        rows = self.timeline(job_tc)
        self.assertTrue(any("还有作业没结束" in t for t in rows), rows[-3:])

    def test_the_start_refusal_is_on_the_timeline_too(self):
        """「起不来」那句话原来在告警板上，现在也归时间线。

        它在页面上只剩下那颗开关变红 + 一句悬停提示 —— 而那两样都不留存。
        """
        tc = self.make_launchable(running=True)      # 端口上有东西 → 起会被拒
        tc.post("/api/backend/start")
        rows = self.timeline(tc)
        self.assertTrue(any("起后端被拒" in t for t in rows), rows[-3:])


class BackendLaunchStatus(LaunchApiBase):
    def test_before_any_launch_it_says_so(self):
        tc = self.make_launchable()
        st = tc.get("/api/backend/launch").json()
        self.assertEqual(st["phase"], "idle")
        self.assertIsNone(st["launch_id"])
        self.assertFalse(st["process_alive"])

    def test_readiness_comes_from_the_backend_not_our_memory(self):
        """后端可能是用户自己开的 —— 没启动过也不影响「它就绪了」。"""
        tc = self.make_launchable()
        st = tc.get("/api/backend/launch").json()
        self.assertFalse(st["backend_ok"])
        self.client.running_flag = True
        st = tc.get("/api/backend/launch").json()
        self.assertTrue(st["backend_ok"])
        self.assertIsNone(st["launch_id"])

    def test_status_carries_the_timeout_so_the_page_can_say_it(self):
        tc = self.make_launchable()
        self.assertGreater(tc.get("/api/backend/launch").json()["ready_timeout_s"], 0)


class BackendLogRoutes(LaunchApiBase):
    def test_log_is_empty_before_anything_happened(self):
        tc = self.make_launchable()
        data = tc.get("/api/backend/log").json()
        self.assertEqual(data["lines"], [])
        self.assertIsNone(data["ready_line"])
        self.assertTrue(data["path"].endswith("backend.log"))

    def test_log_returns_the_tail_and_the_marker_line(self):
        tc = self.make_launchable()
        with open(self.wb.launcher.log_path, "w", encoding="utf-8") as f:
            f.write("loading\n")
            f.write("To see the GUI go to: http://127.0.0.1:8188\n")
        data = tc.get("/api/backend/log").json()
        self.assertEqual(data["lines"][-1],
                         "To see the GUI go to: http://127.0.0.1:8188")
        self.assertIn("To see the GUI", data["ready_line"])

    def test_lines_query_param_is_honoured_and_capped(self):
        tc = self.make_launchable()
        with open(self.wb.launcher.log_path, "w", encoding="utf-8") as f:
            f.write("".join("line %d\n" % i for i in range(50)))
        data = tc.get("/api/backend/log?lines=10").json()
        self.assertEqual(len(data["lines"]), 10)
        self.assertEqual(data["lines"][-1], "line 49")
        self.assertEqual(data["total"], 50)

    def test_a_bogus_lines_param_does_not_500(self):
        tc = self.make_launchable()
        with open(self.wb.launcher.log_path, "w", encoding="utf-8") as f:
            f.write("x\n")
        self.assertEqual(tc.get("/api/backend/log?lines=abc").status_code, 200)


class BackendCarriesLaunchState(ApiBase):
    def test_backend_endpoint_exposes_the_launch_state(self):
        """判据 6 的页面上要能同时看到「后端在不在」与「我刚点的那次启动怎么样了」。"""
        tc = self.make()
        be = tc.get("/api/backend").json()
        self.assertIn("launch", be)
        self.assertEqual(be["launch"]["phase"], "idle")
        self.assertIn("ready_timeout_s", be["launch"])

    def test_the_launch_state_carries_the_layout_the_page_must_display(self):
        """落点方案必须在**点之前**就读得到。

        页面据此先说清代价（受限模式下临时 / 用户 / 输入目录落在工作区，模型下载与
        节点安装不可用）。少了它，用户只能在撞上之后才发现 —— 而那正是没有症状的错。
        """
        layout = self.make().get("/api/backend").json()["launch"]["layout"]
        self.assertIn(layout["mode"], ("normal", "confined"))
        self.assertEqual(len(layout["probes"]), 2)
        for probe in layout["probes"]:
            self.assertIn("what", probe)
            self.assertIn("ok", probe)
        if layout["mode"] == "confined":
            self.assertTrue(layout["note"], "受限就必须有话说")
            self.assertTrue(layout["flags"], "受限就必须真的改落点")
        else:
            # 不受限的机器上一个字节都不许改
            self.assertEqual((layout["flags"], layout["env"], layout["note"]),
                             ([], {}, ""))


class FrontendContract(ApiBase):
    """本机没有可驱动的浏览器，所以前端最容易犯的两类错只能这样守：
    拼错元素 id、拼错事件名。两种都**静默失效** —— 页面上啥也不报，就是不动。
    跨语言的契约在这里对照一遍，比肉眼看代码可靠。
    """

    def read(self, *parts):
        with open(os.path.join(config.STATIC_DIR, *parts), encoding="utf-8") as f:
            return f.read()

    def test_every_id_the_js_looks_up_exists_in_the_html(self):
        html = self.read("index.html")
        js = self.read("app.js")
        # 有些元素是 renderLive() 之后才注入的（例如取消按钮），它们的 id
        # 只出现在 app.js 自己的模板串里 —— 一并算作已声明。
        declared = set(re.findall(r'id="([^"]+)"', html))
        declared |= set(re.findall(r'id="([^"]+)"', js))
        used = set(re.findall(r'\$\("([^"]+)"\)', js))
        used |= set(re.findall(r'getElementById\("([^"]+)"\)', js))
        self.assertEqual(sorted(used - declared), [],
                         "app.js 引用了 index.html 里不存在的元素 id")

    def test_every_event_type_the_js_subscribes_to_is_emitted_somewhere(self):
        js = self.read("app.js")
        # 只认 SSE 订阅面：本地 on() 助手与 es.addEventListener。
        # 别写成泛化的 addEventListener —— 那会把 change/input 这些 DOM
        # 原生事件也算进来，变成假报警。
        subscribed = set(re.findall(r'\bon\("([a-z_]+)"', js))
        subscribed |= set(re.findall(r'es\.addEventListener\("([a-z_]+)"', js))
        self.assertIn("snapshot", subscribed)
        self.assertIn("progress", subscribed)

        emitted = set()
        here = os.path.dirname(os.path.abspath(__file__))
        # 发事件的地方**每个都要列进来** —— 漏一个文件，这个检查就会把
        # 它发的事件误报成「从不发出」。P6 的四个后端启动事件由 launch.py 发，
        # 第一版没有它，于是 backend_log/ready/failed 全被当成「服务端从不发出」。
        for name in ("watcher.py", "server.py", "launch.py"):
            with open(os.path.join(os.path.dirname(here), "workbench", name),
                      encoding="utf-8") as f:
                emitted |= set(re.findall(r'"type":\s*"([a-z_]+)"', f.read()))
        self.assertEqual(sorted(subscribed - emitted), [],
                         "app.js 订阅了服务端从不发出的事件类型")

    def test_js_parses_at_all(self):
        """`node --check` 就是真的语法检查 —— 本机有 node，用它。

        前端在本机没法真跑（没有可驱动的浏览器），而**一个语法错误**
        会让整页静默失效：所有按钮都不响应，控制台之外什么都看不到。
        id/事件名的字符串对账查不出这个。
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("本机没有 node，跳过语法检查")
        js = os.path.join(config.STATIC_DIR, "app.js")
        # node 往管道里写的是 UTF-8；显式指定，不靠 locale 猜 —— 父进程跑在
        # UTF-8 模式下时，`text=True` 的默认值会跟着变，而子进程不会。
        proc = subprocess.run([node, "--check", js], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=60)
        self.assertEqual(proc.returncode, 0,
                         "app.js 语法错误：\n%s" % (proc.stderr or "")[-1500:])

    def test_launch_controls_are_wired(self):
        """后端开关的元素与事件契约。与上传/扫描同理：拼错 id 的症状是「点了没反应」。

        2026-10-02（P20i）之前这一条守的是那块告警板里的「一键启动」；现在守的是
        顶栏那颗开关，**外加**它的两件配套：命令在日志台里那份、以及「停」那条事件。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        self.assertIn('id="be-switch"', html)
        self.assertIn('$("be-switch")', js)
        # 复制启动命令那颗键是 `renderLogEnv()` 建出来的（在日志台的「环境体检」里），
        # 所以它在 index.html 里没有 —— 两边各自的落点分开判，别混成一件事。
        self.assertIn('id="btn-copy-cmd"', js)
        self.assertIn('$("btn-copy-cmd")', js)
        self.assertIn('id="log-cmd"', js)
        # 起、停各一条端点，都从页面发出去
        self.assertIn('postJSON("/api/backend/start", {})', js)
        self.assertIn('postJSON("/api/backend/stop", {})', js)
        # 四条启动/停止事件都要被订阅。**`backend_log` 不在里面**（P20i 起）：
        # 启动那段 stdout 的正文归日志台的「后端日志」那一栏，它读的是同一份文件；
        # 页面里再收一份就只是同一件事的第二个副本（而它一条条地推，最费的是刷新）。
        for ev in ("backend_launching", "backend_ready", "backend_failed",
                   "backend_stopped"):
            with self.subTest(event=ev):
                self.assertIn('on("%s"' % ev, js)

    def test_the_backend_switch_carries_no_text_and_the_alert_panel_is_gone(self):
        """那颗开关**上面不许有文字**，而告警板要**真的拆干净**（P20i）。

        用户要的形状是一颗开关：状态看颜色与滑块位置，细节看悬停提示。
        于是「开关上没有文字」是一条判据，不是审美 —— 往里塞一行「后端未运行」
        就等于把撤掉的那块板子搬到了顶栏上。

        拆干净那一半同样要判：告警板的三样东西（命令 / 启动日志 / 模型芯片）
        **各自有去处**（日志台），但它们的旧 id 一个都不许留在页面里 ——
        留着的样式与查找就是没人用的死代码，而下一次改这块时它会误导人。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        css = self.read("style.css")

        m = re.search(r'<button[^>]*id="be-switch"[^>]*>(.*?)</button>', html, re.S)
        self.assertIsNotNone(m, "顶栏那颗开关没找到")
        self.assertEqual(re.sub(r"<[^>]*>", "", m.group(1)).strip(), "",
                         "开关上出现了文字 —— 状态该走颜色与悬停提示")

        # 它得**听上去**是一颗开关：role + aria-checked 都由 app.js 随状态维护，
        # 名字（读屏用）走 aria-label —— 没有可见文字时这是唯一的名字来源。
        self.assertIn('role="switch"', m.group(0))
        self.assertIn("aria-checked", m.group(0))
        self.assertIn("aria-label", m.group(0))
        self.assertIn('setAttribute("aria-checked"', js)
        self.assertIn('setAttribute("aria-label"', js)
        # 状态是**算出来**的：六档一个不少，别只剩「跑着 / 没跑」两档 ——
        # 「在跑但不是我们起的」（关不掉它）与「在跑但缺模型」是两件不同的事。
        for state in ("checking", "off", "starting", "on", "degraded",
                      "external", "error"):
            with self.subTest(state=state):
                self.assertIn('"%s"' % state, js)

        for gone in ("backend-warn", "btn-start-backend", "bw-cmd", "bw-hint",
                     "bw-title", "bw-models", "bw-launch", "launch-state",
                     "launch-log", "launch-note", "btn-launch-log", "be-ver",
                     "be-dot"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html)
                self.assertNotIn(gone, js)
        for dead in (".model-chip", ".warn-head", ".backend-status", ".launch-note",
                     ".launch-head", ".dot "):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, css)

    def test_the_start_command_still_has_a_home_on_the_page(self):
        """那条命令**只是搬家，不是删掉**（P20i）。

        铁律 1 那条命令（`config.start_command()`）是后端起不来时唯一能自己动手的
        东西，而且它的唯一真源在后端 —— 页面上不许再拼第二份。它现在的落点是
        日志台的「环境体检」：一行命令 + 一颗复制键。
        """
        js = self.read("app.js")
        self.assertIn("be.start_command", js)          # 从后端拿，不自己拼
        self.assertIn("启动命令", js)
        # 复制失败要有第二条路（选中全文自己按 Ctrl+C）：这条命令是「后端起不来」
        # 时唯一的出路，一个闪一下就没了的按钮在这里等于没有出路。
        body = js.split("async function copyStartCommand() {")[1].split("\n}")[0]
        self.assertIn("selectNodeContents", body)
        self.assertIn("copyText", body)
        # 「复制全部」那份也要带上它 —— 那一份是拿去贴给别人或在别处重建现场的
        self.assertIn("== 启动命令 ==", js)

    def test_the_boot_script_row_is_drawn_from_the_backend(self):
        """开机脚本那一行（P22）**用后端给的文案**，页面不自己拼。

        三条起后端的路里，只有这一条没有版本保护（跑的是安装目录里那个 `.bat`，
        不在本仓库），所以判据与文案都只能有一处真源。页面上出现变量名、或者
        自己去看 `boot.missing`，就等于第二份说法已经长出来了 —— 那正是
        「两边悄悄分家」的形状。

        **`present` 判据不能省**：开发机上根本没有那个 `.bat`，那一行不该画。
        一份「永远画、永远写『不同源』」的体检比没有还坏 —— 它会教人忽略它。
        """
        js = self.read("app.js")
        # 注释里正好会提到这些名字（那段注释就是在说「为什么要钉住它们」），
        # 所以先把整行注释剥掉再判。
        code = re.sub(r"^\s*//.*$", "", js, flags=re.M)
        self.assertIn("be.boot_script", code)
        self.assertIn("if (boot.present)", code)
        self.assertIn("boot.summary", code)
        self.assertNotIn("boot.missing", code)
        self.assertNotIn("boot.wrong", code)
        for word in ("HF_ENDPOINT", "HF_HOME", "TORCH_HOME", "huggingface"):
            with self.subTest(word=word):
                self.assertNotIn(word, code)

    def test_sweep_controls_are_wired(self):
        """扫描那一块的元素契约。与上传同理：拼错 id 的症状是「点了没反应」。"""
        html = self.read("index.html")
        js = self.read("app.js")
        for eid in ("sweep-panel", "sweep-seeds", "sweep-steps", "btn-sweep",
                    "sweep-msg", "sweep-grid", "sweep-hint"):
            with self.subTest(eid=eid):
                self.assertIn('id="%s"' % eid, html)
        for eid in ("sweep-seeds", "sweep-steps", "btn-sweep", "sweep-msg",
                    "sweep-grid"):
            with self.subTest(eid=eid):
                self.assertIn('$("%s")' % eid, js)
        # 两个扫描事件必须真的被订阅（它们也是第二个标签页唯一的通知来源）
        self.assertIn('on("sweep_queued"', js)
        self.assertIn('on("sweep_failed"', js)

    def test_upload_controls_referenced_by_the_js_all_exist_in_the_html(self):
        """上传那一块的元素契约 —— 拼错 id 的症状是「点了没反应」。

        2026-09-30 起 `tests/page_shot.py` 能真的把页面渲染出来并驱动它，所以
        「没有可驱动的浏览器」不再是这一块单列的理由。留着它是因为它**几十毫秒
        就能跑**，而截图要好幾秒；而且它守的是**全部** id，截图只看得见我特意
        拍的那几个状态。

        注意 `ref-thumb` / `ref-name` **不再是 id**：多张参考图之后它们是
        **每一块**都有的类（`.ref-chip > .ref-thumb`），id 只能有一个，
        挂在第一块上就成了「第一张是特例」—— 而特例迟早会分家。

        `source_image` 与 `src-options`（手打文件名那个输入框 + datalist）
        **已经整条去掉**：选中集搬进了 `state.refs`。这里反向守一道 ——
        它们要是被加回来，说明有人在页面上又开了第二条写入路径。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        for eid in ("src-file", "upload-msg", "src-info", "confirm-hint",
                    # 虚线框那一套：框 + 框下面那排方块 + 清空
                    "ref-drop", "ref-empty", "ref-picked", "ref-drop-hint",
                    "ref-drop-note", "btn-clear-ref",
                    # 反向词
                    "negative", "negative-wrap", "negative-hint", "negative-len",
                    "btn-raise-cfg",
                    # 成果视图的滑块
                    "result-thumb"):
            with self.subTest(eid=eid):
                self.assertIn('id="%s"' % eid, html)
        for eid in ("upload-msg", "src-info", "confirm-hint",
                    "src-file", "ref-drop", "ref-picked", "ref-drop-note",
                    "btn-clear-ref", "negative", "negative-hint",
                    "btn-raise-cfg", "result-thumb"):
            with self.subTest(eid=eid):
                self.assertIn('$("%s")' % eid, js)
        # 手打文件名那条路：HTML 里没有，JS 里也不许再去取
        for gone in ("source_image", "src-options"):
            with self.subTest(gone=gone):
                self.assertNotIn('id="%s"' % gone, html)
                self.assertNotIn('$("%s")' % gone, js)

    def test_the_reference_chips_are_classes_not_ids(self):
        """多张参考图之后，每一块的图/角标是**类**，不是 id。

        这一条守的是那次改动的**理由**：id 在一个文档里只能有一个，于是
        「多张」就只能做成「第一张有 id、其余没有」—— 那种分岔在 CSS 与
        选择器里会各漏一次（改了一个忘了另一个），而且没有任何症状。
        """
        js = self.read("app.js")
        for cls in ('class="ref-chip', 'class="ref-thumb"',
                    'class="ref-order"', 'class="ref-remove"'):
            with self.subTest(cls=cls):
                self.assertIn(cls, js)
        for gone in ('$("ref-thumb")', '$("ref-name")', '$("ref-remove")',
                     '$("ref-order")'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, js)
        # 文件名从方块上**去掉了**（改成 title/aria-label），所以那个类也不该
        # 再被渲染出来 —— 留着它，CSS 里那条规则就成了没人用的死代码。
        self.assertNotIn('class="ref-name"', js)
        self.assertNotIn(".ref-name", self.read("style.css"))

    def test_the_gallery_reads_three_separate_actions(self):
        """画廊格子的三个动作各有各的入口 —— 这是 2026-10-01 那轮改动的**目的**。

        上一版整块格子是一个 <button>，点哪儿都是「载入参数」：想放大看这张图、
        想把它的提示词填回表单，都没有入口。所以这里守两件事：三个动作名都在
        （`zoom` / `loadprompt` / `load`），而且放大视图那一套 id 齐全
        （`data-act` 拼错的症状同样是「点了没反应」）。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        for act in ('data-act="zoom"', 'data-act="loadprompt"',
                    'data-act="load"'):
            with self.subTest(act=act):
                self.assertIn(act, js)
        for eid in ("lightbox", "lb-stage", "lb-img", "lb-name", "lb-meta",
                    "lb-prompt", "lb-zoom", "lb-copy", "lb-copy-negative",
                    "lb-negative", "lb-negative-wrap", "lb-copy-params",
                    "lb-load-prompt",
                    "lb-load", "lb-prev", "lb-next", "lb-close", "lb-status"):
            with self.subTest(eid=eid):
                self.assertIn('id="%s"' % eid, html)
                self.assertIn('$("%s")' % eid, js)
        # 放大视图用**真 <dialog>**：Esc、焦点陷阱、背后 inert 都是浏览器给的。
        # 换成自己搭的遮罩，这三样都要各写一遍，而漏掉哪一样都没有症状。
        self.assertIn("dlg.showModal()", js)
        self.assertIn("<dialog", html)

    def test_the_ref_remove_button_reveals_on_hover_and_stays_in_the_tab_order(
            self):
        """`×` 平时不显示、悬停或聚焦才出现（2026-10-01 按用户要求）。

        三件事一起守，少一件都有症状：
          · 隐藏只许用 `opacity` —— `display`/`visibility` 会把键从**键盘的
            Tab 顺序**里摘掉，于是「删掉这张参考图」对键盘用户彻底消失，
            而且没有症状（鼠标用户一切正常）；
          · `:hover` 管鼠标、`:focus-within` 管键盘；
          · 整条藏在 `@media (hover: hover) and (pointer: fine)` 里 ——
            触摸屏上**没有悬停这回事**，藏起来等于这个键不存在。
        """
        css = self.read("style.css")
        self.assertIn("@media (hover: hover) and (pointer: fine)", css)
        self.assertIn(".ref-remove { opacity: 0;", css)
        self.assertIn(".ref-chip:hover .ref-remove", css)
        self.assertIn(".ref-chip:focus-within .ref-remove", css)
        hide = css.split(".ref-remove { opacity: 0;")[1].split("}")[0]
        self.assertNotIn("display", hide)
        self.assertNotIn("visibility", hide)

    def test_the_gallery_picks_pages_and_deletes(self):
        """画廊的多选 / 全选 / 分页 / 删除（P20f）。

        真行为归 `page_shot` 第 23 步（那里量的是真布局与真点击），这一层守的是
        **形状**里最容易悄悄坏掉的五条：

        1. 「全选」只吃**本页的格子**（`#gallery .tile`），不是 `state.gallery.items`
           —— 后者可能装着一百多张，而屏幕上只有一页；
        2. **选中集活不过一次刷新**（`fetchGallery` 里那次 `clear()`）—— 留下来
           就会出现「看不见、但已经被勾中」的图，而下一步是删除；
        3. 勾选框**不在** `.tile-shot` 那颗 `<button>` 里面（`<button>` 里塞
           `<input>` 是非法结构，而且点勾选框会顺手打开放大视图）；
        4. 删除**不弹确认框**（2026-10-02 定的：按钮上已经写着张数，进的是回收站，
           而回收站是能还原的）—— 真去弹一个 `window.confirm` 会让这一步变成
           「每次都点两下」，而那是这一轮明确否掉的形状；
        5. 页大小是**实测量出来的**，而且量不到时**不许猜**：面板藏着时所有格子的
           `offsetTop` 都是 0，第一排就是全部格子时那排可能没排满 —— 两种情况下
           量出来的「列数」都是假的，而假的列数会让「每页 3 行」悄悄失效。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        for eid in ("gallery-picks", "btn-pick-all", "btn-pick-none", "btn-delete",
                    "gallery-msg", "gallery-pager", "gallery-page-info",
                    "btn-page-prev", "btn-page-next"):
            with self.subTest(eid=eid):
                self.assertIn('id="%s"' % eid, html)
        # `gallery-picks` 只是样式挂钩（那一行的排版），JS 不碰它 —— 所以它不在
        # 下面这张表里。剩下的每一颗都必须真的被接线，否则就是「点了没反应」。
        for eid in ("btn-pick-all", "btn-pick-none", "btn-delete", "gallery-msg",
                    "gallery-pager", "gallery-page-info", "btn-page-prev",
                    "btn-page-next"):
            with self.subTest(eid=eid):
                self.assertIn('$("%s")' % eid, js)
        # 1. 全选 = 本页
        pick = js[js.index("function pickAllOnPage()"):js.index("function galleryMsg(")]
        self.assertIn('$("gallery").querySelectorAll(".tile")', pick)
        self.assertNotIn("state.gallery.items", pick)
        # 2. 选中集活不过一次刷新
        fetch = js[js.index("async function fetchGallery()"):
                   js.index("function syncPickUI()")]
        self.assertIn("state.galleryPick.clear()", fetch)
        # 3. 勾选框不是那颗按钮的子元素
        self.assertLess(js.index('<label class="tile-pick"'),
                        js.index('class="tile-shot"'),
                        "勾选框被排到 .tile-shot 之后了 —— 图会盖住它，点下去命中的是"
                        "「放大」，而且两个都不会报错")
        # 4. 不弹确认框。P20h 起这个批量函数改叫 `pickedAction(kind)`（归档 / 还原 /
        # 彻底删除**共用这一条路**）—— 三种动作各写一份的话，报账口径迟早分家，
        # 而它们全都会动磁盘上的真文件。
        for fn in ("async function pickedAction(kind)",
                   'PICK_ACTIONS[kind]'):
            with self.subTest(fn=fn):
                self.assertIn(fn, js)
        self.assertEqual(js.count("async function pickedAction("), 1,
                         "批量那一条路只许有一条：多一条就多一份口径")
        delete_fn = js[js.index("async function pickedAction(kind)"):
                       js.index("function renderGallery(")]
        self.assertNotIn("confirm", delete_fn)
        self.assertIn("postJSON(", delete_fn)
        # 发出去的是**回执里的字段**，不是去解析 id 字符串（那是内部约定）
        self.assertIn("it.filename", delete_fn)
        self.assertNotIn("split(\":\")", delete_fn)
        # 5. 量不到就不猜
        cols = js[js.index("function galleryCols()"):
                  js.index("function galleryPageSize()")]
        self.assertIn("offsetParent", cols)
        self.assertIn("n === tiles.length", cols)
        # 6. 勾选框**不许垫底色**（2026-10-02 用户当场指出「选中的图像背后会有一个
        # 黑框」）：选中态自己的蓝底在任何图上都读得出来，垫一块深色只是给它在
        # 蓝框外再套一个框。这一条是防它被「为了白底图上看得见」再垫回来 ——
        # 那个理由实测不成立。
        css = self.read("style.css")
        rule = css[css.index(".tile-pick {"):css.index(".tile-pick input")]
        self.assertNotIn("background", rule)

    def test_a_repeat_submission_says_what_it_really_did(self):
        """「原参数重复生图：秒完成、没有图」这一条（P20g）。

        机制在 ComfyUI 那边：缓存认的是**节点输入**，一字不改地再提一遍它一个节点
        都不跑，只把上一次的产物名回放给 history。所以修法落在两个地方，两边都得
        钉住 —— 它们的失效方式都是**没有症状**：

        1. 服务端每次提交换一次收尾前缀（`qb.unique_save_prefix`）：不换，磁盘上
           什么都不多，而页面写着「完成」；
        2. 页面把「这次没有重新采样」说出来：不说，用户看到的就是 0 秒 + 一片空白；
        3. 那个产物名对应的文件**可能已经不在磁盘上**（P20f 的删除是进回收站）——
           破图不许自己一个人破着，得说清是什么、下一步怎么办。

        判据落在「两处用的是**同一条**判据」上：同一份真相不能一边一个说法。
        真行为归 `page_shot` 第 24 步（真浏览器里那张图真的 404、真的翻出话来）。
        """
        js = self.read("app.js")
        self.assertIn("没有重新采样", js)
        # 收尾节点按**类名**认，不按 id（id 是我们自己铸的，见 builder 那条规矩）
        self.assertIn('const SAVE_CLASSES = new Set(["SaveImage", "SaveImageAdvanced"])', js)
        # 卡片与耗时面板走同一个判据 —— 少一处，另一处就会说反话
        self.assertIn("cacheVerdictOfJob(job)", js)
        self.assertIn("cacheVerdict(rows.map(", js)
        # 判据只认一件事：**除收尾之外还有没有节点真的跑过**
        verdict = js[js.index("function cacheVerdict(items)"):
                     js.index("function cacheVerdictOfJob(job)")]
        self.assertIn("SAVE_CLASSES.has(i.cls)", verdict)
        self.assertIn("upstream.every((i) => i.cached)", verdict)
        # 产物文件不在了：由浏览器给的 error 触发，写法只有一处
        self.assertIn("markShotGone(shot, job)", js)
        self.assertIn('img.classList.add("broken")', js)
        css = self.read("style.css")
        self.assertEqual(
            css.count("repeating-linear-gradient(45deg, transparent 0 8px"), 1,
            "「取不到这张图」的斜纹只能写一处：参考图 / 产物 / 缩略图各写一份，"
            "迟早会分家，而分家之后没有任何症状")

    def test_archived_is_a_stage_not_a_filter_and_it_says_so(self):
        """「删了就从最近任务里移除」这一条（P20h）。

        归档把「删掉」变成「搬到 `output-archived/`」，于是五件事必须**同时**成立，
        而它们各自的失效方式都是没有症状：

        1. 那一档是**另一档**（换的是扫哪个目录），不是又一颗筛选 chip —— 写成 chip
           会变成「在 output/ 里挑出已归档的」，而那批图根本不在 output/ 里；
        2. 正常档那颗按钮打 `/api/gallery/archive`（不再打 delete），归档档里换成
           「还原 / 彻底删除」两颗 —— 同一颗按钮，两档两件事；
        3. 归档档取图**也要换根**（`&archived=1`）：不换，每一格都 404，更坏的一种是
           取到 output/ 里同名的另一张（同名是真实会发生的，见 `gallery.restore_target`）；
        4. 最近任务里那条记录**不画**，判据来自后端（P20j 起是 `job.gone`，见下面那条
           契约测试）—— 页面不许自己猜磁盘（猜错就是「明明归档了还画着一行取不到图
           的记录」）；
        5. 大卡片上，归档的那张**不许再画一张取不到的图** —— 那会走「多半是被删了」
           那句话，把「我们搬走了」说成「没了」，还会把用户支去回收站找。

        真行为归 `page_shot` 第 25 步（真浏览器里真点那两颗按钮、真看那一档的图）。
        """
        js = self.read("app.js")
        html = self.read("index.html")
        css = self.read("style.css")
        # ① 换的是根，不是筛选：清单与取图**两条路**都得带上
        self.assertIn('params.set("archived", "1")', js)
        # 这一行原来逐字钉的是 `state.galleryArchived ? "&archived=1" : ""`。
        # P21d 给 `galleryUrl()` 加了第三个数：**归档档的显式覆盖**。原因是放大视图里
        # 画的那些产物（H3 的视频、拆层的几十张层图）在**当前**输出目录里，与画廊正
        # 翻到哪一档毫无关系 —— 跟着 `state.galleryArchived` 走的话，「翻过归档档之后
        # 再点开一个作业」会去归档目录里找这张图，而它就在眼前那个目录里。
        #
        # 判据因此改成钉**性质**而不是钉那一行的写法：
        #   · 默认仍由 `state.galleryArchived` 决定（正常档不许凭空变成归档档）；
        #   · `&archived=1` 全集只许出现**一次** —— 各写一份的话，它迟早只加在一处，
        #     而症状正是「归档档里格子全 404、放大视图却是好的」。
        self.assertIn("archived === undefined ? state.galleryArchived : archived", js)
        self.assertEqual(js.count('"&archived=1"'), 1)
        # 产物那一路显式传 `false`（第二个参数是「画缩略图」）
        self.assertIn("galleryUrl({ root: art.root, filename: l.filename }, true, false)",
                      js)
        # ② 同一颗按钮两档两件事，分支写在**绑定**那一处（读状态的写法最容易分家）
        self.assertIn('$("btn-delete").onclick = () => pickedAction(', js)
        self.assertIn('state.galleryArchived ? "purge" : "archive"', js)
        self.assertIn('$("btn-restore").onclick = () => pickedAction("restore")', js)
        self.assertIn('id="btn-archived"', html)
        self.assertIn('id="btn-restore"', html)
        self.assertIn(">归档选中的 0 张<", html)      # 初始文案就是「归档」，不是「删除」
        self.assertIn('(archived ? "彻底删除选中的 " : "归档选中的 ")', js)
        # 那颗开关打开时得看得见（与 chip 同一套配色，见 style.css 里那段）
        self.assertIn("button.ghost.on", css)
        # ③ 三个端点各就各位；还原 / 彻底删除**不给 root**（归档里的东西只有一个来源）
        for url in ('"/api/gallery/archive"', '"/api/gallery/restore"',
                    '"/api/gallery/purge"'):
            with self.subTest(url=url):
                self.assertIn(url, js)
        body = js[js.index("async function pickedAction(kind)"):
                  js.index("function renderGallery(data)")]
        self.assertIn("{ name: it.filename }", body)
        self.assertIn("? { root: it.root, name: it.filename }", body)
        # ④ 最近任务：产物取不到的那条不画。判据是后端给的（P20j 起是 `job.gone`，
        # 换判据的原因见 `test_the_jobs_list_and_the_cleaning_follow_the_backend_verdict`）
        self.assertIn("sortedJobs().filter((j) => !j.gone)", js)
        # ⑤ 大卡片：归档分支里不许出现 markShotGone（那是「文件不见了」那一件事的写法）。
        # 判据落在**代码**上：注释里正好会提到这个名字（那段注释就是在说「别走它」）。
        def code(frag):
            frag = re.sub(r"/\*.*?\*/", "", frag, flags=re.S)
            return re.sub(r"//[^\n]*", "", frag)

        start = js.index("if (job.archived) {")
        branch = code(js[start:js.index("} else {", start)])
        self.assertIn("这张已经归档", branch)
        self.assertIn("它没有被删掉", branch)
        self.assertNotIn("markShotGone", branch)
        # 那颗按钮的名字从 `data-name` 取，不许去爬旁边那几段文字
        self.assertIn("btn.dataset.name", js)
        self.assertIn('id="btn-unarchive"', js)

    def test_the_jobs_list_and_the_cleaning_follow_the_backend_verdict(self):
        """「产物取不到的记录不画 + 可以清理」这一条（P20j）。

        三处必须同时成立，而它们各自的失效方式都没有症状：

        1. 最近任务画不画那一行，判据是**后端现算的 `job.gone`**（有产物、但一张都
           取不到）。旧的 `job.archived` 漏了「归档过、又被彻底删除」那一格 ——
           2026-10-03 用户报的就是它：彻底删掉之后那一行**重新冒出来**，还带着一张
           取不到的图。页面不许自己猜磁盘（它也猜不到：`img.onerror` 只知道图没画出来）；
        2. 那颗「清理」存在、接线了，**没得清时禁用**，射程由后端给（`/api/jobs` 的
           `stale`）—— 页面不自己数；清完按**回执**对账（`upsert` 只加不删，
           不按回执删的话被清掉的行会赖在页面上不走）；
        3. 破图那句话**不许把「后端没起」说成「文件被删了」**：那张缩略图是经
           `/api/image` → 后端 `/view` 取的，后端不在时每一条都会走这条路 ——
           旧文案会对着好端端的文件说「多半是被删了」，还把用户支去回收站找。

        真行为归 `page_shot` 第 27 步（真浏览器里真点那颗按钮，请求打桩）。
        """
        js = self.read("app.js")
        html = self.read("index.html")
        self.assertIn("sortedJobs().filter((j) => !j.gone)", js)
        self.assertNotIn("sortedJobs().filter((j) => !j.archived)", js)
        # 那颗按钮：在页面上、接线了、没得清就禁用并说出理由
        self.assertIn('id="btn-clean-jobs"', html)
        self.assertIn('id="clean-note"', html)
        self.assertIn('$("btn-clean-jobs").onclick = cleanJobs;', js)
        self.assertIn("btn.disabled = n === 0;", js)
        self.assertIn('postJSON("/api/jobs/clean"', js)
        self.assertIn("for (const id of r.removed || []) state.jobs.delete(id);", js)
        # 按钮上的数字与禁用态**在提前 return 之前**刷 —— 全都取不到图时列表是空的，
        # 而那正是这个数字最该说得清的时候
        body = js[js.index("function renderJobs()"):
                  js.index("function renderJobsClean()")]
        self.assertIn("renderJobsClean();", body)
        self.assertLess(body.index("renderJobsClean();"), body.index("if (!rows.length)"))
        # 破图：三种原因分开说，而且判据来自后端（页面手里没有磁盘）
        gone = js[js.index("function markShotGone(img, job)"):
                  js.index("async function restoreOne(btn)")]
        self.assertIn("job.gone", gone)
        self.assertIn("state.backend", gone)
        self.assertIn("后端没在跑", gone)
        # 旧那句「多半是被删了」不许留在**代码**里 —— 一律拔掉会把「为什么不能这么
        # 写」那句注释也判红（它正当，而且正是这行代码的历史）。
        body = re.sub(r"/\*.*?\*/", "", gone, flags=re.S)
        body = re.sub(r"//[^\n]*", "", body)
        self.assertNotIn("多半是被删了", body)

    def test_the_tiles_draw_thumbnails_while_everything_else_gets_the_original(self):
        """格子画缩略图，**用这张图的地方**一律拿原图（P20l）。

        2026-10-06 用户报的是「画廊作品像是全量加载，有分辨率较高的作品时页面卡顿」。
        实测：现场一页 18 格的原图合计 **65.4 MB**（最大一张 4096² / 18.5 MB），每张
        都要整份解码；同一页缩略图合计 0.71 MB。

        三条判据，各自对应一处**会悄悄分家**的地方：

        ① 格子的 `<img src>` 是缩略图那个口，放大视图是原图那个口；
        ② 两个口由**同一个函数**拼（参数一模一样）—— 各写一份的话，`&archived=1`
           迟早只加在一处，症状是「归档档里格子全 404，而放大视图是好的」；
        ③ **拖进来的缩略图要改写回原图**：拿 512px 的 JPEG 去当参考图是**没有症状**
           的降级 —— 传得上去、出得来图，只是少了一半像素，而用户以为自己拖的就是
           那一张原图（这条曾经真的坏过，坏法是名字被合成成 `image.png`）。

        真行为归 `page_shot` 第 28 步（真浏览器里量格子 `<img>` 的自然尺寸）。
        """
        js = self.read("app.js")
        # ① 两个口各就各位
        tiles = js[js.index("function renderGallery(data)"):
                   js.index("function scheduleGalleryFit()")]
        self.assertIn("galleryThumbUrl(it)", tiles)
        self.assertNotIn("galleryImageUrl(it)", tiles,
                         "格子画原图 = 这一轮的卡顿原封不动回来了")
        lightbox = js[js.index("function renderLightbox()"):
                      js.index("function setLightboxZoom(on)")]
        self.assertIn("galleryImageUrl(it)", lightbox)
        self.assertNotIn("galleryThumbUrl(it)", lightbox,
                         "放大视图（含 1:1 原始像素）必须是原图")
        # ② 同一个拼法：两个口只是路径不同，参数不许各写一份
        self.assertEqual(js.count('"/api/gallery/" + (thumb ? "thumb" : "image")'), 1)
        self.assertEqual(js.count("function galleryThumbUrl(it)"), 1)
        self.assertEqual(js.count("function galleryImageUrl(it)"), 1)
        # ③ 拖进来那条路：认这个口，而且**把地址改写回原图**
        ref = js[js.index("function refFromUri(uri)"):
                 js.index("async function takeDropped(dt)")]
        ref = re.sub(r"/\*.*?\*/", "", ref, flags=re.S)
        ref = re.sub(r"//[^\n]*", "", ref)
        self.assertIn('"/api/gallery/thumb"', ref)
        self.assertIn('url.pathname = "/api/gallery/image"', ref)

    def test_the_page_says_so_when_its_scripts_never_ran(self):
        """「脚本没跑起来」这一页必须自己喊出来（P20k）。

        2026-10-03 用户看到的是一张冷脸：右上角「连接中…」永远不变、**点哪儿都没反应**
        （后端开关、日志、提交全一样）。原因是浏览器手里那份 `app.js` 与这一页不是
        同一版（或者是它压根没加载成功），于是 `init()` 在第一行就抛，后面一行都不跑。
        这一类坏法**没有任何症状** —— 页面上没有任何东西会抱怨，用户只能来问人。

        三处必须同时成立（缺哪一处横幅都不会出现）：
        ① 守卫要排在 `app.js` **之前**（脚本在后面才加载，晚于它就收不到加载失败了）；
        ② `init()` 跑完必须置 `window.__wb_booted`（否则横幅会误报好页面）；
        ③ 横幅要给**下一步动作**（Ctrl+Shift+R）与**捕获到的那句话**，不然它只是
           把「坏了」换个方式说了一遍。

        真行为归 `logs/ui_hybrid_probe.py`：它把两种坏法（旧 JS + 新 HTML / app.js 404）
        都摆出来，判据是「横幅真的出现且说得出原因」。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        css = self.read("style.css")
        guard = html.index("__wb_boot_errors")
        self.assertLess(guard, html.index("/static/app.js"),
                        "守卫必须排在 app.js 之前")
        self.assertIn('box.id = "boot-warn"', html)
        self.assertIn("Ctrl+Shift+R", html)
        self.assertIn("脚本没跑起来", html)
        # ② 跑完置标记，而且是在 `connect()` 之后（那一步没跑到就说明 init 没跑完）
        tail = js[js.index("  connect();"):js.index("document.addEventListener(\"DOMContentLoaded\"")]
        self.assertIn("window.__wb_booted = true;", tail)
        # ③ 横幅的样式不许依赖任何别的类（那时 app.js 没跑）
        self.assertIn("#boot-warn {", css)

    def test_the_gallery_card_is_an_image_and_badges_only(self):
        """格子上只剩一行小标签（2026-10-01 按用户要求去掉参数 / 提示词 / 来源）。

        这三行不是删掉就完了 —— 它们必须在**别处**有落点，否则是纯粹的丢信息：
        seed / 步数 / 尺寸 / 来源目录 / 完整提示词都在放大视图里
        （`#lb-meta` 与 `#lb-prompt`，另有 `test_the_gallery_reads_three_separate_
        actions` 守那一套 id 齐全）。
        """
        js = self.read("app.js")
        css = self.read("style.css")
        for gone in ('class="tile-sub', 'class="tile-prompt', 'class="tile-origin'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, js)
        # CSS 里那三条规则也必须一起走：留着就是没人用的死代码。
        for dead in (".tile-sub", ".tile-prompt", ".tile-origin"):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, css)
        # 小标签留着（模式 / 耗时 / 2K / 非本链路 / 元数据读不出来）
        self.assertIn('class="tile-badges"', js)
        self.assertIn("非本链路", js)
        self.assertIn("元数据读不出来", js)
        # 参考图 / 源图：**只有放大视图**看得见（格子只剩一行药丸，而「载入参数」
        # 从 2026-10-01 起只拿数值、已经不碰它了）。少了这一行，编辑链路最核心的
        # 那条信息在页面上就彻底没有入口 —— 而那是纯丢信息，不是「减字」。
        lb = js.split("function renderLightbox() {")[1].split("\n}")[0]
        self.assertIn("source_images", lb)
        self.assertIn("源图 ", lb)
        # 「改写」也是**额外功能**的标记（与模式正交），放大视图那行 meta 里要有
        self.assertIn("it.rewrite", lb)

    def test_the_estimate_never_reaches_the_page_but_still_drives_the_gate(self):
        """估时**一个数都不上页面**，但那个数没被删。

        撤过两次：2026-10-01 先撤提交前那一行，然后用户看着进度面板上
        「估算 1 分 08 秒」而实际只要 25 秒，说「去掉估算」—— 于是进度面板与
        「最近任务」里那两个也撤了。

        为什么不留：那个数对「**这台机器此刻有多快**」不敏感 —— 开着 TE-Speed、
        或者显存里还留着上一个模型，实测能差两三倍，而一个偏大的数就摆在真实
        耗时旁边，看起来只是工作台算错了。
        两半分别守：页面上不许再出现这个数（`估算`，注释先剥掉）；而
        `/api/build` 的 `estimate`、任务记录的 `estimate_s`、以及成本预警那道
        门槛（贵的那档要点两下）原样都在。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        css = self.read("style.css")
        self.assertNotIn('id="estimate"', html)
        self.assertNotIn('$("estimate")', js)
        self.assertNotIn("经验估算", html)
        self.assertNotIn(".estimate", css)
        code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        # 注释里还写着「为什么撤」，那不是可见文字 —— 剥掉之后再判
        self.assertNotIn("估算", code, "页面上又出现了估时")
        # 那一栏还在，而且**只画 warn/error**（判据落在 level 上，不列白名单）。
        self.assertIn('id="warnings"', html)
        self.assertIn('w.level !== "info"', js)
        # 那个数还在原处（同一份 estimate 既出成本预警，也记进任务记录）
        self.assertIn("state.build.estimate.confirm", js)

    def test_the_chain_stage_is_drawn_and_does_not_pretend_to_be_progress(self):
        """「出图后放大」那一段画在父作业**下面**，而且不假装那条是真实进度。

        链上的放大是**另一个作业**：父作业那时已经 done（画面上「完成 + 满条」），
        而 GPU 还在忙二三十秒 —— 不给它落点，用户根本看不出还在跑（2026-10-01
        用户报的就是这一条）。
        但 VOSR2 插件**不报进度**（翻过它整个源码，没有一处 `PROGRESS_BAR`），
        能报的只有「正在执行哪个节点」和经过的时间。所以那条只能按时间画，而且
        必须守住两件事：**封顶 95%**（涨到头停住，而不是走满 —— 走满就是我们替
        它编了一个百分比），以及**把这件事写在块里**。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        css = self.read("style.css")
        self.assertIn("chainStage", js)
        # 取消与跳转都指向**子作业**：父作业那时已经完成，顶栏那颗「取消」不在
        self.assertIn("job.chain.job_id", js)
        self.assertIn("data-elapsed-bar", js)
        self.assertIn("Math.min(95", js)
        # 这句话是**可见文字**，钉住它 —— 没有它，那条就成了一个我们编的百分比
        self.assertIn("不是真实进度", js)
        # 它不是第二个进度条元素，是**渲进面板**的一块（所以 HTML 里没有它）
        self.assertNotIn('id="stage-chain"', html)
        self.assertIn(".stage", css)

    def test_the_mode_select_carries_only_shapes(self):
        """`#mode` 只剩三条**形状**（2026-10-01 重构）；配置类的开关在别处。

        这一条守的是**概念别爬回去**：`2k`（分辨率）与 `refine`（改写器）本来都是
        同一张图的**配置**，却占着这个下拉框的两个位置 —— 于是「模式」成了一个什么
        都能往里装的杂物箱，而每次加东西都**没有症状**（页面照跑）。
        新的开关必须有自己的字段和自己的控件。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        sel = html.split('<select id="mode">')[1].split("</select>")[0]
        self.assertEqual(re.findall(r'<option value="([^"]+)"', sel),
                         ["t2i", "edit", "upscale"])
        self.assertNotIn('value="refine"', sel)
        self.assertNotIn('value="2k"', sel)
        # 两个额外功能各自的控件（拼错 id 的症状同样是「点了没反应」）
        for eid in ("rewrite", "rewrite-wrap", "auto_upscale", "auto_scale",
                    "chain-wrap", "sweep-chain-note"):
            with self.subTest(eid=eid):
                self.assertIn('id="%s"' % eid, html)
                self.assertIn('$("%s")' % eid, js)

    def test_module_hints_are_function_only(self):
        """模块提示只写「做什么」（2026-10-01 按用户要求收窄）。

        改动集中在**三处**：高级设置、参数扫描、模式里的放大超分。这三处的提示原本
        混着三类东西：
          · 实测数字（省 66%~70%、+8.8 GB / +113 秒、梯度 1.956 → 0.953）；
          · 对比与理由（「与源图多大几乎无关」「EasyCache 会把画面打烂，所以没接它」）；
          · 自证式解释（「所以它在图生图下不出现」）。
        现在只留功能描述：**一个开关做什么、打开后会发生什么**。数字与证据一行没删，
        它们在 `docs/details.md`（以及本文件旁边的代码注释里）。

        判据落在**形状**上，不逐条比文案 —— 比文案的话，改一句话就得改测试：
          · 这三处的**可见文字**里不许再出现百分比 / 「实测」/ 已知的那几个数字；
          · 每个提示段是**一句话**（去标签后 ≤ 60 字）；判的是「一句话」，不是字数本身；
          · 折叠块的标题不再叫「实测代价 / 放大方式与耗时」；
          · 扫描里那句链的说明只说「不生效」，不再解释理由。
        注释不算可见文字（`text()` 先剥注释）：理由该留在代码里，而不是页面上。
        """
        html = self.read("index.html")
        js = self.read("app.js")

        adv = html.split('id="advanced-wrap"')[1].split('id="sweep-panel"')[0]
        sweep = html.split('id="sweep-panel"')[1].split('id="sweep-grid"')[0]
        ups = html.split('id="upscale-wrap"')[1].split('id="prompt-wrap"')[0]

        def text(frag):
            """可见文字：先剥注释，再剥标签，最后把空白压成一个空格。"""
            frag = re.sub(r"<!--.*?-->", "", frag, flags=re.S)
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", frag)).strip()

        for name, frag in (("高级设置", adv), ("参数扫描", sweep), ("放大超分", ups)):
            with self.subTest(area=name):
                body = text(frag)
                for gone in ("实测", "%", "梯度", "EasyCache"):
                    self.assertNotIn(gone, body,
                                     "%s 的提示里还留着实验证据：%r" % (name, gone))
        for gone in ("+8.8 GB", "+113 秒", "45.3", "1.956", "0.953", "62.4",
                     "约 14 秒", "1024²→2048²"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html)

        paras = (re.findall(r'<p class="check-note dim">(.*?)</p>', adv, re.S)
                 + re.findall(r'<p class="check-note dim">(.*?)</p>', ups, re.S)
                 + re.findall(r'<p class="dim"[^>]*>(.*?)</p>', sweep, re.S))
        self.assertGreaterEqual(len(paras), 5, "这三处的提示段没找全")
        for p in paras:
            t = text(p)
            if not t:
                continue          # `#sweep-chain-note` 由 JS 填，空着是常态
            with self.subTest(hint=t):
                self.assertLessEqual(len(t), 60, "提示该是一句话，不是一段：%r" % t)

        self.assertNotIn("实测代价", html)
        self.assertNotIn("放大方式与耗时", html)

        # 模式下拉里那一项：只说是什么，不带耗时（三个选项同一个形状）。
        sel = html.split('<select id="mode">')[1].split("</select>")[0]
        self.assertEqual(
            [text(o) for o in re.findall(r"<option[^>]*>(.*?)</option>", sel, re.S)],
            ["T2I · 纯文生图", "Edit · 图生图 / 局部编辑", "放大 · VOSR2 超分"])

        # 扫描里那句链的说明：只说「不生效」。
        fn = js.split("function renderSweepChainNote() {")[1].split("\n}")[0]
        self.assertIn("在扫描里不生效", fn)
        self.assertNotIn("扫描就不成立", fn)

    def test_advanced_settings_puts_the_sampler_first_and_drops_the_foldouts(self):
        """高级设置：采样器提到最前、两个加速的折叠块撤掉（2026-10-01 按用户要求）。

        折叠块里只有一句「勾一个，另一个会自动弹掉」——那**就是结论**，折起来还要
        点一下才看得到；而它想说的那件事（二选一）应该**一眼可见**。现在它写在两个
        开关各自的提示行里，**两条都要写**：用户可能先看到任意一个。

        采样器 / 调度器提到改写器上方，是因为那一步最常动，而改写器与两个加速都是
        可选的额外功能（顺序不是审美：折叠面板从上往下就是「先调什么」）。
        """
        html = self.read("index.html")
        adv = html.split('id="advanced-wrap"')[1].split('id="sweep-panel"')[0]
        self.assertLess(adv.index('id="sampler"'), adv.index('id="rewrite-wrap"'))
        self.assertLess(adv.index('id="rewrite-wrap"'), adv.index('id="te_speed"'))
        self.assertLess(adv.index('id="te_speed"'), adv.index('id="lazy_cache"'))
        # Turbo 是**第三个加速开关**，位置在 TE-Speed **之前** —— 链上它也更靠前
        # （`Cache → LoRA → TE-Speed/LazyCache → KSampler`），面板从上往下就是
        # 「链上从前往后」。三个加速的开关因此是挨着的。
        # 它**不参与**下面那个「二选一」（它是权重补丁，不抢那条线）。
        self.assertLess(adv.index('id="rewrite-wrap"'), adv.index('id="turbo"'))
        self.assertLess(adv.index('id="turbo"'), adv.index('id="te_speed"'))
        self.assertNotIn("help-details", adv, "高级设置里不该再有折叠块")
        self.assertEqual(adv.count("二选一"), 2, "两个开关各自的提示行都要说这件事")
        self.assertIn("采样 · 改写 · 加速", html)

    def test_the_tile_loads_a_prompt_into_its_own_box(self):
        """「载入提示词」：点一下，两段**各回各的框**（2026-10-01 按用户要求）。

        用户先说的是「反向词出现后，提示词整体分为了两个部分，而单纯的复制无法区分
        两者」，随后澄清想要的是**载入参数那样的动作** ——「点它的时候自动将这些信息
        填入对应的位置」。所以格子换成两颗**载入**按钮（提示词 / 参数），复制那一族
        留在放大视图（那里有地方，也是「先看清楚再拿走」那一个）。

        三条口径，少一条都有症状（真浏览器那半边在 `page_shot` 第 18 步）：
          · **有才写**：这张图没有反向词就**不动**那个框 —— 与「载入参数」的「缺键 =
            不动」是同一条规则。用「清空」代替「不动」，会在用户手里正改着的时候
            悄悄抹掉一段，而那是看不见的；
          · **放大模式下禁用**（那是唯一没有提示词输入框的模式）：写进一个藏着的框
            等于没有症状的错 —— 切模式才突然冒出来；
          · 判据读**表单此刻的模式**，所以切模式（`applyModeUI`）与重画
            （`renderGallery` / `renderLightbox`）三处都要刷一遍按钮状态。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        tile = js.split('<div class="tile-actions">')[1].split("</div></article>")[0]
        self.assertEqual(re.findall(r'data-act="([a-z]+)"', tile),
                         ["loadprompt", "load"])
        self.assertIn('data-why="empty"', tile)      # 这张图没有提示词 → 禁用并说明
        load = js.split("function loadPromptsFromGallery(item) {")[1].split("\n}")[0]
        self.assertIn("applyParams({negative_prompt: item.negative_prompt || null},"
                      " item.prompt)", load)
        self.assertIn('$("mode").value === "upscale"', load)
        # 放大模式那条禁用还要**说得出理由**，否则按不动就是个哑键
        self.assertIn('id="lb-load-prompt"', html)
        sync = js.split("function syncPromptLoadButtons() {")[1].split("\n}")[0]
        self.assertIn('$("mode").value === "upscale"', sync)
        self.assertIn('dataset.why === "empty"', sync)
        self.assertIn("没有提示词输入框", sync)
        self.assertEqual(js.count("syncPromptLoadButtons();"), 3,
                         "applyModeUI / renderGallery / renderLightbox 三处都要刷")

    def test_the_copy_family_still_never_merges_the_two_prompts(self):
        """复制那一族（现在只在放大视图里）：一段一个动作，谁也不合并。

        它比「载入提示词」多一层要求：剪贴板里那一段必须**干净** —— 不带标签、不带
        另一段。所以放大视图里两段**各占一块**，标签是**兄弟节点**：自动复制失败那条
        兜底会 `selectNodeContents(#lb-prompt)` 整块选中，标签混进去就会被一起复制走。
        """
        html = self.read("index.html")
        js = self.read("app.js")
        self.assertIn('id="lb-prompt"></span>', html)
        self.assertIn('id="lb-negative"></span>', html)
        self.assertIn('id="lb-negative-wrap"', html)
        self.assertIn('<b class="lb-tag">', html)
        copy = js.split("async function copyPrompt(item, btn) {")[1].split("\n}")[0]
        self.assertIn("item.prompt", copy)
        self.assertNotIn("negative_prompt", copy)
        # 两个口径同一批键（`loadFromGallery` 填表单，`paramsLine` 写成一行带走）
        load = js.split("function loadFromGallery(item) {")[1].split("\n}")[0]
        line = js.split("function paramsLine(item) {")[1].split("\n}")[0]
        for key in ("steps", "seed", "cfg", "sampler", "scheduler",
                    "width", "height"):
            with self.subTest(key=key):
                self.assertIn("item." + key, load)
                self.assertIn("item." + key, line)

    def test_a_gallery_drag_is_uploaded_under_the_image_own_name(self):
        """从画廊拖一张图进来，名字必须取 `text/uri-list` 里的 `name=`。

        这个 bug **没有症状**：页面看起来「传上去了」，只是列表里永远只有一张，
        显示的还可能是上一张（用户的症状就是「拖第二张不显示、删掉第一张再拖
        第三张却显示第二张」）。所以守三件事：
          1. drop 走 `takeDropped`，不是直接 `uploadRefs(dt.files)`；
          2. `text/uri-list` 真的被读了 —— 不读就没有真名字；
          3. 字节**重新按 URL 取一次**并带缓存穿透参数（不吃浏览器给的那一份）。
        """
        js = self.read("app.js")
        self.assertIn("takeDropped(event.dataTransfer)", js)
        self.assertIn('getData("text/uri-list")', js)
        self.assertIn("function refFromUri", js)
        self.assertIn('url.searchParams.set("_", String(Date.now()))', js)
        # 老写法必须消失：直接拿 `dt.files` 传，等于把浏览器的合成名字
        # （实测对 `/api/gallery/image?…` 就是 `image.png`）当成真名字用。
        self.assertNotIn("uploadRefs(dt && dt.files)", js)

    def test_loading_params_takes_the_numbers_and_leaves_the_rest_alone(self):
        """画廊「载入参数」只把**数值参数**填回表单（2026-10-01 按用户要求收窄）。

        它以前连提示词、反向词、模式、参考图、加速开关一起覆盖 —— 而它最常被用的
        地方是「拿这张图的参数配**我自己的**提示词」。被覆盖掉的东西往往到出图之后
        才发现，属于本工作区最忌讳的那类**没有症状**的错。

        这一条守的是白名单的**形状**（源码级），两边都算数：
          · `loadFromGallery` 只能传那 7 个键，第二个参数必须是 `null`
            （`applyParams` 里只有 `promptText != null` 才会动提示词）；
          · `selectJob`（「最近任务」那条路）**必须照旧**带提示词 —— 那是「回看一条
            任务当时是什么」，与「把这批参数拿来用」是两件事，别被一起收窄掉。

        真浏览器里的逐项比对在 `tests/page_shot.py` 第 10b 步（那是**会红**的判据）。
        """
        js = self.read("app.js")
        body = js.split("function loadFromGallery(item) {")[1].split("\n}")[0]
        for key in ("item.steps", "item.seed", "item.cfg", "item.sampler",
                    "item.scheduler", "item.width", "item.height"):
            with self.subTest(key=key):
                self.assertIn(key, body)
        for left_alone in ("item.prompt", "item.negative_prompt", "item.mode",
                           "item.is_2k", "item.source_image", "item.source_images",
                           "item.scale", "item.lazy_cache", "item.te_speed"):
            with self.subTest(left_alone=left_alone):
                self.assertNotIn(left_alone, body)
        self.assertIn("}, null);", body)
        # 另一条入口一个字没改：它要的恰恰是「连提示词一起填回来」。
        self.assertIn('applyParams((job && job.params) || {}, '
                      '(job && job.prompt_text) || "");', js)

    def test_the_size_field_is_guarded_by_the_form_current_mode(self):
        """尺寸那条守卫必须读**表单当前**的模式，不能读传进来的 `p.mode`。

        画廊那条路已经不传模式了（载入参数只拿数值），拿 `p.mode` 判就**恒真** ——
        于是往图生图 / 放大模式里写尺寸，而那两栏此刻是收着的：写进去看不见，
        等用户切模式才突然冒出来。这个错同样没有症状。
        """
        js = self.read("app.js")
        body = js.split("function applyParams(p, promptText) {")[1].split("\n}")[0]
        self.assertIn('const currentMode = $("mode").value;', body)
        self.assertNotIn('p.mode !== "edit"', body)
        self.assertNotIn('p.mode !== "upscale"', body)

    def test_the_confined_layout_is_surfaced_before_the_click(self):
        """受限模式那件事要有落点（P20i 起是**两处**：悬停提示 + 日志台）。

        为什么不能只留在悬停提示里：那行字只在鼠标停在开关上时存在，而
        「这台机器的落点被搬过、所以模型下载与节点安装用不了」是一个**持续状态**。
        在日志台的「环境体检」里留一行，它才有第二个、看得见的落点 ——
        原来承担这件事的是顶栏那行小字（`#be-ver`），那一行随告警板一起撤了。
        """
        js = self.read("app.js")
        css = self.read("style.css")
        # 那句话来自**后端算的** note，页面不自己编一遍（两边各写一份必然分家）
        self.assertIn("layoutOf", js)
        self.assertIn("lay.note", js)
        self.assertIn("受限模式", js)
        # ① 开关的悬停提示里要说
        self.assertIn('lay.mode === "confined"', js)
        # ② 日志台的「环境体检」里要有一行
        self.assertIn('row("落点"', js)
        # 落点是**多行说明**，那一行得能折行显示（否则会撑破日志台那一栏）
        self.assertIn(".cmd-row", css)

    def test_the_old_upload_button_is_gone_not_left_dangling(self):
        """虚线框取代了「上传图片」按钮（选中/拖入即上传），所以那个按钮**不该还在**。

        留一个已经没人绑定的按钮比删掉更糟：点下去没有任何反应，而「点了没反应」
        正是这一组测试要消灭的症状。
        """
        self.assertNotIn("btn-upload", self.read("index.html"))
        self.assertNotIn("btn-upload", self.read("app.js"))

    def test_the_js_targets_the_same_endpoints_the_server_declares(self):
        js = self.read("app.js")
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "workbench", "server.py"),
                encoding="utf-8") as f:
            routes = set(re.findall(r'Route\("(/api/[^"]+)"', f.read()))
        # 带路径参数的由**字面前缀**匹配：app.js 里拼的是 "/api/jobs/" + id，
        # 正则只能取到 "/api/jobs/"，而服务端的写法是 "/api/jobs/{job_id}/…"。
        prefixes = {r.split("{")[0] for r in routes}
        literal = {p for p in routes if "{" not in p}
        used = set(re.findall(r'(?:api|postJSON|getJSON)\("(/api/[^"?]+)', js))
        missing = sorted(u for u in used
                         if u not in literal and u not in prefixes)
        self.assertEqual(missing, [], "app.js 打了一个服务端没有的固定路径")

    def test_the_log_view_is_a_full_page_swap_not_a_fourth_tab(self):
        """日志台是**整页**换过去的，不是右栏的第 4 个标签。

        两件事一起守：① 装 `id="logview"` 的那个 `<main>` 在 `#workspace` 的
        `</main>` **之后**（两者是兄弟）—— 塞进右栏的话，日志行只有一半宽，
        每一行都折成两三截；② 入口在顶栏 `.top-right` 里 —— 它回答的是
        「整个工作台 + 后端怎么样」，不属于「成果」那一栏。
        """
        html = self.read("index.html")
        main_end = html.index("</main>", html.index('id="workspace"'))
        # 从 `id="logview"` 往回找它所在的那个标签的开头 —— 直接搜 `<main` 会
        # 先撞上注释里写的那些标签名（第一版就是这么假红的）。
        log_tag = html.rindex("<main", 0, html.index('id="logview"'))
        self.assertGreater(log_tag, main_end, "日志台被塞进了工作区里面")
        top_right = html[html.index('class="top-right"'):html.index("</header>")]
        self.assertIn('id="btn-logs"', top_right, "日志入口要在顶栏")
        tabs_at = html.index('class="result-tabs"')
        tabs = html[tabs_at:html.index("</section>", tabs_at)]
        self.assertNotIn("logview", tabs, "日志台不该是成果栏的第 4 个标签")

    def test_the_enhance_button_sits_outside_the_prompt_label(self):
        """提示词那颗增强按钮**必须在 `<label>` 外面**。

        `<label>` 里的按钮被点时会连带激活它标注的那个 textarea —— 于是点击的
        落点会飘（看起来只是「点不准」），而这是**没有症状**的一类错。
        """
        html = self.read("index.html")
        prompt_start = html.index('id="prompt-wrap"')
        prompt_end = html.index("</label>", prompt_start)
        button = html.index('id="btn-enhance"')
        self.assertGreater(button, prompt_end,
                           "#btn-enhance 跑到 <label> 里面去了")

    def test_there_is_exactly_one_enhance_entry(self):
        """**只有一颗**增强入口（2026-10-01 按用户要求改的形状）。

        旧形状是每个框一颗按钮（`#btn-enhance-prompt` + `#btn-enhance-negative`），
        加一个可增强的东西就要加一颗按钮 —— 扩展不动。现在目标由**后端那张表**
        （`llm.TARGETS_INFO` → `/api/llm` 的 `targets`）给，页面按它建下拉框。
        守两件事：页面上确实只剩一颗；而且**没有硬编码的目标列表**（那等于把
        后端的表在页面里抄了第二份，加目标时又会分家）。
        """
        html = self.read("index.html")
        for gone in ("btn-enhance-prompt", "btn-enhance-negative", "enhance-msg"):
            self.assertNotIn('id="%s"' % gone, html, "旧形状的元素还在：%s" % gone)
        self.assertIn('id="btn-enhance"', html)
        self.assertIn('id="en-target"', html)      # 目标选择器在面板里
        js = self.read("app.js")
        # 页面只能从 `/api/llm` 的 `targets` 建那个下拉框
        self.assertIn("state.llm.targets", js)
        self.assertIn("fillEnhanceTargets", js)

    def test_the_action_list_lives_only_in_the_backend(self):
        """**页面里连「译成英文」这几个字都没有。**

        这就是「加一个动作 = 后端加一行」这句话的判据。抄一份到页面里的话，后端
        加动作时两边会分家 —— 而页面里那份抄本只在接口坏掉时才生效，所以分家之后
        **没有任何症状**，直到某天接口真的坏了一次。
        （兜底只留了默认那一个动作，见 `enhanceActions()`；那不是一张表。）
        """
        html = re.sub(r"<!--.*?-->", "", self.read("index.html"), flags=re.S)
        js = re.sub(r"/\*.*?\*/", "", self.read("app.js"), flags=re.S)
        for gone in ("译成英文", "译成中文", "to_en", "to_zh", "ACTION_TO"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html, "index.html 里抄了一份动作表")
                self.assertNotIn(gone, js, "app.js 里抄了一份动作表")
        # 但也得真的把它建起来 —— 「页面里一个都没有」不能靠什么都不做通过
        self.assertIn('id="en-action"', html)
        self.assertIn("state.llm.actions", js)
        self.assertIn("fillEnhanceActions", js)

    def test_the_enhance_panel_never_rewrites_its_own_button_text(self):
        """面板里那颗按钮的**文字从头到尾不许被改**。

        旧形状那个卡死的 bug 就出在这儿：按钮自己显示「增强中…」、`finally` 里
        再「恢复」，而那个「原样」是从 DOM 里读的（`const back = btn.textContent`）。
        失败一次之后 `flashButton` 已经把「增强中…」当成原样记进了 `dataset.label`，
        于是从那以后**每一步都把它「恢复」成「增强中…」，按钮永远卡住**。
        进度与报错现在都去状态行，所以这条判据守的是「别再改回去」。

        加上翻译之后它叫「执行」：**宁可标签泛一点，也不要让它去跟所选动作同步** ——
        按钮文字一旦是运行时状态的函数，那个 bug 就又有地方长出来了（具体做什么，
        紧挨着的那个下拉框说了）。
        """
        js = self.read("app.js")
        self.assertIsNone(re.search(r'\$\("en-run"\)\.textContent\s*=', js),
                          "这颗按钮的文字被改了 —— 卡死那个 bug 就是这么来的")
        self.assertIn('$("en-status").textContent', js)
        html = self.read("index.html")
        run = re.search(r'id="en-run"[^>]*>([^<]*)</button>', html)
        self.assertIsNotNone(run, "面板里那颗执行按钮不见了")
        self.assertEqual(run.group(1).strip(), "执行",
                         "面板按钮的文字又变成一个会随状态变的东西了")

    def test_the_enhance_request_carries_the_reference_images_it_says_it_does(self):
        """面板上那句「随附 N 张参考图」必须与**真的发出去的东西**同一件事。

        两半都要钉住：① 请求体里带 `refs`（读盘与缩图是后端的事）；② 面板上有
        那一行、而且它写的是**同一份**名单（`llmRefs()`），不是另算一份 ——
        面板说带 2 张、实际带 1 张，是「模型看到的是哪几张图」这件事没法解释的
        开始，而那正是这个功能必须说清的地方。
        """
        js = self.read("app.js")
        html = self.read("index.html")
        self.assertIn('postJSON("/api/llm/enhance", { text, target, action, refs })',
                      js, "增强请求没把参考图带上")
        self.assertIn("const refs = llmRefs();", js)
        # 面板那行与请求取的是**同一个函数**
        self.assertIn("renderEnhanceRefs()", js)
        start = js.index("function renderEnhanceRefs()")
        self.assertIn("llmRefs()", js[start:js.index("function syncEnhanceButtons()")])
        self.assertIn('id="en-refs"', html)
        # 说的那一刻是在**发出去之前**（否则用户看到的是事后的解释）
        self.assertLess(js.index("正在\" + act.label"),
                        js.index("postJSON(\"/api/llm/enhance\", { text, target,"
                                 " action, refs })"))

    def test_the_enhance_panel_only_sends_images_the_generation_will_use(self):
        """**图上会用到的，才给它看。**

        文生图不把参考图喂进模型，那时递给 LLM 会让它**照着那张图写提示词** ——
        而那张图在出图时根本不存在。那是「空转」的近亲：出图能出、图也对，
        只是内容与用户真正想要的那张没关系，而且没有症状。

        判据读**表单此刻的模式**（与「载入参数」那条守卫同一条规矩：读别处的
        模式会恒真/恒假），所以这里连那个读法一起钉住。
        """
        js = self.read("app.js")
        start = js.index("function llmRefs()")
        body = js[start:js.index("function renderEnhanceRefs()")]
        self.assertIn('const mode = $("mode").value;', body)
        self.assertIn('mode === "t2i" ? [] : state.refs.slice()', body)

    def test_the_chaining_button_never_writes_the_form(self):
        """右栏那颗「← 移到左边」是**接力**用的：把这一稿放回左边，接着做下一道
        （典型用法：先增强成中文，再译成英文）。

        它绝不能顺手写回表单 —— 写回表单是「采用」的事。两者混在一起的话，
        「我不想动表单、只是想接着做一道」就没有出路了；更坏的是它会在用户以为
        什么都没发生时改掉他的提示词，而那时候他多半正看着右栏。
        """
        js = self.read("app.js")
        start = js.index("function sendEnhanceToInput()")
        end = js.index("function initEnhance()")
        body = js[start:end]
        for box in ('$("prompt")', '$("negative")',
                    '$("prompt-len")', '$("negative-len")', "scheduleBuild"):
            with self.subTest(box=box):
                self.assertNotIn(box, body, "「移到左边」动了表单：%s" % box)
        # 而它必须真的搬东西 + 真的作废右栏（与「改了输入就作废」同一条规矩）
        self.assertIn('draft.input = draft.result', body)
        self.assertIn('draft.result = ""', body)
        self.assertIn('$("en-input")', body)
        html = self.read("index.html")
        self.assertIn('id="en-send"', html)

    def test_the_log_view_polls_instead_of_hanging_off_sse(self):
        """日志台**自己轮询**，不挂在 SSE 上。

        理由很硬：SSE 断了的时候正是最需要看日志的时候 —— 挂上去的话，
        最该工作的那一刻它跟着一起瞎。判据落在源码上：既要有 `/api/logs`
        与游标，又不许在日志那一族里订阅任何 SSE 事件。
        """
        js = self.read("app.js")
        self.assertIn('getJSON("/api/logs"', js)
        self.assertIn('"?since="', js)
        # 订阅长这样：`on("progress", …)`。**别写成 `assertNotIn("on(", …)`** ——
        # 中文注释与 `.map((c) => …)` 都会撞上它（第一版就是这么假红的）。
        start = js.index("function openLogs()")
        end = js.index("function initLogsView()")
        self.assertIsNone(re.search(r'\bon\(\s*"', js[start:end]),
                          "日志台挂了 SSE 订阅")

    def test_the_llm_key_is_never_written_into_the_page(self):
        """页面**只把密钥那一栏清空**，从不往里写值。

        后端已经保证不回显（`LLMSettings.public` 里没有 api_key），这一条守的是
        前端这一半：万一将来有人「顺手」把掩码或原文填进去，回显就回来了 ——
        而那种事在页面上看不出错。
        """
        js = self.read("app.js")
        assigns = re.findall(r'\$\("llm-key"\)\.value\s*=\s*([^;\n]+)', js)
        self.assertTrue(assigns, "页面应当显式把密钥栏重置成空")
        for value in assigns:
            self.assertEqual(value.strip(), '""',
                             "密钥栏被写进了非空值：%s" % value)
        html = self.read("index.html")
        self.assertIn('type="password" id="llm-key"', html)

    def test_sse_frame_event_field_matches_the_type(self):
        from workbench.events import sse_frame
        frame = sse_frame(7, {"type": "progress", "value": 1})
        self.assertIn("id: 7", frame)
        self.assertIn("event: progress", frame)
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(json.loads(frame.split("data: ", 1)[1])["type"], "progress")


class ArtifactPanel(ApiBase):
    """P21d：放大视图里画那两条新链路的产物（H3 的视频 / 拆层的层集 ＋ PSD 下载）。

    这里的判据全是**源码文本**上的，真行为归 P21e 的 `page_shot`（真浏览器里真点一次
    「下载 PSD」）。之所以值得在这里钉住，是因为它们每一条的失效方式都**看不出来**：

      · 视频走错取图口 → 页面画一个永远转圈的 `<video>`，而日志、状态全是成功；
      · 层格子用了原图 → 一次 24 张 1280² 的 RGBA，页面卡顿（与 P20l 那次同一个病）；
      · **出 PSD 用了缩略图** → 512px 的 JPEG 被烘进 PSD。没有报错、没有提示，
        用户拿到的那份 PSD 就是糊的 —— 这是本轮最要命的一条；
      · 层缺了几张却照样出 PSD → 少层的 PSD 与完整的长得一模一样；
      · 出 PSD 的按钮文字被程序改 → 踩 `flashButton` 那个尖角，之后再也变不回来。
    """

    #: 插件那一份 `ag-psd.bundle.js` 的指纹（297,416 B）。照搬而不是自己打包，
    #: 两端各打一份的话迟早对不上，而症状是「某个层读不出来」。
    BUNDLE_SHA256 = "bdf200282ea3e7d582a1a6746df82561231c9b85c75eb31b8f2965aad83d7794"
    BUNDLE_BYTES = 297416

    def read(self, *parts):
        with open(os.path.join(config.STATIC_DIR, *parts), encoding="utf-8") as f:
            return f.read()

    def stage(self, js):
        return js[js.index("function artifactStage(job)"):
                  js.index("function renderJobs()")]

    def test_the_video_goes_through_the_media_route(self):
        """`/api/media` 支持 `Range`、且认 `output/video/` 子目录；`/api/image` 两样都不。"""
        block = self.stage(self.read("app.js"))
        video = block[block.index('art.kind === "video"'):block.index('art.kind !== "layers"')]
        self.assertIn('"/api/media?filename="', video)
        self.assertIn("encodeURIComponent(art.subfolder", video)
        self.assertNotIn("/api/image", video)

    def test_the_layer_grid_draws_thumbs_but_the_psd_takes_originals(self):
        """同一对取图口，两个用途**刻意相反**（与画廊那条规矩同源）。

        格子要省带宽 ⇒ 缩略图；PSD 要真像素 ⇒ 原图。反过来任何一处都是无症状的降级。
        """
        js = self.read("app.js")
        self.assertIn("galleryUrl({ root: art.root, filename: l.filename }, true, false)", js)
        psd = self.read("psd.js")
        self.assertIn('"/api/gallery/image?root="', psd)
        # 出 PSD 那条路**不许**碰缩略图口
        self.assertNotIn("/api/gallery/thumb", psd)

    def test_the_psd_buttons_only_appear_when_a_psd_can_be_built(self):
        """不能出 PSD 时**一颗按钮都不画** —— 画一颗点了报错的按钮，用户会当成网络问题。"""
        block = self.stage(self.read("app.js"))
        self.assertIn("window.WbPsd.refusal(art", block)
        self.assertIn("if (!why) {", block)
        guard = block[block.index("if (!why) {"):block.index("const cells")]
        self.assertIn('id="btn-psd-rgba"', guard)
        self.assertIn('id="btn-psd-depth"', guard)
        # 守卫之外不许再有那两颗 id（否则「不画」这条就是假的）
        self.assertEqual(block.count('id="btn-psd-rgba"'), 1)
        self.assertEqual(block.count('id="btn-psd-depth"'), 1)

    def test_the_psd_status_never_rewrites_the_button_label(self):
        """进度与失败一律去 `#psd-status`。改按钮文字会踩 `flashButton` 那个尖角。"""
        js = self.read("app.js")
        body = js[js.index("async function downloadPsd(job, kind)"):
                  js.index("function renderJobs()")]
        self.assertIn('$("psd-status")', body)
        self.assertNotIn("btn.textContent", body)
        self.assertNotIn("btn.innerHTML", body)

    def test_the_stage_is_actually_rendered(self):
        live = self.read("app.js")
        live = live[live.index("function renderLive()"):
                    live.index("function statusLabel(s)")]
        self.assertIn("artifactStage(job)", live)
        # 两颗按钮的绑定也得在（画出来点不动 = 没画）
        self.assertIn('$("btn-psd-rgba")', live)
        self.assertIn('$("btn-psd-depth")', live)

    def test_a_video_job_does_not_also_draw_a_broken_image(self):
        """★ **活体验收抓到的**（2026-10-12，真跑了一次 H3）。

        `SaveVideo` 把 mp4 放在 **`images`** 键下（另带一个 `animated: [true]`），
        所以 `comfy.outputs_from_history()` 照原样把它当一个图片产物收进了
        `job.outputs`。照原样画 `<img>` 会得到一张破图 → 触发 `onerror` →
        `markShotGone()` 那句「多半是被删了」，**而那段视频就在下面那块 `<video>`
        里好好地放着**。

        单测与假 DOM 都发现不了它（要真有浏览器解一次 mp4 才知道），
        所以这里钉的是那个守卫本身：有 `artifact` 就不画那张大图。
        """
        live = self.read("app.js")
        live = live[live.index("function renderLive()"):
                    live.index("function statusLabel(s)")]
        self.assertIn("if (outs.length && !job.artifact) {", live)

    def test_missing_layers_are_not_drawn_as_a_broken_image(self):
        """层缺了就写一句人话。画一张取不到的图会走 `markShotGone` 那句「多半是被删了」
        —— 把「这一层被搬走了」说成「没了」是错的。"""
        block = self.stage(self.read("app.js"))
        self.assertIn("layer-missing", block)
        self.assertIn("art.missing", block)

    def test_psd_js_loads_before_app_js(self):
        html = self.read("index.html")
        self.assertLess(html.index("/static/psd.js"), html.index("/static/app.js"))

    def test_the_vendored_psd_bundle_is_the_plugin_s_own_file(self):
        """逐字节照搬。自己再打一份的话两端迟早分家，而症状是「某个层读不出来」。"""
        path = os.path.join(config.STATIC_DIR, "ag-psd.bundle.js")
        self.assertTrue(os.path.isfile(path), "ag-psd.bundle.js 不在 static/ 里")
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(len(data), self.BUNDLE_BYTES)
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.BUNDLE_SHA256)


class Cost(support.LeakCheckedTestCase):
    def test_anchor_1024_20(self):
        secs, _ = cost.estimate_seconds("t2i", 20, 1024, 1024)
        self.assertAlmostEqual(secs, 68.0, delta=4)

    def test_anchor_2048_25_matches_measurement(self):
        """实测 2048²/25 步 = 11 分 05 秒。拟合公式必须能重现它自己那个锚点。"""
        secs, parts = cost.estimate_seconds("t2i", 25, 2048, 2048)
        self.assertAlmostEqual(parts["sampling_s"], 625.0, delta=5)
        self.assertAlmostEqual(secs, 665.0, delta=20)

    def test_rewriter_adds_its_measured_cost(self):
        """改写那个开关（不是模式）必须真的进成本模型。"""
        base, _ = cost.estimate_seconds("t2i", 25, 1024, 1024)
        rew, _ = cost.estimate_seconds("t2i", 25, 1024, 1024, rewrite=True)
        self.assertAlmostEqual(rew - base, config.REWRITER_S, delta=0.01)

    def test_humanize(self):
        self.assertEqual(cost.humanize(45), "45 秒")
        self.assertEqual(cost.humanize(65), "1 分 05 秒")
        self.assertEqual(cost.humanize(665), "11 分 05 秒")
        self.assertEqual(cost.humanize(3700), "1 小时 01 分")


class MiniMaxH3(InputsBase):
    """H3 —— 与 Qwen 那四条**平级**的一条链路，不是 `#mode` 里多一项。

    契约上它与放大最像（都要源图在后端 input/ 里），差别是它**还要提示词**、
    不要步数 / cfg / 尺寸，而且贵得多（310 秒、峰值显存 96.5%）。
    """

    def frames(self, name, w=480, h=864):
        """带真 IHDR 的首帧 —— 后端要读它判长宽比。"""
        head = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", 13, b"IHDR")
                + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
                + b"\x00\x00\x00\x00")
        return self.put(name, head)

    def ready(self, files=(("ref.png", 480, 864),), nodes=True, **kw):
        for args in files:
            self.frames(*args)
        names = [f[0] for f in files]
        return self.make(
            running=kw.pop("running", True),
            input_dir=self.srcdir,
            input_options=names,
            argv=["main.py", "--input-directory", self.srcdir],
            node_classes=kw.pop("node_classes",
                                ("MiniMaxH3ImageToVideo", "ImageResizeKJv2",
                                 "ComfyMathExpression") if nodes else ()),
            **kw)

    def h3(self, **over):
        body = {"mode": "h3", "prompt": "精灵女子抬头", "source_image": "ref.png",
                "seconds": 5, "seed": 42}
        body.update(over)
        return body

    def test_builds_an_h3_graph(self):
        data = self.ready().post("/api/build", json=self.h3()).json()
        self.assertEqual(data["describe"]["mode"], "h3")
        self.assertEqual(data["describe"]["first_frame"], "ref.png")
        self.assertEqual(data["describe"]["prompt"], "精灵女子抬头")
        self.assertEqual(data["describe"]["frames"], 124)
        self.assertTrue(data["describe"]["resize_matches"])
        self.assertEqual(data["workflow_nodes"], 20)

    def test_needs_a_prompt(self):
        r = self.ready().post("/api/build", json=self.h3(prompt="  "))
        self.assertEqual(r.status_code, 400)
        self.assertIn("提示词", r.json()["error"])

    def test_needs_a_first_frame(self):
        r = self.ready().post("/api/build", json=self.h3(source_image=None))
        self.assertEqual(r.status_code, 400)
        self.assertIn("首帧", r.json()["error"])

    def test_refuses_a_second_first_frame(self):
        """图里只有一个 `LoadImage` —— 收下第二张会让用户以为它也参与了。"""
        tc = self.ready(files=(("ref.png", 480, 864), ("b.png", 480, 864)))
        r = tc.post("/api/build", json=self.h3(source_images=["ref.png", "b.png"]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("只用一张", r.json()["error"])

    def test_first_frame_must_be_in_input(self):
        tc = self.ready()
        data = tc.post("/api/build", json=self.h3(source_image="nope.png")).json()
        errs = [w for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(errs)
        self.assertIn("nope.png", " ".join(w["text"] for w in errs))

    def test_always_warns_about_vram(self):
        """96.5% 的峰值显存是**必须**在按下按钮之前说的一件事。"""
        data = self.ready().post("/api/build", json=self.h3()).json()
        text = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("21.24", text)
        self.assertIn("96.5", text)

    def test_says_the_real_frame_count(self):
        data = self.ready().post("/api/build", json=self.h3(seconds=10)).json()
        text = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("243 帧", text)
        # 10 秒这一档**没跑过**，页面必须说出来
        self.assertIn("没实测过", text)

    def test_warns_when_the_first_frame_is_far_from_916(self):
        tc = self.ready(files=(("wide.png", 1024, 512),))
        data = tc.post("/api/build", json=self.h3(source_image="wide.png")).json()
        text = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("裁", text)
        self.assertIn("9:16", text)

    def test_missing_plugin_node_is_an_error_with_a_fileable_fix(self):
        tc = self.ready(nodes=False)
        data = tc.post("/api/build", json=self.h3()).json()
        errs = [w["text"] for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(any("ImageResizeKJv2" in t for t in errs))
        self.assertTrue(any("ComfyUI-KJNodes" in t for t in errs))

    def test_confirm_is_required(self):
        """310 秒 —— 一次误点就是一整个等待，所以必须点两下。"""
        tc = self.ready()
        r = tc.post("/api/run", json=self.h3())
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertTrue(body["confirm"]["required"])
        self.assertIn("超过 120 秒", body["confirm"]["text"])

    def test_auto_upscale_is_rejected(self):
        tc = self.ready()
        r = tc.post("/api/build", json=self.h3(
            auto_upscale={"enabled": True, "scale": 2}))
        self.assertEqual(r.status_code, 400)
        self.assertIn("21.24", r.json()["error"])

    def test_the_estimate_is_flagged_as_a_single_anchor(self):
        data = self.ready().post("/api/build", json=self.h3(seconds=10)).json()
        self.assertTrue(data["estimate"]["parts"]["assumed"])
        self.assertAlmostEqual(data["estimate"]["seconds"], config.H3_SECONDS,
                               delta=0.5)


class SeeThrough(InputsBase):
    """See-through 拆层 —— 第三条链路。没有提示词，要源图。"""

    def ready(self, files=("a.png",), nodes=True, models=None, **kw):
        for name in files:
            self.put(name)
        return self.make(
            running=kw.pop("running", True),
            input_dir=self.srcdir,
            input_options=list(files),
            argv=["main.py", "--input-directory", self.srcdir],
            node_classes=kw.pop("node_classes",
                                ("SeeThrough_GenerateLayers",
                                 "SeeThrough_SavePSD") if nodes else ()),
            models=models,
            **kw)

    def layers(self, **over):
        body = {"mode": "layers", "source_image": "a.png", "seed": 42,
                "resolution": 1024, "steps": 30, "resolution_depth": -1}
        body.update(over)
        return body

    def test_builds_a_layers_graph(self):
        data = self.ready().post("/api/build", json=self.layers()).json()
        self.assertEqual(data["describe"]["mode"], "layers")
        self.assertEqual(data["describe"]["source_image"], "a.png")
        self.assertEqual(data["describe"]["resolution"], 1024)
        self.assertTrue(data["describe"]["depth_follows"])
        self.assertEqual(data["describe"]["resolution_depth_effective"], 1024)
        self.assertEqual(data["workflow_nodes"], 7)

    def test_does_not_need_a_prompt(self):
        r = self.ready().post("/api/build", json=self.layers())
        self.assertEqual(r.status_code, 200)

    def test_needs_a_source_image(self):
        r = self.ready().post("/api/build", json=self.layers(source_image=None))
        self.assertEqual(r.status_code, 400)
        self.assertIn("源图", r.json()["error"])

    def test_refuses_a_second_source_image(self):
        tc = self.ready(files=("a.png", "b.png"))
        r = tc.post("/api/build", json=self.layers(source_images=["a.png", "b.png"]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("只用一张", r.json()["error"])

    def test_says_the_two_measured_anchors(self):
        data = self.ready().post("/api/build", json=self.layers()).json()
        text = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("252", text)
        self.assertIn("403", text)
        # 层数不预判
        self.assertIn("23 层", text)

    def test_explicit_depth_says_it_diverges(self):
        data = self.ready().post(
            "/api/build", json=self.layers(resolution_depth=512)).json()
        text = " ".join(w["text"] for w in data["warnings"])
        self.assertIn("512", text)
        self.assertIn("拆层仍是 1024", text)

    def test_missing_plugin_is_an_error(self):
        tc = self.ready(nodes=False)
        data = tc.post("/api/build", json=self.layers()).json()
        errs = [w["text"] for w in data["warnings"] if w["level"] == "error"]
        self.assertTrue(any("SeeThrough_GenerateLayers" in t for t in errs))

    def test_confirm_is_required(self):
        tc = self.ready()
        r = tc.post("/api/run", json=self.layers())
        self.assertEqual(r.status_code, 409)
        self.assertIn("超过 120 秒", r.json()["confirm"]["text"])

    def test_auto_upscale_is_rejected(self):
        tc = self.ready()
        r = tc.post("/api/build", json=self.layers(
            auto_upscale={"enabled": True, "scale": 2}))
        self.assertEqual(r.status_code, 400)
        self.assertIn("层", r.json()["error"])

    def test_a_resolution_off_the_grid_is_rejected(self):
        r = self.ready().post("/api/build", json=self.layers(resolution=1000))
        self.assertEqual(r.status_code, 400)
        self.assertIn("64", r.json()["error"])

    def test_1280_is_estimated_from_the_second_anchor(self):
        data = self.ready().post("/api/build", json=self.layers(resolution=1280)).json()
        self.assertAlmostEqual(data["estimate"]["seconds"], 403.2, delta=0.5)
        self.assertFalse(data["estimate"]["parts"]["assumed"])


class ModePurity(InputsBase):
    """★ 五条链路的 **`params` 键集必须完全一致**。

    记录 / 画廊项 / 表单回显三处共用同一份形状。少一个键，前端就**静默少画一格**
    —— 不报错、不崩溃，只是某一栏永远空着，而这种坏法看不出是代码的问题。

    ⚠ 这里判的是 **`params`（任务记录那份）**，不是 `describe`。
    两者刻意不同：`describe` 是**从图里反解出来的**，每条链路当然有自己的键
    （放大有 `scale`、H3 有 `frames`、拆层有 `depth_follows`）—— 那个不对称是
    设计，不是缺陷。`params` 才是「三处共用」的那一份。
    """

    MODES = ("t2i", "edit", "upscale", "h3", "layers")

    def bodies(self):
        return {
            "t2i": {"mode": "t2i", "prompt": PROMPT},
            "edit": {"mode": "edit", "prompt": PROMPT, "source_image": "a.png"},
            "upscale": {"mode": "upscale", "source_image": "a.png", "scale": 2},
            "h3": {"mode": "h3", "prompt": PROMPT, "source_image": "a.png"},
            "layers": {"mode": "layers", "source_image": "a.png"},
        }

    def make_all(self):
        self.put("a.png")
        return self.make(
            running=True, input_dir=self.srcdir,
            input_options=["a.png"],
            argv=["main.py", "--input-directory", self.srcdir],
            node_classes=("MiniMaxH3ImageToVideo", "ImageResizeKJv2",
                          "ComfyMathExpression", "SeeThrough_GenerateLayers",
                          "SeeThrough_SavePSD", "TESpeedVOSR2Image",
                          "TESpeedVOSR2Loader"),
            combos={"TESpeedVOSR2Loader": {"model_bundle": ["VOSR2"]}})

    def test_every_mode_is_accepted(self):
        self.make_all()
        for mode, body in self.bodies().items():
            with self.subTest(mode=mode):
                data = self.tc.post("/api/build", json=body).json()
                self.assertNotIn("error", data)
                self.assertEqual(data["describe"]["mode"], mode)

    def test_params_keysets_are_identical(self):
        # 直接问构建器，不经 HTTP —— `params` 不进 `/api/build` 的响应
        # （那是 `describe`），而它恰恰是「三处共用」的那一份。
        self.make_all()
        sets = {}
        for mode, body in self.bodies().items():
            _wf, params, _prompt = server.build_from_body(dict(body))
            sets[mode] = set(params)
        first = sets["t2i"]
        for mode, got in sets.items():
            with self.subTest(mode=mode):
                self.assertEqual(got, first,
                                 "模式 %s 的 params 键集与 t2i 不同：多 %s 少 %s"
                                 % (mode, sorted(got - first), sorted(first - got)))

    def test_the_new_keys_are_null_on_the_old_chains(self):
        """反向也要钉一次：两个新键在 Qwen 那三条上恒为 None，而不是缺键。"""
        self.make_all()
        for mode in ("t2i", "edit", "upscale"):
            for body in (dict(self.bodies()[mode]),):
                _wf, params, _p = server.build_from_body(body)
                with self.subTest(mode=mode):
                    self.assertIn("seconds", params)
                    self.assertIsNone(params["seconds"])
                    self.assertIn("resolution_depth", params)
                    self.assertIsNone(params["resolution_depth"])

    def test_weird_modes_are_named_in_the_error(self):
        """报错要列出**五条**合法值 —— 只列 `qb.MODES` 会漏掉三条。"""
        self.make_all()
        r = self.tc.post("/api/build", json={"mode": "nonsense", "prompt": "x"})
        self.assertEqual(r.status_code, 400)
        text = r.json()["error"]
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.assertIn(mode, text)


class JobArtifact(InputsBase):
    """`/api/job` 里那个**现算**的产物描述（视频 / 层集）。

    它必须现算、不能记进 store：产物被挪走、被还原之后，记下来的那个字段
    不会跟着变，而它会一脸笃定地说着过时的话。
    """

    TOKEN = "a1b2c3d4"

    def make_out(self):
        out = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-out-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        self.outdir = out
        return self.make(running=True, input_dir=self.srcdir,
                         input_options=[],
                         argv=["main.py", "--output-directory", out])

    def write(self, rel, data=b"x"):
        path = os.path.join(self.outdir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def record(self, job_id, mode):
        self.wb.store.create(job_id=job_id, mode=mode, params={"mode": mode},
                             prompt_text="", workflow={}, estimate_s=1.0)
        return job_id

    def test_h3_job_reports_its_video(self):
        tc = self.make_out()
        self.write("video/MiniMax_H3_%s_00001.mp4" % self.TOKEN, b"0123456789")
        job = self.record("%s-9999-0000-0000-000000000000" % self.TOKEN, "h3")
        art = tc.get("/api/jobs/%s" % job).json()["artifact"]
        self.assertEqual(art["kind"], "video")
        self.assertEqual(art["filename"],
                         "MiniMax_H3_%s_00001.mp4" % self.TOKEN)
        self.assertEqual(art["subfolder"], "video")
        self.assertEqual(art["bytes"], 10)

    def test_layers_job_reports_its_layer_set(self):
        tc = self.make_out()
        prefix = "seethrough_%s_20261012_101530_ab12cd34" % self.TOKEN
        for tag in ("tail", "face"):
            self.write("%s_%s.png" % (prefix, tag))
        info = {"prefix": "seethrough", "timestamp": "20261012_101530",
                "width": 1024, "height": 1024,
                "layers": [{"name": tag, "filename": "%s_%s.png" % (prefix, tag),
                            "left": 0, "top": 0, "right": 10, "bottom": 20,
                            "depth_median": 0.5} for tag in ("tail", "face")]}
        self.write("%s_layers.json" % prefix, json.dumps(info))
        job = self.record("%s-1111-0000-0000-000000000000" % self.TOKEN, "layers")
        art = tc.get("/api/jobs/%s" % job).json()["artifact"]
        self.assertEqual(art["kind"], "layers")
        self.assertEqual(art["count"], 2)
        self.assertEqual(art["missing"], [])
        self.assertEqual(art["width"], 1024)

    def test_an_old_chain_job_has_a_null_artifact(self):
        """键恒定：别的链路也有这个键，只是恒为 None。"""
        tc = self.make_out()
        job = self.record("ffffffff-2222-0000-0000-000000000000", "t2i")
        data = tc.get("/api/jobs/%s" % job).json()
        self.assertIn("artifact", data)
        self.assertIsNone(data["artifact"])

    def test_a_job_whose_files_are_gone_says_so_without_inventing_anything(self):
        tc = self.make_out()
        job = self.record("%s-3333-0000-0000-000000000000" % self.TOKEN, "h3")
        self.assertIsNone(tc.get("/api/jobs/%s" % job).json()["artifact"])

    def test_the_store_record_is_not_mutated(self):
        """★ 现算，不落盘 —— 落盘之后还原回来的产物会一直显示「找不到」。"""
        tc = self.make_out()
        self.write("video/MiniMax_H3_%s_00001.mp4" % self.TOKEN)
        job = self.record("%s-4444-0000-0000-000000000000" % self.TOKEN, "h3")
        self.assertIsNotNone(tc.get("/api/jobs/%s" % job).json()["artifact"])
        self.assertNotIn("artifact", self.wb.store.get(job))


class MediaRoutes(InputsBase):
    """`/api/media` —— 视频产物的取物口。边界**不比** `/api/image` 宽。"""

    def make_out(self):
        out = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-media-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        self.outdir = out
        return self.make(running=True, input_dir=self.srcdir,
                         input_options=[],
                         argv=["main.py", "--output-directory", out])

    def write(self, rel, data=b"M" * 64):
        path = os.path.join(self.outdir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_serves_a_video(self):
        tc = self.make_out()
        self.write("video/MiniMax_H3_a1b2c3d4_00001.mp4")
        r = tc.get("/api/media?filename=MiniMax_H3_a1b2c3d4_00001.mp4"
                   "&subfolder=video")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content), 64)
        self.assertIn("video/mp4", r.headers.get("content-type", ""))

    def test_supports_range_so_the_scrubber_works(self):
        """★ 这条就是 `/api/media` 存在的理由。

        `<video>` 拖进度条靠 `Range`。没有它，浏览器每拖一次就要把整段重下一遍
        —— 而一个 2~20 MB 的 mp4 那样用就是没法看。
        """
        tc = self.make_out()
        self.write("video/MiniMax_H3_a1b2c3d4_00001.mp4", b"0123456789" * 8)
        r = tc.get("/api/media?filename=MiniMax_H3_a1b2c3d4_00001.mp4"
                   "&subfolder=video", headers={"Range": "bytes=0-9"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.content, b"0123456789")
        self.assertIn("content-range", {k.lower() for k in r.headers})

    def test_refuses_a_png(self):
        """图片走 `/api/image` —— 两条路的边界写在同一处，别在这里开口子。"""
        tc = self.make_out()
        self.write("photo.png")
        r = tc.get("/api/media?filename=photo.png")
        self.assertEqual(r.status_code, 400)
        self.assertIn("/api/image", r.text)

    def test_refuses_to_walk_out_of_the_output_root(self):
        tc = self.make_out()
        r = tc.get("/api/media?filename=..%2F..%2Fsecret.mp4")
        self.assertEqual(r.status_code, 404)

    def test_missing_file_and_missing_name(self):
        tc = self.make_out()
        self.assertEqual(tc.get("/api/media?filename=nope.mp4").status_code, 404)
        self.assertEqual(tc.get("/api/media").status_code, 400)

    def test_the_video_does_not_leak_into_the_gallery(self):
        """★ H3 的视频**不进画廊** —— 它落在子目录里，而画廊只扫顶层。

        这条是**不变量**，不是巧合：把它当巧合的话，哪天有人给画廊加上递归
        就会一夜之间多出一屋子「未识别」的格子。

        断言只看**有没有这个视频**，不看屋子是不是空的 —— `candidate_roots()`
        还会扫到工作区里真实的 `output/`（本机那几十张 Qwen 图），
        拿「空屋子」当判据会去依赖别人的磁盘。
        """
        tc = self.make_out()
        self.write("video/MiniMax_H3_a1b2c3d4_00001.mp4")
        data = tc.get("/api/gallery").json()
        names = [i["filename"] for i in data["items"]]
        self.assertNotIn("MiniMax_H3_a1b2c3d4_00001.mp4", names)
        self.assertFalse([n for n in names if n.endswith(".mp4")], names[:5])


if __name__ == "__main__":
    unittest.main(verbosity=2)
