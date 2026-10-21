"""逐节点耗时 —— 读探针报告，算**采样拆分**与**覆盖率**。

## 探针报告的形状（照实抄的，别猜）

```json
{
  "<prompt_id>": {
    "nodes": {
      "459:458": {"class_type": "KSampler", "compute_ms": 45456.298,
                  "calls": 1, "cached": false, "start_ms": 1680.46,
                  "vram_used_mb": 18655.4}
    },
    "node_types": {"459:458": "KSampler", ...},
    "sampling": {"calls": 1, "steps": 20, "total_ms": 45431.364},
    "cached_nodes": ["13", "459:451", ...],
    "started_at": 1790687641.5, "ended_at": 1790687690.6,
    "wall_ms": 49080.92, "t0": 29043.86
  }
}
```

`prompt_id` 就是工作台的 `job_id`（§7.1 第 2 条），所以查表不需要映射。

## 为什么要「拆采样」

`KSampler` 节点的 `compute_ms` 与 `sampling.total_ms` **不是一回事**：

- `sampling.total_ms` 只包住 `KSampler.sample()` 里那段**纯去噪循环**
- 节点的 `compute_ms` 还含节点内其余开销（条件准备、latent 准备等）

实测那一次：节点 45456.3 ms vs 纯循环 45431.4 ms，**差 24.9 ms**。
不看这一层就答不出「20 步里每步多久」——而「每步多久」正是调参时唯一想知道的数。

**注意别重复计数**：`sampling.total_ms` 已经含在 KSampler 节点的 `compute_ms`
里面了，所以 `Σ compute_ms` 是干净的，不要再把循环单独加一遍。

## 覆盖率是这里最有用的一个数

`Σ compute_ms ÷ wall_ms`。实测 **99.7%** —— 时间**全都**在节点里，
没有神秘的进程外开销。真掉到 60% 才说明有问题（在等什么？GC？换页？）。

## 探针没开的时候

报告里查不到这个 `prompt_id` 就是「没有数据」，**不是错误**：出图完全正常，
只是没有耗时。UI 要如实这么说，别摆一个空表面板。
"""
import os

from . import config
from .util import now_ms, read_json

try:
    import psutil
except ImportError:                       # 纯标准库也能跑，只是判活弱一点
    psutil = None

# 判断「哪个节点是采样节点」用类名而不是硬编码 id —— 换个模板 id 就变了
SAMPLER_CLASS = "KSampler"


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _pid_alive(pid):
    """True / False / **None（判不了）**。三态，因为猜错会撒谎。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return None


def probe_state(path=None):
    """当前后端带没带探针。

    状态文件由 `node_probe.py` 装载时写一次，里面有 pid。**文件在 ≠ 探针在** ——
    进程死掉后文件会留下来，所以必须配合判活。这一条是 `/api/backend` 里
    「本次无耗时数据（后端未带探针）」那句话的依据。
    """
    path = path or config.PROBE_STATE
    state = read_json(path)
    if not isinstance(state, dict) or not state.get("probe"):
        return {"known": False, "alive": False, "pid": None,
                "state_path": path, "report_path": config.PROBE_REPORT,
                "note": "后端没有登记探针状态 —— 启动时没带 COMFY_NODE_PROBE=1"
                        "（或用的是 P3 之前的旧探针）"}
    alive = _pid_alive(state.get("pid"))
    report_path = state.get("report_path") or config.PROBE_REPORT
    return {
        "known": True,
        "alive": bool(alive),
        "alive_unknown": alive is None,
        "pid": state.get("pid"),
        "boot_ts": state.get("boot_ts"),
        "state_path": path,
        "report_path": report_path,
        "report_mtime": _mtime_ms(report_path),
        "note": None if alive else
                "登记的进程 %s 已经不在了（后端重启过，这个状态文件是陈旧的）"
                % state.get("pid"),
    }


def _mtime_ms(path):
    try:
        return int(os.path.getmtime(path) * 1000)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def load_report(path=None):
    """读整份探针报告。读不到返回 `{}` —— 没开探针是正常状态，不是错误。"""
    data = read_json(path or config.PROBE_REPORT)
    return data if isinstance(data, dict) else {}


def block_for(report, prompt_id):
    block = (report or {}).get(prompt_id)
    return block if isinstance(block, dict) else None


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(block):
    """把一块报告变成 UI 直接能画的东西。`None` = 没有这块数据。"""
    if not isinstance(block, dict):
        return None
    wall = _num(block.get("wall_ms"))
    raw_nodes = block.get("nodes")
    if not isinstance(raw_nodes, dict):
        raw_nodes = {}
    sampling = block.get("sampling")
    if not isinstance(sampling, dict):
        sampling = {}

    rows = []
    total_ms = 0.0
    sampler_rows = []
    for node_id, node in raw_nodes.items():
        if not isinstance(node, dict):
            continue
        ms = _num(node.get("compute_ms")) or 0.0
        total_ms += ms
        class_type = node.get("class_type") or block.get("node_types", {}).get(node_id)
        row = {
            "node_id": node_id,
            "class_type": class_type or "?",
            "compute_ms": round(ms, 1),
            "calls": node.get("calls") or 0,
            "cached": bool(node.get("cached")),
            "start_ms": (round(_num(node.get("start_ms")), 1)
                         if _num(node.get("start_ms")) is not None else None),
            "vram_used_mb": (round(_num(node.get("vram_used_mb")), 1)
                             if _num(node.get("vram_used_mb")) is not None else None),
            "is_sampler": class_type == SAMPLER_CLASS,
        }
        if wall:
            row["pct_of_wall"] = round(100.0 * ms / wall, 2)
        if row["is_sampler"]:
            sampler_rows.append(row)
        rows.append(row)

    rows.sort(key=lambda r: r["compute_ms"], reverse=True)

    accounted = total_ms
    for row in rows:
        row["pct_of_accounted"] = (round(100.0 * row["compute_ms"] / accounted, 2)
                                   if accounted else None)

    # --- 采样拆分 ---
    loop_ms = _num(sampling.get("total_ms")) or 0.0
    steps = sampling.get("steps")
    steps = int(steps) if isinstance(steps, (int, float)) and steps else None
    split = {
        "loop_ms": round(loop_ms, 1),
        "loop_calls": sampling.get("calls") or 0,
        "steps": steps,
        "per_step_ms": round(loop_ms / steps, 2) if (loop_ms and steps) else None,
        "pct_of_wall": round(100.0 * loop_ms / wall, 2) if (wall and loop_ms) else None,
        "node_ms": None,
        "node_overhead_ms": None,
    }
    if sampler_rows:
        node_ms = sum(r["compute_ms"] for r in sampler_rows)
        split["node_ms"] = round(node_ms, 1)
        # 只有**真的采到过**循环才谈得上「节点内其他」。否则差值是整个节点
        # 耗时，把它说成「开销」是误导。
        if loop_ms > 0:
            split["node_overhead_ms"] = round(node_ms - loop_ms, 1)

    cached = [c for c in (block.get("cached_nodes") or []) if isinstance(c, str)]
    return {
        "available": True,
        "wall_ms": round(wall, 1) if wall else None,
        "total_ms": round(total_ms, 1),
        "coverage_pct": round(100.0 * accounted / wall, 2) if wall else None,
        "unaccounted_ms": round(wall - accounted, 1) if wall else None,
        "warm_start": bool(cached),
        "cached_nodes": cached,
        "cached_count": len(cached),
        "node_count": len(rows),
        "sampling": split,
        "slowest": rows[0] if rows else None,
        "rows": rows,
        "started_at": block.get("started_at"),
        "ended_at": block.get("ended_at"),
        "error": block.get("error"),
    }


def for_job(prompt_id, report=None, path=None):
    """按 job_id 取耗时。没数据时返回 `available: False` + 一句人话原因。"""
    if not prompt_id:
        return {"available": False, "reason": "没有给 job_id"}
    report = load_report(path) if report is None else report
    block = block_for(report, prompt_id)
    if block is None:
        return {
            "available": False,
            "reason": "探针报告里没有这次任务（后端未带探针，或报告被清过）",
            "report_path": path or config.PROBE_REPORT,
            "known_prompts": len(report or {}),
        }
    summary = summarize(block)
    summary["report_path"] = path or config.PROBE_REPORT
    return summary


def report_overview(path=None):
    """报告本身的状态 —— 给 `/api/backend` 与「本机总共采到几次」用。"""
    report_path = path or config.PROBE_REPORT
    report = load_report(report_path)
    return {
        "report_path": report_path,
        "report_mtime": _mtime_ms(report_path),
        "captured": len(report),
        "prompt_ids": sorted(report.keys())[-10:],
        "checked_at": now_ms(),
    }
