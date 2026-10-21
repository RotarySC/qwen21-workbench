"""SSE 广播 + 断线重放。

线程模型是这里唯一的难点：`Watcher` 在自己的线程里读 ComfyUI 的 WS，
而订户队列是 `asyncio.Queue` —— **只能在事件循环里碰**。所以跨线程一律走
`publish_threadsafe()`（`loop.call_soon_threadsafe`），与 ComfyUI 自己
`send_sync` 的做法一致。

重放是为了判据 2：刷新页面时浏览器会带 `Last-Event-ID` 重连，把断线期间
漏掉的事件补上。光靠重放还不够 —— 刷新可能横跨很久，所以服务端在连接建立时
还会单独推一份 `snapshot`（见 `server.py`）。两者叠加：不丢，最多重复。
"""
import asyncio
import json
import threading
from collections import deque


class Broker:
    def __init__(self, replay_size=512, queue_size=1024, on_event=None):
        self._loop = None
        self._lock = threading.Lock()
        self._subs = {}
        self._next_sid = 0
        self._seq = 0
        self._replay = deque(maxlen=replay_size)
        self._queue_size = queue_size
        # 旁路观察者（日志台拿它当时间线）。**不参与**订阅与重放：
        # 它看到的是「刚发布的这一条」，与有没有浏览器连着无关。
        self.on_event = on_event

    # --- 生命周期 -------------------------------------------------------
    def bind_loop(self, loop):
        """记录事件循环，`publish_threadsafe` 靠它。在 uvicorn startup 时调。"""
        self._loop = loop

    @property
    def last_id(self):
        with self._lock:
            return self._seq

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subs)

    # --- 订阅 -----------------------------------------------------------
    def subscribe(self):
        q = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            sid = self._next_sid
            self._next_sid += 1
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid):
        with self._lock:
            self._subs.pop(sid, None)

    # --- 发布 -----------------------------------------------------------
    @staticmethod
    def _offer(q, item):
        """慢客户端：丢最旧的。它要的是最新状态，不是完整历史。"""
        try:
            q.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def publish(self, event):
        """在事件循环线程里发布。返回事件序号。"""
        with self._lock:
            self._seq += 1
            eid = self._seq
            self._replay.append((eid, event))
            subs = list(self._subs.values())
            tap = self.on_event
        # 旁路先走，且**绝不能让它影响分发**：日志台坏掉不该让出图停下来。
        # 放在锁外面 —— tap 里可能碰别的锁，握着这把锁调它迟早撞死。
        if tap is not None:
            try:
                tap(event)
            except Exception:                          # noqa: BLE001
                pass
        for q in subs:
            self._offer(q, (eid, event))
        return eid

    def publish_threadsafe(self, event):
        """从任意线程发布（Watcher 线程用这个）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return None
        loop.call_soon_threadsafe(self.publish, event)
        return None

    def replay_since(self, last_id):
        with self._lock:
            return [item for item in self._replay if item[0] > last_id]


def sse_frame(event_id, event):
    """一条 SSE 帧。

    `event:` 取类型名，前端就能用 `es.addEventListener("progress", …)`
    分开订阅；`data:` 里同时留一份 `type`，两边都能读。
    """
    etype = event.get("type", "message")
    body = json.dumps(event, ensure_ascii=False)
    if event_id is None:
        return "event: %s\ndata: %s\n\n" % (etype, body)
    return "id: %d\nevent: %s\ndata: %s\n\n" % (event_id, etype, body)
