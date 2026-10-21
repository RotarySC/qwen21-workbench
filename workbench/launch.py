"""一键拉起 ComfyUI 后端（P6）—— 跑的就是 AGENTS.md 铁律 1 那条命令。

**为什么不是 `launch_comfyui`**：comfy-cli 的 `--launch-extras` 按空格切分
（`command/launch.py:438`），而 `--extra-model-paths-config` 那个 yaml 路径
含空格（`Comfy Desktop`），必然断。所以这里自己 `Popen`，一个参数一个元素，
不经任何 shell 拆分。

三条安全规则，都是**有意**的：

1. **端口上已经有响应就拒绝启动。** 不抢别人的实例 —— 那个可能是用户自己开的、
   也可能是 Desktop 开的，抢过来只会得到一个端口冲突和一个莫名其妙的退出。
2. **日志写文件，不走管道。** 管道有个 64 KB 的缓冲；没人读它的那一刻，
   子进程就**阻塞在 write 上**，看起来像卡死（冒烟脚本踩过这个坑，见 AGENTS.md）。
   写文件既没有这个上限，也让「回显历史日志」变成一次 `read` 就能做的事。
3. **不自动重启、不杀不是自己起的进程。** 起来之后就不主动管它 ——
   工作台重启后认不出那个实例也没关系，就绪判断只看 `/system_stats`。
   唯一会真的去停的场合是**用户点了那颗开关的「关」**（`stop()`），而且
   仍然只停本进程起的那个：句柄只在本进程里有意义，认不出的一律拒绝
   （「工作台重启过」= 认不出，这时只能请用户自己关）。

进程句柄只在**本进程**里有意义：工作台重启后 `Popen` 没了，`status()` 会如实说
「这次不是我启动的」，而不是假装知道。

**受限模式**：这台机器上工作台自己只能写工作区（实测，见 `config.BACKEND_CACHE`
那段注释），子进程继承这个限制。所以起之前先**探**两处 —— 有效的临时目录、以及
ComfyUI 的 `user/`；探不进去就把落点搬进工作区（临时 / 用户 / 输入目录），并把
这件事**报给页面**：换了落点却不说的后果是「为什么 Manager 装不了节点」这种
没有症状的问题。探测只认一个判据 —— 真的建一个文件再删掉，因为权限位看不出这类拒绝。
"""
import logging
import os
import subprocess
import sys
import threading
import time

from . import config, util

# 就绪轮询上限。spec §6.6 定的是 180 秒：冷启动要读 16 GiB 权重的元数据、
# 加载 custom nodes，实测十几秒；180 秒是给「机器同时在干别的」留的余量。
READY_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 1.0

# 成功标志。ComfyUI 启动完会打印这一行；不过**就绪与否以 `/system_stats` 为准**
# （那才是工作台真的要用到的东西），这一行只用来在日志里给出更早的提示。
READY_MARKER = "To see the GUI go to"

# 日志尾部最多回显多少字符 —— 够看出「yaml 路径错了」或「端口被占」。
LOG_TAIL_BYTES = 24000

# 停后端时等它退出的上限（P20i）。`terminate()` 在 Windows 上是
# `TerminateProcess`（不可挡），所以正常情况是毫秒级；给 10 秒是留给
# 「它在做不可中断的 IO」那种极端情况 —— 到点就如实说没停掉，不假装成功。
STOP_TIMEOUT_S = 10.0

# 这一族的日志会进日志台的环（`logbuf`）。**停后端的三条结局都要留痕**，
# 尤其是「被拒绝」：拒绝只写在页面那颗开关的悬停提示里的话，用户一刷新就没了，
# 而「点了没反应」是本工作区最忌讳的那种错。
LOGGER = logging.getLogger("workbench.launch")


def _descendants(pid):
    """那个后端**下面还挂着谁**。

    实测（2026-10-02，`logs/launch_pid_tree.py`）：`Popen` 给的 pid 是一个
    **启动壳** —— `argv[0]` 是 venv 里的 `python.exe`，而真正跑 ComfyUI、
    真正监听端口的是它派生的**孩子**：

        pid 18644  python.exe        ← 我们 Popen 的那个（壳）
          pid 14564  python.exe      ← 真正在跑的那个，监听端口的也是它
            pid 16428  conhost.exe

    同一次实测里，`TerminateProcess` 掉那个壳之后孩子**跟着一起没了**（端口 1 秒
    内空掉、孩子的 `is_running()` 变 False）。但那是一条**实测到的行为**、
    不是谁保证的契约 —— 所以停的时候还是先按孩子走一遍。

    psutil 不在也能活（工作台不硬依赖它，见 requirements.txt）：那就退回
    「只停那个壳」，而实测那一支够用。
    """
    try:
        import psutil
    except ImportError:
        return []
    try:
        return psutil.Process(pid).children(recursive=True)
    except Exception:                                   # noqa: BLE001
        return []


def _still_running(proc):
    """那个 psutil.Process **现在还活着吗**。查不了就当它已经没了。"""
    try:
        return proc.is_running()
    except Exception:                                   # noqa: BLE001
        return False


def _wait_gone(kids, proc, timeout):
    """等那棵树真的消失，返回**还活着**的 pid（等到超时也算）。

    为什么要等、而不是查一眼：`TerminateProcess` 是**异步**的 —— 刚发完信号，
    `is_running()` 仍然会报 True。实测（2026-10-02，第一次跑
    `logs/backend_toggle_e2e.py`）：停完立刻查，两个孩子都「还在」，两秒后
    一个都不剩。查一眼就判的话，**每一次正常的停止**都会报「没停干净」，
    而一条总在喊狼来了的判据，等于没有判据。
    """
    deadline = time.time() + timeout
    while True:
        left = [k.pid for k in kids if _still_running(k)]
        # 壳那份用 `poll()` 判（`proc` 是 Popen，不是 psutil 对象 —— 两个 API
        # 别混着用：`psutil` 那套只用在孩子上）
        if not left and proc.poll() is not None:
            return []
        if time.time() >= deadline:
            return left or [proc.pid]
        time.sleep(0.1)


class LaunchError(ValueError):
    """起不来，或不该起。继承 ValueError —— server 统一变成 400。"""


# --- 受限启动：探测 + 落点 --------------------------------------------------
# 探测用的文件名。**建完马上删**，不进 git 也不留垃圾。
PROBE_NAME = ".workbench-write-probe"

# 受限时要递的开关：落点 → ComfyUI 的 CLI 参数
CONFINED_FLAGS = (
    ("comfy_temp", "--temp-directory"),
    ("user", "--user-directory"),
    ("input", "--input-directory"),
)


def write_probe(directory):
    """这个进程能不能往 `directory` 里写？返回 `(ok, 原因)`。

    判据与 `tempfile._get_default_tempdir()` 同源：**真的建一个文件再删掉**。
    权限位在这里没有用 —— 实测同一台机器、同一个账号，只有工作区里那个解释器
    写不了 %TEMP%（Low 完整性），而它的 DACL 上明明挂着 `<用户>:(F)`。
    """
    if not directory:
        return False, "没有这个目录"
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return False, "建不了目录（%s）" % (exc.strerror or exc)
    probe = os.path.join(directory, PROBE_NAME)
    try:
        with open(probe, "wb") as f:
            f.write(b"x")
    except OSError as exc:
        return False, exc.strerror or str(exc)
    try:
        os.remove(probe)
    except OSError:
        pass            # 删不掉不影响结论：写成功了才是要问的事
    return True, None


# 子进程找临时目录的顺序（`tempfile._candidate_tempdirs()` 的前三个）
TEMP_ENV_ORDER = ("TMPDIR", "TEMP", "TMP")


def env_temp_dir():
    """子进程会**先**认的那个临时目录。

    特意不用 `tempfile.gettempdir()`：它的候选表最后一项是 `os.getcwd()`，于是
    「工作台自己」拿到的是仓库目录（可写）、而「子进程」的 cwd 是 ComfyUI 安装目录
    （不可写）—— 同一句探测，结论与子进程真实遇到的**正好相反**。实测：受限的那个
    解释器里 `gettempdir()` 返回的是工作区（cwd 兜底），它的子进程照样死于
    `No usable temporary directory`。
    """
    for name in TEMP_ENV_ORDER:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def choose_layout(temp_dir=None, comfy_root=None, probe=None):
    """这一次启动该怎么摆落点。**只看探测结果，不看猜测。**

    返回的字典给三处用：拼 argv（`flags`）、塞进子进程环境（`env`）、
    以及页面上那句「受限模式」（`note` / `paths` / `probes`）。

    只探两处就够，因为两处各对应一条**实测出来的**要求：

    - **环境里指的那个临时目录** —— torch 导入时要在那儿建编译缓存；写不了就死在
      `import torch`（真实现象：`No usable temporary directory found in [...]`）。
    - **ComfyUI 的 `user/`** —— 设置、数据库、Manager 的配置与缓存、日志都写这儿。
      它写不了，**同一层**的 `input/`（参考图落点）也一定写不了，所以两个一起搬。

    都没问题就返回 `mode="normal"` 且 flags/env 全空 —— 不受限的机器上
    **一个字节的差别都不该有**。
    """
    probe = probe or write_probe
    if temp_dir is None:
        temp_dir = env_temp_dir()
    comfy_root = config.COMFY_ROOT if comfy_root is None else comfy_root
    user_dir = os.path.join(comfy_root, "user")
    install_target = user_dir if os.path.isdir(user_dir) else comfy_root

    temp_ok, temp_why = probe(temp_dir)
    if not temp_ok and not temp_dir:
        temp_why = "TMPDIR / TEMP / TMP 一个都没设"
    install_ok, install_why = probe(install_target)
    probes = [
        {"what": "临时目录", "path": temp_dir or "(未设置)",
         "ok": temp_ok, "reason": temp_why},
        {"what": "ComfyUI 安装目录", "path": install_target,
         "ok": install_ok, "reason": install_why},
    ]
    if temp_ok and install_ok:
        return {"mode": "normal", "probes": probes, "paths": {},
                "flags": [], "env": {}, "note": ""}

    paths = config.confined_layout()
    env, want, moved = {}, set(), []
    if not temp_ok:
        # 三个变量都设：tempfile 按 TMPDIR → TEMP → TMP 的顺序找候选，
        # 少设一个就少一条路（实测那个堆栈里就是三个都指向写不进去的地方）。
        for name in ("TMPDIR", "TEMP", "TMP"):
            env[name] = paths["tmp"]
        env["TORCHINDUCTOR_CACHE_DIR"] = paths["torch"]
        want.add("comfy_temp")
        moved.append(("临时目录", paths["tmp"]))
    if not install_ok:
        want |= {"user", "input"}
        moved += [("用户目录", paths["user"]), ("输入目录", paths["input"])]

    flags = []
    for key, flag in CONFINED_FLAGS:
        if key in want:
            flags += [flag, paths[key]]

    note = ("受限模式：这个工作台只能写工作区，所以%s搬进了工作区 —— "
            "模型能读、出图能出、参考图能传；模型下载与节点安装用不了"
            "（那两个要写 ComfyUI 的安装目录）。"
            % "、".join(name for name, _ in moved))
    return {"mode": "confined", "probes": probes,
            "paths": {name: path for name, path in moved},
            "flags": flags, "env": env, "note": note}


class Launcher:
    """管一个由本进程启动的后端实例。**不做多实例**，也没有池。"""

    def __init__(self, client, log_path=None, probe_report=None,
                 probe_state=None, ready_timeout=None, on_event=None,
                 probe=None):
        self.client = client
        self.log_path = log_path or config.BACKEND_LOG
        self.probe_report = probe_report or config.PROBE_REPORT
        self.probe_state = probe_state or config.PROBE_STATE
        self.ready_timeout = (config.BACKEND_READY_TIMEOUT_S if ready_timeout is None
                              else ready_timeout)
        # 可写性探测器。**传进来的**而不是直接调模块函数 —— 单测要能规定
        # 「这台机器写不了 %TEMP%」，而不是依赖跑测试的那台机器真写不了。
        self._probe = probe
        self._layout = None
        # 事件出口。**传进来的**而不是自己 import broker —— launch 只管进程，
        # 不该知道 SSE 的存在。工作台传的是 `broker.publish_threadsafe`，
        # 因为这里从**普通线程**发事件。
        self.on_event = on_event
        self._lock = threading.RLock()
        self._proc = None
        self._state = None          # 见 _fresh_state()
        self._thread = None
        self._cancel = threading.Event()
        self._log_pos = 0

    def _emit(self, event):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as exc:                    # noqa: BLE001
                # 推事件失败不该影响启动 —— 但**也不能没有痕迹**：页面的进度与
                # 状态全靠这条通道，静默失败的样子是「点了启动，然后什么都没发生」。
                LOGGER.warning("推事件失败（%s）：%s", event.get("type"), exc)

    # --- 内部 -----------------------------------------------------------
    def layout(self):
        """这一次启动的落点方案。**算一次就缓存。**

        它是**这个进程**的属性（能不能写工作区外面，取决于我们是怎么被启动的），
        不随请求变 —— 所以 `/api/backend` 每次轮询都拿得到，而探测只做一次：
        为了回答一个只读的问题，反复往 %TEMP% 和安装目录里建/删文件没道理。
        """
        with self._lock:
            if self._layout is None:
                self._layout = choose_layout(probe=self._probe)
            return self._layout

    def _fresh_state(self, **over):
        st = {
            "launch_id": None,
            "pid": None,
            "port": config.COMFY_PORT,
            "started_at": None,
            "ended_at": None,
            "ready": False,
            "ready_ms": None,
            "phase": "idle",        # idle|starting|ready|failed|exited|stopped
                                    # （stopped = 用户把这颗开关关掉了，与
                                    #  failed/exited 是**两件事**：那两种是没起来）
            "exit_code": None,
            "error": None,
            "error_short": None,
            "command": None,
            "log_path": self.log_path,
            # 落点方案（normal / confined）随状态一起给出去 —— 页面要在**点之前**
            # 就知道这个按钮会以什么方式起，而不是点完才从日志里发现。
            "layout": self.layout(),
        }
        st.update(over)
        return st

    def _marker(self, launch_id, argv):
        """每次启动写一行分隔，日志能分辨「哪一段是哪一次」。

        受限模式那句也写进来 —— 换了落点却不留痕，事后翻日志只会觉得
        「临时目录怎么跑到工作区去了」。
        """
        head = ("\n===== %s 启动 pid=%s 端口=%s =====\n%s\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
                   config.COMFY_PORT, " ".join(argv)))
        note = self.layout().get("note")
        if note:
            head += "# %s\n" % note
        return head.encode("utf-8")

    def command(self, port=None):
        """铁律 1 的命令，逐字来自 `config.start_command()`，只换端口。

        **一个参数一个列表元素** —— 绝不拼成一个字符串再过 shell，
        否则那个含空格的 yaml 路径就会断成两截（这正是不能用 launch_comfyui 的原因）。

        **受限于才追加落点开关**：`config.start_command()` 是给用户复制的那条命令
        （他自己在 PowerShell 里跑，不受这个限制，该用正常的落点），这里是给
        子进程用的那条，两者**故意不同**。
        """
        argv = list(config.start_command()["argv"])
        if port is not None and port != config.COMFY_PORT:
            i = argv.index("--port")
            argv[i + 1] = str(port)
        return argv + list(self.layout()["flags"])

    def child_env(self):
        """给子进程的环境。探针三个变量是 spec §6.6 明确要求的。

        探针打开后**出图就带逐节点耗时**（P3 那块面板的数据），所以「一键启动」
        顺手把探针带上 —— 否则用户点一次按钮，还得自己去设环境变量才能看到耗时，
        而面板会显示「本次无耗时数据」，看起来像探针坏了。
        """
        env = dict(os.environ)
        env["COMFY_NODE_PROBE"] = "1"
        env["COMFY_NODE_PROBE_OUT"] = self.probe_report
        env["COMFY_NODE_PROBE_STATE"] = self.probe_state
        # ★ `config.BACKEND_ENV`（目前只有 `HF_ENDPOINT`）—— See-through 要靠它
        # 把插件里那几处写死的 `huggingface.co` 整体换到镜像站去。**不加就没有症状**：
        # 页面会说「后端在跑」，然后拆层那条链路退避 5 次之后整次跑死，报一个看不懂的错。
        #
        # 与 `config.start_command()["display"]` 那份**同一处真源** —— 只改一边，
        # 「从开关起的后端」与「照抄命令起的后端」就跑在两套环境里，
        # 而页面分不出这两者。
        env.update(config.BACKEND_ENV)
        # 受限模式下才动这三个变量 + torch 的缓存目录（见 `choose_layout`）。
        # 正常机器上这里是空的 —— 不该因为「可能受限」就把所有人的临时目录搬走。
        env.update(self.layout()["env"])
        return env

    def _popen_kwargs(self):
        """Windows：脱离控制台 + 独立进程组。别的平台给个合理的等价物。

        注意 **stdout/stderr 不在这里** —— 由 `start()` 打开日志文件后传进去。
        写文件而不是管道：管道有个 64 KB 缓冲，没人读它的那一刻子进程就
        **阻塞在 write 上**（冒烟脚本踩过这个坑），而文件没有上限。
        """
        kw = {
            "cwd": config.COMFY_ROOT,
            "env": self.child_env(),
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":
            kw["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kw["start_new_session"] = True
        return kw

    def preflight(self):
        """起之前先把**能静态查出来的**问题挡掉，别让用户等 180 秒再看到一句
        「超时」。这两条是最常见的两个原因，而且都能提前知道。"""
        argv = self.command()
        exe = argv[0]
        if not os.path.isfile(exe):
            raise LaunchError("找不到 ComfyUI 的解释器：%s\n"
                              "（`QWEN21_COMFY_ROOT` 或安装位置变了？）" % exe)
        main_py = os.path.join(config.COMFY_ROOT, "main.py")
        if not os.path.isfile(main_py):
            raise LaunchError("找不到 %s —— 安装目录不对？" % main_py)
        yaml = config.find_instance_yaml()
        if not yaml:
            raise LaunchError(
                "找不到模型路径配置（%s）。没有它那四个模型文件在后端里不可见，"
                "起来也是白起 —— 先用 Comfy Desktop 打开一次让它生成。"
                % config.INSTANCE_YAML_GLOB)
        layout = self.layout()
        if layout["mode"] == "confined":
            # 落点得**现在就建出来**：子进程受同样的限制，而它要建的是「能写」的
            # 那些目录 —— 这里建不了就说明连工作区都写不进去，那就别起，
            # 免得又递给用户一段 `import torch` 的堆栈。
            check = self._probe or write_probe
            for name, path in layout["paths"].items():
                ok, why = check(path)
                if not ok:
                    raise LaunchError(
                        "受限模式下%s要落在工作区里，但 %s 写不了（%s）。\n"
                        "这台机器上工作台自己只能写工作区；连工作区都写不了，"
                        "就只能在你**自己的** PowerShell 里起后端：把上面那条命令"
                        "复制过去。（不要双击 `run.bat` —— 那起的是工作台。）"
                        % (name, path, why))
        return {"argv": argv, "cwd": config.COMFY_ROOT, "yaml": yaml,
                "layout": layout}

    def _reject_if_occupied(self):
        """端口上已经有响应就拒绝 —— 不抢别人的实例。"""
        if self.client.probe() is not False:
            raise LaunchError(
                "端口 %d 上已经有东西在响应了，不抢它的端口。\n"
                "如果那就是你要用的后端，直接刷新页面即可；"
                "确认它不是你要的，先自己关掉再来点这个按钮。"
                % config.COMFY_PORT)

    # --- 对外 -----------------------------------------------------------
    def start(self, port=None):
        """起一个新的，然后**在后台线程里**等它就绪。立刻返回。

        不在请求里等就绪：冷启动十几秒到几十秒，让 HTTP 请求挂着不合适
        （spec §6.6 也是这个意思 —— 轮询与日志都经 SSE 回显）。
        """
        with self._lock:
            if self._state and self._state["phase"] == "starting":
                raise LaunchError("已经在启动中了（pid=%s），别重复点。"
                                  % self._state.get("pid"))
            self._reject_if_occupied()
            info = self.preflight()
            argv = self.command(port)

            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)),
                        exist_ok=True)
            kw = self._popen_kwargs()
            self._cancel.clear()
            try:
                # **追加**而不是 "wb" 重写：起一次失败、改点东西再起一次是很常见的，
                # 重写会把上一次那段的失败原因抹掉 —— 而那正是最该留着的东西。
                # 两端都用 O_APPEND，所以我们写的分隔行与子进程的输出不会互相插花。
                logf = open(self.log_path, "ab")
            except OSError as exc:
                raise LaunchError("日志文件写不了（%s）：%s"
                                  % (self.log_path, exc))
            # 只把**这一次**启动之后的内容当「新日志」推给页面。
            # 上一次启动留下的那些仍然能在 /api/backend/log 里看到（那里读整份尾部）。
            self._log_pos = logf.tell()
            try:
                logf.write(self._marker(launch_id=None, argv=argv))
                logf.flush()
            except OSError:
                pass
            try:
                # `stderr=STDOUT` 已经在 _popen_kwargs 里了：让子进程的 stderr
                # 走**同一个底层句柄**，这样两个流共用一个文件位置，不会互相插花。
                # （别把两个流各自 open 一次同一个文件 —— 那是两个文件位置。）
                proc = subprocess.Popen(argv, stdout=logf, bufsize=0, **kw)
            except OSError as exc:
                logf.close()
                raise LaunchError("起不来：%s\n命令：%s" % (exc, " ".join(argv)))
            finally:
                logf.close()

            launch_id = "%d-%d" % (proc.pid, int(time.time() * 1000))
            self._proc = proc
            self._state = self._fresh_state(
                launch_id=launch_id, pid=proc.pid, port=port or config.COMFY_PORT,
                started_at=util.now_ms(), phase="starting",
                command=argv, yaml=info["yaml"], layout=info["layout"],
                requested_port=port)
            self._thread = threading.Thread(
                target=self._wait_ready, args=(launch_id,), daemon=True)
            self._thread.start()
            return dict(self._state)

    def _pump_log(self):
        """把日志文件里**新长出来的**部分推给页面。

        只读新增的那一段（记住偏移），不重复推也不整读 —— 日志会一直长，
        每次整读是 O(文件大小)，而这里每秒都要跑一次。
        """
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return []
        if size <= self._log_pos:
            # 文件被截断/换掉了（比如用户手动清空），从头再来
            if size < self._log_pos:
                self._log_pos = 0
            else:
                return []
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._log_pos)
                blob = f.read(262144)
        except OSError:
            return []
        if not blob:
            return []
        self._log_pos += len(blob)
        # 末尾可能是半行 —— 留着下次再读，别把半行推出去
        text = blob.decode("utf-8", errors="replace")
        lines = [ln.rstrip("\r") for ln in text.splitlines()]
        if lines:
            self._emit({"type": "backend_log", "lines": lines})
        return lines

    def _wait_ready(self, launch_id):
        """轮询到就绪 / 退出 / 超时。**三种结局都要说清**，包括给日志。"""
        t0 = time.time()
        deadline = t0 + self.ready_timeout
        while time.time() < deadline:
            if self._cancel.is_set():
                return
            self._pump_log()
            rc = self._proc.poll() if self._proc else None
            if rc is not None:
                self._pump_log()
                # 进程没了，但**可能是我们自己停的**（`stop()` 会先设这个旗标）。
                # 不在这里再看一眼的话，用户点「关」得到的是一条「后端启动失败」
                # ——两件事的处置完全相反，而画面上只差一个颜色。
                if self._cancel.is_set():
                    return
                short, detail = explain_failure(self.log_tail(), rc, self.layout())
                self._finish(launch_id, phase="exited", exit_code=rc,
                             error=detail, error_short=short)
                self._emit({"type": "backend_failed", "launch_id": launch_id,
                            "exit_code": rc, "error": short})
                return
            if self.client.probe() is True:
                ready_ms = int((time.time() - t0) * 1000)
                self._pump_log()
                self._finish(launch_id, phase="ready", ready_ms=ready_ms)
                self._emit({"type": "backend_ready", "launch_id": launch_id,
                            "pid": self._state.get("pid"),
                            "ready_ms": ready_ms})
                return
            time.sleep(POLL_INTERVAL_S)
        self._pump_log()
        # 与上面那条同一个理由：**超时**这条路上也要先看是不是我们自己停的。
        # 少了这一句，「刚好在超时那一刻点关」会把 stopped 改回 failed ——
        # 页面上那颗开关就从「灰」变成「红」，而用户明明是自己关掉它的。
        if self._cancel.is_set():
            return
        self._finish(launch_id, phase="failed",
                     error="等了 %d 秒还没就绪。进程可能还活着但没起来 —— "
                           "看日志尾部。" % int(self.ready_timeout))
        self._emit({"type": "backend_failed", "launch_id": launch_id,
                    "error": self._state.get("error")})

    def _finish(self, launch_id, **fields):
        with self._lock:
            if not self._state or self._state["launch_id"] != launch_id:
                return
            self._state.update(fields)
            self._state["ended_at"] = util.now_ms()
            # `ready` 是给页面直接用的一眼可见的布尔；`phase` 是细状态。
            # 两个都维护**是有意的**：状态机里少设一个字段，
            # 表现就是「phase 说 ready 了，但 ready 还是 False」——
            # 页面照着 ready 渲染就会显示「没就绪」，而其实已经好了。
            self._state["ready"] = self._state["phase"] == "ready"

    def cancel(self):
        """只用来打断**等待**，不杀进程。

        用户想真把它停掉是另一件事，走 `stop()`。
        """
        self._cancel.set()

    def stop(self):
        """停掉**本进程起的**那个后端（用户点开关的「关」）。别人的实例不碰。

        红线与 `start()` 那条一样，只是方向相反：**只动自己起的那个**。
        进程句柄只在本进程里有意义，所以两种情况下这里会拒绝，而且都要说清
        该怎么办 —— 「点了没反应」等于没有症状的错：

        - 工作台重启过：`self._proc` 没了，那个后端还活着（`DETACHED_PROCESS`，
          不属于任何窗口），我们认不出它；
        - 后端是用户在别处起的（自己的 PowerShell、Comfy Desktop、另一个工作台）。

        **「那个 pid」不是一个进程，是一棵树**：`Popen` 给的 pid 是 venv 的启动壳，
        真正跑 ComfyUI 的是它的孩子（见 `_descendants` 那段实测）。所以这里是
        「先孩子、后壳」，停完还要核一遍有没有漏的 —— `LOGGER.info` 那句把
        「停了几棵树」记下来，事后翻日志能对得上。
        """
        with self._lock:
            proc = self._proc
            st = dict(self._state) if self._state else {}
            alive = proc is not None and proc.poll() is None
            if not alive:
                raise LaunchError(
                    "这个后端不是工作台这次起的（工作台重启过，或它是你在别处起的），"
                    "所以我不杀它 —— 认不出的进程一律不动。\n"
                    "要停它：在你启动它的那个窗口里按 Ctrl+C；或者打开任务管理器，"
                    "结束那个 python.exe（后端会把这一段写进日志台）。")
            pid = proc.pid
            launch_id = st.get("launch_id")
            # 先打断等待线程：进程一没，它会报「启动失败」，而那正是我们想要的
            # 结果（`_wait_ready` 里也有这条检查兜底，两边都留着 —— 这里的时序
            # 是「它可能正在 poll 与报错之间」）。
            self._cancel.set()
        # 先按**孩子**走（见 `_descendants`：真正跑 ComfyUI 的是壳的孩子），
        # 再停那个壳自己。
        kids = _descendants(pid)
        t0 = time.time()
        for kid in kids:
            try:
                kid.terminate()
            except Exception:                           # noqa: BLE001
                pass
        proc.terminate()
        try:
            code = proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            LOGGER.warning("停后端：pid %s 收到终止信号后 %.0f 秒没退出",
                           pid, STOP_TIMEOUT_S)
            raise LaunchError(
                "pid %s 收到终止信号后 %.0f 秒还没退出 —— 它在做不可中断的 IO，"
                "或者卡住了。任务管理器里结束它。"
                % (pid, STOP_TIMEOUT_S))
        # **停完要核一遍**：壳死了不等于它下面那个也没了。只看 pid 的话，
        # 一个活着的孤儿进程仍然占着显存与端口，而页面上那颗开关已经画成灰的了。
        # （等它走，不是查一眼就判 —— 理由见 `_wait_gone`。）
        left = _wait_gone(kids, proc, STOP_TIMEOUT_S)
        if left:
            LOGGER.warning("停后端：pid %s 停了，但还留着 %s（孩子=%s）",
                           pid, left, [k.pid for k in kids])
            raise LaunchError(
                "pid %s 已经停了，但它派生的 %s 还在运行（那个才是真正跑 ComfyUI 的"
                "进程）。任务管理器里结束它们 —— 页面不把这次当成「已停」。"
                % (pid, "、".join(str(x) for x in left)))
        with self._lock:
            self._proc = None
            self._finish(launch_id, phase="stopped", exit_code=code,
                         error=None, error_short=None)
            self._state["stopped_ms"] = int((time.time() - t0) * 1000)
            # 停了几棵树也记下来 —— 日志台那一行（`backend_stopped` 事件）要说得清
            # 「连孩子一起停了」，而不是只报一个壳的 pid。
            self._state["derived"] = len(kids)
            out = dict(self._state)
        # 成功**不写 logger**：`server.post_backend_stop` 会推一条 `backend_stopped`
        # 事件，那条会进时间线；两边都写就成了同一件事的两行（实测：时间线上
        # 「后端已停」连着出现两次，一句是事件、一句是日志）。**拒绝**那两条反过来
        # 必须写日志 —— 它们不产生事件，不写就没有落点。
        return out

    def status(self, liveness=None):
        """当前状态 + 日志尾部。**没启动过就如实说没有**。

        `liveness` 可以由调用方传进来复用（`/api/backend` 已经探过一次）——
        免得为了同一个问题在一屏里打两次后端。
        """
        with self._lock:
            st = dict(self._state) if self._state else self._fresh_state()
            proc = self._proc
        if st["launch_id"]:
            # 进程句柄只在**本进程**里有效；重启后的工作台认不出来，也不装认识
            alive = proc is not None and proc.poll() is None
            st["process_alive"] = alive
            if alive and st["phase"] == "starting":
                st["elapsed_ms"] = util.now_ms() - (st["started_at"] or 0)
        else:
            st["process_alive"] = False
        # 就绪判断**永远看后端本身**，不看我们记的状态：
        # 后端可能是用户自己开的，也可能工作台重启过而它还在跑
        if liveness is None:
            liveness = self.client.liveness()
        st["backend_up"] = liveness != "down"
        st["backend_ok"] = liveness == "ok"
        st["liveness"] = liveness
        st["log_tail"] = self.log_tail()
        st["ready_timeout_s"] = int(self.ready_timeout)
        return st

    def log_tail(self, max_bytes=LOG_TAIL_BYTES):
        return read_tail(self.log_path, max_bytes)


def read_tail(path, max_bytes=LOG_TAIL_BYTES):
    """读文件尾部若干字节，按行拆开。文件不在/读不了返回空表。

    刻意**不做**「从头读再截断」：ComfyUI 的日志会一直长（每个请求几行），
    启动几次就上兆。只 seek 到末尾附近读，代价是常数。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                # 从中间截起的第一行多半是半截的，丢掉
                f.readline()
            blob = f.read(max_bytes)
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    return [ln.rstrip("\r") for ln in text.splitlines()]


def backend_log_candidates(port=None, extra=()):
    """ComfyUI 自己写的那份日志在哪 —— 按**位置**算，不猜文件名。

    两处 `user/`：正常模式是安装目录的 `user/`（与 CLI 同一个），受限模式是
    工作区 `.cache/comfy-backend/user/`（见 `config.confined_layout`）。
    两份都算候选，页面按**最近写入**挑一份显示，并且把**全部**候选列出来。

    **为什么要列全部**：陈旧的日志比没有更坏 —— 它看起来像「后端死了」。
    实测（2026-10-01）本机后端在跑但空闲了一个半小时，日志最后写入停在
    1.5 小时前，光看文件会以为它早崩了。
    """
    port = port or config.COMFY_PORT
    dirs = []
    for d in (os.path.join(config.COMFY_ROOT, "user"),
              config.confined_layout()["user"]):
        if d not in dirs:
            dirs.append(d)
    out = []
    for index, d in enumerate(dirs):
        where = "安装目录" if index == 0 else "受限模式落点"
        for stem in ("comfyui_%d" % port, "comfyui"):
            # 轮转过的上一份也列出来：当前这份刚开起来时，它才是历史
            for suffix in ("", ".prev", ".prev2"):
                path = os.path.join(d, "%s%s.log" % (stem, suffix))
                if any(c["path"] == path for c in out):
                    continue
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                out.append({
                    "path": path,
                    "label": "ComfyUI 自己写的（%s）%s" % (where, stem),
                    "size": info.st_size,
                    "mtime_ms": int(info.st_mtime * 1000),
                })
    for path, label in extra:
        try:
            info = os.stat(path)
        except OSError:
            continue
        out.append({"path": path, "label": label, "size": info.st_size,
                    "mtime_ms": int(info.st_mtime * 1000)})
    # 最近写入的排最前 —— 页面默认显示第一份，其余进「来源」那一行
    out.sort(key=lambda r: -r["mtime_ms"])
    return out


def readiness_line(lines):
    """日志里有没有出现那句成功标志。给页面做「更早的提示」用。

    **它不决定就绪** —— 决定就绪的是 `/system_stats` 返回 200。
    这两个信号在正常启动时会先后出现，用日志标志只是让页面早一两秒有反应。
    """
    for line in reversed(lines[-200:]):
        if READY_MARKER in line:
            return line.strip()
    return None


# 已知的启动失败特征 → 人话 + 该怎么办。
# **为什么要这张表**：起不来时用户手上只有一段堆栈，而那段堆栈里最像原因的那一行
# 往往不是原因。第一版只给一句「多半是端口被占或 yaml 不对」—— 实测撞上的是
# 「没有可写的临时目录」，于是那句话把人指错了方向。
#
# 「没有可写的临时目录」这一条**不在表里**：它要分「受限模式已经搬过落点」与
# 「没搬过」两种说法（见 `_temp_advice`），而表里每条都只有一句话。
TEMP_SIGNATURE = "No usable temporary directory"

_FAILURE_SIGNATURES = (
    ("address already in use",
     "端口被占了。先关掉占着 %d 的那个程序，或者确认它是不是你要用的后端。"
     % config.COMFY_PORT),
    # 同一个错误在 Windows 上会有两种写法：ComfyUI 那边可能报 `[Errno 10048]`，
    # 也可能报 `WinError 10048`。两种都要认 —— 只认一种的时候，
    # 另一种会掉进「认不出来」的兜底里，而那正是这一版要消灭的情况。
    ("[Errno 10048]",
     "端口被占了（Errno 10048）。先关掉占着 %d 的那个程序。" % config.COMFY_PORT),
    ("WinError 10048",
     "端口被占了（WinError 10048）。先关掉占着 %d 的那个程序。" % config.COMFY_PORT),
    ("extra-model-paths-config",
     "模型路径配置有问题 —— 四个模型文件在后端里不可见。"
     "用 Comfy Desktop 打开一次重新生成那份 yaml。"),
    ("ModuleNotFoundError",
     "缺 Python 依赖 —— ComfyUI 那份 venv 可能坏了，"
     "在它的目录里重跑一次安装。"),
    ("ImportError",
     "导入失败 —— ComfyUI 那份 venv 可能坏了，看日志里第一段 Traceback。"),
)


def _temp_advice(layout):
    """「找不到可写的临时目录」该怎么说。**看这一次到底有没有搬过落点。**

    2026-10-01 之前这里写的是「由别的程序代为启动时子进程会继承写限制」+
    「自己双击 run.bat」—— 两句都实测是错的：那个工作台是**用户自己**在
    Explorer 里双击 `run.bat` 起的（父链就是 explorer.exe），而 `run.bat`
    起的是**工作台**、不是后端，照它做只会原地打转。
    """
    head = "ComfyUI 找不到可写的临时目录（torch 要在里面建编译缓存）。\n"
    if layout and layout.get("mode") == "confined":
        head += ("受限模式**已经**把临时目录指进工作区了，还是写不进去 —— "
                 "说明连工作区都写不了，这个按钮在这台机器上救不回来。\n")
    else:
        head += "它把候选目录都试了一遍（含 %TEMP%），全被拒绝。\n"
    return (head +
            "**办法**：把上面那条命令复制到**你自己的** PowerShell 里跑 —— "
            "那样起的后端不受这个限制。\n"
            "**不要**双击 `run.bat`：那起的是工作台，不是后端。")


def explain_failure(log_tail, exit_code=None, layout=None):
    """把「进程退了」翻译成一句能照着做的话。**先看日志，日志说不出来才泛泛而谈。**

    返回 `(short, detail)`：`short` 进状态行，`detail` 是完整的几句。
    `layout` 是这一次启动的落点方案（`Launcher.layout()`）。
    """
    text = "\n".join(log_tail or [])
    if TEMP_SIGNATURE in text:
        return ("起不来：ComfyUI 找不到可写的临时目录", _temp_advice(layout))
    for needle, advice in _FAILURE_SIGNATURES:
        if needle in text:
            return ("起不来：%s" % advice.splitlines()[0], advice)
    head = "后端进程退出了"
    if exit_code is not None:
        head += "（退出码 %d）" % exit_code
    return (head + " —— 看日志。",
            head + "。日志尾部没有认出已知的原因，"
                   "往上翻日志里第一段 Traceback，那里才是真正的起点。")
