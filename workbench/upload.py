"""参考图上传 —— 送进 ComfyUI **真正会读**的那个 `input/` 目录。

`input` 和 `output` 一样有**双胞胎**（实测）：

    …\\ComfyUI-Installs\\ComfyUI\\ComfyUI\\input   2 个文件   ← 铁律 1 那个进程读它
    …\\ComfyUI-Shared\\input                      1 个文件   ← Desktop 启动读它

证据是 `LoadImage` 的候选表：铁律 1 起着的后端给出的 `image` 选项恰好等于
前者的文件列表（`Qwen_2.1_fashion_00004.png` / `example.png`），而后者那个
`1.png` 根本不在里面。

写错目录的后果是**静默的**：文件真的躺在磁盘上，`os.path.exists` 也为真，
看起来一切正常 —— 但 ComfyUI 校验时按 `value_not_in_list` 打回。更坏的是
「这个名字在别的目录里存在」，于是你盯着一张不是自己的参考图查半天。

所以这里的判据不是「我写到哪了」，而是「**后端读到了吗**」：`verified` 只在
`LoadImage` 的候选表里真的出现该文件名时才为 True。与 P2 画廊同一条纪律 ——
文件名对账，不认自己的推断。

两条落盘路径（`upload_any` 是默认入口）
--------------------------------------
    backend  请 ComfyUI 自己写（`POST /upload/image`）—— **首选**
    local    工作台进程自己写（`save()`），写完回读候选表对账

为什么首选 backend，理由有两条，都不是「顺手」：

1. **曾经只有它写得了。** 2026-09-30 三路隔离实测：同一个目录，**当时的工作区
   解释器**被拒（`PermissionError`），ComfyUI 自己的解释器写得进去，会话的 pwsh
   也写得进去 —— 拦人的是**强制完整性标签**（解释器是 Low，就写不了没有标签、
   因而按 Medium 算的对象），跟 DACL 无关。2026-10-01 起工作台的解释器搬到了
   工作区外（`%LOCALAPPDATA%\\qwen21-venv`），这条限制不再挡路。
   **但首选 backend 这条决定不改**：理由 2 与它无关，而且换台机器（或哪天解释器
   又被放回被打标的树里）时，这条路仍然是对的。
2. **落点不再需要猜。** 双胞胎 `input/` 的问题（这一节开头那段）在使用 backend
   路径时**整个消失**：ComfyUI 按自己的配置落盘，我们连目录都不必知道。

`local` 路径保留着，而且**不是死代码**：工作台若跑在沙箱**之外**的解释器上
（比如自己双击 `run.bat`），它一样能写，那时两条路都通。后端没起时也只有它。
两条路都是先核名字、再核字节，然后回读候选表 —— 判据只有一套。

顺带一个实测结论，它决定「覆盖同名文件」安不安全：`LoadImage.IS_CHANGED`
（`nodes.py`）返回的是**文件内容的 sha256**，不是 mtime。所以往同一个名字覆盖一张
不同的图，ComfyUI 的节点缓存会正确失效，不会把旧图喂下去（那会是一种「空转」）。
"""
import mimetypes
import os

from . import comfy, config, gallery, util

# 后端用来取参考图的节点。候选表就是「它现在能读到的文件」——我们的活体判据。
LOAD_IMAGE_CLASS = "LoadImage"
LOAD_IMAGE_FIELD = "image"

MAX_NAME = 120
# 2K 的成品约 2.4 MB；32 MiB 对参考图绰绰有余，同时挡住「拖错文件」。
# 上传是**在内存里**收完整体的（见 server.post_upload），所以这个上限也是内存上限。
MAX_BYTES = 32 * 1024 * 1024

# 只收**任何平台**都由 stdlib 内置表映射到 `image/*` 的后缀（不依赖 Windows 注册表）。
# 为什么跟着 `mimetypes` 走：ComfyUI 的 loadImage 就是用
# `folder_paths.filter_files_content_types(files, ["image"])` 过滤候选表的，
# 而后者的实现是 `mimetypes.guess_type`。用别的规则就会漂移 ——
# 我们收了、后端不认，就正好落进上面说的静默失败。
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")

BAD_CHARS = set('<>:"/\\|?*')


class UploadError(ValueError):
    """参数/名字不合法。继承 ValueError —— server 已经统一把它变成 400。"""


def is_image_name(name):
    """后端候选表会不会把它算成图片。跟 `mimetypes` 对，不跟我们的偏好对。"""
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in IMAGE_EXTS:
        return False
    return (mimetypes.guess_type("x" + ext, strict=False)[0] or "").startswith("image/")


def safe_name(name):
    """把用户给的文件名收成一个**纯文件名**，不合格就抛。

    两道防线是刻意的：这里挡分隔符与非法字符，`save()` 再核一次最终路径仍在根里。
    上传是唯一一个「客户端决定落盘位置」的入口，不能只靠一层。
    """
    if not isinstance(name, str):
        raise UploadError("文件名必须是字符串")
    name = name.strip()
    if not name:
        raise UploadError("缺少文件名（用 ?filename= 或 X-Filename 头给）")
    # 浏览器一般只给 basename，但别信它 —— 两种分隔符都要当成路径处理
    name = name.replace("\\", "/").split("/")[-1].strip()
    if name in ("", ".", ".."):
        raise UploadError("文件名不合法：%r" % name)
    if len(name) > MAX_NAME:
        raise UploadError("文件名太长（上限 %d 字符）" % MAX_NAME)
    bad = sorted(c for c in set(name) if c in BAD_CHARS or ord(c) < 32)
    if bad:
        raise UploadError("文件名含非法字符：%s" % " ".join(repr(c) for c in bad))
    if name.endswith((".", " ")):
        # Windows 会静默去掉结尾的点/空格，于是「写进去的名字」和「读出来的名字」
        # 不是同一个 —— 这类不一致最难查，宁可在门口挡掉
        raise UploadError("文件名不能以点或空格结尾：%r" % name)
    if not is_image_name(name):
        raise UploadError("不是图片后缀：%s（只收 %s）"
                          % (os.path.splitext(name)[1] or "（无后缀）",
                             " ".join(IMAGE_EXTS)))
    return name


# ---------------------------------------------------------------------------
# 找后端在读的那个 input/ 目录
# ---------------------------------------------------------------------------
def root_candidates(client=None, stats=None):
    """候选 input 目录，按可信度排序。**都返回**（含不存在的），让 UI 能解释。

    顺序与 `gallery.candidate_roots` 同源：启动参数最权威（它是**当前进程**在用的），
    然后是铁律 1 的安装目录，最后才是 Desktop 与工作区。
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

    if stats is None and client is not None:
        try:
            stats = client.system_stats()
        except Exception:          # 后端没起 / 网络抖 —— 推断路径本来就是尽力而为
            stats = None
    argv = ((stats or {}).get("system") or {}).get("argv") or []
    for i, token in enumerate(argv):
        if token in ("--input-directory", "--input_directory") and i + 1 < len(argv):
            add(argv[i + 1], "启动参数 --input-directory", "argv")
        elif token.startswith(("--input-directory=", "--input_directory=")):
            add(token.split("=", 1)[1], "启动参数 --input-directory", "argv")

    install = gallery.desktop_install_root()
    if install:
        add(os.path.join(install, "ComfyUI", "input"),
            "手动启动（铁律 1 的读点）", "manual")

    add(os.path.join(config.COMFY_ROOT, "input"),
        "手动启动（推算路径）", "manual-fallback")
    add(os.path.join(_shared_root(), "input"), "Desktop 启动", "desktop")
    add(os.path.join(config.WORKSPACE, "input"), "工作区", "workspace")
    return roots


def _shared_root():
    """`ComfyUI-Shared` —— Desktop 启动时的数据目录。读不到就退化成空串。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "Comfy-Desktop", "ComfyUI-Shared")


def list_dir_images(path):
    """目录里的图片文件名（已排序）。目录不存在/读不了返回 None —— 与 [] 区分。"""
    try:
        names = os.listdir(path)
    except OSError:
        return None
    out = []
    for n in names:
        try:
            if os.path.isfile(os.path.join(path, n)) and is_image_name(n):
                out.append(n)
        except OSError:
            continue
    return sorted(out)


def resolve(client=None, stats=None):
    """定出后端在读的 input 目录，并**说清是怎么定出来的**。

    首选办法是拿 `LoadImage` 的候选表逐名对账（这就是「后端能读到的文件集」）。
    对不上时**不装作知道** —— 返回 `verified: False` 加一句人话，让调用方
    （以及页面上的用户）知道这次的落点只是推断。
    """
    roots = root_candidates(client=client, stats=stats)
    if not roots:                                  # 理论上不会，兜底
        return {"path": None, "label": None, "kind": None, "exists": False,
                "verified": False, "how": "一个候选目录都推不出来",
                "candidates": [], "options": None}

    options = visible_names(client)

    if options is None:
        top = roots[0]
        return {**top, "verified": False, "options": None, "candidates": roots,
                "how": "读不到 %s.%s 的候选表（后端没起？），按启动参数与安装目录推断"
                       % (LOAD_IMAGE_CLASS, LOAD_IMAGE_FIELD)}

    want = set(options)
    if want:
        for r in roots:
            got = list_dir_images(r["path"])
            if got is not None and set(got) == want:
                return {**r, "verified": True, "options": options,
                        "candidates": roots,
                        "how": "候选表的 %d 个文件与该目录逐名一致" % len(want)}
        # 候选表非空却没有一个目录对得上 → 后端读的是我们不知道的第三个目录。
        # 这时**任何**候选都是猜的，必须如实说。
        top = roots[0]
        return {**top, "verified": False, "options": options, "candidates": roots,
                "how": "后端候选表有 %d 个文件，但没有任何候选目录与之逐名一致 —— "
                       "它读的可能是第三个 input 目录" % len(want)}

    # 候选表是空的：所有空目录彼此无法区分，此时「对账」等于没对账。
    # 按可信度取第一个，但**标成未验证** —— 从空集里推出结论正是要避免的那种假自信。
    top = roots[0]
    return {**top, "verified": False, "options": options, "candidates": roots,
            "how": "后端候选表是空的，无法区分各个空目录；按可信度选了 %s" % top["label"]}


def list_inputs(client=None, stats=None):
    """页面要的东西：当前能用的参考图 + 落点 + 是否已验证。"""
    root = resolve(client=client, stats=stats)
    files = list_dir_images(root.get("path")) if root.get("path") else None
    return {
        "root": root.get("path"),
        "root_label": root.get("label"),
        "root_kind": root.get("kind"),
        "root_exists": bool(root.get("exists")),
        "verified": bool(root.get("verified")),
        "how": root.get("how"),
        # files=None 表示读不到目录（≠ 目录为空），两种病要分开说
        "files": files,
        "backend_files": root.get("options"),
        "candidates": [{k: r.get(k) for k in ("path", "label", "kind", "exists")}
                       for r in (root.get("candidates") or [])],
        "max_bytes": MAX_BYTES,
        "extensions": list(IMAGE_EXTS),
    }


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def _validated(name, data):
    """两条落盘路径**共用**的入参校验：先名字、再字节。

    抽出来不是为了少写两行，而是为了让两条路走同一道门 —— 分头各写一份，
    迟早会有一条漏掉「空文件」或「上限」，而那种漏**没有症状**。
    """
    name = safe_name(name)
    data = bytes(data)
    if not data:
        raise UploadError("空文件")
    if len(data) > MAX_BYTES:
        raise UploadError("文件太大：%.1f MB，上限 %d MB"
                          % (len(data) / 1048576.0, MAX_BYTES // 1048576))
    return name, data


def visible_names(client):
    """后端 `LoadImage` 现在能读到的文件名。**读不到返回 `None`**（≠ 空表）。

    这一个函数就是「后端能看见什么」的唯一来源 —— `resolve()`、`upload()`、
    `upload_any()` 都从它取。三处各问一遍的话，一次请求里就会对同一条事实
    拿到三份可能不同的答案。
    """
    if client is None:
        return None
    try:
        return client.combo_options(LOAD_IMAGE_CLASS, LOAD_IMAGE_FIELD)
    except Exception:                              # noqa: BLE001
        return None


def save(root, name, data, overwrite=True):
    """把字节写进 `root/name`。原子写，所以读者要么看到旧图要么看到新图。"""
    if not root:
        raise UploadError("没有可写的 input 目录")
    name, data = _validated(name, data)

    base = os.path.abspath(root)
    path = os.path.abspath(os.path.join(base, name))
    # 第二道防线：safe_name 已经去掉分隔符了，这里核的是**最终路径真的还在根里**
    if os.path.normcase(os.path.dirname(path)) != os.path.normcase(base):
        raise UploadError("拒绝写到目录之外：%s" % name)

    existed = os.path.exists(path)
    if existed and not overwrite:
        raise UploadError("已存在同名文件：%s（要替换请带 overwrite=1）" % name)
    old_size = os.path.getsize(path) if existed and os.path.isfile(path) else None

    util.atomic_write_bytes(path, data)
    return {
        "filename": name,
        "path": path,
        "root": os.path.abspath(root),
        "bytes": len(data),
        "existed": existed,
        # 大小一样也照样算「已替换」——只有内容 sha256 相同才敢说没变，
        # 这里不为了一个好听的词去读一遍旧文件
        "replaced": bool(existed),
        "old_bytes": old_size,
    }


def _verify(client, out):
    """回读候选表：**后端真的看得见它吗**。这才是这一层要回答的问题。"""
    visible = visible_names(client)
    out["backend_files"] = visible
    if visible is None:
        out["verified"] = False
        out["warning"] = ("写好了，但读不到 %s 的候选表，无法证明后端能看见它。"
                          "后端起好后请在本页刷新参考图列表。" % LOAD_IMAGE_CLASS)
    elif out["filename"] in visible:
        out["verified"] = True
        out["warning"] = None
    else:
        out["verified"] = False
        out["warning"] = ("写进去了，但后端候选表里没有它（候选表现在 %d 个文件）"
                          "—— 后端读的是另一个 input 目录。" % len(visible))
    return out


def upload_via_backend(client, name, data, overwrite=True, before=None):
    """请 **ComfyUI 自己**把这张图写进它的 `input/`，然后**证明它读得到**。

    返回的形状与 `save()` 对齐（`filename`/`bytes`/`existed`/`replaced`），
    外加 `via="backend"`。两处差别都要如实说：

    - `path`/`root` 是 `None`。**不是没算出来，是本来就不该由我们报** ——
      文件落在 ComfyUI 进程的目录里，我们只从它的回执里拿到一个文件名。
      猜一个目录填进这两个字段，就正好制造出这个模块一直在消灭的那种假自信。
    - `filename` 用的是**后端回执里的**名字：`overwrite=false` 撞上同名时，
      ComfyUI 会改成 `name (1).png` 而不是拒绝（`server.py::image_upload`）。
    """
    name, data = _validated(name, data)
    info = client.upload_image(name, data, overwrite=overwrite)
    saved = info.get("name")
    # `before` 是**上传之前**读到的候选表（调用方已经取过一次，别重复往返）。
    # 读不到（None）时 `existed` 报 None —— 「不知道」比猜一个 False 诚实。
    existed = None if before is None else (saved in before or name in before)
    out = {
        "filename": saved,
        "path": None,
        "root": None,
        "bytes": len(data),
        "existed": existed,
        "replaced": bool(existed),
        "old_bytes": None,
        "subfolder": info.get("subfolder") or "",
        "via": "backend",
    }
    if saved != name:
        # 名字被后端改了。**必须说出来**：用户以为传的是 `a.png`，而图里引用的
        # 会是 `a (1).png`；不提就等于埋了个坑。
        out["renamed_from"] = name
    return _verify(client, out)


def upload(client, name, data, overwrite=True):
    """**本地写** + 证明后端看得见。返回里的 `root` 就是落点。

    这是老路径，现在只在 `upload_any` 的兜底分支里被调用（后端没起、或它不认
    `/upload/image`）。它必须先`resolve()` 出落点 —— 那是它唯一能知道自己写对
    没写对的办法。
    """
    root = resolve(client=client)
    out = save(root.get("path"), name, data, overwrite=overwrite)
    out["root_label"] = root.get("label")
    out["root_verified"] = bool(root.get("verified"))
    out["how"] = root.get("how")
    out["via"] = "local"
    out["candidates"] = [{k: r.get(k) for k in ("path", "label", "kind", "exists")}
                         for r in (root.get("candidates") or [])]
    return _verify(client, out)


def upload_any(client, name, data, overwrite=True):
    """上传的**默认入口**：先请后端写，不行才退回本地写。

    兜底不是「保险起见」—— 它有明确的适用面：后端没起、后端没有
    `/upload/image`、或者工作台**跑在沙箱之外**（那时本地写本来就通）。
    真实原因放在 `backend_error` 里原样带回：否则用户只看到「写不了 input
    目录」，不知道我们其实先请后端写过一遍。
    """
    name, data = _validated(name, data)

    # 「不许覆盖」这一条只有**我们**实现得了：ComfyUI 的 `/upload/image` 撞上同名
    # 会改名而不是拒绝，那会把「拒绝」变成「悄悄换了个名字」—— 后者正是最难查的
    # 那种错。所以先在候选表上判一道；判不了（表读不到）就交给本地写，它在磁盘上
    # 有同样的判据。
    before = visible_names(client)
    if not overwrite and before is not None and name in before:
        raise UploadError("已存在同名文件：%s（要替换请带 overwrite=1）" % name)
    # 表读不到时不能走 backend：它会把「同名」处理成改名，而我们已经答应了拒绝。
    can_delegate = overwrite or before is not None

    backend_error = None
    if client is not None and can_delegate:
        try:
            return upload_via_backend(client, name, data, overwrite=overwrite,
                                      before=before)
        except (comfy.ComfyUnreachable, comfy.ComfyUploadFailed) as exc:
            backend_error = str(exc)

    try:
        out = upload(client, name, data, overwrite=overwrite)
    except OSError as exc:
        # 两条路都没写进去。**把两条的原话都带上** —— 只报本地那条，用户会以为
        # 「工作台写不了文件」，而真正的第一现场是后端那一句。
        detail = "写不了 input 目录：%s" % exc
        if backend_error:
            detail += "。先前请 ComfyUI 自己写也没成：%s" % backend_error
        raise OSError(detail) from exc
    out["backend_error"] = backend_error
    return out


# ---------------------------------------------------------------------------
# 读一张**已经在后端 input/ 里**的图
# ---------------------------------------------------------------------------
def read_reference(name, client=None, stats=None):
    """把一张参考图的**字节**取回来（给 LLM 看）。找不到就抛，**绝不返回空**。

    为什么不走后端那条 `/view`：`comfy.fetch_view` 把**任何** HTTP 错误都报成
    「连不上」（它分不出 404），于是「这张图后端没有」会说成「后端没起」——
    用户照着那句去修，会修错地方。磁盘这条路能分清「目录里没有它」与
    「不知道目录在哪」，而这两件事的改法完全不同。

    三段判定，按可信度排：

      verified    `resolve()` 已经拿 `LoadImage` 候选表逐名对账过 → 在那个目录里读
      唯一命中    没能对账，但**只有一个**候选目录里有这个名字 → 读它（`verified=False`）
      其余        一个都没有 → 抛；**两个以上**都有 → 抛

    最后一条不是洁癖：`input/` 实测有双胞胎（安装目录与 `ComfyUI-Shared`）。
    同一个名字在两边都存在时，只有后端自己知道它读的是哪一个；后端不在，
    这个问题就**没有诚实的答案** —— 猜一个然后把另一张图的字节发给模型，
    会让它照着那张图写提示词，而没有症状。
    """
    name = safe_name(name)
    root = resolve(client=client, stats=stats)
    candidates = root.get("candidates") or []
    verified = bool(root.get("verified"))
    if verified and root.get("path"):
        path = os.path.join(root["path"], name)
        if not os.path.isfile(path):
            # 对账过的目录里没有它 —— 与页面上方块那颗「后端 input/ 里没有」同源
            raise UploadError("后端 input/ 里没有『%s』（%s）"
                              % (name, root.get("how")))
    else:
        hits = []
        for r in candidates:
            p = os.path.join(r["path"], name)
            try:
                if os.path.isfile(p):
                    hits.append((r, p))
            except OSError:                       # 目录读不了就当它没有
                continue
        if len(hits) > 1:
            raise UploadError("『%s』在 %d 个 input 目录里都有（%s）—— 分不清后端读的"
                              "是哪一个。把后端起起来就能对账，或换一张只在一边的图。"
                              % (name, len(hits),
                                 " / ".join(h[0]["label"] for h in hits)))
        if not hits:
            raise UploadError("找不到『%s』：%d 个候选 input 目录里都没有它（%s）"
                              % (name, len(candidates), root.get("how")))
        path = hits[0][1]

    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        raise UploadError("读不了『%s』：%s" % (name, exc)) from exc
    if len(data) > MAX_BYTES:
        raise UploadError("『%s』超过 %d MB" % (name, MAX_BYTES // 1048576))
    return {"name": name, "data": data, "path": path,
            "root": os.path.dirname(path), "verified": verified,
            "how": root.get("how"), "root_label": root.get("label")}
