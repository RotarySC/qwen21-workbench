"""ComfyUI 客户端 —— 同步 HTTP + WS 地址 + 模型可见性体检。

**刻意同步**（`httpx.Client`）。理由：调用都在本机、通常 5 ms 级；异步化只会
把「谁在哪个线程」这件事搅浑。真正慢的两处另有安排 ——
`/view` 取图是整块读（单用户本机，2K 图约 10 MB，值得换来简单），
出图的等待靠 `Watcher` 轮询而不是长连接。

WS 本身不在这里：`websocket-client` 是阻塞式的，跑在 `watcher.py` 的线程里，
这样能用一个 `settimeout(1.0)` 把「读消息」和「轮询 /history」交织在同一个循环。
"""
import mimetypes
import uuid

import httpx

from . import config


class ComfyUnreachable(RuntimeError):
    """连不上 ComfyUI —— 与「连上了但图不合法」是两回事，不要混。"""


class ComfyUploadFailed(RuntimeError):
    """后端**在答话**，但这次上传它没接（HTTP 非 200 / 响应体不是我们认得的形状）。

    与 `ComfyUnreachable` 分开：一个是「它说不」，一个是「它没答」。上传的调用方
    对两者的处置相同（退回本地写），但日志与报错里必须能分清 ——
    「后端没起」与「后端不认这个接口」是两条完全不同的排查路线。
    """


class ComfyRejected(RuntimeError):
    """ComfyUI 拒绝了这份图（校验失败）。`node_errors` 里有逐节点的原因。"""

    def __init__(self, message, node_errors=None, raw=None):
        super().__init__(message)
        self.message = message
        self.node_errors = node_errors or {}
        self.raw = raw


def _queue_item_id(item):
    """队列项结构：(number, prompt_id, prompt, extra_data, outputs_to_execute)。"""
    if isinstance(item, (list, tuple)) and len(item) > 1:
        return item[1]
    return None


# /upload/image 的 boundary。**每次调用都换一个**：固定串在正文里偶然出现
# （参考图是二进制，什么字节都可能撞上）就会把这一段截断。uuid4 的十六进制
# 撞上 32 字节随机序列的概率可以忽略，而换一次的代价是零。
_BOUNDARY_PREFIX = "----workbench-upload-"


def multipart_body(fields=(), files=(), boundary=None):
    """手搓 `multipart/form-data`，返回 `(content_type, body_bytes)`。

    为什么自己拼而不上 `python-multipart`：那个包只在**解析**端需要，我们这里是
    **生成**端。为了发一个文件把「零安装」的底座变成「要装」不划算 —— 形态是固定的
    （几个短字段 + 一个文件），拼起来十几行。

    `files` 的每项是 `(字段名, 文件名, 字节, MIME)`。文件名按 UTF-8 原样写进
    `Content-Disposition`：**实测**（curl -F + 中文名）aiohttp 那边收得到
    `\u63a2\u9488_...png`，磁盘上也是原名，所以不需要 RFC 2231 那套 `filename*=`
    绕法。文件名里的引号与反斜杠由 `upload.safe_name` 在更上游挡掉。
    """
    boundary = boundary or (_BOUNDARY_PREFIX + uuid.uuid4().hex)
    out = bytearray()
    for name, value in fields:
        out += ("--%s\r\n" % boundary).encode("ascii")
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n'
                % name).encode("utf-8")
        out += str(value).encode("utf-8") + b"\r\n"
    for field, filename, blob, content_type in files:
        out += ("--%s\r\n" % boundary).encode("ascii")
        out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (field, filename)).encode("utf-8")
        out += ("Content-Type: %s\r\n\r\n" % content_type).encode("ascii")
        out += bytes(blob) + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode("ascii")
    return "multipart/form-data; boundary=%s" % boundary, bytes(out)


class ComfyClient:
    """注意 `trust_env=False`：本机 `NO_PROXY` 里有一个不带方括号写法的
    `[::1]`，httpx 把它当 URL 解析会直接抛 `InvalidURL: Invalid port: ':1]'`
    —— 也就是说**连 httpx.Client() 都构造不出来**，不是请求失败而是构造失败。
    实测：`trust_env=True` 必炸，`trust_env=False` 正常。

    即便没有这个坑也该关掉：工作台 → ComfyUI 是回环调用，任何时候都不该
    经过系统代理。
    """
    def __init__(self, base=None, ws_base=None, timeout=None):
        self.base = (base or config.COMFY_BASE).rstrip("/")
        self.ws_base = ws_base or config.COMFY_WS
        self.timeout = timeout or config.HTTP_TIMEOUT
        self._client = httpx.Client(base_url=self.base, timeout=self.timeout,
                                    headers={"Accept": "application/json"},
                                    trust_env=False)

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def ws_url(self, client_id):
        return "%s?clientId=%s" % (self.ws_base, client_id)

    # --- 探活 -----------------------------------------------------------
    def probe(self):
        """三态探活。**别把「没答好」当成「没起」**。

        - `True`  200，健康
        - `False` 连接被拒 —— 这是唯一可以说「没起」的情形
        - `None`  连上了，但没干净答完（超时 / 5xx）

        第三态是实测逼出来的：ComfyUI 冷加载 16 GiB 权重时，`/system_stats`
        能超过 2 秒不答。早先这里一律返回 False，于是**正在跑任务的**后端被
        报成「未运行」，页面提示用户去启动一个已经启动着的东西 —— 而且此时
        提交会被自己的检查挡掉 503，正好在用户最想排队的时候。

        5xx 也算第三态：它在答话，说明进程活着，只是这个请求不行。
        """
        # **连接**与**读取**用两个不同的上限，这不是随手写的：
        # 实测本机连一个没人监听的回环端口，`WSAECONNREFUSED` 要 ~2.1 秒才回来
        # （raw socket / .NET / httpx 三路一致）。若连接也用 2 秒，就会卡在拒绝
        # 到达之前超时 —— 「连接被拒」于是被误判成「没答好」，而后者不算没起，
        # 结果就是 probe() 永远不返回 False：页面永远说不出「后端未运行」，
        # 一键启动也会因为「端口似乎被占」而永远拒绝。
        # 后端真的在跑时这条用不到（实测 50 毫秒）：它只在「可能没起」时才付出代价。
        timeout = httpx.Timeout(connect=config.PROBE_CONNECT_TIMEOUT,
                                read=config.PROBE_TIMEOUT,
                                write=config.PROBE_TIMEOUT,
                                pool=config.PROBE_TIMEOUT)
        try:
            r = self._client.get("/system_stats", timeout=timeout)
        except httpx.ConnectError:
            return False
        except httpx.HTTPError:
            return None
        return True if r.status_code == 200 else None

    def liveness(self):
        """给页面用的字符串状态：`ok` / `down` / `unclear`。

        比布尔多一个值是有意义的 —— 「没起」要用户去拉后端，
        「没答好」要用户等一下，两件事的处置完全不同。
        """
        value = self.probe()
        if value is True:
            return "ok"
        return "down" if value is False else "unclear"

    def running(self):
        """还留着的老接口：**只有确定连不上**才说 False。

        「没答好」（`None`）算「在跑」—— 进程确实活着。
        """
        return self.probe() is not False

    def system_stats(self):
        try:
            return self._client.get("/system_stats").json()
        except (httpx.HTTPError, ValueError):
            return None

    # --- 节点信息与模型体检 ---------------------------------------------
    def object_info(self, node_class):
        try:
            r = self._client.get("/object_info/" + node_class)
            if r.status_code != 200:
                return None
            return r.json()
        except (httpx.HTTPError, ValueError):
            return None

    def combo_options(self, node_class, field):
        """取 COMBO 输入的可选值列表。读不到返回 None（≠ 空列表）。"""
        info = self.object_info(node_class)
        if not isinstance(info, dict):
            return None
        node = info.get(node_class)
        if not isinstance(node, dict):
            return None
        spec = ((node.get("input") or {}).get("required") or {}).get(field)
        if not isinstance(spec, list):
            return None
        # 形状通常是 [ [选项…], {元数据} ]；老一点的版本直接就是 [选项…]
        first = spec[0] if spec else None
        if isinstance(first, list):
            return first
        if spec and all(isinstance(x, str) for x in spec):
            return spec
        return None

    def nodes_report(self, entries=None):
        """可选插件的节点类在后端注册了没有？（判定逻辑见模块函数 `node_presence`）

        `present` 三态与 `models_report` 一致：`None` = **问不到**（后端没起），
        `False` = 后端起了但没这个类（插件没装 / 装了没重启）。
        两种病处置不同 —— 一个去拉后端，一个去装插件再重启。

        `entries` 是**调用方按链路换掉的那张表**，形状同 `config.REQUIRED_NODES`
        （`(key, 节点类, 目录名)`），与 `models_report(extra=…)` 同一个用法：
        H3 与 See-through 各有自己的插件节点，拿 Qwen 那张表去体检它们
        只会刷出一屏假警报 —— 而假警报会让真警报失效。
        """
        return node_presence(self.object_info, entries)

    def models_report(self, entries=None):
        """铁律 1 的直接体检：模型文件在后端的候选表里吗？

        `present=None` 表示**读不到候选表**（后端没起 / 节点不存在），
        与 `present=False`（后端起了但这个文件不可见）是两种病，UI 要分开说。

        **`entries` 是「这份体检要查哪几个文件」的整份表**，形状与
        `config.REQUIRED_MODELS` / `node_presence` 的 `entries` 一样
        （`(key, 节点类, 字段, 文件名)`）；`None` = 就查 `config.REQUIRED_MODELS`
        那四个。

        为什么是**整份替换**而不是「追加几个」：五条链路各有自己那套权重
        （Qwen 四个、H3 五个、拆层两个目录），而**互相之间一个都不用**。
        拿 Qwen 那四个去体检一次拆层，会刷出四条「后端看不到这些模型文件」——
        全是假警报，而假警报会让真警报失效。要追加就在**调用方**把两张表接起来
        （`config.REQUIRED_MODELS + turbo_extra`），组合规则只有一处。
        """
        source = config.REQUIRED_MODELS if entries is None else tuple(entries)
        cache = {}
        out = {}
        for key, node_class, field, filename in source:
            ck = (node_class, field)
            if ck not in cache:
                cache[ck] = self.combo_options(node_class, field)
            options = cache[ck]
            if options is None:
                out[key] = {"node_class": node_class, "field": field,
                            "filename": filename, "present": None,
                            "option_count": None,
                            "note": "读不到 %s 的 %s 候选表" % (node_class, field)}
            else:
                out[key] = {"node_class": node_class, "field": field,
                            "filename": filename,
                            "present": filename in options,
                            "option_count": len(options), "note": None}
        return out

    # --- 参考图上传 -----------------------------------------------------
    def upload_image(self, name, data, overwrite=True, subfolder="", type_="input"):
        """把字节交给 **ComfyUI 自己**写进它读的那个 `input/`（`POST /upload/image`）。

        这是参考图上传的**首选**路径，理由有两条，都不是「顺手」：

        1. **只有它写得了。** 工作台跑在**工作区**解释器上，而本机那条沙箱规则
           只按**可执行文件的位置**判定 —— 实测同一个目录，工作区解释器
           `PermissionError`、ComfyUI 自己的解释器写得进去（三路隔离验过，
           见 `workbench/upload.py` 顶部）。让写盘发生在 ComfyUI 进程里，
           这条限制就绕开了。
        2. **落点不再需要猜。** 本机的 `input/` 和 `output/` 一样有双胞胎
           （安装目录 / ComfyUI-Shared），写错的那个是**静默**失败。让 ComfyUI
           按它自己的配置落盘，就没有「我写到哪了」这个问题。

        返回后端自己的回执 `{"name", "subfolder", "type"}`。注意 `name` **可能
        不是**我们递进去的那个：`overwrite=false` 遇到同名时，ComfyUI 会改成
        `name (1).png` 而不是拒绝（`server.py::image_upload`）—— 所以调用方必须
        以返回值里的名字为准，别拿自己那份当真。
        """
        content_type, body = multipart_body(
            fields=[("overwrite", "true" if overwrite else "false"),
                    ("subfolder", subfolder or ""),
                    ("type", type_ or "input")],
            files=[("image", name, data,
                    mimetypes.guess_type(name, strict=False)[0]
                    or "application/octet-stream")])
        try:
            r = self._client.post("/upload/image", content=body,
                                  headers={"Content-Type": content_type},
                                  timeout=config.UPLOAD_TIMEOUT)
        except httpx.TimeoutException as exc:
            raise ComfyUnreachable(
                "上传失败：ComfyUI 没在 %s 秒内回应（它可能正忙着出图）：%s"
                % (config.UPLOAD_TIMEOUT, exc))
        except httpx.HTTPError as exc:
            raise ComfyUnreachable("上传失败，连不上 ComfyUI：%s" % exc)
        if r.status_code != 200:
            raise ComfyUploadFailed("ComfyUI 拒绝了这次上传（HTTP %d）：%s"
                                    % (r.status_code, (r.text or "")[:200]))
        try:
            info = r.json()
        except ValueError:
            raise ComfyUploadFailed("ComfyUI 的上传响应不是 JSON（HTTP %d）"
                                    % r.status_code)
        if not isinstance(info, dict) or not info.get("name"):
            raise ComfyUploadFailed("ComfyUI 的上传响应里没有文件名：%r" % (info,))
        return info

    # --- 队列与历史 -----------------------------------------------------
    def submit(self, workflow, job_id, client_id):
        """提交。`job_id` 由调用方铸成规范 UUID 并当 `prompt_id` 用。

        `client_id` 必须和 WS 连接用的一致 —— ComfyUI 在
        `execution.py:736` 用**提交时**的 `extra_data["client_id"]` 去设
        `server.client_id`，而 `execution_start/cached/success/executed`
        只在 `server.client_id` 非空时发出。
        """
        body = {"prompt": workflow, "client_id": client_id, "prompt_id": job_id}
        try:
            r = self._client.post("/prompt", json=body)
        except httpx.TimeoutException as exc:
            # 连上了但没及时答完 ≠ 连不上。**实测**：跑 2048² 时 ComfyUI 自己的
            # HTTP 也会被占住（4 MP 的单次前向约 25 秒，事件循环被饿着）——
            # 那时 `/queue` 五秒都答不出来，GPU 却是 100% 在算。
            raise ComfyUnreachable(
                "提交失败：ComfyUI 没在 %s 秒内回应。它**多半正忙着**（不是没起）——"
                "正在跑 2048² 那种大图时，它自己的 HTTP 接口也会被占住十几二十秒。"
                "等手上这张跑完再提交。" % config.HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ComfyUnreachable("提交失败，连不上 ComfyUI：%s" % exc)
        try:
            data = r.json()
        except ValueError:
            raise ComfyUnreachable("提交失败，/prompt 返回了非 JSON（HTTP %d）"
                                   % r.status_code)
        if r.status_code != 200 or not isinstance(data, dict) or "prompt_id" not in data:
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                msg = err.get("message") or err.get("type") or "提交被拒绝"
                details = err.get("details")
                if details:
                    msg = "%s（%s）" % (msg, details)
            elif isinstance(err, str):
                msg = err
            else:
                msg = "提交被拒绝（HTTP %d）" % r.status_code
            raise ComfyRejected(msg, (data or {}).get("node_errors"), data)
        return data

    def queue(self):
        try:
            return self._client.get("/queue").json()
        except (httpx.HTTPError, ValueError):
            return None

    def queue_ids(self):
        """`{prompt_id: "running"|"pending"}`；**读不到时返回 `None`**。

        这个 `None` 很要紧：`Watcher._poll` 靠它区分「队列是空的」和「我不知道
        队列长什么样」。混成 `{}` 的话，后端网络抖一下，所有在跑的任务会被
        当成「既不在队列也查不到历史」而判死。
        """
        q = self.queue()
        if not isinstance(q, dict):
            return None
        out = {}
        for item in q.get("queue_running") or []:
            pid = _queue_item_id(item)
            if pid:
                out[pid] = "running"
        for item in q.get("queue_pending") or []:
            pid = _queue_item_id(item)
            if pid and pid not in out:
                out[pid] = "pending"
        return out

    def history(self, prompt_id=None):
        path = "/history" if prompt_id is None else "/history/" + prompt_id
        try:
            return self._client.get(path).json()
        except (httpx.HTTPError, ValueError):
            return None

    # --- 取消 -----------------------------------------------------------
    def cancel(self, job_id, running=False):
        """`running` 时走定向 interrupt，否则从待跑队列里删。两个都发也无害。"""
        did = []
        if running:
            try:
                self._client.post("/interrupt", json={"prompt_id": job_id})
                did.append("interrupt")
            except httpx.HTTPError:
                pass
        try:
            self._client.post("/queue", json={"delete": [job_id]})
            did.append("queue-delete")
        except httpx.HTTPError:
            pass
        return did

    # --- 取图 -----------------------------------------------------------
    def fetch_view(self, filename, subfolder="", type_="output"):
        """取图字节。走 ComfyUI 的 `/view` 而不是自己拼磁盘路径 ——
        不必猜 output 目录在哪（Desktop 与手动启动不一样），也不会猜错。"""
        params = {"filename": filename, "subfolder": subfolder or "",
                  "type": type_ or "output"}
        try:
            r = self._client.get("/view", params=params, timeout=config.VIEW_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ComfyUnreachable("取图失败，连不上 ComfyUI：%s" % exc)
        if r.status_code != 200:
            raise ComfyUnreachable("取图失败，/view 返回 HTTP %d" % r.status_code)
        return r.headers.get("content-type", "image/png"), r.content


# --- 可选插件的节点体检（纯函数，与传输分开）-------------------------------
def node_presence(object_info, entries=None):
    """把「怎么判定某个节点类在不在」单独拿出来。

    为什么要抽成模块函数：`FakeComfy` 是**另写一份**的替身，不是 `ComfyClient`
    的子类。判定逻辑要是两边各写一份，测试就成了「拿自己的假设验自己的实现」——
    真后端哪天改了判据，它一声不响。抽出来之后两边共用同一份判定，只有「怎么问」
    不同（真：HTTP；假：内存）。

    判据是**实测**的：真后端（8188）对**存在**的类回 `200 + {类名: {...}}`，
    对**不存在**的类回 **`200 + {}`**（不是 404）。所以「类不在返回的字典里」
    才是「后端没有这个类」；返回的根本不是 dict 才是「问不到」（后端没起）。
    """
    out = {}
    for key, node_class, folder in (config.REQUIRED_NODES if entries is None
                                    else entries):
        info = object_info(node_class)
        if not isinstance(info, dict):
            out[key] = {"node_class": node_class, "folder": folder,
                        "present": None,
                        "note": "读不到 %s 的节点信息" % node_class}
        elif node_class not in info:
            out[key] = {"node_class": node_class, "folder": folder,
                        "present": False,
                        "note": "后端没有这个节点类（插件没装，或装了没重启）"}
        else:
            out[key] = {"node_class": node_class, "folder": folder,
                        "present": True, "note": ""}
    return out


# --- 结果解析（纯函数，便于测试）----------------------------------------
def outputs_from_history(entry):
    """从 `/history/<id>` 的条目里抽出图片产物列表。

    对畸形输入一律返回空表而不是抛异常 —— 这是外部数据，一个坏掉的 history
    条目不该让轮询线程整个崩掉。
    """
    images = []
    if not isinstance(entry, dict):
        return images
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return images
    for node_id, out in outputs.items():
        if not isinstance(out, dict):
            continue
        for img in out.get("images") or []:
            if not isinstance(img, dict) or not img.get("filename"):
                continue
            images.append({
                "node_id": node_id,
                "filename": img["filename"],
                "subfolder": img.get("subfolder", "") or "",
                "type": img.get("type", "output") or "output",
            })
    return images


def history_status(entry):
    """`(status, error)`：把 history 条目归一成工作台的终态。"""
    if not isinstance(entry, dict):
        return None, None
    status = entry.get("status") or {}
    messages = status.get("messages") or []
    for event, data in messages:
        if not isinstance(data, dict):
            continue
        if event == "execution_error":
            node_id = data.get("node_id")
            # 官方负载里直接带 node_type（`execution.py:704`）；prompt 反查是兜底
            node_type = data.get("node_type")
            if node_type is None:
                prompt = entry.get("prompt")
                if (isinstance(prompt, list) and len(prompt) > 2
                        and isinstance(prompt[2], dict)):
                    node_type = (prompt[2].get(node_id) or {}).get("class_type")
            msg = data.get("exception_message") or data.get("exception_type") or "执行出错"
            return "error", {"message": msg, "node_id": node_id,
                             "node_type": node_type,
                             "traceback": (data.get("traceback") or [])[-6:]}
        if event == "execution_interrupted":
            return "cancelled", None
    if status.get("completed"):
        return "done", None
    if status.get("status_str") == "error":
        return "error", {"message": "执行出错（history 未给出细节）"}
    return None, None


def history_timestamps(entry):
    """从 history 的 messages 里取 (start_ms, end_ms)。官方时间戳，比我们的钟准。"""
    if not isinstance(entry, dict):
        return None, None
    messages = ((entry.get("status") or {}).get("messages") or [])
    start = end = None
    for event, data in messages:
        ts = data.get("timestamp") if isinstance(data, dict) else None
        if event == "execution_start" and ts:
            start = ts
        elif event in ("execution_success", "execution_error", "execution_interrupted") and ts:
            end = ts
    return start, end
