#!/usr/bin/env python3
"""画廊的缩略图 —— 缩得对、缓存得对、**缩不了的时候一定有兜底**。

为什么这个小模块值得一整套判据：它自己失败的样子**没有症状**。缩不出来的后果
不是报错，是「格子依然有图，只是慢」—— 用户看到的就是上一轮的卡顿回来了。
所以这里每一条判据都盯着一个具体的坏法：

1. **缓存键漏了 mtime** → 覆盖过的同名图会画着上一张的脸（ComfyUI 的序号在后端
   重启后从 1 重来，这条在本机是真实会发生的）；
2. **落盘不是原子的** → 两个请求同时要同一张时，其中一个可能读到半个 JPEG，
   页面上是随机一张破图，刷新一下又好了；
3. **缩不出来时抛出去** → 格子上直接没有图（该退回原图）；
4. **收尾把刚写的那一份删掉** → 缓存永远不命中，看起来就是「这个缓存没用」。

Pillow 是硬依赖（`requirements-lock.txt` 里有；本模块的正面判据都要真缩一次图），
与 `test_vision.py` 同一个做法。
"""
import io
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench import thumbs  # noqa: E402

from PIL import Image  # noqa: E402


def real_png(size=(1024, 1024), color=(200, 30, 30), mode="RGB"):
    """一张**真能解码**的 PNG（判「缩得出来吗」只能用这种）。"""
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def broken_png():
    """一块长得像 PNG、但**解不开**的东西（没有 IDAT）。

    这正是 `tests/test_api.py::make_png_blob` 造出来的那种「够路由测试用」的图 ——
    真浏览器打开它是破图。缩略图这条路必须在这里**退回原图**，不许报错。
    """
    body = b"prompt\x00{}"
    return (b"\x89PNG\r\n\x1a\n" +
            struct.pack(">I", len(body)) + b"tEXt" + body +
            struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xFFFFFFFF) +
            struct.pack(">I", 0) + b"IEND" + struct.pack(">I", 0))


class ThumbsBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-thumb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = os.path.join(self.tmp, "thumbs")
        # 进程级的收尾计时器是全局的：不复位的话，前一条判据跑过一次收尾，
        # 后一条就永远看不到收尾发生（**假绿**，而且是那种最像成功的假绿）。
        thumbs._last_prune = 0.0
        self.addCleanup(setattr, thumbs, "_last_prune", 0.0)

    def write(self, name="a.png", blob=None):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(real_png() if blob is None else blob)
        return path


class Key(ThumbsBase):
    """缓存键：认「路径 + mtime + 字节数」，不认序号。"""

    def test_the_same_file_gives_the_same_key(self):
        path = self.write()
        self.assertEqual(thumbs.key_for(path), thumbs.key_for(path))

    def test_rewriting_the_file_changes_the_key(self):
        path = self.write()
        before = thumbs.key_for(path)
        with open(path, "wb") as f:
            f.write(real_png(color=(30, 200, 30)))
        # ★ 把 mtime **显式**推前一秒（这条判据原来靠「两次写入的 mtime 必然不同」，
        # 而那是**赌**，不是判据）。
        #
        # 为什么赌输了：`write()` 造的是 1024² 的**纯色** PNG，这一行换的只是颜色
        # （红 → 绿）。同尺寸、同纯色、同一条编码路径 ⇒ 两份字节**长度完全相同**，
        # 于是键里「字节数」那一项也区分不开，**只剩 mtime_ns**。而两次写入相隔
        # 微秒，Windows 给文件时间的粒度比这粗得多 ⇒ 落在同一个 tick 上时键一个
        # 字节都不差。实测：全套件里撞过一次，而单独跑 8 次全绿。
        #
        # 判据要证明的是「**mtime 在键里**」（漏了它，覆盖过的同名图就会画着上一张
        # 的脸）。那就把 mtime 明确改掉 —— 这样 `key_for` 若不看 mtime，键不变，
        # 这条照样失败；而且不再看运气。
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.assertNotEqual(thumbs.key_for(path), before,
                            "覆盖过的同名图必须换一个键，否则会画着上一张的脸")

    def test_the_target_size_is_part_of_the_key(self):
        path = self.write()
        self.assertNotEqual(thumbs.key_for(path, side=256),
                            thumbs.key_for(path, side=512))

    def test_two_different_files_do_not_share_a_key(self):
        a = self.write("a.png")
        b = self.write("b.png", real_png(color=(1, 2, 3)))
        self.assertNotEqual(thumbs.key_for(a), thumbs.key_for(b))


class Render(ThumbsBase):
    """真的缩一次 —— 尺寸、格式、白底。"""

    def test_it_is_a_jpeg_within_the_side(self):
        blob = thumbs.render(self.write(), side=512)
        self.assertTrue(blob.startswith(b"\xff\xd8\xff"), "应当是 JPEG")
        with Image.open(io.BytesIO(blob)) as im:
            self.assertLessEqual(max(im.size), 512)
            self.assertEqual(im.mode, "RGB")

    def test_the_long_side_is_what_gets_scaled(self):
        path = self.write(blob=real_png(size=(2000, 500)))
        with Image.open(io.BytesIO(thumbs.render(path, side=512))) as im:
            self.assertEqual(im.size, (512, 128))

    def test_a_small_image_is_not_upscaled(self):
        path = self.write(blob=real_png(size=(64, 64)))
        with Image.open(io.BytesIO(thumbs.render(path, side=512))) as im:
            self.assertEqual(im.size, (64, 64))

    def test_transparency_becomes_white_not_black(self):
        """JPEG 没有 alpha：直接 convert("RGB") 会把透明处**变黑**，而那种黑在
        格子上看起来像是作者画出来的 —— 颜色错了不会报错，只会一直错。"""
        buf = io.BytesIO()
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        im.save(buf, "PNG")
        path = self.write(blob=buf.getvalue())
        with Image.open(io.BytesIO(thumbs.render(path, side=512))) as out:
            r, g, b = out.convert("RGB").getpixel((32, 32))
        self.assertGreater(r + g + b, 600, "透明处应当是白的（%d,%d,%d）" % (r, g, b))


class Ensure(ThumbsBase):
    """`ensure()` —— 取图那条路唯一的入口。"""

    def test_it_builds_then_hits(self):
        path = self.write()
        first, why = thumbs.ensure(path, self.cache)
        self.assertEqual(why, "built")
        self.assertTrue(os.path.isfile(first))
        second, why2 = thumbs.ensure(path, self.cache)
        self.assertEqual((second, why2), (first, "hit"))
        self.assertEqual(os.listdir(self.cache), [os.path.basename(first)],
                         "第二次不该再写一份")

    def test_a_broken_png_returns_none_instead_of_raising(self):
        path = self.write(blob=broken_png())
        dest, why = thumbs.ensure(path, self.cache)
        self.assertIsNone(dest, "缩不出来必须让调用方去发原图，而不是抛出去")
        self.assertTrue(why, "理由不能是空的 —— 调用方要用它写日志")

    def test_a_missing_source_returns_none(self):
        dest, why = thumbs.ensure(os.path.join(self.tmp, "nope.png"), self.cache)
        self.assertIsNone(dest)
        self.assertTrue(why)

    def test_without_pillow_it_says_so_and_touches_nothing(self):
        path = self.write()
        with mock.patch.object(thumbs, "Image", None):
            self.assertFalse(thumbs.available())
            self.assertIsNone(thumbs.ensure(path, self.cache)[0])
            self.assertEqual(thumbs.ensure(path, self.cache)[1], "no-pillow")
        self.assertFalse(os.path.isdir(self.cache), "没装 Pillow 时连目录都不该建")
        self.assertEqual(self.cached_count(), 0)

    def test_no_temp_files_are_left_behind(self):
        for i in range(3):
            thumbs.ensure(self.write("a%d.png" % i), self.cache)
        leftovers = [n for n in os.listdir(self.cache) if n.startswith(".tmp")]
        self.assertEqual(leftovers, [], "原子落盘的临时文件必须自己收干净")

    def test_everything_lands_in_the_cache_directory(self):
        path = self.write()
        before = sorted(os.listdir(self.tmp))
        thumbs.ensure(path, self.cache)
        self.assertEqual(sorted(n for n in os.listdir(self.tmp) if n != "thumbs"),
                         before)

    def cached_count(self):
        try:
            return len(os.listdir(self.cache))
        except OSError:
            return 0


class Prune(ThumbsBase):
    """收尾：两个预算、先删最旧、**绝不删刚写的那一份**。"""

    def seed(self, n, size=1000, age=0):
        os.makedirs(self.cache, exist_ok=True)
        rows = []
        for i in range(n):
            path = os.path.join(self.cache, "%02d.jpg" % i)
            with open(path, "wb") as f:
                f.write(b"x" * size)
            # 越后面的越新 —— 收尾删的是「最旧」，判据得知道谁最旧
            os.utime(path, (1000 + i, 1000 + i))
            rows.append(path)
        return rows

    def test_under_budget_is_untouched(self):
        rows = self.seed(3)
        self.assertEqual(thumbs.prune(self.cache, max_files=10,
                                      max_bytes=10 ** 6), 0)
        self.assertEqual(len(os.listdir(self.cache)), 3)

    def test_it_removes_the_oldest_until_under_the_file_budget(self):
        rows = self.seed(5)
        self.assertEqual(thumbs.prune(self.cache, max_files=2, max_bytes=10 ** 6), 3)
        left = sorted(os.listdir(self.cache))
        self.assertEqual(left, [os.path.basename(r) for r in rows[3:]])

    def test_it_also_honours_the_byte_budget(self):
        rows = self.seed(4, size=1000)
        self.assertEqual(thumbs.prune(self.cache, max_files=99, max_bytes=2500), 2)
        self.assertEqual(len(os.listdir(self.cache)), 2)

    def test_it_never_removes_the_one_just_built(self):
        """收尾把自己刚写的那一份删掉 = 下一次请求又重建一遍 —— 看起来就是
        「缓存从来不命中」，而且会一直这样。"""
        rows = self.seed(3)
        newest = max(rows, key=os.path.getmtime)
        thumbs.prune(self.cache, max_files=1, max_bytes=10 ** 6, keep=newest)
        self.assertIn(os.path.basename(newest), os.listdir(self.cache))

    def test_in_flight_temp_files_are_left_alone(self):
        os.makedirs(self.cache, exist_ok=True)
        tmp = os.path.join(self.cache, ".tmp-abc.jpg")
        with open(tmp, "wb") as f:
            f.write(b"x" * 10)
        thumbs.prune(self.cache, max_files=0, max_bytes=0)
        self.assertTrue(os.path.isfile(tmp), "别人的临时文件不许碰")

    def test_maybe_prune_stays_quiet_inside_the_interval(self):
        self.seed(5)
        thumbs._last_prune = 0.0
        self.assertEqual(thumbs.maybe_prune(self.cache, max_files=1, interval=60,
                                            now=100.0), 4)
        self.assertEqual(thumbs.maybe_prune(self.cache, max_files=1, interval=60,
                                            now=110.0), 0,
                         "间隔之内不许再扫一遍目录")

    def test_usage_reports_what_is_there(self):
        self.seed(3, size=1000)
        got = thumbs.usage(self.cache)
        self.assertEqual(got["files"], 3)
        self.assertEqual(got["bytes"], 3000)
        self.assertEqual(thumbs.usage(os.path.join(self.tmp, "nope")),
                         {"files": 0, "bytes": 0})


class RealFiles(support.LeakCheckedTestCase):
    """拿**真实产物**缩一次 —— 现场那几张 4096² / 2896² 正是要治的那批。"""

    def test_real_pngs_shrink_a_lot(self):
        paths = support.real_output_pngs()
        if not paths:
            self.skipTest("output/ 里没有 PNG")
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-thumbreal-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # 最大的那张最说明问题，也最慢 —— 只缩它一张。
        path = max(paths, key=os.path.getsize)
        with open(path, "rb") as f:
            pass
        src_bytes = os.path.getsize(path)
        dest, why = thumbs.ensure(path, tmp)
        self.assertIsNotNone(dest, why)
        with Image.open(dest) as im:
            self.assertLessEqual(max(im.size), thumbs.SIDE)
        self.assertLess(os.path.getsize(dest), src_bytes / 10.0,
                        "最大的一张应当缩到 1/10 以下（%.1f MB → %.2f MB）"
                        % (src_bytes / 1048576.0,
                           os.path.getsize(dest) / 1048576.0))


if __name__ == "__main__":
    unittest.main()
