#!/usr/bin/env python3
"""活体冒烟（P2）—— 画廊与提示词库。

不需要 GPU、不需要出图，约十几秒。验的是 **P2 的判据 3** 以及在单测里
没法诚实声明的那一半：**重启服务后东西还在**。

判据 3 拆成三条可量化的：
  a) 出图出现在画廊里，带 seed / 步数 / 模式
  b) 点一下能把参数填回表单 —— 即画廊项必须**齐备**表单需要的每个字段
  c) 重启服务后画廊仍在（提示词库同理）

做法：用**临时 state 目录**跑（不碰你真实的提示词库），但把真实
`runs.jsonl` 拷进去 —— 这样「与运行记录对账」那一段也能验到，
而且结果可复现。

退出码：0 通过 · 1 失败
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

# 控制台是 GBK，遇到 `²` 这种字符 print 会抛 UnicodeEncodeError 把冒烟打断
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

LOG = []
REPORT = {}
# 表单需要的每个字段 —— 「点一下能把参数填回表单」就是这串
FORM_FIELDS = ("mode", "steps", "seed", "width", "height")


def log(msg):
    line = "[%6.2fs] %s" % (time.time() - log.t0, msg)
    LOG.append(line)
    print(line, flush=True)
log.t0 = time.time()


class Server:
    """起一个工作台，能停能再起 —— 「重启后还在吗」要真的重启才算数。"""

    def __init__(self, port, state_dir, env_extra=None):
        self.port = port
        self.state_dir = state_dir
        self.env_extra = env_extra or {}
        self.proc = None
        self.tail = []

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self):
        env = dict(os.environ, QWEN21_PORT=str(self.port),
                   QWEN21_STATE=self.state_dir,
                   PYTHONIOENCODING="utf-8",
                   # 本机 NO_PROXY 里那个裸 `[::1]` 会让 httpx 构造失败
                   NO_PROXY="127.0.0.1,localhost")
        env.update(self.env_extra)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "workbench.server", "--port", str(self.port)],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")

        def drain():
            for line in self.proc.stdout:
                self.tail.append(line.rstrip())
        threading.Thread(target=drain, daemon=True).start()

        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                if httpx.get(self.base + "/api/health", timeout=1.0,
                             trust_env=False).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        return False

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def get(self, path, **kw):
        return httpx.get(self.base + path, timeout=60.0, trust_env=False, **kw)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8198)
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "smoke_gallery.json"))
    a = ap.parse_args(argv)

    failures = []
    tmp = tempfile.mkdtemp(prefix="qwen21-smoke-gal-")

    # 用真实 runs.jsonl 的副本 —— 让「与运行记录对账」也能验，且可复现
    real_runs = os.path.join(REPO, "state", "runs.jsonl")
    if os.path.exists(real_runs):
        shutil.copy2(real_runs, os.path.join(tmp, "runs.jsonl"))
        log("已把真实 runs.jsonl 拷进临时 state（%d 字节）"
            % os.path.getsize(real_runs))
    else:
        log("注意：真实 runs.jsonl 不存在，这次验不到「对账」那一段")

    srv = Server(a.port, tmp)
    if not srv.start():
        log("x 工作台没起来")
        return 1

    try:
        with httpx.Client(base_url=srv.base, timeout=60.0, trust_env=False) as c:
            # ---- 1) 画廊有东西，且元数据齐全 ----
            gal = c.get("/api/gallery?limit=200").json()
            log("画廊：扫到 %d 张，识别为本链路 %d 张"
                % (gal["scanned"], gal["counts"]["recognized"]))
            log("  输出目录：")
            for r in gal["roots"]:
                log("    [%s] %s  %s" % ("有" if r["exists"] else "无",
                                         r["label"], r["path"]))
            if not gal["scanned"]:
                failures.append("画廊什么都没扫到")
            if not gal["counts"]["recognized"]:
                failures.append("没有一张被识别为本链路")

            recognized = [i for i in gal["items"] if i["recognized"]]
            if recognized:
                item = recognized[0]
                log("判据 3a · 最新一张：%s mode=%s steps=%s seed=%s %sx%s"
                    % (item["filename"], item["mode"], item["steps"],
                       item["seed"], item["width"], item["height"]))
                for field in ("mode", "steps", "seed"):
                    if item.get(field) is None:
                        failures.append("画廊项缺 %s（判据 3 要求带 seed/步数/模式）"
                                        % field)
                missing = [f for f in FORM_FIELDS if item.get(f) is None]
                log("判据 3b · 表单字段齐备：%s"
                    % ("是" if not missing else "缺 " + ",".join(missing)))
                if missing:
                    failures.append("画廊项填不回表单，缺 %s" % missing)
                if not (item.get("prompt") or "").strip():
                    failures.append("画廊项没有提示词文本")
                # 这里原本有一句 `if item.get("is_2k") or item.get("mode") == "2k": pass`
                # —— 空分支。`2k` 那个模式 2026-10-01 撤销了，顺手清掉。

            # ---- 2) 图片能从磁盘取回 ----
            if recognized:
                item = recognized[0]
                r = c.get("/api/gallery/image",
                          params={"root": item["root"], "name": item["filename"]})
                log("取图 %s → %d 字节 %s"
                    % (item["filename"], len(r.content),
                       r.headers.get("content-type")))
                if r.status_code != 200 or r.content[:8] != b"\x89PNG\r\n\x1a\n":
                    failures.append("画廊取图失败（HTTP %d）" % r.status_code)

            # ---- 3) 与运行记录对账 ----
            with_timings = [i for i in gal["items"] if i.get("has_timings")]
            conflicts = [i for i in gal["items"] if i.get("run_match") == "conflict"]
            log("对账：%d 张有耗时，%d 张标了冲突"
                % (len(with_timings), len(conflicts)))
            if os.path.exists(real_runs) and not with_timings:
                failures.append("有 runs.jsonl 却一张都对不上账")
            for it in with_timings:
                if not it.get("run", {}).get("wall_ms"):
                    failures.append("%s 标了 has_timings 却没有 wall_ms"
                                    % it["filename"])
                break
            for it in conflicts:
                if it.get("run") is not None:
                    failures.append("%s 标了冲突却还是挂了 run" % it["filename"])
                break

            # ---- 4) 非本链路的图被标出来，而不是冒充 ----
            foreign = [i for i in gal["items"] if not i["recognized"]]
            if foreign:
                bad = [i["filename"] for i in foreign if i.get("mode") is not None]
                log("非本链路 %d 张（%s）" % (len(foreign),
                                             ", ".join(i["filename"] for i in foreign[:3])))
                if bad:
                    failures.append("非本链路的图被标了 mode：%s" % bad)

            # ---- 5) 提示词库：写 → 重启 → 还在 ----
            title = "冒烟测试_%d" % int(time.time())
            created = c.post("/api/prompts",
                             json={"text": "smoke test prompt",
                                   "title": title, "tags": ["smoke"]}).json()
            pid = created["id"]
            log("提示词库：已建 id=%s" % pid)
            before = c.get("/api/gallery").json()
            before_ids = [i["id"] for i in before["items"]]

        # ---- 重启 ----
        log("重启工作台……")
        srv.stop()
        time.sleep(0.5)
        if not srv.start():
            failures.append("重启后工作台没起来")
            raise SystemExit(1)

        with httpx.Client(base_url=srv.base, timeout=60.0, trust_env=False) as c:
            rows = c.get("/api/prompts").json()["prompts"]
            still = [r for r in rows if r["id"] == pid]
            log("判据 3c · 重启后提示词库有 %d 条，本条的还在：%s"
                % (len(rows), bool(still)))
            if not still:
                failures.append("重启后提示词丢了")
            elif still[0]["tags"] != ["smoke"]:
                failures.append("重启后标签丢了")

            after = c.get("/api/gallery").json()
            after_ids = [i["id"] for i in after["items"]]
            log("判据 3c · 重启后画廊 %d 张，与重启前一致：%s"
                % (after["scanned"], before_ids == after_ids))
            if before_ids != after_ids:
                failures.append("重启后画廊列表变了（%d → %d）"
                                % (len(before_ids), len(after_ids)))

            c.delete("/api/prompts/" + pid)
            left = c.get("/api/prompts").json()["total"]
            log("已清理测试提示词，剩余 %d 条" % left)
            if left != 0:
                failures.append("清理后还剩 %d 条（临时 state 目录本该只有这一条）" % left)
    finally:
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    log("=" * 62)
    ok = not failures
    if failures:
        log("失败 %d 项：" % len(failures))
        for f in failures:
            log("  x " + f)
    else:
        log("全部通过：判据 3（画廊带参数、填得回表单、重启后仍在）")

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


if __name__ == "__main__":
    sys.exit(main())
