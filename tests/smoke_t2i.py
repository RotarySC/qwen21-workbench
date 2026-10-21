#!/usr/bin/env python3
"""活体冒烟 —— 真起工作台、真提交一次出图、真等它跑完。

与其余测试的分工：那些用 `TestClient` + 内存替身，毫秒级、不需要任何后端；
这一个**必须**有 ComfyUI 在跑，约一分半，验的是「替身假装不出来的那部分」：

  判据 1  提交 → 1 秒内看到步进（这里量的是第一个事件到达的延迟）
  判据 2  跑的中途**另开一条 SSE**，快照里必须已经有这个在跑的任务
  判据 3  产物 PNG 能取回，且 tEXt 里的 prompt/seed/steps 与提交的一致
          （这一步顺带证明 `prompt_id` 这个键真的把 PNG 与运行记录串起来了）

用法：

    python tests/smoke_t2i.py                # 默认 1024²/20 步
    python tests/smoke_t2i.py --steps 25 --port 8199

退出码：0 通过 · 1 失败 · 2 后端没起（跳过，不算失败）
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

from workbench import pngmeta  # noqa: E402

# PowerShell 的默认控制台是 GBK，遇到 `²` 这类字符 print 会直接抛
# UnicodeEncodeError 把整个冒烟打断 —— 报错信息还跟真正的失败长得一样。
# 这里保持控制台自己的编码（中文在 PowerShell 里才显示得对），只把不能编码的
# 字符降级成 `?`。机器可读的结论另外落一份 JSON。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

PROMPT = ("Photorealistic studio photograph of a single fresh green apple on a "
          "pale marble surface, soft directional daylight from the upper left, "
          "visible skin speckles, shallow depth of field, no text, no watermark.")

LOG = []
REPORT = {}


def log(msg):
    line = "[%7.2fs] %s" % (time.time() - log.t0, msg)
    LOG.append(line)
    print(line, flush=True)
log.t0 = time.time()


class Sse(threading.Thread):
    """后台读一条 SSE 流，原因子原样记下来（带到达时刻）。"""

    def __init__(self, url, label):
        super().__init__(daemon=True)
        self.url = url
        self.label = label
        self.events = []          # (时刻, 类型, 负载)
        self.stop = threading.Event()
        self.error = None

    def run(self):
        try:
            with httpx.Client(timeout=None, trust_env=False) as c:
                with c.stream("GET", self.url) as r:
                    etype = None
                    for line in r.iter_lines():
                        if self.stop.is_set():
                            break
                        if line.startswith("event: "):
                            etype = line[7:].strip()
                        elif line.startswith("data: ") and etype:
                            try:
                                payload = json.loads(line[6:])
                            except ValueError:
                                payload = None
                            self.events.append((time.time(), etype, payload))
                            etype = None
        except Exception as exc:                       # 收尾时被掐断是正常的
            if not self.stop.is_set():
                self.error = "%s: %s" % (type(exc).__name__, exc)

    def wait_for(self, etype, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ts, t, payload in self.events:
                if t == etype:
                    return ts, payload
            time.sleep(0.05)
        return None, None

    def of(self, etype):
        return [e for e in self.events if e[1] == etype]


def wait_health(base, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/api/health", timeout=1.0, trust_env=False)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--comfy-port", type=int, default=8188)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--seed", type=int, default=20260929)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "smoke_report.json"))
    a = ap.parse_args(argv)

    base = "http://127.0.0.1:%d" % a.port
    env = dict(os.environ,
               QWEN21_PORT=str(a.port),
               QWEN21_COMFY_PORT=str(a.comfy_port),
               PYTHONIOENCODING="utf-8",
               # 后端地址是本机回环；NO_PROXY 里那个 `[::1]` 会让 httpx 构造失败
               NO_PROXY="127.0.0.1,localhost")

    log("起工作台 :%d（ComfyUI 指向 :%d）" % (a.port, a.comfy_port))
    proc = subprocess.Popen(
        [sys.executable, "-m", "workbench.server", "--port", str(a.port)],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")

    def drain():
        for line in proc.stdout:
            LOG.append("  server| " + line.rstrip())
    threading.Thread(target=drain, daemon=True).start()

    failures = []
    try:
        if not wait_health(base):
            log("✗ 工作台没起来")
            return 1

        with httpx.Client(base_url=base, timeout=60.0, trust_env=False) as c:
            be = c.get("/api/backend").json()
            log("后端 %s · 模型 %s · %s"
                % ("在跑" if be["running"] else "未运行",
                   "齐" if be["models_ok"] else "不全", be["hint"]))
            if not be["running"]:
                log("→ 跳过：ComfyUI 没起。这不是工作台的失败。")
                return 2
            if not be["models_ok"]:
                log("✗ 四个模型文件不全，先修铁律 1 的 yaml：%s"
                    % be["missing_models"])
                return 1
            log("start_command（判据 6 会原样给到页面）:")
            log("  " + be["start_command"]["display"].replace("\n", "\n  "))

            # 接 SSE 之后再提交，否则可能漏掉最早的几个事件
            sse = Sse(base + "/api/events", "primary")
            sse.start()
            sse.wait_for("snapshot", 5.0)

            body = {"mode": "t2i", "prompt": PROMPT, "steps": a.steps,
                    "width": a.width, "height": a.height, "seed": a.seed,
                    "cfg": 1.0, "sampler": "euler", "scheduler": "simple"}

            log("提交 T2I %d² / %d 步 / seed %d"
                % (a.width, a.steps, a.seed))
            t0 = time.time()
            run = c.post("/api/run", json=body).json()
            job_id = run["job_id"]
            REPORT["job_id"] = job_id
            REPORT["submitted_body"] = body
            log("job_id=%s  prompt_id=%s  估算 %ss"
                % (job_id, run["prompt_id"], run["estimate_s"]))
            if run["job_id"] != run["prompt_id"]:
                failures.append("job_id 与 prompt_id 不同值")

            # ---- 判据 1：1 秒内看到步进 ----
            first_ts, first = None, None
            deadline = time.time() + 10.0
            while time.time() < deadline and first is None:
                for ts, etype, payload in sse.events:
                    if etype in ("job_started", "node", "progress", "cached"):
                        first_ts, first = ts, (etype, payload)
                        break
                time.sleep(0.02)
            if first is None:
                failures.append("10 秒内没收到任何进度事件")
                log("✗ 判据 1：没有进度事件")
            else:
                lag = first_ts - t0
                log("判据 1 · 首个进度事件 %s 在提交后 %.3f 秒到达"
                    % (first[0], lag))
                if lag > 1.0:
                    failures.append("首个进度事件 %.3f 秒 > 1 秒" % lag)

            # ---- 判据 2：中途另开一条流，快照必须认得这个任务 ----
            time.sleep(2.0)
            mid = c.get("/api/jobs?limit=20").json()
            if job_id not in mid["active"]:
                failures.append("跑的中途 /api/jobs 不认为它在 active")
            sse2 = Sse(base + "/api/events", "second")
            sse2.start()
            _, snap = sse2.wait_for("snapshot", 5.0)
            if snap is None:
                failures.append("第二条 SSE 没拿到 snapshot")
            else:
                ids = [j["job_id"] for j in snap["jobs"]]
                log("判据 2 · 新连接的快照里有 %d 个任务，含本任务：%s"
                    % (len(ids), job_id in ids))
                if job_id not in ids:
                    failures.append("刷新后快照里找不到在跑的任务")
                elif snap["jobs"][ids.index(job_id)]["status"] not in ("running", "queued"):
                    failures.append("快照里本任务状态是 %s"
                                    % snap["jobs"][ids.index(job_id)]["status"])

            # ---- 等收尾 ----
            ts, done = sse.wait_for("job_done", a.timeout)
            if done is None:
                err = sse.of("job_error")
                failures.append("没等到 job_done（job_error=%s）" % (err[:1] or None))
                log("✗ 没跑完")
            else:
                log("完成：wall_ms=%s  outputs=%s"
                    % (done.get("wall_ms"),
                       [o["filename"] for o in done.get("outputs", [])]))

            # 轮询也必须认到终态（WS 丢了也一样）
            time.sleep(1.5)
            rec = c.get("/api/jobs/" + job_id).json()
            log("工作台记录：status=%s wall_ms=%s"
                % (rec["status"], rec.get("wall_ms")))
            if rec["status"] != "done":
                failures.append("任务记录状态是 %s，不是 done" % rec["status"])

            # ---- 判据 3：产物与元数据 ----
            outs = rec.get("outputs") or []
            if not outs:
                failures.append("记录里没有产物")
            else:
                img = c.get("/api/image", params={"job_id": job_id})
                blob = img.content
                log("取图 %s → %d 字节 %s"
                    % (outs[-1]["filename"], len(blob), img.headers.get("content-type")))
                if blob[:8] != b"\x89PNG\r\n\x1a\n":
                    failures.append("取回的不是 PNG")
                else:
                    wf = pngmeta.prompt_workflow(blob)
                    info = pngmeta.summarize(wf or {})
                    log("PNG 元数据：mode=%s steps=%s seed=%s %sx%s cfg=%s"
                        % (info.get("mode"), info.get("steps"), info.get("seed"),
                           info.get("width"), info.get("height"), info.get("cfg")))
                    for key, want in (("steps", a.steps), ("seed", a.seed),
                                      ("width", a.width), ("height", a.height)):
                        if info.get(key) != want:
                            failures.append("PNG 里的 %s=%r，期望 %r"
                                            % (key, info.get(key), want))
                    # 断边修复的证据：提示词必须真的在 on_false 上
                    if (wf or {}).get("459:474", {}).get("inputs", {}).get("on_false") != PROMPT:
                        failures.append("PNG 里的 on_false 不是我们的提示词（断边没修）")
                    if info.get("prompt") != PROMPT:
                        failures.append("PNG 里反解出的提示词与提交不符")

            # ---- runs.jsonl ----
            runs = os.path.join(REPO, "state", "runs.jsonl")
            if not os.path.exists(runs):
                failures.append("state/runs.jsonl 不存在")
            else:
                hits = [json.loads(l) for l in open(runs, encoding="utf-8")
                        if l.strip() and job_id in l]
                log("runs.jsonl 里 %d 条本任务记录" % len(hits))
                if not hits:
                    failures.append("runs.jsonl 里没有本任务")

            sse.stop.set(); sse2.stop.set()
            log("主 SSE 共 %d 个事件，类型：%s"
                % (len(sse.events),
                   sorted({e[1] for e in sse.events})))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    log("=" * 62)
    ok = not failures
    if failures:
        log("失败 %d 项：" % len(failures))
        for f in failures:
            log("  x " + f)
    else:
        log("全部通过：判据 1（1 秒内步进）、判据 2（刷新接得上）、判据 3（产物与元数据）")

    REPORT.update({"ok": ok, "failures": failures, "log": LOG,
                   "job_id": REPORT.get("job_id"),
                   "seconds": round(time.time() - log.t0, 1)})
    try:
        os.makedirs(os.path.dirname(a.report), exist_ok=True)
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(REPORT, f, ensure_ascii=False, indent=2)
        log("结论已写入 " + a.report)
    except OSError as exc:
        log("写不了结论文件：%s" % exc)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
