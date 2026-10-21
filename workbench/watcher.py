"""WS 读线程 + `/history` 轮询 —— 任务状态的唯一推进者。

## 为什么 WS 只是「加速层」

ComfyUI 的事件路由有个坑：`execution.py:736` 用**提交时**的
`extra_data["client_id"]` 去设 `server.client_id`，而
`execution_start` / `execution_cached` / `executed` / `execution_success`
只在 `server.client_id` 非空时发出（`if self.server.client_id is not None`）。
也就是说 —— **谁最后提交，事件就归谁**。如果用户同时从 ComfyUI 自带 Web UI
提交了一个任务，`server.client_id` 会被改成那个浏览器，我们这边 EventSource
就再也收不到 WS 事件了。

所以本模块的判据是：**`/history` 是权威，WS 只负责让进度好看**。
收尾（终态、产物、耗时）只有一条代码路径 `_finish_from_history()`，
WS 的成功/失败消息只是提前触发它一次，轮询永远在兜底。

## 一个 client_id，两处使用

`/prompt` 的 body 与 WS 的 `?clientId=` 必须是同一个值（否则事件发给别人）。
它落盘在 `state/client_id.txt`：工作台重启后仍用同一个 id，
ComfyUI 的 `sockets[sid]` 会在重连时替换掉旧条目，是官方支持的复用方式
（`server.py:274-276`）。
"""
import json
import logging
import os
import threading
import time
import uuid

import websocket

from . import comfy, config
from .util import atomic_write_text, now_ms

logger = logging.getLogger("workbench.watcher")


def load_or_create_client_id(path=None):
    path = path or config.CLIENT_ID_FILE
    cid = None
    existed = os.path.exists(path)
    try:
        with open(path, encoding="utf-8") as f:
            cid = f.read().strip()
    except OSError as exc:
        # **文件在、却读不出来**才是要说的那件事：id 决定事件路由
        # （见模块开头），悄悄换一个会让 WS 事件从此推给一个不存在的客户端 ——
        # 而那看起来只是「进度不动了」。文件根本不在（第一次跑）是正常的，
        # 不报。
        if existed:
            logger.warning("client_id 文件读不了（%s），改用新生成的 id：%s",
                           path, exc)
    if not cid or len(cid) < 8:
        cid = uuid.uuid4().hex
        atomic_write_text(path, cid + "\n")
    return cid


class Watcher:
    """后台线程。`start()` 后可以随时 kill 后端或重启工作台，它会自己接回来。"""

    def __init__(self, store, broker, client=None, tick=0.5,
                 poll_interval=None, client_id=None, client_id_file=None,
                 on_done=None):
        self.store = store
        self.broker = broker
        self.client = client or comfy.ComfyClient()
        # 出图完成后要不要接一段自动放大：**钩子是注入的**（见 `_maybe_chain`）。
        # watcher 不认识 server（反过来的依赖已经有了），所以提交那件事留在 server。
        self.on_done = on_done
        # client_id 是**状态**，所以跟 state 目录走 —— 否则测试会把 id 写进
        # 真实仓库的 state/ 里。落盘是为了工作台重启后仍是同一个身份。
        self.client_id = client_id or load_or_create_client_id(client_id_file)
        self.tick = tick
        self.poll_interval = (config.POLL_INTERVAL if poll_interval is None
                              else poll_interval)
        self._thread = None
        self._stop = threading.Event()
        self._ws = None
        self._ws_connected = False
        self._backend_up = None
        self._last_poll = 0.0
        self._last_queue_remaining = None
        self.stats = {"ws_messages": 0, "ws_reconnects": 0, "polls": 0,
                      "foreign_jobs": 0, "last_ws_error": None}

    # --- 生命周期 -------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="comfy-watcher",
                                        daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout=3.0):
        self._stop.set()
        self._close_ws()
        if self._thread:
            self._thread.join(timeout)

    def _publish(self, event):
        self.broker.publish_threadsafe(event)

    # --- 主循环 ---------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                if not self.client.running():
                    self._set_backend(False)
                    self._close_ws()
                    self._stop.wait(1.5)
                    continue
                self._set_backend(True)
                self._ensure_ws()
                if now_ms() - self._last_poll >= self.poll_interval * 1000:
                    self._last_poll = now_ms()
                    self._poll()
                raw = self._recv()
                if raw:
                    self.handle_raw(raw)
            except Exception as exc:                      # 线程不能死
                self.stats["last_ws_error"] = "%s: %s" % (type(exc).__name__, exc)
                self._close_ws()
                self._stop.wait(1.0)

    def _recv(self):
        if not self._ws_connected or self._ws is None:
            self._stop.wait(self.tick)
            return None
        try:
            return self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except (websocket.WebSocketException, OSError) as exc:
            self.stats["last_ws_error"] = "%s: %s" % (type(exc).__name__, exc)
            self._close_ws()
            return None

    def _ensure_ws(self):
        if self._ws_connected:
            return
        try:
            self._ws = websocket.create_connection(
                self.client.ws_url(self.client_id),
                timeout=self.tick, enable_multithread=True)
            self._ws_connected = True
            self.stats["ws_reconnects"] += 1
        except Exception as exc:
            self.stats["last_ws_error"] = "%s: %s" % (type(exc).__name__, exc)
            self._ws = None
            self._ws_connected = False
            self._stop.wait(1.0)

    def _close_ws(self):
        ws, self._ws = self._ws, None
        self._ws_connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _set_backend(self, up):
        if self._backend_up == up:
            return
        self._backend_up = up
        self._publish({"type": "backend", "running": up})

    # --- 消息处理（可单测：不碰网络）------------------------------------
    def handle_raw(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            return                    # 二进制帧 = 采样预览图，P1 不用
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        self.stats["ws_messages"] += 1
        etype = msg.get("type")
        data = msg.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        handler = getattr(self, "_ws_" + str(etype), None)
        if handler is not None:
            handler(data)

    def _job(self, data):
        pid = data.get("prompt_id")
        if not pid:
            return None, None
        job = self.store.get(pid)
        return pid, job

    def _class_type(self, job, node_id):
        return ((job or {}).get("node_classes") or {}).get(node_id)

    # --- WS 事件 --------------------------------------------------------
    def _ws_status(self, data):
        remaining = (((data.get("status") or {}).get("exec_info") or {})
                     .get("queue_remaining"))
        self._last_queue_remaining = remaining
        self._publish({"type": "status", "queue_remaining": remaining})

    def _ws_execution_start(self, data):
        pid, job = self._job(data)
        if pid is None:
            return
        if job is None:
            # 外来任务（从 ComfyUI 自带 UI 提交的）。不接管，只让页面知道
            # 「后端正忙」，免得用户以为自己的任务卡住了。
            self.stats["foreign_jobs"] += 1
            self._publish({"type": "external_job", "prompt_id": pid})
            return
        self.store.mark_running(pid)
        self._publish({"type": "job_started", "job_id": pid, "prompt_id": pid,
                       "mode": job.get("mode")})

    def _ws_execution_cached(self, data):
        pid, job = self._job(data)
        if job is None:
            return
        nodes = data.get("nodes") or []
        self.store.patch(pid, cached_nodes=nodes)
        self._publish({"type": "cached", "job_id": pid, "prompt_id": pid,
                       "nodes": nodes})

    def _ws_executing(self, data):
        node = data.get("node")
        if node is None:
            # main.py:374 的「队列空了」信号，**不是**完成通知 —— 收尾走 /history
            return
        pid, job = self._job(data)
        if job is None:
            return
        class_type = self._class_type(job, node)
        self.store.mark_running(pid, node_id=node, class_type=class_type)
        self._publish({"type": "node", "job_id": pid, "prompt_id": pid,
                       "node_id": node, "class_type": class_type,
                       "node_index": None})

    def _ws_progress(self, data):
        pid, job = self._job(data)
        if job is None:
            return
        node = data.get("node")
        value, maximum = data.get("value"), data.get("max")
        self.store.patch_progress(pid, node, self._class_type(job, node),
                                  value, maximum)
        self._publish({"type": "progress", "job_id": pid, "prompt_id": pid,
                       "node_id": node, "value": value, "max": maximum})

    def _ws_executed(self, data):
        pid, job = self._job(data)
        if job is None:
            return
        out = data.get("output") or {}
        images = comfy.outputs_from_history({"outputs": {data.get("node"): out}})
        if not images:
            return
        seen = {(o.get("filename"), o.get("subfolder")) for o in job.get("outputs") or []}
        merged = list(job.get("outputs") or [])
        for img in images:
            if (img["filename"], img["subfolder"]) not in seen:
                merged.append(img)
        self.store.patch(pid, outputs=merged)
        self._publish({"type": "output", "job_id": pid, "prompt_id": pid,
                       "outputs": images})

    def _ws_execution_success(self, data):
        pid, _ = self._job(data)
        if pid:
            self._finish_from_history(pid)

    def _ws_execution_error(self, data):
        # 此刻 /history 可能还没写完（历史条目是在 prompt_worker 里补的），
        # 所以先只把错误细节报给页面，终态仍由 _finish_from_history 定。
        pid, job = self._job(data)
        if job is None:
            return
        self._publish({"type": "job_error_detail", "job_id": pid,
                       "prompt_id": pid,
                       "message": data.get("exception_message")
                                  or data.get("exception_type") or "执行出错",
                       "node_id": data.get("node_id"),
                       "node_type": self._class_type(job, data.get("node_id"))})
        self._finish_from_history(pid)

    def _ws_execution_interrupted(self, data):
        pid, _ = self._job(data)
        if pid:
            self._finish_from_history(pid)

    # --- 收尾（唯一路径）------------------------------------------------
    def _finish_from_history(self, pid):
        entry = self.client.history(pid)
        if not isinstance(entry, dict):
            return False
        single = entry.get(pid) if pid in entry else entry
        return self._finish(pid, single)

    def _finish(self, pid, entry):
        status, error = comfy.history_status(entry)
        if status is None:
            return False
        outputs = comfy.outputs_from_history(entry)
        start, end = comfy.history_timestamps(entry)
        wall = (end - start) if (start and end) else None
        rec = self.store.finish(pid, status, outputs=outputs, error=error,
                                wall_ms=wall)
        if rec is None:                 # 已经从别处收过尾了
            return False
        if status == "done":
            self._publish({"type": "job_done", "job_id": pid, "prompt_id": pid,
                           "wall_ms": rec["wall_ms"], "outputs": outputs})
            self._maybe_chain(pid, rec)
        elif status == "error":
            self._publish({"type": "job_error", "job_id": pid, "prompt_id": pid,
                           "message": (error or {}).get("message", "执行出错"),
                           "node_type": (error or {}).get("node_type")})
        else:
            self._publish({"type": "job_cancelled", "job_id": pid, "prompt_id": pid})
        return True

    # --- 收尾之后的钩子：出图后自动放大 ---------------------------------
    def _maybe_chain(self, pid, rec):
        """出图完成后要不要接一段放大。

        **为什么挂在这里**：收尾只有这一条代码路径（WS 只是提前触发，轮询兜底），
        所以链只需要在一个地方开口 —— 挂在 WS 消息上会漏掉「WS 被别的客户端
        抢走、只剩轮询收尾」的那一半。

        `on_done` 由 server 注入（它才知道怎么提交、怎么记成本）。钩子里的异常
        **一律吞掉并记进 stats**：链坏了是链的事，不能让「收尾」这件事失败 ——
        那会让一个已经成功的作业变成永远收不了尾。
        """
        if not self.on_done:
            return
        spec = (rec.get("params") or {}).get("auto_upscale") or {}
        if not spec.get("enabled"):
            return
        try:
            self.on_done(pid)
            self.stats["chains_started"] = self.stats.get("chains_started", 0) + 1
        except Exception as exc:                      # noqa: BLE001
            self.stats["last_chain_error"] = "%s: %s" % (type(exc).__name__, exc)

    # --- 轮询兜底 -------------------------------------------------------
    def _poll(self):
        """每个 tick 一次：把在跑的任务对齐到后端真实状态。

        这是判据 2 的地基 —— 页面刷新、工作台重启、WS 被别的客户端抢走，
        都不影响它。
        """
        self.stats["polls"] += 1
        active = self.store.active()
        qids = self.client.queue_ids()
        if qids is None:
            return
        if self._last_queue_remaining is None:
            self._last_queue_remaining = len(qids)
        for job in active:
            pid = job.get("prompt_id")
            if not pid:
                continue
            if pid in qids:
                if qids[pid] == "running" and job.get("status") == "queued":
                    self.store.mark_running(pid)
                continue
            if self._finish_from_history(pid):
                continue
            # 既不在队列里，也查不到历史
            if now_ms() - (job.get("created_at") or 0) > config.LOST_GRACE_MS:
                rec = self.store.finish(pid, "error", error={
                    "message": "任务既不在后端队列里、也查不到历史 —— "
                               "后端可能重启过，这次运行的产物无法确认",
                    "code": "lost"})
                if rec is not None:
                    self._publish({"type": "job_error", "job_id": pid,
                                   "prompt_id": pid, "code": "lost",
                                   "message": rec["error"]["message"]})

    def wait_finish(self, pid, timeout=5.0, interval=0.25):
        """阻塞等一个任务走完收尾路径。取消按钮用它 —— 被 interrupt 的任务
        history 里很快会出现 `execution_interrupted`，值得等几百毫秒拿到真实
        终态，而不是抢在它前面硬写一个「已取消」。"""
        deadline = now_ms() + int(timeout * 1000)
        while True:
            if self._finish_from_history(pid):
                return True
            rec = self.store.get(pid)
            if rec is None or rec.get("status") in ("done", "error", "cancelled"):
                return False
            if now_ms() >= deadline:
                return False
            time.sleep(interval)

    # --- 对外快照 -------------------------------------------------------
    def state(self):
        return {
            "client_id": self.client_id,
            "ws_connected": self._ws_connected,
            "backend_up": bool(self._backend_up),
            "queue_remaining": self._last_queue_remaining,
            "stats": dict(self.stats),
            "poll_interval_s": self.poll_interval,
        }
