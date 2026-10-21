"""纯标准库的 PNG 像素读取 —— 只给测试用，不进产品。

为什么需要它：判「空转」（提示词没生效/两张图其实一样）唯一可靠的判据是**比像素**，
而工作台的解释器里没有 numpy 也没有 PIL（技术底座承诺零安装，见 spec §9）。
把像素解码放进产品是不对的 —— 工作台只需要 PNG 的 tEXt（`pngmeta.py` 已经够了），
像素只有测试关心。所以它待在 `tests/` 里，靠标准库 `zlib` 解 IDAT。

只支持 8 位、非隔行、RGB/RGBA —— 正是 ComfyUI 自己写出来的那几种。
**遇到别的会抛异常而不是猜**：静默解错会给出一个**错的**像素差，
那比直接失败危险得多（AGENTS.md「空转」那一节就是被这类假信号坑过）。

可信度：`tests/test_pngpix.py` 拿磁盘上真实的产物验结构，另外用 ComfyUI 那份
带 PIL 的解释器做过一次逐字节对拍（见 README 的 P4 验收）。
"""
import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PngError(ValueError):
    pass


def read_header(blob):
    """返回 (width, height, bit_depth, color_type, channels, interlace)。"""
    if not blob.startswith(PNG_SIG):
        raise PngError("不是 PNG（签名不对）")
    pos = len(PNG_SIG)
    while pos + 8 <= len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        ctype = blob[pos + 4:pos + 8]
        if ctype == b"IHDR":
            body = blob[pos + 8:pos + 8 + length]
            if len(body) < 13:
                raise PngError("IHDR 被截断")
            w, h, depth, color, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body[:13])
            if color not in _CHANNELS:
                raise PngError("不认识的 color_type %d" % color)
            return w, h, depth, color, _CHANNELS[color], interlace
        if ctype == b"IEND":
            break
        pos += 12 + length
    raise PngError("没有找到 IHDR")


def _idat(blob):
    pos = len(PNG_SIG)
    chunks = []
    while pos + 8 <= len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        ctype = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + length]
        if ctype == b"IDAT":
            chunks.append(body)
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if not chunks:
        raise PngError("没有 IDAT")
    return b"".join(chunks)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _as_blob(thing):
    """`str` = 路径，`bytes` = 图像本体。**不做“看着像路径就当路径”的猜测** ——
    猜错会把「这不是 PNG」误报成「找不到文件」，两种病的修法完全不同。"""
    if isinstance(thing, str):
        with open(thing, "rb") as f:
            return f.read()
    if isinstance(thing, (bytes, bytearray)):
        return bytes(thing)
    raise PngError("既不是路径（str）也不是字节：%r" % type(thing))


def pixels(blob_or_path):
    """返回 (width, height, channels, bytearray)。

    `channels` 是每像素的字节数（RGB=3、RGBA=4）。行之间**已经反过滤**，
    所以是可直接逐字节比较的原始像素。
    """
    blob = _as_blob(blob_or_path)

    w, h, depth, _color, channels, interlace = read_header(blob)
    if depth != 8:
        raise PngError("只支持 8 位（这个是 %d 位）" % depth)
    if interlace:
        raise PngError("不支持隔行 PNG")
    stride = w * channels
    try:
        raw = zlib.decompress(_idat(blob))
    except zlib.error as exc:
        # 统一成 PngError —— 调用方只需要认一种异常
        raise PngError("IDAT 解不开：%s" % exc)
    if len(raw) < (stride + 1) * h:
        raise PngError("解出来的数据比标称的短：%d < %d"
                       % (len(raw), (stride + 1) * h))

    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise PngError("第 %d 行的过滤类型不认识：%d" % (y, ftype))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, channels, out


def diff_of_pixels(a, b, limit=None):
    """比较两个**已解码**的像素组 `(w, h, ch, data)`。

    与 `diff_stats` 分开是有原因的：解码一次 1024² 要 **约 4 秒**（纯 Python
    反过滤，实测 1 µs/字节）。调用方往往要拿同一张图比好几次，先 `pixels()`
    一次再反复调这里，就把 N 次解码压成 1 次 —— 冒烟脚本尤其需要。

    `identical` **永远是精确的**（大整数 XOR，C 速度）；百分比类只在
    `changed_pct` / `mean_abs` / `max_abs` 上受 `limit` 影响，抽样时 `sampled=True`。
    """
    wa, ha, ca, pa = a
    wb, hb, cb, pb = b
    if (wa, ha, ca) != (wb, hb, cb):
        raise PngError("两张图尺寸/通道不同：%sx%sx%s vs %sx%sx%s"
                       % (wa, ha, ca, wb, hb, cb))
    total = len(pa)

    # 精确的「是否逐像素相同」，一次大整数运算而不是 400 万次循环
    identical = (int.from_bytes(pa, "big") ^ int.from_bytes(pb, "big")) == 0

    changed = 0
    diff_sum = 0
    max_abs = 0
    if identical:
        # XOR 已经覆盖了全部字节，统计量是**精确的 0**，不是「抽了一部分」
        scope = total
        sampled = False
    else:
        scope = total if limit is None else min(limit, total)
        sampled = scope != total
        for i in range(scope):
            d = pa[i] - pb[i]
            if d:
                if d < 0:
                    d = -d
                diff_sum += d
                if d > max_abs:
                    max_abs = d
                changed += 1
    return {
        "width": wa, "height": ha, "channels": ca,
        "bytes": total,
        "compared_bytes": scope,
        "sampled": sampled,
        "changed_bytes": changed,
        "changed_pct": round(100.0 * changed / scope, 3) if scope else 0.0,
        "mean_abs": round(diff_sum / float(scope), 6) if scope else 0.0,
        "max_abs": max_abs,
        # 关键判定：True = 逐像素相同。若本来该不同，那就是**空转**。
        "identical": identical,
    }


def diff_stats(a, b, limit=None):
    """便利包装：两张图（路径或字节）解码后交给 `diff_of_pixels`。

    要拿同一张图比多次请直接用 `diff_of_pixels`，别反复走这里 ——
    每次调用都会重新解码（1024² 约 4 秒）。
    """
    return diff_of_pixels(pixels(a), pixels(b), limit=limit)
