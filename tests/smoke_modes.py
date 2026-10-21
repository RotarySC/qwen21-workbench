#!/usr/bin/env python3
"""活体冒烟：四模式各出一图 + 编辑能改指定元素（P4 的门）。

与其余测试的分工：那些用 `TestClient` + 内存替身，毫秒级、不需要任何后端。
这里要的是**真的**：真的起一个工作台进程，真的上传一张参考图，真的让
ComfyUI 出图，然后把像素读出来比。GPU 上每次 1~11 分钟，所以它不进单测。

判「空转」用的是 `pngpix`（纯标准库）而不是文件哈希 —— 见 AGENTS.md「空转」：
PNG 文件哈希不同**不代表**像素不同（ComfyUI 把 prompt JSON 嵌进 tEXt，
换个提示词必然改哈希，两张完全一样的图也会有不同的哈希）。

    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe tests\\smoke_modes.py --mode t2i --mode edit
    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe tests\\smoke_modes.py            # 四个全跑，约 17 分钟

产物：`state/smoke_modes_report.json`（机器可读的结论）+ 控制台逐条日志。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

import pngpix  # noqa: E402
from workbench import pngmeta  # noqa: E402

# PowerShell 的控制台是 GBK，遇到 `²` 这类字符 print 会直接抛 UnicodeEncodeError
# 把整个冒烟打断 —— 而且报错信息跟真正的失败长得一样。这里只把不能编码的
# 字符降级成 `?`，保留控制台自己的编码（中文才显示得对）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

T2I_PROMPT = ("Photorealistic studio photograph of a single fresh green apple on a "
              "pale marble surface, soft directional daylight from the upper left, "
              "visible skin speckles, shallow depth of field, no text, no watermark.")
REFINE_PROMPT = "a red apple on a wooden table"
EDIT_PROMPT = "Make the sunglasses bright red"

LOG = []
REPORT = {"phases": {}, "images": {}, "checks": []}


def log(msg):
    line = "[%7.2fs] %s" % (time.time() - log.t0, msg)
    LOG.append(line)
    print(line, flush=True)
log.t0 = time.time()


def check(name, ok, detail=""):
    REPORT["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    log("%-46s %s%s" % (name, "PASS" if ok else "**FAIL**",
                        ("  " + detail) if detail else ""))
    return ok


def skip_check(name, reason):
    """记录下来但**不算失败**：这是环境限制，不是产品缺陷。

    分得清这两种很重要 —— 把「沙箱不许写」记成失败，会让人以后去修一个
    根本不存在的问题；直接不记，又会让「没验过」冒充「验过了」。
    """
    REPORT["checks"].append({"name": name, "ok": None, "skipped": True,
                             "detail": reason})
    log("%-46s SKIP  %s" % (name, reason))
    return False


def is_sandbox_denial(exc):
    """`PermissionError` 且没有「文件已存在」之类的其它解释 —— 就是受限挡住了。

    本机实测（2026-10-01 前后都成立）：解释器若落在被打 Low 完整性标签的目录树里，
    写 `%LOCALAPPDATA%` 一律被拒，连 `open(path,'wb')` 都过不去（见 AGENTS.md
    铁律 2）。工作台的解释器现在搬到了工作区外（`%LOCALAPPDATA%\\qwen21-venv`），
    所以**正常情况下这一项不该再被拒** —— 留着这条判据是为了「跑错了解释器」时
    如实记 SKIP，而不是把它报成产品失败。
    """
    if not isinstance(exc, PermissionError):
        return False
    return getattr(exc, "errno", None) in (13, None)


def wait_health(base, timeout=40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/api/health", timeout=1.0, trust_env=False)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def wait_backend(base, timeout=10.0):
    try:
        return httpx.get(base + "/api/backend", timeout=5.0,
                         trust_env=False).json()
    except httpx.HTTPError:
        return None


def find_output(roots, filename):
    """在候选输出目录里按文件名找产物。找不到返回 None。

    注意有**两个** output 目录（见 AGENTS.md）：手工启动与 Desktop 启动各一个。
    所以这里全扫，而且只认真正存在的那份。
    """
    for root in roots or []:
        path = root.get("path")
        if not path:
            continue
        cand = os.path.join(path, filename)
        if os.path.isfile(cand):
            return cand
    return None


def run_one(client, base, mode, args):
    """提交一个变体并等它跑完。返回 (job 回执, 产物磁盘路径 or None)。

    **`mode` 在这里是这份脚本自己的变体标签，不是 HTTP 的 `mode` 字段**：
    2026-10-01 重构之后 `refine` 与 `2k` 不再是模式（变成了额外开关与尺寸），
    所以标签换成了 `rewrite` / `big`，而它们提交的都是 `mode="t2i"`。
    """
    body = {"mode": "t2i", "prompt": T2I_PROMPT, "steps": args.steps,
            "seed": args.seed, "cfg": 1.0, "confirm": True}
    if mode == "edit":
        body["mode"] = "edit"
        body["prompt"] = EDIT_PROMPT
        body["source_image"] = args.ref
        body["resolution"] = 1024
        body["steps"] = args.edit_steps
    elif mode == "rewrite":
        body["prompt"] = REFINE_PROMPT
        body["rewrite"] = True          # 额外功能（原来是模式 "refine"）
    elif mode == "big":
        body["width"] = 2048            # 2K 就是显式尺寸（原来是模式 "2k"）
        body["height"] = 2048
        body["steps"] = args.big_steps
    else:
        body["width"] = 1024
        body["height"] = 1024

    t0 = time.time()
    r = client.post(base + "/api/run", json=body, timeout=60.0)
    if r.status_code != 200:
        log("  提交失败 HTTP %d: %s" % (r.status_code, r.text[:400]))
        return None, None, body
    job_id = r.json()["job_id"]
    log("  %s 已提交 job_id=%s，等它跑完（预计 %s）"
        % (mode, job_id, r.json().get("estimate_s", "?")))

    # 用 /history 兜底的那条路收尾，同时把 SSE 的事件数也看一眼
    last_log = 0.0
    while time.time() - t0 < args.timeout:
        time.sleep(1.0)
        j = client.get(base + "/api/jobs/" + job_id, timeout=20.0).json()
        st = j.get("status")
        el = time.time() - t0
        if el - last_log > 15:
            last_log = el
            prog = j.get("progress") or {}
            log("    %5.0fs status=%s progress=%s" % (el, st, prog.get("value")))
        if st in ("done", "error", "cancelled"):
            break
    else:
        log("  超时 %s 秒仍未收尾" % args.timeout)

    j = client.get(base + "/api/jobs/" + job_id, timeout=20.0).json()
    wall = j.get("wall_ms") or int((time.time() - t0) * 1000)
    log("  %s 收尾：status=%s wall=%s ms outputs=%d"
        % (mode, j.get("status"), wall, len(j.get("outputs") or [])))
    outs = j.get("outputs") or []
    path = None
    if outs:
        name = outs[-1].get("filename")
        roots = (REPORT["backend"] or {}).get("output_roots") or []
        path = find_output(roots, name)
        if path is None:
            log("  **产物文件在磁盘上找不到**：%s" % name)
    return j, path, body


def _one_mode(c, base, mode, a, ref, ref_pix, pix_by_mode):
    """跑一个模式并把它所有的检查做完。异常由调用方兜住。

    注意 `info2` 里的 width/height **可能是 None** —— edit 图里没有
    `459:456`（EmptyLatentImage，EDIT_DROP 摘掉了），所以 PNG 元数据里就没有尺寸。
    日志行一律用 `%s`，别用 `%d`：第一次跑就是 `%d` 撞上 None 抛了 TypeError，
    把后面还没跑的 refine 与 2K 一起带走了。
    """
    log("--- %s ---" % mode)
    job, path, body = run_one(c, base, mode, a)
    entry = {"job_id": (job or {}).get("job_id"),
             "status": (job or {}).get("status"),
             "wall_ms": (job or {}).get("wall_ms"),
             "outputs": (job or {}).get("outputs"),
             "file": path, "prompt": body["prompt"]}
    if job and job.get("status") != "done":
        entry["error"] = job.get("error")
    REPORT["images"][mode] = entry
    if not check("%s 出图成功" % mode, entry["status"] == "done",
                 "status=%s err=%s" % (entry["status"],
                                       str(entry.get("error"))[:200])):
        return
    if not check("%s 产物文件落盘" % mode, bool(path), str(path)):
        return

    with open(path, "rb") as f:
        blob = f.read()
    wf = pngmeta.prompt_workflow(blob)
    info2 = pngmeta.summarize(wf or {})
    entry["png"] = info2
    entry["png_bytes"] = len(blob)
    log("    PNG：%s %s %s steps=%s seed=%s mode=%s source=%s"
        % (os.path.basename(path),
           "%sx%s" % (info2.get("width"), info2.get("height"))
           if info2.get("width") else "(无尺寸 — edit 图没有 latent 节点)",
           "%.2f MB" % (len(blob) / 1048576.0), info2.get("steps"),
           info2.get("seed"), info2.get("mode"), info2.get("source_image")))

    # 空转（图级）：提示词必须**落到它该落的地方**。每个模式落点不同，
    # 这不是随便定的（实测三种 vs 一种）：
    #   t2i / refine / 2k —— 走 switch：
    #       · `459:474.on_false` ← **断边修复**补的就是这条边
    #       · `459:471.prompt`   ← 改写器（TextGenerate）的入参
    #   edit —— 上面两个节点在 edit 图里**被摘掉了**（EDIT_DROP），
    #       提示词直接写成文字编码器 `459:452.prompt`
    #
    # 别拿 `459:452.prompt` 去比非 edit 模式：那里它是**链接**
    # （形如 `["459:474", 0]`）不是字面量 —— 第一版就是这么比字符串，
    # 结果四种模式全被误报成 FAIL，而图其实是对的。
    if mode == "edit":
        expect = {"459:452": {"prompt": body["prompt"]}}
    else:
        expect = {"459:474": {"on_false": body["prompt"]},
                  "459:471": {"prompt": body["prompt"]}}
    landed = []
    for nid, fields in sorted(expect.items()):
        node = (wf or {}).get(nid) or {}
        for field, want in sorted(fields.items()):
            got = (node.get("inputs") or {}).get(field)
            landed.append((got == want, "%s.%s" % (nid, field)))
    check("%s 提示词落在该落的地方（断边已补）" % mode,
          all(ok for ok, _ in landed),
          " ".join("%s=%s" % (n, "ok" if ok else "MISMATCH")
                   for ok, n in landed))
    check("%s 该有的节点都在（没有链接断边）" % mode,
          all((wf or {}).get(nid) for nid in expect),
          "nodes=%d" % len(wf or {}))

    # 每个模式图的形状（与构建器的模式接线一一对应）
    if mode == "edit":
        check("edit 的 PNG 记下了参考图",
              info2.get("source_image") == ref,
              str(info2.get("source_image")))
        check("edit 图里没有 switch / 改写器（这两节点被摘掉了）",
              "459:474" not in (wf or {}) and "459:471" not in (wf or {}),
              "nodes=%s" % sorted((wf or {}).keys()))
        lo = ((wf or {}).get("900") or {}).get("inputs") or {}
        check("edit 图里 LoadImage 指向参考图",
              lo.get("image") == ref, "image=%r" % lo.get("image"))
        check("edit 的 KSampler 用 TextEncode 的 latent（不是 EmptyLatentImage）",
              ((wf or {}).get("459:458") or {}).get("inputs", {}).get(
                  "latent_image") == ["459:452", 2],
              "latent_image=%r" % ((wf or {}).get("459:458") or {}).get(
                  "inputs", {}).get("latent_image"))
    else:
        want_switch = (mode == "rewrite")
        got_switch = ((wf or {}).get("459:474") or {}).get(
            "inputs", {}).get("switch")
        check("%s 的 switch=%s（改写器该%s）"
              % (mode, want_switch, "开" if want_switch else "关"),
              got_switch is want_switch, "switch=%r" % got_switch)

    # 空转（像素级）：与参考图/其它模式比，绝不能是「逐像素相同」
    pix = pngpix.pixels(blob)
    entry["pixels"] = {"w": pix[0], "h": pix[1], "ch": pix[2]}
    if mode == "edit":
        try:
            st = pngpix.diff_of_pixels(pix, ref_pix, limit=a.limit)
            entry["vs_ref"] = st
            log("    vs 参考图：变了 %.2f%%（mean_abs=%.3f max=%d）"
                % (st["changed_pct"], st["mean_abs"], st["max_abs"]))
            check("edit 确实改动了画面（不是原图直出）",
                  not st["identical"] and st["changed_pct"] > 10.0,
                  "changed=%.2f%%（整个画面都会被重掷，见 AGENTS.md）"
                  % st["changed_pct"])
        except pngpix.PngError as exc:
            check("edit 与参考图尺寸一致", False, str(exc))

    for other, opix in list(pix_by_mode.items()):
        if other == mode:
            continue
        try:
            st = pngpix.diff_of_pixels(pix, opix, limit=a.limit)
            entry.setdefault("vs_other", {})[other] = st
            check("%s 与 %s 不同（换模式换图）" % (mode, other),
                  not st["identical"],
                  "changed=%.2f%%" % st["changed_pct"])
        except pngpix.PngError:
            # 2K 与 1K 尺寸不同 —— 本来就不该逐像素比
            pass
    pix_by_mode[mode] = pix


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8201)
    ap.add_argument("--comfy-port", type=int, default=8188)
    ap.add_argument("--mode", action="append", default=None,
                    choices=["t2i", "edit", "rewrite", "big"],
                    help="可重复；不给就四个全跑。"
                         "这里是**变体**标签：rewrite = t2i + rewrite=True，"
                         "big = t2i + 2048²（2026-10-01 起两者都不是模式了）")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--edit-steps", type=int, default=25)
    ap.add_argument("--big-steps", type=int, default=10,
                    help="2K 默认只跑 10 步（25 步实测 11 分钟，10 步够判「能出一图」）")
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--timeout", type=float, default=1500.0)
    ap.add_argument("--ref", default=None,
                    help="参考图名；不给就用 input/ 里最大的那张 PNG")
    ap.add_argument("--upload-probe", default=None,
                    help="要上传的本地文件；不给就复制 --ref 当探针")
    ap.add_argument("--limit", type=int, default=400000,
                    help="像素百分比统计的抽样上限（identical 始终精确）")
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "smoke_modes_report.json"))
    ap.add_argument("--keep", action="store_true", help="不删临时 state 目录")
    a = ap.parse_args(argv)

    modes = a.mode or ["t2i", "edit", "rewrite", "big"]
    base = "http://127.0.0.1:%d" % a.port
    # 刻意用仓库里的 state/ 而不是 tempfile.mkdtemp()：本机实测工作区解释器
    # **写不了 %TEMP%**（沙箱按可执行文件位置判定），tempfile 会悄悄退回 cwd，
    # 于是临时目录落在仓库根目录、还带一条 warning。放在 state/ 既确定又被忽略。
    scratch = os.path.join(REPO, "state")
    os.makedirs(scratch, exist_ok=True)
    state = tempfile.mkdtemp(prefix="smoke-modes-state-", dir=scratch)
    REPORT["state_dir"] = state
    REPORT["modes"] = modes

    env = dict(os.environ)
    env["QWEN21_STATE"] = state
    env["QWEN21_COMFY_PORT"] = str(a.comfy_port)
    proc = subprocess.Popen(
        [sys.executable, "-m", "workbench.server", "--port", str(a.port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")

    # **必须**有人一直读这个 pipe。uvicorn 的访问日志几十秒就能把 64 KB 的
    # 管道缓冲填满，然后服务端**阻塞在 write 上**，整个工作台看起来像死了一样
    # （表现为客户端 read timeout，而不是连接被拒 —— 很容易误判成「服务端慢」）。
    server_log = []

    def drain():
        for line in proc.stdout:
            server_log.append(line.rstrip())
    t = threading.Thread(target=drain, daemon=True)
    t.start()

    try:
        if not wait_health(base):
            log("工作台没起来；子进程输出：")
            proc.terminate()
            time.sleep(0.5)
            print("\n".join(server_log[-40:]))
            return 2
        log("工作台已起 → %s" % base)

        with httpx.Client(timeout=60.0, trust_env=False) as c:
            be = wait_backend(base)
            REPORT["backend"] = be
            if not be or not be.get("running"):
                log("ComfyUI 后端没在跑，先按 AGENTS.md 铁律 1 拉起它")
                return 2
            log("后端 running=%s version=%s models_ok=%s"
                % (be["running"], be.get("comfyui_version"), be.get("models_ok")))
            check("后端就绪且四个模型可见", be.get("models_ok"),
                  "missing=%s" % be.get("missing_models"))
            if not be.get("models_ok"):
                return 2

            # --- 参考图：清单 + 真上传 -----------------------------------
            info = c.get(base + "/api/inputs", timeout=30.0).json()
            REPORT["inputs_before"] = info
            log("input 落点：%s（%s）" % (info.get("root"), info.get("how")))
            check("input 落点已与后端候选表对账", info.get("verified"),
                  "files=%s" % info.get("files"))
            files = info.get("files") or []
            if not files:
                log("input/ 里一张图都没有，没法做 edit")
                return 2

            ref = a.ref
            if not ref:
                ref = max(files, key=lambda n: os.path.getsize(
                    os.path.join(info["root"], n)))
            a.ref = ref
            REPORT["ref"] = ref
            log("参考图：%s" % ref)

            # 上传探针：**真的**走一遍上传，然后用后端候选表核对
            src = a.upload_probe or os.path.join(info["root"], ref)
            up_name = "p4_probe_%d.png" % int(time.time())
            with open(src, "rb") as f:
                blob = f.read()
            r = c.post("%s/api/upload?filename=%s" % (base, up_name),
                       content=blob, timeout=120.0)
            # 失败的响应体里就是原因，**别丢** —— 上一次把它扔掉，
            # 结果是只知道 500、不知道 500 为什么
            try:
                up = r.json()
            except ValueError:
                up = {"raw": r.text[:500]}
            REPORT["upload"] = {"status": r.status_code, "resp": up}
            log("上传 %s（%.1f MB）→ HTTP %d"
                % (up_name, len(blob) / 1048576.0, r.status_code))
            err = str(up.get("error") or "")
            sandboxed = r.status_code == 500 and ("Permission denied" in err
                                                  or "写不了" in err)
            if sandboxed:
                skip_check("上传写入 ComfyUI 的 input/", 
                           "沙箱拒绝工作区解释器写 %%LOCALAPPDATA%%（见 AGENTS.md 铁律 2）"
                           "；run.bat 不经沙箱，能写。原因：%s" % err[:120])
            else:
                check("上传返回 201（新名字）", r.status_code == 201,
                      "status=%d %s" % (r.status_code, err[:200]))
                check("上传后后端候选表里能看到它", up.get("verified"),
                      str(up.get("warning") or up.get("how")))
                disk = os.path.join(info["root"], up_name)
                check("上传的字节真的落在后端读的目录里",
                      os.path.isfile(disk) and os.path.getsize(disk) == len(blob),
                      disk)
            after = c.get(base + "/api/inputs", timeout=30.0).json()
            REPORT["inputs_after"] = after
            if not sandboxed:
                check("上传后的清单里出现了新文件名",
                      up_name in (after.get("files") or []),
                      "files=%d" % len(after.get("files") or []))
            # 清单本身总是能读的（读 %LOCALAPPDATA% 不受限）——
            # 所以「后端能读到哪些文件」这条链路在沙箱下也验得了
            check("参考图清单与后端候选表逐名一致",
                  after.get("backend_files") is not None
                  and sorted(after["backend_files"]) == sorted(after.get("files") or []),
                  "backend=%s" % after.get("backend_files"))

            # --- 成本预警：贵的那档不带 confirm 必须被拦 -------------------
            # 刻意用「改写器 + 1 步」：它的理由是**显存**（+8.82 GiB），与秒数
            # 无关，所以门槛一定会触发；而万一门槛坏了，真的提交出去的也只是一次
            # 1 步的活儿。（2026-10-01 之前这里是 `mode="refine"`，现在那个开关
            # 叫 `rewrite`。）早先这里用 2K + 2 步，两个坑一起踩：2 步算出来才
            # 65 秒、本来就不该拦（**这是我判据写错**），而它真的提交之后还占住
            # GPU，把后面的 t2i 挤成 503。
            gate = {"mode": "t2i", "rewrite": True, "prompt": T2I_PROMPT,
                    "steps": 1, "seed": 1}
            rg = c.post(base + "/api/run", json=gate, timeout=30.0)
            try:
                gjson = rg.json()
            except ValueError:
                gjson = {}
            REPORT["gate"] = {"status": rg.status_code, "resp": gjson}
            check("贵的那档不带 confirm 被拦成 409", rg.status_code == 409,
                  "status=%d" % rg.status_code)
            check("被拦时给出了原因",
                  bool((gjson.get("confirm") or {}).get("text")),
                  str(gjson.get("error"))[:140])

            # --- 编辑模式的参考图预检 ------------------------------------
            bad = {"mode": "edit", "prompt": EDIT_PROMPT, "steps": 2,
                   "source_image": "definitely-not-here.png", "resolution": 1024,
                   "seed": 1}
            rb = c.post(base + "/api/build", json=bad, timeout=30.0).json()
            warns = rb.get("warnings") or []
            hit = [w for w in warns if "definitely-not-here.png" in w.get("text", "")]
            REPORT["edit_preflight"] = warns
            check("参考图不在 input/ 时提交前就说清", bool(hit) and
                  hit[0]["level"] == "error", str(hit[:1]))

            # --- 四模式各出一图 -----------------------------------------
            ref_pix = pngpix.pixels(os.path.join(info["root"], ref))
            REPORT["ref_pixels"] = {"w": ref_pix[0], "h": ref_pix[1],
                                    "ch": ref_pix[2]}
            log("参考图已解码：%dx%d ch=%d（后面判空转用）"
                % (ref_pix[0], ref_pix[1], ref_pix[2]))
            # 已解码的像素放**局部**变量，不进 REPORT ——
            # bytes 不能 JSON 序列化，中途异常时会把写报告也一起弄坏
            pix_by_mode = {}

            for mode in modes:
                # 每个模式**各自兜住异常**：一个模式的检查/日志写崩了，
                # 不该把后面还没跑的模式一起带走 —— 那是在烧 GPU 时间。
                # （第一次跑就是这么丢掉 refine 和 2K 的：edit 的日志行用了 %d
                #   去格式化一个 None。）
                try:
                    _one_mode(c, base, mode, a, ref, ref_pix, pix_by_mode)
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    REPORT.setdefault("mode_errors", {})[mode] = \
                        "%s: %s" % (type(exc).__name__, exc)
                    REPORT["checks"].append(
                        {"name": "%s 的检查没跑完" % mode, "ok": False,
                         "detail": "%s: %s" % (type(exc).__name__, exc)})
                    log("**%s 的检查中途抛异常**：%s: %s"
                        % (mode, type(exc).__name__, exc))

            # --- 逐节点耗时也该跟着有数据（P3 与 P4 不冲突）-------------
            tm = {}
            for mode, entry in REPORT["images"].items():
                if not entry.get("job_id"):
                    continue
                d = c.get("%s/api/jobs/%s/timings" % (base, entry["job_id"]),
                          timeout=30.0).json()
                # 覆盖率在**顶层** `coverage_pct`，不在 `summary` 里 ——
                # 第一版读错了键，四种模式全报 null，看起来像「探针没数据」；
                # 而实际上探针数据好得很（99.92%）。
                tm[mode] = {"available": d.get("available"),
                            "reason": d.get("reason"),
                            "coverage_pct": d.get("coverage_pct"),
                            "warm_start": d.get("warm_start"),
                            "sampling": d.get("sampling"),
                            "slowest": (d.get("slowest") or {}).get("class_type")}
            REPORT["timings"] = tm
            log("耗时面板：%s" % json.dumps(tm, ensure_ascii=False))

        graded = [x for x in REPORT["checks"] if x["ok"] is not None]
        skipped = [x for x in REPORT["checks"] if x["ok"] is None]
        ok = all(x["ok"] for x in graded)
        REPORT["ok"] = ok
        REPORT["seconds"] = round(time.time() - log.t0, 1)
        REPORT["server_log_tail"] = server_log[-80:]
        _write(a.report)
        log("")
        log("%d 项：%d 通过 / %d 失败 / %d 跳过（环境限制）"
            % (len(REPORT["checks"]),
               sum(1 for x in graded if x["ok"]),
               sum(1 for x in graded if not x["ok"]), len(skipped)))
        if skipped:
            log("跳过的项（**不算通过**，只是在本环境里验不了）：")
            for x in skipped:
                log("  · %s" % x["name"])
        if not ok:
            log("--- 工作台日志尾部（失败时最需要看的就是这段）---")
            for line in server_log[-30:]:
                log("  server| " + line)
        log("结论：%s（%.0f 秒）" % ("全部通过" if ok else "**有失败**",
                                    REPORT["seconds"]))
        log("报告：%s" % a.report)
        return 0 if ok else 1
    except Exception as exc:
        # 客户端这边的异常同样要配上下文 —— 不然只知道「超时了」，不知道服务端在干什么
        import traceback
        log("**冒烟自己抛异常**：%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        REPORT["exception"] = "%s: %s" % (type(exc).__name__, exc)
        REPORT["server_log_tail"] = server_log[-80:]
        _write(a.report)
        log("--- 工作台日志尾部 ---")
        for line in server_log[-40:]:
            log("  server| " + line)
        return 3
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not a.keep:
            shutil.rmtree(state, ignore_errors=True)


def _write(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(REPORT, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as exc:
        log("报告写不了：%s" % exc)


if __name__ == "__main__":
    sys.exit(main())
