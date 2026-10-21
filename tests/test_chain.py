#!/usr/bin/env python3
"""出图后自动放大（串联）：把产物搬进 `input/`，再提交第二个作业。

单独一个文件，因为它守的是一条**跨两段**的机制。三条判据各自对应一个真栽过的坑：

  · **名字取自回执**：VOSR2 只读得到后端 `input/` 里它认得的那一份，而上传撞名时
    后端会**改名**（P14 刚栽过：拿自己以为的名字去用，失败得很难懂）；
  · **链失败不许把第一个作业变成失败**：链跑的时候那个作业已经 `done` 了，
    它的产物与记录都不该被这一段覆盖；
  · **找不到产物要说人话**：静默什么都不做比报错难查得多。

    python tests/test_chain.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import support  # noqa: E402
from workbench import server, upload  # noqa: E402
from workbench.util import now_ms  # noqa: E402

# 链要读出来的产物名。**带进程号**：`candidate_roots()` 会把本机真实的输出目录也
# 列进来，用 `support.history_entry()` 那个默认名（`…_00042.png`）会在本机的
# `output/` 里找到**同名**的真图 —— 于是「产物不存在」那一支永远测不到，
# 而另外几条判据也会偷偷断言在本机的老图上。
PNG_NAME = "chainfix_%d.png" % os.getpid()
# 上传回执给的名字**故意与输入不同**：真后端撞名时会改名，链必须用回执那个。
RECEIPT = "chainfix_%d_00001_.png" % os.getpid()


def outputs_for(name=PNG_NAME):
    return {"461": {"images": [{"filename": name, "subfolder": "",
                                "type": "output"}]}}


class ChainBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()               # 临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(),
                                    prefix="qwen21-chain-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out_dir = os.path.join(self.tmp, "output")
        os.makedirs(self.out_dir)
        # 输出目录靠**启动参数**认（与画廊同一套 candidate_roots），所以 argv 里
        # 必须有 --output-directory：只认一个写死的目录会在落点搬家后静默找不到。
        self.client = support.FakeComfy(
            argv=["python", "main.py", "--output-directory", self.out_dir])
        self.wb = server.Workbench(state_dir=self.tmp, client=self.client)
        self.broker = support.RecordingBroker()
        self.wb.broker = self.broker
        self.wb.store.create(
            job_id="parent-1", mode="t2i",
            params={"steps": 20, "seed": 12345, "rewrite": False,
                    "auto_upscale": {"enabled": True, "scale": 3}},
            prompt_text="a cat", workflow={"459:458": {"class_type": "KSampler"}})

    def put_output(self, name=PNG_NAME, blob=b"\x89PNG\r\n\x1a\n" + b"0" * 64):
        path = os.path.join(self.out_dir, name)
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def finish_parent(self, outputs=None, put_blob=True):
        """让父作业走**真的**那条收尾路径（`_finish_from_history`）落地。

        钩子换成同步版本：产品里它会把协程排回事件循环（`_schedule_chain`），
        这里直接跑完，断言才看得到链的结果。
        """
        if put_blob:
            self.put_output()
        self.wb.watcher.on_done = lambda pid: asyncio.run(
            server._chain_upscale(self.wb, pid))
        self.client.history_map["parent-1"] = support.history_entry(
            "parent-1", outputs=outputs_for() if outputs is None else outputs)
        self.wb.watcher._finish_from_history("parent-1")
        return self.wb.store.get("parent-1")


class HappyPath(ChainBase):
    def test_the_finished_image_is_uploaded_under_its_own_name(self):
        up = mock.Mock(return_value={"filename": RECEIPT})
        submit = mock.AsyncMock(return_value={"ok": True, "job_id": "child-9"})
        with mock.patch.object(upload, "upload_any", up), \
                mock.patch.object(server, "_submit_one", submit):
            rec = self.finish_parent()

        # 1) 搬的**就是这次的第一张产物**（名字与字节都从磁盘来）
        args = up.call_args[0]
        self.assertEqual(args[1], PNG_NAME)
        self.assertTrue(args[2].startswith(b"\x89PNG"))
        # 2) 提交的第二个作业用**回执里的名字**，不是我们以为的那个
        body = submit.call_args[0][1]
        self.assertEqual(body["mode"], "upscale")
        self.assertEqual(body["source_image"], RECEIPT)
        # 3) 倍数与 seed 都跟着走（链可复现：同一个 seed 再跑得到同一张）
        self.assertEqual(body["scale"], 3)
        self.assertEqual(body["seed"], 12345)
        self.assertTrue(body["confirm"], "链上的第二次提交不该再打断用户")
        # 4) 子作业记着父作业
        self.assertEqual(submit.call_args[1]["parent_job_id"], "parent-1")
        # 5) 父记录上留了链的脚印，并且推给了页面
        self.assertEqual(rec["chain"]["status"], "submitted")
        self.assertEqual(rec["chain"]["job_id"], "child-9")
        self.assertEqual(rec["chain"]["source"], RECEIPT)
        self.assertEqual(rec["chain"]["scale"], 3)
        self.assertEqual(self.broker.of("job_chain")[0]["chain"]["job_id"],
                         "child-9")

    def test_the_parent_stays_done(self):
        up = mock.Mock(return_value={"filename": RECEIPT})
        submit = mock.AsyncMock(return_value={"ok": True, "job_id": "child-9"})
        with mock.patch.object(upload, "upload_any", up), \
                mock.patch.object(server, "_submit_one", submit):
            rec = self.finish_parent()
        self.assertEqual(rec["status"], "done")
        self.assertIsNotNone(rec["wall_ms"])
        self.assertEqual([o["filename"] for o in rec["outputs"]], [PNG_NAME])


class Failures(ChainBase):
    """链坏了是**链**的事 —— 第一个作业照旧成功，但这件事必须说出来。"""

    def test_upload_failure_is_reported_and_nothing_is_submitted(self):
        boom = mock.Mock(side_effect=upload.UploadError("写不了 input/"))
        submit = mock.AsyncMock()
        with mock.patch.object(upload, "upload_any", boom), \
                mock.patch.object(server, "_submit_one", submit):
            rec = self.finish_parent()
        self.assertEqual(rec["status"], "done")          # 父作业不受影响
        self.assertEqual(rec["chain"]["status"], "failed")
        self.assertIn("写不了 input/", rec["chain"]["error"])
        submit.assert_not_awaited()
        self.assertTrue(self.broker.of("job_chain"),
                        "链失败也要推给页面，不能静默")

    def test_missing_output_says_so_in_plain_words(self):
        submit = mock.AsyncMock()
        with mock.patch.object(server, "_submit_one", submit):
            # 产物**不落盘** —— 这条链没有任何东西可以搬
            rec = self.finish_parent(put_blob=False)
        self.assertEqual(rec["chain"]["status"], "failed")
        self.assertIn("找不到这次的产物文件", rec["chain"]["error"])
        submit.assert_not_awaited()

    def test_a_refused_second_submit_is_reported(self):
        up = mock.Mock(return_value={"filename": RECEIPT})
        submit = mock.AsyncMock(return_value={
            "ok": False, "status": 409,
            "body": {"error": "成本预警：再点一次确认"}})
        with mock.patch.object(upload, "upload_any", up), \
                mock.patch.object(server, "_submit_one", submit):
            rec = self.finish_parent()
        self.assertEqual(rec["chain"]["status"], "failed")
        self.assertIn("成本预警", rec["chain"]["error"])


if __name__ == "__main__":
    unittest.main()
