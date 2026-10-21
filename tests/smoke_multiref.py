#!/usr/bin/env python3
"""多参考图**能力测试** —— 不是「接线对不对」，而是「第二张真的进去了吗」。

判据怎么设计
------------
「图里连着两个 LoadImage」这件事，单测已经钉住了（那是**接线**）。真问题是：
第二张图到底参与了运算没有？—— 因为「接上了但被忽略」正是本工作区最怕的那种错
（能跑、有产物，而输入与输出无关）。

所以这里是一组**对照实验**：两次提交只差参考图列表，其余全一样（同一提示词、
同一 seed、同一分辨率、同一步数）。

    A  refs = [图1]          基线
    B  refs = [图1, 图2]     只多加一张

判据：**A 与 B 的产物必须不相同**。若第二张被静默忽略，两次提交会给出**逐位相同**
的图 —— 那就是「空转」的定义。反过来，只要 B≠A，就说明第二张真的进了采样。

`identical` 走大整数 XOR，是精确的，不受抽样影响；`pixel_pct` 是另一个口径，
两个都印出来（本工作区混用过这两个量纲一次，见「对照实验」那一节）。

怎么取证
--------
全程走**工作台的 HTTP 接口**（`/api/build` → `/api/run` → `/api/jobs/{id}`），
也就是浏览器点「出图」时走的那条路 —— 不另写一份脚本绕开它。
产物经 `GET /api/image?filename=…` 取回（**不猜输出目录**：本机有两个），
比对用 `wfprobe.diff_stats`，那个仪器在「对照实验」一节里已经用过。

用法
----
    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe tests/smoke_multiref.py
    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe tests/smoke_multiref.py --a x.png --b y.png

需要工作台在跑（默认 127.0.0.1:8189），且两张参考图都已在 ComfyUI 的 input/ 里。
两次出图合计约 3 分钟（实测 71.5 秒 + 109.1 秒，16 步 / 1024²）。
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

import wfprobe  # noqa: E402

BASE = "http://127.0.0.1:8189"
STEPS = 16
SEED = 20260930
PROMPT = ("Combine everything visible in the reference pictures into a single "
          "coherent photograph. Keep each subject's own colours, materials and "
          "shape. Studio lighting, photorealistic, one scene.")


def post(path, body, timeout=60):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def image_bytes(item):
    """按产物回执取回**字节**。走 `/api/image`，不拼磁盘路径 ——
    本机有不止一个输出目录（见 gallery.py），猜一个就是那种静默的错。"""
    q = urllib.parse.urlencode({"filename": item["filename"],
                                "subfolder": item.get("subfolder", "") or "",
                                "type": item.get("type", "output") or "output"})
    with urllib.request.urlopen(BASE + "/api/image?" + q, timeout=120) as r:
        return r.read()


def body_for(refs):
    return {"mode": "edit", "prompt": PROMPT, "source_images": list(refs),
            "steps": STEPS, "seed": SEED, "resolution": 1024, "cfg": 1.0,
            "sampler": "euler", "scheduler": "simple"}


def run_one(label, refs, checks):
    """构建 → 提交 → 等终态 → 返回 `(job_id, 产物字节, 记录)`。"""
    print("\n[%s] refs = %s" % (label, "、".join(refs)))
    built = post("/api/build", body_for(refs))
    loads = sorted(k for k, v in built["node_classes"].items()
                   if v == "LoadImage")
    print("  图上 LoadImage 节点: %s" % loads)
    for w in built["warnings"]:
        if "参考图" in w["text"]:
            print("  %s: %s" % (w["level"], w["text"]))

    out = post("/api/run", body_for(refs))
    job = out["job_id"]
    print("  已提交 %s  队列号 %s  估算 %s 秒"
          % (job[:8], out["queue_number"], out["estimate_s"]))

    deadline = time.time() + 900
    last = None
    while time.time() < deadline:
        rec = get("/api/jobs/" + job)
        if rec["status"] != last:
            last = rec["status"]
            print("    %-9s %s" % (last, rec.get("wall_ms") or ""))
        if rec["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(3)
    rec = get("/api/jobs/" + job)
    if rec["status"] != "done":
        raise SystemExit("任务没跑成功：%s %s" % (rec["status"], rec.get("error")))
    picks = rec.get("outputs") or []
    if not picks:
        raise SystemExit("任务成功但没产物")
    blob = image_bytes(picks[-1])
    wall_s = (rec.get("wall_ms") or 0) / 1000.0
    print("  产物 %s  %d 字节  墙钟 %.1f 秒"
          % (picks[-1]["filename"], len(blob), wall_s))
    checks.append({"what": "%s 跑完" % label, "ok": True,
                   "detail": "%s / %.1f 秒 / %d 字节"
                             % (picks[-1]["filename"], wall_s, len(blob))})
    return {"job_id": job, "file": picks[-1]["filename"], "wall_ms": rec.get("wall_ms"),
            "load_image_nodes": loads, "bytes": len(blob)}, blob


def main(argv=None):
    global BASE                      # 必须在第一次读 BASE 之前（--url 的默认值就是它）
    ap = argparse.ArgumentParser(description="多参考图能力测试（对照实验）")
    ap.add_argument("--url", default=BASE)
    ap.add_argument("--a", default="apple_1024.png", help="第 1 张参考图")
    ap.add_argument("--b", default="fashion_512.png", help="第 2 张参考图")
    ap.add_argument("--report", default=os.path.join(REPO, "state",
                                                     "multiref_report.json"))
    args = ap.parse_args(argv)
    BASE = args.url.rstrip("/")

    try:
        health = get("/api/health", timeout=6)
    except Exception as exc:                                   # noqa: BLE001
        print("SKIP: 工作台不在 %s（%s）" % (BASE, exc))
        return 2
    if not health.get("ok"):
        print("SKIP: 工作台没答 ok：%r" % health)
        return 2

    inputs = get("/api/inputs", timeout=30)["files"] or []
    for name in (args.a, args.b):
        if name not in inputs:
            print("SKIP: 后端 input/ 里没有 %s（现有：%s）"
                  % (name, "、".join(inputs)))
            return 2

    checks = []
    report = {"prompt": PROMPT, "steps": STEPS, "seed": SEED,
              "refs_a": [args.a], "refs_b": [args.a, args.b], "checks": checks}

    ra, blob_a = run_one("A 一张", [args.a], checks)
    rb, blob_b = run_one("B 两张", [args.a, args.b], checks)
    report["runs"] = {"one": ra, "two": rb}

    print("\n[比对] 只差「多加了一张参考图」，其余全同")
    diff = wfprobe.diff_stats(wfprobe.pixels(blob_a), wfprobe.pixels(blob_b))
    print(json.dumps(diff, ensure_ascii=False, indent=1))
    report["diff_one_vs_two"] = diff

    checks.append({"what": "图上真的多了一个 LoadImage",
                   "ok": sorted(ra["load_image_nodes"]) == ["900"]
                         and sorted(rb["load_image_nodes"]) == ["900", "901"],
                   "detail": "A=%s B=%s" % (ra["load_image_nodes"],
                                            rb["load_image_nodes"])})
    checks.append({"what": "第二张参考图**有效**（产物不许逐位相同）",
                   "ok": not diff["identical"],
                   "detail": "pixel_pct=%.1f%% byte_mean_abs=%.2f"
                             % (diff["pixel_pct"], diff["byte_mean_abs"])})
    report["verdict"] = ("PASS" if all(c["ok"] for c in checks) else "FAIL")

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("")
    for c in checks:
        print("  [%s] %-34s %s" % ("OK" if c["ok"] else "FAIL", c["what"],
                                   c["detail"]))
    print("\n判据：A 与 B 必须**不相同**（相同 = 第二张参考图被静默忽略）")
    print("结果：%s" % report["verdict"])
    print("报告：%s" % args.report)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
