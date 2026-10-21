#!/usr/bin/env python3
"""A/B 对照的仪器 —— 把「只改一个旋钮」变成可复用的动作。

这个模块是 2026-09-30 那轮 TE-Speed 参数扫描里反复手写的东西的正式版。
它只做四件事，每一件都是那一轮**真栽过一次**的：

1. **底图必须从真实产物里取，不要照文档重拼。**
   `reference_graph()` 从一张 PNG 的 tEXt 里取回「当时真正提交的那份图」。
   自己拼第二份基准图就等于引入了第二个变量，而两个变量一起动的时候，
   两组数字**都解释不了**。这是整轮扫描里最值钱的一个动作。

2. **接不上必须当场炸。**
   `insert_model_node()` 把 `QwenImage21Cache → KSampler` 那条边改成
   `→ 新节点 →`。扫描是**成批**跑的：有一格没接上，出来的就是「那一格没变快」
   —— 看起来像一条结论，其实是仪器坏了。这个仓库里「能出图但一秒没省」的错
   没有任何症状，所以要在这里拦住。

3. **你传的参数不一定等于生效的参数。**
   `effective_mismatch()` 拿「我要的值」对「它自报的值」。实测 TE-Speed：
   `reuse_threshold=0.0` 会被改写成 **0.018**、`start/end_percent=0.0` 会变成
   窗口 0.18-0.86，而 `0.001`/`0.017`/`0.019` **全部原样保留** —— 不是下限
   夹紧，是恰好 `0.0` 在这个插件里有特殊含义（"自动"）。没有这条检查，那一轮
   会把 0.0 当成「关掉」，而它根本不是，整批结论的量纲就全错了。

4. **「变了多少」有两个都叫 changed_pct 的量。**
   `diff_stats()` 一次返回两个，名字里写死是哪个：
     `byte_*`  —— 逐**字节**、**全部**通道（含 alpha），与
                  `pngpix.diff_of_pixels` 完全一致；
     `pixel_*` —— 逐**像素**、**只有颜色**通道（RGB）。
   同一对文件实测差 40 个百分点（54.839% 对 94.758%）。混用过一次，整张表的
   量纲就悄悄换掉了 —— 而两张表看起来都「正常」。仓库里还有第三个陷阱：
   ComfyUI 的产物 alpha **并不恒为 255**（实测 253/254/255），所以含 alpha 的
   那份均值里混着与画面无关的噪声。

**只用标准库**（配合 `tests/pngpix.py`）：工作台的解释器里没有 numpy 也没有
PIL，这是技术底座承诺的「零安装」。这也是为什么两个口径**只有一个实现** ——
换 numpy 再算一遍，就又有了两份定义，而那正是第 4 条要防的事。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pngpix                      # noqa: E402
import qwen21_build as qb          # noqa: E402
from workbench import pngmeta      # noqa: E402

KSAMPLER = qb.KSAMPLER
CACHE = qb.CACHE


class ProbeError(ValueError):
    """仪器的输入不对：不是 PNG / 不是这条链路 / 底图接法变了。

    刻意与 `pngpix.PngError` 分开 —— 那个说的是「这张图解不开」，这个说的是
    「这张图读得出，但它不是我要的那种图」。处置完全不同，混在一起会误导。
    """


# ---------------------------------------------------------------------------
# 底图
# ---------------------------------------------------------------------------
def reference_graph(png, *, ksampler=KSAMPLER):
    """从一张**真实产物**里取回当时真正提交的那份 API 图（新的对象，随便改）。

    `png` 可以是路径也可以是 PNG 字节。读不出来 / 不是这条链路就抛 `ProbeError`。
    """
    wf = pngmeta.prompt_workflow(png)
    if not isinstance(wf, dict) or not wf:
        raise ProbeError(
            "这张 PNG 里没有 prompt 元数据 —— 它多半不是 ComfyUI 出的图"
            "（read_image 之类工具重存过的副本也会把它洗掉）")
    if ksampler not in wf:
        raise ProbeError("这份图里没有节点 %r —— 不是这条 Qwen-Image-2.1 链路"
                         % ksampler)
    return wf


def model_chain_tail(wf, *, ksampler=KSAMPLER):
    """KSampler 现在吃的 model 来自谁。

    用来自检「我以为的那条边」就是「真的那条边」—— 接加速节点之前之后都该看一眼。
    """
    return (wf.get(ksampler) or {}).get("inputs", {}).get("model")


def insert_model_node(wf, node_id, class_type, inputs, *, ksampler=KSAMPLER,
                      cache=CACHE, title=None):
    """把 `cache → ksampler` 改成 `cache → 新节点 → ksampler`（**就地**改 wf）。

    和 `qwen21_build._wire_*` 一个套路，但这里要能插**任意**节点 —— 扫描的时候
    被扫的插件通常还没进构建器（它可能永远不会进）。

    `inputs` 里就算带了 `model` 也会被覆盖：这个节点的新上游已经由参数决定了，
    让它自己填就等于允许"接错上游还能跑"。

    接不上就抛 `ProbeError`，理由见模块说明的第 2 条。
    """
    if node_id in wf:
        raise ProbeError("节点 id %r 已经在图里了 —— 插进去会覆盖掉原节点"
                         % node_id)
    tail = model_chain_tail(wf, ksampler=ksampler)
    if tail != [cache, 0]:
        raise ProbeError(
            "%s 的 model 指向 %r，不是 %r；没法把 %s 接在 QwenImage21Cache "
            "后面（已经有别的节点占住了同一条边？加速类插件都在抢它）"
            % (ksampler, tail, [cache, 0], class_type))
    node = {"class_type": class_type, "inputs": dict(inputs)}
    if title:
        node["_meta"] = {"title": title}
    node["inputs"]["model"] = [cache, 0]
    wf[node_id] = node
    wf[ksampler]["inputs"]["model"] = [node_id, 0]
    return wf


# ---------------------------------------------------------------------------
# 「我传的」对「它自报的」
# ---------------------------------------------------------------------------
_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
_KV = re.compile(r"([A-Za-z_]\w*)=(\S+)")


def parse_kv(text):
    """把一行自报参数拆成字典：`threshold=0.200000 window=0.18-0.86 ...`

    两件实测过的小事：
      · 数值要**按数**比 —— `0.200000` 与 `0.2` 是同一个值；
      · `window=0.18-0.86` 里那个连字符是**值的一部分**，不能当分隔符。
    所以取值用的是「非空白」而不是某个字符集。
    """
    out = {}
    for key, raw in _KV.findall(text or ""):
        out[key] = float(raw) if _NUMBER.match(raw) else raw
    return out


def _same(want, got):
    if want == got:
        return True
    try:
        return abs(float(want) - float(got)) <= 1e-9
    except (TypeError, ValueError):
        return False


def effective_mismatch(requested, reported):
    """`[(key, 我要的, 它自报的), ...]` —— 空列表才叫「我传的就是生效的」。

    `reported` 用 `parse_kv()` 拆一行日志或一次 status 输出就有。
    值为 `None` 表示它**压根没报这一项** —— 那也是一种 mismatch，别忽略。
    """
    out = []
    for key, want in (requested or {}).items():
        if key not in reported:
            out.append((key, want, None))
        elif not _same(want, reported[key]):
            out.append((key, want, reported[key]))
    return out


# ---------------------------------------------------------------------------
# 像素
# ---------------------------------------------------------------------------
def pixels(png):
    """`pngpix.pixels()` 的直通 —— 放这里只是让调用方只 import 一个模块。"""
    return pngpix.pixels(png)


def diff_stats(a, b):
    """两个**都已解码**的像素组之间的差（`pngpix.pixels()` 的返回值）。

    一次遍历给出两种口径。**它们的名字不同，不是同义词**：

        byte_pct / byte_mean_abs   分母 = 总字节数（**含 alpha**）
        pixel_pct                  分母 = 像素数，任一**颜色**通道不同才算变了
        pixel_mean_abs             分母 = 像素数 × 颜色通道数（RGB）

    先 `pixels()` 一次、再反复调这里 —— 解一张 1024² 要约 4 秒，别反复解。
    `identical`（逐位相同）走大整数 XOR，是精确的，不受任何抽样影响。
    """
    wa, ha, ca, pa = a
    wb, hb, cb, pb = b
    if (wa, ha, ca) != (wb, hb, cb):
        raise pngpix.PngError("两张图尺寸/通道不同：%sx%sx%s vs %sx%sx%s"
                              % (wa, ha, ca, wb, hb, cb))
    total = len(pa)
    npix = wa * ha
    color = 3 if ca >= 3 else 1        # RGB/RGBA 的前三个是颜色；灰度就一个

    if (int.from_bytes(pa, "big") ^ int.from_bytes(pb, "big")) == 0:
        return {"width": wa, "height": ha, "channels": ca, "pixels": npix,
                "color_channels": color, "identical": True,
                "byte_pct": 0.0, "byte_mean_abs": 0.0,
                "pixel_pct": 0.0, "pixel_mean_abs": 0.0,
                "max_abs": 0, "max_abs_color": 0}

    changed_bytes = byte_sum = max_abs = max_abs_color = 0
    changed_px = px_sum = 0
    for p in range(npix):
        base = p * ca
        hit = False
        for c in range(ca):
            d = pa[base + c] - pb[base + c]
            if not d:
                continue
            if d < 0:
                d = -d
            changed_bytes += 1
            byte_sum += d
            if d > max_abs:
                max_abs = d
            if c < color:
                px_sum += d
                hit = True
                if d > max_abs_color:
                    max_abs_color = d
        if hit:
            changed_px += 1
    return {
        "width": wa, "height": ha, "channels": ca, "pixels": npix,
        "color_channels": color, "identical": False,
        "byte_pct": round(100.0 * changed_bytes / total, 3),
        "byte_mean_abs": round(byte_sum / float(total), 6),
        "pixel_pct": round(100.0 * changed_px / npix, 3),
        "pixel_mean_abs": round(px_sum / float(npix * color), 6),
        "max_abs": max_abs, "max_abs_color": max_abs_color,
    }


def gradient_energy(px, channel=1):
    """相邻像素梯度能量 —— 判「细节有没有被抹平」的那把尺。

        g = mean( (|I[y][x+1]-I[y][x]| + |I[y+1][x]-I[y][x]|) / 2 )

    默认取绿通道（索引 1）；灰度图只有一个通道时自动退到它。

    **绝对值没有意义，只能与同一批的原图比。** 真实照片在 1.9 上下；被抹平的
    会明显低于自己那张原图（实测 LazyCache 1.956 → 0.953）；纯噪声高得多
    （十几）。换通道或换公式，数就换了一把尺子 —— 这一轮就吃过一次这个亏，
    所以公式写死在这里、只此一份。
    """
    w, h, ch, data = px
    if ch <= 0 or w < 2 or h < 2:
        return 0.0
    if channel >= ch:
        channel = 0
    stride = w * ch
    dx = dy = 0
    for y in range(h):
        base = y * stride + channel
        for x in range(w - 1):
            i = base + x * ch
            dx += abs(data[i + ch] - data[i])
    for y in range(h - 1):
        base = y * stride + channel
        for x in range(w):
            i = base + x * ch
            dy += abs(data[i + stride] - data[i])
    return round((dx / float((w - 1) * h) + dy / float(w * (h - 1))) / 2.0, 4)
