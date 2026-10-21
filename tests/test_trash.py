#!/usr/bin/env python3
"""`trash.py` —— 「删了还能回来」这件事本身要有人守着。

这个模块只有四十行，但它守着画廊里唯一一个**不可逆**的动作。三条判据对应
三种失败，其中最要紧的是第二条：**没有 send2trash 的时候一张都不许删**。
退回 `os.remove` 会让「删不掉」悄悄变成「永久删掉了」—— 那正是这整个功能
存在的理由被抹掉的方式，而且没有任何症状。
"""
import builtins
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench import trash  # noqa: E402


class TrashBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-trash-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def put(self, name="a.png", blob=b"\x89PNG\r\n\x1a\nnot really a png"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(blob)
        return path


class Load(TrashBase):
    def test_the_package_is_there_on_this_machine(self):
        """`requirements-lock.txt` 钉了它。钉松了要当场红，而不是等用户点了删除。

        （跳过不是失败 —— 那种失效最难发现，所以这里刻意让它红。）
        """
        ok, why = trash.available()
        self.assertTrue(ok, "这台机器上没有 send2trash：%s" % why)

    def test_a_missing_package_is_reported_as_such(self):
        with mock.patch("builtins.__import__", side_effect=self._no_send2trash):
            ok, why = trash.available()
        self.assertFalse(ok)
        self.assertIn("send2trash", why)
        # 页面要照做的那句话必须在消息里
        self.assertIn("pip install send2trash", why)

    @staticmethod
    def _no_send2trash(name, *a, **kw):
        if name == "send2trash" or name.startswith("send2trash."):
            raise ImportError("No module named 'send2trash'（测试造的）")
        return builtins.__import__(name, *a, **kw)


class Refuses(TrashBase):
    def test_a_missing_package_deletes_nothing(self):
        path = self.put()
        with mock.patch("builtins.__import__", side_effect=Load._no_send2trash):
            with self.assertRaises(trash.TrashError) as cm:
                trash.trash(path)
        self.assertEqual(cm.exception.kind, "unavailable")
        self.assertIn("pip install send2trash", str(cm.exception))
        # **最重要的一条**：一个字节都不许动
        self.assertTrue(os.path.exists(path))

    def test_a_file_that_is_already_gone_is_not_counted_as_deleted(self):
        with self.assertRaises(trash.TrashError) as cm:
            trash.trash(os.path.join(self.tmp, "nope.png"))
        self.assertEqual(cm.exception.kind, "missing")

    def test_a_refusal_from_the_bin_is_reported_verbatim(self):
        path = self.put()

        def refuse(_p):
            raise OSError("另一个进程正在使用这个文件")
        with mock.patch.object(trash, "_load", lambda: refuse):
            with self.assertRaises(trash.TrashError) as cm:
                trash.trash(path)
        self.assertEqual(cm.exception.kind, "failed")
        self.assertIn("另一个进程正在使用这个文件", str(cm.exception))
        self.assertTrue(os.path.exists(path))


class Moves(TrashBase):
    def test_it_hands_the_exact_path_to_the_bin(self):
        path = self.put()
        seen = []

        def record(p):
            seen.append(p)
            os.remove(p)          # 「进了回收站」在磁盘上的等价物：它离开原地

        with mock.patch.object(trash, "_load", lambda: record):
            trash.trash(path)
        self.assertEqual(seen, [path])
        self.assertFalse(os.path.exists(path))
