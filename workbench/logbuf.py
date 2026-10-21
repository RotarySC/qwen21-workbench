"""运行日志的环形缓冲 —— 日志台唯一的数据源。

**为什么不是「把 stdout 抄一份」**：工作台从 `run.bat` 双击起时，输出只落在那个
控制台窗口里；抄 stdout 还收不到 `uvicorn` 自己的记录（启动失败、500 的堆栈都在
那里）。装一个 `logging.Handler` 两样都能收 —— 顺带给这个包补上日志：在这之前
全仓只有一句启动 `print`。

环里装两种东西，同一个行形状：

| kind | 来源 | 谁写进去 |
|---|---|---|
| `event` | 事件类型名（`job_error` / `sweep_failed` …） | `events.Broker` 的一个 tap → `note_event()` |
| `log` | logger 名（`workbench.server` / `uvicorn.error` …） | `BufferHandler` |

**事件只在一处挂钩**：`Broker.publish()` 里加一个 tap，于是全部已有事件都自动
进来，不必去改那十几个 `publish()` 调用点；启动流程走的是
`publish_threadsafe`，它只是把 `publish` 排进事件循环，所以线程里的事件一样收得到。
"""
import logging
import threading
import traceback
from collections import deque

from .util import now_ms

DEFAULT_CAPACITY = 2000
SEVERITIES = ("info", "warn", "error")
KIND_EVENT = "event"
KIND_LOG = "log"

# uvicorn 的**访问日志不进环**。日志台自己每秒轮询一次 `/api/logs`，把它记下来
# 的话，环里很快就只剩「GET /api/logs 200」—— 日志台会**把自己冲掉**，而且
# 没有症状（页面照常工作，只是别的东西都被挤出去了）。
# 只在这里挡：不动 uvicorn 自己的输出，控制台照旧打印访问日志。
SKIP_LOGGERS = ("uvicorn.access",)

# 事件类型 → 级别。**表在后端**：前端只负责画，不再各存一份映射（两份迟早分家）。
_EVENT_LEVEL = {
    "job_error": "error",
    "job_error_detail": "error",
    "sweep_failed": "error",
    "backend_failed": "error",
    "job_cancelled": "warn",
}

# 不进时间线的事件 —— 这是**决定**，不是遗漏：
#   progress  每个采样步/每次 tick 一条，进来就把别的东西全冲掉
#   node      逐节点执行消息，一个作业几十条
#   status    队列剩余数变化，扫描时几十条；那个数在「环境体检」里一直看得见
#   backend_log  后端 stdout 的**正文**归「后端日志」那一块，不在这里再抄一遍
#   snapshot  SSE 连接时的全量快照，不是一次「发生过的事」
_SKIP_EVENTS = frozenset({"progress", "node", "status", "backend_log",
                          "snapshot"})


def short_id(value):
    """作业 / 扫描的 id 都很长，时间线上只要前 8 位认得出来就行。"""
    text = str(value or "")
    return text[:8] if text else "?"


def level_of_number(levelno):
    """`logging` 的级别 → 本模块的三档。"""
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    return "info"


class LogBuffer:
    """环形缓冲。满了丢**最旧**的 —— 日志要的是最近发生了什么。"""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        self.capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        self._rows = deque(maxlen=self.capacity)
        self._seq = 0

    @property
    def last_seq(self):
        with self._lock:
            return self._seq

    def append(self, level, source, text, detail=None, kind=KIND_EVENT,
               event_type=None, ts=None):
        text = (text or "").strip()
        if not text and not detail:
            return None
        with self._lock:
            self._seq += 1
            row = {
                "seq": self._seq,
                "ts": int(ts if ts is not None else now_ms()),
                "level": level if level in SEVERITIES else "info",
                "kind": kind,
                "source": str(source or ""),
                "event": event_type,
                "text": text,
                "detail": detail or None,
            }
            self._rows.append(row)
            return row

    def rows(self, since=None, level=None, kind=None, limit=None):
        """`since` 是**游标语义**：只给 `seq > since` 的行。

        `since` 比当前 `last_seq` 还大 = 服务重启过（seq 从 1 重来），这时
        **返回全部**并由调用方报 `reset`。不这么办的话，页面会永远收到空列表，
        而看起来就像「这段时间什么都没发生」—— 正是本工作区最忌讳的那种错。
        """
        with self._lock:
            out = list(self._rows)
            last = self._seq
        if since is not None and since <= last:
            out = [r for r in out if r["seq"] > since]
        if level == "warn":
            out = [r for r in out if r["level"] in ("warn", "error")]
        elif level == "error":
            out = [r for r in out if r["level"] == "error"]
        if kind:
            out = [r for r in out if r["kind"] == kind]
        if limit:
            out = out[-int(limit):]
        return out

    def clear(self):
        with self._lock:
            self._rows.clear()


# --- 全局环（进程内单例）---------------------------------------------------
BUFFER = LogBuffer()
_installed = False


class BufferHandler(logging.Handler):
    """把 `logging` 记录收进环。**永远不抛** —— 日志坏掉不能带崩业务。"""

    def __init__(self, buffer, level=logging.INFO):
        super().__init__(level=level)
        self.buffer = buffer

    def emit(self, record):
        try:
            # 同一个 record 会经过**每一个** handler。装了不止一处时（root 与
            # `uvicorn.error` 都挂），不打这个标记就会一行记成两行 ——
            # 而重复的行看起来只是「后端话多」，不会有人去查。
            if getattr(record, "_logbuf_seen", False):
                return
            record._logbuf_seen = True
            if any(record.name == n or record.name.startswith(n + ".")
                   for n in SKIP_LOGGERS):
                return
            detail = None
            if record.exc_info:
                detail = "".join(traceback.format_exception(*record.exc_info))
            self.buffer.append(
                level=level_of_number(record.levelno),
                source=record.name,
                text=record.getMessage(),
                detail=detail,
                kind=KIND_LOG,
                ts=int(record.created * 1000),
            )
        except Exception:                              # noqa: BLE001
            self.handleError(record)


def install(buffer=None, level=logging.INFO):
    """装到 root logger 上，并把 `workbench` 这一族调到 INFO。**幂等** ——
    `create_app()` 每次调用都走一遍，测试里会调很多次，重复装就会一行记成好几行。

    **为什么要显式设 level**：root 的默认级别是 WARNING，而 `workbench.server`
    自己没设级别（NOTSET），于是它的有效级别跟着 root 走 —— `logger.info(...)`
    在**到达 handler 之前**就被丢掉了，环里一片空白，而没有任何报错。
    只调 `workbench` 这一族（不动 root 的级别）：root 一调，第三方库的 INFO
    会一起灌进环里，把真正要看的东西挤走。
    """
    global _installed
    target = buffer or BUFFER
    if _installed and buffer is None:
        return target
    handler = BufferHandler(target, level=level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger("workbench").setLevel(level)
    if buffer is None:
        _installed = True
    return target


def attach_to_uvicorn(buffer=None, level=logging.INFO):
    """**必须在 uvicorn 配好日志之后再调一次**（`lifespan` 里）。

    为什么 `install()` 一次搞不定：uvicorn 启动时会
    `dictConfig(LOGGING_CONFIG)`，而 dictConfig 会把它**配置过的**那些 logger
    的 handler 全部清空 —— `uvicorn` 与 `uvicorn.error` 都在里面，于是先前挂上去
    的 handler 被**悄悄摘掉**。而 `uvicorn.error` 恰恰是
    「Exception in ASGI application」那条 500 堆栈走的 logger，单独挂 root 也
    收不到它（它的父 logger `uvicorn` 带着控制台 handler 且 `propagate: False`，
    那条链到不了 root）。

    `lifespan` 在 dictConfig 之后才跑，所以挂在那里才是稳的。
    handler 里那个 `_logbuf_seen` 标记保证重复挂不会一行记成两行。
    """
    target = buffer or BUFFER
    handler = BufferHandler(target, level=level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).addHandler(handler)
    return handler


def note(text, level="info", source="workbench", detail=None):
    """写一条**事项**（不是 logging 记录）。"""
    return BUFFER.append(level=level, source=source, text=text, detail=detail,
                         kind=KIND_EVENT)


def classify_event(event):
    """事件 → `(级别, 摘要, 详情)`；返回 `None` 表示这一条不进时间线。

    摘要写成**一句话**，人看得懂的那种（与提交前那一栏同规矩）——
    时间线不是 JSON 转储。
    """
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    if not etype or etype in _SKIP_EVENTS:
        return None
    level = _EVENT_LEVEL.get(etype, "info")
    detail = None

    if etype == "job_queued":
        text = "作业 %s 已入队" % short_id(event.get("job_id"))
    elif etype == "job_started":
        text = "作业 %s 开始执行" % short_id(event.get("job_id"))
    elif etype == "job_done":
        images = (event.get("outputs") or {}).get("images") or []
        text = "作业 %s 完成（%d 张）" % (short_id(event.get("job_id")),
                                          len(images))
    elif etype == "job_cancelled":
        text = "作业 %s 已撤销" % short_id(event.get("job_id"))
    elif etype == "job_error":
        text = "作业 %s 失败：%s" % (short_id(event.get("job_id")),
                                     event.get("message") or "（后端没给原因）")
    elif etype == "job_error_detail":
        node = event.get("node_type") or event.get("node_id")
        text = "作业 %s 在节点 %s 出错：%s" % (
            short_id(event.get("job_id")), node or "?",
            event.get("message") or "（后端没给原因）")
    elif etype == "job_chain":
        # 链（出图后自动放大）成功与失败**同一个事件类型**，靠 payload 区分。
        chain = event.get("chain") or {}
        label = " ×%s" % chain.get("scale") if chain.get("scale") else ""
        if chain.get("error"):
            level = "error"
            text = "作业 %s 的出图后自动放大%s失败：%s" % (
                short_id(event.get("job_id")), label, chain["error"])
        else:
            text = "作业 %s 的出图后自动放大%s：%s" % (
                short_id(event.get("job_id")), label,
                chain.get("status") or "已排上")
    elif etype == "sweep_queued":
        cells = event.get("cells") or event.get("total")
        text = "扫描 %s 已排队%s" % (
            short_id(event.get("sweep_id")),
            "（%d 格）" % len(cells) if isinstance(cells, (list, tuple))
            else ("（%d 格）" % cells if cells else ""))
    elif etype == "sweep_failed":
        text = "扫描 %s 第 %s/%s 格失败，整次作废：%s" % (
            short_id(event.get("sweep_id")), event.get("at"), event.get("total"),
            event.get("message") or "（后端没给原因）")
    elif etype == "backend_launching":
        text = "正在启动后端（pid %s%s）" % (
            event.get("pid") or "?",
            "，端口 %s" % event["port"] if event.get("port") else "")
    elif etype == "backend_ready":
        text = "后端已就绪"
    elif etype == "backend_stopped":
        # 退出码**不写进去**：用户点「关」得到的就是那个（TerminateProcess 给 1），
        # 它看起来像出错，其实正是要的结果。派生进程数才是值得说的那件事 ——
        # 那个后端是一棵树（venv 的 python.exe 只是壳，见 `launch._descendants`）。
        bits = []
        if event.get("derived") is not None:
            bits.append("连带 %s 个派生进程" % event["derived"])
        if event.get("stopped_ms") is not None:
            bits.append("用了 %s ms" % event["stopped_ms"])
        text = "后端已停（pid %s%s）" % (
            event.get("pid") or "?", ("，" + "，".join(bits)) if bits else "")
    elif etype == "backend_failed":
        text = "后端启动失败：%s" % (event.get("error") or event.get("message")
                                     or "（后端没给原因）")
        detail = event.get("detail")
    elif etype == "backend":
        text = "后端连接：%s" % ("已连接" if event.get("running") else "已断开")
    elif etype == "external_job":
        text = "别处提交的作业 %s 也进入跟踪" % short_id(event.get("prompt_id"))
    elif etype == "cached":
        text = "作业 %s 命中缓存" % short_id(event.get("job_id"))
    elif etype == "output":
        images = (event.get("outputs") or {}).get("images") or []
        text = "作业 %s 产出了 %d 张图" % (short_id(event.get("job_id")),
                                           len(images))
    else:
        text = "事件 %s" % etype
        detail = str(event)
    return level, text, detail


def note_event(event):
    """`events.Broker` 的 tap。**永远不抛** —— 记录失败不能影响事件分发。"""
    try:
        row = classify_event(event)
        if row is None:
            return None
        level, text, detail = row
        return BUFFER.append(level=level, source=event.get("type"),
                             text=text, detail=detail, kind=KIND_EVENT,
                             event_type=event.get("type"))
    except Exception:                                  # noqa: BLE001
        return None
