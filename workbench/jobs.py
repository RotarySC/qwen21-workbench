"""任务状态与运行记录 —— 全部落盘，判据 2（刷新不丢）靠它。

两条独立的落盘通道，**职责不重叠**：

- `state/jobs/<id>.json` —— 每个任务一个文件，**可变**，随进度覆盖写。
  用于「工作台重启后把在跑的任务认回来」。
- `state/runs.jsonl` —— 每完成一次**追加一行**，**只增不改**。
  用于画廊与历史统计（P2）。追加的行里含探针摘要（P3）。

**`job_id` 就是 `prompt_id`。** 工作台自己铸一个规范 UUID 当 `prompt_id` 提交
（`POST /prompt` 接受，见 ComfyUI `comfy_execution/jobs.py:validate_job_id`），
于是 PNG 元数据、ComfyUI `/history`、探针报告、运行记录天然共用一个键，
不需要任何映射表 —— 这正是 spec §6.4 说的「钥匙」，而它不需要被拼出来。
"""
import os
import threading

from . import config
from .util import (append_jsonl, atomic_write_json, now_ms, read_json,
                   read_jsonl)

STATUSES = ("queued", "running", "done", "error", "cancelled")
TERMINAL = ("done", "error", "cancelled")
ACTIVE = ("queued", "running")

# 进度事件每次采样都来（40 步 = 40 次），没必要每次都写盘。
PROGRESS_SAVE_MIN_MS = 1000


def new_job(job_id, mode, params, prompt_text, workflow, estimate_s=None,
            sweep_id=None, cell=None, cell_index=None, parent_job_id=None):
    ts = now_ms()
    return {
        "job_id": job_id,
        "prompt_id": job_id,          # 刻意同值，见模块 docstring
        "mode": mode,
        "params": params,
        "prompt_text": prompt_text,
        # 从提交的图里抄出来，UI 才能把节点 id 显示成类名（P3 的耗时表也要）
        "node_classes": {nid: node.get("class_type")
                         for nid, node in workflow.items()},
        "node_count": len(workflow),
        "estimate_s": estimate_s,
        # P5：扫描分组。单个任务时是 None。刻意**不塞进 params** ——
        # params 描述的是「这一次出图」，分组是记录层的事；
        # 混在一起会让 `loadFromGallery` 把分组信息当参数填回表单。
        "sweep_id": sweep_id,
        "cell": cell,                 # {seed, steps}，扫描里这一格的取值
        # 格子在网格里的序号。**并排显示的次序靠它，不靠时间戳** ——
        # 同一毫秒能建好几个任务，按 created_at 排会出现说不清的乱序。
        "cell_index": cell_index,
        # 出图后自动放大那条链里的**子作业**才带它（= 父作业的 id）。
        # 与 sweep_id 同一个层次：都是「这次提交属于谁」，不是出图参数。
        "parent_job_id": parent_job_id,
        "status": "queued",
        "created_at": ts,
        "started_at": None,
        "ended_at": None,
        "wall_ms": None,
        "source": "workbench",        # 与「认领到的外来任务」区分
        "node_id": None,
        "class_type": None,
        "progress": None,             # {value, max}
        "cached_nodes": [],
        "outputs": [],
        "error": None,
    }


class JobStore:
    def __init__(self, jobs_dir=None, runs_file=None, max_jobs=800):
        self.jobs_dir = jobs_dir or config.JOBS_DIR
        self.runs_file = runs_file or config.RUNS_FILE
        self.max_jobs = max_jobs
        self._lock = threading.RLock()
        self._jobs = {}
        self._last_progress_save = {}
        os.makedirs(self.jobs_dir, exist_ok=True)
        self.load()

    # --- 磁盘 -----------------------------------------------------------
    def path(self, job_id):
        return os.path.join(self.jobs_dir, job_id + ".json")

    def _save(self, rec):
        atomic_write_json(self.path(rec["job_id"]), rec)

    def load(self):
        """载入全部任务文件。**不改状态** —— 对账交给 Watcher。
        上次进程死掉留下的 queued/running 可能是真的还在跑，也可能是后端一起
        重启了，这里无从判断，猜错会撒谎。"""
        with self._lock:
            self._jobs.clear()
            try:
                names = os.listdir(self.jobs_dir)
            except OSError:
                names = []
            for name in names:
                if not name.endswith(".json"):
                    continue
                rec = read_json(os.path.join(self.jobs_dir, name))
                if isinstance(rec, dict) and rec.get("job_id"):
                    self._jobs[rec["job_id"]] = rec
            return len(self._jobs)

    # --- 增删改查 -------------------------------------------------------
    def create(self, **kwargs):
        rec = new_job(**kwargs)
        with self._lock:
            self._jobs[rec["job_id"]] = rec
            self._save(rec)
        return rec

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def patch(self, job_id, persist=True, **fields):
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return None
            rec.update(fields)
            if persist:
                self._save(rec)
            return rec

    def patch_progress(self, job_id, node_id, class_type, value, maximum):
        """进度更新：内存每次都改，落盘节流到 1 秒一次。"""
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return None
            rec["node_id"] = node_id
            if class_type:
                rec["class_type"] = class_type
            rec["progress"] = {"value": value, "max": maximum}
            if rec["status"] in ACTIVE:
                rec["status"] = "running"
            last = self._last_progress_save.get(job_id, 0)
            if now_ms() - last >= PROGRESS_SAVE_MIN_MS:
                self._last_progress_save[job_id] = now_ms()
                self._save(rec)
            return rec

    def mark_running(self, job_id, node_id=None, class_type=None):
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return None
            if rec["status"] == "queued":
                rec["started_at"] = now_ms()
            rec["status"] = "running"
            if node_id is not None:
                rec["node_id"] = node_id
            if class_type is not None:
                rec["class_type"] = class_type
            self._save(rec)
            return rec

    def finish(self, job_id, status, outputs=None, error=None, wall_ms=None):
        """终态：改任务文件 + 往 runs.jsonl 追一行。

        **返回 `None` 表示「我什么都没做」** —— 任务是别人的，或者已经收过尾了。
        调用方靠这个区分「这次是我收的尾」和「别人已经收过了」，才不会重复广播。
        （WS 的成功消息与轮询几乎同时想收尾，这条是常态而不是边角。）
        """
        assert status in TERMINAL, status
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return None
            if rec["status"] in TERMINAL:
                return None
            ended = now_ms()
            started = rec.get("started_at") or rec.get("created_at") or ended
            rec["status"] = status
            rec["ended_at"] = ended
            rec["wall_ms"] = wall_ms if wall_ms is not None else (ended - started)
            rec["error"] = error
            if outputs is not None:
                rec["outputs"] = outputs
            rec["progress"] = None
            rec.pop("node_id", None)
            self._save(rec)
            self._append_run(rec)
            self._prune()
            return rec

    def _append_run(self, rec):
        append_jsonl(self.runs_file, {
            "job_id": rec["job_id"],
            "prompt_id": rec["prompt_id"],
            "mode": rec["mode"],
            "params": rec["params"],
            "prompt_text": rec["prompt_text"],
            "status": rec["status"],
            "created_at": rec["created_at"],
            "started_at": rec["started_at"],
            "ended_at": rec["ended_at"],
            "wall_ms": rec["wall_ms"],
            "outputs": rec["outputs"],
            "error": rec["error"],
            "cached_nodes": rec.get("cached_nodes") or [],
        })

    def _prune(self):
        """任务文件会一直堆积。只裁已终态的最旧的，在跑的绝不动。"""
        if len(self._jobs) <= self.max_jobs:
            return
        terminal = [r for r in self._jobs.values() if r["status"] in TERMINAL]
        terminal.sort(key=lambda r: r.get("ended_at") or 0)
        for rec in terminal[:len(self._jobs) - self.max_jobs]:
            try:
                os.unlink(self.path(rec["job_id"]))
            except OSError:
                pass
            self._jobs.pop(rec["job_id"], None)

    def forget(self, job_ids):
        """把记录**从账里去掉**（P20j 的「清理」）→ 真的删掉的那批 id。

        与 `_prune()` 的分工：那一条按**条数**裁最旧的，这一条按**调用方挑好的
        名单**删 —— 名单是「产物已经找不回来」那些，只有 server 知道（它要摸磁盘
        与归档目录，`jobs` 这一层不认识文件）。

        **在跑的绝不动**：调用方已经挡过一道，这里再挡一道。这两处都不可逆，
        而不可逆的东西值得被说两遍。

        **`runs.jsonl` 不动**：那是历史账（画廊按文件名跟它对账），而这张列表的
        数据源是 `state/jobs/*.json`。把账也划掉，等于让「这张图是哪次跑出来的」
        这个问题永远没有答案 —— 而档案本来就不该由「列表清理」来改写。
        """
        removed = []
        with self._lock:
            for job_id in list(job_ids or []):
                rec = self._jobs.get(job_id)
                if rec is None or rec["status"] in ACTIVE:
                    continue
                try:
                    os.unlink(self.path(job_id))
                except OSError:
                    pass
                self._jobs.pop(job_id, None)
                removed.append(job_id)
        return removed

    # --- 查询 -----------------------------------------------------------
    def list(self, limit=50, offset=0, mode=None, sweep_id=None):
        with self._lock:
            rows = list(self._jobs.values())
        if mode:
            rows = [r for r in rows if r["mode"] == mode]
        if sweep_id:
            rows = [r for r in rows if r.get("sweep_id") == sweep_id]
        rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        return rows[offset:offset + limit]

    def sweep(self, sweep_id):
        """一次扫描的全部格子，**按格子在网格里的序号**排。

        刻意不按时间排序：同一毫秒能建好几个任务，`created_at` 分不出先后，
        并排显示的次序会变得说不清。`cell_index` 是提交时定下来的，永远准。
        """
        rows = self.list(limit=10000, sweep_id=sweep_id)
        rows.sort(key=lambda r: (r.get("cell_index") is None,
                                 r.get("cell_index") or 0,
                                 r.get("created_at") or 0))
        return rows

    def active(self):
        with self._lock:
            return [r for r in self._jobs.values() if r["status"] in ACTIVE]

    def count(self):
        with self._lock:
            return len(self._jobs)

    def runs(self, limit=200):
        """runs.jsonl 反向读取（最新在前）。画廊与统计用。"""
        rows = read_jsonl(self.runs_file)
        rows.sort(key=lambda r: r.get("ended_at") or 0, reverse=True)
        return rows[:limit]
