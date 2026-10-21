#!/usr/bin/env python3
"""活体冒烟：3 个 seed 扫描（判据 5）。

判据 5 要的是「3 个 seed 扫描并排出图，各带 `wall_ms` 与 s/步」。所以这里要证的不是
「接口返回了 3 条记录」，而是四件事：

1. 三格**真的排了三个不同的 seed**（记录对了但提交的图同一个 seed，
   会得到三张一模一样的图，而页面显示成一次成功的扫描）；
2. 三张图**逐像素不同** —— 这才是「seed 真的生效了」的证据（见 AGENTS.md「空转」）；
3. 每格都有 `wall_ms` 与 s/步，而且 s/步**带来源**（探针读数 vs wall÷步数，
   后者含加载与解码、偏大，两个数不是同一个量）；
4. 探针在线时用的是**探针读数**，不是退化值。

    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe tests\\smoke_sweep.py --steps 20

产物：`state/smoke_sweep_report.json`。约 3~4 分钟（3 × 20 步）。
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

# PowerShell 的控制台是 GBK，遇到 `²` 这类字符 print 会直接抛 UnicodeEncodeError
# 把整个冒烟打断 —— 而报错信息还跟真正的失败长得一样。只把不能编码的降级成 `?`。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

PROMPT = ("Photorealistic studio photograph of a single fresh green apple on a "
          "pale marble surface, soft directional daylight from the upper left, "
          "visible skin speckles, shallow depth of field, no text, no watermark.")

SEEDS = [20260931, 20260932, 20260933]

LOG = []
REPORT = {"checks": []}


def log(msg):
    line = "[%7.2fs] %s" % (time.time() - log.t0, msg)
    LOG.append(line)
    print(line, flush=True)
log.t0 = time.time()


def check(name, ok, detail=""):
    REPORT["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    log("%-52s %s%s" % (name, "PASS" if ok else "**FAIL**",
                        ("  " + detail) if detail else ""))
    return ok


def wait_health(base, timeout=40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base + "/api/health", timeout=1.0,
                         trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def find_output(roots, filename):
    for root in roots or []:
        path = root.get("path")
        if path and os.path.isfile(os.path.join(path, filename)):
            return os.path.join(path, filename)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8202)
    ap.add_argument("--comfy-port", type=int, default=8188)
    ap.add_argument("--mode", default="t2i")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--limit", type=int, default=300000)
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "smoke_sweep_report.json"))
    a = ap.parse_args(argv)

    seeds = [int(x) for x in a.seeds.split(",") if x.strip()]
    base = "http://127.0.0.1:%d" % a.port
    REPORT["seeds"] = seeds
    REPORT["steps"] = a.steps

    scratch = os.path.join(REPO, "state")
    os.makedirs(scratch, exist_ok=True)
    state = tempfile.mkdtemp(prefix="smoke-sweep-state-", dir=scratch)
    env = dict(os.environ)
    env["QWEN21_STATE"] = state
    env["QWEN21_COMFY_PORT"] = str(a.comfy_port)
    proc = subprocess.Popen(
        [sys.executable, "-m", "workbench.server", "--port", str(a.port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")

    # **必须**有人一直读这个 pipe：uvicorn 的日志几十秒就能把 64 KB 填满，
    # 之后服务端阻塞在 write 上，整个工作台看起来像死了（客户端只看到 read timeout）。
    server_log = []

    def drain():
        for line in proc.stdout:
            server_log.append(line.rstrip())
    threading.Thread(target=drain, daemon=True).start()

    try:
        if not wait_health(base):
            log("工作台没起来；子进程输出：")
            proc.terminate()
            time.sleep(0.5)
            print("\n".join(server_log[-40:]))
            return 2
        log("工作台已起 → %s" % base)

        with httpx.Client(timeout=60.0, trust_env=False) as c:
            be = c.get(base + "/api/backend", timeout=10.0).json()
            REPORT["backend"] = {k: be.get(k) for k in
                                 ("running", "liveness", "comfyui_version",
                                  "models_ok")}
            REPORT["probe"] = be.get("probe", {}).get("alive")
            if not check("后端就绪且四个模型可见", be.get("models_ok"),
                         "running=%s models_ok=%s" % (be.get("running"),
                                                      be.get("models_ok"))):
                return 2
            probe_on = bool(be.get("probe", {}).get("alive"))
            log("探针在线：%s（决定 s/步 的来源）" % probe_on)

            body = {"mode": a.mode, "prompt": PROMPT, "steps": a.steps,
                    "width": a.width, "height": a.height, "cfg": 1.0,
                    "grid": {"seed": seeds}}
            t0 = time.time()
            r = c.post(base + "/api/sweep", json=body, timeout=120.0)
            REPORT["submit"] = {"status": r.status_code, "resp": r.json()}
            if not check("扫描被接受（201）", r.status_code == 201,
                         "status=%d %s" % (r.status_code, r.text[:300])):
                return 2
            data = r.json()
            sweep_id = data["sweep_id"]
            check("排了 %d 张" % len(seeds), data["count"] == len(seeds),
                  "count=%s" % data["count"])

            # 等所有格子收尾
            last = 0.0
            while time.time() - t0 < a.timeout:
                rep = c.get("%s/api/sweep/%s" % (base, sweep_id),
                            timeout=30.0).json()
                el = time.time() - t0
                if el - last > 15:
                    last = el
                    log("    %5.0fs 完成 %s/%s"
                        % (el, rep.get("finished"), rep.get("count")))
                if rep.get("finished") >= rep.get("count"):
                    break
                time.sleep(1.5)
            else:
                log("超时 %s 秒仍未全部收尾" % a.timeout)

            rep = c.get("%s/api/sweep/%s" % (base, sweep_id),
                        timeout=30.0).json()
            REPORT["report"] = rep
            check("三格全部跑完", rep.get("finished") == len(seeds),
                  "finished=%s/%s" % (rep.get("finished"), rep.get("count")))
            check("全部 done（没有 error/cancelled）", rep.get("done") == len(seeds),
                  "done=%s failed=%s cancelled=%s"
                  % (rep.get("done"), rep.get("failed"),
                     rep.get("cancelled")))

            cells = sorted(rep.get("cells") or [],
                           key=lambda c: c.get("cell_index") or 0)

            # 1) 三格真的是三个不同的 seed
            got_seeds = [c["cell"]["seed"] for c in cells]
            check("三格的 seed 就是给的那三个", got_seeds == seeds,
                  "%s" % got_seeds)

            # 2) 每格都有 wall_ms 与 s/步，且带来源
            walls = [c.get("wall_ms") for c in cells]
            check("每格都有 wall_ms", all(w is not None for w in walls),
                  "%s" % [None if w is None else round(w) for w in walls])
            sources = [(c.get("per_step") or {}).get("source") for c in cells]
            check("每格的 s/步 都带了来源",
                  all(s in ("probe", "wall_over_steps") for s in sources),
                  "%s" % sources)
            if probe_on:
                check("探针在线时用的是探针读数（不是退化的 wall÷步数）",
                      all(s == "probe" for s in sources), "%s" % sources)
                steps_ok = all((c.get("per_step") or {}).get("steps") == a.steps
                               for c in cells)
                check("探针读数标了正确的步数", steps_ok,
                      "%s" % [(c.get("per_step") or {}).get("steps")
                              for c in cells])

            # 3) 产物落盘 + 逐像素不同（判据 5 的实质）
            roots = (be.get("output_roots") or [])
            pix = []
            for cell in cells:
                outs = cell.get("outputs") or []
                if not outs:
                    check("seed=%s 有产物" % cell["cell"]["seed"], False, "无 outputs")
                    continue
                path = find_output(roots, outs[0]["filename"])
                if not check("seed=%s 产物落盘" % cell["cell"]["seed"], bool(path),
                             outs[0]["filename"] if path else "磁盘上找不到"):
                    continue
                p = pngpix.pixels(path)
                pix.append((cell["cell"]["seed"], path, p))
                log("    seed=%-12s %s  %dx%d  wall=%s ms  %s s/步"
                    % (cell["cell"]["seed"], os.path.basename(path), p[0], p[1],
                       round(cell.get("wall_ms") or 0),
                       round((cell.get("per_step") or {}).get("ms") or 0, 1)))

            # 逐对比较：任意两张都不能逐像素相同
            pairs_done = 0
            for i in range(len(pix)):
                for j in range(i + 1, len(pix)):
                    sa, pa, xa = pix[i]
                    sb, pb, xb = pix[j]
                    try:
                        st = pngpix.diff_of_pixels(xa, xb, limit=a.limit)
                    except pngpix.PngError as exc:
                        check("seed %s 与 %s 尺寸一致" % (sa, sb), False, str(exc))
                        continue
                    pairs_done += 1
                    check("seed %s 与 %s 逐像素不同（seed 真的生效）" % (sa, sb),
                          not st["identical"],
                          "changed=%.2f%%" % st["changed_pct"])
            REPORT["pairs"] = pairs_done
            check("三张两两都比过了（3 对）", pairs_done == 3,
                  "pairs=%d" % pairs_done)

            # 4) 总耗时 = 各格之和（不能凭空多出来）
            total = rep.get("wall_ms_total")
            s = sum(w for w in walls if w is not None)
            check("总耗时就是各格之和", total == s, "total=%s sum=%s" % (total, s))

        graded = [x for x in REPORT["checks"]]
        ok = all(x["ok"] for x in graded)
        REPORT["ok"] = ok
        REPORT["seconds"] = round(time.time() - log.t0, 1)
        REPORT["server_log_tail"] = server_log[-60:]
        _write(a.report)
        log("")
        log("%d 项：%d 通过 / %d 失败" % (len(graded),
                                       sum(1 for x in graded if x["ok"]),
                                       sum(1 for x in graded if not x["ok"])))
        if not ok:
            log("--- 工作台日志尾部 ---")
            for line in server_log[-25:]:
                log("  server| " + line)
        log("结论：%s（%.0f 秒）" % ("全部通过" if ok else "**有失败**",
                                    REPORT["seconds"]))
        log("报告：%s" % a.report)
        return 0 if ok else 1
    except Exception as exc:
        import traceback
        log("**冒烟自己抛异常**：%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        REPORT["exception"] = "%s: %s" % (type(exc).__name__, exc)
        REPORT["server_log_tail"] = server_log[-60:]
        _write(a.report)
        return 3
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
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
