#!/usr/bin/env python3
"""给 LLM 看的那一份参考图 —— 缩图、白底、摆正，以及**每一种失败**。

为什么这个小模块也值得一整套判据：它的失败**没有症状**。少缩一次只是多传几 MB，
压缩坏了只是模型看得糊一点，而**唯一真正致命的那一条**（图没上车）在这里被挡住 ——
把一份空图或一张解不开的图「当作没有图」发出去，实测会让模型**照着看不见的图编**
（见 `logs/llm_vision_probe.log`），而那段编出来的描述会被「采用」写进提示词。
所以这一层的每一个 `raise` 都要有判据钉着。
"""
import base64
import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench import vision  # noqa: E402

from PIL import Image  # noqa: E402


def png(size, color=(200, 30, 30), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def decoded(prepared):
    return Image.open(io.BytesIO(base64.b64decode(prepared["b64"])))


def photo_like(size=(2048, 2048), seed=7):
    """一张「像照片」的图：低频大色块 + 高频细节。

    刻意**不**用纯色图测「省了多少」—— 纯色 PNG 压得比 JPEG 还好，拿它当基准会
    得出反的结论（实测 2048² 纯色：PNG 17 KB、存成 JPEG 反而 6 KB，只有 2.6 倍，
    而同样大小的照片型图 PNG 5523 KB → JPEG 189 KB，29 倍）。
    """
    import random
    rnd = random.Random(seed)
    n = size[0]
    small = Image.new("RGB", (max(1, n // 32), max(1, size[1] // 32)))
    small.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                   for _ in range(small.width * small.height)])
    im = small.resize(size, Image.BICUBIC)
    px = im.load()
    for y in range(0, size[1], 3):
        for x in range(0, n, 3):
            r, g, b = px[x, y]
            d = rnd.randrange(-9, 10)
            px[x, y] = (max(0, min(255, r + d)), max(0, min(255, g + d)),
                        max(0, min(255, b + d)))
    return im


class Prepare(support.LeakCheckedTestCase):

    def test_a_big_image_is_shrunk_to_the_long_side(self):
        """2048×1024 → 长边 1024，**比例不变**（1024×512，不是 1024×1024）。"""
        out = vision.prepare("big.png", png((2048, 1024)))
        self.assertEqual((out["width"], out["height"]), (1024, 512))
        self.assertEqual((out["src_width"], out["src_height"]), (2048, 1024))
        self.assertEqual(out["mime"], "image/jpeg")

    def test_a_small_image_is_not_blown_up(self):
        """小图不放大 —— 放大只是把字节数涨上去，一个新像素都换不来。"""
        out = vision.prepare("small.png", png((200, 150)))
        self.assertEqual((out["width"], out["height"]), (200, 150))

    def test_it_is_dramatically_smaller_than_the_source(self):
        """这就是这一层存在的理由：本机参考图实测 1.6~8.4 MB，而端点在远端。

        用「像照片」的图当基准（见 `photo_like`）：实测 2048² 5523 KB → 189 KB，
        **29 倍**。判据留到 8 倍这么宽，是因为这条测的是「数量级」，
        不是在钉某个 JPEG 版本的具体压缩率。
        """
        buf = io.BytesIO()
        photo_like().save(buf, "PNG")
        src = buf.getvalue()
        out = vision.prepare("huge.png", src)
        self.assertEqual(out["bytes"], len(base64.b64decode(out["b64"])))
        self.assertLessEqual(max(out["width"], out["height"]), vision.MAX_SIDE)
        self.assertLess(out["bytes"], len(src) / 8,
                        "缩完还这么大，等于没缩：%d → %d" % (len(src), out["bytes"]))

    def test_transparency_is_flattened_onto_white(self):
        """透明压到黑底会让模型看见一张比实际更暗的图。"""
        buf = io.BytesIO()
        Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(buf, "PNG")   # 全透明
        out = vision.prepare("clear.png", buf.getvalue())
        px = decoded(out).convert("RGB").getpixel((32, 32))
        self.assertTrue(all(v > 240 for v in px), "透明区没有压成白底：%r" % (px,))

    def test_exif_orientation_is_applied(self):
        """手机照片带方向：不摆正，模型看到的是一张躺着的图，而它会照着躺着的写。"""
        buf = io.BytesIO()
        im = Image.new("RGB", (40, 20), (10, 200, 10))
        exif = Image.Exif()
        exif[274] = 6                                    # 顺时针 90°
        im.save(buf, "JPEG", exif=exif)
        out = vision.prepare("phone.jpg", buf.getvalue())
        self.assertEqual((out["width"], out["height"]), (20, 40))

    def test_the_source_size_is_reported_alongside_the_output_size(self):
        out = vision.prepare("big.png", png((1500, 700)))
        self.assertEqual(out["src_bytes"], len(png((1500, 700))))
        self.assertEqual(out["name"], "big.png")

    # --- 失败：四种，一种都不许变成「那就当没有图吧」 ----------------------

    def test_empty_bytes_are_refused(self):
        with self.assertRaises(vision.ImageError) as cm:
            vision.prepare("empty.png", b"")
        self.assertEqual(cm.exception.kind, "empty")

    def test_garbage_is_refused_with_the_decode_kind(self):
        with self.assertRaises(vision.ImageError) as cm:
            vision.prepare("broken.png", b"not an image at all" * 8)
        self.assertEqual(cm.exception.kind, "decode")

    def test_an_oversize_file_is_refused_before_decoding(self):
        with mock.patch.object(vision, "MAX_SRC_BYTES", 16):
            with self.assertRaises(vision.ImageError) as cm:
                vision.prepare("fat.png", b"x" * 32)
        self.assertEqual(cm.exception.kind, "too_big")

    def test_without_pillow_it_says_how_to_install_it(self):
        """少一个包不该把整个工作台拖下水（import 是 try/except 的），
        而**走到这一步**时要说得清怎么修 —— 只报一个 ImportError 会像产品坏了。"""
        with mock.patch.object(vision, "Image", None), \
                mock.patch.object(vision, "_PIL_ERROR", "ImportError: 没有这个模块"):
            with self.assertRaises(vision.ImageError) as cm:
                vision.prepare("a.png", b"x")
        self.assertEqual(cm.exception.kind, "missing")
        self.assertIn("requirements-lock.txt", str(cm.exception))

    def test_every_failure_names_the_file(self):
        """面板里那一行要说得清是**哪一张**取不到/解不开 —— 「有一张图失败了」
        等于让用户自己去猜哪一张（而参考图常常挂着三五张）。"""
        cases = [(b"", "empty.png"), (b"garbage" * 10, "broken.png")]
        for data, name in cases:
            with self.assertRaises(vision.ImageError) as cm:
                vision.prepare(name, data)
            self.assertIn(name, str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
