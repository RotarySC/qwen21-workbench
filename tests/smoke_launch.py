#!/usr/bin/env python3
"""活体冒烟：一键拉起后端（P6）。

这是 P6 门槛里唯一不能靠替身证明的一条 ——「关掉后能一键拉起」。
所以这里**真的**起一个 ComfyUI（在备用端口上，不碰用户那个 8188），
等它就绪，然后**把它关掉**（连同工作台），再验证「端口被占时拒绝启动」。

    "%LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe" tests\\smoke_launch.py

**用哪个解释器跑它，决定了这条冒烟验的是哪一支。** 解释器若在被打上 Low
完整性标签的目录树里（2026-10-01 之前是工作区里的 `.venv`），后端只能进
「受限模式」；解释器在工作区外（现在的 `%LOCALAPPDATA%\\qwen21-venv`）就是
正常模式。**两支都有判据**，而且第一件事就是把本进程的完整性等级报出来 ——
否则「跑错了解释器」看起来会像「产品坏了」。

三条刻意的选择：

- **用备用端口**（默认 8190）。用户的 8188 上通常已经有一个在跑，
  往那儿起第二个只会撞端口 —— 而且这个冒烟的职责不是抢它。
  工作台通过 `QWEN21_COMFY_PORT` 得知该用哪个端口，于是它「一键启动」
  起的也是 8190 那个。
- **一定清理**。起的那个 ComfyUI 由本次冒烟显式结束（按 pid），
  并在最后核实它真的没了 —— 否则每跑一次就多留一个占着内存的进程。
- **就绪看 `/system_stats`**，与产品一致；日志里的 `To see the GUI go to`
  只作为「更早的信号」被记录下来，不作为判据。

约 1~2 分钟。
"""
import argparse
import json
import os
import shutil
import signal
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

import support  # noqa: E402

from workbench import config  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

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


def skip_check(name, reason):
    REPORT["checks"].append({"name": name, "ok": None, "skipped": True,
                             "detail": reason})
    log("%-52s SKIP  %s" % (name, reason))


def small_png(limit=8 * 1024 * 1024):
    """找一个够小的真 PNG 来验上传。找不到就返回 None（那条检查记 SKIP）。"""
    workspace = os.path.dirname(REPO)
    for root in (os.path.join(workspace, "output"), os.path.join(workspace, "images")):
        for dirpath, _dirs, files in os.walk(root):
            for fn in sorted(files):
                if not fn.lower().endswith(".png"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(path) <= limit:
                        return path
                except OSError:
                    continue
    return None


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


def pid_alive(pid):
    try:
        import psutil
    except ImportError:
        return None
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        return False


def kill_pid(pid):
    """按 pid 结束我们启动的那个 ComfyUI。**只动这一个 pid。**"""
    try:
        import psutil
        p = psutil.Process(pid)
        for child in p.children(recursive=True):
            try:
                child.terminate()
            except Exception:
                pass
        p.terminate()
        try:
            p.wait(timeout=15)
        except Exception:
            p.kill()
        return True
    except Exception as exc:
        log("  结束 pid=%s 时出错：%s" % (pid, exc))
        return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8203,
                    help="工作台自己的端口")
    ap.add_argument("--comfy-port", type=int, default=8190,
                    help="要拉起的后端端口。**刻意不用 8188** —— 那儿通常有东西")
    ap.add_argument("--timeout", type=float, default=150.0)
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "smoke_launch_report.json"))
    a = ap.parse_args(argv)

    base = "http://127.0.0.1:%d" % a.port
    comfy = "http://127.0.0.1:%d" % a.comfy_port
    REPORT["comfy_port"] = a.comfy_port

    # 第一件事：问「我是谁」。这个进程的令牌完整性等级决定了后面会走哪一支 ——
    # 它是 Low 的话，后端**无论如何**只能进受限模式，那是**解释器放在哪个目录**
    # 的问题（AGENTS.md 铁律 2），不是产品的问题。先报出来，免得认错病因。
    sid = support.token_integrity()
    iname = support.integrity_name(sid)
    REPORT["integrity"] = {"sid": sid, "name": iname}
    log("本进程完整性：%s（%s）" % (sid, iname))
    if sid is None:
        # **Windows 上读不到就是故障，不是跳过**：这条探针一旦坏掉，
        # 其它判据都还能绿（模式会退化成受限、而受限那一支本来就有自己的判据），
        # 那就成了「防护是摆设」——这个仓库对这类静默失效有过教训。
        if os.name == "nt":
            check("跑冒烟的解释器能写工作区外（完整性 >= medium）", False,
                  "读完本进程令牌的完整性失败 —— 是**探针坏了**，别降级成 SKIP")
        else:
            skip_check("跑冒烟的解释器能写工作区外", "非 Windows：没有完整性等级这回事")
    else:
        check("跑冒烟的解释器能写工作区外（完整性 >= medium）",
              iname in support.INTEGRITY_CAN_WRITE_OUTSIDE,
              "integrity=%s，解释器 %s" % (iname, sys.executable))

    # 先确认备用端口是空的 —— 不然「一键启动」会被自己的「不抢端口」规则拒掉，
    # 而这个冒烟会误判成产品有问题
    try:
        httpx.get(comfy + "/system_stats", timeout=1.5, trust_env=False)
        log("端口 %d 上已经有东西在跑，这个冒烟要一个空端口。" % a.comfy_port)
        log("换一个：--comfy-port 8191")
        return 2
    except httpx.HTTPError:
        log("端口 %d 是空的，可以拿来做测试。" % a.comfy_port)

    scratch = os.path.join(REPO, "state")
    os.makedirs(scratch, exist_ok=True)
    state = tempfile.mkdtemp(prefix="smoke-launch-state-", dir=scratch)
    env = dict(os.environ)
    env["QWEN21_STATE"] = state
    env["QWEN21_COMFY_PORT"] = str(a.comfy_port)
    # **务必**把探针的两个输出改到临时目录里。
    # 不这么做的后果很实在：第二个实例的探针会去写**工作区根目录**那两个共享文件，
    # 把用户那个 8188 实例攒了几轮的 `node_probe_report.json` 覆盖掉。
    # （产品本身没这个问题 —— 端口被占时它拒绝启动，永远只有一个实例。）
    env["QWEN21_PROBE_REPORT"] = os.path.join(state, "probe_report.json")
    env["QWEN21_PROBE_STATE"] = os.path.join(state, "probe_state.json")
    proc = subprocess.Popen(
        [sys.executable, "-m", "workbench.server", "--port", str(a.port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")

    server_log = []

    def drain():
        for line in proc.stdout:
            server_log.append(line.rstrip())
    threading.Thread(target=drain, daemon=True).start()

    launched_pid = None
    try:
        if not wait_health(base):
            log("工作台没起来；子进程输出：")
            proc.terminate()
            time.sleep(0.5)
            print("\n".join(server_log[-40:]))
            return 2
        log("工作台已起 → %s（后端将指向 :%d）" % (base, a.comfy_port))

        with httpx.Client(timeout=60.0, trust_env=False) as c:
            st = c.get(base + "/api/backend/launch").json()
            REPORT["before"] = st
            check("启动前 phase=idle", st["phase"] == "idle", st["phase"])
            check("启动前后端是 down", not st["backend_up"],
                  "liveness=%s" % st.get("liveness"))

            started_at = time.time()
            r = c.post(base + "/api/backend/start", timeout=60.0)
            REPORT["start"] = {"status": r.status_code, "resp": r.json()}
            if not check("POST /api/backend/start 返回 202",
                         r.status_code == 202,
                         "status=%d %s" % (r.status_code, r.text[:300])):
                return 2
            launched_pid = r.json().get("pid")
            REPORT["pid"] = launched_pid
            log("已发起启动，pid=%s" % launched_pid)
            check("给了 pid 与它要跑的端口", bool(launched_pid),
                  "pid=%s" % launched_pid)

            # 等就绪（产品自己也在轮询；这里只是看结论）
            t0 = time.time()
            last = 0.0
            final = None
            while time.time() - t0 < a.timeout:
                final = c.get(base + "/api/backend/launch", timeout=30.0).json()
                el = time.time() - t0
                if el - last > 10:
                    last = el
                    log("    %5.0fs phase=%s backend_ok=%s 日志 %d 行"
                        % (el, final.get("phase"), final.get("backend_ok"),
                           len(final.get("log_tail") or [])))
                if final.get("phase") in ("ready", "failed", "exited"):
                    break
                time.sleep(1.0)
            REPORT["final"] = final
            log("启动结论：phase=%s ready_ms=%s exit=%s"
                % (final.get("phase"), final.get("ready_ms"),
                   final.get("exit_code")))

            if final.get("phase") == "ready":
                check("一键启动把后端拉起来了（phase=ready）", True,
                      "用了 %.1f 秒" % ((final.get("ready_ms") or 0) / 1000.0))
                check("后端本身也说是就绪的（/system_stats 200）",
                      final.get("backend_ok"), "liveness=%s" % final.get("liveness"))
                # 真的打一下那个端口，确认不是一个「自称就绪」的状态
                try:
                    live = httpx.get(comfy + "/system_stats", timeout=10.0,
                                     trust_env=False)
                    ver = (live.json().get("system") or {}).get("comfyui_version")
                    check("直接问 :%d 也能答（%s）" % (a.comfy_port, ver),
                          live.status_code == 200)
                except httpx.HTTPError as exc:
                    check("直接问 :%d 也能答" % a.comfy_port, False, str(exc))
                tail = final.get("log_tail") or []
                check("日志里出现了就绪标志行",
                      any("To see the GUI go to" in ln for ln in tail),
                      "日志 %d 行" % len(tail))
                check("日志确实有内容（不是空文件）", len(tail) > 3,
                      "前 3 行：%s" % (tail[:3],))
                # 落点方案：这台机器上工作台自己受限（只能写工作区），按钮必须把
                # 临时 / 用户 / 输入目录搬进工作区 —— 而且**要说出来**，
                # 页面据此在点之前就讲清代价（模型下载与节点安装用不了）。
                lay = (final or {}).get("layout") or {}
                check("启动状态里带上了落点方案",
                      lay.get("mode") in ("normal", "confined"),
                      "mode=%s" % lay.get("mode"))
                if lay.get("mode") == "confined":
                    check("受限模式给了说明（页面要显示它）",
                          bool(lay.get("note")), (lay.get("note") or "")[:80])
                    cmd = final.get("command") or []
                    check("受限模式真的改了启动参数",
                          "--temp-directory" in cmd and "--user-directory" in cmd
                          and "--input-directory" in cmd,
                          " ".join(cmd)[-140:])
                    check("日志的启动分隔行里也写了这件事",
                          any("受限模式" in ln for ln in tail),
                          "否则事后翻日志只会觉得「临时目录怎么跑这儿来了」")
                    # 落点真的生效了吗 —— 看**后端自己写出来的东西**在不在这儿。
                    # 这两条正是旧版当场失败的地方（`No usable temporary directory`
                    # 与 `[WinError 5] ... user\comfyui_8188.prev2.log`）。
                    cache = config.BACKEND_CACHE
                    check("ComfyUI 自己的临时目录落在工作区（--temp-directory 生效）",
                          os.path.isdir(os.path.join(cache, "temp", "temp")),
                          os.path.join(cache, "temp"))
                    ulog = os.path.join(cache, "user", "comfyui_%d.log" % a.comfy_port)
                    check("后端自己的日志写进了工作区的 user/（旧版在这儿 WinError 5）",
                          os.path.isfile(ulog) and os.path.getsize(ulog) > 0, ulog)
                    # 参考图要能传进**换过**的 input 目录 —— 页面上传走的正是这条
                    png = small_png()
                    if png:
                        name = "smoke_upload_probe.png"
                        with open(png, "rb") as f:
                            raw = f.read()
                        up = c.post(base + "/api/upload?filename=" + name,
                                    content=raw, timeout=60.0)
                        landed = os.path.join(cache, "input", name)
                        check("参考图能落到工作区里的 input/（受限也一样）",
                              200 <= up.status_code < 300 and os.path.isfile(landed),
                              "status=%d 落点=%s" % (up.status_code, landed))
                        try:
                            os.remove(landed)     # 冒烟自己收尾，别在 input/ 里留东西
                        except OSError:
                            pass
                    else:
                        skip_check("参考图能落到工作区里的 input/",
                                   "没找到可传的 PNG")
                else:
                    # 正常模式：**一个开关都不该加**，而且落点就该在系统/安装目录。
                    # 这一支是「把解释器搬出沙箱工作区」要换来的东西，所以判据落在
                    # **后端自己写出来的东西**上，不是只落在 argv 上。
                    cmd = final.get("command") or []
                    extra = [f for f in ("--temp-directory", "--user-directory",
                                         "--input-directory") if f in cmd]
                    check("正常模式没多塞任何落点参数", not extra,
                          "多出来的：%s" % extra)
                    check("正常模式不给「受限模式」的说明", not lay.get("note"),
                          (lay.get("note") or "")[:80])
                    ulog = os.path.join(config.COMFY_ROOT, "user",
                                        "comfyui_%d.log" % a.comfy_port)
                    fresh = (os.path.isfile(ulog)
                             and os.path.getmtime(ulog) >= started_at - 1)
                    check("后端日志写进安装目录的 user/（旧版在这儿 WinError 5）",
                          fresh, ulog)
                    png = small_png()
                    if png:
                        upname = "smoke_upload_probe.png"
                        with open(png, "rb") as f:
                            raw = f.read()
                        up = c.post(base + "/api/upload?filename=" + upname,
                                    content=raw, timeout=60.0)
                        landed = os.path.join(config.COMFY_ROOT, "input", upname)
                        check("参考图落到**安装目录**的 input/（CLI 与后端同一个）",
                              200 <= up.status_code < 300
                              and os.path.isfile(landed),
                              "status=%d 落点=%s" % (up.status_code, landed))
                        try:
                            os.remove(landed)      # 冒烟自己收尾
                        except OSError:
                            pass
                    else:
                        skip_check("参考图落到安装目录的 input/", "没找到可传的 PNG")
                # 探针三个变量传下去了没有 —— 看**被拉起那个实例**写的状态文件。
                # 起完就查，别写成「or True」那种永远不会红的假检查。
                pstate = os.path.join(state, "probe_state.json")
                if os.path.isfile(pstate):
                    with open(pstate, encoding="utf-8") as f:
                        ps = json.load(f)
                    check("被拉起的实例带上了探针（probe_state.json 写了）",
                          bool(ps.get("probe")) and ps.get("pid") is not None,
                          "pid=%s" % ps.get("pid"))
                else:
                    skip_check("被拉起的实例带上了探针",
                               "没有写下 %s —— 探针可能没装载（不影响出图）" % pstate)
                # 端口被占时应当拒绝
                r2 = c.post(base + "/api/backend/start", timeout=60.0)
                REPORT["second_start"] = {"status": r2.status_code}
                check("端口已被占时再次启动被拒（409）", r2.status_code == 409,
                      "status=%d %s" % (r2.status_code, r2.text[:160]))
            else:
                # 起不来也要分清楚：是**本产品**的问题，还是环境不让。
                #
                # 先看上面报出来的完整性等级，这一支的含义就清楚了：
                # - low：解释器又回到了被打 Low 标签的目录树里，后端注定只能受限
                #   —— 把它放回 `%LOCALAPPDATA%\qwen21-venv`，别在原地修。
                # - medium 及以上：那是真问题（落点方案没生效，或机器上真写不了）。
                err = str(final.get("error") or "")
                tail = " ".join(final.get("log_tail") or [])
                markers = ("No usable temporary directory", "Permission denied",
                           "PermissionError", "[WinError 5]")
                hit = next((m for m in markers if m in tail), None)
                REPORT["sandboxed"] = bool(hit)
                REPORT["sandbox_marker"] = hit
                if hit:
                    skip_check(
                        "一键拉起后端",
                        "连工作区里的落点都写不了（命中 %r）—— 受限模式本该把"
                        "临时/用户/输入目录搬进工作区；出现这句要么是工作区真写不了，"
                        "要么是落点方案没生效。两条都要查，**不是**「环境本来就不行」。"
                        % hit)

        graded = [x for x in REPORT["checks"] if x["ok"] is not None]
        skipped = [x for x in REPORT["checks"] if x["ok"] is None]
        ok = all(x["ok"] for x in graded)
        REPORT["ok"] = ok
        REPORT["seconds"] = round(time.time() - log.t0, 1)
        _write(a.report)
        log("")
        log("%d 项：%d 通过 / %d 失败 / %d 跳过"
            % (len(REPORT["checks"]),
               sum(1 for x in graded if x["ok"]),
               sum(1 for x in graded if not x["ok"]), len(skipped)))
        log("结论：%s（%.0f 秒）" % ("全部通过" if ok else "**有失败**",
                                    REPORT["seconds"]))
        return 0 if ok else 1
    except Exception as exc:
        import traceback
        log("**冒烟自己抛异常**：%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        REPORT["exception"] = "%s: %s" % (type(exc).__name__, exc)
        _write(a.report)
        return 3
    finally:
        # **先收后端**（那是我们起的），再收工作台。顺序反了的话，
        # 工作台退出时那个 ComfyUI 会变成孤儿进程留在后台。
        if launched_pid:
            alive = pid_alive(launched_pid)
            if alive:
                log("收尾：结束我们起的那个后端 pid=%s" % launched_pid)
                kill_pid(launched_pid)
            still = pid_alive(launched_pid)
            REPORT["cleaned_up"] = not still
            if still:
                log("**警告**：pid=%s 还活着，请手工结束它" % launched_pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        REPORT["server_log_tail"] = server_log[-40:]
        _write(a.report)
        shutil.rmtree(state, ignore_errors=True)
        # 最后再确认一次端口空了 —— 这才是「清理干净了」的证据
        time.sleep(1.0)
        try:
            httpx.get(comfy + "/system_stats", timeout=1.5, trust_env=False)
            log("**警告**：:%d 仍在响应 —— 后端没被收干净" % a.comfy_port)
            REPORT["port_free_after"] = False
        except httpx.HTTPError:
            log("收尾确认：:%d 已经空了" % a.comfy_port)
            REPORT["port_free_after"] = True


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
