#!/usr/bin/env python3
"""参数扫描：一份底图，逐格只改一个旋钮，量速度与画质。

**这是「活体」工具** —— 需要一个真在跑的后端（默认 `127.0.0.1:8188`），
而且要带探针（`COMFY_NODE_PROBE=1`）：速度那栏取的是探针的
`sampling.total_ms`，**只包住纯去噪循环**，不是墙钟。墙钟含加载/编码/解码，
拿它比加速是没有意义的（这也是 `AGENTS.md`「别只看节点」那节讲的事）。

用法：

    # 要用工作台那个解释器（httpx 在那儿，见 README「解释器在哪」）
    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe sweep_tool.py \\
        --base <某张真实产物.png> \\
        --preset te_speed \\
        --log <后端 stderr 日志> \\
        --report sweep.json --analyze

它把三件事一起做掉，缺一条这轮的结论就立不住：

  · **底图从真实产物里取**（`wfprobe.reference_graph`），不是照文档重拼；
  · **每格都记「插件自报的生效值」并与「我要的值」对账**
    （`wfprobe.effective_mismatch`）—— 实测 `reuse_threshold=0.0` 会被
    改写成 0.018，正是靠这条才发现；
  · **两套像素口径都给**（`wfprobe.diff_stats`）—— `byte_*` 与 `pixel_*`
    不是同义词，混用会让整张表悄悄换量纲。

`--log` 是可选的，但不给就丢掉了**机制证据**：那一轮能说清「速度来自
attention 而不是步预测」，靠的就是日志里那行
`predicted 2/20 steps ... full_steps=18`。
"""
import argparse
import copy
import json
import os
import re
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx                     # noqa: E402
import wfprobe                   # noqa: E402
from workbench import config     # noqa: E402

HOST = "http://127.0.0.1:8188"
KSAMPLER = wfprobe.KSAMPLER


# ---------------------------------------------------------------------------
# 格子
# ---------------------------------------------------------------------------
class Cell:
    """一格 = 往底图里插一个节点 + 一组输入 + 一句人话说明。"""

    def __init__(self, cid, note, node_id, class_type, inputs):
        self.id = cid
        self.note = note
        self.node_id = node_id
        self.class_type = class_type
        self.inputs = dict(inputs)

    def __repr__(self):
        return "<Cell %s %s>" % (self.id, self.note)


TE_SPEED_NODE = "9900"
TE_SPEED_CLASS = "TESpeedQwenImage21"


def te_speed_cell(cid, note, *, attention="kitchen_int8",
                  step_cache="te_predictor", reuse_threshold=0.20,
                  error_limit=0.08, verbose=True):
    """一格 TE-Speed。

    `verbose` 默认 **True**（与产品侧相反）：扫描要的正是那行自报日志。
    生产接线在 `qwen21_build.TE_SPEED_*`，那里是 False —— 逐步骤刷屏会淹掉日志。

    `start/end_percent` 写 0.0 不是笔误：在这个插件里 `0.0` = 「用它的默认窗口」
    （实测生效为 0.18-0.86），而 `0.18` 那样写是「显式数值」——**没验过**是不是
    同一条路径。扫描要复现的是实测那批，所以照抄 0.0。
    """
    return Cell(cid, note, TE_SPEED_NODE, TE_SPEED_CLASS, {
        "attention": attention,
        "step_cache": step_cache,
        "reuse_threshold": reuse_threshold,
        "start_percent": 0.0,
        "end_percent": 0.0,
        "predictor_error_limit": error_limit,
        "verbose": verbose,
    })


# 2026-09-30 实测那一批。注意 `thr=0.001` 那几格是**故意的**：
# 它才是「关掉步预测」（最小 change 是 0.0403 > 0.001），用来把 attention
# 后端的速度与画质单独量出来；`0.0` 做不到这件事（会被改写成 0.018）。
TE_SPEED_PRESET = (
    te_speed_cell("A1_err002", "thr0.06 err0.02 —— 误差闸门收到最紧",
                  reuse_threshold=0.06, error_limit=0.02),
    te_speed_cell("A2_err032", "thr0.06 err0.32 —— 放到最宽",
                  reuse_threshold=0.06, error_limit=0.32),
    te_speed_cell("B1_thr010", "thr0.10 —— 阈值抬一格",
                  reuse_threshold=0.10),
    te_speed_cell("B2_thr020", "thr0.20 —— 抬到 0.20",
                  reuse_threshold=0.20),
    te_speed_cell("S1_speed", "thr0.20 + speed —— 最激进的一格",
                  reuse_threshold=0.20, step_cache="speed"),
    te_speed_cell("E0_default_off", "default + thr0.001 —— 关掉步预测的空转包装",
                  attention="default", reuse_threshold=0.001),
    te_speed_cell("E2_int8_off", "int8 + thr0.001 —— 纯 attention 的速度与画质",
                  reuse_threshold=0.001),
)

PRESETS = {"te_speed": TE_SPEED_PRESET}


# ---------------------------------------------------------------------------
# 日志：机制证据
# ---------------------------------------------------------------------------
def parse_te_speed_log(text):
    """从后端日志里刮出「它到底做了什么」。

    **这是插件专属的**（别的插件要另写一个）。它给的是机制层证据：
    那轮能说清「速度来自 attention 不是步预测」，靠的就是
    `predicted 2/20 steps ... full_steps=18` 这一行。
    """
    out = {}
    m = re.search(r"Qwen TE predictor config: (.+)", text or "")
    if m:
        out["reported"] = wfprobe.parse_kv(m.group(1))
    m = re.search(r"predicted (\d+)/(\d+) steps", text or "")
    if m:
        out["predicted"], out["steps_seen"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"full_steps=(\d+)", text or "")
    if m:
        out["full_steps"] = int(m.group(1))
    m = re.search(r"decision summary: (\{.*\})", text or "")
    if m:
        out["summary"] = m.group(1)
    # 这两个是「闸门到底触发过没有」的直接读数：全 0 说明那个旋钮根本没参与
    out["cooldown_hits"] = len(re.findall(r"result=cooldown", text or ""))
    out["deviation_hits"] = len(re.findall(r"result=deviation", text or ""))
    return out


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------
def _submit(http, workflow, client_id):
    r = http.post(HOST + "/prompt",
                  json={"prompt": workflow, "client_id": client_id})
    if r.status_code != 200:
        raise RuntimeError("提交被拒 %s：%s" % (r.status_code, r.text[:1200]))
    return r.json()["prompt_id"]


def _wait(http, prompt_id, timeout):
    """等 `/history` 出现这个 prompt_id。

    采样期后端的 HTTP 会被短暂饿死（2048² 实测 5 秒不答），所以**读失败要忽略
    继续等**，不能当成任务失败。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = http.get("%s/history/%s" % (HOST, prompt_id))
            if r.status_code == 200 and prompt_id in r.json():
                return r.json()[prompt_id]
        except Exception:
            pass
        time.sleep(0.7)
    raise TimeoutError("等 %s 的历史超时（%.0f 秒）" % (prompt_id, timeout))


def _img_names(history):
    return [im.get("filename")
            for entry in (history.get("outputs") or {}).values()
            for im in (entry.get("images") or [])]


def _tail(path):
    if not path or not os.path.exists(path):
        return 0
    return os.path.getsize(path)


def _slice(path, offset):
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8", "replace")


def _probe_block(prompt_id):
    try:
        with open(config.PROBE_REPORT, encoding="utf-8") as f:
            block = json.load(f).get(prompt_id)
    except Exception as exc:
        return {"probe_error": "读不到探针报告：%s" % exc}
    if not isinstance(block, dict):
        return {"probe_error": "报告里没有这个 prompt_id（可能被 60 条窗口挤掉了）"}
    sampling = block.get("sampling") or {}
    nodes = block.get("nodes") or {}
    return {
        # **这个**才是能拿来比加速的数（只包住纯去噪循环）
        "sampling_ms": sampling.get("total_ms"),
        "sampling_steps": sampling.get("steps"),
        "wall_ms": block.get("wall_ms"),
        "node_ms": {k: round(v.get("compute_ms") or 0.0, 1)
                    for k, v in sorted(nodes.items())
                    if (v.get("compute_ms") or 0) > 0},
    }


def run(base_graph, cells, *, steps=None, log_path=None, out_json=None,
        timeout=900.0, quiet=False):
    """逐格跑，返回结果列表。每跑完一格就落盘 —— 中途崩了前面的还在。"""
    client_id = str(uuid.uuid4())
    results = []
    with httpx.Client(trust_env=False, timeout=120.0) as http:
        for cell in cells:
            wf = copy.deepcopy(base_graph)
            if steps is not None:
                wf[KSAMPLER]["inputs"]["steps"] = steps
            wfprobe.insert_model_node(wf, cell.node_id, cell.class_type,
                                      cell.inputs)
            offset = _tail(log_path)
            t0 = time.time()
            prompt_id = _submit(http, wf, client_id)
            history = _wait(http, prompt_id, timeout)
            rec = {
                "cell": cell.id, "note": cell.note,
                "class_type": cell.class_type,
                "requested": dict(cell.inputs),
                "prompt_id": prompt_id,
                "wall_s": round(time.time() - t0, 2),
                "status": (history.get("status") or {}).get("status_str"),
                "images": _img_names(history),
            }
            rec.update(_probe_block(prompt_id))
            rec["log"] = parse_te_speed_log(_slice(log_path, offset))
            # **「我传的」对「它自报的」** —— 空列表才叫真的生效了
            rec["param_drift"] = wfprobe.effective_mismatch(
                cell.inputs, rec["log"].get("reported") or {})
            results.append(rec)
            if out_json:
                with open(out_json, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)
            if not quiet:
                lg = rec["log"]
                print("[%-16s] %-8s sampling=%-9s full/预测=%s/%s  %s"
                      % (cell.id, rec["status"], rec["sampling_ms"],
                         lg.get("full_steps"), lg.get("predicted"),
                         "  ! 参数漂移 %s" % (rec["param_drift"],)
                         if rec["param_drift"] else ""))
    return results


# ---------------------------------------------------------------------------
# 量
# ---------------------------------------------------------------------------
def analyze(base_png, results, *, out_dir=None):
    """与底图逐格比像素，打出「速度 / 画质」同一张表。"""
    out_dir = out_dir or config.OUTPUT_DIR
    ref = wfprobe.pixels(base_png)
    ref_g = wfprobe.gradient_energy(ref)
    rows = []
    for rec in results:
        names = rec.get("images") or []
        if not names:
            rows.append({**rec, "error": "这一格没有出图"})
            continue
        path = os.path.join(out_dir, names[0])
        if not os.path.exists(path):
            rows.append({**rec, "error": "找不到 %s" % path})
            continue
        px = wfprobe.pixels(path)
        stats = wfprobe.diff_stats(ref, px)
        rows.append({**rec, "file": names[0],
                     "gradient": wfprobe.gradient_energy(px), **stats})
    return {"base": os.path.basename(base_png), "base_gradient": ref_g,
            "rows": rows}


def print_table(report):
    print("\n底图 %s  梯度 g=%s" % (report["base"], report["base_gradient"]))
    print("%-16s %9s %8s %8s | %8s %9s | %7s %9s %5s"
          % ("格", "采样ms", "全算", "预测", "byte%", "byte_mean",
             "pixel%", "px_mean", "max"))
    for r in report["rows"]:
        if "error" in r:
            print("%-16s ERROR %s" % (r["cell"], r["error"]))
            continue
        print("%-16s %9s %8s %8s | %8s %9s | %7s %9s %5s  %s"
              % (r["cell"], r["sampling_ms"], r["log"].get("full_steps"),
                 r["log"].get("predicted"), r["byte_pct"], r["byte_mean_abs"],
                 r["pixel_pct"], r["pixel_mean_abs"], r["max_abs_color"],
                 "g=%s" % r["gradient"]))


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="一份底图逐格改一个旋钮，量速度（探针）与画质（两套口径）")
    ap.add_argument("--base", required=True,
                    help="底图：一张**真实产物** PNG（从它的 tEXt 取回当时提交的图）")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="te_speed")
    ap.add_argument("--steps", type=int, help="覆盖步数（默认沿用底图里的）")
    ap.add_argument("--log", help="后端 stderr 日志路径（给了才有机制证据）")
    ap.add_argument("--report", help="结果 JSON 落点")
    ap.add_argument("--out-dir", help="产物目录（默认取 config.OUTPUT_DIR）")
    ap.add_argument("--analyze", action="store_true", help="跑完顺手打像素表")
    ap.add_argument("--only", help="只跑这些格（逗号分隔）")
    a = ap.parse_args(argv)

    cells = PRESETS[a.preset]
    if a.only:
        want = {s.strip() for s in a.only.split(",") if s.strip()}
        cells = tuple(c for c in cells if c.id in want)
        if not cells:
            raise SystemExit("--only 一个都没匹配上：%s" % a.only)

    graph = wfprobe.reference_graph(a.base)
    print("底图 %s：%d 个节点，%s.model=%r，steps=%s"
          % (os.path.basename(a.base), len(graph), KSAMPLER,
             wfprobe.model_chain_tail(graph),
             graph[KSAMPLER]["inputs"].get("steps")))
    print("准备跑 %d 格：%s" % (len(cells), [c.id for c in cells]))

    results = run(graph, cells, steps=a.steps, log_path=a.log,
                  out_json=a.report)
    ok = [r for r in results if r["status"] == "success"]
    print("\n%d/%d 格 success" % (len(ok), len(results)))
    drifted = [r["cell"] for r in results if r.get("param_drift")]
    if drifted:
        print("**参数漂移**（我传的 ≠ 它自报的）：%s" % drifted)
        for r in results:
            if r.get("param_drift"):
                print("   %s: %s" % (r["cell"], r["param_drift"]))

    if a.analyze:
        report = analyze(a.base, results, out_dir=a.out_dir)
        print_table(report)
        if a.report:
            with open(os.path.splitext(a.report)[0] + ".pixels.json", "w",
                      encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
