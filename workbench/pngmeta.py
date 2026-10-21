"""PNG tEXt 元数据解析 —— 纯标准库，不需要 Pillow。

ComfyUI 把**整份提交的 API 图**塞进 PNG 的 `tEXt` 块，键名 `prompt`。
这让画廊不必建数据库：扫目录读元数据就能知道模式/seed/步数/尺寸/提示词，
而且**后端没起的时候也能用**。

为什么不用 Pillow：`spec §11` 把缩略图缓存明确列为不做，而工作台的解释器里
本来也没有 Pillow。这一个函数就是全部的图像解析需求。

只处理 `tEXt`（latin-1，明文）与 `iTXt`（UTF-8，未压缩）。`zTXt` 是压缩的，
ComfyUI 不写它；真遇到了会静默跳过 —— 宁可少一个字段，也不要为了它把 zlib
和一段猜出来的格式拽进来。

## 从磁盘读时**不整份读**（2026-10-06）

`png_text(path)` 原来是一句 `f.read()`：画廊每扫一遍都要对每个 PNG 调一次它，
而现场一张放大产物是 **18.5 MB**（4096²）—— 扫一页 18 张就是 65 MB 的读盘与
内存，只为了里面那 1~20 KB 的元数据。现在按 PNG 的块结构走：块头与文本块真的
读，`IDAT` 用 `seek` 跳过去（**不依赖**「tEXt 在 IDAT 之前」这个顺序，一路走到
`IEND` 为止，文本块在前在后都读得到 —— 别的工具写的 PNG 顺序不一定一样）。
"""
import json
import os
import struct

# 单个文本块的字节上限。实测 ComfyUI 那份 prompt 图是 1~20 KB，给到 4 MB 已经很宽；
# 再大就是坏文件或者恶意文件 —— 那种一律当「没有元数据」（**不是**把内存拖下水）。
MAX_TEXT_CHUNK = 4 * 1024 * 1024

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _absorb(ctype, body, out):
    """把一个文本块解进 `out`。**两个入口共用这一份解析**（字节那路与文件那路）。"""
    if ctype == b"tEXt":
        key, _, value = body.partition(b"\x00")
        out[key.decode("latin-1")] = value.decode("latin-1")
    elif ctype == b"iTXt":
        # keyword\0 compflag(1) compmethod(1) langtag\0 translated\0 text
        parts = body.split(b"\x00", 2)
        if len(parts) == 3:
            key = parts[0].decode("latin-1")
            rest = parts[2]
            if len(rest) >= 2 and rest[0] == 0:      # compflag 0 = 未压缩
                tail = rest[2:]
                if b"\x00" in tail:
                    _, _, text = tail.partition(b"\x00")
                else:
                    text = tail
                out[key] = text.decode("utf-8", "replace")


def png_text_from_bytes(blob):
    """`{键: 值}`。非 PNG 或没有文本块时返回空表。"""
    out = {}
    if not blob or blob[:8] != _SIGNATURE:
        return out
    pos = 8
    size = len(blob)
    while pos + 8 <= size:
        try:
            length, ctype = struct.unpack(">I4s", blob[pos:pos + 8])
        except struct.error:
            break
        body = blob[pos + 8:pos + 8 + length]
        pos += 12 + length                       # 长度 + 类型 + 数据 + CRC
        if ctype in (b"tEXt", b"iTXt"):
            _absorb(ctype, body, out)
        elif ctype == b"IEND":
            break
    return out


def png_text(path):
    """从文件里读文本块 —— **只真的读块头与文本块**，图片数据用 seek 跳过。"""
    out = {}
    try:
        f = open(path, "rb")
    except OSError:
        return out
    with f:
        if f.read(8) != _SIGNATURE:
            return out
        while True:
            head = f.read(8)
            if len(head) < 8:
                break                            # 截断的文件：能读到多少算多少
            length, ctype = struct.unpack(">I4s", head)
            if ctype == b"IEND":
                break
            if ctype in (b"tEXt", b"iTXt") and length <= MAX_TEXT_CHUNK:
                body = f.read(length)
                if len(body) < length:
                    break
                _absorb(ctype, body, out)
                f.seek(4, os.SEEK_CUR)           # 块的 CRC
                continue
            f.seek(length + 4, os.SEEK_CUR)      # 数据 + CRC 一起跳过
    return out


def prompt_workflow(blob_or_path):
    """从 PNG 里取回提交时那份 API 图。取不到返回 None。"""
    text = (png_text(blob_or_path) if isinstance(blob_or_path, str)
            else png_text_from_bytes(blob_or_path))
    raw = text.get("prompt")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def png_size(blob_or_path):
    """PNG 的 (宽, 高)，**只读 IHDR**（前 24 字节），不解像素。

    用途是给放大模式算输出尺寸：放大图里只记「源图文件名 × scale」，
    要知道输出多大就必须去问那张源图。IHDR 是 PNG 的第一个 chunk，
    所以读 24 字节就够 —— 不必为了两个整数把整幅图拉进内存。

    取不到返回 None（不是 (0,0)）：调用方要能区分「读不到」和「读到了但为零」。
    """
    if isinstance(blob_or_path, str):
        try:
            with open(blob_or_path, "rb") as f:
                blob = f.read(24)
        except OSError:
            return None
    else:
        blob = blob_or_path or b""
    if len(blob) < 24 or blob[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    length, ctype = struct.unpack(">I4s", blob[8:16])
    if ctype != b"IHDR" or length < 8:
        return None
    width, height = struct.unpack(">II", blob[16:24])
    if width <= 0 or height <= 0:
        return None
    return width, height


# 从图里反查参数用的节点键 —— 与 qwen21_build 里的常量同源，
# 但这里不 import 它：画廊要能读**任何**一份历史 PNG，包括早于构建器的。
_KSAMPLER = "459:458"
_TEXT_ENCODE = "459:452"
_LATENT = "459:456"
_SWITCH = "459:474"
_LOAD_IMAGE = "900"
_UNET = "459:451"
_LAZY_CACHE = "990"       # 只有开了缓存加速的图才有这个节点
_TE_SPEED = "9900"        # 只有开了 TE-Speed 的图才有这个节点
# 只有挂了 Turbo LoRA 的图才有这个节点。**按 id + 类名一起认**：与上面两个不同，
# `LoraLoaderModelOnly` 是个**通用类**（任何 LoRA 都是它），光看类名会把「哪天
# 用户自己挂了别的 LoRA」也算成 turbo；光看 id 又不说明它是什么。两个一起判才
# 落在「这是我们构建器插的那个 turbo」上。
_TURBO_LORA = "9800"
_TURBO_CLASS = "LoraLoaderModelOnly"

# 放大（VOSR2）—— **另一个模型**的图。按 `class_type` 认，不按节点 id：
# 那张图的 id（1..5）是我们自己铸的，换个版本就可能变，而类名是插件定的。
_VOSR2_IMAGE = "TESpeedVOSR2Image"
_VOSR2_LOADER = "TESpeedVOSR2Loader"

# 本工作区这条 Qwen-Image-2.1 链路的指纹。
# 用途是分辨「我们自己出的图」和 output/ 目录里别人的图 —— 实测那儿躺着
# 5 张 MINIMAX_*.png（另一条链路、Desktop 启动时产出的）。硬把它们当 Qwen
# 图展示会刷出一堆 None，得有个标记。
_QWEN_MARKER = _TEXT_ENCODE


def node_by_class(workflow, class_type):
    """按类名找一个节点，返回 `(nid, node)`；没有就 `(None, None)`。"""
    if not isinstance(workflow, dict):
        return None, None
    for nid, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") == class_type:
            return nid, node
    return None, None


def _edit_references(workflow, te):
    """图生图挂着的参考图，**按 `images.image_N` 的编号**排序。

    为什么从连线反查、而不是数 `LoadImage` 节点：连线才代表「真的喂进了文本
    编码器」。图里多挂一个没接上的 LoadImage（手搭的图会有）不该被算成参考图。

    为什么按类名 + 编号认、而不是 import `qwen21_build`：画廊要读**任何**一份
    历史 PNG，包括构建器之前那些手搭的 —— 这正是这个模块顶部那条纪律。
    """
    out = []
    pairs = []
    for key, value in (te or {}).items():
        if not key.startswith("images.image_"):
            continue
        try:
            index = int(key.rsplit("_", 1)[-1])
        except (TypeError, ValueError):
            continue
        nid = value[0] if isinstance(value, (list, tuple)) and value else None
        pairs.append((index, nid))
    for _, nid in sorted(pairs):
        node = workflow.get(str(nid)) if nid is not None else None
        name = ((node or {}).get("inputs") or {}).get("image")
        if name:
            out.append(name)
    if not out:
        # 兜底：没有 autogrow 键的老图，按「有哪些 LoadImage」取（id 排序）
        for nid in sorted((k for k, v in workflow.items()
                           if (v or {}).get("class_type") == "LoadImage"),
                          key=lambda k: (len(str(k)), str(k))):
            name = (workflow[nid].get("inputs") or {}).get("image")
            if name:
                out.append(name)
    return out


def _upscale_summary(workflow, image_node):
    """VOSR2 放大图的那几个字段。

    `width`/`height` **如实报 None**：输出尺寸 = 源图尺寸 × scale，而源图尺寸
    不在图里（图只记文件名）。这里不猜、也不拿 1024 冒充 —— 要知道真实输出
    尺寸得去读那张源图的 IHDR（`png_size()`），那是调用方的事。
    """
    img = image_node.get("inputs") if isinstance(image_node, dict) else None
    img = img if isinstance(img, dict) else {}
    _, load = node_by_class(workflow, "LoadImage")
    src = ((load or {}).get("inputs") or {}).get("image")
    return {
        "mode": "upscale",
        # 本链路自己出的图 —— 只是模型不同，不是「别人的图」
        "recognized": True,
        # 与普通图保持**同一个键集**（两条链的字段形状别分家）：放大没有文本编码器，
        # 也就没有改写器。
        "rewrite": False,
        "unet": None,
        "is_2k": False,
        "prompt": "",           # 放大没有提示词
        "negative_prompt": "",
        "steps": None,          # one-step 模型
        "seed": img.get("seed"),
        "cfg": None,
        "sampler": None,
        "scheduler": None,
        "width": None,
        "height": None,
        "source_image": src,
        "source_images": [src] if src else [],
        "scale": img.get("scale"),
        "lazy_cache": False,
        "te_speed": False,
        "turbo": False,
        "nodes": len(workflow),
    }


def _as_text(value):
    """那个位置**未必是文本** —— 老图里它可能是一条 link（`["459:472", 0]`）。

    断边年代（见 AGENTS.md「断边」）的产物就是这样：提示词**根本没进文本编码器**，
    于是 switch 的 `on_false` 与 `TextEncode` 的 `prompt` 都只是指向子图输入节点的一条
    边。本机 `Qwen_image_2.1_00001/00002` 两张正是这个形状。

    `prompt` / `negative_prompt` 的契约是**字符串**：搜索（`needle in prompt.lower()`）、
    格子、放大视图全按字符串用它。放过一个 list，后果不是「少显示一句话」，而是
    **在画廊搜索框里输入任何字都 500** —— 而且只在扫到那一张时才发生（换台机器就是
    另一个日子，换一批图就是另一张）。

    收成 `""` 是**如实**的：那两张图身上根本没有「生效过的提示词」。
    """
    return value if isinstance(value, str) else ""


def summarize(workflow):
    """把一份 API 图压成画廊需要的那几个字段。

    对**不是本链路**的图也要能返回（字段为 None），不抛异常 ——
    画廊扫的是磁盘，里面什么都可能有。
    """
    if not isinstance(workflow, dict):
        return {}
    # 放大图**先分流**：它没有 KSampler，掉进下面那套 Qwen 逻辑会被判成
    # 「不认识」（mode=None），而那正是它不该得到的待遇。
    _, up_node = node_by_class(workflow, _VOSR2_IMAGE)
    if up_node is not None:
        return _upscale_summary(workflow, up_node)
    ks = (workflow.get(_KSAMPLER) or {}).get("inputs") or {}
    te = (workflow.get(_TEXT_ENCODE) or {}).get("inputs") or {}
    lat = (workflow.get(_LATENT) or {}).get("inputs") or {}
    sw = (workflow.get(_SWITCH) or {}).get("inputs") or {}
    unet = (workflow.get(_UNET) or {}).get("inputs") or {}
    if not isinstance(ks, dict):
        ks = {}
    editing = _LOAD_IMAGE in workflow

    width = lat.get("width") if isinstance(lat.get("width"), int) else None
    height = lat.get("height") if isinstance(lat.get("height"), int) else None

    if editing:
        mode = "edit"
        prompt = _as_text(te.get("prompt"))
        sources = _edit_references(workflow, te)
        width = height = None
    elif not ks:
        # KSampler 都没有 —— 多半不是这个模板。如实说「不认识」，
        # 别硬套成 t2i 让画廊显示一条假的记录。
        mode = None
        prompt = _as_text(te.get("prompt"))
        sources = []
    else:
        # 改写是**额外开关**（2026-10-01 起），不再是模式：`mode` 只表达形状。
        # `switch=True` 的老图照样读得出来 —— 它现在就落在 `rewrite` 这个键上，
        # 所以画廊里那些历史 refine 图一张都不会丢。
        mode = "t2i"
        prompt = _as_text(sw.get("on_false")) or _as_text(te.get("prompt"))
        sources = []

    return {
        "mode": mode,
        "rewrite": bool(sw.get("switch")),
        "recognized": _QWEN_MARKER in workflow,
        "unet": unet.get("unet_name"),
        "is_2k": bool(width and width >= 2048) or bool(height and height >= 2048),
        "prompt": prompt,
        "negative_prompt": _as_text(te.get("negative_prompt")),
        "steps": ks.get("steps"),
        "seed": ks.get("seed"),
        "cfg": ks.get("cfg"),
        "sampler": ks.get("sampler_name"),
        "scheduler": ks.get("scheduler"),
        "width": width,
        "height": height,
        # 单数就是复数里的第一张（**第一张决定输出尺寸**，见 nodes_qwen.py）。
        # 两个键都给，是因为用它们的地方本来就不同：画廊卡片用单数，
        # 多张参考图那份列表用复数。
        "source_image": (sources[0] if sources else None),
        "source_images": sources,
        # 与 `_upscale_summary` 保持**同一个键集** —— 前端拿到「键缺失」和
        # 拿到 null 是两种待遇，别让两种图分家。
        "scale": None,
        "lazy_cache": _LAZY_CACHE in workflow,
        "te_speed": _TE_SPEED in workflow,
        "turbo": _has_turbo(workflow),
        "nodes": len(workflow),
    }


def _has_turbo(workflow):
    """这张图挂的是不是**我们那个** Turbo LoRA。

    两个条件一起判（见 `_TURBO_LORA` 上面的注释）：节点 id 是我们铸的那一个，
    **并且**类名是 `LoraLoaderModelOnly` —— 只看 id 不说明它是什么，只看类名
    会把任何别的 LoRA 都算进来。
    """
    node = workflow.get(_TURBO_LORA)
    return isinstance(node, dict) and node.get("class_type") == _TURBO_CLASS
