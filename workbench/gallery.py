"""画廊 —— 扫磁盘上的 PNG，不建数据库。

## 为什么不按 `prompt_id` 关联（spec §6.5 在这里是错的）

spec 原稿说「与 `runs.jsonl` 按 `prompt_id` 关联出耗时」。**实测这条路不通**：
ComfyUI 写进 PNG 的 tEXt 只有 `prompt` 一个键，**没有 prompt_id**：

    >>> png_text("Qwen_image_2.1_00014.png").keys()
    dict_keys(['prompt'])

（P1 之所以能把 `job_id == prompt_id`，是因为工作台**自己铸的** id 并当
`prompt_id` 提交，不是因为它能读回来。）

所以唯一的连接键是**文件名** —— `runs.jsonl` 的 `outputs[].filename`。
文件名不是可靠键：ComfyUI 的序号（`Qwen_image_2.1_00014`）**每次启动重置**，
跨重启会重名。因此这里做**对账**而不是直接相信：拿 PNG 里那份图反解出的参数
（steps / seed / 尺寸）与运行记录的参数比，一致才认，不一致标 `conflict`
并且**不显示耗时**。宁可少显示，也不要显示错的。

## 画廊不依赖那个连接

模式 / seed / 步数 / 尺寸 / 提示词全在 PNG 自己身上，所以 **runs.jsonl 被删、
后端没起，画廊照样能用**，只是没有耗时。这是 spec §6.5 说的「降级而不是报错」。

## 为什么扫多个目录

实测这台机器上有**两个**输出目录，而且**两个都有图**：

    ComfyUI-Shared\\output                      5 张 MINIMAX_*.png   ← Desktop 启动
    ComfyUI-Installs\\ComfyUI\\ComfyUI\\output  29 张 Qwen_image_*.png ← 手动启动（旧版铁律 1）

`%APPDATA%\\Comfy Desktop\\settings.json` 里的 `outputDir` 指向**前者**，
它只对 Desktop 启动成立 —— 真正的权威是**当前进程的启动参数**。

**现在铁律 1 带 `--output-directory` 指向工作区**（`config.OUTPUT_DIR`），
所以新图落在 `…\\ComfyUI\\output`；而**旧图仍在安装目录里**，不能因为改了落点就看不见了。
只扫一个就会漏掉一半历史，所以这里全部扫、逐项标注来自哪个根。

（`--output-directory` 与工作区那一条指向同一个目录时会被去重 —— `add()` 按路径去重，
先加的是 argv 那条，所以标签会是「启动参数 --output-directory」，位置在列表最前。）
"""
import json
import os
import sys

from . import config, pngmeta
from .util import now_ms

# `layers_build` 在仓库根目录（与 `qwen21_build` 同处）—— 那里有层集的两条命名
# 正则。判据只此一处，画廊的排除规则与 `artifacts.py` 共用它，别在这里另写一份。
if config.REPO not in sys.path:
    sys.path.insert(0, config.REPO)
import layers_build as lb  # noqa: E402

# Comfy Desktop 的两个配置文件。`installations.json` 里有 installPath，
# `settings.json` 里有 outputDir / inputDir —— 注意 outputDir 只对
# **Desktop 启动**成立。
DESKTOP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                           "Comfy Desktop")
DESKTOP_SETTINGS = os.path.join(DESKTOP_DIR, "settings.json")
DESKTOP_INSTALLATIONS = os.path.join(DESKTOP_DIR, "installations.json")

# 文件名里带这些就跳过（ComfyUI 会写 _temp 中间产物）
SKIP_PREFIXES = ("_temp",)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def desktop_settings(path=None):
    return _read_json(path or DESKTOP_SETTINGS) or {}


def desktop_output_dir(path=None):
    """Desktop 启动时的落点。**不是**铁律 1 那条命令的落点。"""
    return desktop_settings(path).get("outputDir")


def desktop_install_root(path=None):
    """`installations.json` 里第一个带 installPath 的本地安装。

    它的 `installPath` 是 `…\\ComfyUI-Installs\\ComfyUI`，真正的 checkout 在
    它的 `ComfyUI` 子目录里（`…\\ComfyUI-Installs\\ComfyUI\\ComfyUI`）。
    """
    rows = _read_json(path or DESKTOP_INSTALLATIONS)
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("installPath"):
            return row["installPath"]
    return None


def candidate_roots(client=None, stats=None):
    """候选输出目录，按可信度排序。**都返回**（含不存在的），让 UI 能解释。

    `stats` 可以传进来复用 —— `/api/backend` 已经取过一次 `/system_stats`，
    没必要为同一件事再打一次。
    """
    roots = []

    def add(path, label, kind):
        if not path:
            return
        norm = os.path.normcase(os.path.normpath(path))
        if any(os.path.normcase(os.path.normpath(r["path"])) == norm for r in roots):
            return
        roots.append({"path": path, "label": label, "kind": kind,
                      "exists": os.path.isdir(path)})

    # 1) 启动参数最权威 —— 它是**当前进程**实际在用的
    if stats is None and client is not None:
        stats = client.system_stats()
    argv = ((stats or {}).get("system") or {}).get("argv") or []
    for i, token in enumerate(argv):
        if token in ("--output-directory", "--output_directory") and i + 1 < len(argv):
            add(argv[i + 1], "启动参数 --output-directory", "argv")
        elif token.startswith("--output-directory="):
            add(token.split("=", 1)[1], "启动参数 --output-directory", "argv")

    install = desktop_install_root()
    if install:
        add(os.path.join(install, "ComfyUI", "output"),
            "手动启动（铁律 1 的落点）", "manual")

    # config.COMFY_ROOT 是从 %LOCALAPPDATA% 拼出来的，作为兜底
    add(os.path.join(config.COMFY_ROOT, "output"),
        "手动启动（推算路径）", "manual-fallback")

    add(desktop_output_dir(), "Desktop 启动", "desktop")
    # 与 config.OUTPUT_DIR 同一个常量 —— 落盘的写者与读图的扫者共用一个真源，
    # 否则改了启动参数却忘了这里，画廊就会对着一张张新图说「一张都没有」。
    add(config.OUTPUT_DIR, "工作区", "workspace")
    return roots


def safe_join(root, name):
    """把名字拼进根目录，并确认结果**仍在根里**。

    画廊按「根序号 + 文件名」取图（见 server 的 `/api/gallery/image`），
    文件名来自客户端，所以必须自己挡路径穿越，不能指望调用方。
    """
    if not name or not root:
        return None
    if name.startswith(("/", "\\")) or ":" in name:
        return None
    parts = name.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    full = os.path.normpath(os.path.join(root, *parts))
    base = os.path.normpath(root)
    if os.path.normcase(full) != os.path.normcase(base) and \
            not os.path.normcase(full).startswith(os.path.normcase(base) + os.sep):
        return None
    return full


# ---------------------------------------------------------------------------
# 「根序号 + 文件名」→ 磁盘上那一条路径（P20f）
#
# 取图（`/api/gallery/image`）与删图（`/api/gallery/delete`）共用这一份判断。
# **不许各写一份**：两边要是分家，就会长出「页面上画得出来、却删不掉」，
# 或者更糟的「删掉的是另一个」——而这两种都没有症状。
# ---------------------------------------------------------------------------
def resolve_output_file(roots, root_index, name):
    """`(路径, 拒绝理由, HTTP 码)`；成功时后两项都是 None。

    `root_index` 由调用方解析成 int（不在这里 `int()`：数字写错该报的是
    「root 必须是整数」，那是请求形状的问题，不是这一层的问题）。
    """
    if not 0 <= root_index < len(roots or []):
        return None, "root 越界", 404
    path = safe_join((roots[root_index].get("path") or ""), name or "")
    if path is None:
        return None, "非法的文件名", 400
    if not os.path.isfile(path):
        return None, "图片不在了", 404
    return path, None, None


def deletable(roots, root_index, name):
    """能不能删这一项 → `(路径, 拒绝理由)`（能删时理由是 None）。

    比取图**多两条**，都是为了收窄「客户端能点名删什么」：

      * 只删 `.png` —— 名字是客户端给的，不挡的话 `input/` 里的 `.txt`、
        后端自己的产物、任何同目录的文件都能被删掉；
      * 不碰 `_temp` 开头的 —— 那是 ComfyUI **正在写**的中间产物，删掉等于
        弄坏一次正在进行的出图（它们本来也不在画廊里，是白名单之外的一类）。
    """
    path, why, _code = resolve_output_file(roots, root_index, name)
    if path is None:
        return None, why
    base = os.path.basename(path)
    if not base.lower().endswith(".png"):
        return None, "%s 不是 PNG，画廊只删图" % base
    if base.startswith(SKIP_PREFIXES):
        return None, "%s 是正在写的中间产物，不删" % base
    return path, None


# ---------------------------------------------------------------------------
# 归档（P20h）：画廊的「删除」不删文件，把它挪到 `config.ARCHIVE_DIR`
#
# 三条形状上的决定：
#
#   * **只收 `config.OUTPUT_DIR` 那一处**。判据必须是**路径**，不是 `kind` ——
#     铁律 1 的 `--output-directory` 指的就是这个目录，`candidate_roots()` 里
#     argv 那一条先加、去重之后**顶着 `kind="argv"` 站在第一个**（实测
#     `/api/gallery` 的 roots[0] 就是它）。按 `kind=="workspace"` 去判会**正好
#     漏掉日常那一支**，而且没有症状。
#   * 归档目录**不进 `candidate_roots()`**：正常画廊因此天然看不见它，一句筛选都
#     不用写。要看它，服务端把 `archive_root()` 单独当一个根传进 `scan()`。
#   * 还原**不改名**：`runs_index()` 是**按文件名**跟任务账对账的，改名等于让那张图
#     跟它的耗时脱钩（P20g 那条「看着对、其实没有」的亲戚）。撞名就整项失败，并说
#     出挡住它的是谁 —— 不覆盖、也不替它取新名字。
# ---------------------------------------------------------------------------
ARCHIVE_LABEL = "已归档"


def archive_root():
    """归档目录，形状与 `candidate_roots()` 的每一项一致 —— 好当根用。"""
    return {"path": config.ARCHIVE_DIR, "label": ARCHIVE_LABEL, "kind": "archive",
            "exists": os.path.isdir(config.ARCHIVE_DIR)}


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a or "")) == \
        os.path.normcase(os.path.normpath(b or ""))


def archivable(roots, root_index, name):
    """能不能把这一项归档 → `(路径, 拒绝理由)`（能时理由是 None）。

    比 `deletable()` 多一条：**它的根必须是 `config.OUTPUT_DIR`**（见上面那段）。
    其余逐字共用 —— 归档与删除要是各判一套，就会长出「画得出来却归不了档」那种
    没有症状的分叉。
    """
    if not 0 <= root_index < len(roots or []):
        return None, "root 越界"
    root = roots[root_index] or {}
    if not _same_path(root.get("path"), config.OUTPUT_DIR):
        return None, ("这张在「%s」那个目录里，不在归档范围（归档只认工作区 output/）"
                      % (root.get("label") or "另一个"))
    return deletable(roots, root_index, name)


def resolve_archived(name):
    """归档目录里的一个文件 → `(路径, 拒绝理由)`。

    与取图/删图同一套边界（只 `.png`、不碰 `_temp*`、`..` 一律挡），只是根固定成
    归档目录。所以还原与彻底删除**不需要客户端给 root** —— 归档里的东西只有一个来源，
    多一个可以被伪造成数字的参数，就是多一条「按它去动另一个目录」的路。
    """
    if name is None or str(name).strip() == "":
        return None, "没给文件名"
    name = str(name)
    path = safe_join(config.ARCHIVE_DIR, name)
    if path is None:
        return None, "非法的文件名"
    if not os.path.isfile(path):
        return None, "它不在归档目录里了"
    base = os.path.basename(path)
    if not base.lower().endswith(".png"):
        return None, "%s 不是 PNG" % base
    if base.startswith(SKIP_PREFIXES):
        return None, "%s 是中间产物，不该在归档里" % base
    return path, None


def restore_target(name):
    """还原的落点 + 撞名检查 → `(目标路径, 拒绝理由)`。"""
    path, why = resolve_archived(name)
    if path is None:
        return None, why
    base = os.path.basename(path)
    dest = safe_join(config.OUTPUT_DIR, base)
    if dest is None:
        return None, "非法的文件名"
    if os.path.exists(dest):
        # 会撞名是有原因的，不是异常：ComfyUI 的下一个序号是**扫现行文件名**算的，
        # 归档走一张 `…_00088.png` 之后，CLI 那条路会**再写一张同名的**。
        return None, ("output\\%s 已经占着这个名字（不覆盖、也不替它改名 —— "
                      "改了就跟任务记录对不上号了）" % base)
    return dest, None


def archived_names():
    """归档目录里的文件名集合。

    `/api/jobs` 用它给每条任务标「产物已归档」。磁盘是唯一真源：手工往归档目录里塞
    一张、或手工搬走一张，页面下一次轮询就跟着变。
    """
    try:
        names = os.listdir(config.ARCHIVE_DIR)
    except OSError:
        return set()
    return {n for n in names
            if n.lower().endswith(".png") and not n.startswith(SKIP_PREFIXES)
            and os.path.isfile(os.path.join(config.ARCHIVE_DIR, n))}


# ---------------------------------------------------------------------------
# 「这条任务的产物现在还在不在磁盘上」（P20j）
#
# 为什么这件事必须由**后端**按**磁盘**现算，而不是让页面从 `img.onerror` 反推：
# 任务缩略图走的是 `/api/image` → 后端 `/view`，所以「取不到图」有两组毫不相干的
# 原因 —— **文件真的没了**、或者**后端没起**。让页面去猜，它就会把「后端没起」
# 写成「已经不在磁盘上了（多半是被删了）」，还顺手把用户指去回收站找一张好端端
# 的图（2026-10-03 用户报的那张截图就是这么来的）。知道磁盘的那一层才能下结论。
#
# 四个字段各有各的用处（`output_verdict`），别合并成一个 flag。
# ---------------------------------------------------------------------------
def output_rel(o):
    """一件产物 → 相对某个输出目录的那条路径（`subfolder` 可能非空）。

    与 `server._first_output_path()`（自动放大那条链取产物）同一个算法 ——
    两处要是分家，就会出现「链找得到、这里说没了」那种没有症状的分叉。
    """
    name = (o or {}).get("filename") or ""
    sub = ((o or {}).get("subfolder") or "").strip("/\\")
    return ("%s/%s" % (sub, name)) if sub else name


def output_on_disk(roots, o):
    """这一件产物现在还能从某个候选输出目录里取到吗 → 那条路径 or None。

    逐个根去问，**命中就停**（日常第一个根就是工作区 output/）。
    """
    rel = output_rel(o)
    if not rel:
        return None
    for root in roots or []:
        path = safe_join((root or {}).get("path") or "", rel)
        if path and os.path.isfile(path):
            return path
    return None


def output_verdict(outputs, roots, archived):
    """一条任务记录的产物**此刻**的处境。

    - `on_disk` —— **至少一张**还在候选输出目录里（画得出图）；
    - `archived` —— **每一张**都在归档目录里（现有判据，一个字没改：卡片的
      「这张已经归档 · 还原这张」靠它）；
    - `restorable` —— 至少一张还能还原。这是清理时的**免死金牌**：还原回
      `output/` 之后那一行必须带着耗时与参数回来（P20h 第④条）；
    - `gone` —— **有产物、但一张都取不到了**。最近任务据此不画这一行。

    归档、从归档页彻底删除、手工搬走，在 `gone` 这里合成**同一个答案** ——
    对「最近任务」来说它们没有区别，都是「这一行没有图可以给你看」（2026-10-03
    用户定的：用同一条规则，别再分三种）。分两种的坏处上一版已经领教过了：
    「归档过、又被彻底删除」落进缝里，那一行会**重新冒出来**，还带着一张取不到的图。

    **没有产物时一律不判 `gone`**：排队/在跑/失败/取消都还没有产物，把它们藏起来
    是这个工作区最不能接受的坏法之一（失败必须看得见）。
    """
    outs = [o for o in (outputs or []) if (o or {}).get("filename")]
    if not outs:
        return {"on_disk": False, "archived": False, "restorable": False,
                "gone": False}
    live = [o for o in outs if output_on_disk(roots, o)]
    in_arc = [o for o in outs if o["filename"] in (archived or ())]
    return {
        "on_disk": bool(live),
        "archived": len(in_arc) == len(outs),
        "restorable": bool(in_arc),
        "gone": not live,
    }


# ---------------------------------------------------------------------------
# 与运行记录对账
# ---------------------------------------------------------------------------
def runs_index(runs):
    """`{filename: [run, ...]}`。只收真的出了图的终态记录。

    对畸形行一律跳过：`runs.jsonl` 是追加写的文本，半截行、空行、别人手改过的
    行都可能出现。画廊是只读视图，不该因为一条坏记录就整个打不开。
    """
    index = {}
    for run in runs or []:
        if not isinstance(run, dict) or run.get("status") != "done":
            continue
        outputs = run.get("outputs")
        if not isinstance(outputs, list):
            continue
        for out in outputs:
            if not isinstance(out, dict):
                continue
            name = out.get("filename")
            if name:
                index.setdefault(name, []).append(run)
    return index


_COMPARE_FIELDS = ("steps", "seed", "width", "height")


def _agrees(png_info, run):
    """PNG 反解出的参数 vs 运行记录里的参数。

    返回 True（对得上）/ False（对不上）/ None（没有可比的字段）。
    刻意不比 `mode`：`2k` 这个模式 2026-10-01 已经撤销，但**老记录里还写着 "2k"**，
    而 PNG 反解出来是 "t2i"（2K 在结构上就是 t2i + 大尺寸），拿它比会对一屋子
    历史图假报警。`rewrite` 同理：老记录记的是 `mode="refine"`，没有这个键。
    """
    params = run.get("params") or {}
    comparable = 0
    for key in _COMPARE_FIELDS:
        a = png_info.get(key)
        b = params.get(key)
        if a is None or b is None:
            continue          # width 可能是「链接到 ResolutionSelector」，不算矛盾
        comparable += 1
        if a != b:
            return False
    return True if comparable else None


def match_run(png_info, candidates):
    """在一组同名的运行记录里挑一个，并给出可信度。"""
    best, verdict = None, "none"
    for run in candidates or []:
        agreed = _agrees(png_info, run)
        if agreed is True:
            return run, "confirmed"
        if agreed is None and verdict == "none":
            best, verdict = run, "weak"
        elif agreed is False and verdict == "none":
            best, verdict = run, "conflict"
    return best, verdict


def _run_summary(run):
    if not run:
        return None
    return {"job_id": run.get("job_id"), "wall_ms": run.get("wall_ms"),
            "status": run.get("status"),
            "cached_nodes": run.get("cached_nodes") or [],
            "mode": run.get("mode")}


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def _item(root_index, root, name, stat_result, index):
    path = os.path.join(root["path"], name)
    unreadable = False
    try:
        workflow = pngmeta.prompt_workflow(path)
    except OSError:
        workflow = None
    if workflow is None:
        unreadable = True
        info = {"mode": None, "recognized": False, "prompt": "", "steps": None,
                "seed": None, "width": None, "height": None, "is_2k": False,
                "rewrite": False, "unet": None}
    else:
        info = pngmeta.summarize(workflow)

    run, verdict = match_run(info, (index or {}).get(name))
    item = {
        "id": "%d:%s" % (root_index, name),
        "root": root_index,
        "root_label": root.get("label"),
        "root_kind": root.get("kind"),
        "filename": name,
        "size_bytes": stat_result.st_size,
        "mtime": int(stat_result.st_mtime * 1000),
        "unreadable": unreadable,
        "run": _run_summary(run) if verdict in ("confirmed", "weak") else None,
        "run_match": verdict,
        "has_timings": verdict == "confirmed",
    }
    item.update(info)
    return item


def _iter_files(roots):
    for i, root in enumerate(roots):
        path = root.get("path")
        if not path:
            continue
        try:
            names = sorted(os.listdir(path))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".png"):
                continue
            if name.startswith(SKIP_PREFIXES):
                continue
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            yield i, root, name, st


def layer_set_prefixes(root_path):
    """这个根目录里有哪些「层集」—— 认出配套的 `*_layers.json`，返回 prefix 集合。

    ★ 这是排除规则的**第二半**。第一半是形状（`lb.LAYER_RE`），但形状**不唯一属于
    See-through**：用户自己丢进 `output/` 的一张图完全可能叫
    `photo_20260101_120000_deadbeef_cat.png`。所以两半同时成立才算层集：

        形状对  ∧  同根目录里有 `…_layers.json`

    只按形状排会**静默藏掉真图**；只按前缀排（改 `SKIP_PREFIXES`）更是把「按名字猜」
    写进了产品的判断里。两半在一起才是可证的。
    """
    out = set()
    try:
        names = os.listdir(root_path)
    except OSError:
        return out
    for name in names:
        match = lb.LAYERS_JSON_RE.match(name)
        if match:
            out.add(match.group("prefix"))
    return out


def is_layer_set_member(name, prefixes):
    """这个文件名是不是某个**已确认存在**的层集的一员。"""
    if not prefixes:
        return False
    match = lb.LAYER_RE.match(name)
    return bool(match) and match.group("prefix") in prefixes


def count_by(items):
    counts = {"all": len(items), "recognized": 0, "unrecognized": 0,
              "t2i": 0, "edit": 0, "upscale": 0, "is_2k": 0, "rewrite": 0,
              "with_timings": 0}
    for it in items:
        counts["recognized" if it.get("recognized") else "unrecognized"] += 1
        mode = it.get("mode")
        if mode in ("t2i", "edit", "upscale"):
            counts[mode] += 1
        if it.get("is_2k"):
            counts["is_2k"] += 1
        # 改写是**额外功能**（2026-10-01 起），与模式分开数 —— 一张图可以既是
        # T2I 又是改写过的，合成一个数就说明不了这件事。
        if it.get("rewrite"):
            counts["rewrite"] += 1
        if it.get("has_timings"):
            counts["with_timings"] += 1
    return counts


def scan(roots, index=None, limit=120, offset=0, mode=None, query=None,
         recognized=None, two_k=None, rewrite=None):
    """返回结果字典。**逐项降级**：读不懂的图照样列出来，只是标 unreadable。

    筛选项都对应 UI 上的一个 chip：
      `mode`       —— t2i / edit / upscale（形状）
      `rewrite`    —— 只要打开过改写器的（**额外功能**，与 mode 正交：
                      一张图可以既是 T2I 又是改写过的）
      `recognized` —— True 只要本链路的，False 只要别人的（output/ 里混着
                      5 张 MINIMAX），None 全都要
      `two_k`      —— 只要 2048² 的
    """
    # ★ 层集的成员**不进画廊**。理由与判据见 `layer_set_prefixes()`：
    # 一次拆层会在输出目录顶层丢下 40~50 个 PNG，不挡的话每跑一次就多 48 个
    # 「作品」（`pngmeta` 解不出参数，全是「未识别」），一屏就被它们占满。
    #
    # **H3 的视频不用挡**：它的前缀是 `video/MiniMax_H3`，落在 `output/video/`
    # 子目录里，而 `_iter_files()` 只 `listdir` 每个根的**顶层**。
    # 那是个不变量而不是巧合，`tests/test_gallery.py` 有一条判据钉着它。
    prefixes_by_root = {}
    items = []
    for i, root, name, st in _iter_files(roots):
        base = root.get("path") or ""
        if base not in prefixes_by_root:
            prefixes_by_root[base] = layer_set_prefixes(base)
        if is_layer_set_member(name, prefixes_by_root[base]):
            continue
        items.append(_item(i, root, name, st, index))
    items.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    counts = count_by(items)

    selected = items
    if mode:
        selected = [x for x in selected if x.get("mode") == mode]
    # 与 `two_k` 同一套写法：**只在为真时筛**（缺参数 = 不筛），而不是三态。
    # 「改写」这颗 chip 只会问「给我改写过的」，没有「给我没改写的」那一半 ——
    # 写成三态的话，缺参数（False）会变成「只要没改写的」，把大半屋子图筛没。
    if rewrite:
        selected = [x for x in selected if x.get("rewrite")]
    if recognized is not None:
        want = bool(recognized)
        selected = [x for x in selected if bool(x.get("recognized")) == want]
    if two_k:
        selected = [x for x in selected if x.get("is_2k")]
    if query:
        needle = query.strip().lower()
        if needle:
            selected = [x for x in selected
                        if needle in (x.get("prompt") or "").lower()
                        or needle in x["filename"].lower()]

    page = selected[offset:offset + limit]
    return {
        "items": page,
        "total": len(selected),
        "scanned": len(items),
        "offset": offset,
        "limit": limit,
        "counts": counts,
        "roots": [{"index": i, "path": r.get("path"), "label": r.get("label"),
                   "kind": r.get("kind"), "exists": r.get("exists")}
                  for i, r in enumerate(roots)],
        "generated_at": now_ms(),
    }
