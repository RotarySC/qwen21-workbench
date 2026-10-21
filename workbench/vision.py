# -*- coding: utf-8 -*-
"""把一张参考图收成 **给 LLM 看** 的那一份。

为什么要有这一层（不是「顺手转一下格式」）：

**参考图在本机是几 MB 的 PNG。**实测用户 `input/` 里那批：1.6 MB ~ 8.4 MB
（2048² 的成品 5.8 MB、2K 时装图 8.4 MB）。原样发出去要 base64 再涨三分之一，
一张就是 **2~11 MB** —— 而这是个**远端**端点，发的是上行带宽，
付的是图片 token。模型要的是「看清是什么」，不是那些像素。

所以这里只做三件事，而且**只有这三件**：

    exif 摆正 → 长边收到 `MAX_SIDE` → 存成 JPEG（有透明就压在白底上）

三条纪律：

1. **失败就抛，绝不后退成纯文本。** 这个模块的每一个 `raise` 都在挡同一件事：
   让「图没上车」的那一次请求**发出去**。实测（见 `logs/llm_vision_probe.log`）
   问一个**没有图**的模型「这张图里有什么」，它会**理直气壮地编一张**出来
   （两次跑编得还不一样），而那段编出来的描述会被「采用」写进提示词、照它出图 ——
   出图能出、耗时正常、日志干净。**没有症状**，正是本工作区最忌讳的那一类。
2. **JPEG + 白底是刻意的。**透明背景压到黑底会让模型看见一张比实际更暗的图；
   而 JPEG 是唯一一个「所有端点都收」的格式（webp 不一定）。
3. **Pillow 缺席不许把整个工作台拖下水。**所以 import 在这里是 `try/except` 的：
   装了就用，没装只有**这一条路**报错（`ImageError(kind="missing")` 说清怎么装），
   其它功能照常。`server.py` import 本模块，顶格 `import PIL` 会让少一个包等于
   整个页面起不来。
"""
import base64
import io

try:                                              # pragma: no cover - 装机就该有
    from PIL import Image, ImageOps
    _RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS
    _PIL_ERROR = None
except Exception as exc:                          # noqa: BLE001
    Image = ImageOps = None
    _RESAMPLE = None
    _PIL_ERROR = "%s: %s" % (type(exc).__name__, exc)

# 长边收到多少。1024 是**故意贴着**本链路自己的出图边长选的：图生图的参考图
# 本来就是 1024²，收到这个数等于「常见情况不缩」；而 2048² 的成品对半砍。
MAX_SIDE = 1024
# q88：再低能看出块状，再高的字节数涨得比画质快。实测 2048² 的 5.8 MB PNG
# 到这里约 200 KB（约 1/29）。
QUALITY = 88
# 解压前的字节上限，与上传那条路同一个数（`upload.MAX_BYTES`）。
MAX_SRC_BYTES = 32 * 1024 * 1024


class ImageError(ValueError):
    """带 `kind` 的失败。`kind` 给判据用，文案给人看。"""

    def __init__(self, message, kind="image"):
        super().__init__(message)
        self.kind = kind


def prepare(name, data):
    """返回 `{name, mime, b64, bytes, width, height, src_width, src_height, src_bytes}`。

    `b64` 是**已经编好的** base64 —— 拼 data URI 是 `llm.build_messages` 的事，
    这里不碰消息形状（两边各管一件事）。
    """
    if Image is None:
        raise ImageError("没装 Pillow，画不出给模型看的那一份（%s）。"
                         "装上它就好：`pip install -r requirements-lock.txt`"
                         % _PIL_ERROR, kind="missing")
    data = bytes(data or b"")
    if not data:
        raise ImageError("『%s』是空的（0 字节）" % name, kind="empty")
    if len(data) > MAX_SRC_BYTES:
        raise ImageError("『%s』太大：%.1f MB（上限 %d MB）"
                         % (name, len(data) / 1048576.0,
                            MAX_SRC_BYTES // 1048576), kind="too_big")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            # 手机拍的照片带 EXIF 方向：不摆正，模型看到的是一张躺着的图
            # （而它会照着躺着的描述写提示词 —— 又是一个没有症状的错）。
            im = ImageOps.exif_transpose(im)
            src_w, src_h = im.size
            if im.mode in ("RGBA", "LA") or (im.mode == "P"
                                             and "transparency" in im.info):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, (255, 255, 255))
                flat.paste(im, mask=im.split()[-1])
                im = flat
            else:
                im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE), _RESAMPLE)
            out = im.size
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=QUALITY, optimize=True)
        blob = buf.getvalue()
    except Exception as exc:                      # noqa: BLE001
        raise ImageError("『%s』解不开（%s: %s）"
                         % (name, type(exc).__name__, exc), kind="decode") from exc
    return {
        "name": name,
        "mime": "image/jpeg",
        "b64": base64.b64encode(blob).decode("ascii"),
        "bytes": len(blob),
        "width": out[0], "height": out[1],
        "src_width": src_w, "src_height": src_h,
        "src_bytes": len(data),
    }
