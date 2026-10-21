#!/usr/bin/env python3
"""真的把工作台渲染出来看一眼 —— headless Chromium + CDP，纯标准库。

为什么要它
----------
AGENTS.md 长期挂着一条：「工作台的页面渲染没被真正看过」。之所以一直没看，
原因写在文档里的那句是「没有浏览器驱动接在这个 agent 上」—— **那不是平台做不到，
是没人把驱动交过来**。而 Chromium 就在磁盘上，`--headless` + CDP 也根本不需要
第三方驱动：CDP 是个 WebSocket 协议，用标准库自己接一个最小客户端就够了。

这一层补的是**别人补不了的那一段**：
  * `test_frontend_form.py` / `test_frontend_views.py` 用假 DOM 验**逻辑** ——
    它们量的滑块位置是我「喂」进去的，不知道真布局落在哪；
  * `test_api.py::FrontendContract` 验**一致性**（id / SSE 名字 / 路由 / 语法）；
  * 只有真浏览器能回答「布局对不对、东西挡没挡住、缩略图真的出来了吗」。

它验的六件事
------------
1. **滑块真的对位** —— 量 `#result-thumb` 与选中标签的 `getBoundingClientRect`，
   两者必须重合（假 DOM 里这一条永远验不了，那里没有布局引擎）。
2. **虚线框真的能选文件 / 拖进来** —— 两条路各走一次：
   `DOM.setFileInputFiles` 走的是**真的** `<input type=file>`（与用户点选择器
   完全同一个入口），拖放则是造一个真 `DataTransfer` 派发 `drop`。
   两条都走完整的 `uploadRefs()` → `/api/upload` → 后端候选表回读核对。
   2026-10-01 起还要验**累积**：第二批选完，第一批必须还在。
3. **缩略图真的画得出来** —— 它走后端 `/view`，所以 `naturalWidth > 0` 本身
   就证明后端认得那张图。
4. **反向词的门槛真的出现在页面上** —— CFG=1.0 时标题写「填了，但不生效」、
   红色警告在、按钮在；点一下那个按钮之后变成「生效中」。
5. **画廊的三个入口与放大视图** —— 点图开模态 `<dialog>`（`matches(":modal")`、
   焦点锁在里面、背后的元素 focus 不动）、点一下切 1:1、复制提示词有回话、
   `←/→` 换张、**Esc 用真按键关得掉**（模态对话框的 Esc 由浏览器处理，
   页面里造一个 `KeyboardEvent` 根本关不掉 —— 那样验出来的「能关」是假的）。
   2026-10-01 起顺带量「页面上还有多少字」：格子**只剩一行小标签**（参数 /
   提示词 / 来源那三行按用户要求去掉了）、提交前那一栏不再显示估时、
   说明性的 info 也不再上页面。
6. **`×` 只在悬停或聚焦时出现**（2026-10-01）—— 量的是**计算样式里的
   `opacity`**：悬停前 0、悬停后 1、鼠标挪开而用键盘聚焦它时也是 1。它**一直在
   DOM 里**（藏起来又够不着，等于这个键不存在），所以「元素在不在」验不出这件
   事；而 `:hover` 只能靠 CDP 真的挪鼠标过去 —— 在页面里 `dispatchEvent` 造不出
   浏览器给的伪类。

用法
----
    # 需要工作台已经在跑（默认 127.0.0.1:8189）
    %LOCALAPPDATA%\\qwen21-venv\\Scripts\\python.exe tests/page_shot.py --out <输出目录>

**照出来的图必须用眼睛看。** 脚本只能量「元素在不在、位置对不对」，
好不好看、有没有挤在一起，它一概不知道。
"""
import argparse
import base64
import glob
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# 工作区（运维现场）在仓库的上一层 —— 上传用的真图从那里的 output/ 取。
WORKSPACE = os.path.dirname(REPO)
DEFAULT_OUT = os.path.join(REPO, "state", "page_shots")


def pick_samples(count=2):
    """挑 `count` 张**真实存在**的 PNG，当「用户在选择器里挑的文件」。

    刻意用真图而不是现造一个 1×1 的假 PNG：上传之后这张图还要**真的预览出来**
    （`naturalWidth > 0`），而假图会把「后端读得到」这条验成一句空话。
    取最小的那几张 —— 这个脚本的目的不是压测上传带宽。
    """
    found = []
    for pattern in (os.path.join(WORKSPACE, "output", "*.png"),
                    os.path.join(WORKSPACE, "images", "*.png")):
        found.extend(glob.glob(pattern))
    found = sorted(set(found), key=lambda p: os.path.getsize(p))
    return found[:count]

# Chromium 优先（playwright 那份最可控），Edge 兜底。**路径要能找得到** ——
# 找不到就明确说「没有浏览器」，而不是让后面报一个看不懂的连接错误。
CHROME_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA") or "", "ms-playwright",
                 "chromium-1228", "chrome-win64", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA") or "", "ms-playwright",
                 "chromium-1217", "chrome-win64", "chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]

# 拖放那一步的落名。**源图不写死**：用第 4 步刚上传的那张 ——
# 它一定在后端**当前**的 input/ 里。写死一个安装目录里的固定夹具会在受限模式下
# 必然取不到（那时后端的 input/ 在工作区，见 launch.py 的受限模式），
# 而取不到就该炸 —— 静默跳过等于把这一步变成空转的绿。
DROP_NAME = "via_drop_test.png"
NEGATIVE_TEXT = "blurry, low quality, watermark"

# 从本页拖一张图时，**浏览器给的文件名**。实测（真 CDP 拖放，见
# `logs/gallery_drag_probe.py` 的 `page_saw`）它按「URL 末段 + MIME 后缀」合成 ——
# `/api/gallery/image?root=0&name=X.png` 得到的就是这个与 X 毫无关系的 `image.png`。
# 产品**不许**吃这个名字（吃了就会每张都往同一个名字上覆盖、列表按名字去重后
# 永远只有一张），所以这个字符串在下面那条判据里是「反例」的角色。
BROWSER_DRAG_NAME = "image.png"

# 从画廊拖图那一步用的页面工具。三件事：算 sha256、按**实测到的形状**造一次拖放、
# 把列表里每一格的缩略图与「我拖的那张」比字节。
#
# 为什么字节要**真的按那个 URL 取回来**：这样「传上去的是不是这一张」才是可判的
# （比 sha256），而不是「看起来有图」。名字则**故意写成浏览器那个错的** ——
# 只要产品还在用文件名，这条判据就会红。
#
# P20l 起格子上那个 `src` 是**缩略图**（`/api/gallery/thumb`），所以这里记**两份**
# 哈希：拖的那个地址（缩略图那一份，反例）与它对应的**原图**（`/api/gallery/image`）。
# 落地的必须是后者 —— 拿 512px 的 JPEG 当参考图是**没有症状**的降级。
JS_GALLERY_DRAG = r"""
(() => {
  window.__sha = async (url) => {
    const r = await fetch(url, {cache: "no-store"});
    const d = await crypto.subtle.digest("SHA-256", await r.arrayBuffer());
    return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
  };
  window.__dragged = {};
  window.__original = {};
  window.__dragFromGallery = async (index, browserName) => {
    const img = document.querySelectorAll("#gallery .tile img")[index];
    const src = img.getAttribute("src");
    const abs = new URL(src, location.href).toString();
    const blob = await (await fetch(abs)).blob();
    const dt = new DataTransfer();
    dt.items.add(new File([blob], browserName, {type: blob.type || "image/png"}));
    dt.setData("text/uri-list", abs);
    dt.setData("text/html", '<img src="' + src + '">');
    const box = document.getElementById("ref-drop");
    for (const t of ["dragenter", "dragover", "drop"]) {
      box.dispatchEvent(new DragEvent(t,
        {bubbles: true, cancelable: true, dataTransfer: dt}));
    }
    const p = new URL(abs).searchParams;
    const real = p.get("name");
    const origAbs = location.origin + "/api/gallery/image?root=" +
      encodeURIComponent(p.get("root")) + "&name=" + encodeURIComponent(real);
    const thumbSha = await window.__sha(abs);
    const origSha = await window.__sha(origAbs);
    window.__dragged[real] = thumbSha;
    window.__original[real] = origSha;
    return {source: src, realName: real, fileFromBrowser: browserName,
            size: blob.size, originalSize: (await (await fetch(origAbs)).blob()).size,
            thumbsAreTheSame: thumbSha === origSha};
  };
  window.__chipReview = async () => {
    const out = [];
    for (const c of document.querySelectorAll("#ref-picked .ref-chip")) {
      const thumb = c.querySelector(".ref-thumb");
      const sha = await window.__sha(thumb.getAttribute("src"));
      out.push({name: c.dataset.name,
                natural: thumb.naturalWidth + "x" + thumb.naturalHeight,
                sha: sha.slice(0, 10),
                sameAsDragged: window.__dragged[c.dataset.name] === sha,
                sameAsOriginal: window.__original[c.dataset.name] === sha});
    }
    return out;
  };
  return true;
})()
"""

# `×` 那一步要量的东西：它**一直在 DOM 里**（键盘 Tab 才够得到它），变的是计算
# 样式里的 `opacity` —— 所以判据是 opacity，不是「元素在不在」。`rendered`
# 顺带把「有没有被 display:none 之类摘掉」也量进去：那正是这一条最怕的回归。
REF_REMOVE_STATE = """(() => {
  const b = document.querySelector("#ref-picked .ref-remove");
  if (!b) return null;
  const cs = getComputedStyle(b);
  return {opacity: Number(cs.opacity), visibility: cs.visibility,
          display: cs.display, rendered: b.offsetParent !== null,
          tabIndex: b.tabIndex,
          // 这条媒体查询要是**不**成立（无悬停设备），`×` 就该常显 —— 把它一起
          // 记下来，免得把「这台机器没有悬停」误读成「隐藏没生效」。
          hoverMedia: matchMedia("(hover: hover) and (pointer: fine)").matches};
})()"""


def find_chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    for name in ("chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（客户端帧必须加掩码，服务端的不加）
# ---------------------------------------------------------------------------
class WS:
    def __init__(self, url, timeout=60.0):
        parts = urlparse(url)
        self.sock = socket.create_connection((parts.hostname, parts.port or 80),
                                             timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (("?" + parts.query) if parts.query else "")
        self.sock.sendall((
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
            % (path, parts.hostname, parts.port or 80, key)).encode())
        self.buf = bytearray()
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手时对端就关了")
            self.buf += chunk
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败：%r" % head[:200])
        self.buf = bytearray(rest)

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 被对端关掉了")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send(self, text):
        self._frame(0x1, text.encode("utf-8"))

    def recv(self):
        """收一整条消息。CDP 的大消息（截图）**会分片**，所以要拼到 FIN。"""
        data = b""
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode, masked = b1 & 0x80, b1 & 0x0F, b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(n)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("WebSocket 对端要求关闭")
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            data += payload
            if fin:
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def http_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Page:
    """一个 headless 页面 + 它的 CDP 连接。"""

    def __init__(self, chrome, url, width, height, profile_dir,
                 keep_profile=False):
        # `profile_dir` 是**调用方指定**的，不用 `tempfile.mkdtemp()` ——
        # 工作区解释器的临时目录会退化到 cwd（AGENTS.md 记过），于是它会往
        # 仓库根目录里丢一个 `qwen21-page-xxxx/`，实测丢过。
        self.profile = profile_dir
        self.keep_profile = keep_profile
        os.makedirs(self.profile, exist_ok=True)
        self.proc = subprocess.Popen(
            [chrome, "--headless=new",
             # `--no-sandbox` 不是可选项。playwright 那份 chromium **直接**运行
             # 而没带沙箱参数时会立刻退出（实测 exit 0x80000003
             # = STATUS_BREAKPOINT，且不写 DevToolsActivePort）；加上它就好了。
             # 这是本机、无头、只连 127.0.0.1 的一次性实例，可以接受。
             "--no-sandbox",
             "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--hide-scrollbars", "--force-device-scale-factor=1",
             "--remote-debugging-port=0",
             # `--user-data-dir` 必须给：一来不与用户自己开着的浏览器抢会话
             # （实测不给的话它会直接「转交」给已有的那个进程然后立刻退出），
             # 二来不改它就想去写 %TEMP%，那种失败看起来像别的问题。
             "--user-data-dir=" + self.profile,
             "--window-size=%d,%d" % (width, height),
             # 起在 about:blank，导航交给 CDP 显式做 —— 把 URL 当启动参数传给
             # `--headless=new` 时，`/json/list` 里先冒出来的可能是那个空白页，
             # 于是后面所有 `getElementById` 都拿到 null（实测踩过）。
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._id = 0
        # CDP **事件**（没有 id 的消息）的落脚处。`call()` 会把它们攒在这儿 ——
        # 不攒就会丢：`call()` 只认自己那个 id，别的一律跳过，而
        # `Input.dragIntercepted`（原生拖放的数据）恰恰就是一个事件。
        self.events = []
        port = self._wait_for_port()
        target = self._wait_for_page(port)
        self.ws = WS(target["webSocketDebuggerUrl"])
        self.call("Page.enable")
        self.call("Runtime.enable")
        # 视口固定住，否则截图尺寸会随窗口装饰漂移。
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height,
                   "deviceScaleFactor": 1, "mobile": False})
        self.call("Page.navigate", {"url": url})

    def _wait_for_port(self, timeout=40):
        path = os.path.join(self.profile, "DevToolsActivePort")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("浏览器起来了又退出（exit %s）"
                                   % self.proc.returncode)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    first = f.readline().strip()
                if first:
                    return int(first)
            time.sleep(0.2)
        raise TimeoutError("等不到 DevToolsActivePort（%s）" % path)

    def _wait_for_page(self, port, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                targets = http_json("http://127.0.0.1:%d/json/list" % port)
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
                continue
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t
            time.sleep(0.2)
        raise TimeoutError("没有可用的 page target")

    def call(self, method, params=None, timeout=60):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result") or {}
            if "id" not in msg:            # 事件：留着给 wait_event，别丢
                self.events.append(msg)
        raise TimeoutError("CDP %s 没答" % method)

    def _pump(self, seconds):
        """收一小会儿消息，事件进 `self.events`，回执丢掉。

        需要它是因为 `recv()` 是阻塞的：想「等一个事件」就得先能**非阻塞地**
        把已经到达的消息收进来，否则只能在一条消息上死等。
        """
        old = self.ws.sock.gettimeout()
        try:
            self.ws.sock.settimeout(seconds)
            try:
                while True:
                    msg = json.loads(self.ws.recv())
                    if "id" not in msg:
                        self.events.append(msg)
            except (TimeoutError, OSError):
                return                        # 到点了 / 连接没了
        finally:
            self.ws.sock.settimeout(old)

    def wait_event(self, method, timeout=15):
        """等一个 CDP 事件（没有 id 的消息）。"""
        deadline = time.time() + timeout
        while True:
            for i, msg in enumerate(self.events):
                if msg.get("method") == method:
                    return self.events.pop(i).get("params") or {}
            if time.time() > deadline:
                raise TimeoutError("等不到 CDP 事件 %s（收到过 %s）"
                                   % (method, [m.get("method") for m in self.events]))
            self._pump(0.2)

    def center_of(self, selector):
        """元素中心点的视口坐标（先把它滚进视野）。"""
        point = self.eval("""(() => {
          const el = document.querySelector(%s);
          if (!el) return null;
          el.scrollIntoView({block: "center"});
          const r = el.getBoundingClientRect();
          return [Math.round(r.left + r.width / 2),
                  Math.round(r.top + r.height / 2)];
        })()""" % json.dumps(selector))
        if not point:
            raise RuntimeError("找不到元素：%s" % selector)
        return point

    def native_drag(self, src_selector, dst_selector):
        """**真·原生拖放**：在源上按下 → 移动（超过阈值）→ 让 Chrome 起拖 →
        把截获的拖动数据重放到目标上，返回那份数据。

        为什么必须走 CDP 的 `Input.setInterceptDrags`，而不是在页面里造一个
        `DataTransfer` 再把「文件」塞进去：原生拖放里 `DataTransfer` 的内容
        —— **尤其 `files` 里那个 File 叫什么名字** —— 是**浏览器自己造的**。
        自己造一份等于把「浏览器会给什么」这个问题提前假设掉，而这一条要验的
        恰恰就是它（实测：从页面里拖一张 `<img>`，Chrome 给的 File 名字是
        `image.png`，与那张图真实的名字毫无关系）。
        """
        self.events = []
        self.call("Input.setInterceptDrags", {"enabled": True})
        try:
            sx, sy = self.center_of(src_selector)
            self.call("Input.dispatchMouseEvent",
                      {"type": "mousePressed", "x": sx, "y": sy, "button": "left",
                       "buttons": 1, "clickCount": 1})
            # 分几步挪：一步到位有时不触发「起拖」判定（阈值是几像素）
            for i in range(1, 7):
                self.call("Input.dispatchMouseEvent",
                          {"type": "mouseMoved", "x": sx + i * 5, "y": sy + i * 3,
                           "button": "left", "buttons": 1})
                time.sleep(0.02)
            data = self.wait_event("Input.dragIntercepted", timeout=5).get("data")
            dx, dy = self.center_of(dst_selector)
            for kind in ("dragEnter", "dragOver", "drop"):
                self.call("Input.dispatchDragEvent",
                          {"type": kind, "x": dx, "y": dy, "data": data})
                time.sleep(0.15)
            self.call("Input.dispatchMouseEvent",
                      {"type": "mouseReleased", "x": sx + 30, "y": sy + 18,
                       "button": "left", "buttons": 0, "clickCount": 1})
            return data
        finally:
            self.call("Input.setInterceptDrags", {"enabled": False})

    def eval(self, expr, timeout=60):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": True}, timeout=timeout)
        if r.get("exceptionDetails"):
            detail = r["exceptionDetails"]
            raise RuntimeError("页面里抛了：%s"
                               % json.dumps(detail, ensure_ascii=False)[:600])
        return (r.get("result") or {}).get("value")

    def wait_for(self, expr, timeout=30, what="条件"):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.eval(expr)
            if last:
                return last
            time.sleep(0.25)
        raise TimeoutError("等不到：%s（最后的值 %r）" % (what, last))

    def shot(self, path):
        r = self.call("Page.captureScreenshot",
                      {"format": "png", "captureBeyondViewport": False})
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path

    def set_file_input(self, selector, paths):
        """把**真的文件**塞进一个 `<input type=file>`，就像用户在选择器里点了它。

        这是唯一能验「点击虚线框 → 选文件 → 上传」这条路的手段：文件选择器
        本身是浏览器进程的 UI，脚本点不到，但 `DOM.setFileInputFiles` 走的是
        **同一个**入口 —— 它触发真的 `change` 事件，页面那边的处理函数与用户
        手动选文件时**一模一样**（不是我们 dispatch 一个假 event 在自说自话）。

        用 `backendNodeId` 而不是 `nodeId`：后者会在导航/DOM 变动后失效，
        这个页面一直在重渲染。
        """
        doc = self.call("DOM.getDocument", {"depth": -1, "pierce": True})
        found = self.call("DOM.querySelector", {
            "nodeId": doc["root"]["nodeId"], "selector": selector})
        node = found.get("nodeId")
        if not node:
            raise RuntimeError("DOM 里没有 %s" % selector)
        self.call("DOM.setFileInputFiles",
                  {"files": list(paths), "nodeId": node})
        return True

    def click_selector(self, selector, timeout=10):
        """在元素**中心**派发一次真鼠标点击（`Input.dispatchMouseEvent`）。

        为什么不用 `element.click()`：那只是一个 DOM 事件，**不带用户激活**。
        剪贴板那条路（`navigator.clipboard.writeText`）恰恰要用户激活 —— 用 JS
        点，验的是「没有用户激活时它会退到哪条路」，而不是用户点下去会发生
        什么。两件事都得知道，所以这一层用真鼠标事件。

        `mouseMoved` 也要先发一次：Chromium 的命中测试认「指针最后停在哪」。

        **先把元素滚进视野**（与 `center_of` 同一件事）：左列是个滚动容器，
        元素在 DOM 里有尺寸、`getBoundingClientRect()` 也非零，但它可能**不在
        视野里** —— 这时按它的视口坐标点下去，命中的是那块位置真正画着的东西，
        于是「点了没反应」。2026-10-01 实测撞上：反向词那颗增强按钮在左列下方，
        点下去弹窗没开（而 `wait_for` 只会说「等不到弹窗打开」，指向完全错的地方）。
        """
        box = self.wait_for("""(() => {
          const e = document.querySelector(%s);
          if (!e) return null;
          e.scrollIntoView({block: "center"});
          const r = e.getBoundingClientRect();
          if (!r.width || !r.height) return null;
          return {x: Math.round(r.left + r.width / 2),
                  y: Math.round(r.top + r.height / 2)};
        })()""" % json.dumps(selector), timeout, "元素 %s 可点" % selector)
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent",
                      {"type": kind, "x": box["x"], "y": box["y"],
                       "button": "left", "clickCount": 1})
        time.sleep(0.2)
        return box

    def press_key(self, key, code, key_code):
        """真按键。**Esc 必须走这里**：模态 `<dialog>` 的 Esc 是浏览器自己
        处理的，页面里造一个 `KeyboardEvent` 根本关不掉它 —— 那样验出来的
        「能关」是假的。"""
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent",
                      {"type": kind, "key": key, "code": code,
                       "windowsVirtualKeyCode": key_code,
                       "nativeVirtualKeyCode": key_code})
        time.sleep(0.2)

    def close(self):
        try:
            self.ws.close()
        except Exception:                                   # noqa: BLE001
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.profile and not self.keep_profile:
            shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# 页面操作片段 —— 一律是**立即执行的表达式**（`Runtime.evaluate` 只求值，
# 不会替你调用一个函数字面量）。
# ---------------------------------------------------------------------------
JS_THUMB_VS_TAB = """(() => {
  const tab = document.querySelector('.result-tab[aria-selected="true"]');
  const thumb = document.getElementById("result-thumb");
  if (!tab || !thumb) return null;
  const t = thumb.getBoundingClientRect(), b = tab.getBoundingClientRect();
  return {tab: tab.id,
          thumbLeft: Math.round(t.left), tabLeft: Math.round(b.left),
          thumbWidth: Math.round(t.width), tabWidth: Math.round(b.width),
          dLeft: Math.round(t.left - b.left), dWidth: Math.round(t.width - b.width)};
})()"""


def js_click(eid):
    return "(document.getElementById(%s).click(), true)" % json.dumps(eid)


def js_set_mode(mode):
    return ('(() => { const m = document.getElementById("mode");'
            ' m.value = %s;'
            ' m.dispatchEvent(new Event("change", {bubbles: true}));'
            ' return m.value; })()' % json.dumps(mode))


def js_set_input(eid, value):
    return ('(() => { const el = document.getElementById(%s);'
            ' el.value = %s;'
            ' el.dispatchEvent(new Event("input", {bubbles: true}));'
            ' return el.value; })()' % (json.dumps(eid), json.dumps(value)))


def chip_count(n):
    """选中集里有几块。拖放/上传那条路要等的就是它。"""
    return 'document.querySelectorAll("#ref-picked .ref-chip").length === %d' % n


def js_watch_drop():
    """在页面上挂一个旁观者，记录 drop 事件**有没有真的带着文件落到框上**。

    这一条与「文件写没写进 input/」是**两件事**，必须分开记：
    前者是我们要验的 UI 行为，后者在本机会被沙箱挡住（见 WRITE_BLOCK_NOTE）。
    混在一起记，就会把环境限制误报成拖放坏了。
    """
    return """(() => {
  window.__drop_dbg = [];
  const drop = document.getElementById("ref-drop");
  for (const t of ["dragenter", "drop"]) {
    drop.addEventListener(t, (e) => window.__drop_dbg.push(
      t + ": dt=" + (e.dataTransfer ? "yes" : "null") +
      " files=" + (e.dataTransfer && e.dataTransfer.files
                   ? e.dataTransfer.files.length : "-") +
      " name=" + (e.dataTransfer && e.dataTransfer.files[0]
                  ? e.dataTransfer.files[0].name : "-")), true);
  }
  return true;
})()"""


WRITE_BLOCK_NOTE = (
    "工作台跑在**工作区里**的解释器上，而它写不了 %LOCALAPPDATA%。"
    "三路隔离实测：工作区解释器 FAILED、ComfyUI 自己的解释器（在工作区外）"
    "WROTE、会话 pwsh WROTE —— 所以这是环境限制（AGENTS.md 铁律 2），"
    "不是拖放或上传的缺陷。要让它真的 PASS，得让工作台跑在工作区外的解释器上，"
    "而 ComfyUI 那个 venv 里没有 starlette/uvicorn。")


def js_drop_file(src_name, new_name):
    """造一个**真的** DataTransfer 派发 drop。

    为什么不用 `dt.files = [...]`：那个 setter 只接受 FileList，而 FileList
    没法从 JS 造。`items.add()` 是唯一能真的往 DataTransfer 里塞 File 的路。

    这一条验的是整条拖放链路 —— `dragenter` → `drop` → `uploadRef(f)` →
    `/api/upload` → 后端候选表回读核对。以前它是 SKIP（「写不了 input/」），
    而现在连「拖进去」这个动作本身也一起验了。
    """
    return """(async () => {
  const res = await fetch("/api/image?filename=" + encodeURIComponent(%s) +
                          "&subfolder=&type=input");
  if (!res.ok) throw new Error("取源图失败 HTTP " + res.status);
  const blob = await res.blob();
  const file = new File([blob], %s, {type: blob.type || "image/png"});
  const dt = new DataTransfer();
  dt.items.add(file);
  const drop = document.getElementById("ref-drop");
  const mk = (t) => new DragEvent(t, {bubbles: true, cancelable: true,
                                      dataTransfer: dt});
  drop.dispatchEvent(mk("dragenter"));
  drop.dispatchEvent(mk("drop"));
  return "dispatched";
})()""" % (json.dumps(src_name), json.dumps(new_name))


def main(argv=None):
    # 控制台是 GBK（936）时，一个**不在码表里**的字符会让 print 抛
    # UnicodeEncodeError —— 而 `note()` 打的是页面上的真值，于是整轮会在
    # **判据都过了之后**崩在收尾打印上（2026-10-01 实测：日志台那个「▶」）。
    # 只把 errors 放宽（编码不变，中文照旧可读），码表外的字符变成 `?`：
    # 判据的载体是截图与 JSON 报告，控制台只是给人扫一眼的。
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    ap = argparse.ArgumentParser(description="把工作台渲染出来并驱动它")
    ap.add_argument("--url", default="http://127.0.0.1:8189",
                    help="工作台地址（默认 8189）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--keep-profile", action="store_true")
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--sample", action="append", default=None,
                    help="拿来当「用户选的文件」的 PNG 路径，可给两次"
                         "（默认自动从工作区 output/ 里挑最小的两张）")
    args = ap.parse_args(argv)

    samples = [os.path.abspath(p) for p in (args.sample or [])]
    if len(samples) < 2:
        samples = (samples + pick_samples(2))[:2]
    missing = [p for p in samples if not os.path.isfile(p)]
    if missing:
        print("SKIP: 找不到可上传的样例图：%s" % "、".join(missing))
        return 2
    if len(samples) < 2:
        # 多参考图那一步要**两张**。凑不齐就说 SKIP，别让它跑成一条假失败。
        print("SKIP: 需要两张样例图才能验多参考图（只找到 %d 张）" % len(samples))
        return 2

    chrome = args.chrome or find_chrome()
    if not chrome:
        print("SKIP: 找不到任何浏览器可执行文件（找过 %s）"
              % ", ".join(CHROME_CANDIDATES))
        return 2
    print("chrome: %s" % chrome)

    # 工作台必须先活着 —— 不然整轮验证只会照出一张「后端未运行」的告警板，
    # 而那种产物看起来也是"成功截了图"。
    try:
        health = http_json(args.url + "/api/health", timeout=6)
        if not health.get("ok"):
            print("SKIP: 工作台没答 ok：%r" % health)
            return 2
    except Exception as exc:                                # noqa: BLE001
        print("SKIP: 工作台不在 %s（%s）" % (args.url, exc))
        return 2

    os.makedirs(args.out, exist_ok=True)
    # `--user-data-dir` 必须是**绝对路径**：这个路径会出现在浏览器的 argv 里，
    # 而带 `..` 的相对路径会让 chrome 起来就退出（实测 exit 21，且 stderr 被
    # 丢进 DEVNULL，所以只看得到「浏览器起来了又退出」这一句）。
    args.out = os.path.abspath(args.out)
    summary = {"url": args.url, "chrome": chrome, "shots": [], "checks": {},
               "samples": samples}
    page = Page(chrome, args.url, args.width, args.height,
                os.path.join(args.out, ".browser-profile"),
                keep_profile=args.keep_profile)

    def note(key, value):
        summary["checks"][key] = value
        print("  %-34s %s" % (key, json.dumps(value, ensure_ascii=False)))

    failures = []

    def check(key, got, want):
        """记一条**会红**的判据。

        这个脚本原本只记读数（报告给人看，退出码永远 0）。但「拖三张必须成为三格、
        而且每格的缩略图就是我拖的那张」这种事不该靠人眼在截图里比对 —— 给它一个
        能红的地方：读数照常进报告，同时攒进 `failures`，最后非 0 退出。
        """
        ok = got == want
        if not ok:
            failures.append({"key": key, "got": got, "want": want})
        note(key, {"got": got, "want": want, "ok": ok})

    def shot(name):
        path = os.path.join(args.out, name)
        page.shot(path)
        summary["shots"].append(path)
        print("  shot -> %s" % path)

    try:
        # 先确认**已经站在工作台那一页上**再等别的 —— 直接等 readyState 的话，
        # about:blank 当场就是 complete，后面的 getElementById 全拿 null。
        page.wait_for('!!document.getElementById("be-switch")', 30, "到达工作台页面")
        page.wait_for('document.readyState === "complete"', 30, "页面加载完")
        # 探活答完 = 那颗开关不再是 checking（原来等的是药丸那行字停掉「正在检查」）
        page.wait_for('document.getElementById("be-switch").dataset.state'
                      ' !== "checking"', 25, "后端探活答完")
        # 画廊是异步抓的；等它落地，否则第一张截图会是「扫描中…」
        page.wait_for('!document.getElementById("gallery").textContent'
                      '.includes("扫描中")', 30, "画廊扫完")

        print("\n[1] 默认（T2I）+ 滑块对位")
        note("thumb_initial", page.eval(JS_THUMB_VS_TAB))
        # 右上角那颗开关：**没有文字**，状态全在颜色/滑块位置与悬停提示里
        # （2026-10-02 起；在那之前是「一颗圆点 + 一行状态字」，再之前那块告警板
        # 还会把启动命令与模型芯片铺在页面上）。记成证据而不是靠眼睛看 12px 的字。
        note("backend_switch", page.eval("""(() => {
          const el = document.getElementById("be-switch");
          const knob = el.querySelector(".pow-knob");
          return {state: el.dataset.state, checked: el.getAttribute("aria-checked"),
                  disabled: el.disabled, text: el.textContent.trim(),
                  label: el.getAttribute("aria-label"),
                  title: el.title.trim().slice(0, 200),
                  knob: getComputedStyle(knob).backgroundColor,
                  track: getComputedStyle(el).borderStyle};
        })()"""))
        # 估时那一行**已经不在了**（2026-10-01 按用户要求去掉）。这一条要有读数：
        # 「没看见」不算判据 —— 上一版它就在这个位置、就长这个样子。
        note("estimate_line", page.eval("""(() => {
          const dock = document.querySelector(".submit-dock");
          return {elementExists: !!document.getElementById("estimate"),
                  classInDom: !!dock.querySelector(".estimate"),
                  mentionsEstimate: dock.textContent.indexOf("经验估算") >= 0};
        })()"""))
        shot("01_initial_t2i.png")

        print("\n[2] 点「画廊」：滑块要跟着走")
        page.eval(js_click("tab-gallery"))
        time.sleep(0.4)
        note("thumb_gallery", page.eval(JS_THUMB_VS_TAB))
        shot("02_tab_gallery.png")

        print("\n[3] 图生图：虚线框 + 框下面的小缩略图")
        page.eval(js_set_mode("edit"))
        page.wait_for('!document.getElementById("src-wrap")'
                      '.classList.contains("hidden")', 10, "源图字段出现")
        # 还没选图时：虚线框在、预览块**不在**（预览是下面的 ref-picked，
        # 不是框里那个空状态）
        note("dropzone_empty", page.eval("""(() => {
          const drop = document.getElementById("ref-drop");
          const picked = document.getElementById("ref-picked");
          const r = drop.getBoundingClientRect();
          const p = picked.getBoundingClientRect();
          return {dropTop: Math.round(r.top), dropBottom: Math.round(r.bottom),
                  pickedHidden: picked.classList.contains("hidden"),
                  pickedTop: Math.round(p.top),
                  emptyVisible: r.height > 60};
        })()"""))

        # --- 3a. **真的**文件选择器（CDP 把文件塞进 <input type=file>）------
        # 这一条验的就是用户报的那个问题：「只能填已有的文件名，选文件传不上去」。
        # `DOM.setFileInputFiles` 触发的是真的 change 事件，与手动选文件同一条路。
        print("\n[4] 选文件上传（真 <input type=file>，不是假的 event）")
        page.set_file_input("#src-file", [samples[0]])
        page.wait_for('document.getElementById("upload-msg")'
                      '.textContent.indexOf("已上传") >= 0', 40, "选文件上传有回话")
        time.sleep(1.0)
        note("picker_msg", page.eval(
            'document.getElementById("upload-msg").textContent.trim()'))
        note("picker_state", page.eval("""(() => {
          const chips = [...document.querySelectorAll("#ref-picked .ref-chip")];
          const drop = document.getElementById("ref-drop").getBoundingClientRect();
          const first = chips[0] && chips[0].getBoundingClientRect();
          const img = document.querySelector("#ref-picked .ref-thumb");
          return {chips: chips.length,
                  names: chips.map(c => c.dataset.name),
                  // 文件名**不在**方块上了（按用户要求去掉）：改成 title，
                  // 所以这里量的是「方块上有没有文字」与「title 在不在」
                  chipText: chips.map(c => (c.textContent || "").trim()),
                  titles: chips.map(c => c.getAttribute("title")),
                  hasName: !!document.querySelector("#ref-picked .ref-name"),
                  removeIsCorner: (() => {
                    const b = document.querySelector("#ref-picked .ref-remove");
                    if (!b || !first) return null;
                    const r = b.getBoundingClientRect();
                    return {insideCard: r.right <= first.right + 1 &&
                                        r.top >= first.top - 1,
                            size: [Math.round(r.width), Math.round(r.height)]};
                  })(),
                  order: [...document.querySelectorAll("#ref-picked .ref-order")]
                          .map(e => e.textContent),
                  // 缩略图必须在虚线框**下面**（用户要求的位置）
                  thumbBelowDrop: first ? first.top >= drop.bottom - 2 : null,
                  thumbBox: img ? [Math.round(img.getBoundingClientRect().width),
                                   Math.round(img.getBoundingClientRect().height)] : null,
                  natural: img ? img.naturalWidth + "x" + img.naturalHeight : null};
        })()"""))
        shot("03_edit_picked.png")

        # --- 3c. `×` 只在**悬停 / 聚焦**时出现（2026-10-01 按用户要求）--------
        # 这一条只能在这一层验：`:hover` 是浏览器给的伪类，在页面里 `dispatchEvent`
        # 一个 MouseEvent **不会**让它成立 —— 必须走 CDP 真的挪鼠标过去。键盘那半
        # （Tab 聚焦也要现身）同理，`:focus-within` 只有真浏览器有。
        print("\n[4b] 参考图的 ×：悬停前看不见，悬停后看得见，键盘聚焦也看得见")
        page.eval('document.querySelector("#ref-picked .ref-chip")'
                  '.scrollIntoView({block: "center"})')
        page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5,
                                               "buttons": 0})
        time.sleep(0.4)                     # 淡出淡入走完（--motion = 180ms）
        before = page.eval(REF_REMOVE_STATE)
        box = page.eval("""(() => {
          const r = document.querySelector("#ref-picked .ref-chip")
                     .getBoundingClientRect();
          return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)];
        })()""")
        page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": box[0],
                                               "y": box[1], "buttons": 0})
        time.sleep(0.4)
        note("ref_remove_hover",
             {"before": before, "after": page.eval(REF_REMOVE_STATE)})
        shot("03b_ref_remove_hover.png")
        # 鼠标挪开、改用键盘：聚焦到它上面也必须现身 —— 藏起来又够不着，
        # 等于「删掉这张参考图」这个动作对键盘用户不存在。
        page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5,
                                               "buttons": 0})
        page.eval('document.querySelector("#ref-picked .ref-remove").focus()')
        time.sleep(0.4)
        note("ref_remove_focused", page.eval(REF_REMOVE_STATE))
        shot("03c_ref_remove_focused.png")
        page.eval('document.activeElement.blur()')  # 别把焦点停在一个会删图的键上

        # --- 3b. 再选一张：**上传是累积的**（用户这一轮要求改的就是它）--------
        # 只选 `samples[1]` 这一张。旧行为（「设定这一批」）下第一张会被顶掉、
        # 只剩下 1 块；现在必须是 2 块 —— 这件事实只有真的跑一遍才看得见。
        print("\n[5] 再选一张：第一批必须还在（上传从替换改成累积）")
        page.set_file_input("#src-file", samples[1:2])
        page.wait_for('document.querySelectorAll("#ref-picked .ref-chip")'
                      '.length === 2', 40, "两块都在")
        page.eval(js_set_input("prompt", "combine the two reference pictures"))
        page.wait_for('document.getElementById("warnings")'
                      '.textContent.indexOf("2 张参考图") >= 0', 25,
                      "后端把「有几张」说出来了")
        note("accumulated_refs", page.eval("""(() => {
          const chips = [...document.querySelectorAll("#ref-picked .ref-chip")];
          const warns = [...document.querySelectorAll("#warnings .warn-line")];
          return {chips: chips.length,
                  names: chips.map(c => c.dataset.name),
                  uploadMsg: document.getElementById("upload-msg").textContent.trim(),
                  warns: warns.map(e => e.textContent.trim())
                              .filter(t => t.indexOf("参考图") >= 0),
                  // 说明性的 info **不上页面**（2026-10-01 按用户要求）：这一句
                  // 上一版就长在这儿（「已在后端 input/ 里」），现在必须找不到 ——
                  // 而会拦人的 warn 一条不少。两个读数放一起才是同一件事的两面。
                  infoLineGone: document.getElementById("warnings").textContent
                                .indexOf("已在后端 input/ 里") < 0,
                  lines: warns.map(e => e.className + " | "
                                        + e.textContent.slice(0, 30))};
        })()"""))
        shot("04_refs_accumulate.png")

        print("\n[6] 拖进来（真 DataTransfer，走完整 uploadRefs → /api/upload）")
        page.eval(js_watch_drop())
        page.eval(js_drop_file(os.path.basename(samples[0]), DROP_NAME))
        page.wait_for('document.getElementById("upload-msg").textContent.length > 0',
                      30, "上传有回话")
        time.sleep(1.5)                     # 让 uploadRefs 里那个 await 落定
        note("drop_events", json.loads(page.eval(
            "JSON.stringify(window.__drop_dbg || [])")))
        msg = page.eval('document.getElementById("upload-msg").textContent.trim()')
        note("drop_upload_msg", msg)
        if "已上传" in msg:
            # 拖进来之后是**三张**：前两张是选的，这一张是拖的。累积行为在
            # 拖放这条路上也得成立 —— 两条路写的是同一份 `state.refs`。
            refs = page.eval("""[...document.querySelectorAll("#ref-picked .ref-chip")]
                               .map(c => c.dataset.name)""")
            note("drop_upload", {"result": "PASS", "refs": refs,
                                 "count": len(refs)})
            page.wait_for('document.querySelector("#ref-picked .ref-thumb")'
                          '.naturalWidth > 0', 20, "拖入后的缩略图")
        elif "Permission denied" in msg or "写不了" in msg:
            # 环境挡住的，不是坏掉的。照实记 SKIP + 原因，**不伪装成通过**。
            note("drop_upload", {"result": "SKIP_ENV", "reason": msg,
                                 "why": WRITE_BLOCK_NOTE})
        else:
            raise RuntimeError("拖放上传出现了预料之外的回答：%r" % msg)
        shot("05_drop_uploaded.png")

        # --- 6b. 从画廊把图**拖**进来（用户报的那条流程本身）------------------
        # 症状：拖第 2 张不显示、删掉第 1 张再拖第 3 张却显示第 2 张。
        # 根因是**浏览器的文件名**：从本页拖一张图时它合成一个 `image.png`
        # （按「URL 末段 + MIME 后缀」），于是每张都往同一个名字上覆盖
        # （`/api/upload` 默认 overwrite=1），而选中集按名字去重 —— 列表里永远
        # 只有一张，显示的还可能是上一张的内容。实测证据：用户拖过三张之后，
        # 后端 `input/` 里只有一个 `image.png`，大小正好等于**第二张**画廊图。
        # 判据是**字节**：每一格的缩略图必须与我拖的那张 sha256 相同。
        #
        # P20l 起「我拖的那张」要分清是哪一份：格子上的 `src` 是**缩略图**，而落地
        # 的必须是**原图**（`refFromUri` 把地址改写回去了）。所以判据从「落地 == 拖
        # 的那个地址的字节」改成「落地 == 那一张的**原图**字节」，并**同时**要求
        # 两份哈希不同 —— 否则这条判据在「原图恰好等于缩略图」时是空转的绿。
        print("\n[6b] 从画廊拖图：三张各自一格，落地的是那一张的**原图**")
        page.eval(JS_GALLERY_DRAG)
        page.eval(js_click("btn-clear-ref"))
        page.wait_for(chip_count(0), 15, "先清空")

        def wait_chips(n, why):
            page.wait_for(chip_count(n), 40, why)

        dragged, counts = [], []
        wait_chips(0, "空列表")
        dragged.append(page.eval("window.__dragFromGallery(0, %s)"
                                 % json.dumps(BROWSER_DRAG_NAME)))
        wait_chips(1, "第 1 张进列表")
        counts.append(1)
        dragged.append(page.eval("window.__dragFromGallery(1, %s)"
                                 % json.dumps(BROWSER_DRAG_NAME)))
        wait_chips(2, "第 2 张进列表（旧行为在这里断掉）")
        counts.append(2)
        # 用户流程里那一步：删掉第 1 张
        page.eval('document.querySelector("#ref-picked .ref-remove").click()')
        wait_chips(1, "删一张后只剩一张")
        counts.append(1)
        dragged.append(page.eval("window.__dragFromGallery(2, %s)"
                                 % json.dumps(BROWSER_DRAG_NAME)))
        wait_chips(2, "第 3 张进列表")
        counts.append(2)

        review = page.eval("window.__chipReview()")
        backend_inputs = (http_json(args.url + "/api/inputs").get("files") or [])
        real_names = [d["realName"] for d in dragged]
        note("gallery_drag", {
            "dragged": dragged,
            "chip_counts": counts,
            "chips_in_list": [r["name"] for r in review],
            "thumb_natural": [r["natural"] for r in review],
            "chip_matches_the_original": [r["sameAsOriginal"] for r in review],
            "chip_matches_the_thumbnail": [r["sameAsDragged"] for r in review],
            "the_two_hashes_really_differ":
                [not d["thumbsAreTheSame"] for d in dragged],
            "all_three_landed_in_input": [n in backend_inputs for n in real_names],
            "browser_name_used_anywhere": BROWSER_DRAG_NAME in
                                          [r["name"] for r in review],
        })
        check("gallery_drag_chip_counts", counts, [1, 2, 1, 2])
        check("gallery_drag_names_are_their_own", [r["name"] for r in review],
              [real_names[1], real_names[2]])
        check("gallery_drag_uploads_the_original_not_the_thumbnail",
              [r["sameAsOriginal"] for r in review], [True, True])
        # 反例：落地的那份**不许**等于格子画的缩略图 —— 两条哈希不一样，上面那条
        # 才不是空转的绿（顺带钉住「缩略图不再被当成原图用」）。
        check("gallery_drag_does_not_upload_the_thumbnail_bytes",
              [r["sameAsDragged"] for r in review], [False, False])
        check("gallery_drag_never_uses_the_browser_name",
              BROWSER_DRAG_NAME in [r["name"] for r in review], False)
        check("gallery_drag_all_landed_in_input",
              [n in backend_inputs for n in real_names], [True, True, True])
        shot("05b_gallery_drag.png")

        print("\n[7] 反向词在 CFG=1.0：门槛必须出现")
        page.eval(js_set_mode("t2i"))
        # 提示词必须有 —— 没有的话 refreshBuild() 根本不会去问后端，
        # 而这一轮要验的正是「后端的结论有没有被端到页面上」。
        page.eval(js_set_input("prompt", "a single ripe red apple on a table"))
        page.eval('document.getElementById("negative-wrap").open = true; true')
        page.eval(js_set_input("negative", NEGATIVE_TEXT))
        page.wait_for('document.getElementById("negative-hint")'
                      '.textContent.length > 0', 20, "反向词状态字出现")
        note("negative_hint_cfg1", page.eval(
            'document.getElementById("negative-hint").textContent'))
        note("negative_button", page.eval(
            'document.getElementById("btn-raise-cfg").textContent'))
        note("negative_warn", page.eval(
            '[...document.querySelectorAll("#warnings .warn-line")]'
            '.map(e => e.className + " :: " + e.textContent.trim())'))
        shot("06_negative_inert.png")

        # 那个「让它生效」的按钮在折叠区里，而折叠区是左栏偏下的字段 ——
        # 不滚过去它就只在 DOM 里、不在眼里。**DOM 里有**与**屏幕上看得见**
        # 是两件事，而这一层要验的正是后者。
        # 用 `scrollIntoView` 而不是 `scrollTop = scrollHeight`：后者只保证"滚到底"，
        # 到底之后这个按钮可能反而被推到视口**上方**去了（实测就是这样）。
        page.eval('document.getElementById("btn-raise-cfg")'
                  '.scrollIntoView({block: "center"})')
        time.sleep(0.4)
        note("raise_button_visible", page.eval("""(() => {
          const b = document.getElementById("btn-raise-cfg");
          const r = b.getBoundingClientRect();
          return {text: b.textContent, width: Math.round(r.width),
                  top: Math.round(r.top), bottom: Math.round(r.bottom),
                  onScreen: r.top >= 0 && r.bottom <= window.innerHeight,
                  hidden: b.classList.contains("hidden")};
        })()"""))
        shot("06b_negative_button.png")

        print("\n[8] 点「让它生效」：CFG 提上去，状态字要翻过来")
        page.eval(js_click("btn-raise-cfg"))
        page.wait_for('document.getElementById("negative-hint")'
                      '.textContent.indexOf("生效中") >= 0', 20, "状态字变生效中")
        note("cfg_after_raise", page.eval('document.getElementById("cfg").value'))
        note("negative_hint_live", page.eval(
            'document.getElementById("negative-hint").textContent'))
        note("negative_button_hidden", page.eval(
            'document.getElementById("btn-raise-cfg")'
            '.classList.contains("hidden")'))
        shot("08_negative_live.png")

        print("\n[9] 状态字不许滞留：提示词清空后它必须跟着消失")
        # 这条是**真的渲染之后**才发现的缺陷：refreshBuild() 在提示词为空时提前
        # 返回、只清 warnings、不碰反向词那一块，于是标题会顶着上一轮的结论。
        # 假 DOM 里永远看不见，因为那里没有人真的点两下再看一眼。
        page.eval(js_set_input("prompt", ""))
        page.wait_for('document.getElementById("negative-hint")'
                      '.textContent.length === 0', 20, "状态字被清掉")
        note("hint_after_prompt_cleared", page.eval(
            'document.getElementById("negative-hint").textContent'))
        note("raise_button_hidden_after_clear", page.eval(
            'document.getElementById("btn-raise-cfg")'
            '.classList.contains("hidden")'))
        shot("09_no_stale_hint.png")

        # --- 2026-10-01：画廊的三个动作与放大视图 ---------------------------
        # 这一轮改的是「点一下图会发生什么」：上一版整块格子是一个按钮，点哪儿
        # 都是「载入参数」，想放大看、想拿走提示词都没有入口。下面这几步就是
        # 那条改动的判据 —— 而且**只能**在这一层验：假 DOM 里没有布局，
        # 也没有 `:modal` 与 Esc。
        print("\n[10] 画廊：一张图三个入口（放大 / 载入提示词 / 载入参数）")
        page.eval(js_click("tab-gallery"))
        page.wait_for('document.querySelectorAll("#gallery .tile").length > 0',
                      30, "画廊里有图")
        page.eval('document.querySelector("#gallery .tile .tile-shot")'
                  '.scrollIntoView({block: "center"})')
        time.sleep(0.4)
        note("tile_actions", page.eval("""(() => {
          const t = document.querySelector("#gallery .tile");
          const acts = [...t.querySelectorAll("[data-act]")];
          return {tiles: document.querySelectorAll("#gallery .tile").length,
                  acts: acts.map(b => b.dataset.act),
                  labels: acts.map(b => b.textContent.trim()),
                  // 没提示词的那几张应当是**禁用**的（按下去说不出理由就成哑键）
                  promptDisabled: t.querySelector('[data-act="loadprompt"]').disabled,
                  promptEnabled: document.querySelectorAll(
                      '#gallery [data-act="loadprompt"]:not([disabled])').length,
                  zoomHint: !!t.querySelector(".tile-zoom"),
                  // 整块格子**不再**是一个按钮（那样点哪儿都是载入参数）
                  tileTag: t.tagName,
                  // 格子上只剩一行小标签（2026-10-01 按用户要求去掉那三行描述）：
                  // `captionText` 是格子里**所有**的文字，`goneLines` 是那三行还剩
                  // 几条（必须是 0，`-webkit-line-clamp` 那种「看着像没有」不算数）。
                  captionText: t.querySelector(".tile-caption").textContent.trim(),
                  goneLines: [".tile-sub", ".tile-prompt", ".tile-origin"]
                              .filter(sel => t.querySelector(sel)).length,
                  pills: t.querySelectorAll(".tile-badges .pill").length};
        })()"""))
        shot("10_gallery_tiles.png")

        # --- 10b. 「载入参数」只拿数值（2026-10-01 按用户要求收窄）--------------
        # 它以前会把提示词、反向词、模式、参考图、加速开关一起覆盖掉 —— 而它最常
        # 被用的地方是「拿这张图的参数配**我自己的**提示词」，被悄悄换掉的东西往往
        # 到出图之后才发现。所以：先把表单摆成一个**好认的、与目标图处处不同**的
        # 基线，再点那张图的「载入参数」，然后逐项比 ——
        #   · 提示词 / 反向词 / 模式 / 参考图方块数 / 两个加速开关 = **一个字没动**；
        #   · 步数 / seed / CFG / 采样器 / 调度器 / 尺寸 = **变成这张图的**。
        # 期望值**不写死**在脚本里：从 `/api/gallery` 按格子上的 `data-id` 找那一项
        # 取。写死等于把「我以为的」当成判据。
        print("\n[10b] 载入参数：只有数值参数会变，提示词 / 模式 / 参考图一个字不动")
        page.eval(JS_GALLERY_DRAG)      # 下面要用它带来的 `__dragFromGallery`
        items = http_json(args.url + "/api/gallery").get("items") or []
        target = next((x for x in items
                       if x.get("recognized") and x.get("mode") == "t2i"
                       and x.get("steps") is not None and x.get("cfg") is not None
                       and x.get("seed") is not None
                       and x.get("width") and x.get("height")), None)
        if target is None:
            note("load_params", {"result": "SKIP",
                                 "why": "画廊里没有一张可用的 t2i 图"})
        else:
            # 出图那几档尺寸就写在这儿（与 `index.html` 的 `#size` 对齐）：
            # 挑一个与目标图**不同**的当基线，否则「尺寸被载入了」这条判据是空的。
            want_size = "%dx%d" % (target["width"], target["height"])
            presets = ["1024x1024", "1280x768", "768x1280", "2048x2048"]
            base_size = next((s for s in presets if s != want_size), "1280x768")
            baseline = {
                "prompt": "基线提示词：这一段一个字都不该被画廊改掉",
                "negative": "baseline negative words",
                "mode": "t2i",
                "size": base_size,
                "steps": str(int(target["steps"]) + 1),
                "seed": str(int(target["seed"]) + 1),
                "cfg": "%.4g" % (float(target["cfg"]) + 0.5),
            }
            page.eval(js_set_mode(baseline["mode"]))
            page.eval(js_set_input("prompt", baseline["prompt"]))
            page.eval(js_set_input("negative", baseline["negative"]))
            page.eval(js_set_input("steps", baseline["steps"]))
            page.eval(js_set_input("seed", baseline["seed"]))
            page.eval(js_set_input("cfg", baseline["cfg"]))
            page.eval('(() => { const s = document.getElementById("size");'
                      ' s.value = ' + json.dumps(baseline["size"]) + ";"
                      ' s.dispatchEvent(new Event("change", {bubbles: true}));'
                      ' return s.value; })()')
            page.eval('(() => { const c = document.getElementById("lazy_cache");'
                      ' c.checked = true;'
                      ' c.dispatchEvent(new Event("change", {bubbles: true}));'
                      ' return c.checked; })()')
            # 基线里至少有**一张**参考图：「载入不碰它」在 0 张上验不出东西。
            # 走的还是 6b 那条真上传路（回执里的真名字进选中集）。
            page.eval("window.__dragFromGallery(0, %s)" % json.dumps(BROWSER_DRAG_NAME))
            time.sleep(1.2)
            chips_before = page.eval(
                'document.querySelectorAll("#ref-picked .ref-chip").length')

            clicked = page.eval(
                '(() => { const t = [...document.querySelectorAll("#gallery .tile")]'
                '.find(e => e.dataset.id === ' + json.dumps(target["id"]) + ");"
                ' if (!t) return "NO_TILE";'
                ' t.querySelector(\'[data-act="load"]\').click(); return "OK"; })()')
            page.wait_for('document.getElementById("run-msg").textContent'
                          '.includes("载入参数")', 20, "载入参数回话")
            after = page.eval("""(() => ({
              prompt: document.getElementById("prompt").value,
              negative: document.getElementById("negative").value,
              mode: document.getElementById("mode").value,
              chips: document.querySelectorAll("#ref-picked .ref-chip").length,
              lazy: document.getElementById("lazy_cache").checked,
              te: document.getElementById("te_speed").checked,
              steps: document.getElementById("steps").value,
              seed: document.getElementById("seed").value,
              cfg: document.getElementById("cfg").value,
              sampler: document.getElementById("sampler").value,
              scheduler: document.getElementById("scheduler").value,
              size: document.getElementById("size").value,
              width: document.getElementById("width").value,
              height: document.getElementById("height").value,
              msg: document.getElementById("run-msg").textContent
            }))()""")

            def as_float(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            # 尺寸落在预设档上就用档位，不在就应当落到「自定义」+ 宽高两个框。
            size_landed = (after["size"] == want_size
                           or (after["size"] == "custom"
                               and after["width"] == str(target["width"])
                               and after["height"] == str(target["height"])))
            note("load_params", {
                "clicked": clicked, "target_id": target["id"],
                "want": {"steps": str(int(target["steps"])),
                         "seed": str(int(target["seed"])),
                         "cfg": target["cfg"], "sampler": target["sampler"],
                         "scheduler": target["scheduler"], "size": want_size},
                "baseline": dict(baseline, chips=chips_before),
                "after": after, "size_landed": size_landed,
            })
            check("load_baseline_had_a_ref", chips_before >= 1, True)
            check("load_keeps_prompt", after["prompt"], baseline["prompt"])
            check("load_keeps_negative", after["negative"], baseline["negative"])
            check("load_keeps_mode", after["mode"], baseline["mode"])
            check("load_keeps_refs", after["chips"], chips_before)
            check("load_keeps_accel_switches", [after["lazy"], after["te"]],
                  [True, False])
            check("load_takes_steps", after["steps"], str(int(target["steps"])))
            check("load_takes_seed", after["seed"], str(int(target["seed"])))
            check("load_takes_cfg", as_float(after["cfg"]), float(target["cfg"]))
            check("load_takes_sampler", after["sampler"], target["sampler"])
            check("load_takes_scheduler", after["scheduler"], target["scheduler"])
            check("load_takes_size", size_landed, True)
            # 那句回话必须点名这张图 —— 载入是个把表单改掉的动作，改了什么、
            # 没改什么都得当场说清（说不清就是「没有症状的错」）。
            check("load_message_names_the_file",
                  target["filename"] in after["msg"], True)
            shot("10b_load_params.png")

        print("\n[11] 点图放大：真 <dialog>、适应窗口、焦点在里、Esc 能关")
        page.click_selector("#gallery .tile .tile-shot")
        page.wait_for('document.getElementById("lightbox").open === true', 20,
                      "放大视图打开")
        time.sleep(0.5)                      # 让大图解码完再量
        note("lightbox_open", page.eval("""(() => {
          const d = document.getElementById("lightbox");
          const img = document.getElementById("lb-img");
          const stage = document.getElementById("lb-stage");
          const ir = img.getBoundingClientRect(), sr = stage.getBoundingClientRect();
          return {open: d.open,
                  modal: d.matches(":modal"),
                  name: document.getElementById("lb-name").textContent,
                  meta: document.getElementById("lb-meta").textContent,
                  promptChars: document.getElementById("lb-prompt").textContent.length,
                  natural: img.naturalWidth + "x" + img.naturalHeight,
                  shown: Math.round(ir.width) + "x" + Math.round(ir.height),
                  stage: Math.round(sr.width) + "x" + Math.round(sr.height),
                  // 「适应窗口」必须**真的**适应：整张图在舞台里，且没被拉伸
                  fitsStage: ir.width <= sr.width + 1 && ir.height <= sr.height + 1,
                  keptRatio: Math.abs(ir.width / ir.height
                                      - img.naturalWidth / img.naturalHeight) < 0.01,
                  focusInside: d.contains(document.activeElement),
                  prevDisabled: document.getElementById("lb-prev").disabled,
                  // 背后的页面真的是 inert 的：试着把焦点挪出去，挪不动才对。
                  // 这是模态 <dialog> 送的，自己搭的遮罩不会有。
                  outsideFocus: (() => {
                    const back = document.getElementById("gallery-search");
                    if (back) back.focus();
                    return document.activeElement.id || document.activeElement.tagName;
                  })()};
        })()"""))
        shot("11_lightbox_fit.png")

        print("\n[12] 切到原始像素：1:1 才看得见细节")
        page.click_selector("#lb-zoom")
        note("lightbox_1to1", page.eval("""(() => {
          const img = document.getElementById("lb-img");
          const stage = document.getElementById("lb-stage");
          const r = img.getBoundingClientRect();
          return {zoomed: stage.classList.contains("zoomed"),
                  button: document.getElementById("lb-zoom").textContent,
                  shown: Math.round(r.width) + "x" + Math.round(r.height),
                  matchesNatural: Math.round(r.width) === img.naturalWidth,
                  scrollable: stage.scrollWidth > stage.clientWidth + 1 ||
                              stage.scrollHeight > stage.clientHeight + 1};
        })()"""))
        shot("12_lightbox_1to1.png")

        print("\n[13] 复制提示词：按钮自己回话（剪贴板能不能回读另说）")
        # 无头里 `navigator.clipboard.readText()` 要剪贴板读权限。这条命令只有
        # 浏览器级连接才保证支持 ——**先试一把**，成了就把内容读回来跟页面上
        # 那段全文比；不成就如实记一句「读不回来」，绝不假装验过了。
        try:
            page.call("Browser.grantPermissions", {
                "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
                "origin": args.url}, timeout=10)
            note("clipboard_permission", "已授予（能回读内容）")
        except Exception as exc:                            # noqa: BLE001
            note("clipboard_permission", {"granted": False, "why": str(exc)[:160]})
        page.click_selector("#lb-copy")
        time.sleep(0.5)
        note("copy_feedback", page.eval("""(() => {
          const b = document.getElementById("lb-copy");
          return {label: b.textContent.trim(),
                  err: b.classList.contains("flasherr"),
                  status: document.getElementById("lb-status").textContent,
                  stillOpen: document.getElementById("lightbox").open,
                  // 兜底那条路会把全文选中，这里看有没有选中
                  selection: String((window.getSelection() || {}).toString() || "")
                              .slice(0, 40)};
        })()"""))
        try:
            got = page.eval("navigator.clipboard.readText()", timeout=15)
            want = page.eval('document.getElementById("lb-prompt").textContent')
            note("clipboard_match", {"read": (got or "")[:60],
                                     "sameAsPrompt": got == want,
                                     "chars": len(got or "")})
        except Exception as exc:                            # noqa: BLE001
            note("clipboard_readback", {"result": "SKIP", "why": str(exc)[:160],
                                        "note": "剪贴板读不回来 —— 这一步只验到"
                                                "按钮回话与兜底路径"})
        shot("13_copy_prompt.png")

        print("\n[14] ←/→ 换张：真按键（Esc 只能由浏览器处理）")
        before = page.eval('document.getElementById("lb-name").textContent')
        page.press_key("ArrowRight", "ArrowRight", 39)
        note("lightbox_next", page.eval("""(() => {
          const prev = document.getElementById("lb-prev");
          return {name: document.getElementById("lb-name").textContent,
                  meta: document.getElementById("lb-meta").textContent,
                  prevNowEnabled: !prev.disabled,
                  focusInside: document.getElementById("lightbox")
                                .contains(document.activeElement)};
        })()""") | {"before": before})
        page.press_key("Escape", "Escape", 27)
        page.wait_for('document.getElementById("lightbox").open === false', 10,
                      "Esc 关掉放大视图")
        note("lightbox_closed", page.eval("""(() => ({
          open: document.getElementById("lightbox").open,
          focusBack: document.activeElement
                     ? document.activeElement.className : null,
          focusInGallery: !!document.activeElement.closest
                          && !!document.activeElement.closest("#gallery")
        }))()"""))
        shot("14_after_esc.png")

        # --- 15. 放大视图那行 meta 必须报出参考图 -----------------------------
        # 为什么单列一条：格子只剩一行药丸，而「载入参数」已经不碰参考图了 ——
        # 这行 meta 是页面上**唯一**还能看到「这张图用了哪张图」的地方（编辑链路的
        # 核心信息）。少了它，那件事在页面上就彻底没有入口了。
        print("\n[15] 放大视图 meta：参考图 / 源图（页面上唯一能看到它的地方）")
        items = http_json(args.url + "/api/gallery").get("items") or []
        ref_item = next((x for x in items
                         if (x.get("source_images") or []) and x.get("recognized")),
                        None)
        if ref_item is None:
            note("lightbox_meta_refs", {
                "result": "SKIP",
                "why": "画廊里没有带参考图的图 —— 这一条判据在本机不成立"})
        else:
            page.eval(js_click("tab-gallery"))
            # 先回到「全部」这一档（过滤掉的话，目标项根本不在页面手里）
            page.eval("(() => { const c = document.querySelectorAll("
                      "'#gallery-chips .chip')[0]; if (c) c.click(); return true; })()")
            # 找格子用 `dataset.id ===`，**不要**把 id 拼进 CSS 选择器：id 里带 `:`
            # （`0:xxx.png`），而 JSON 给的是双引号，拼进 `[data-id="…"]` 会当场
            # SyntaxError（第一次跑就是这样炸的）。
            page.wait_for("[...document.querySelectorAll('#gallery .tile')]"
                          ".some(e => e.dataset.id === %s)"
                          % json.dumps(ref_item["id"]), 30, "目标图在画廊里")
            opened = page.eval(
                "(() => { if (typeof openLightbox !== 'function') return 'NO_FN';"
                " openLightbox(" + json.dumps(ref_item["id"]) + "); return 'OK'; })()")
            page.wait_for('document.getElementById("lightbox").open === true', 20,
                          "放大视图打开")
            meta = page.eval('document.getElementById("lb-meta").textContent')
            note("lightbox_meta_refs", {
                "opened": opened, "item": ref_item["id"],
                "source_images": ref_item["source_images"], "meta": meta})
            check("lightbox_meta_names_the_source_image",
                  ref_item["source_images"][0] in (meta or ""), True)
            if len(ref_item["source_images"]) > 1:
                check("lightbox_meta_counts_the_source_images",
                      ("%d 张" % len(ref_item["source_images"])) in (meta or ""), True)
            shot("15_lightbox_refs.png")

            # 同一张图再点一次「载入参数」—— 这次用**放大视图那个入口**，而且
            # 参考图块数必须一个不变。为什么要在这里再验一遍：第 10b 步的靶子是一张
            # t2i 图，它本来就**没有**参考图，那条 `load_keeps_refs` 在那张图上
            # 不可能红（白的）。这一张有 2 张，判据才有牙齿；顺带把放大视图那个
            # 入口也走一遍（它以前只被「点了先关窗」那件事覆盖）。
            chips_before = page.eval(
                'document.querySelectorAll("#ref-picked .ref-chip").length')
            # 模式同理：第 10b 的靶子是 t2i 图，载入它**不会**让模式变（那条
            # `load_keeps_mode` 在它身上也不会红）。这一张是 Edit，有牙齿。
            mode_before = page.eval('document.getElementById("mode").value')
            page.click_selector("#lb-load")
            # 等的必须是**这一张**的回话：上一步那句「已从 … 载入参数」还挂在
            # 状态行上，只等「载入参数」这四个字会当场命中上一句（假绿）。
            page.wait_for('document.getElementById("run-msg").textContent'
                          '.includes(' + json.dumps(ref_item["filename"]) + ")", 20,
                          "放大视图里那个入口的回话")
            after_refs = page.eval(
                'document.querySelectorAll("#ref-picked .ref-chip").length')
            after_mode = page.eval('document.getElementById("mode").value')
            note("lightbox_load_keeps_refs", {
                "item": ref_item["id"], "chips_before": chips_before,
                "chips_after": after_refs,
                "mode_before": mode_before, "mode_after": after_mode,
                "msg": page.eval('document.getElementById("run-msg").textContent')})
            check("load_ref_target_really_has_some", chips_before >= 2, True)
            check("load_keeps_refs_for_an_image_that_has_some",
                  after_refs, chips_before)
            check("load_keeps_mode_for_an_edit_image", after_mode, mode_before)

        # --- 16. 模式只剩「形状」，改写与链是额外功能（2026-10-01 重构）--------
        # 这一轮改的是**下拉框里还剩什么**，以及那两个开关发出去的是什么。
        # 提交体直接用 `collectBody()` 取（它是全局函数）—— 真提交一次要 60 秒，
        # 而这里要验的是「发什么」，不是「后端怎么处理」。
        print("\n[16] 模式只剩三条形状；改写与「出图后自动放大」都是额外功能")
        modes = page.eval("(() => { const m = document.getElementById('mode');"
                          " return [...m.options].map(o => o.value); })()")
        check("mode_select_has_only_three_shapes", modes,
              ["t2i", "edit", "upscale"])
        body = page.eval("""(() => {
          const m = document.getElementById("mode");
          m.value = "t2i";
          document.getElementById("rewrite").checked = true;
          document.getElementById("auto_upscale").checked = true;
          document.getElementById("auto_scale").value = "4";
          applyModeUI();
          return collectBody();
        })()""")
        check("body_says_rewrite_not_mode_refine", body.get("mode"), "t2i")
        check("body_carries_the_rewrite_flag", body.get("rewrite"), True)
        check("body_carries_the_chain", body.get("auto_upscale"),
              {"enabled": True, "scale": 4})
        hidden = page.eval("""(() => {
          const m = document.getElementById("mode");
          const isHidden = (id) => document.getElementById(id)
              .classList.contains("hidden");
          const out = {};
          for (const v of ["t2i", "edit", "upscale"]) {
            m.value = v; applyModeUI();
            out[v] = {rewrite: isHidden("rewrite-wrap"), chain: isHidden("chain-wrap")};
          }
          m.value = "t2i"; applyModeUI();
          document.getElementById("rewrite").checked = false;
          document.getElementById("auto_upscale").checked = false;
          return out;
        })()""")
        check("rewrite_control_shows_only_on_t2i",
              [hidden["t2i"]["rewrite"], hidden["edit"]["rewrite"],
               hidden["upscale"]["rewrite"]], [False, True, True])
        check("chain_control_hides_only_on_upscale",
              [hidden["t2i"]["chain"], hidden["edit"]["chain"],
               hidden["upscale"]["chain"]], [False, False, True])
        shot("16_extra_features.png")

        # 画廊那一颗 chip：`Refine` 换成「改写」，而且它与模式**正交** ——
        # 老 refine 产物靠 PNG 里的 switch 反推出来，所以历史图一张都不该丢。
        chips = page.eval("(() => [...document.querySelectorAll("
                          "'#gallery-chips .chip')].map(c => c.dataset.mode))()")
        check("gallery_chips_replaced_refine_with_rewrite", chips,
              ["", "t2i", "edit", "2k", "rewrite", "__foreign"])
        page.eval("(() => { const c = [...document.querySelectorAll("
                  "'#gallery-chips .chip')].find(x => x.dataset.mode === "
                  "'rewrite'); if (c) c.click(); return true; })()")
        page.wait_for('!document.getElementById("gallery").textContent'
                      '.includes("扫描中")', 20, "改写那一档筛完")
        time.sleep(0.4)
        rw = page.eval("""(() => {
          const tiles = [...document.querySelectorAll("#gallery .tile")];
          return {tiles: tiles.length,
                  sayingRewrite: tiles.filter(t => t.querySelector(".tile-caption")
                      .textContent.includes("改写")).length,
                  first: tiles.length ? tiles[0].querySelector(".tile-caption")
                      .textContent.trim() : null};
        })()""")
        note("gallery_rewrite_chip", rw)
        check("the_rewrite_chip_finds_the_old_rewritten_images",
              rw["tiles"] > 0, True)
        check("every_tile_in_that_chip_is_marked_rewrite",
              rw["sayingRewrite"], rw["tiles"])
        shot("16b_rewrite_chip.png")

        # --- 17. 模块提示只留功能描述（2026-10-01 按用户要求收窄）-------------
        # 三处：高级设置、参数扫描、模式里的放大超分。这里量的**不是**源码那份判据
        # （它在 `test_api.py::FrontendContract` 里）—— 这里量**渲染出来的文字**，
        # 所以必须先把 `<details>` 真的打开：折着的时候那些节点不在渲染树里
        # （`innerText` 拿不到），「文案改坏了」在那之前根本量不到。
        print("\n[17] 模块提示只留功能描述（高级设置 / 参数扫描 / 放大超分）")
        hints = page.eval("""(() => {
          const text = (el) => (el.innerText || "").replace(/\\s+/g, " ").trim();
          const openAll = (root) => {
            root.open = true;
            root.querySelectorAll("details").forEach(d => { d.open = true; });
            return root;
          };
          const grab = (root) => [...root.querySelectorAll("p")].map(text)
              .filter(s => s.length);
          const adv = openAll(document.getElementById("advanced-wrap"));
          const sweep = openAll(document.getElementById("sweep-panel"));
          document.getElementById("mode").value = "upscale";
          applyModeUI();
          const up = openAll(document.getElementById("upscale-wrap"));
          const sel = document.getElementById("mode").selectedOptions[0];
          return {advanced: grab(adv), sweep: grab(sweep), upscale: grab(up),
                  paragraphs: [...adv.querySelectorAll("p")].map(text),
                  option: (sel ? sel.textContent : "").trim()};
        })()""")
        note("module_hints", hints)
        flat = " ".join(hints["advanced"] + hints["sweep"] + hints["upscale"])
        for gone in ("实测", "%", "梯度", "EasyCache"):
            check("hints_have_no_%s" % gone, gone in flat, False)
        longest = max([len(t) for t in hints["paragraphs"]] or [0])
        note("longest_hint_chars", longest)
        check("every_hint_is_one_line", longest <= 60, True)
        check("upscale_option_carries_no_numbers", hints["option"],
              "放大 · VOSR2 超分")

        # 扫描里那句话：勾上链**当场**出现，而且只说「不生效」（不再解释理由）。
        page.eval("""(() => {
          document.getElementById("mode").value = "t2i";
          document.getElementById("auto_upscale").checked = true;
          applyModeUI();
          return true;
        })()""")
        time.sleep(0.2)
        check("sweep_chain_note_says_what_happens_only",
              page.eval('document.getElementById("sweep-chain-note")'
                        '.textContent.trim()'),
              "「出图后自动放大」在扫描里不生效。")
        # 左栏是**滚动容器**：不滚过去，`<details>` 打开了也照样在画面外 ——
        # 那样拍出来的图什么也证明不了（第一版就是这么拍的，看了一眼才发现）。
        page.eval('document.getElementById("advanced-wrap")'
                  '.scrollIntoView({block: "start"}); true')
        time.sleep(0.35)
        shot("17_hints_advanced.png")
        page.eval('document.getElementById("sweep-panel")'
                  '.scrollIntoView({block: "center"}); true')
        time.sleep(0.35)
        shot("17b_hints_chain_and_sweep.png")

        # 放大模式那两支：下拉里那一项 + 倍数旁边那个折叠块（一行功能事实）。
        page.eval("""(() => {
          document.getElementById("auto_upscale").checked = false;
          document.getElementById("mode").value = "upscale";
          applyModeUI();
          document.getElementById("upscale-wrap").querySelectorAll("details")
              .forEach(d => { d.open = true; });
          document.getElementById("upscale-wrap")
              .scrollIntoView({block: "start"});
          return true;
        })()""")
        time.sleep(0.35)
        shot("17c_upscale_hints.png")
        page.eval("""(() => {
          document.getElementById("mode").value = "t2i";
          applyModeUI();
          return true;
        })()""")

        # --- 18. 「载入提示词」：两段各回自己的框（2026-10-01 按用户要求）----------
        # 用户先说的是「提示词整体分为了两个部分，而单纯的复制无法区分两者」，随后
        # 澄清了想要的是**载入参数那样的动作** ——「点它的时候自动将这些信息填入对应
        # 的位置」。所以这一条量的不是剪贴板，而是**表单里那两个框有没有被填对**：
        # 提示词框 ← 正向，反向词框 ← 反向，而且这张图没有反向词时**那个框一个字不动**。
        #
        # 本机画廊里未必有「填过反向词」的图（真实产物都是空反向词），所以造两条条目
        # 喂给真正的 `renderGallery` / `renderLightbox` —— 走的是真渲染路径，只有数据
        # 是编的（与 `views_probe.js` 同一个理由）。
        print("\n[18] 载入提示词：两段各回自己的框；没有反向词就不动那个框；放大模式禁用")
        page.eval("""(() => {
          const base = (state.gallery.items || [])[0];
          // 文件名照抄基座那一条：缩略图是**按 root + name 现取的**，编一个名字会
          // 在截图里留下一块「图裂了」的占位（第一版就是这样，看着像个 bug）。
          const withNeg = Object.assign({}, base, {
            id: "probe-neg", prompt: "a green apple",
            negative_prompt: "blurry, text",
            steps: 25, seed: 7, cfg: 1, sampler: "euler",
            scheduler: "simple", width: 1024, height: 1024,
            scale: null, source_image: null,
          });
          const noNeg = Object.assign({}, withNeg, {
            id: "probe-noneg",
            prompt: "a blue teapot", negative_prompt: "",
          });
          state.gallery.items = [noNeg, withNeg].concat(state.gallery.items || []);
          renderGallery(state.gallery);
          return true;
        })()""")
        time.sleep(0.3)
        tiles = page.eval("""(() => {
          const grab = (i) => {
            const t = document.querySelectorAll("#gallery .tile")[i];
            const btns = [...t.querySelectorAll(".tile-actions button")];
            return {labels: btns.map(b => b.textContent.trim()),
                    // 挤压的症状：文字比按钮宽（nowrap 之下就是溢出画面）
                    over: btns.map(b => Math.round(b.scrollWidth - b.clientWidth))};
          };
          return {withNeg: grab(1), noNeg: grab(0)};
        })()""")
        note("tile_buttons", tiles)
        check("the_tile_offers_both_loads", tiles["withNeg"]["labels"],
              ["载入提示词", "载入参数"])
        widest = max(tiles["withNeg"]["over"])
        note("tile_button_overflow_px", widest)
        check("no_tile_button_text_overflows", widest <= 0, True)
        page.eval('document.getElementById("gallery").scrollTop = 0; true')
        time.sleep(0.25)
        shot("18a_tile_load_buttons.png")

        # 先往表单里放一段**认得出来**的旧内容：这样才能确认「有才写」是**真的没动**，
        # 而不是被清空、也不是写成了别的。
        page.eval("""(() => {
          document.getElementById("prompt").value = "旧的提示词";
          document.getElementById("negative").value = "旧的反向词";
          return true;
        })()""")
        # 第 2 格（有反向词）→ 两段都该被换掉
        page.eval('document.querySelectorAll(\'#gallery .tile '
                  '[data-act="loadprompt"]\')[1].click(); true')
        time.sleep(0.4)
        filled = page.eval("""(() => ({
          prompt: document.getElementById("prompt").value,
          negative: document.getElementById("negative").value,
          msg: document.getElementById("run-msg").textContent.trim(),
        }))()""")
        note("load_prompt_filled", filled)
        check("load_prompt_writes_the_positive_into_its_own_box",
              filled["prompt"], "a green apple")
        check("load_prompt_writes_the_negative_into_its_own_box",
              filled["negative"], "blurry, text")

        # 第 1 格（**没有**反向词）→ 提示词换掉、反向词一个字不动
        page.eval("""(() => {
          document.getElementById("prompt").value = "旧的提示词";
          document.getElementById("negative").value = "旧的反向词";
          return true;
        })()""")
        page.eval('document.querySelectorAll(\'#gallery .tile '
                  '[data-act="loadprompt"]\')[0].click(); true')
        time.sleep(0.4)
        kept = page.eval("""(() => ({
          prompt: document.getElementById("prompt").value,
          negative: document.getElementById("negative").value,
        }))()""")
        check("load_prompt_without_a_negative_leaves_that_box_alone",
              [kept["prompt"], kept["negative"]], ["a blue teapot", "旧的反向词"])

        # 放大模式：那颗按钮**禁用**（这是唯一没有提示词输入框的模式），切回来可用。
        # 只看**前两格**（我造的那两条）—— `#gallery` 里有 100 多格，全量取来比会把
        # 期望值写成 100 多项（第一版就是这么写红的：产品是对的，判据的形状错了）。
        modes = page.eval("""(() => {
          const btns = [...document.querySelectorAll("#gallery .tile")]
              .slice(0, 2)
              .map(t => t.querySelector('[data-act="loadprompt"]'));
          const m = document.getElementById("mode");
          m.value = "upscale"; applyModeUI();
          const off = btns.map(b => b.disabled);
          const title = btns[0].title;
          m.value = "t2i"; applyModeUI();
          return {off, title, backOn: btns.map(b => b.disabled)};
        })()""")
        note("load_prompt_in_upscale_mode", modes)
        check("load_prompt_is_disabled_in_upscale_mode", modes["off"], [True, True])
        check("load_prompt_says_why_it_is_disabled",
              "没有提示词输入框" in (modes["title"] or ""), True)
        check("it_comes_back_when_the_mode_does", modes["backOn"], [False, False])

        # 放大视图：两块各装自己那一段；复制那一族仍然**一段一个动作**（留在那里）
        page.eval('openLightbox("probe-neg"); true')
        time.sleep(0.4)
        lb = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {prompt: q("lb-prompt").textContent,
                  negative: q("lb-negative").textContent,
                  negShown: !q("lb-negative-wrap").classList.contains("hidden"),
                  negCopyShown: !q("lb-copy-negative").classList.contains("hidden"),
                  paramsEnabled: !q("lb-copy-params").disabled,
                  tags: [...document.querySelectorAll("#lightbox .lb-tag")]
                        .map(e => e.textContent)};
        })()""")
        check("the_two_prompts_are_shown_apart",
              [lb["prompt"], lb["negative"]], ["a green apple", "blurry, text"])
        check("each_block_carries_its_own_label", lb["tags"][:2], ["提示词", "反向词"])
        check("the_negative_block_and_button_appear_together",
              [lb["negShown"], lb["negCopyShown"]], [True, True])
        check("copy_params_is_enabled_on_a_run_with_numbers",
              lb["paramsEnabled"], True)
        shot("18b_copy_parts.png")

        # 三段各剪一次：**剪贴板里只有那一段**（复制那一族的口径没变）
        wanted = {"lb-copy": "a green apple",
                  "lb-copy-negative": "blurry, text",
                  "lb-copy-params": "steps=25  seed=7  cfg=1  sampler=euler  "
                                    "scheduler=simple  1024×1024"}
        for btn_id, expect in wanted.items():
            page.click_selector("#" + btn_id)
            time.sleep(0.45)
            try:
                got = page.eval("navigator.clipboard.readText()", timeout=15)
            except Exception as exc:                        # noqa: BLE001
                note("clipboard_readback_" + btn_id,
                     {"result": "SKIP", "why": str(exc)[:120]})
                continue
            check("clipboard_of_" + btn_id, got, expect)

        # 放大视图那个入口也走一遍（它先关窗，那句话才看得见）
        page.eval("""(() => {
          document.getElementById("prompt").value = "";
          document.getElementById("negative").value = "";
          return true;
        })()""")
        page.click_selector("#lb-load-prompt")
        time.sleep(0.5)
        from_lb = page.eval("""(() => ({
          open: document.getElementById("lightbox").open,
          prompt: document.getElementById("prompt").value,
          negative: document.getElementById("negative").value,
        }))()""")
        check("the_lightbox_entry_fills_the_same_boxes",
              [from_lb["open"], from_lb["prompt"], from_lb["negative"]],
              [False, "a green apple", "blurry, text"])

        page.eval("""(() => {
          state.gallery.items = (state.gallery.items || [])
              .filter(x => x.id !== "probe-neg" && x.id !== "probe-noneg");
          closeLightbox();
          fetchGallery();
          return true;
        })()""")

        # --- 19. 日志台：整页切换，四块都在，回来时表单一个字不丢（P20）--------
        # 这几条只有真浏览器量得出来：`hidden` 到底有没有生效、轮询有没有真把
        # 数据画上屏、返回时输入框里的字还在不在。假 DOM 里 `hidden` 只是个
        # 布尔属性，「切过去又切回来」这件事根本没有发生过。
        print("\n[19] 日志台：整页切换 + 四块内容 + 返回不丢表单")
        page.eval("""(() => {
          const m = document.getElementById("mode");
          m.value = "t2i"; applyModeUI();
          return true;
        })()""")
        # 走 `js_set_input`（真的派发 `input`），**不要直接写 `.value`** ——
        # 直接写绕过了页面自己的 input 处理，于是 `#prompt-len` 会停在上一轮的
        # 字数上，而第 20 步那条「计数与框一致」会红在一个**假**的地方
        # （2026-10-01 实测：计数停在 `a green apple` 的 13）。
        page.eval(js_set_input("prompt", "日志台往返用的哨兵提示词"))
        page.eval(js_set_input("negative", "哨兵反向词"))
        page.click_selector("#btn-logs")
        # 等第一趟拉完（`logs-meta` 会从「正在读…」变成时间）
        page.wait_for('!document.getElementById("logs-meta").textContent'
                      '.includes("正在读")', 25, "日志台首趟拉完")
        time.sleep(1.2)          # 再等一轮轮询，确认它是**持续**在刷
        logs = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          const feed = q("log-feed");
          const rows = [...feed.querySelectorAll(".feed-row")];
          const dts = [...q("log-env").querySelectorAll("dt")];
          const envKeys = dts.map(e => e.textContent);
          const bootAt = dts.findIndex(e => e.textContent === "开机脚本");
          const bootDd = bootAt < 0 ? null
              : q("log-env").querySelectorAll("dd")[bootAt];
          return {
            workspaceHidden: q("workspace").hidden,
            logviewShown: !q("logview").hidden,
            focused: document.activeElement ? document.activeElement.id : null,
            envKeys,
            cmd: (q("log-cmd") || {}).textContent || "",
            copyBtn: !!q("btn-copy-cmd"),
            startCmd: ((state.backend || {}).start_command || {}).display || "",
            bootRow: bootAt >= 0,
            bootText: bootDd ? bootDd.textContent.trim() : "",
            bootPresent: !!(state.backend || {}).boot_script
                         && !!state.backend.boot_script.present,
            feedRows: rows.length,
            feedErrors: rows.filter(r => r.className.includes("error")).length,
            meta: q("logs-meta").textContent.trim(),
            backendLabel: q("log-backend-label").textContent.trim(),
            backendLines: q("log-backend").textContent.split("\\n").length,
            workbenchText: q("log-workbench").textContent.trim().slice(0, 200),
            sources: q("log-backend-sources").textContent.trim().slice(0, 400),
          };
        })()""")
        note("log_view", logs)
        check("the_log_view_replaces_the_workspace",
              [logs["workspaceHidden"], logs["logviewShown"]], [True, True])
        check("the_focus_follows_the_view", logs["focused"], "btn-logs-back")
        # 四块都要有内容 —— **空着**是最坏的一种结果：日志页空着看起来像
        # 「什么都没发生」，而那正是出事的时刻。
        check("the_env_block_is_filled", len(logs["envKeys"]) >= 6, True)
        check("the_timeline_has_rows", logs["feedRows"] > 0, True)
        check("the_backend_pane_names_its_file", len(logs["backendLabel"]) > 0, True)
        check("the_backend_pane_has_lines", logs["backendLines"] > 1, True)
        check("the_workbench_pane_is_not_empty", len(logs["workbenchText"]) > 0, True)
        check("the_backend_sources_list_the_candidates",
              "候选" in logs["sources"], True)
        # 启动命令搬到了这里（P20i 起）：告警板撤掉之后，页面**唯一**能拿到那条
        # 铁律 1 命令的地方就是这一行。所以量两件事：它在这儿，且**逐字**等于后端
        # 给的那份（页面不许自己拼第二份）。
        check("the_start_command_lives_here",
              "启动命令" in logs["envKeys"], True)
        check("and_it_is_exactly_the_backends_own_text",
              logs["cmd"] == logs["startCmd"] and len(logs["cmd"]) > 40, True)
        check("and_it_can_be_copied", logs["copyBtn"], True)
        # 开机脚本那一行（P22）：三条起后端的路里，唯一**不在本仓库里**的一条。
        # 这台机器上有没有那个 `.bat` 是**不一定的**（开发机上就没有），所以判据
        # 落在「页面与后端给的那份一致」上 —— 那是页面该守的契约；「有那一行」
        # 是那台机器的事。`present` 为假时页面**不该画**，这一条同时也守住了
        # 「永远画一行、永远写不同源」那种会教人忽略它的体检。
        check("the_boot_script_row_follows_the_payload",
              logs["bootRow"], logs["bootPresent"])
        check("and_it_is_never_drawn_empty",
              (not logs["bootRow"]) or len(logs["bootText"]) > 0, True)
        note("boot_script_row", {"drawn": logs["bootRow"],
                                 "text": logs["bootText"]})
        shot("19a_log_view.png")

        # 那一行长什么样：开发机上没有那个 `.bat`（`present:false` ⇒ 按设计不画），
        # 所以「不同源」这一档只能注入进去看。文案由后端给，注入的就是它的形状。
        page.eval("""(() => {
          if (!state.backend) return false;
          state.backend.boot_script = {
            present: true, ok: false, path: "C:\\\\x\\\\start-comfyui.bat",
            missing: ["HF_ENDPOINT", "HF_HOME"], wrong: [],
            summary: "缺 HF_ENDPOINT、HF_HOME —— 开机起的那个后端跑在另一套环境里，"
                   + "而这里照样写着「后端就绪」。",
          };
          renderLogEnv();
          return true;
        })()""")
        time.sleep(0.25)
        boot = page.eval("""(() => {
          const dts = [...document.querySelectorAll("#log-env dt")];
          const i = dts.findIndex(e => e.textContent === "开机脚本");
          const dd = i < 0 ? null : document.querySelectorAll("#log-env dd")[i];
          return {drawn: !!dd, cls: dd ? dd.className : "",
                  text: dd ? dd.textContent.trim() : ""};
        })()""")
        note("boot_script_drift", boot)
        check("a_drifting_boot_script_is_drawn_and_marked",
              [boot["drawn"], boot["cls"]], [True, "warn"])
        check("and_it_names_which_variables_are_missing",
              "HF_ENDPOINT" in boot["text"] and "另一套环境" in boot["text"], True)
        shot("19b_boot_script_drift.png")

        # 「只看错误」：切过去要**重画**（时间线是只追加的，不画一遍就看不到过滤）。
        # 先塞两条**合成**的记录进去（一错一 info）—— 否则这一条会**空过**：
        # 刚启动的工作台真错误数是 0，于是「过滤后没有 info」和「过滤后什么都没有」
        # 分不出来，而后者是坏掉的样子（第一版就是这么「过」的）。
        page.eval("""(() => {
          const now = Date.now();
          state.logs.rows.push({seq: 90001, ts: now, level: "error", kind: "event",
                                source: "probe-error", event: "probe", detail: null,
                                text: "合成的一条错误（判据用）"});
          state.logs.rows.push({seq: 90002, ts: now, level: "info", kind: "event",
                                source: "probe-info", event: "probe", detail: null,
                                text: "合成的一条 info（判据用）"});
          state.logs.rendered = 0; state.logs.filteredAt = null;
          renderLogs(true);
          return true;
        })()""")
        page.eval("""(() => {
          const box = document.getElementById("logs-errors-only");
          box.checked = true; box.onchange();
          return true;
        })()""")
        time.sleep(0.3)
        only = page.eval("""(() => {
          const rows = [...document.querySelectorAll("#log-feed .feed-row")];
          return {count: rows.length,
                  levels: [...new Set(rows.map(r => r.className))],
                  texts: rows.map(r => r.textContent.trim()),
                  label: document.getElementById("logs-count").textContent};
        })()""")
        note("log_errors_only", only)
        check("the_error_filter_drops_every_info_row",
              [lv for lv in only["levels"] if "info" in lv], [])
        check("the_error_filter_keeps_the_error",
              [any("合成的一条错误" in t for t in only["texts"]),
               any("合成的一条 info" in t for t in only["texts"])], [True, False])
        shot("19b_log_errors_only.png")
        page.eval("""(() => {
          const box = document.getElementById("logs-errors-only");
          box.checked = false; box.onchange(); return true;
        })()""")

        # 返回：**表单里的字必须还在**。这里量的是两个框的值，不是「看起来还在」
        page.click_selector("#btn-logs-back")
        time.sleep(0.3)
        back = page.eval("""(() => ({
          workspaceHidden: document.getElementById("workspace").hidden,
          logviewHidden: document.getElementById("logview").hidden,
          prompt: document.getElementById("prompt").value,
          negative: document.getElementById("negative").value,
          focused: document.activeElement ? document.activeElement.id : null,
        }))()""")
        note("back_from_logs", back)
        check("the_workspace_comes_back", [back["workspaceHidden"], back["logviewHidden"]],
              [False, True])
        check("the_form_survives_the_round_trip",
              [back["prompt"], back["negative"]],
              ["日志台往返用的哨兵提示词", "哨兵反向词"])
        check("the_focus_returns_to_the_entry_button", back["focused"], "btn-logs")

        # --- 20. LLM 增强面板（P20；形状在 2026-10-01 由用户要求改过）----------
        # 一句话：**一颗入口 → 面板里输入与增强 → 采用增强稿**。
        #
        # 这一条的历史：上一版的形状是「每个框一颗按钮 → 点一下就跑 → 弹对照」，
        # 而那颗按钮**会永远卡在「增强中…」**：它自己显示「增强中…」、`finally`
        # 里再「恢复」，而那个「原样」是从 DOM 里读的 —— 失败一次之后
        # `flashButton` 已经把「增强中…」当成原样记进了 `dataset.label`，
        # 于是从那以后每一步都把它「恢复」成「增强中…」。用户实测就是
        # 「401 → 400 → 成功」这个顺序。
        # 所以下面**刻意先失败一次、再成功一次** —— 那才是抓得住这个 bug 的顺序。
        print("\n[20] LLM 增强面板：一颗入口、输入与增强都在面板里、失败一次也不能卡住")
        page.eval("""(() => {
          // 装一层 fetch 替身。**真正那一个要存在闭包里**（`real`），不要存在
          // 全局上：这几处以前各存各的（`window.__real_fetch = window.fetch`），
          // 只要有一层没还回去，下一层存下来的就是**上一个替身**，而替身的兜底
          // 分支又绕回它自己 —— `Maximum call stack size exceeded`，被 `api()`
          // 包成「连不上工作台自己」。2026-10-01 实测骗过一次：第 23 步整段变红，
          // 看起来像画廊坏了。
          window.__fetch_stack = window.__fetch_stack || [];
          if (!window.__fetch_stack.length) window.__fetch_stack.push(window.fetch);
          const real = window.__fetch_stack[0];
          window.__enhance_mode = "ok";
          window.__enhance_calls = [];
          window.fetch = async (path, opts) => {
            if (String(path).indexOf("/api/llm/enhance") === 0) {
              const body = JSON.parse(opts.body);
              window.__enhance_calls.push(body);
              if (window.__enhance_mode === "fail") {
                return new Response(JSON.stringify(
                  {error: "密钥不对或无权限（HTTP 401）", kind: "auth"}),
                  {status: 502, headers: {"Content-Type": "application/json"}});
              }
              return new Response(JSON.stringify({
                ok: true, text: "ENHANCED:" + body.text, model: "stub-model",
                elapsed_ms: 1234, target: body.target,
                usage: {total_tokens: 77},
              }), {status: 200, headers: {"Content-Type": "application/json"}});
            }
            return real(path, opts);
          };
          // 判据不该依赖**这台机器配没配** LLM：明确摆成已配置，然后单独判
          // 「按钮的禁用状态与配置状态一致」那一条（那才是产品要保证的关系）。
          const configured = !!(state.llm && state.llm.settings
                                && state.llm.settings.configured);
          window.__was_configured = configured;
          if (!configured) {
            state.llm = Object.assign({}, state.llm || {}, {
              settings: {configured: true, has_key: true, key_masked: "sk-…stub"},
              targets: [{id: "prompt", label: "提示词", field: "prompt"},
                        {id: "negative", label: "反向词", field: "negative"}],
              actions: [{id: "enhance", label: "增强", noun: "增强稿"},
                        {id: "to_en", label: "译成英文", noun: "译稿"},
                        {id: "to_zh", label: "译成中文", noun: "译稿"}],
            });
          }
          syncEnhanceButtons();
          return true;
        })()""")
        gate = page.eval("""(() => ({
          wasConfigured: window.__was_configured,
          disabled: document.getElementById("btn-enhance").disabled,
          configured: !!(state.llm && state.llm.settings && state.llm.settings.configured),
          oneButton: !document.getElementById("btn-enhance-prompt")
                     && !document.getElementById("btn-enhance-negative"),
        }))()""")
        note("enhance_entry", gate)
        check("there_is_only_one_enhance_entry", gate["oneButton"], True)
        check("the_entry_is_enabled_iff_the_llm_is_configured",
              gate["disabled"], not gate["configured"])

        page.eval(js_set_input("prompt", "一只猫"))
        page.click_selector("#btn-enhance")
        page.wait_for('document.getElementById("enhance").open === true', 15,
                      "增强面板打开")
        time.sleep(0.3)
        opened = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {open: q("enhance").open,
                  targets: [...q("en-target").options].map(o => [o.value, o.textContent]),
                  picked: q("en-target").value,
                  input: q("en-input").value,
                  inputTag: q("en-input").tagName,
                  editable: !q("en-input").readOnly && !q("en-input").disabled,
                  to: q("en-to").textContent,
                  adoptDisabled: q("en-adopt").disabled,
                  title: q("en-title").textContent,
                  focusInside: q("enhance").contains(document.activeElement)};
        })()""")
        note("enhance_panel_opened", opened)
        # 目标表来自后端 —— 页面按它建下拉框（这就是「扩展性」那一半）
        check("the_target_list_comes_from_the_server",
              opened["targets"], [["prompt", "提示词"], ["negative", "反向词"]])
        check("the_panel_opens_on_the_prompt", opened["picked"], "prompt")
        check("the_input_is_prefilled_from_the_form", opened["input"], "一只猫")
        # **输入是 can 编辑的 textarea**（不是上一版那样只读的 <pre>）
        check("the_panel_has_an_editable_input",
              [opened["inputTag"], opened["editable"]], ["TEXTAREA", True])
        check("there_is_no_draft_before_running", "还没有增强稿" in opened["to"], True)
        check("adopt_is_greyed_out_before_running", opened["adoptDisabled"], True)

        # —— 先失败一次 ——
        page.eval('window.__enhance_mode = "fail"; true')
        page.eval(js_set_input("en-input", "一只橘色的猫"))
        page.click_selector("#en-run")
        page.wait_for('document.getElementById("en-status").textContent'
                      '.indexOf("失败") >= 0', 15, "失败落进状态行")
        time.sleep(0.3)
        failed = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {status: q("en-status").textContent,
                  statusBad: q("en-status").classList.contains("bad"),
                  run: q("en-run").textContent.trim(),
                  runDisabled: q("en-run").disabled,
                  adoptDisabled: q("en-adopt").disabled};
        })()""")
        note("enhance_after_a_failure", failed)
        check("a_failure_is_told_inside_the_panel",
              "401" in failed["status"], True)
        check("a_failure_is_marked_as_a_failure", failed["statusBad"], True)
        # **这两条就是那个卡死 bug 的判据**：按钮文字从头到尾不变、而且没被卡住
        # （加翻译之后它叫「执行」：具体做哪一道由紧挨着的下拉框说，按钮文字宁可
        # 泛一点，也不能是运行时状态的函数 —— 那个 bug 就是这么长出来的）
        check("the_run_button_keeps_its_label", failed["run"], "执行")
        check("the_run_button_does_not_stick", failed["runDisabled"], False)

        # —— 再成功一次 ——（同一颗按钮，同一个面板，什么都没重开）
        page.eval('window.__enhance_mode = "ok"; true')
        page.click_selector("#en-run")
        page.wait_for('document.getElementById("en-to").textContent'
                      '.indexOf("ENHANCED:") === 0', 15, "增强稿出现")
        time.sleep(0.3)
        done = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {to: q("en-to").textContent, toCount: q("en-to-count").textContent,
                  meta: q("en-meta").textContent, run: q("en-run").textContent.trim(),
                  runDisabled: q("en-run").disabled,
                  adoptDisabled: q("en-adopt").disabled,
                  adoptLabel: q("en-adopt").textContent.trim(),
                  calls: window.__enhance_calls.slice(),
                  formPrompt: document.getElementById("prompt").value};
        })()""")
        note("enhance_after_success", done)
        check("the_draft_lands_in_the_right_pane", done["to"], "ENHANCED:一只橘色的猫")
        check("the_panel_reports_the_model_and_the_time",
              "stub-model" in done["meta"], True)
        check("the_run_button_still_says_the_same_after_a_success",
              done["run"], "执行")
        check("the_run_button_is_useable_again", done["runDisabled"], False)
        # **输入在面板里**：发出去的是面板 textarea 里的字，不是表单里那一栏
        # （表单里此刻还写着「一只猫」）
        check("it_sends_what_is_typed_in_the_panel",
              [done["calls"][-1]["text"], done["calls"][-1]["target"],
               done["formPrompt"]], ["一只橘色的猫", "prompt", "一只猫"])
        check("adopt_becomes_available_once_there_is_a_draft",
              done["adoptDisabled"], False)
        check("the_adopt_button_says_which_draft_it_takes",
              done["adoptLabel"], "采用增强稿")
        shot("20a_enhance_panel.png")

        # 改了输入 → 那份稿子**立刻作废**（留着一份对不上的稿子是最坏的一种：
        # 「采用」一按就把它写回表单，而它对应的不是你眼前这段字）
        page.eval(js_set_input("en-input", "只改了半句"))
        time.sleep(0.3)
        stale = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {to: q("en-to").textContent, adoptDisabled: q("en-adopt").disabled};
        })()""")
        check("editing_the_input_voids_the_old_draft",
              [stale["to"], stale["adoptDisabled"]], ["（还没有增强稿）", True])

        # 重新增强 → 采用 → 写回**表单里那一栏**
        page.click_selector("#en-run")
        page.wait_for('document.getElementById("en-to").textContent'
                      '.indexOf("ENHANCED:") === 0', 15, "第二份增强稿出现")
        page.click_selector("#en-adopt")
        page.wait_for('document.getElementById("enhance").open === false', 10,
                      "采用后关窗")
        time.sleep(0.3)
        adopted = page.eval("""(() => ({
          open: document.getElementById("enhance").open,
          prompt: document.getElementById("prompt").value,
          len: document.getElementById("prompt-len").textContent,
        }))()""")
        note("enhance_adopted", adopted)
        check("adopting_writes_the_draft_into_the_form",
              adopted["prompt"], "ENHANCED:只改了半句")
        check("adopting_updates_the_counter", adopted["len"], "14")

        # 切换目标：**同一个面板**，不用第二颗按钮
        page.eval(js_set_input("negative", "blurry"))
        page.click_selector("#btn-enhance")
        page.wait_for('document.getElementById("enhance").open === true', 15,
                      "增强面板再次打开")
        page.eval("""(() => {
          const s = document.getElementById("en-target");
          s.value = "negative"; s.onchange();
          return true;
        })()""")
        time.sleep(0.3)
        switched = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {picked: q("en-target").value, input: q("en-input").value,
                  title: q("en-title").textContent,
                  to: q("en-to").textContent};
        })()""")
        note("enhance_switched_target", switched)
        check("switching_the_target_reloads_that_field",
              [switched["picked"], switched["input"], "反向词" in switched["title"]],
              ["negative", "blurry", True])
        page.click_selector("#en-run")
        page.wait_for('document.getElementById("en-to").textContent'
                      '.indexOf("ENHANCED:") === 0', 15, "反向词的稿子出现")
        page.click_selector("#en-adopt")
        page.wait_for('document.getElementById("enhance").open === false', 10,
                      "反向词采用后关窗")
        time.sleep(0.3)
        neg_done = page.eval("""(() => ({
          negative: document.getElementById("negative").value,
          prompt: document.getElementById("prompt").value,
          len: document.getElementById("negative-len").textContent,
          lastTarget: window.__enhance_calls[window.__enhance_calls.length - 1].target,
        }))()""")
        note("enhance_negative_adopted", neg_done)
        check("the_same_panel_serves_the_negative",
              [neg_done["lastTarget"], neg_done["negative"], neg_done["prompt"]],
              ["negative", "ENHANCED:blurry", "ENHANCED:只改了半句"])
        check("the_negative_counter_is_updated", neg_done["len"], "15")

        # Esc = 放弃：改到一半关窗，表单一个字都不能动
        page.eval(js_set_input("prompt", "哨兵：不该被写回"))
        page.click_selector("#btn-enhance")
        page.wait_for('document.getElementById("enhance").open === true', 15,
                      "增强面板第三次打开")
        page.eval(js_set_input("en-input", "面板里改的，按 Esc 应当丢掉"))
        page.press_key("Escape", "Escape", 27)
        page.wait_for('document.getElementById("enhance").open === false', 10,
                      "Esc 关掉面板")
        time.sleep(0.3)
        after_esc = page.eval("""(() => ({
          open: document.getElementById("enhance").open,
          prompt: document.getElementById("prompt").value,
        }))()""")
        check("escape_discards_everything", after_esc["prompt"], "哨兵：不该被写回")
        shot("20b_enhance_target.png")

        # ---- 20c. 参考图（P20e）：说在明处，而且**只有图上会用到的才带** --------
        # 后端那一截（读盘 → 缩图 → 送出去）在单测里（`test_upload` /
        # `test_vision` / `test_llm`）；这一段验的是**页面这一半**：
        # ① 面板要说得出这次带哪几张（模型看到什么，用户得能一眼对上）；
        # ② 请求里真的带上它们；③ 文生图**不带** —— 那时图不参与生成，递给 LLM
        # 会让它照着那张图写提示词，而那张图在出图时根本不存在（没有症状的错）。
        print("\n[20c] 参考图：面板说清带哪几张、请求里真的带上、文生图不带")
        mode_before = page.eval('document.getElementById("mode").value')
        page.eval("window.__refs_before_refstep = state.refs.slice(); true")
        page.eval("""(() => {
          // 装一层 fetch 替身。**真正那一个要存在闭包里**（`real`），不要存在
          // 全局上：这几处以前各存各的（`window.__real_fetch = window.fetch`），
          // 只要有一层没还回去，下一层存下来的就是**上一个替身**，而替身的兜底
          // 分支又绕回它自己 —— `Maximum call stack size exceeded`，被 `api()`
          // 包成「连不上工作台自己」。2026-10-01 实测骗过一次：第 23 步整段变红，
          // 看起来像画廊坏了。
          window.__fetch_stack = window.__fetch_stack || [];
          if (!window.__fetch_stack.length) window.__fetch_stack.push(window.fetch);
          const real = window.__fetch_stack[0];
          window.__enhance_calls = [];
          window.__enhance_status = [];
          window.fetch = async (path, opts) => {
            if (String(path).indexOf("/api/llm/enhance") === 0) {
              const body = JSON.parse(opts.body);
              window.__enhance_calls.push(body);
              // 状态行在**请求飞出去的那一刻**长什么样 —— 回来之后它就被重画了，
              // 所以只能在这里取（这是「发之前就说了要带图」的唯一证据）。
              window.__enhance_status.push(
                document.getElementById("en-status").textContent);
              return new Response(JSON.stringify({
                ok: true, text: "ENHANCED:" + body.text, model: "stub-model",
                elapsed_ms: 1234, target: body.target, action: body.action,
                images: (body.refs || []).length,
                usage: {total_tokens: 77},
              }), {status: 200, headers: {"Content-Type": "application/json"}});
            }
            return real(path, opts);
          };
          return true;
        })()""")
        page.eval(js_set_mode("edit"))
        page.eval('setRefs(["ref_a.png", "ref_b.png"]); true')
        page.click_selector("#btn-enhance")
        page.wait_for('document.getElementById("enhance").open === true', 15,
                      "增强面板（图生图）打开")
        time.sleep(0.3)
        edit_line = page.eval(
            'document.getElementById("en-refs").textContent')
        note("enhance_refs_edit", edit_line)
        check("the_panel_says_which_images_go", edit_line,
              "随附 2 张参考图：ref_a.png、ref_b.png")
        shot("20c_enhance_refs.png")
        page.eval(js_set_input("en-input", "一只猫"))
        page.click_selector("#en-run")
        page.wait_for("window.__enhance_calls.length === 1", 15, "带图的增强回来")
        time.sleep(0.3)
        with_refs = page.eval("""(() => ({
          refs: (window.__enhance_calls[0] || {}).refs || null,
          status: window.__enhance_status[0] || "",
          meta: document.getElementById("en-meta").textContent,
        }))()""")
        note("enhance_with_refs", with_refs)
        check("the_request_carries_the_reference_images", with_refs["refs"],
              ["ref_a.png", "ref_b.png"])
        check("the_status_says_how_many_go_out_before_it_goes",
              "2 张参考图" in with_refs["status"], True)
        check("the_receipt_counts_the_images", "2 张图" in with_refs["meta"], True)

        page.press_key("Escape", "Escape", 27)
        page.wait_for('document.getElementById("enhance").open === false', 10,
                      "Esc 关掉面板")
        page.eval(js_set_mode("t2i"))
        page.click_selector("#btn-enhance")
        page.wait_for('document.getElementById("enhance").open === true', 15,
                      "增强面板（文生图）打开")
        time.sleep(0.3)
        t2i_line = page.eval("""(() => ({
          refs: document.getElementById("en-refs").textContent,
          stillPicked: state.refs.length,
        }))()""")
        note("enhance_refs_t2i", t2i_line)
        check("the_panel_says_the_images_will_not_go", t2i_line["refs"],
              "这次不带图：文生图不会用到参考图")
        # 选中集没被清掉 —— 变的只是「这次发不发」，不是用户选了什么
        check("the_reference_stays_selected_it_just_is_not_sent",
              t2i_line["stillPicked"], 2)
        page.eval(js_set_input("en-input", "一只猫"))
        page.click_selector("#en-run")
        page.wait_for("window.__enhance_calls.length === 2", 15, "不带图的增强回来")
        time.sleep(0.3)
        t2i_sent = page.eval("(window.__enhance_calls[1] || {}).refs || null")
        check("the_t2i_request_carries_no_images", t2i_sent, [])
        shot("20d_enhance_refs_t2i.png")
        page.press_key("Escape", "Escape", 27)
        page.wait_for('document.getElementById("enhance").open === false', 10,
                      "收尾关面板")
        # 把表单还给后面那几步（这一段只是借一下模式与选中集）
        page.eval("setRefs(window.__refs_before_refstep || []); true")
        page.eval(js_set_mode(mode_before))


        # ---- 21. 翻译：同一个面板、同一套 输入 → 稿 → 采用，只多一个下拉框 ----
        # 用户这一轮要的「中英翻译」**不是第二颗按钮**，而是顶栏多一个「做什么」。
        # 这一段盯三处：动作真的发给了后端、右栏与「采用」的说法跟着动作变、
        # 以及「← 移到左边」能把一稿接到下一道上（忠实翻译那一支的全部意义就在于
        # 「先增强、再翻译」两步走 —— 一步到位那种会把用户没写过的元素一起补进去）。
        print("\n[21] 翻译：动作表来自后端、稿子按动作叫名字、接力接得上")
        page.eval("""(() => {
          // 装一层 fetch 替身。**真正那一个要存在闭包里**（`real`），不要存在
          // 全局上：这几处以前各存各的（`window.__real_fetch = window.fetch`），
          // 只要有一层没还回去，下一层存下来的就是**上一个替身**，而替身的兜底
          // 分支又绕回它自己 —— `Maximum call stack size exceeded`，被 `api()`
          // 包成「连不上工作台自己」。2026-10-01 实测骗过一次：第 23 步整段变红，
          // 看起来像画廊坏了。
          window.__fetch_stack = window.__fetch_stack || [];
          if (!window.__fetch_stack.length) window.__fetch_stack.push(window.fetch);
          const real = window.__fetch_stack[0];
          window.__enhance_mode = "ok";
          window.__enhance_calls = [];
          window.fetch = async (path, opts) => {
            if (String(path).indexOf("/api/llm/enhance") === 0) {
              const body = JSON.parse(opts.body);
              window.__enhance_calls.push(body);
              const tag = body.action === "enhance" ? "ENHANCED:" : "TRANSLATED:";
              return new Response(JSON.stringify({
                ok: true, text: tag + body.text, model: "stub-model",
                elapsed_ms: 1234, target: body.target, action: body.action,
                usage: {total_tokens: 77},
              }), {status: 200, headers: {"Content-Type": "application/json"}});
            }
            return real(path, opts);
          };
          return true;
        })()""")
        page.eval(js_set_input("prompt", "一只橘色的猫坐在窗台上"))
        page.click_selector("#btn-enhance")
        page.wait_for('document.getElementById("enhance").open === true', 15,
                      "增强面板打开")
        time.sleep(0.3)
        acts = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {options: [...q("en-action").options].map(o => [o.value, o.textContent]),
                  picked: q("en-action").value,
                  run: q("en-run").textContent.trim(),
                  sendDisabled: q("en-send").disabled};
        })()""")
        note("translate_actions", acts)
        # **动作表来自后端** —— 页面里连「译成英文」这几个字都没有（有源码判据）
        check("the_action_list_comes_from_the_server", acts["options"],
              [["enhance", "增强"], ["to_en", "译成英文"], ["to_zh", "译成中文"]])
        check("the_panel_opens_on_the_default_action", acts["picked"], "enhance")
        check("the_run_button_is_not_an_action_name", acts["run"], "执行")
        check("there_is_nothing_to_move_before_running", acts["sendDisabled"], True)

        # 换成「译成英文」
        page.eval("""(() => {
          const s = document.getElementById("en-action");
          s.value = "to_en"; s.onchange();
          return true;
        })()""")
        time.sleep(0.3)
        picked = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {picked: q("en-action").value,
                  heading: q("en-to-title").textContent,
                  adopt: q("en-adopt").textContent.trim(),
                  status: q("en-status").textContent,
                  input: q("en-input").value};
        })()""")
        note("translate_picked", picked)
        # 右栏标题与「采用」那两个字**跟着动作走**，但都不是页面里写死的
        check("the_draft_is_renamed_by_the_action",
              [picked["heading"], picked["adopt"]], ["译稿", "采用译稿"])
        check("the_status_line_names_the_action",
              "译成英文" in picked["status"], True)
        # **换动作不动左边的字**：那是用户自己写的，跟选哪一道无关
        check("switching_the_action_keeps_what_you_typed",
              picked["input"], "一只橘色的猫坐在窗台上")

        page.click_selector("#en-run")
        page.wait_for('document.getElementById("en-to").textContent'
                      '.indexOf("TRANSLATED:") === 0', 15, "译稿出现")
        time.sleep(0.3)
        translated = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {to: q("en-to").textContent,
                  count: q("en-to-count").textContent,
                  adoptDisabled: q("en-adopt").disabled,
                  sendDisabled: q("en-send").disabled,
                  last: window.__enhance_calls[window.__enhance_calls.length - 1]};
        })()""")
        note("translate_done", translated)
        check("it_sends_the_action_the_user_picked",
              translated["last"]["action"], "to_en")
        check("the_translation_lands_in_the_right_pane",
              translated["to"], "TRANSLATED:一只橘色的猫坐在窗台上")
        check("adopt_becomes_available_for_a_translation",
              translated["adoptDisabled"], False)
        check("the_move_button_becomes_available_too",
              translated["sendDisabled"], False)
        shot("21a_enhance_translate.png")

        # —— 接力：把这一稿放到左边，接着做下一道 ——
        page.click_selector("#en-send")
        time.sleep(0.3)
        chained = page.eval("""(() => {
          const q = (id) => document.getElementById(id);
          return {input: q("en-input").value,
                  to: q("en-to").textContent,
                  adoptDisabled: q("en-adopt").disabled,
                  sendDisabled: q("en-send").disabled,
                  form: document.getElementById("prompt").value,
                  focus: document.activeElement === q("en-input")};
        })()""")
        note("translate_chained", chained)
        check("the_draft_moves_into_the_left_pane",
              chained["input"], "TRANSLATED:一只橘色的猫坐在窗台上")
        # 搬完**右栏立刻作废**（右边那份对应的已经不是左边这段字了）
        check("the_right_pane_is_voided_after_the_move",
              [chained["to"], chained["adoptDisabled"]], ["（还没有译稿）", True])
        # **但它一个字都没动表单** —— 写回表单是「采用」的事
        check("moving_left_does_not_write_the_form",
              chained["form"], "一只橘色的猫坐在窗台上")
        check("the_cursor_is_where_the_next_step_happens", chained["focus"], True)

        # 换到「译成中文」再跑一次：两条方向各走一遍，而且**动作真的换了**
        page.eval("""(() => {
          const s = document.getElementById("en-action");
          s.value = "to_zh"; s.onchange();
          return true;
        })()""")
        page.click_selector("#en-run")
        page.wait_for('document.getElementById("en-to").textContent'
                      '.indexOf("TRANSLATED:") === 0', 15, "中文稿出现")
        time.sleep(0.3)
        other = page.eval("""(() => ({
          last: window.__enhance_calls[window.__enhance_calls.length - 1],
        }))()""")
        check("the_second_direction_goes_out_as_its_own_action",
              other["last"]["action"], "to_zh")
        page.click_selector("#en-adopt")
        page.wait_for('document.getElementById("enhance").open === false', 10,
                      "采用译稿后关窗")
        time.sleep(0.3)
        zh_adopted = page.eval("""(() => ({
          prompt: document.getElementById("prompt").value,
          len: document.getElementById("prompt-len").textContent,
        }))()""")
        note("translate_adopted", zh_adopted)
        check("adopting_a_translation_writes_the_form",
              zh_adopted["prompt"], "TRANSLATED:TRANSLATED:一只橘色的猫坐在窗台上")
        shot("21b_enhance_translate_adopted.png")

        # ---- 22. 出图后放大：父作业下面接着画那一段 ----
        # 用户报的：链上的放大跑起来了，可面板上是「完成 + 100%」，看不出还在忙。
        # 这一段用**注入的作业**驱动真的 `renderLive()`（那块是真 DOM、真 CSS），
        # 量四件事：块画在不在、条子是不是**按时间**长的（而不是一上来就满）、
        # 有没有**假装**它是真实百分比、以及父作业那一栏不再出现「估算」。
        print("\n[22] 出图后放大：父作业下面接着画那一段")
        page.eval("""(() => {
          const now = Date.now();
          state.jobs.set("shot-parent", {
            job_id: "shot-parent", mode: "t2i", status: "done",
            created_at: now - 60000, started_at: now - 60000, ended_at: now - 35000,
            wall_ms: 25000, estimate_s: 67.5,
            params: {mode: "t2i", steps: 20, width: 1024, height: 1024,
                     auto_upscale: {enabled: true, scale: 2}},
            prompt_text: "一只橘色的猫", outputs: [], node_classes: {},
            chain: {status: "submitted", scale: 2, job_id: "shot-kid"},
          });
          state.jobs.set("shot-kid", {
            job_id: "shot-kid", parent_job_id: "shot-parent", mode: "upscale",
            status: "running", created_at: now - 6000, started_at: now - 6000,
            estimate_s: 20.2, params: {mode: "upscale", scale: 2}, prompt_text: "",
            outputs: [], node_id: "3", class_type: "TESpeedVOSR2Image",
            node_classes: {"3": "TESpeedVOSR2Image"},
          });
          // 预置耗时缓存：这两个 id 在服务端并不存在，不预置就会去问一次、拿回
          // 一个「读耗时失败」的盒子，把截图弄脏（那不是这一段要验的东西）
          state.timings.set("shot-parent", {available: false, reason: ""});
          state.currentId = "shot-parent";
          // **切到「当前结果」**：只改 currentId 的话面板确实重画了（判据读得到），
          // 但屏幕还停在画廊那一个标签上 —— 截图会拍到一个空画面。
          // 第一版就是这么假绿的：11 条判据全过，图上什么都没有。
          setResultTab("live", true);
          renderLive();
          return true;
        })()""")
        time.sleep(1.0)
        stage = page.eval("""(() => {
          const s = document.getElementById("stage-chain");
          const bar = s ? s.querySelector(".bar > i") : null;
          const el = s ? s.querySelector("[data-elapsed]") : null;
          return {there: !!s,
                  label: s ? s.querySelector("strong").textContent : null,
                  width: bar ? parseFloat(bar.style.width) : null,
                  elapsed: el ? el.textContent : null,
                  cancel: !!document.getElementById("btn-chain-cancel"),
                  openBtns: s ? s.querySelectorAll(".stage-open").length : null,
                  panel: document.getElementById("live").textContent};
        })()""")
        note("chain_stage_running", stage)
        check("the_chain_stage_is_drawn_under_the_parent", stage["there"], True)
        check("it_names_the_stage_and_the_factor", stage["label"], "出图后放大 ×2")
        check("it_tells_you_how_long_it_has_been_running",
              bool(stage["elapsed"]) and "秒" in stage["elapsed"], True)
        # **按时间走**：跑了 6 秒 / 估算 20 秒 ⇒ 一条真在长、但没满的条子
        check("the_bar_moves_by_time_not_by_nothing",
              [stage["width"] > 2, stage["width"] < 95], [True, True])
        # 而且**不假装** —— 这句话必须在块里（没有它，那条就是我们编的百分比）
        check("it_does_not_pretend_the_bar_is_real_progress",
              "不是真实进度" in stage["panel"], True)
        # 父作业那一栏**不再有估算**（这一轮按用户要求撤掉了）
        check("the_parent_panel_no_longer_shows_an_estimate",
              "估算" in stage["panel"], False)
        check("it_can_be_cancelled_from_where_you_are_looking", stage["cancel"], True)
        check("there_is_nothing_to_open_before_it_finishes", stage["openBtns"], 0)
        shot("22a_chain_running.png")

        # 放大跑完 → 同一块变成完成态，并给一个「看放大后的图」的落点
        page.eval("""(() => {
          const kid = state.jobs.get("shot-kid");
          kid.status = "done"; kid.wall_ms = 25300; kid.ended_at = Date.now();
          renderLive();
          return true;
        })()""")
        time.sleep(0.4)
        stage_done = page.eval("""(() => {
          const s = document.getElementById("stage-chain");
          const btn = s ? s.querySelector(".stage-open") : null;
          return {text: s ? s.textContent : "",
                  openBtns: s ? s.querySelectorAll(".stage-open").length : null,
                  target: btn ? btn.dataset.job : null,
                  cancel: !!document.getElementById("btn-chain-cancel")};
        })()""")
        note("chain_stage_done", stage_done)
        check("the_stage_reports_the_finished_upscale",
              ["完成" in stage_done["text"], "25 秒" in stage_done["text"]],
              [True, True])
        check("it_offers_a_way_to_see_the_upscaled_image", stage_done["openBtns"], 1)
        check("that_button_points_at_the_child_job", stage_done["target"], "shot-kid")
        check("the_cancel_button_is_gone_once_it_is_done", stage_done["cancel"], False)
        shot("22b_chain_done.png")

        # 收拾：注入的那两个作业要撤掉（下一次跑不该看到这一轮的假作业）
        page.eval("""(() => {
          state.jobs.delete("shot-parent"); state.jobs.delete("shot-kid");
          state.timings.delete("shot-parent");
          state.currentId = null;
          renderLive(); renderJobs();
          return true;
        })()""")

        # 收拾：把 fetch 还回去，两个框清干净（下一次跑不该看到这一轮的哨兵）
        page.eval("""(() => {
          window.fetch = (window.__fetch_stack || [])[0] || window.fetch;
          window.__fetch_stack = [];
          document.getElementById("prompt").value = "";
          document.getElementById("negative").value = "";
          document.getElementById("prompt-len").textContent = "0";
          document.getElementById("negative-len").textContent = "0";
          scheduleBuild();
          return true;
        })()""")

        # [23] 画廊：每页 3 行、全选本页、删除（P20f）
        #
        # 分页的页大小是**实测量的列数 × 3 行**，所以判据必须量「屏幕上真的有
        # 三行」——「有 18 个格子」验不出这件事（窄屏下 18 个格子是 18 行）。
        print("\n[23] 画廊：每页 3 行、全选本页、删除")
        page.eval(js_click("tab-gallery"))
        # 上一段按下了「改写」那颗 chip（第 16b 步），只剩两张图 —— 一个两张图的
        # 画廊验不了分页，而分页正是这一步要看的东西。所以先回到「全部」，
        # 顺手把可能还开着的放大视图关掉（模态对话框会把后面的点击全吃掉）。
        page.eval("""(() => {
          closeLightbox();
          state.galleryMode = "";
          state.galleryPage = 0;
          state.galleryQuery = "";
          document.getElementById("gallery-search").value = "";
          const chip = document.querySelector('#gallery-chips .chip[data-mode=""]');
          if (chip) chip.click();
          return true;
        })()""")
        after_reset = None
        for _ in range(30):
            after_reset = page.eval("""(() => ({
              mode: state.galleryMode, page: state.galleryPage,
              cols: state.galleryCols, limit: (state.gallery || {}).limit,
              total: (state.gallery || {}).total,
              resultTab: state.resultTab,
              hidden: document.getElementById("view-gallery").hidden,
              tiles: document.querySelectorAll("#gallery .tile").length,
              ghtml: document.getElementById("gallery").textContent.slice(0, 120),
            }))()""")
            if after_reset["total"] > 20:
                break
            time.sleep(0.5)
        note("gallery_after_reset", after_reset)
        check("the_gallery_is_back_to_everything_after_the_chip_is_cleared",
              after_reset["total"] > 20, True)
        page.wait_for('document.querySelectorAll("#gallery .tile").length > 0')
        # 切进画廊会触发一次「重量列数 → 重取」，等它落定：第一页要么正好排满
        # 三行，要么已经把整个画廊摆完了（图不够一页时就是这个形态）。
        grid = page.wait_for("""(() => {
          const g = document.getElementById("gallery");
          const tiles = [...g.querySelectorAll(".tile")];
          if (!tiles.length) return null;
          const top = tiles[0].offsetTop;
          let cols = 0;
          for (const t of tiles) { if (t.offsetTop !== top) break; cols++; }
          const d = state.gallery || {};
          if (!d.limit || cols < 1) return null;
          if (tiles.length !== Math.min(cols * 3, d.total)) return null;   // 还在重取
          return {tiles: tiles.length, cols, rows: new Set(tiles.map(t => t.offsetTop)).size,
                  limit: d.limit, total: d.total, scanned: d.scanned,
                  info: document.getElementById("gallery-page-info").textContent};
        })()""", 25, "画廊按 3 行排好")
        note("gallery_page", grid)
        # 「有 18 个格子」验不出「每页 3 行」（窄屏下 18 个格子是 18 行），
        # 所以判据量的是**行**。
        check("the_page_never_shows_more_than_three_rows", grid["rows"] <= 3, True)
        more = grid["total"] > grid["cols"] * 3      # 不止一页
        if more:
            check("a_full_page_is_exactly_three_rows",
                  [grid["tiles"], grid["rows"]], [grid["cols"] * 3, 3])
        else:
            # 图不够一页时「三行」无从谈起 —— 这时要判的是**没有漏掉**。
            # 刻意打一行出来（跳过不是失败，但要看得见）。
            note("gallery_single_page",
                 "这批图不足一页（%d 张 / %d 列），分页那几条改判「没漏掉」"
                 % (grid["total"], grid["cols"]))
            check("everything_fits_on_the_one_page", grid["tiles"], grid["total"])
        check("the_pager_says_which_page_and_how_many",
              [grid["info"].startswith("第 1 / "), "共 %d 张" % grid["total"] in grid["info"]],
              [True, True])
        shot("23a_gallery_page1.png")
        # 分页条在网格**下面** —— 滚到底必须看得见它，否则「下一页」等于没有入口。
        page.eval("""(() => {
          const v = document.getElementById("view-gallery");
          v.scrollTop = v.scrollHeight;
          return true;
        })()""")
        time.sleep(0.4)
        pager = page.eval("""(() => {
          const el = document.getElementById("gallery-pager");
          const r = el.getBoundingClientRect();
          return {hidden: el.hidden, inView: r.top < window.innerHeight && r.bottom > 0,
                  prevOff: document.getElementById("btn-page-prev").disabled,
                  nextOff: document.getElementById("btn-page-next").disabled};
        })()""")
        note("gallery_pager", pager)
        check("the_pager_is_visible_once_you_scroll_to_the_bottom",
              [pager["hidden"], pager["inView"]], [False, True])
        check("on_the_first_page_only_next_is_usable",
              [pager["prevOff"], pager["nextOff"]], [True, False])
        shot("23e_gallery_pager.png")
        page.eval('document.getElementById("view-gallery").scrollTop = 0; true')
        time.sleep(0.3)

        # 勾选框与参考图那颗 `×` 同一种做法：悬停才浮出来，**勾上了就常显**。
        # 量的是计算样式里的 opacity（元素一直在 DOM 里，「在不在」验不出这件事）。
        pick_sel = "#gallery .tile .tile-pick"
        box = page.center_of(pick_sel)
        page.call("Input.dispatchMouseEvent",
                  {"type": "mouseMoved", "x": 4, "y": 4, "buttons": 0})
        time.sleep(0.4)
        opacity_rest = page.eval(
            "getComputedStyle(document.querySelector(%s)).opacity" % json.dumps(pick_sel))
        page.call("Input.dispatchMouseEvent",
                  {"type": "mouseMoved", "x": box[0], "y": box[1], "buttons": 0})
        time.sleep(0.4)
        opacity_hover = page.eval(
            "getComputedStyle(document.querySelector(%s)).opacity" % json.dumps(pick_sel))
        note("pick_box_opacity", {"rest": opacity_rest, "hover": opacity_hover})
        check("the_pick_box_appears_on_hover", [opacity_rest, opacity_hover], ["0", "1"])

        page.click_selector("#gallery .tile .tile-pick input")
        picked = page.eval("""(() => {
          const t = document.querySelector("#gallery .tile");
          return {sel: document.querySelectorAll("#gallery .tile.sel").length,
                  first: t.classList.contains("sel"),
                  label: document.getElementById("btn-delete").textContent,
                  off: document.getElementById("btn-delete").disabled,
                  hint: document.getElementById("btn-delete").title};
        })()""")
        note("picked_one", picked)
        check("clicking_the_box_marks_that_one_tile", [picked["sel"], picked["first"]],
              [1, True])
        check("the_box_does_not_open_the_lightbox_as_well",
              page.eval("!document.getElementById('lightbox').open"), True)
        check("the_archive_button_counts_what_is_ticked", picked["label"],
              "归档选中的 1 张")
        check("and_it_became_clickable", picked["off"], False)
        # 鼠标挪开：勾上的那一格必须还看得见（藏起来等于让用户看不见自己选了谁）
        page.call("Input.dispatchMouseEvent",
                  {"type": "mouseMoved", "x": 4, "y": 4, "buttons": 0})
        time.sleep(0.4)
        opacity_after = page.eval(
            "getComputedStyle(document.querySelector(%s)).opacity" % json.dumps(pick_sel))
        check("a_ticked_box_stays_visible_when_the_mouse_leaves", opacity_after, "1")
        # 键盘那半：聚焦到一个**没勾**的格子，它也要现身 —— 藏起来又够不着，
        # 等于「选中这张」这个动作对键盘用户不存在。`opacity` 是过渡属性，
        # 所以要等它走完再读（读早了会读到动画中途的值）。
        page.eval("""(() => {
          const b = document.querySelectorAll("#gallery .tile .tile-pick input")[1];
          if (b) b.focus();
          return !!b;
        })()""")
        time.sleep(0.4)
        keyboard_op = page.eval("""(() => {
          const b = document.querySelectorAll("#gallery .tile .tile-pick")[1];
          return b ? getComputedStyle(b).opacity : "第二格不存在";
        })()""")
        page.eval("document.activeElement.blur()")
        check("a_focused_box_shows_up_too", keyboard_op, "1")
        shot("23b_gallery_picked.png")

        # 全选 = 本页：格子全勾上，而总数**大于**一页（所以这条不是空转）
        page.eval(js_click("btn-pick-all"))
        time.sleep(0.3)
        allsel = page.eval("""(() => ({
          tiles: document.querySelectorAll("#gallery .tile").length,
          sel: document.querySelectorAll("#gallery .tile.sel").length,
          boxes: document.querySelectorAll("#gallery .tile input[data-pick]:checked").length,
          label: document.getElementById("btn-delete").textContent,
          total: (state.gallery || {}).total,
        }))()""")
        note("picked_all_on_page", allsel)
        check("select_all_ticks_every_tile_on_the_page",
              [allsel["sel"], allsel["boxes"]], [allsel["tiles"], allsel["tiles"]])
        if more:
            check("select_all_stops_at_the_page", allsel["tiles"] < allsel["total"], True)
        check("and_the_button_says_so", allsel["label"],
              "归档选中的 %d 张" % allsel["tiles"])
        shot("23c_gallery_select_all.png")

        # 翻页：内容换了、选中集**清空**（不许出现「看不见但已被勾中」的图）
        first_id = page.eval('document.querySelector("#gallery .tile").dataset.id')
        page.eval(js_click("btn-page-next"))
        time.sleep(0.8)
        page2 = page.eval("""(() => ({
          first: document.querySelector("#gallery .tile").dataset.id,
          sel: document.querySelectorAll("#gallery .tile.sel").length,
          info: document.getElementById("gallery-page-info").textContent,
          label: document.getElementById("btn-delete").textContent,
        }))()""")
        if more:
            note("gallery_page2", page2)
            check("the_next_page_shows_other_images", page2["first"] != first_id, True)
            check("the_pager_moved", page2["info"].startswith("第 2 / "), True)
            # 「翻页会丢掉选中集」只有在**真的翻得动**的时候才验得了：
            # 只有一页时那颗键是灰的，点它什么都没发生（选中集当然还在）。
            check("flipping_the_page_drops_the_selection", page2["sel"], 0)
            check("nothing_is_ticked_going_into_the_next_page", page2["label"],
                  "归档选中的 0 张")
        else:
            # 只有一页：翻页键该是灰的（一颗点了没反应的按钮不解释自己）
            note("gallery_no_page_two", page2["info"])
            check("with_a_single_page_there_is_nothing_to_flip_to",
                  page.eval("document.getElementById('btn-page-next').disabled"), True)

        # 归档这一步**打桩**。真归档会动现场 output/ 里那 23 张真图 ——
        # 自动化测试不许碰它们（那条路径由 API 层对着临时目录真跑，见
        # `test_api.GalleryArchive`；端点到真目录那一跳由 `logs/gallery_archive_e2e.py` 验）。
        # 这里验的是**页面发出去的东西**与**收尾**。
        #
        # P20h 起，同一颗按钮在正常档打的是 `/api/gallery/archive`（不是 delete）——
        # 桩要跟着端点走，否则这条会变成「发了个没人拦的请求」，而判据读到的
        # `__arch` 永远是空的（一条**看着像在跑**的判据）。
        page.eval("""(() => {
          // 替身只拦 `/api/gallery/archive`，其余一律交给**真的那个**（闭包里的
          // `real`，见上面那段注释：存全局会被下一层替身覆盖，然后绕回自己）。
          window.__fetch_stack = window.__fetch_stack || [];
          if (!window.__fetch_stack.length) window.__fetch_stack.push(window.fetch);
          const real = window.__fetch_stack[0];
          window.__arch = [];
          window.__confirmed = 0;
          window.confirm = () => { window.__confirmed += 1; return true; };
          window.fetch = (path, opts) => {
            if (path === "/api/gallery/archive") {
              const body = JSON.parse(opts.body);
              window.__arch.push(body);
              return Promise.resolve({ok: true, status: 200, json: async () => ({
                archived: body.items.map((i) => ({root: i.root, name: i.name, bytes: 1400000})),
                failed: [], bytes: body.items.length * 1400000,
                count: body.items.length})});
            }
            return real(path, opts);
          };
          return true;
        })()""")
        page.eval(js_click("btn-page-prev"))
        time.sleep(0.8)
        page.eval(js_click("btn-pick-all"))
        time.sleep(0.3)
        page.click_selector("#btn-delete")
        page.wait_for("window.__arch.length > 0", 10, "归档请求发出去了")
        time.sleep(1.2)
        deleted = page.eval("""(() => {
          const tiles = [...document.querySelectorAll("#gallery .tile")];
          const body = window.__arch[0];
          return {items: body.items.length,
                  shape: body.items.every((i) => typeof i.root === "number"
                                             && typeof i.name === "string"
                                             && i.name.endsWith(".png")),
                  names: body.items.map((i) => i.name).sort(),
                  tiles: tiles.length,
                  tileNames: tiles.map((t) => t.dataset.id.split(":").slice(1).join(":"))
                                .sort(),
                  confirmed: window.__confirmed,
                  msg: document.getElementById("gallery-msg").textContent,
                  sel: document.querySelectorAll("#gallery .tile.sel").length};
        })()""")
        note("gallery_archive", {"items": deleted["items"], "tiles": deleted["tiles"],
                                 "confirmed": deleted["confirmed"],
                                 "msg": deleted["msg"]})
        check("the_request_names_one_png_per_ticked_tile",
              [deleted["items"], deleted["shape"]], [deleted["tiles"], True])
        check("and_those_are_the_tiles_that_were_on_screen",
              deleted["names"], deleted["tileNames"])
        check("archiving_asks_nothing_first", deleted["confirmed"], 0)
        check("the_notice_says_where_they_went",
              ["张 → 已归档（可还原" in deleted["msg"],
               "MB" in deleted["msg"]], [True, True])
        # 用户点名的：**不列文件名**（那是他刚在屏幕上勾过的东西，再念一遍是噪音）
        check("the_notice_does_not_list_file_names", ".png" in deleted["msg"], False)
        check("the_selection_is_empty_afterwards", deleted["sel"], 0)
        shot("23d_gallery_archived.png")

        # 收拾：fetch 与 confirm 还回去，消息清掉（下一次跑不该看到这一轮的哨兵）
        page.eval("""(() => {
          window.fetch = (window.__fetch_stack || [])[0] || window.fetch;
          window.__fetch_stack = [];
          delete window.confirm;
          delete window.__arch;
          document.getElementById("gallery-msg").textContent = "";
          return true;
        })()""")

        # ---- 24. 「秒完成、却没有图」到底发生了什么（P20g）----
        # 用户报的：原参数重复生图 → 秒完成、却没有任何图像产生。查下去是 ComfyUI
        # 的缓存：它认的是**节点输入**，一字不改地再提一遍就一个节点都不跑，只把
        # 上一次的产物名回放给 history —— 那张要是已经被删（P20f 的删除是进回收站），
        # 就既没有新图、也拿不回旧图。
        # 两边各修一半：① 服务端每次提交都换收尾节点的前缀（保证写得下新文件，
        # 见 `qwen21_build.unique_save_prefix`）；② 页面把「这次没有重新采样」说出来，
        # 并且**当那个名字对应的文件已经不在磁盘上时**别只留一张破图。
        print("\n[24] 缓存命中：说清「这次没有重新采样」，也说清那张图还在不在")
        cached_step = page.eval("""(async () => {
          const now = Date.now();
          const classes = {"459:451": "UNETLoader", "459:452": "TextEncodeQwenImage21",
                           "459:458": "KSampler", "461": "SaveImageAdvanced"};
          const up = ["459:451", "459:452", "459:458"];
          const job = (id, over) => state.jobs.set(id, Object.assign({
            job_id: id, mode: "edit", status: "done",
            created_at: now - 900, started_at: now - 900, ended_at: now - 500,
            wall_ms: 412, params: {mode: "edit", steps: 20, seed: 7391818},
            prompt_text: "同一份参数再提一遍", outputs: [],
            node_classes: classes, node_count: 4,
          }, over));
          // 修好之后的形态：上游全在缓存里，收尾那个（每次都跑）不在
          job("shot-all", {cached_nodes: up.slice()});
          // 修之前留下的老记录：连收尾都在缓存里（那时它一个节点都没跑）
          job("shot-legacy", {cached_nodes: up.concat(["461"])});
          // 只有一部分命中 —— 照旧叫「温启动」
          job("shot-warm", {cached_nodes: ["459:451"]});
          // 产物名对应的文件已经不在磁盘上了。`gone` / `on_disk` 是**后端现算的**
          // 那一份（P20j）—— 真实记录里这两个字段由 `/api/jobs` 给，页面据此
          // 把「产物不在了」与「后端没在跑」分开说。夹具不带上它，测的就是
          // 「页面在猜磁盘」，而那正是这一轮要拆掉的东西。
          job("shot-gone", {cached_nodes: up.slice(), gone: true, on_disk: false,
                            outputs: [{filename: "__shot_missing.png",
                                       subfolder: "", type: "output"}]});
          for (const id of ["shot-all", "shot-legacy", "shot-warm", "shot-gone"]) {
            state.timings.set(id, {available: false, reason: ""});
          }
          setResultTab("live", true);
          const render = (id) => { state.currentId = id; renderLive(); };
          const panel = () => document.getElementById("live");
          const meta = () => panel().querySelector(".meta").textContent;
          const caption = () => (panel().querySelector("figcaption") || {}).textContent || "";

          render("shot-all");   const all = {meta: meta(), caption: caption()};
          render("shot-legacy"); const legacy = {meta: meta()};
          render("shot-warm");  const warm = {meta: meta()};

          // ③ 产物不在磁盘上：等浏览器自己报错（判据是它给的 error，不是我们猜磁盘）
          render("shot-gone");
          let gone = {broken: false, caption: caption()};
          for (let i = 0; i < 50; i++) {
            const img = panel().querySelector("figure img");
            if (img && img.classList.contains("broken")) {
              gone = {broken: true, caption: caption()};
              break;
            }
            await new Promise((r) => setTimeout(r, 100));
          }

          // ④ 反面对照：一张**真存在**的图不能被标成「不在磁盘上」
          const g = await (await fetch("/api/gallery")).json();
          const real = (g.items || [])[0];
          let realShot = null;
          if (real) {
            job("shot-real", {cached_nodes: up.slice(),
                              outputs: [{filename: real.filename, subfolder: "",
                                         type: "output"}]});
            state.timings.set("shot-real", {available: false, reason: ""});
            render("shot-real");
            for (let i = 0; i < 50; i++) {
              const img = panel().querySelector("figure img");
              if (img && img.complete && img.naturalWidth > 0) break;
              await new Promise((r) => setTimeout(r, 100));
            }
            const img = panel().querySelector("figure img");
            realShot = {loaded: !!(img && img.naturalWidth > 0),
                        broken: !!(img && img.classList.contains("broken")),
                        caption: caption()};
          }
          return {all, legacy, warm, gone, real: realShot};
        })()""")
        note("cache_hit_cards", cached_step)
        check("an_all_cached_resubmit_says_it_did_not_sample_again",
              "没有重新采样" in cached_step["all"]["meta"], True)
        # 修之前留下的记录（连收尾都在缓存里）也要说得出同一句话 —— 老记录还在列表里
        check("the_records_from_before_the_fix_say_it_too",
              "没有重新采样" in cached_step["legacy"]["meta"], True)
        check("a_partial_hit_still_reads_as_a_warm_start",
              ["温启动（1 个节点走缓存）" in cached_step["warm"]["meta"],
               "没有重新采样" in cached_step["warm"]["meta"]], [True, False])
        gone_shot = cached_step["gone"]
        check("a_missing_product_says_so_instead_of_showing_a_broken_image",
              gone_shot["broken"], True)
        check("and_it_names_the_next_step",
              "产物已经不在了" in gone_shot["caption"]
              and "重出一次" in gone_shot["caption"], True)
        if cached_step["real"] is not None:
            check("an_image_that_is_really_there_is_not_called_missing",
                  [cached_step["real"]["loaded"], cached_step["real"]["broken"],
                   "取不到图" in cached_step["real"]["caption"]],
                  [True, False, False])
        else:
            note("no_real_image_for_the_control", "画廊是空的，反面对照这一条没跑")
        page.eval('state.currentId = "shot-all"; renderLive(); true')
        time.sleep(0.4)
        shot("24a_cached_all.png")
        page.eval('state.currentId = "shot-gone"; renderLive(); true')
        time.sleep(1.2)          # 等浏览器的 error 事件把那张破图换掉
        shot("24b_shot_missing.png")

        # 收拾：注入的作业与耗时缓存都要撤掉（下一次跑不该看到这一轮的假作业）
        page.eval("""(() => {
          for (const id of ["shot-all", "shot-legacy", "shot-warm",
                            "shot-gone", "shot-real"]) {
            state.jobs.delete(id); state.timings.delete(id);
          }
          state.currentId = null;
          renderLive(); renderJobs();
          return true;
        })()""")

        # ---- 25. 「已归档」那一档（P20h）----
        # 归档把「删掉」变成「搬走」：正常档那颗按钮打 archive，那一档里换成
        # 还原 / 彻底删除。这一步走**真的 UI 与真的后端**（那一档扫的就是真归档目录），
        # 只在两处打桩：真归档会动现场 output/ 的真图（不许），真还原会把夹具搬回
        # output/（也不许）—— 那两条路由由 API 层对临时目录真跑
        # （`test_api.GalleryArchive`），端点到真目录那一跳由
        # `logs/gallery_archive_e2e.py` 真跑。
        #
        # 夹具是**复制**一张真图到归档目录里（只读源文件，不动它），跑完删掉自己那份。
        # 为什么不自己画一张小 PNG：读不出元数据的图在格子上是「?」占位块，
        # 那样这一档的图取不取得到就完全没人验了 —— 而那正是最该验的一跳。
        print("\n[25] 已归档那一档：格子来自归档目录，按钮换成还原 / 彻底删除")
        page.eval("setResultTab('gallery', true)")
        time.sleep(0.8)
        arch_ctx = page.eval("""(async () => {
          const r = await (await fetch("/api/gallery?limit=1")).json();
          const it = (r.items || [])[0];
          const a = await (await fetch("/api/gallery?archived=1")).json();
          return {dir: ((a.roots || [])[0] || {}).path || "",
                  srcRoot: it ? ((r.roots || []).find((x) => x.index === it.root) || {}).path : "",
                  srcName: it ? it.filename : ""};
        })()""")
        note("archive_dir", arch_ctx)
        check("the_backend_reports_an_archive_directory", bool(arch_ctx["dir"]), True)
        fixture = "Qwen_image_2.1_zzpageshot_00001.png"
        arch_path = os.path.join(arch_ctx["dir"], fixture)
        have_fixture = bool(arch_ctx["srcRoot"] and arch_ctx["srcName"])
        if have_fixture:
            os.makedirs(arch_ctx["dir"], exist_ok=True)
            shutil.copyfile(os.path.join(arch_ctx["srcRoot"], arch_ctx["srcName"]),
                            arch_path)

        if have_fixture:
            page.eval(js_click("btn-archived"))
            time.sleep(1.4)
            stage = page.eval("""(() => {
              const del = document.getElementById("btn-delete");
              const back = document.getElementById("btn-restore");
              const img = document.querySelector("#gallery .tile img");
              const tiles = [...document.querySelectorAll("#gallery .tile")];
              return {archived: state.galleryArchived,
                      receipt: (state.gallery || {}).archived === true,
                      pressed: document.getElementById("btn-archived").getAttribute("aria-pressed"),
                      count: document.getElementById("gallery-count").textContent,
                      del: del.textContent, back: back.textContent,
                      backHidden: back.hidden,
                      names: tiles.map((t) => t.dataset.id.split(":").slice(1).join(":")),
                      src: img ? img.getAttribute("src") : "",
                      loaded: !!(img && img.naturalWidth > 0)};
            })()""")
            note("archived_stage", stage)
            check("the_toggle_switches_the_stage",
                  [stage["archived"], stage["receipt"], stage["pressed"]],
                  [True, True, "true"])
            check("the_heading_says_it_is_the_archived_stage",
                  "张已归档" in stage["count"], True)
            check("the_buttons_change_meaning",
                  [stage["del"], stage["back"], stage["backHidden"]],
                  ["彻底删除选中的 0 张", "还原选中的 0 张", False])
            check("the_archived_tile_is_on_screen", stage["names"], [fixture])
            # 这一档的图**不在** `candidate_roots()` 里，取图那条路也得跟着换根 ——
            # 少了它，每一格都 404，更坏的一种是取到 output/ 里同名的另一张。
            check("the_image_request_also_asks_for_the_archive_root",
                  "archived=1" in stage["src"], True)
            check("and_that_image_really_loaded", stage["loaded"], True)

            page.click_selector("#gallery .tile .tile-pick input")
            time.sleep(0.4)
            ticked = page.eval("""(() => ({
              del: document.getElementById("btn-delete").textContent,
              back: document.getElementById("btn-restore").textContent,
              sel: document.querySelectorAll("#gallery .tile.sel").length}))()""")
            check("both_buttons_count_the_same_ticked_set",
                  [ticked["del"], ticked["back"], ticked["sel"]],
                  ["彻底删除选中的 1 张", "还原选中的 1 张", 1])
            shot("25a_archived_stage.png")

            # 还原打桩（真还原会把夹具搬回 output/ —— 那是用户的作品，不许碰）。
            # 这里要验的是**请求的形状**：归档里的东西只有一个来源，所以**不给 root**。
            page.eval("""(() => {
              window.__fetch_stack = window.__fetch_stack || [];
              if (!window.__fetch_stack.length) window.__fetch_stack.push(window.fetch);
              const real = window.__fetch_stack[0];
              window.__back = [];
              window.fetch = (path, opts) => {
                if (path === "/api/gallery/restore") {
                  const body = JSON.parse(opts.body);
                  window.__back.push(body);
                  return Promise.resolve({ok: true, status: 200, json: async () => ({
                    restored: body.items.map((i) => ({name: i.name, bytes: 900})),
                    failed: [], bytes: body.items.length * 900,
                    count: body.items.length})});
                }
                return real(path, opts);
              };
              return true;
            })()""")
            page.click_selector("#btn-restore")
            page.wait_for("window.__back.length > 0", 10, "还原请求发出去了")
            time.sleep(1.2)
            back = page.eval("""(() => {
              const body = window.__back[0];
              return {items: body.items.length,
                      names: body.items.map((i) => i.name),
                      extra: Object.keys(body.items[0]).filter((k) => k !== "name"),
                      msg: document.getElementById("gallery-msg").textContent};
            })()""")
            note("archived_restore", back)
            check("restore_asks_for_one_name_per_ticked_tile", back["names"], [fixture])
            check("restore_does_not_send_a_root", back["extra"], [])
            check("restore_says_where_it_went", "已还原回 output/" in back["msg"], True)

            # 「从最近任务里移除」正是用户要的那一件事 —— 单独量一遍，**配一个对照**：
            # 同一条作业产物取不到时那一行不在、取得到时在。只量「不在了」的话，
            # 一个「列表根本没画」（比如 renderJobs 提前 return）也会让它变绿。
            #
            # P20j 起判据是 `gone`（有产物、但一张都取不到了），不再是 `archived`：
            # 归档的那张也算 gone（它还多一个 restorable），所以这对照仍然成立，
            # 而「归档过又被彻底删除」那一种也不再从两值判据的缝里漏回来。
            rows = page.eval("""(() => {
              const mk = (id, gone) => {
                state.jobs.set(id, {job_id: id, mode: "edit", status: "done",
                  created_at: Date.now(), params: {steps: 20, seed: 1},
                  prompt_text: "任务行对照", gone: gone, on_disk: !gone,
                  archived: gone, restorable: gone,
                  node_classes: {"461": "SaveImageAdvanced"}, node_count: 1,
                  outputs: [{filename: %s, subfolder: "", type: "output"}]});
              };
              mk("row-archived", true);
              mk("row-normal", false);
              renderJobs();
              const ids = [...document.querySelectorAll("#jobs .job-row")]
                .map((r) => r.dataset.job);
              return {ids: ids, count: document.getElementById("jobs-count").textContent};
            })()""" % json.dumps(fixture))
            note("jobs_list_filter", rows)
            check("a_row_whose_product_is_gone_is_not_drawn",
                  "row-archived" in rows["ids"], False)
            check("a_normal_one_keeps_its_row", "row-normal" in rows["ids"], True)

            # 大卡片：产物已归档的作业**不许再画一张取不到的图** —— 那会走
            # 「多半是被删了」那句话，把「我们搬走了」说成「没了」。
            live = page.eval("""(async () => {
              const now = Date.now();
              state.jobs.set("shot-arch", {
                job_id: "shot-arch", mode: "edit", status: "done",
                created_at: now - 900, started_at: now - 900, ended_at: now - 500,
                wall_ms: 500, params: {mode: "edit", steps: 20, seed: 1},
                prompt_text: "已经归档的那张", archived: true,
                node_classes: {"461": "SaveImageAdvanced"}, node_count: 1,
                outputs: [{filename: %s, subfolder: "", type: "output"}]});
              state.timings.set("shot-arch", {available: false, reason: ""});
              setResultTab("live", true);
              state.currentId = "shot-arch";
              renderLive();
              await new Promise((r) => setTimeout(r, 400));
              const panel = document.getElementById("live");
              const btn = panel.querySelector("#btn-unarchive");
              return {hasImg: !!panel.querySelector("figure img"),
                      text: panel.textContent,
                      btn: btn ? btn.textContent : "",
                      name: btn ? btn.dataset.name : ""};
            })()""" % json.dumps(fixture))
            note("archived_live_card", live)
            check("an_archived_product_is_not_drawn_as_a_broken_image",
                  live["hasImg"], False)
            check("the_card_says_it_is_archived", "这张已经归档" in live["text"], True)
            check("and_offers_a_restore_for_that_exact_file",
                  [live["btn"], live["name"]], ["还原这张", fixture])
            shot("25b_archived_live_card.png")
            # 那颗按钮打的是**同一个端点**，名字从 `data-name` 取（不许去爬旁边那几段文字）
            page.eval(js_click("btn-unarchive"))
            page.wait_for("window.__back.length > 1", 10, "大卡片那颗还原也发出去了")
            time.sleep(1.0)
            one = page.eval("""(() => {
              const body = window.__back[1];
              return {names: body.items.map((i) => i.name)};
            })()""")
            check("the_card_restores_exactly_that_one", one["names"], [fixture])

            # 反面对照：切回正常档，三样东西都得**换回去**，而且归档的图不许出现。
            # （「不许出现」这条天然成立 —— 夹具只存在于归档目录。留着它是因为它正是
            #   用户要的那件事的直白陈述：归档之后，正常档里就没有它了。）
            page.eval("setResultTab('gallery', true)")
            page.eval(js_click("btn-archived"))
            time.sleep(1.4)
            normal_stage = page.eval("""(() => {
              const back = document.getElementById("btn-restore");
              return {archived: state.galleryArchived,
                      del: document.getElementById("btn-delete").textContent,
                      backHidden: back.hidden,
                      pressed: document.getElementById("btn-archived").getAttribute("aria-pressed"),
                      names: [...document.querySelectorAll("#gallery .tile")].map(
                        (t) => t.dataset.id.split(":").slice(1).join(":"))};
            })()""")
            note("back_to_the_normal_stage", normal_stage)
            check("switching_back_restores_the_archive_button",
                  [normal_stage["archived"], normal_stage["del"],
                   normal_stage["backHidden"], normal_stage["pressed"]],
                  [False, "归档选中的 0 张", True, "false"])
            check("the_archived_image_is_not_in_the_normal_stage",
                  fixture in normal_stage["names"], False)

            # 收拾：替身还回去、注入的作业撤掉、夹具删掉
            page.eval("""(() => {
              window.fetch = (window.__fetch_stack || [])[0] || window.fetch;
              window.__fetch_stack = [];
              delete window.__back;
              document.getElementById("gallery-msg").textContent = "";
              state.jobs.delete("shot-arch");
              state.jobs.delete("row-archived");
              state.jobs.delete("row-normal");
              state.timings.delete("shot-arch");
              state.currentId = null;
              renderLive(); renderJobs();
              return true;
            })()""")
            if os.path.exists(arch_path):
                os.remove(arch_path)
            note("archived_fixture_cleaned", not os.path.exists(arch_path))
            check("the_archived_fixture_is_cleaned_up", os.path.exists(arch_path), False)
        else:
            note("no_real_image_for_the_archived_stage",
                 "画廊是空的，这一档没有可复制的夹具 —— 第 25 步整段没跑")

        # ---- 26. 后端开关（P20i）------------------------------------------------
        # 这颗开关打的是**真端点**：`/api/backend/start` 会真拉起后端（22 GB 权重、
        # 几十秒、占 GPU），`/api/backend/stop` 会真的在 8188 上杀进程 —— 自动化
        # 测试一件都不许干。所以两条路由都打桩，这一步只验「点一下发的是哪一条」
        # 与「六种形态各自长什么样」；**端点到真进程那一跳**由
        # `logs/backend_toggle_e2e.py` 在备用端口上真跑（那边验的是「监听的 pid
        # 就是我们起的那个」）。
        print("\n[26] 后端开关：没有文字的一颗滑动开关，点一下起、再点一下停")
        # 一块桩，管三件事：
        #   ① 两条**动作**路由（起 / 停）记下请求并回 2xx —— 它们是开关真的要做的事；
        #   ② `/api/backend` 与 `/api/backend/launch` 回**我们注入的那一份**；
        #   ③ 一个「摆一档」的入口（`__switchSet`）与一个「读一档」的入口
        #      （`__switchRead`），下面每一档都走它们。
        page.eval("""(() => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          const resp = (o) => Promise.resolve({ok: true, status: 200,
                                               json: async () => o});
          window.__fetch_stack = window.__fetch_stack || [];
          if (!window.__fetch_stack.length) window.__fetch_stack.push(window.fetch);
          const real = window.__fetch_stack[0];
          window.__be = [];
          window.__injected = null;
          window.__stub = (path, opts) => {
            const p = String(path);
            if (p === "/api/backend/start" || p === "/api/backend/stop") {
              window.__be.push(p);
              return Promise.resolve({ok: true, status: 202,
                json: async () => ({phase: p.indexOf("start") >= 0
                                            ? "starting" : "stopped"})});
            }
            if (window.__injected && p.indexOf("/api/backend/launch") === 0) {
              return resp(window.__injected.launch);
            }
            if (window.__injected && p.indexOf("/api/backend") === 0) {
              return resp(window.__injected);
            }
            return real(path, opts);
          };
          window.fetch = window.__stub;
          window.__switchBase = {
            running: false, models_ok: false, missing_models: [],
            hint: "后端未运行。按铁律 1 的命令拉起它。",
            launch: {phase: "idle", process_alive: false, layout: {mode: "normal"}},
            start_command: {display: "cd <安装目录>\\npython main.py"},
          };
          window.__switchSet = async (patch) => {
            const payload = Object.assign({}, window.__switchBase, patch,
              {launch: Object.assign({}, window.__switchBase.launch,
                                     patch.launch || {})});
            window.__injected = payload;
            // **压到它站住为止**（最多 4 次）。只设一次是不够的：`fetchBackend()`
            // 是异步的，页面在它 `await` 的时候可能已经发过一次（SSE 的 `backend`
            // 事件、或那条 15 秒定时器），结果会在我们设完之后才落地，把
            // `state.backend` 换成真那份 —— 而那次请求发生在桩装好**之前**，
            // 于是现象是「一次 fetch 都没有，状态自己变了」（实测：degraded 那一档
            // 第一次就是这么丢的，读出来是 off）。
            let stuck = false;
            for (let i = 0; i < 4 && !stuck; i++) {
              state.backend = payload;
              renderBackendSwitch();
              await sleep(340);   // 过渡动画（180ms）走完 + 让在飞行中的那次落地
              stuck = state.backend === payload;
            }
            return stuck;
          };
          window.__switchRead = () => {
            const el = document.getElementById("be-switch");
            const knob = el.querySelector(".pow-knob");
            const track = el.getBoundingClientRect();
            const ball = knob.getBoundingClientRect();
            return {state: el.dataset.state, checked: el.getAttribute("aria-checked"),
                    disabled: el.disabled, text: el.textContent.trim(),
                    label: el.getAttribute("aria-label"), title: el.title,
                    knob: getComputedStyle(knob).backgroundColor,
                    knobTransform: getComputedStyle(knob).transform,
                    // 滑块的位置**用矩形量、比的是两边的心**：`starting` 那档正在跑
                    // 脉动（`transform: scale()`），读 transform 得到的是缩放中的矩阵，
                    // 跟「在左还是在右」根本不是一回事（第一次就是这么判错的）。
                    // 也不能拿滑块的**左边缘**跟轨道中线比：on 那档 `translateX(22px)`
                    // 之后左边缘正好压在中线左边一点（实测 25 对 26），于是「在右半边」
                    // 这条判据会**假红** —— 量心才对（14 对 26、36 对 26，两边都干净）。
                    knobCenter: Math.round(ball.left + ball.width / 2 - track.left),
                    trackWidth: Math.round(track.width),
                    border: getComputedStyle(el).borderStyle,
                    box: getComputedStyle(el).backgroundColor};
          };
          return true;
        })()""")

        # 六种形态逐一抹出来（真状态取决于这台机器上后端起没起，不能拿它当输入）。
        # 每一档都读**计算样式**：颜色与虚线是这一颗开关唯一的信息载体。
        #
        # **每换一档等 320ms**（> 那条 180ms 的 transition）：底色与滑块位置都是
        # 过渡动画，刚设完就读，读到的是**动画起点** —— 而且 `getComputedStyle`
        # 会把正在插值的颜色序列化成 `oklab(...)`，五档读出来同一串数，看起来
        # 却像「各档颜色都不一样」。这一条是**实测**踩出来的，不是预防性写的。
        states = page.eval("""(async () => {
          const set = (patch) => window.__switchSet(patch).then(() => {
            const r = window.__switchRead();
            r.stuck = state.backend === window.__injected;
            return r;
          });
          return {
            off: await set({}),
            on: await set({running: true, models_ok: true,
                     launch: {phase: "ready", process_alive: true}}),
            external: await set({running: true, models_ok: true,
                     launch: {phase: "idle", process_alive: false}}),
            degraded: await set({running: true, missing_models: ["vae"],
                     launch: {phase: "ready", process_alive: true}}),
            error: await set({launch: {phase: "exited",
                     error: "端口 8188 上已经有东西在响应了，不抢它的端口。"}}),
            starting: await set({launch: {phase: "starting", elapsed_ms: 4200,
                     ready_timeout_s: 180, process_alive: true}}),
            confined: await set({launch: {layout: {mode: "confined",
                     note: "临时/用户/输入目录都在工作区里"}}}),
          };
        })()""")
        note("backend_switch_states", {k: {kk: vv for kk, vv in v.items()
                                          if kk in ("state", "checked", "text",
                                                    "label", "border", "box",
                                                    "knobCenter", "trackWidth",
                                                    "stuck")}
                                      for k, v in states.items()})
        # ⓪ 先确认**六档都真的摆上去了**：注入进去的状态会被在飞行中的一次
        # `fetchBackend()` 换掉（见 `__switchSet` 那段注释）。这一条不过的话，
        # 下面所有读数量的都是别的档位，而它们仍然可能「看着都挺对」。
        check("every_state_actually_stuck", [s["stuck"] for s in states.values()],
              [True for _ in states])
        # ① 用户要的形状：**开关上没有文字**（六档都不许有）
        check("the_switch_never_shows_text",
              [s["text"] for s in states.values()], ["" for _ in states])
        # ② 六档各是一副样子，而且名字（读屏用）跟着档位变
        check("each_state_has_its_own_look",
              [states[k]["state"] for k in ("off", "on", "external", "degraded",
                                            "error", "starting")],
              ["off", "on", "external", "degraded", "error", "starting"])
        check("aria_checked_follows_the_state",
              [states["off"]["checked"], states["on"]["checked"],
               states["error"]["checked"], states["external"]["checked"]],
              ["false", "true", "false", "true"])
        # 可访问名字（读屏用）要**把档位分得开**：七个状态里 `confined` 与 `off`
        # 本来就是同一个档（受限模式只多一句提示），所以判据是「**不同的档位**有各自
        # 的名字」，不是「七种名字」（第一版写成 7，红了一条 —— 而那是我判据写错）。
        check("the_accessible_name_tells_the_states_apart",
              len({s["label"] for s in states.values()})
              == len({s["state"] for s in states.values()}), True)
        # ③ 「在跑但不是我们起的」必须**看得出来是另一件事**：颜色/位置与「在跑」
        # 一样（后端确实是好的），只有边框是虚线（这一颗关不掉它）
        check("an_external_backend_still_looks_on",
              [states["external"]["box"], states["on"]["box"]],
              [states["on"]["box"], states["on"]["box"]])
        check("but_you_can_tell_it_is_not_ours_to_switch_off",
              [states["external"]["border"], states["on"]["border"]],
              ["dashed", "solid"])
        # ④ 红了就是红了（**与三档「好」的颜色都不同** —— 不写死 RGB 值，
        # 那种断言会在换配色时假红，而它想守的是「一眼能看出不一样」），
        # 而且那句原因要**在悬停提示里**（开关上不许写字）
        check("a_failed_launch_looks_like_nothing_else",
              [states["error"]["knob"] == states["off"]["knob"],
               states["error"]["knob"] == states["on"]["knob"],
               states["error"]["knob"] == states["degraded"]["knob"]],
              [False, False, False])
        check("and_it_stays_switched_off_when_it_failed_to_come_up",
              [states["error"]["state"], states["error"]["checked"]],
              ["error", "false"])
        check("the_error_sits_in_the_hover_hint",
              "不抢它的端口" in states["error"]["title"], True)
        check("the_off_and_on_hints_say_what_a_click_will_do",
              ["点一下 = 停掉它" in states["on"]["title"],
               "点一下 = 按铁律 1" in states["off"]["title"]], [True, True])
        # ⑤ 受限模式原来只写在顶栏那行小字上（那行已经没有了）——
        # 现在它在悬停提示里，且日志台的「环境体检」里另有一行
        check("the_confined_marker_is_still_said_somewhere",
              "受限模式" in states["confined"]["title"], True)
        # ⑥ 启动中：滑块**停在左半边**（还没起来），只是脉动。
        # 位置比的是滑块的心与轨道的心（见 `__switchRead` 里那段注释：量边缘会假红）。
        check("starting_does_not_pretend_it_is_up_yet",
              [states["starting"]["checked"],
               states["off"]["knobCenter"] < states["off"]["trackWidth"] / 2,
               states["starting"]["knobCenter"] < states["starting"]["trackWidth"] / 2,
               states["on"]["knobCenter"] > states["on"]["trackWidth"] / 2],
              ["false", True, True, True])
        check("and_while_starting_it_cannot_be_clicked",
              states["starting"]["disabled"], True)

        # 两张图**各摆各照**：摆完六档再连照六张的话，六张全是同一个时刻的屏幕
        # （第一版就是这么写的 —— 六张图的 sha256 一模一样，而读数是对的）。
        page.eval("window.__switchSet(%s)" % json.dumps(
            {"running": True, "models_ok": True,
             "launch": {"phase": "ready", "process_alive": True}}))
        shot("26a_backend_switch_on.png")
        page.eval("window.__switchSet(%s)" % json.dumps(
            {"launch": {"phase": "exited",
                        "error": "端口 8188 上已经有东西在响应了，不抢它的端口。"}}))
        shot("26b_backend_switch_error.png")

        # ⑦ 点一下到底是起还是停 —— **逐条端点**量，不看界面上的颜色
        # （颜色是我自己注入的，拿它当输入等于套套逻辑）。
        click_endpoints = page.eval("""(async () => {
          const click = async (patch) => {
            await window.__switchSet(patch);
            const el = document.getElementById("be-switch");
            const before = window.__be.length;
            el.click();
            await new Promise((r) => setTimeout(r, 250));
            return window.__be.slice(before);
          };
          const out = {};
          out.off = await click({});
          out.on = await click({running: true, models_ok: true,
                                launch: {phase: "ready", process_alive: true}});
          out.external = await click({running: true, models_ok: true,
                                launch: {phase: "idle", process_alive: false}});
          out.starting = await click({launch: {phase: "starting", elapsed_ms: 100,
                                ready_timeout_s: 180, process_alive: true}});
          return out;
        })()""")
        note("backend_switch_clicks", click_endpoints)
        check("clicking_it_off_starts_the_backend",
              click_endpoints["off"], ["/api/backend/start"])
        check("clicking_it_on_stops_the_backend",
              click_endpoints["on"], ["/api/backend/stop"])
        # 外部后端也发**停**那条：拒绝由后端说（并把原因写进日志台），
        # 页面不许自己拦下来 —— 自己拦的话，那句话就没有落点。
        check("an_external_backend_asks_the_backend_to_stop_too",
              click_endpoints["external"], ["/api/backend/stop"])
        check("nothing_is_sent_while_it_is_starting",
              click_endpoints["starting"], [])

        # 收拾：真状态读回来，替身还回去
        page.eval("""(async () => {
          window.fetch = (window.__fetch_stack || [])[0] || window.fetch;
          window.__fetch_stack = [];
          window.__injected = null;
          delete window.__be;
          delete window.__switchSet;
          delete window.__switchRead;
          await fetchBackend();
          return true;
        })()""")
        time.sleep(0.4)
        restored = page.eval("""(() => {
          const el = document.getElementById("be-switch");
          return {state: el.dataset.state, text: el.textContent.trim()};
        })()""")
        note("backend_switch_restored", restored)
        check("the_switch_goes_back_to_the_real_state",
              [restored["text"], restored["state"] == "checking"], ["", False])

        # ------------------------------------------------------------------
        # [27] 最近任务的「清理」（P20j）
        # ------------------------------------------------------------------
        print("\n[27] 最近任务：产物取不到的记录不画，那颗「清理」只删记录")
        # 先切到「最近任务」那一档：那颗按钮在 `#view-jobs` 里，而它是 `hidden` 的
        # （`click_selector` 等的是「有尺寸、可点」，藏着的元素永远等不到）。
        page.eval(js_click("tab-jobs"))
        time.sleep(0.4)
        # `/api/jobs` 与 `/api/jobs/clean` 都打桩：真点那颗按钮会**真的删掉**现场
        # 账里那些记录（本机现在就有二十条是「产物已经找不回来」的），自动化测试
        # 不许动它们 —— 与第 23 / 25 步对 `output/` 的处理同一个理由。
        #
        # 本地那张表**先清空**再喂两份夹具：前面二十几步已经把它填满了真实记录，
        # 直接 upsert 会得到「真实记录 + 我的两条」，于是判据（只画哪一行）分不清
        # 是我修对了还是本来那样。清空是安全的 —— `/api/jobs` 会把他们全都还回来
        # （最后一步的 `fetchJobs()` 就是干这个）。
        page.eval("""(() => {
          state.jobs.clear();
          state.currentId = null;
          const resp = (o) => Promise.resolve({ok: true, status: 200,
                                               json: async () => o});
          const base = {status: "done", mode: "t2i", params: {steps: 20},
                        subfolder: "", type: "output", archived: false};
          const live = Object.assign({}, base, {
            job_id: "live0001", created_at: 2000, prompt_text: "产物还在磁盘上的那条",
            outputs: [{filename: "live.png", subfolder: "", type: "output"}],
            gone: false, on_disk: true, restorable: false});
          const gone = Object.assign({}, base, {
            job_id: "gone0001", created_at: 1000, prompt_text: "产物已经找不回来的那条",
            outputs: [{filename: "gone.png", subfolder: "", type: "output"}],
            gone: true, on_disk: false, restorable: false});
          window.__jobs_stack = window.__jobs_stack || [];
          if (!window.__jobs_stack.length) window.__jobs_stack.push(window.fetch);
          window.__jobs_data = {jobs: [live, gone], stale: 2, total: 2, active: []};
          window.__jobs_calls = [];
          window.fetch = (path, opts) => {
            const p = String(path);
            if (p.indexOf("/api/jobs/clean") === 0) {
              window.__jobs_calls.push({path: p, method: (opts || {}).method,
                                        body: (opts || {}).body});
              // 清完之后后端就不会再回那两条了 —— 替身也得跟着变，否则
              // 「按回执对账」这条判据会被自己的替身骗过去（它下一句就 fetchJobs）。
              window.__jobs_data = {jobs: [live], stale: 0, total: 1, active: []};
              return resp({removed: ["gone0001"], count: 1});
            }
            if (p.indexOf("/api/jobs") === 0) {
              window.__jobs_calls.push({path: p});
              return resp(window.__jobs_data);
            }
            return window.__jobs_stack[0](path, opts);
          };
          return true;
        })()""")
        page.eval("fetchJobs()")
        time.sleep(0.4)
        panel = page.eval("""(() => {
          const btn = document.getElementById("btn-clean-jobs");
          return {rows: [...document.querySelectorAll("#jobs .job-row")].map((e) => e.dataset.job),
                  text: btn.textContent, disabled: btn.disabled, title: btn.title,
                  note: document.getElementById("clean-note").textContent,
                  count: document.getElementById("jobs-count").textContent};
        })()""")
        note("jobs_clean_panel", panel)
        check("only_the_row_whose_product_is_on_disk_is_drawn", panel["rows"], ["live0001"])
        check("the_count_under_the_title_counts_what_is_drawn",
              [panel["count"], panel["text"]], ["（1）", "清理 2 条"])
        check("the_clean_button_is_live", panel["disabled"], False)
        check("the_button_says_the_product_files_are_not_touched",
              "产物文件一张不动" in panel["title"], True)
        shot("27a_jobs_clean_button.png")

        page.click_selector("#btn-clean-jobs")
        time.sleep(0.7)
        after = page.eval("""(() => {
          const btn = document.getElementById("btn-clean-jobs");
          const posts = (window.__jobs_calls || []).filter((c) => c.method === "POST");
          return {rows: [...document.querySelectorAll("#jobs .job-row")].map((e) => e.dataset.job),
                  text: btn.textContent, disabled: btn.disabled, title: btn.title,
                  note: document.getElementById("clean-note").textContent,
                  stillInTable: state.jobs.has("gone0001"),
                  posts: posts};
        })()""")
        note("jobs_clean_after", after)
        check("clicking_it_posts_to_the_clean_endpoint",
              [p["path"].split("?")[0] for p in after["posts"]], ["/api/jobs/clean"])
        check("the_body_is_an_empty_object", after["posts"][0]["body"], "{}")
        check("the_receipt_is_on_the_page",
              ["清掉 1 条" in after["note"], "产物文件一张没动" in after["note"]],
              [True, True])
        check("the_removed_row_leaves_the_local_table", after["stillInTable"], False)
        check("the_removed_row_is_not_drawn_anymore", after["rows"], ["live0001"])
        check("nothing_left_to_clean_so_the_button_goes_back_to_disabled",
              [after["text"], after["disabled"]], ["清理", True])
        shot("27b_jobs_after_clean.png")

        # 全都取不到图的时候：列表空着，但那个数字与那句话还在（它们最该在这时说得清）
        page.eval("""(() => {
          // 同样先清空：`upsert` 只加不删，留着刚才那条 `live0001` 的话，
          // 这一屏就不是「全都取不到图」——判据会以为自己验的是这个场景，其实不是。
          state.jobs.clear();
          state.currentId = null;
          window.__jobs_data = {jobs: [{job_id: "g2", status: "done", mode: "t2i",
            params: {}, prompt_text: "全都没了", created_at: 1,
            outputs: [{filename: "x.png", subfolder: "", type: "output"}],
            gone: true, on_disk: false, archived: false, restorable: false}],
            stale: 1, total: 1, active: []};
          return true;
        })()""")
        page.eval("fetchJobs()")
        time.sleep(0.4)
        empty = page.eval("""(() => {
          const btn = document.getElementById("btn-clean-jobs");
          return {rows: document.querySelectorAll("#jobs .job-row").length,
                  empty: document.getElementById("jobs").textContent.trim(),
                  text: btn.textContent, disabled: btn.disabled};
        })()""")
        note("jobs_all_gone", empty)
        check("an_empty_list_still_counts_what_can_be_cleaned",
              [empty["rows"], empty["text"], empty["disabled"]], [0, "清理 1 条", False])
        check("the_empty_state_says_why",
              "产物都已经不在磁盘上了" in empty["empty"], True)

        # 收拾：真 fetch 还回去，替身那几条也从本地那张表里摘掉（`gone0001` 早被
        # 回执删掉了），再把**真实**的任务列表读回来
        page.eval("""(async () => {
          window.fetch = (window.__jobs_stack || [])[0] || window.fetch;
          window.__jobs_stack = [];
          delete window.__jobs_data;
          delete window.__jobs_calls;
          state.jobs.delete("live0001");
          state.jobs.delete("g2");
          await fetchJobs();
          return true;
        })()""")
        time.sleep(0.5)
        restored_jobs = page.eval("""(() => ({
          rows: document.querySelectorAll("#jobs .job-row").length,
          note: document.getElementById("clean-note").textContent
        }))()""")
        note("jobs_restored", restored_jobs)

        # ------------------------------------------------------------------
        # [28] 格子画的是缩略图，放大视图才是原图（P20l）
        # ------------------------------------------------------------------
        # 为什么这一条非要在真浏览器里量：格子上那张 `<img>` 的自然尺寸是**浏览器
        # 解码之后**的像素，服务端发错字节（比如把原图当缩略图发）在源码里看不出
        # 任何异常，只有量出来才知道。量 `naturalWidth` 与**同一张图在后端记录里的
        # 宽**比 —— 判据不写死 512（那是服务端的常量，写死就是它的第二份副本）。
        print("\n[28] 画廊格子画缩略图（原图留给放大视图与拖动）")
        page.eval(js_click("tab-gallery"))
        page.eval("""(() => {
          closeLightbox();
          state.galleryMode = ""; state.galleryPage = 0; state.galleryQuery = "";
          const box = document.getElementById("gallery-search");
          if (box) box.value = "";
          return true;
        })()""")
        tiles_seen = 0
        for _ in range(30):
            tiles_seen = page.eval('document.querySelectorAll("#gallery .tile").length')
            if tiles_seen:
                break
            time.sleep(0.4)
        if not tiles_seen:
            # 一盘空画廊（output/ 被清空过）验不了这一条 —— 说清是跳过，不是通过。
            note("gallery_thumbs", {"skipped": "画廊里没有格子"})
        else:
            # 等这几张**真的画出来**：只等 `<img>` 进 DOM 会把「还没加载」读成 0，
            # 而 `0 < 原图宽` 恒真 —— 一条看起来像成功的假判据。
            thumbs = page.wait_for("""(() => {
              const imgs = [...document.querySelectorAll("#gallery .tile img")];
              if (!imgs.length) return null;
              if (!imgs.every((i) => i.complete && i.naturalWidth > 0)) return null;
              return imgs.map((i) => ({
                id: i.closest(".tile").dataset.id, src: i.getAttribute("src"),
                w: i.naturalWidth, h: i.naturalHeight}));
            })()""")
            real = {it["id"]: it for it in
                    (http_json(args.url + "/api/gallery?limit=200").get("items") or [])}
            pairs = [{"id": t["id"], "tile": t["w"], "original": real.get(t["id"], {}).get("width"),
                      "file": real.get(t["id"], {}).get("filename")}
                     for t in thumbs]
            note("tile_thumbnail_sizes", pairs[:3])
            check("every_tile_asks_for_the_thumbnail",
                  all("/api/gallery/thumb?" in t["src"] for t in thumbs), True)
            # 原图比缩略图大的那些才比得出来（原图本来就小的图，两者相等是对的）
            big = [p for p in pairs if p["original"] and p["original"] >= 1024]
            check("no_tile_draws_the_original_pixels",
                  [len(big) > 0, all(p["tile"] < p["original"] for p in big)], [True, True])
            shot("28a_gallery_thumbnails.png")

            # 放大视图：**必须是原图**（1:1 原始像素那条路靠它）
            first = max(pairs, key=lambda p: p["original"] or 0)
            page.eval("openLightbox(" + json.dumps(first["id"]) + ")")
            lb = page.wait_for("""(() => {
              const img = document.getElementById("lb-img");
              if (!img.complete || !img.naturalWidth) return null;
              return {src: img.getAttribute("src"), w: img.naturalWidth};
            })()""")
            note("lightbox_source", lb)
            check("the_lightbox_asks_for_the_original",
                  "/api/gallery/image?" in lb["src"], True)
            check("the_lightbox_has_the_original_pixels",
                  [lb["w"] == first["original"], (first["file"] or "") in lb["src"]],
                  [True, True])
            shot("28b_lightbox_original.png")
            page.eval("closeLightbox()")

            # 拖进来那条路：格子上那张是缩略图，而**拖走的必须是原图**
            # （拿 512px 的 JPEG 当参考图是没有症状的降级：传得上去、能出图，
            # 只是少了一半像素）。这里真调一次页面里那个函数。
            ref = page.eval("""(() => {
              const r = refFromUri("/api/gallery/thumb?root=0&name=a.png");
              return r ? {name: r.name, url: r.url} : null;
            })()""")
            note("drop_from_a_thumbnail", ref)
            check("a_dragged_thumbnail_is_rewritten_to_the_original",
                  [bool(ref), ref and ref["name"],
                   bool(ref and "/api/gallery/image?" in ref["url"])],
                  [True, "a.png", True])

    finally:
        page.close()

    path = os.path.join(args.out, "page_shot_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\nreport: %s" % path)
    print("shots : %d" % len(summary["shots"]))
    print("\n**看图这一步不能省** —— 脚本只量得出元素在不在、位置对不对。")
    if failures:
        print("\n**判据未过 %d 条**：" % len(failures))
        for f in failures:
            print("  - %s：得到 %s，期望 %s"
                  % (f["key"], json.dumps(f["got"], ensure_ascii=False),
                     json.dumps(f["want"], ensure_ascii=False)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
