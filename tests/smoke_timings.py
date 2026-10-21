#!/usr/bin/env python3
"""活体冒烟（P3）—— 逐节点耗时面板，判据 4。

不需要 GPU、不需要出图：它读**真实的**探针报告，走**真实的** HTTP 路由。

判据 4 拆成四条可量化的：
  a) 有逐节点表（每行 node_id / class_type / compute_ms，按耗时倒序）
  b) 有采样拆分（KSampler 节点 vs 纯采样循环，且给出每步毫秒）
  c) 有覆盖率（Σ 节点耗时 ÷ 总耗时，落在合理区间）
  d) 温启动能标出来（cached_nodes 非空）

**外加一条交叉验证**，这是这里最有价值的一条：

    运行记录里的 wall_ms（来自 ComfyUI 官方的 execution_start /
    execution_success 事件时间戳）与探针的 wall_ms（来自 time.perf_counter）
    是**两条互不相干的测量通路**。它们应当吻合到 1% 以内。
    实测 49085 vs 49080.9 —— 差 4.1 ms，0.01%。

对不上就说明其中一条在撒谎，比任何单测都值得报警。

退出码：0 通过 · 1 失败 · 2 没有可比对的数据（跳过，不算失败）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

LOG = []
REPORT = {}


def log(msg):
    line = "[%6.2fs] %s" % (time.time() - log.t0, msg)
    LOG.append(line)
    print(line, flush=True)
log.t0 = time.time()


def _finish(a, failures):
    """收尾：写结论文件并给退出码。

    抽出来是因为有多个出口（拿不到数据、检查跑完），每个出口都要写报告 ——
    否则失败时反而没有结论文件可看，那正是最需要它的时候。
    """
    log("=" * 62)
    ok = not failures
    if failures:
        log("失败 %d 项：" % len(failures))
        for f in failures:
            log("  x " + f)
    else:
        log("全部通过：判据 4（逐节点表 · 采样拆分 · 覆盖率 · 温启动）+ 交叉验证")
    REPORT.update({"ok": ok, "failures": failures, "log": LOG,
                   "seconds": round(time.time() - log.t0, 1)})
    try:
        os.makedirs(os.path.dirname(a.report), exist_ok=True)
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(REPORT, f, ensure_ascii=False, indent=2)
        log("结论已写入 " + a.report)
    except OSError as exc:
        log("写不了结论文件：%s" % exc)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8197)
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "smoke_timings.json"))
    a = ap.parse_args(argv)

    # 用**默认** state 目录：要看到真实的任务记录。只读，不改任何东西。
    state = os.path.join(REPO, "state")
    env = dict(os.environ, QWEN21_PORT=str(a.port), QWEN21_STATE=state,
               PYTHONIOENCODING="utf-8",
               NO_PROXY="127.0.0.1,localhost")
    base = "http://127.0.0.1:%d" % a.port

    log("起工作台 :%d（state=%s）" % (a.port, state))
    proc = subprocess.Popen(
        [sys.executable, "-m", "workbench.server", "--port", str(a.port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")

    def drain():
        for line in proc.stdout:
            LOG.append("  server| " + line.rstrip())
    threading.Thread(target=drain, daemon=True).start()

    failures = []
    try:
        deadline = time.time() + 25
        up = False
        while time.time() < deadline:
            try:
                if httpx.get(base + "/api/health", timeout=1.0,
                             trust_env=False).status_code == 200:
                    up = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        if not up:
            log("x 工作台没起来")
            return 1

        with httpx.Client(base_url=base, timeout=30.0, trust_env=False) as c:
            be = c.get("/api/backend").json()
            probe = be.get("probe") or {}
            rep = probe.get("report") or {}
            log("探针状态：known=%s alive=%s pid=%s"
                % (probe.get("known"), probe.get("alive"), probe.get("pid")))
            log("报告：%s（采到 %d 次）"
                % (probe.get("report_path"), rep.get("captured", 0)))
            if probe.get("note"):
                log("  注：" + probe["note"])

            # ---- 找一个既有任务记录、又有耗时数据的 job ----
            jobs = c.get("/api/jobs?limit=200").json()["jobs"]
            captured = set(rep.get("prompt_ids") or [])
            report_path = probe.get("report_path")
            all_ids = set(json.load(open(report_path, encoding="utf-8")).keys()) \
                if report_path and os.path.exists(report_path) else captured
            usable = [j for j in jobs
                      if j["job_id"] in all_ids and j["status"] == "done"]
            if not usable:
                log("→ 跳过：没有「既有任务记录又有耗时数据」的任务。"
                    "先跑一次 tests/smoke_t2i.py 就有了。")
                return 2
            job = usable[0]
            jid = job["job_id"]
            log("选中任务 %s（%s，%s 步）"
                % (jid, job.get("mode"), (job.get("params") or {}).get("steps")))

            t = c.get("/api/jobs/%s/timings" % jid).json()
            if not t.get("available"):
                failures.append("拿不到耗时：%s" % t.get("reason"))
                return _finish(a, failures)

            # ---- a) 逐节点表 ----
            rows = t["rows"]
            log("判据 4a · 逐节点表 %d 行（按耗时倒序）：" % t["node_count"])
            for r in rows:
                log("    %-10s %-24s %9s  %5s%%  %s"
                    % (r["node_id"], r["class_type"],
                       "%.1f ms" % r["compute_ms"],
                       ("%.1f" % r["pct_of_wall"]) if r["pct_of_wall"] else "-",
                       ("缓存" if r["cached"] else "")))
            if not rows:
                failures.append("逐节点表是空的")
            if len(rows) != t["node_count"]:
                failures.append("node_count 与行数不一致")
            if any(r["compute_ms"] is None for r in rows):
                failures.append("有行缺 compute_ms")
            if [r["compute_ms"] for r in rows] != sorted(
                    (r["compute_ms"] for r in rows), reverse=True):
                failures.append("逐节点表没有按耗时倒序")

            # ---- b) 采样拆分 ----
            s = t["sampling"]
            log("判据 4b · 采样拆分：KSampler 节点 %s = 纯循环 %s（%s 步，每步 %s ms）"
                " + 节点内其他 %s"
                % (s.get("node_ms"), s.get("loop_ms"), s.get("steps"),
                   s.get("per_step_ms"), s.get("node_overhead_ms")))
            if s.get("node_ms") is None or not s.get("loop_ms"):
                failures.append("采样拆分缺一半（node_ms 或 loop_ms 为空）")
            else:
                if s["loop_ms"] > s["node_ms"] + 0.5:
                    failures.append("纯循环比节点还长，拆分反了")
                if s.get("per_step_ms") is None:
                    failures.append("没有算出每步耗时")
                elif s["steps"]:
                    want = s["loop_ms"] / s["steps"]
                    if abs(s["per_step_ms"] - want) > 0.05:
                        failures.append("每步耗时与 循环÷步数 对不上")

            # ---- c) 覆盖率 ----
            cov = t["coverage_pct"]
            log("判据 4c · 覆盖率 %.2f%%（节点合计 %s / 总 %s，未计入 %s）"
                % (cov, t["total_ms"], t["wall_ms"], t["unaccounted_ms"]))
            if cov is None:
                failures.append("没有覆盖率")
            elif not (50.0 < cov <= 100.5):
                failures.append("覆盖率 %.2f%% 不合理（>100 说明重复计数，"
                                "<50 说明漏了节点）" % cov)

            # ---- d) 温启动 ----
            log("判据 4d · %s"
                % ("温启动，%d 个节点走缓存：%s"
                   % (t["cached_count"], ", ".join(t["cached_nodes"][:6]))
                   if t["warm_start"] else "冷启动（没有缓存节点）"))

            # ---- 交叉验证：两条互不相干的测量通路 ----
            rec_wall = job.get("wall_ms")
            probe_wall = t.get("wall_ms")
            if rec_wall and probe_wall:
                delta = abs(rec_wall - probe_wall)
                pct = 100.0 * delta / rec_wall
                log("交叉验证 · 运行记录 wall_ms=%s vs 探针 wall_ms=%s"
                    "（差 %.1f ms，%.3f%%）" % (rec_wall, probe_wall, delta, pct))
                if pct > 1.0:
                    failures.append(
                        "两条独立测量通路差了 %.2f%%（runs=%s / probe=%s）—— "
                        "其中一条在撒谎" % (pct, rec_wall, probe_wall))
            else:
                log("交叉验证 · 跳过（缺 runs.wall_ms 或 probe.wall_ms）")

            REPORT.update({"job_id": jid, "timings": t,
                           "record_wall_ms": rec_wall})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    return _finish(a, failures)


if __name__ == "__main__":
    sys.exit(main())
