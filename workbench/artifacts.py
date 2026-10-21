"""两条新链路的产物：按**作业自己的 token** 从磁盘上认回来。

## 为什么不能靠 `/history`

① **See-through 那次跑，`/history` 里一个产物名都没有。**
   `SeeThrough_SavePSD.save()` 返回的是一个**裸字符串**（`RETURN_TYPES = ("STRING",)`），
   不是 `{"ui": {...}}` 那种形状 ⇒ `/history/<id>.outputs` 里那一格是空的。二十几个
   层文件与那份 `_layers.json`，一个名字都不在里面。靠 history 的话，一次成功的
   拆层会显示成「完成、但没有产物」。

② **`seethrough_psd_info.log` 是「最后写入者胜」的指针。**
   那是插件自己那颗「Download PSD」按钮用的（它只关心最近一次）。拿它当历史回看，
   看到的永远是**最近一次**的层，而不是你点开那个作业的层 —— 张冠李戴，而且
   两个作业长得一模一样，看不出来。

③ **H3 的视频落在 `output/video/` 子目录**，走的是另一套取图口。

## 那靠什么

**文件名里那段 token。** `qwen21_build.unique_save_prefix()` 在提交前把
`job_id[:8]` 插进收尾节点的 `filename_prefix`，于是同一批产物共享那一段：

    MiniMax_H3_a1b2c3d4_00001.mp4
    seethrough_a1b2c3d4_20261012_101530_ab12cd34_tail.png
    seethrough_a1b2c3d4_20261012_101530_ab12cd34_layers.json

而它在作业与产物之间是唯一的键（八位十六进制前缀，来自 `uuid4`）。这套办法在
PNG 那条路上已经验证过了（收尾前缀那一套），只是那时只用它解决缓存命中。

## 全是纯函数

只读磁盘，不写、不缓存、不认「作业」这个概念 —— 拿 `(roots, token)` 就能测。
"""
import json
import os
import sys

from . import config

# `h3_build` / `layers_build` 在仓库根目录（与 `qwen21_build` 同处），
# `server.py` 也是这么找它们的。这里自己补一次：这个模块可能先于 server 被 import。
if config.REPO not in sys.path:
    sys.path.insert(0, config.REPO)
import h3_build as h3b            # noqa: E402
import layers_build as lb         # noqa: E402

#: 视频产物认这几个后缀。`SaveVideo` 的 `format: auto` 给的是 mp4；
#: 另几个留着是因为换一台机器上换了编码器就会换后缀，而那时**没有症状** ——
#: 页面只会说「取不到产物」。
VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov")

#: 视频下探几层子目录。**一层的理由是它的落点**：`SaveVideo` 的前缀是
#: `video/MiniMax_H3`，所以文件在 `output/video/` 里。再多就是乱猜了。
VIDEO_DEPTH = 1

#: 层文件下探几层 —— **零**。`SeeThrough_SavePSD.save()` 是
#: `Image.save(os.path.join(output_dir, name))`，层就在输出根目录的顶层。
#: 下探反而会从别的目录里认出同名文件。
LAYERS_DEPTH = 0


def token_of(job_id):
    """作业 id → 收尾前缀里那一段。

    **必须与 `server._submit_one()` 里那个 `job_id[:8]` 同源。** 两处不一致的症状是
    「作业永远找不到自己的产物」，而页面只会说「取不到」。
    """
    return str(job_id or "")[:8]


def _walk(root_path, rel="", depth=0):
    """在自己这一层按名字排序列文件；`depth > 0` 时再下探子目录。

    排序是为了让结果**确定**（同一份磁盘状态给出同一份读数），
    不是为了让谁排在前面 —— 挑选由调用方按 mtime 做。
    """
    full = os.path.join(root_path, rel) if rel else root_path
    try:
        names = sorted(os.listdir(full))
    except OSError:
        return
    for name in names:
        path = os.path.join(full, name)
        try:
            is_dir = os.path.isdir(path)
        except OSError:
            continue
        if is_dir:
            if depth > 0:
                child = "%s/%s" % (rel, name) if rel else name
                for item in _walk(root_path, child, depth - 1):
                    yield item
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        yield root_path, rel, name, path, st


def _candidates(roots, depth):
    """遍历候选输出根，带上**根的下标**。

    下标是给前端用的：层的缩略图走 `/api/gallery/thumb?root=<下标>&name=<文件名>`
    —— 那条路只认「第几个候选根 + 文件名」。少了它，前端只能要么拉原图
    （24 张 1024² 的 RGBA，上百 MB），要么自己去猜那个下标 ——
    而 `candidate_roots()` 的顺序是**服务端**算的，猜它就是造第二个真源。
    """
    for index, root in enumerate(roots or ()):
        base = (root or {}).get("path")
        if not base:
            continue
        for item in _walk(base, "", depth):
            yield index, item


def _subfolder(rel):
    """ComfyUI 的 `subfolder` 一律用正斜杠（Windows 上 `os.sep` 是反斜杠）。"""
    return (rel or "").replace("\\", "/").strip("/")


def find_video(roots, token, depth=VIDEO_DEPTH):
    """找出这个作业的**那一个**视频。找不到返回 `None`。

    多个命中时取 **mtime 最新**的那个：同一份参数再提一次会写出 `_00002.mp4`，
    而用户要看的是刚跑的那一个。
    """
    token = str(token or "")
    if not token:
        return None
    best = None
    for index, (root_path, rel, name, path, st) in _candidates(roots, depth):
        if not name.lower().endswith(VIDEO_EXT):
            continue
        if not lb.token_in_prefix(name.rsplit(".", 1)[0], token):
            continue
        if best is None or st.st_mtime > best["mtime"]:
            best = {"kind": "video",
                    # 候选根的下标 —— 取图口按「第几个根 + 文件名」寻址
                    "root": index,
                    "filename": name,
                    "subfolder": _subfolder(rel),
                    "bytes": st.st_size,
                    "mtime": st.st_mtime}
    return best


def find_layer_set(roots, token, depth=LAYERS_DEPTH):
    """找出这个作业的那一份层清单，并把层**逐项核在磁盘上**。

    返回 `None` 表示这一批没找到；找到了但清单读不出来、或有的层不在磁盘上，
    都**如实报出来**（`error` / `missing`），不装作一切正常 —— 层集会因为一次
    归档、一次手工搬走而少几张，而「少了几张」与「全都还在」在页面上长得一样。
    """
    token = str(token or "")
    if not token:
        return None
    found = None
    for index, (root_path, rel, name, path, st) in _candidates(roots, depth):
        match = lb.LAYERS_JSON_RE.match(name)
        if not match or not lb.token_in_prefix(match.group("prefix"), token):
            continue
        if found is None or st.st_mtime > found[0]:
            found = (st.st_mtime, index, root_path, rel, name, path)
    if found is None:
        return None

    _, index, root_path, rel, name, path = found
    out = {"kind": "layers",
           # 候选根的下标 —— 层缩略图按「第几个根 + 文件名」寻址（见 _candidates）
           "root": index,
           "info": name, "subfolder": _subfolder(rel),
           "layers": [], "count": 0, "missing": [], "error": None}
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError) as exc:
        out["error"] = "层清单读不出来：%s" % exc
        return out
    if not isinstance(info, dict):
        out["error"] = "层清单不是对象"
        return out

    out["width"] = info.get("width")
    out["height"] = info.get("height")
    out["prefix"] = info.get("prefix")
    raw = info.get("layers")
    if not isinstance(raw, list):
        out["error"] = "层清单里没有 layers 数组"
        return out

    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("filename"):
            continue
        layer = {"name": entry.get("name"),
                 # 层与清单在同一个目录里，所以子目录沿用清单那一层
                 "subfolder": out["subfolder"],
                 "filename": entry["filename"],
                 "left": entry.get("left"), "top": entry.get("top"),
                 "right": entry.get("right"), "bottom": entry.get("bottom"),
                 "depth_median": entry.get("depth_median")}
        for key in ("filename", "depth_filename"):
            if key not in entry:
                continue
            target = entry.get(key)
            if not target:
                continue
            sub = out["subfolder"]
            probe = os.path.join(root_path, sub.replace("/", os.sep), target) \
                if sub else os.path.join(root_path, target)
            if not os.path.isfile(probe):
                out["missing"].append(target)
            if key == "depth_filename":
                layer["depth_filename"] = target
        out["layers"].append(layer)
    out["count"] = len(out["layers"])
    return out


def resolve(mode, roots, token):
    """按模式分流。别的模式返回 `None`（它们的产物在 `/history` 里，画廊也认）。"""
    if mode == h3b.MODE:
        return find_video(roots, token)
    if mode == lb.MODE:
        return find_layer_set(roots, token)
    return None
