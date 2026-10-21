"""画廊的缩略图 —— 格子上那张 180px 的小图，不该是 18 MB 的原图。

## 为什么必须做（2026-10-06 实测）

现场那二十来张图里有 4096² / 2896² / 2752² 的放大产物，**一页 18 格的原图合计
65.4 MB**（最大一张 18.5 MB）。浏览器把每一张都整份下下来、整份解码成位图 ——
4096² 一张就是 64 MB 的解码内存，18 格是一 GB 量级。用户报的「其中有分辨率
较高的作品时页面会卡顿」就是这个。

同一页缩到长边 512 的 JPEG（q82）之后是 **0.71 MB（92×）**，每格解码内存从
64 MB 掉到 1 MB 上下。而格子在 CSS 里是 `width:100%` + `aspect-ratio:1` 的
正方形（`object-fit:cover`），180~260px 宽、2 倍屏也只有 512px 可用 ——
再多给的像素一个都画不出来。

## 三条边界

1. **它只是「画」出来的那一份。** 任何**用**这张图的地方（放大视图、1:1 原始像素、
   拖去做参考图）都必须走 `/api/gallery/image` —— 拿缩略图当原图是**没有症状**的
   降级：传得上去、出得来图，只是少了一半像素，而页面上完全看不出来。
   页面那一侧由 `refFromUri()` 把缩略图地址**改写回原图地址**兜住。
2. **Pillow 不在 = 这条路整个消失**，取图退回原图（与 `vision.py` 同一条纪律：
   少一个包只许这一条路报错，不许把整个工作台拖下水）。页面上看不出区别，只是
   仍旧慢 —— 所以启动时会在日志里说一句。
3. **缓存键认「路径 + mtime_ns + 字节数」**，不认「序号」。ComfyUI 的序号在后端
   重启后从 1 重来、同名文件会被覆盖，而覆盖一定改 mtime —— 少了它，新图会画着
   上一张的脸，且没有症状（见 `gallery.py` 顶部那条「文件名不是可靠键」）。
"""
import hashlib
import io
import logging
import os
import tempfile
import time

try:
    # 与 `vision.py` 同一个写法：缺包只让**这一条路**报错。
    from PIL import Image
except Exception as _exc:                       # noqa: BLE001 - ImportError 之外的坑也吃
    Image = None
    _PIL_ERROR = "%s: %s" % (type(_exc).__name__, _exc)
else:
    _PIL_ERROR = ""

logger = logging.getLogger("workbench.thumbs")

# 长边像素。格子最宽约 260px，2 倍屏 520 —— 512 是「够用 + 不浪费」那一档。
SIDE = 512
QUALITY = 82
SUFFIX = ".jpg"

# 缓存预算。两个都要有：只按个数算，一堆 4096² 的缩略图也能攒到 GB 级；
# 只按字节算，小图多的时候会攒下几万个目录项，而每次落盘都要 `listdir` 一遍。
MAX_FILES = 2000
MAX_BYTES = 256 * 1024 * 1024

# 收尾的间隔：**不是**每个请求都扫一遍目录（18 个格子就是 18 次 listdir）。
PRUNE_INTERVAL = 60.0

# 上一次收尾的时刻（进程级）。测试可以直接改它，也可以给 `maybe_prune` 传 interval。
_last_prune = 0.0


def available():
    """Pillow 在不在。不在 = 缩略图这条路不存在，取图一律发原图。"""
    return Image is not None


def why_unavailable():
    """Pillow 缺席的原因（装得上时是空串）。给人看的那一句。"""
    return _PIL_ERROR


def cache_dir(state_dir):
    """缩略图落在**状态目录**下面（`state/` 整个是 gitignore 的）。"""
    return os.path.join(state_dir, "thumbs")


def key_for(src, st=None, side=SIDE, quality=QUALITY):
    """缓存文件名 = 路径 + mtime_ns + 字节数 + 目标尺寸 + 质量。

    `side` / `quality` 也进键：改了这两个常量就等于**整批失效**，不需要谁去手工
    清缓存 —— 那种「改了参数但页面还是旧图」的坑没有症状。
    """
    if st is None:
        st = os.stat(src)
    digest = hashlib.sha1(
        os.path.normcase(os.path.abspath(src)).encode("utf-8")).hexdigest()
    return "%s-%d-%d-%d-q%d%s" % (digest[:16], st.st_mtime_ns, st.st_size,
                                  side, quality, SUFFIX)


def cached(src, cache_root, **kw):
    """缓存里已经有了就返回它的路径，否则 None（**不建**）。"""
    try:
        dest = os.path.join(cache_root, key_for(src, **kw))
    except OSError:
        return None
    return dest if os.path.isfile(dest) else None


def render(src, side=SIDE, quality=QUALITY):
    """把源图缩成长边 `side` 的 JPEG 字节。解不开就抛 —— 由调用方决定怎么办。"""
    if Image is None:
        raise RuntimeError("没装 Pillow（%s）" % _PIL_ERROR)
    with Image.open(src) as im:
        im = _flatten(im)
        im.thumbnail((side, side), Image.LANCZOS, reducing_gap=2.0)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _flatten(im):
    """透明压白底 —— 与 `vision.prepare` 同一条规矩。

    JPEG 没有 alpha 通道，直接 `convert("RGB")` 会把透明处**变黑**，而那种黑
    在格子上看起来像是作者画出来的。本链路目前出的是 RGB，这一条是给将来
    （透明背景那条模板）留的：颜色错了不会报错，只会一直错。
    """
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        return flat
    return im.convert("RGB")


def ensure(src, cache_root, side=SIDE, quality=QUALITY):
    """拿一份**可以发出去**的缩略图。

    返回 `(路径, 说明)`。`路径 is None` 就表示「这一张缩不出来，请发原图」——
    说明里是原因（`no-pillow` / `hit` / `built` / 具体异常）。调用方**不许**把
    None 当成错误：缩略图是装饰，它失败不该让用户看不到图。
    """
    if Image is None:
        return None, "no-pillow"
    try:
        st = os.stat(src)
        dest = os.path.join(cache_root, key_for(src, st, side, quality))
    except OSError as exc:
        return None, "stat: %s" % exc
    if os.path.isfile(dest):
        return dest, "hit"
    try:
        os.makedirs(cache_root, exist_ok=True)
    except OSError as exc:
        logger.warning("缩略图目录建不出来：%s（%s）", cache_root, exc)
        return None, "缓存目录建不出来：%s" % exc
    try:
        blob = render(src, side, quality)
    except Exception as exc:                    # noqa: BLE001 - Pillow 的花样很多
        # 解不开（半张 PNG、非图片、格式不认识）就是「发原图」——浏览器那边至少
        # 还能试一次，而且失败的样子与今天一模一样，不会多出一种坏法。
        logger.info("缩略图做不出来，这一张发原图：%s（%s: %s）",
                    src, type(exc).__name__, exc)
        return None, "%s: %s" % (type(exc).__name__, exc)
    # **原子落盘**：两个请求同时要同一张时会各写一份临时文件，最后一个 replace 胜出。
    # 少了这一步，另一个请求可能读到**半个 JPEG** —— 页面上是随机一张破图，
    # 刷新一下又好了，属于最难查的那种。
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=cache_root, prefix=".tmp-", suffix=SUFFIX)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, dest)
        tmp = None
    except Exception as exc:                    # noqa: BLE001
        logger.warning("缩略图写不进缓存：%s（%s）", src, exc)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return None, "写不进缓存：%s" % exc
    maybe_prune(cache_root, keep=dest)
    return dest, "built"


def usage(cache_root):
    """`{files, bytes}` —— 给证据和探针看，不在请求路径上。"""
    files = 0
    total = 0
    try:
        names = os.listdir(cache_root)
    except OSError:
        return {"files": 0, "bytes": 0}
    for name in names:
        try:
            st = os.stat(os.path.join(cache_root, name))
        except OSError:
            continue
        files += 1
        total += st.st_size
    return {"files": files, "bytes": total}


def maybe_prune(cache_root, keep=None, interval=PRUNE_INTERVAL, now=None, **kw):
    """按**时间间隔**收一次缓存，返回删掉的个数。

    「每写一张就扫一遍目录」在画廊这种场景里是自伤：一页 18 张就是 18 次
    `listdir`（缓存大了以后每次都几十毫秒）。所以收尾挂在一个计时器上。

    预算那两个参数原样转给 `prune()`（判据要给小预算，不然得先造 2000 份缓存）。
    """
    global _last_prune
    now = time.monotonic() if now is None else now
    if interval and now - _last_prune < interval:
        return 0
    _last_prune = now
    return prune(cache_root, keep=keep, **kw)


def prune(cache_root, max_files=MAX_FILES, max_bytes=MAX_BYTES, keep=None):
    """把缓存收进预算内，返回删掉的个数。**只动 `thumbs/` 里的文件**。

    `keep`（刚写出来那一份）绝不删 —— 否则「刚建好就被自己的收尾删掉」，
    下一次请求又重建一遍，看起来就是「缓存从来不命中」。
    """
    try:
        names = os.listdir(cache_root)
    except OSError:
        return 0
    rows = []
    total = 0
    for name in names:
        if name.startswith(".tmp"):             # 在飞的临时文件，别碰
            continue
        full = os.path.join(cache_root, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full):
            continue
        rows.append((st.st_mtime_ns, st.st_size, full))
        total += st.st_size
    if len(rows) <= max_files and total <= max_bytes:
        return 0
    rows.sort()                                 # 最旧的在最前面
    kept = os.path.abspath(keep) if keep else None
    removed = 0
    for _, size, full in rows:
        if len(rows) - removed <= max_files and total <= max_bytes:
            break
        if kept and os.path.abspath(full) == kept:
            continue
        try:
            os.unlink(full)
        except OSError:
            continue
        total -= size
        removed += 1
    if removed:
        logger.info("缩略图缓存超预算，收掉 %d 份（现在 %d 份 / %.1f MB）",
                    removed, len(rows) - removed, total / 1048576.0)
    return removed
