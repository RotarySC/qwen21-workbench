"""测试替身 —— 不碰真实 ComfyUI、不碰真实状态目录、不碰网络。

放在这里而不是各测试文件里，是因为多个测试文件都要用同一套；
`FakeComfy` 刻意实现 `ComfyClient` 的**对外表面**而不是继承它，
这样才能证明「换掉后端整条链路仍然自洽」。

`fake_comfy_install()` / `patch_launch_config()` / `fake_popen()` 是 P6 的
「一键启动」用的：真起一次 ComfyUI 要十几秒、占内存、本机还已经有一个在跑
（起第二个只会撞端口），所以把那三个外部依赖都换成替身。
`test_launch.py` 与 `test_api.py` 共用它们。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workbench import comfy, config, launch  # noqa: E402


# ---------------------------------------------------------------------------
# 历史快照 + 一个新默认
# ---------------------------------------------------------------------------
def with_default_te_speed(wf):
    """把「TE-Speed 默认接上」这个**已知的差**补进一张历史快照。

    `tests/fixtures/*.json` 是**重构之前**的旧脚本产物 —— 那时 `build()` 还没有
    TE-Speed 这个默认值，所以「今天的产物」与「历史快照」之间**应该**只差这一个
    节点。那一层判据要守的正是「其余接线一个字节没变」，所以把差补出来再比，
    而不是整组改成「显式关掉默认再比」—— 后者会让**默认那条路**彻底没人守。

    补的方式是调**构建器自己的** `_wire_te_speed`：在这儿手抄一份接线等于养第二个
    真源，哪天接线改了，抄的那份不会跟着变，也永远不会红。
    """
    import copy

    import qwen21_build as qb          # REPO 已在上面进了 sys.path
    out = copy.deepcopy(wf)
    qb._wire_te_speed(out)
    return out


# ---------------------------------------------------------------------------
# 「真实出图」夹具：候选目录**不止一个**，别再写死安装目录
#
# 落点改过：`--output-directory` 现在指向工作区 `output/`（`config.OUTPUT_DIR`），
# 历史图也搬过去了。写死安装目录的查找会让「拿真图当夹具」的那些测试
# **静默跳过** —— 实测搬完家后 10 条不再执行，而全量套件仍然是绿的，
# 跳过理由还写着「output/ 里没有 PNG」（磁盘上明明有 28 张，只是换了地方）。
# **跳过不是失败**，这才是这类失效最难发现的地方。
#
# 不把 Desktop 的 `ComfyUI-Shared\output` 算进来：那里的图属于**另一个模型**
# （MINIMAX），拿它当夹具会让「模式必须是 t2i/refine/edit」这类断言直接失败。
# ---------------------------------------------------------------------------
def real_output_pngs():
    """真实出图的 PNG 路径，按候选目录顺序收集（工作区优先，安装目录兜底）。"""
    out = []
    seen = set()
    legacy = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Comfy-Desktop",
                          "ComfyUI-Installs", "ComfyUI", "ComfyUI", "output")
    for d in (config.OUTPUT_DIR, legacy):
        if not d or not os.path.isdir(d):
            continue
        key = os.path.normcase(os.path.normpath(d))
        if key in seen:
            continue
        seen.add(key)
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(".png"):
                out.append(os.path.join(d, n))
    return out


def real_output_png_by_name():
    """`{文件名: 完整路径}` —— 要点名某一张图时用。"""
    return {os.path.basename(p): p for p in real_output_pngs()}


# ---------------------------------------------------------------------------
# 临时目录：托管、追踪、到期检查
#
# 为什么需要这一层（实测，不是预防性代码）：解释器曾是 Low 完整性
# （见上面那段），`%TEMP%` **不可写**，于是裸 `tempfile.mkdtemp()` 会**静默退化
# 到当前工作目录**，也就是仓库根。跑一次全量测试会在仓库根掉下 9 个
# `qwen21-api-*` / `qwen21-launch-*` 目录（实测）。它们唯一的内容是
# `backend.log`，而 `.gitignore` 里的 `*.log` 又把它们忽略了 —— 于是
# `git status` 一直是干净的，残留**肉眼和 git 都看不见**。
#
# 2026-10-01 起解释器搬到了工作区外（`%LOCALAPPDATA%\qwen21-venv`，Medium，
# `%TEMP%` 能写了），但**这一层照旧**：泄漏检查需要一个稳定根，而系统临时目录
# 一旦换回受限解释器就又会退化。别把判据建立在「跑测试的这台机器能不能写
# `%TEMP%`」上。
# ---------------------------------------------------------------------------
TEST_TMP_ROOT = os.path.join(HERE, "__pycache__", ".tmp")


def scratch_dir():
    """给测试用的临时目录根，**保证可写且不在仓库根**。

    顺序是刻意反过来的：**先仓库内、再系统临时目录**。两个理由：受限解释器下
    `tempfile.gettempdir()` 自己就退化成**仓库根**（`%TEMP%` 写不进去），于是
    「优先系统临时目录」等于「优先仓库根」；而泄漏检查需要一个**稳定**的根 ——
    它按「落在仓库根」判泄漏，根要是随机变，判据就跟着飘。所以不管 `%TEMP%`
    能不能写，都先走仓库内那一个。
    """
    repo = os.path.realpath(REPO)
    cands = [TEST_TMP_ROOT, tempfile.gettempdir()]
    for base in cands:
        rp = os.path.realpath(base)
        if rp == repo or rp.startswith(repo + os.sep) and base != TEST_TMP_ROOT:
            continue                      # 不把仓库根/仓库内其它位置当临时目录
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".w")
            with open(probe, "w"):
                pass
            os.remove(probe)
            return base
        except OSError:
            continue
    return None          # 交给调用方如实报错，不假装有


class _TempTracker:
    """托管 `tempfile.mkdtemp`，让每条测试的临时目录都暴露出来。"""

    def __init__(self):
        self.made = []          # 本次测试新建的
        self._orig = None
        self._bad = []          # (测试名, 路径) 累积，供收尾报告用
        self._pinned = set()    # 进这条测试之前 scratch 里**已有**的目录

    def patch(self):
        if self._orig is not None:
            return
        self._orig = tempfile.mkdtemp
        # 包在**当时**的 mkdtemp 外面（可能是调用方自己打的桩），
        # 这样即使调用方换了实现也仍然被追踪到。
        def _mkdtemp(*a, **kw):
            p = self._orig(*a, **kw)
            self.made.append(p)
            return p
        tempfile.mkdtemp = _mkdtemp

    def restore(self):
        if self._orig is None:
            return
        tempfile.mkdtemp = self._orig
        self._orig = None

    def begin(self):
        """开始追踪一条测试。**必须在 setUp 最开头调用。**"""
        self.patch()
        self.made = []
        # 记下进这条测试之前已经存在的东西：那是**类级**目录（`setUpClass` 建的），
        # 要活到 `tearDownClass`，绝不能当成垃圾收拾掉。
        self._pinned = self._snapshot()

    @staticmethod
    def _snapshot():
        try:
            names = os.listdir(TEST_TMP_ROOT)
        except OSError:
            return set()
        return {os.path.realpath(os.path.join(TEST_TMP_ROOT, n)) for n in names}

    @property
    def pinned(self):
        return self._pinned

    def check(self, test, result):
        """在 `stopTest` 里查泄漏 —— 那时 `addCleanup` 已经跑完了。

        为什么不在 `tearDown` 里查：`addCleanup` 注册的 `rmtree` 是在
        `tearDown` **之后**才执行的（`TestCase.run` 的顺序是 setUp → test →
        tearDown → doCleanups → stopTest），在 tearDown 里查会把每一条都误报成泄漏。
        所以这里挂在 `stopTest` 上，并且只有真的还有东西留下来才报。

        只把**落在仓库根**的算泄漏：`scratch_dir()` 是仓库内有意使用的临时目录
        （`tests/__pycache__/.tmp`，已忽略并按前缀自动清理），不算泄漏。
        """
        repo = os.path.realpath(REPO)
        scratch = os.path.realpath(TEST_TMP_ROOT)
        leaked = []
        for p in self.made:
            rp = os.path.realpath(p)
            if rp == scratch or rp.startswith(scratch + os.sep):
                continue                      # 有意使用的 scratch，不算泄漏
            if (rp == repo or rp.startswith(repo + os.sep)) and os.path.isdir(p):
                leaked.append(p)

        for p in self.made:
            if os.path.isdir(p):
                break
        else:
            return                       # 这条测试没建过任何临时目录，无需清理
        # 把 scratch 下**这条测试自己建的、而且没人再认领的**目录收掉。
        #
        # 只碰自己建的，这很重要：这里曾经写成「扫 scratch 下所有目录并 rmtree」，
        # 结果把 `test_legacy_cli` 在 `setUpClass` 里建的**备份目录**删了 ——
        # 那目录要留到 `tearDownClass` 才用来还原现场，于是整组测试连带
        # `tearDownClass` 一起红。收拾垃圾不能顺手删别人正在用的东西。
        for p in self.made:
            rp = os.path.realpath(p)
            if not rp.startswith(scratch + os.sep):
                continue
            if rp in self.pinned:        # 类级目录：活到 tearDownClass
                continue
            shutil.rmtree(p, ignore_errors=True)

        if leaked:
            self._bad.append((test.id(), leaked))
            result.addFailure(test, self._as_failure(test, leaked))

    @staticmethod
    def _as_failure(test, leaked):
        msg = ("这些临时目录没被清掉，留在了仓库根：\n  "
               + "\n  ".join(leaked)
               + "\n（临时目录请用 support.scratch_dir() 作为相对根，"
                 "并在 tearDown/addCleanup 里删掉）")
        try:
            raise AssertionError(msg)
        except AssertionError:
            return sys.exc_info()

    def leaked_report(self):
        return list(self._bad)

    def leftovers(self):
        """仓库根（以及 tests/ 下）没被任何一条测试收掉的临时目录。"""
        found = []
        for parent in (REPO, HERE):
            try:
                names = os.listdir(parent)
            except OSError:
                continue
            for n in names:
                if n.startswith(("qwen21-", "smoke-")) and os.path.isdir(
                        os.path.join(parent, n)):
                    found.append(os.path.join(parent, n))
        return sorted(found)


TRACKER = _TempTracker()


class _CheckOnStopTest:
    """把 TestResult 包一层，好在 `stopTest` 里查泄漏。

    `TestCase.run()` 内部一定会调 `result.stopTest(self)`（就在 `doCleanups()`
    之后），所以挂在**这里**是确定会被执行到的公开行为。

    前两版都栽在「钩子其实从没被调用」上，值得记下来：
      1. `def stopTest(self)` 写在 TestCase 上 —— `TestCase` **没有**这个方法，
         它是 TestResult 的；写上去就是个死方法。
      2. 覆盖 `TestCase.run()` 去读 `self._outcome` —— `_outcome` 只在
         `TestCase.run()` **内部**才设置，在外面读永远是 `None`，
         于是每次都走「拿不到内部状态」的退路，检查一次没跑。
    两次都是**测试依然全绿、防护其实是摆设** —— 这种沉默失效比没有防护更糟，
    所以现在有 `_LeakCheckSelfTest` 拿反面控制盯着它。
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def stopTest(self, test):
        inner = self._inner
        try:
            TRACKER.check(test, inner)
        finally:
            inner.stopTest(test)


class LeakCheckedTestCase(unittest.TestCase):
    """测试基类：让「临时目录忘了删」当场变红，而不是攒成仓库根的垃圾。

    写法上只要求子类的 `setUp` 第一行调 `super().setUp()`。
    """

    def setUp(self):
        super().setUp()
        TRACKER.begin()          # 子类若覆盖 setUp，第一行调 super() 即可

    def run(self, result=None):
        return super().run(_CheckOnStopTest(result or self.defaultTestResult()))


# ---------------------------------------------------------------------------
# 本进程令牌的完整性等级（Windows 强制完整性控制）
#
# 为什么测试也要读它：`%TEMP%` 与 ComfyUI 安装目录的 DACL 上都写着
# `<用户>:(F)`，可解释器若是 Low，写它们一律 `PermissionError` —— 拦人的是
# **强制标签**，不是 DACL（权限位看不出这种拒绝，这是本仓库的老坑）。
#
# Low 只有两个来源：父进程的令牌，或者**映像文件自己**的标签（取两者较小）。
# 2026-10-01 实测来源是后者：DSH 的沙箱给工作区根 `…\Projects\dsh` 打了一个
# **继承式** Low 标签（那一行没有 `(I)`，就设在那一层），于是那棵树下
# **每个新建文件**都是 Low —— 包括 9/27 建的 `.venv`（而建它的解释器是
# Medium：文件标签由**创建位置**决定，与创建者无关）。
# 所以「解释器放在哪个目录里」是这条链路唯一要紧的事，而它一改变，
# `%TEMP%` / 安装目录能不能写、后端起不起得来就跟着变。
# 判据落在这里（而不是产品里）：这是**环境**的性质，产品只负责如实报后果。
# ---------------------------------------------------------------------------
INTEGRITY_NAMES = {
    "S-1-16-0": "untrusted",
    "S-1-16-4096": "low",
    "S-1-16-8192": "medium",
    "S-1-16-12288": "high",
    "S-1-16-16384": "system",
}

# 能写的下限：Medium 及以上可以写「无标签」（按 Medium 算）与更低的标签；
# Low 只能写带 Low 标签的地方 —— 也就是只能写被打标的那棵树。
INTEGRITY_CAN_WRITE_OUTSIDE = ("medium", "high", "system")


def token_integrity():
    """本进程令牌的完整性等级 SID（如 `S-1-16-8192`）；读不到返回 None。

    只用 `ctypes`（标准库）：`pywin32` 能三行写出来，但它不在
    `requirements.txt` 里，而这条判据不值得新增一个依赖。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        # 64 位下 HANDLE 是 8 字节：不写 restype/argtypes 会被截断成 int32
        # （`logs/il_probe.py` 第一版就栽在这儿，报的是「OpenProcessToken 失败」）。
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_wchar_p)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

        TOKEN_QUERY, TOKEN_INTEGRITY_LEVEL = 0x0008, 25
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                        TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0,
                                         ctypes.byref(size))
            buf = ctypes.create_string_buffer(max(size.value, 8))
            if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf,
                                                size.value, ctypes.byref(size)):
                return None
            # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attr } }
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            out = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
                return None
            value = out.value
            kernel32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
            return value
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return None          # 读不到就是读不到，别让探针本身变成失败源


def integrity_name(sid=None):
    """`S-1-16-8192` → `medium`；SID 不认识或读不到就原样回传。"""
    sid = token_integrity() if sid is None else sid
    if not sid:
        return "unknown"
    return INTEGRITY_NAMES.get(sid, sid)


# ---------------------------------------------------------------------------
# P6「一键启动」的替身
# ---------------------------------------------------------------------------
def fake_comfy_install(tmp, with_yaml=True):
    """造一棵「看起来像 ComfyUI 安装」的目录树。

    返回 `(root, exe, yaml_path_or_None)`。`preflight()` 会去查这三样东西在不在，
    所以测试要能把它们造出来、也能单独删掉一个来验提前报错。
    """
    root = os.path.join(tmp, "comfy")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "main.py"), "w"):
        pass
    exe = os.path.join(root, "python.exe")
    with open(exe, "w"):
        pass
    ydir = os.path.join(tmp, "Comfy Desktop", "instance-model-paths")
    os.makedirs(ydir, exist_ok=True)
    ypath = os.path.join(ydir, "inst-1.yaml")
    if with_yaml:
        with open(ypath, "w"):
            pass
    return root, exe, (ypath if with_yaml else None)


def patch_launch_config(root, exe, yaml_path):
    """把 `config` 里与「现场」相关的常量打上桩，返回那条 argv。

    它们都是模块级算好的（`COMFY_ROOT` / `start_command()` / glob 出来的 yaml），
    只能这么换。**调用方负责在 tearDown 里 stop 这些 patcher。**

    顺带把「这台机器认哪个临时目录」也钉住：本机的工作区解释器**曾经**自己就是
    受限的（只能写工作区），不钉的话「启动命令里该有哪些开关」会随跑测试的机器
    变。受限那一支由 `test_launch.ConfinedLayout` 注入探测器专门测 —— 所以这里
    钉的是「这一支测试不依赖机器状态」，不是「受限不存在」。
    """
    argv = [exe, "-s", "main.py", "--enable-manager",
            "--extra-model-paths-config", yaml_path or "<none>", "--port", "8188"]
    stub_temp = os.path.join(root, "temp")
    os.makedirs(stub_temp, exist_ok=True)
    patchers = [
        mock.patch.object(launch, "env_temp_dir", lambda: stub_temp),
        mock.patch.object(config, "COMFY_ROOT", root),
        mock.patch.object(config, "start_command",
                          lambda: {"cwd": root, "argv": argv,
                                   "yaml_found": bool(yaml_path),
                                   "display": " ".join(argv)}),
        mock.patch.object(config, "find_instance_yaml", lambda: yaml_path),
    ]
    for p in patchers:
        p.start()
    return argv, patchers


class FakeProc:
    """够用的 Popen 替身：`poll()` 返回预设退出码，或者一直活着。

    `terminate()` / `wait()` 是给「停后端」（P20i）用的：默认的替身真的会被停掉
    （`terminate` 之后再 `poll` 就有退出码了），`stubborn=True` 的那种则
    「收到信号也不退」—— 那条路必须**如实报没停掉**，不许假装成功。
    """

    def __init__(self, pid=4242, exit_code=None, exit_after=0.0, stubborn=False):
        self.pid = pid
        self._code = exit_code
        self._exit_after = exit_after
        self._born = time.time()
        self.stubborn = stubborn
        self.terminated = False

    def poll(self):
        if self._code is None:
            return None
        if self._exit_after and (time.time() - self._born) < self._exit_after:
            return None
        return self._code

    def terminate(self):
        self.terminated = True
        if self._code is None and not self.stubborn:
            self._code = -1          # TerminateProcess 的样子：进程没了

    def wait(self, timeout=None):
        if self._code is None:
            raise launch.subprocess.TimeoutExpired(str(self.pid), timeout)
        return self._code


def fake_popen(proc=None, captured=None):
    """换掉 `launch.subprocess.Popen`，顺便记下真实传进去的参数。"""
    holder = {"argv": None, "kw": None}

    def _popen(a, **kw):
        holder["argv"] = list(a)
        holder["kw"] = kw
        if captured is not None:
            captured.append(holder)
        return proc or FakeProc()
    return mock.patch.object(launch.subprocess, "Popen", _popen), holder


def full_models(present=True, note=None):
    return {key: {"node_class": nc, "field": f, "filename": fn,
                  "present": present, "option_count": 3, "note": note}
            for key, nc, f, fn in config.REQUIRED_MODELS}


class FakeComfy:
    def __init__(self, running=True, models=None, submit_error=None,
                 input_dir=None, input_options=None, argv=None,
                 node_classes=(), combos=None):
        self.running_flag = running
        self.models = full_models() if models is None else models
        self.submit_error = submit_error
        # P5：「扫到一半崩了」要能造出来 —— 第 N 次提交失败（从 1 起算）
        self.fail_at = None
        self.submitted = []
        self.cancelled = []
        self.queue = {}          # prompt_id -> "running" | "pending"
        # 提交过的任务自动算「还在排队」。P5 的撤销路径要区分
        # 「确知它排着」与「读不到队列」，测试得能把前者造出来。
        self.auto_queue = False
        self.history_map = {}    # prompt_id -> history 条目
        self.views = {}          # filename -> (content_type, bytes)
        self.closed = False
        # P4：参考图。`input_dir` 是「我们以为的落点」，`input_options` 是
        # 「后端说它能读到什么」—— 两者**故意分开**，测试才能构造出
        # 「写进去了但后端看不见」这个真实存在的情形。
        self.input_dir = input_dir
        self.input_options = input_options
        self.argv = argv           # None = 不给 argv（老后端/读不到）
        # 已注册的可选插件节点类。默认空 = "后端在跑，但没装任何插件" —— 这正是
        # 一台干净机器的样子，也是新开关最该被测到的那个状态。
        self.node_classes = set(node_classes)
        # 可选插件节点各自的 COMBO 输入：`{类名: {字段: [选项…]}}`。
        # 放大的 `TESpeedVOSR2Loader.model_bundle` 就是靠它判「权重下没下」——
        # 那个候选表是插件扫 `models/vosr2/` 扫出来的，空表=没下。
        # **和真后端同一个形状**（`[[选项…], {}]`），所以 `combo_options`
        # 走的还是同一条读法，替身不另写一份解析。
        self.combos = {k: dict(v) for k, v in (combos or {}).items()}
        self.object_info_calls = []
        # P4：`unclear` 是第三种状态 —— 连上了但没干净答完（超时/5xx）。
        # 与「没起」必须分开：这时进程活着，提交照样该被接受（ComfyUI 自己排队）。
        self.unclear = False

    def probe(self):
        if self.unclear:
            return None
        return True if self.running_flag else False

    def liveness(self):
        return {True: "ok", False: "down", None: "unclear"}[self.probe()]

    def running(self):
        return self.probe() is not False

    def system_stats(self):
        system = {"comfyui_version": "0.37.4"}
        if self.argv is not None:
            system["argv"] = list(self.argv)
        return {"system": system,
                "devices": [{"name": "cuda:0", "vram_total": 23621992448,
                             "vram_free": 6522807212}]}

    def object_info(self, node_class):
        self.object_info_calls.append(node_class)
        if node_class == "LoadImage" and self.input_options is not None:
            return {"LoadImage": {"input": {
                "required": {"image": [list(self.input_options), {}]}}}}
        # **实测过的形状**（真后端 8188）：类存在 → 200 + `{类名: {...}}`；
        # 类不存在 → **200 + `{}`**（不是 404）。所以「类不在返回的字典里」
        # 才是「后端没有这个类」的判据，而不是「读不到」。
        if node_class in self.node_classes:
            required = {}
            for field, options in (self.combos.get(node_class) or {}).items():
                required[field] = [list(options), {}]
            return {node_class: {"input": {"required": required}}}
        return {}

    def combo_options(self, node_class, field):
        return self._combo(node_class, field)

    def upload_image(self, name, data, overwrite=True, subfolder="", type_="input"):
        """替身版的 `POST /upload/image` —— 行为照抄真后端。

        两处必须一样，否则测试验的就不是真那条路：

        1. 写进**它自己的** input 目录（`input_dir`）。这正是这条路的全部价值 ——
           落点由后端决定，我们不必猜。
        2. `overwrite` 不为真时撞上同名**改名**（`a (1).png`），而不是拒绝。
           真后端的 `server.py::image_upload` 就是这么写的；「拒绝同名」那条
           承诺是**我们**在 `upload.upload_any` 里另外兜的。

        刻意**不**顺手更新 `input_options`：那张表是「后端说它能读到什么」，
        与「我刚写了什么」是两件事。好几条测试正是靠「写进去了但候选表还没更新」
        来构造未验证那一格 —— 替身替它更新了，那一格就永远测不到了。
        """
        if not self.running_flag:
            raise comfy.ComfyUnreachable("替身：后端没起，上传无从谈起")
        if not self.input_dir:
            raise comfy.ComfyUploadFailed("替身：没有 input 目录")
        final = name
        if not overwrite:
            stem, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(os.path.join(self.input_dir, final)):
                final = "%s (%d)%s" % (stem, i, ext)
                i += 1
        os.makedirs(self.input_dir, exist_ok=True)
        with open(os.path.join(self.input_dir, final), "wb") as f:
            f.write(bytes(data))
        self.uploaded = getattr(self, "uploaded", [])
        self.uploaded.append((final, bytes(data)))
        return {"name": final, "subfolder": subfolder or "",
                "type": type_ or "input"}

    def _combo(self, node_class, field):
        info = self.object_info(node_class)
        if not isinstance(info, dict):
            return None
        node = info.get(node_class)
        if not isinstance(node, dict):
            return None
        spec = ((node.get("input") or {}).get("required") or {}).get(field)
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return spec[0]
        return None

    def models_report(self, entries=None):
        """替身**必须照抄真 client 的签名**（`entries` 是「查哪几个文件」的整份表）。

        少一个参数就是 `TypeError`，而它在 `asyncio.to_thread` 里 —— 报出来的是
        一坨「协程失败」，看不出是签名的事。五条链路各有自己那张表（Qwen 四个、
        H3 五个、拆层两个目录），所以这里也得能收。
        """
        if entries is None:
            return dict(self.models)
        out = {}
        for key, node_class, field, filename in entries:
            if key in self.models:
                out[key] = self.models[key]
                continue
            options = self.combo_options(node_class, field)
            out[key] = {
                "node_class": node_class, "field": field, "filename": filename,
                "present": None if options is None else (filename in options),
                "option_count": None if options is None else len(options),
                "note": None if options is not None else
                        "读不到 %s 的 %s 候选表" % (node_class, field),
            }
        return out

    def nodes_report(self, entries=None):
        # 判定复用 `comfy.node_presence` —— 替身不另写一份判据，
        # 否则测试就是拿自己的假设去验自己的实现。
        return comfy.node_presence(self.object_info, entries)

    def queue_ids(self):
        if self.auto_queue:
            return {s["job_id"]: "pending" for s in self.submitted}
        return dict(self.queue)

    def history(self, prompt_id=None):
        if prompt_id is None:
            return dict(self.history_map)
        if prompt_id in self.history_map:
            return {prompt_id: self.history_map[prompt_id]}
        return {}

    def submit(self, workflow, job_id, client_id):
        self.submitted.append({"workflow": workflow, "job_id": job_id,
                               "client_id": client_id})
        if self.fail_at and len(self.submitted) == self.fail_at:
            raise self.submit_error or comfy.ComfyRejected(
                "第 %d 次提交被拒（测试造的）" % self.fail_at, None, None)
        if self.submit_error:
            raise self.submit_error
        return {"prompt_id": job_id, "number": len(self.submitted)}

    def cancel(self, job_id, running=False):
        self.cancelled.append((job_id, running))
        return ["interrupt", "queue-delete"]

    def fetch_view(self, filename, subfolder="", type_="output"):
        if filename not in self.views:
            raise comfy.ComfyUnreachable("取图失败：没有 %s" % filename)
        return self.views[filename]

    def close(self):
        self.closed = True


class RecordingBroker:
    """记录事件而不真推 —— 单测要的是「发了什么」，不是「谁收到了」。"""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)
        return len(self.events)

    def publish_threadsafe(self, event):
        return self.publish(event)

    def bind_loop(self, loop):
        pass

    def subscribe(self):
        raise NotImplementedError("RecordingBroker 不订阅")

    def unsubscribe(self, sid):
        pass

    def replay_since(self, last_id):
        return []

    @property
    def subscriber_count(self):
        return 0

    def types(self):
        return [e.get("type") for e in self.events]

    def of(self, etype):
        return [e for e in self.events if e.get("type") == etype]


def history_entry(prompt_id, outputs=None, cached=None, start=1000, end=51000,
                  error=None, interrupted=False):
    """造一个与 ComfyUI `/history/<id>` 同形的条目（字段名照实测抄的）。"""
    messages = [["execution_start", {"prompt_id": prompt_id, "timestamp": start}]]
    if cached:
        messages.append(["execution_cached",
                         {"nodes": cached, "prompt_id": prompt_id,
                          "timestamp": start + 7}])
    if error:
        messages.append(["execution_error", {
            "prompt_id": prompt_id, "node_id": error.get("node_id", "459:458"),
            "node_type": error.get("node_type", "KSampler"),
            "exception_message": error.get("message", "boom"),
            "exception_type": error.get("type", "RuntimeError"),
            "traceback": error.get("traceback", ["line a", "line b"]),
            "timestamp": end}])
    if interrupted:
        messages.append(["execution_interrupted",
                         {"prompt_id": prompt_id, "node_id": "459:458",
                          "timestamp": end}])
    elif not error:
        messages.append(["execution_success",
                         {"prompt_id": prompt_id, "timestamp": end}])
    return {
        "prompt": [1, prompt_id, {"459:458": {"class_type": "KSampler"}}, {}, []],
        "outputs": outputs if outputs is not None else {
            "461": {"images": [{"filename": "Qwen_image_2.1_00042.png",
                                "subfolder": "", "type": "output"}]}},
        "status": {
            "status_str": "error" if error else "success",
            "completed": not error and not interrupted,
            "messages": messages,
        },
    }
