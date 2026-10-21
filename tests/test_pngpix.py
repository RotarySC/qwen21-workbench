#!/usr/bin/env python3
"""纯标准库 PNG 像素读取 —— 这块代码的价值全在「它算得对不对」。

所以这里干两件事：
1. 对磁盘上**真实的** ComfyUI 产物验结构与自洽（找不到产物就跳过，
   与 test_pngmeta.py 同一个做法）；
2. 对畸形输入证明它**抛异常**而不是返回一个错的像素数组 ——
   静默解错会给出一个错的像素差，那正是「空转」判据最怕的假信号。

与 PIL 的逐字节对拍不在这里（那要 ComfyUI 那份带 PIL 的解释器），
结果记在 README 的 P4 验收里：6 张真实产物全部 byte-identical。
"""
import hashlib
import os
import struct
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pngpix  # noqa: E402
import support  # noqa: E402  (测试基类 / 临时目录)

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def real_outputs():
    """真实出图的路径 —— 由 `support` 按**候选目录**找，别在这儿写死安装目录。"""
    return support.real_output_pngs()


_DECODED = {}


def decoded(path):
    """解一次就记住 —— 1024² 纯 Python 反过滤要 **约 4 秒**（实测），
    同一条测试里反复解同一张图会把 `unittest discover` 从 16 秒拖到 70 秒。
    """
    key = (path, os.path.getmtime(path))
    if key not in _DECODED:
        _DECODED[key] = pngpix.pixels(path)
    return _DECODED[key]


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def forward_filter(rows, channels, ftypes):
    """按指定的过滤类型把原始行**正向**过滤并打包。

    这是解码方向的逆运算，用来造出五种过滤类型齐备的测试图 ——
    真实产物只用到 1 和 4（实测 1023 行 Paeth + 1 行 Sub），
    0/2/3 那三条分支只能这样覆盖。造出来的文件另外用 PIL 解过一遍，
    确认它是符合规范的 PNG（否则就变成「我的两个函数自己对自己」）。
    """
    out = bytearray()
    prev = bytearray(len(rows[0]))
    for row, ft in zip(rows, ftypes):
        stride = len(row)
        line = bytearray(stride)
        for i in range(stride):
            a = row[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ft == 0:
                line[i] = row[i]
            elif ft == 1:
                line[i] = (row[i] - a) & 0xFF
            elif ft == 2:
                line[i] = (row[i] - b) & 0xFF
            elif ft == 3:
                line[i] = (row[i] - ((a + b) >> 1)) & 0xFF
            elif ft == 4:
                line[i] = (row[i] - _paeth(a, b, c)) & 0xFF
            else:
                raise ValueError("bad ftype")
        out.append(ft)
        out += line
        prev = bytearray(row)
    return bytes(out)


def _chunk(ctype, body):
    return (struct.pack(">I", len(body)) + ctype + body +
            struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))


def make_png(pixels, ftypes, color_type=2):
    """造一张真的 PNG。`pixels` 是 bytes（RGB 或 RGBA 的连续像素）。"""
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    total = len(pixels)
    # 尺寸由像素数与行数推：调用方给整幅像素，行数 = ftypes 的长度
    h = len(ftypes)
    stride = total // h
    assert stride * h == total, "像素数与行数对不上"
    w = stride // channels
    rows = [pixels[y * stride:(y + 1) * stride] for y in range(h)]
    raw = forward_filter(rows, channels, ftypes)
    return (PNG_SIG +
            _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type,
                                        0, 0, 0)) +
            _chunk(b"IDAT", zlib.compress(raw, 9)) +
            _chunk(b"IEND", b""))


def patch_ihdr(blob, offset, value):
    """改 IHDR 里的一个字节。解析器不校验 CRC，所以这样够用。**
    """
    pos = blob.index(b"IHDR") + 4
    out = bytearray(blob)
    out[pos + offset] = value
    return bytes(out)


# color_type → 每像素通道数。**只此一份** —— 造图与验图必须用同一张表，
# 否则会出现「文件是 RGBA、像素却只按 RGB 数」这种自己骗自己的情形
# （第一次写这块时就栽在这儿：把 color_type 6 也当成 3 通道了）。
CT_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def handmade(w, h, ftypes, color_type=2, seed=0):
    """造一张尺寸、过滤方式、像素**全都已知**的 PNG。返回 (blob, pixels, channels)。"""
    channels = CT_CHANNELS[color_type]
    pix = bytes(((i * 7 + (i // channels) * 13 + seed) & 0xFF)
                for i in range(w * h * channels))
    return make_png(pix, ftypes, color_type), pix, channels


class HandBuiltExactness(support.LeakCheckedTestCase):
    """五种过滤类型各造一张，逐像素比已知答案。

    真实产物只用到过滤类型 1 和 4（实测 1023 行 Paeth + 1 行 Sub），
    所以 0/2/3 那三条分支只能靠这样造出来的图覆盖。造出来的文件另外交给
    PIL 解过一遍、确认像素与预期一致（见 README 的 P4 验收），
    这样这条测试就不是「我的两个函数自己对自己」。
    """

    def test_every_filter_type_round_trips_exactly(self):
        for ft in (0, 1, 2, 3, 4):
            with self.subTest(ftype=ft):
                blob, pix, ch = handmade(5, 4, [ft] * 4)
                w, h, got_ch, got = pngpix.pixels(blob)
                self.assertEqual((w, h, got_ch), (5, 4, ch))
                self.assertEqual(bytes(got), pix)

    def test_a_mix_of_filters_in_one_image(self):
        """真实编码器是逐行挑过滤方式的 —— 一张图里混着来也要对。"""
        blob, pix, ch = handmade(7, 5, [0, 1, 2, 3, 4])
        w, h, got_ch, got = pngpix.pixels(blob)
        self.assertEqual((w, h, got_ch), (7, 5, ch))
        self.assertEqual(bytes(got), pix)

    def test_rgba_round_trips(self):
        blob, pix, ch = handmade(4, 3, [4, 1, 0], color_type=6)
        self.assertEqual(ch, 4)
        w, h, got_ch, got = pngpix.pixels(blob)
        self.assertEqual((w, h, got_ch), (4, 3, 4))
        self.assertEqual(bytes(got), pix)

    def test_greyscale_round_trips(self):
        blob, pix, ch = handmade(6, 2, [1, 4], color_type=0)
        self.assertEqual(ch, 1)
        w, h, got_ch, got = pngpix.pixels(blob)
        self.assertEqual((w, h, got_ch), (6, 2, 1))
        self.assertEqual(bytes(got), pix)

    def test_diff_of_two_known_images(self):
        a, _pa, _c = handmade(4, 2, [0, 0], seed=0)
        b, _pb, _c2 = handmade(4, 2, [0, 0], seed=1)
        stats = pngpix.diff_stats(a, b)
        self.assertFalse(stats["identical"])
        self.assertEqual(stats["bytes"], 4 * 2 * 3)
        self.assertGreater(stats["changed_bytes"], 0)

    def test_diff_of_different_sizes_is_refused(self):
        a, _pa, _c = handmade(4, 2, [0, 0])
        b, _pb, _c2 = handmade(4, 8, [0] * 8)        # 两张都是**合法**的 PNG
        self.assertEqual(pngpix.pixels(a)[:2], (4, 2))
        self.assertEqual(pngpix.pixels(b)[:2], (4, 8))
        with self.assertRaises(pngpix.PngError):
            pngpix.diff_stats(a, b)


class OnRealOutputs(support.LeakCheckedTestCase):
    """没有真实产物时整类跳过 —— 这些断言只有在有图可验时才有意义。

    这里**刻意只用一张**真实产物：解一张 1024² 要 4 秒，多用几张就是在给
    单测上罚时。畸形输入那类改用合成图（`handmade`），一样能覆盖全部代码路径。
    """

    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.files = real_outputs()
        if not self.files:
            self.skipTest("磁盘上没有 ComfyUI 产物可验")
        self.path = self.files[-1]

    def test_decodes_and_is_self_consistent(self):
        w, h, ch, data = decoded(self.path)
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)
        self.assertIn(ch, (3, 4))
        self.assertEqual(len(data), w * h * ch)

    def test_matches_the_ihdr_dimensions(self):
        w, h, ch, data = decoded(self.path)
        with open(self.path, "rb") as f:
            w2, h2, _d, _c, n, interlace = pngpix.read_header(f.read())
        self.assertEqual((w, h, ch), (w2, h2, n))
        self.assertEqual(interlace, 0)
        self.assertEqual(len(data), w * h * n)

    def test_an_image_is_identical_to_itself(self):
        """自比必须相同 —— 这是判空转的基线，走精确的大整数路径。"""
        d = decoded(self.path)
        stats = pngpix.diff_of_pixels(d, d)
        self.assertTrue(stats["identical"])
        self.assertFalse(stats["sampled"])
        self.assertEqual(stats["changed_bytes"], 0)
        self.assertEqual(stats["mean_abs"], 0.0)
        self.assertEqual(stats["max_abs"], 0)

    def shape_of(self, path):
        """只看 IHDR 就够（64 字节），不用解整幅 —— 挑配对时不需要像素。"""
        with open(path, "rb") as f:
            w, h, _d, _c, ch, _i = pngpix.read_header(f.read(64))
        return (w, h, ch)

    def test_detector_says_both_yes_and_no(self):
        """这条测的是**仪器**，不是出图：同一个函数必须既能判「不同」也能判「相同」。

        为什么要正反两面都测：如果它永远返回 identical=True，只测「不同」才会
        发现；反过来永远返回 False，只测「相同」才会发现。只测一边，
        另一种坏法就藏在「测试通过」后面。

        取样用**文件哈希**当独立信号：哈希相同 ⇒ 内容应当逐像素相同
        （同 seed 同提示词重跑两次就是这种情形，实测确实如此 ——
        本机的 output/ 里就躺着这样两对）。哈希不同 ⇒ 应当不同。
        尺寸必须配对，不然 diff 会按设计抛异常（不同尺寸不去比）。
        """
        by_shape = {}
        for path in self.files:
            by_shape.setdefault(self.shape_of(path), []).append(path)
        group = max(by_shape.values(), key=len) if by_shape else []
        by_hash = {}
        for path in group:
            with open(path, "rb") as f:
                blob = f.read()
            by_hash.setdefault(hashlib.sha256(blob).hexdigest(), []).append(path)

        same = next((v for v in by_hash.values() if len(v) >= 2), None)
        if same:
            stats = pngpix.diff_of_pixels(decoded(same[0]), decoded(same[1]))
            self.assertTrue(stats["identical"],
                            "文件哈希相同却解出了不同的像素 —— 解码器有问题")
            self.assertEqual(stats["changed_bytes"], 0)

        diff = None
        keys = sorted(by_hash)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                if keys[i] != keys[j]:
                    diff = (by_hash[keys[i]][0], by_hash[keys[j]][0])
                    break
            if diff:
                break
        if diff is None:
            self.skipTest("这一组里没有两张内容不同的产物")
        stats = pngpix.diff_of_pixels(decoded(diff[0]), decoded(diff[1]),
                                      limit=300000)
        self.assertFalse(stats["identical"],
                         "文件哈希不同却是同一批像素 —— 判据会漏报空转")
        self.assertTrue(stats["sampled"])
        self.assertEqual(stats["compared_bytes"], 300000)
        self.assertGreater(stats["changed_pct"], 50.0)

    def test_a_real_png_also_reads_as_metadata(self):
        """解码器不该影响 pngmeta —— 尺寸这条信息两条路必须一致。

        **这条用例原来是取 `files[-1]`（最新一张）单独断言，那是个坑。**
        「最新一张恰好是哪张图」会决定它测什么：基准图出来的产物宽高是**链接**
        （指向 ResolutionSelector），而 pngmeta 对链接型尺寸**有意**返回 None
        （另有 test_linked_dimensions_report_none_not_a_number 钉着这条）。
        于是往 output/ 里多放几张基准图产物，这条用例就开始断言一件本来就不
        成立的事 —— 而它报的错（"None != 1024"）看起来像解码器坏了。

        改成对**每一张**真实产物断言那条永远成立的关系：
        pngmeta 给出的尺寸**要么是 None（链接型，它就该是 None），要么等于 PNG
        真正的像素尺寸** —— 也就是「它从不编一个错的数」。
        只看 tEXt 与 IHDR，不解像素，所以 72 张也就几十毫秒。
        """
        from workbench import pngmeta
        numeric = 0
        for path in self.files:
            with open(path, "rb") as f:
                blob = f.read()
            info = pngmeta.summarize(pngmeta.prompt_workflow(blob) or {})
            w, h, _d, _c, _n, _i = pngpix.read_header(blob)
            for field, got, real in (("width", info["width"], w),
                                     ("height", info["height"], h)):
                if got is None:
                    continue          # 链接型尺寸：pngmeta 有意报 None
                numeric += 1
                self.assertEqual(got, real,
                                 "%s 的 %s：pngmeta 说 %r，实际像素是 %r"
                                 % (os.path.basename(path), field, got, real))
        # 一条字面尺寸都没有的话，上面的循环一次都没断言 —— 那是**静默空转**，
        # 正是这个仓库最想消灭的东西。本机实测有 14 张带字面尺寸。
        self.assertGreater(numeric, 0,
                           "output/ 里没有一张带字面宽高的产物 —— 这条断言等于没跑")

    def test_accepts_a_raw_blob_as_well_as_a_path(self):
        with open(self.path, "rb") as f:
            blob = f.read()
        self.assertEqual(pngpix.pixels(blob), decoded(self.path))


class Malformed(support.LeakCheckedTestCase):
    """畸形输入一律用**合成图**，不碰真实产物 ——
    这些用例要的是「解析器会拒绝」，样本多大无关紧要，
    而解一张 1024² 要 4 秒，用真图就是白花时间。
    """

    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.good, self.pix, self.ch = handmade(9, 7, [0, 4, 1, 2, 3, 0, 4],
                                                color_type=6)
        self.assertIsNotNone(self.pix)

    def test_not_a_png(self):
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(b"not a png at all, definitely not")

    def test_empty_blob(self):
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(PNG_SIG)

    def test_a_plain_path_string_that_does_not_exist(self):
        """`str` 一律当路径。读不到就抛 FileNotFoundError，不伪装成 PngError。"""
        with self.assertRaises(FileNotFoundError):
            pngpix.pixels(os.path.join(HERE, "definitely-not-here.png"))

    def test_sixteen_bit_is_refused_not_guessed(self):
        """16 位按 8 位解会得到一个「看起来合理」的数组 —— 必须拒绝。"""
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(patch_ihdr(self.good, 8, 16))

    def test_interlaced_is_refused(self):
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(patch_ihdr(self.good, 12, 1))

    def test_unknown_color_type_is_refused(self):
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(patch_ihdr(self.good, 9, 99))

    def test_broken_idat_is_reported_as_png_error(self):
        pos = self.good.index(b"IDAT")
        out = bytearray(self.good)
        out[pos + 4:pos + 12] = b"\x00" * 8        # 破坏压缩流
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(bytes(out))

    def test_truncated_after_idat_does_not_crash_weirdly(self):
        cut = self.good[:self.good.index(b"IDAT") + 20]
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(cut)

    def test_a_short_row_stream_is_refused(self):
        """IDAT 解出来的比标称的短 —— 不能补零凑合，那会给出错的行。"""
        w, h, depth, color, ch, inter = pngpix.read_header(self.good)
        raw = zlib.compress(b"\x00" + b"\x00" * (w * ch))    # 只给一行
        blob = (PNG_SIG +
                _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0)) +
                _chunk(b"IDAT", raw) + _chunk(b"IEND", b""))
        self.assertLess(len(zlib.decompress(raw)), (w * ch + 1) * h)
        with self.assertRaises(pngpix.PngError):
            pngpix.pixels(blob)

    def test_bad_argument_type(self):
        for bad in (None, 42, 3.5, ["a"]):
            with self.assertRaises(pngpix.PngError):
                pngpix.pixels(bad)


class HeaderRead(support.LeakCheckedTestCase):
    def test_reading_a_header_needs_no_idat(self):
        """IHDR 在最前面 —— 只读它的时候不该要求整张图完整。"""
        files = real_outputs()
        if not files:
            self.skipTest("磁盘上没有 ComfyUI 产物")
        with open(files[-1], "rb") as f:
            head = f.read(64)
        w, h, depth, color, ch, interlace = pngpix.read_header(head)
        self.assertGreater(w, 0)
        self.assertEqual(depth, 8)
        self.assertIn(color, (2, 6))
        self.assertEqual(interlace, 0)
        self.assertEqual(ch, 4 if color == 6 else 3)


if __name__ == "__main__":
    unittest.main()
