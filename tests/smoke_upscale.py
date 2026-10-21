#!/usr/bin/env python3
"""活体冒烟：放大模式（`upscale`）走通一次真的。

它要证的不是「接口返回了 200」，而是四件事 —— 每一件都是假替身**证不了**的：

1. 手搭的那张 VOSR2 图**真的被 ComfyUI 收下并跑完**（替身只回一句 accepted）；
2. 输出**真的是放大后的尺寸**（源图 1024² ×2 → 2048²，靠读产物 PNG 的 IHDR 验，
   不靠接口自报）；
3. 画廊**认得出**它：`mode=upscale`、`recognized=True`、`scale`/`source_image`
   都在（之前它是「列出来了但描述不了」的那一类）；
4. **不用提示词**也能构建 —— 这条以前是硬性要求，放大把它打开了口子。

它自己起一个工作台（备用端口），但**要求 ComfyUI 已经在 8188 上跑着** ——
放大必须真的加载 VOSR2 那 6.5 GiB 权重，那件事这个脚本不负责拉起来。
后端没起就记 **SKIP**（不是 PASS，也不是 FAIL）。

    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe tests\\smoke_upscale.py

产物：`state/smoke_upscale_report.json`。约 30 秒（含一次真跑）。
"""
import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

COMFY = "http://127.0.0.1:8188"
REPORT = {"checks": [], "started_at": None, "mode": "upscale"}
SRC = "apple_1024.png"          # 已在 ComfyUI input/ 里的一张 1024² 图


def log(msg):
    print(msg, flush=True)


def check(name, ok, detail=""):
    REPORT["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    log("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  —— " + detail) if detail else ""))
    return ok


def skip(name, why):
    REPORT["checks"].append({"name": name, "ok": None, "detail": why})
    log("  [SKIP] %s —— %s" % (name, why))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url, timeout=15):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def get_json(url, timeout=30):
    """取 JSON。与 `get`（拿原始字节，取图用）分开 —— 混用会拿到 bytes
    然后在下游 `blob.get(...)` 上炸掉，而报错点离真正的原因很远。"""
    status, blob = get(url, timeout=timeout)
    return status, json.loads(blob.decode("utf-8"))


def post(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"error": body}


def png_dims(blob):
    if blob[:8] != b"\x89PNG\r\n\x1a\n" or blob[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", blob[16:24])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--scale", type=int, default=2)
    # 默认每次现取一个新 seed。**这不只是为了「跑得不一样」**：同一份工作流
    # 原样重提会被 ComfyUI 整图缓存命中（每个节点 cached=True、wall_ms 几十毫秒、
    # 连文件都不新写），于是这个脚本会**在一片 PASS 里什么也没验**。
    # 换 seed 会改 73% 的像素（实测），forced 真重算；下面还有一条判据兜着。
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "smoke_upscale_report.json"))
    a = ap.parse_args(argv)
    seed = a.seed if a.seed is not None else int(time.time()) % 2147483647
    REPORT["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    REPORT["seed"] = seed

    log("== 前置检查 ==")
    try:
        status, _ = get(COMFY + "/system_stats")
        backend_up = status == 200
    except Exception as exc:                                  # noqa: BLE001
        backend_up = False
        log("    后端没起：%s" % exc)
    if not backend_up:
        skip("放大的端到端", "ComfyUI 8188 没在跑 —— 放大要真加载 VOSR2 权重，"
                             "这个脚本不负责拉起它")
        _write(a.report)
        return 0
    check("ComfyUI 8188 在跑", True)

    port = a.port or free_port()
    state = tempfile.mkdtemp(prefix="qwen21-smoke-upscale-")
    env = dict(os.environ)
    env["QWEN21_STATE"] = state
    env["QWEN21_COMFY_PORT"] = "8188"
    proc = subprocess.Popen(
        [sys.executable, "-m", "workbench.server", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    base = "http://127.0.0.1:%d" % port
    server_log = []

    try:
        log("\n== 等工作台起来（pid %s, 端口 %d）==" % (proc.pid, port))
        ready = False
        for _ in range(60):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                if get(base + "/api/health", timeout=3)[0] == 200:
                    ready = True
                    break
            except Exception:                                  # noqa: BLE001
                continue
        if not check("工作台起来了", ready):
            _write(a.report)
            return 1

        log("\n== 1) 不带提示词构建 ==")
        body = {"mode": "upscale", "source_image": a.src,
                "scale": a.scale, "seed": seed}
        log("    seed = %d（现取的，为的是不撞上整图缓存）" % seed)
        st, built = post(base + "/api/build", body)
        check("没有提示词也能构建", st == 200, "http %s %s"
              % (st, built.get("error", "")))
        if st != 200:
            _write(a.report)
            return 1
        desc = built.get("describe", {})
        check("describe 认得放大", desc.get("mode") == "upscale",
              "mode=%r scale=%r steps=%r" % (desc.get("mode"), desc.get("scale"),
                                             desc.get("steps")))
        check("反解不猜尺寸", desc.get("width") is None and desc.get("height") is None,
              "width=%r height=%r" % (desc.get("width"), desc.get("height")))
        check("没有步数这个旋钮", desc.get("steps") is None)

        out = built.get("output_size") or {}
        check("从源图 IHDR 算出输出尺寸",
              out.get("width") == 1024 * a.scale and out.get("height") == 1024 * a.scale,
              json.dumps(out, ensure_ascii=False))
        parts = (built.get("estimate") or {}).get("parts", {})
        check("用的是放大的成本公式（upscale_s，不是 sampling_s）",
              "upscale_s" in parts and "sampling_s" not in parts,
              json.dumps(parts, ensure_ascii=False))
        errs = [w for w in built.get("warnings", []) if w.get("level") == "error"]
        check("插件与权重都就位（没有 error 级）", not errs,
              " / ".join(w["text"][:80] for w in errs))

        log("\n== 2) 真跑一次 ==")
        t0 = time.time()
        st, run = post(base + "/api/run", body)
        if not check("提交被接受", st == 200, "http %s %s"
                     % (st, run.get("error", ""))):
            _write(a.report)
            return 1
        pid = run["job_id"]
        final = None
        while time.time() - t0 < 300:
            time.sleep(1.5)
            try:
                _, job = get_json("%s/api/jobs/%s" % (base, pid))
            except Exception:                                  # noqa: BLE001
                continue
            if job.get("status") in ("done", "error", "cancelled"):
                final = job
                break
        check("跑完了（没有超时）", final is not None)
        if final is None:
            _write(a.report)
            return 1
        check("终态是 done", final.get("status") == "done",
              str(final.get("status")))
        wall = final.get("wall_ms")
        check("拿到了 wall_ms", bool(wall), "%s ms（墙钟 %.1f s）"
              % (wall, time.time() - t0))
        REPORT["wall_ms"] = wall
        REPORT["outputs"] = final.get("outputs")
        # **这条是防「一片 PASS 里什么也没验」的。** 整图缓存命中时每个节点
        # 都是 cached=True、wall_ms 只有几十毫秒、连文件都不新写 —— 上面那几条
        # 尺寸与画质的判据会统统通过，而实际上一次放大都没跑。实测真跑约 13 秒，
        # 命中约 0.04 秒，3 秒这条线把两者分得干干净净。
        check("这次是真的重算了（不是整图缓存命中）",
              wall is not None and wall >= 3000,
              "wall_ms=%s（缓存命中会是几十毫秒；真跑实测约 13 秒）" % wall)

        log("\n== 3) 产物真的是放大后的尺寸 ==")
        outs = final.get("outputs") or []
        if not outs:
            check("有产物", False, "outputs 是空的")
        else:
            im = outs[0]
            url = ("%s/api/image?filename=%s&subfolder=%s&type=%s"
                   % (base, urllib.parse.quote(im["filename"]),
                      urllib.parse.quote(im.get("subfolder", "") or ""),
                      im.get("type", "output")))
            _, blob = get(url, timeout=60)
            dims = png_dims(blob)
            check("产物的 IHDR 就是 ×%d 之后的大小" % a.scale,
                  dims == (1024 * a.scale, 1024 * a.scale), str(dims))
            REPORT["output_dims"] = list(dims) if dims else None
            REPORT["filename"] = im.get("filename")

        log("\n== 4) 画廊认得它 ==")
        _, gal = get_json(base + "/api/gallery?limit=500")
        items = gal.get("items") or gal.get("images") or []
        mine = [i for i in items if i.get("mode") == "upscale"]
        check("画廊里出现了 mode=upscale 的条目", bool(mine),
              "%d 条" % len(mine))
        rec = [i for i in mine if i.get("recognized")]
        check("它们是 recognized（不是「别人的图」）", len(rec) == len(mine),
              "%d/%d" % (len(rec), len(mine)))
        if rec:
            one = rec[-1]
            check("画廊报得出 scale / 源图 / seed",
                  one.get("scale") is not None and one.get("source_image")
                  and one.get("seed") is not None,
                  "scale=%s src=%s seed=%s" % (one.get("scale"),
                                               one.get("source_image"),
                                               one.get("seed")))
        counts = gal.get("counts") or {}
        check("计数里有 upscale 这一档", "upscale" in counts,
              json.dumps(counts, ensure_ascii=False))

    except Exception as exc:                                   # noqa: BLE001
        import traceback
        log("**冒烟自己抛异常**：%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        REPORT["exception"] = "%s: %s" % (type(exc).__name__, exc)
        _write(a.report)
        return 3
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            if proc.stdout is not None:
                server_log = proc.stdout.read().splitlines()[-40:]
        except Exception:                                      # noqa: BLE001
            pass
        shutil.rmtree(state, ignore_errors=True)

    fails = [c for c in REPORT["checks"] if c["ok"] is False]
    passed = [c for c in REPORT["checks"] if c["ok"] is True]
    log("\n== 小结 ==  PASS %d / FAIL %d" % (len(passed), len(fails)))
    if fails:
        REPORT["server_log_tail"] = server_log
        for c in fails:
            log("   FAIL: %s —— %s" % (c["name"], c["detail"]))
    _write(a.report)
    return 0 if not fails else 1


def _write(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(REPORT, f, ensure_ascii=False, indent=2)
            f.write("\n")
        log("\n报告：%s" % path)
    except OSError as exc:
        log("报告写不了：%s" % exc)


if __name__ == "__main__":
    sys.exit(main())
