#!/usr/bin/env python3
"""一键启动后端（P6）—— 不真的起 ComfyUI，用假进程 + 假探活。

真起一次要十几秒、占内存、还要模型路径齐全，而且**本机就有个后端在跑**，
起第二个只会撞端口。所以这里把「进程」与「探活」两个外部世界都换成替身，
专测那些真正容易错的地方：

- 端口上已经有响应时**必须拒绝**（不抢别人的实例）；
- 起之前静态能查出来的问题（解释器/入口/yaml 不在）**要提前报**，
  而不是让用户等 180 秒看到一句「超时」；
- 三种结局都要能说清：就绪 / 进程退出 / 超时，**每种都带上日志尾部**；
- 命令是**逐元素**拼的，绝不过 shell —— 那个 yaml 路径含空格，
  拼成字符串就会断成两截（这正是不能用 `launch_comfyui` 的原因）。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench import config, launch  # noqa: E402


class FakeProc:
    """够用的 Popen 替身：`poll()` 返回预设的退出码，或者一直活着。

    `terminate()` / `wait()` 是给「停后端」（P20i）用的 —— 与 `support.FakeProc`
    同一套语义（那边给路由层用，这边给 Launcher 单测用）。
    """

    def __init__(self, pid=4242, exit_code=None, exit_after=0.0, stubborn=False):
        self.pid = pid
        self._code = exit_code
        self._exit_after = exit_after
        self._born = time.time()
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    def poll(self):
        if self._code is None:
            return None
        if self._exit_after and (time.time() - self._born) < self._exit_after:
            return None
        return self._code

    def terminate(self):
        self.terminated = True
        self.killed = True
        if self._code is None and not self.stubborn:
            self._code = -1

    def wait(self, timeout=None):
        if self._code is None:
            raise launch.subprocess.TimeoutExpired(str(self.pid), timeout)
        return self._code


def ok_probe(directory=None):
    """可写性探测器替身：一律说能写。

    **必须给**：本机的工作区解释器就受限（实测只能写工作区），不给替身的话
    「起得来 / 命令不带多余开关」这些用例的结论就会取决于跑测试的是哪台机器。
    """
    return True, None


def denying_probe(denied):
    """探测器替身：`denied` 里的路径一律说写不了，并记下探过哪些路径。"""
    calls = []

    def probe(directory):
        calls.append(directory)
        if directory in denied:
            return False, "Permission denied"
        return True, None
    probe.calls = calls
    return probe


class LaunchBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-launch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = os.path.join(self.tmp, "backend.log")
        self.client = support.FakeComfy(running=False)   # 一开始后端没起
        self.events = []
        self.launcher = launch.Launcher(
            self.client, log_path=self.log,
            probe_report=os.path.join(self.tmp, "report.json"),
            probe_state=os.path.join(self.tmp, "probe_state.json"),
            ready_timeout=2.0, on_event=self.events.append,
            probe=ok_probe)

    def fake_root(self, yaml=True):
        """造一个「看起来像 ComfyUI 安装」的目录，让 preflight 过。"""
        root = os.path.join(self.tmp, "comfy")
        os.makedirs(root, exist_ok=True)
        open(os.path.join(root, "main.py"), "w").close()
        exe = os.path.join(root, "python.exe")
        open(exe, "w").close()
        ydir = os.path.join(self.tmp, "Comfy Desktop", "instance-model-paths")
        os.makedirs(ydir, exist_ok=True)
        ypath = os.path.join(ydir, "inst-1.yaml")
        if yaml:
            open(ypath, "w").close()
        return root, exe, (ypath if yaml else None)

    def patch_env(self, root, exe, ypath):
        """把 config 里那几个「现场相关」的常量打上桩。

        `COMFY_ROOT` / `start_command()` 都是模块级算好的，只能这么换。
        """
        argv = [exe, "-s", "main.py", "--enable-manager",
                "--extra-model-paths-config", ypath or "<none>", "--port", "8188"]
        patchers = [
            mock.patch.object(config, "COMFY_ROOT", root),
            mock.patch.object(config, "start_command",
                              lambda: {"cwd": root, "argv": argv,
                                       "yaml_found": bool(ypath),
                                       "display": " ".join(argv)}),
            mock.patch.object(config, "find_instance_yaml", lambda: ypath),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return argv

    def spawn(self, argv, proc=None, captured=None):
        """把 subprocess.Popen 换掉，顺便记下真实传进去的参数。"""
        holder = {"argv": None, "kw": None}

        def fake_popen(a, **kw):
            holder["argv"] = list(a)
            holder["kw"] = kw
            if captured is not None:
                captured.append(holder)
            return proc or FakeProc()
        p = mock.patch.object(launch.subprocess, "Popen", fake_popen)
        p.start()
        self.addCleanup(p.stop)
        return holder


class CommandCarriesTheOutputDir(support.LeakCheckedTestCase):
    """落盘开关必须出现在**命令的唯一真源**里。

    页面给用户复制的那条命令、以及「一键启动」按钮，都从 `config.start_command()`
    取 argv。于是「只改文档、忘了改这里」会让两边悄悄分家：文档说写工作区，
    页面却递出一条写安装目录的命令 —— 而这种分家**没有症状**，图照样出，
    只是跑到别处去了。所以这条守的是「写者与文档一致」。
    """

    def test_argv_points_comfyui_at_the_workspace(self):
        argv = config.start_command()["argv"]
        self.assertIn("--output-directory", argv)
        self.assertEqual(argv[argv.index("--output-directory") + 1],
                         config.OUTPUT_DIR)
        self.assertTrue(os.path.isabs(config.OUTPUT_DIR),
                        "落盘目录必须是绝对路径 —— 否则相对的是 ComfyUI 的 cwd")

    def test_the_copied_command_says_the_same_thing(self):
        cmd = config.start_command()["display"]
        self.assertIn('--output-directory "%s"' % config.OUTPUT_DIR, cmd)

    def test_the_copied_command_carries_the_backend_env(self):
        """★ 那条可复制命令**也必须**带上 `HF_ENDPOINT`。

        与 `child_env()` 是同一份真源。只改一处的话，「从开关起的后端」与
        「照抄命令起的后端」就跑在两套环境里，而页面分不出这两者 ——
        照抄那条起出来的后端跑不了 See-through。
        """
        command = config.start_command()
        self.assertIn('set "HF_ENDPOINT=https://hf-mirror.com"', command["display"])
        self.assertEqual(command["env"], dict(config.BACKEND_ENV))

    def test_every_backend_env_var_reaches_the_copied_command(self):
        """★ 这条判据是**跟着 `BACKEND_ENV` 长**的，不逐字钉某几个变量。

        `BACKEND_ENV` 里每加一个变量，这里自动跟着守 —— 否则加了 `HF_HOME`
        那种「位置类」变量、只改了一半，照抄那条命令起的后端就换了一套缓存，
        而两边的 `models` 体检都写着正常。
        """
        display = config.start_command()["display"]
        for key, value in sorted(config.BACKEND_ENV.items()):
            self.assertIn('set "%s=%s"' % (key, value), display)

    def test_the_cache_roots_are_pinned_and_do_not_move_with_the_account(self):
        """缓存位置必须是**绝对路径**、且由 `CACHE_ROOT` 定死。

        默认值（`HF_HOME` = `~/.cache/huggingface`、`TORCH_HOME` = `~/.cache/torch`）
        是**账户相对**的：同一个后端以 SYSTEM 起与以普通用户起看到的不是同一个
        目录。实测后果是 LaMa 检查点在 SYSTEM 眼里「不存在」⇒ 前后发静默地不拆。

        ⚠ 判据是「**不随账户变**」，**不是**「不在 profile 目录下面」—— 实测
        `CACHE_ROOT` 本身就落在用户目录底下（`%USERPROFILE%\…`），那**没关系**：
        SYSTEM 对那个路径有完全访问权（实测：SYSTEM 起的那次出图把
        `refs/main` 真写进了共享缓存）。要紧的是那个字符串是**算出来的常量**，
        不是 `expanduser` 出来的。
        """
        for key in ("HF_HOME", "TORCH_HOME"):
            value = config.BACKEND_ENV[key]
            self.assertTrue(os.path.isabs(value), (key, value))
            self.assertNotIn("~", value, key)
            self.assertTrue(value.startswith(config.CACHE_ROOT), (key, value))
        self.assertEqual(
            config.CACHE_ROOT,
            os.environ.get("QWEN21_CACHE_DIR")
            or os.path.join(os.path.dirname(config.COMFY_ROOT), "cache"))

    def test_the_copied_command_is_a_batch_body_not_a_oneliner(self):
        """环境变量**单独占一行**。

        实测 `cmd /c set "VAR=…" && cd /d … && python …` 那种一行链撑不住
        （后端会起不来，而页面照旧说「命令给你了」）。所以 display 必须是一段
        `.bat` 的正文：`set` 一行、`cd` 一行、启动一行。
        """
        display = config.start_command()["display"]
        lines = [ln for ln in display.split("\n") if ln.strip()]
        self.assertGreaterEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("set "), lines[0])
        self.assertNotIn("&&", display)

    def test_a_space_in_the_path_does_not_split_it(self):
        """路径将来含空格也得是**一个**元素 —— 别把它「优化」成字符串过 shell。"""
        weird = r"C:\has space\out"
        with mock.patch.object(config, "OUTPUT_DIR", weird):
            command = config.start_command()
            argv = command["argv"]
            self.assertEqual(argv[argv.index("--output-directory") + 1], weird)
            self.assertIn('--output-directory "%s"' % weird, command["display"])


class BootScriptIsTheSameSource(support.LeakCheckedTestCase):
    """开机脚本必须与 `BACKEND_ENV` 同源 —— 而它**不在本仓库里**，只能对照。

    后端有**三条**起来的路：工作台那颗开关、页面上那条可复制命令、开机时那个
    计划任务。前两条同源（都在 `config` 里），第三条跑的是安装目录里那个 `.bat`。

    2026-10-12 实测（出图主机重启之后）：后端由开机任务以 **SYSTEM** 拉起，
    那条 `.bat` 里没有 `HF_ENDPOINT` ⇒ 拆层在 `huggingface.co` 上退避 5 次、
    GPU 0%、整次跑死，而工作台说「后端就绪」。所以这条判据守的不是文案，
    是**那个文件里到底有没有那几行**。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbboot")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(config, "COMFY_ROOT", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def write(self, lines):
        """按 Windows 的 CRLF 写一份 `.bat` —— 那个文件本来就是 CRLF。"""
        path = os.path.join(self.tmp, config.BOOT_SCRIPT_NAME)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\r\n".join(["@echo off"] + list(lines)) + "\r\n")
        return path

    def env_lines(self, skip=(), quoted=True):
        out = []
        for key, value in sorted(config.BACKEND_ENV.items()):
            if key in skip:
                continue
            out.append('set "%s=%s"' % (key, value) if quoted
                       else "set %s=%s" % (key, value))
        return out

    def test_the_name_is_the_one_the_scheduled_task_runs(self):
        self.assertEqual(config.BOOT_SCRIPT_NAME, "start-comfyui.bat")

    def test_a_matching_script_is_ok(self):
        self.write(self.env_lines())
        got = config.boot_script()
        self.assertTrue(got["present"])
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["missing"], [])
        self.assertEqual(got["wrong"], [])

    def test_the_unquoted_form_also_matches(self):
        """`set K=V` 与 `set "K=V"` 都得算数 —— 两种写法在真机上都出现过。"""
        self.write(self.env_lines(quoted=False))
        self.assertTrue(config.boot_script()["ok"])

    def test_a_missing_variable_is_reported(self):
        """★ 就是 2026-10-12 那一台：`HF_ENDPOINT` 不在。"""
        self.write(self.env_lines(skip={"HF_ENDPOINT"}))
        got = config.boot_script()
        self.assertFalse(got["ok"])
        self.assertIn("HF_ENDPOINT", got["missing"])
        self.assertEqual(got["wrong"], [])

    def test_a_wrong_value_is_reported_with_both_sides(self):
        """值不一样比「没有」更坏：看起来设了，其实指向别处。"""
        lines = self.env_lines(skip={"HF_ENDPOINT"})
        lines.append('set "HF_ENDPOINT=https://huggingface.co"')
        self.write(lines)
        got = config.boot_script()
        self.assertEqual([w["key"] for w in got["wrong"]], ["HF_ENDPOINT"])
        self.assertEqual(got["wrong"][0]["want"], "https://hf-mirror.com")
        self.assertEqual(got["wrong"][0]["got"], "https://huggingface.co")
        self.assertEqual(got["missing"], [])

    def test_a_commented_out_line_does_not_count(self):
        """注释掉一行等于没设 —— 而「注释掉」正是最容易骗过肉眼的写法。

        两个变量都得**真的不在**文件里，否则这条判据自己就骗了自己：
        追加一行注释掉的 `HF_HOME`、而它上面早有一行真的 `HF_HOME`，那
        「没算数」与「本来就在」两种成因分不开。
        """
        lines = self.env_lines(skip={"HF_ENDPOINT", "HF_HOME"})
        lines.append('REM set "HF_ENDPOINT=https://hf-mirror.com"')
        lines.append(':: set "HF_HOME=%s"' % config.BACKEND_ENV["HF_HOME"])
        self.write(lines)
        got = config.boot_script()
        self.assertIn("HF_ENDPOINT", got["missing"])
        self.assertIn("HF_HOME", got["missing"])

    def test_an_absent_script_is_reported_not_raised(self):
        """没有这条开机路径**不是错误**（开发机就没有）—— 但也不能算「同源」。"""
        got = config.boot_script()
        self.assertFalse(got["present"])
        self.assertFalse(got["ok"])
        self.assertEqual(got["missing"], sorted(config.BACKEND_ENV))
        self.assertTrue(got["path"].endswith(config.BOOT_SCRIPT_NAME))

    def test_the_rest_of_the_crlf_file_does_not_confuse_it(self):
        """真 `.bat` 里那一堆 `REM` 与 `cd /d` 都不许被当成变量。"""
        lines = ["REM Start ComfyUI backend (Windows side, not WSL).",
                 "cd /d \"C:\\somewhere\""] + self.env_lines() + [
                     '".venv\\Scripts\\python.exe" -s main.py --port 8188']
        self.write(lines)
        got = config.boot_script()
        self.assertTrue(got["ok"], got)


class Rejects(LaunchBase):
    def test_refuses_when_something_already_answers_on_the_port(self):
        """不抢别人的端口 —— 那个实例可能是用户自己或 Desktop 开的。"""
        self.client.running_flag = True          # 端口上有东西
        with self.assertRaises(launch.LaunchError) as ctx:
            self.launcher.start()
        self.assertIn("不抢它的端口", str(ctx.exception))

    def test_refuses_while_there_is_a_200_but_not_a_2xx_question(self):
        """「没答好」（unclear）也算有人在 —— 别往上叠。"""
        self.client.unclear = True
        with self.assertRaises(launch.LaunchError):
            self.launcher.start()

    def test_missing_interpreter_is_caught_before_spawning(self):
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        os.unlink(exe)
        with self.assertRaises(launch.LaunchError) as ctx:
            self.launcher.start()
        self.assertIn("找不到 ComfyUI 的解释器", str(ctx.exception))

    def test_missing_main_py_is_caught_before_spawning(self):
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        os.unlink(os.path.join(root, "main.py"))
        with self.assertRaises(launch.LaunchError) as ctx:
            self.launcher.start()
        self.assertIn("main.py", str(ctx.exception))

    def test_missing_model_paths_yaml_is_caught_before_spawning(self):
        """没有那个 yaml，四个模型在后端里不可见 —— 起来也是白起。"""
        root, exe, _ = self.fake_root(yaml=False)
        self.patch_env(root, exe, None)
        with self.assertRaises(launch.LaunchError) as ctx:
            self.launcher.start()
        self.assertIn("模型路径配置", str(ctx.exception))

    def test_double_click_is_refused_while_still_starting(self):
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        self.spawn(None, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            # probe 返回 None（没答好）→ 不触发「端口被占」，但也不会就绪
            self.launcher.start()
            with self.assertRaises(launch.LaunchError) as ctx:
                self.launcher.start()
        self.assertIn("已经在启动中", str(ctx.exception))


class CommandShape(LaunchBase):
    def test_argv_is_built_element_by_element(self):
        """**一个参数一个列表元素**，绝不过 shell。

        yaml 路径里含 `Comfy Desktop`（一个空格）。拼成字符串再拆，
        或者交给 shell，都会在那个空格上断成两截 —— 这正是不能用
        `launch_comfyui` 的原因（它的 `--launch-extras` 按空格切分）。
        """
        root, exe, ypath = self.fake_root()
        argv = self.patch_env(root, exe, ypath)
        self.assertIn(" ", ypath, "这个桩必须带空格，否则测不出东西")
        holder = self.spawn(argv, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
        got = holder["argv"]
        self.assertEqual(got, argv)
        # yaml 路径是**一个**元素，没有被空格切开
        i = got.index("--extra-model-paths-config")
        self.assertEqual(got[i + 1], ypath)
        self.assertEqual(got[i + 1].count(" "), 1)

    def test_no_shell_is_used(self):
        root, exe, ypath = self.fake_root()
        argv = self.patch_env(root, exe, ypath)
        holder = self.spawn(argv, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
        self.assertNotIn("shell", holder["kw"])
        self.assertFalse(holder["kw"].get("shell"))

    def test_cwd_is_the_comfyui_root(self):
        root, exe, ypath = self.fake_root()
        argv = self.patch_env(root, exe, ypath)
        holder = self.spawn(argv, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
        self.assertEqual(holder["kw"]["cwd"], root)

    def test_probe_env_vars_are_passed(self):
        """探针三个变量是 spec §6.6 要求的，而且没它就没有逐节点耗时。"""
        root, exe, ypath = self.fake_root()
        argv = self.patch_env(root, exe, ypath)
        holder = self.spawn(argv, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
        env = holder["kw"]["env"]
        self.assertEqual(env["COMFY_NODE_PROBE"], "1")
        self.assertTrue(env["COMFY_NODE_PROBE_OUT"].endswith("report.json"))
        self.assertTrue(env["COMFY_NODE_PROBE_STATE"].endswith("probe_state.json"))

    def test_backend_env_is_passed(self):
        """★ `HF_ENDPOINT` 必须在子进程环境里。

        不加**没有症状**：页面会说「后端在跑」，然后 See-through 那条链路在插件里
        写死的 `frankjoshua/juggernautXL…` 上退避 5 次之后整次跑死，
        报一个看不懂的错（那台机器连不上 huggingface.co，实测 15 秒超时黑洞）。
        """
        root, exe, ypath = self.fake_root()
        argv = self.patch_env(root, exe, ypath)
        holder = self.spawn(argv, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
        env = holder["kw"]["env"]
        for key, value in config.BACKEND_ENV.items():
            with self.subTest(key=key):
                self.assertEqual(env[key], value)
        self.assertEqual(env["HF_ENDPOINT"], "https://hf-mirror.com")

    def test_log_is_a_file_not_a_pipe(self):
        """管道有 64 KB 缓冲，没人读就**把子进程卡在 write 上**（冒烟踩过）。

        这里同时确认：stdout 指的是那个日志文件，不是 PIPE。
        """
        root, exe, ypath = self.fake_root()
        argv = self.patch_env(root, exe, ypath)
        holder = self.spawn(argv, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
        kw = holder["kw"]
        self.assertNotEqual(kw.get("stdout"), launch.subprocess.PIPE)
        self.assertEqual(kw["stderr"], launch.subprocess.STDOUT)
        self.assertTrue(os.path.exists(self.log))

    def test_windows_uses_a_detached_new_process_group(self):
        root, exe, ypath = self.fake_root()
        argv = self.patch_env(root, exe, ypath)
        holder = self.spawn(argv, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
        if os.name != "nt":
            self.skipTest("只在 Windows 上验 creationflags")
        flags = holder["kw"]["creationflags"]
        self.assertTrue(flags & launch.subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & launch.subprocess.CREATE_NEW_PROCESS_GROUP)


class WriteProbe(support.LeakCheckedTestCase):
    """探测只有一个判据：**真的建一个文件再删掉**。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-probe-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_writable_dir_says_yes_and_leaves_nothing_behind(self):
        okay, why = launch.write_probe(self.tmp)
        self.assertTrue(okay, why)
        self.assertIsNone(why)
        self.assertEqual(os.listdir(self.tmp), [], "探测文件用完要删掉")

    def test_it_creates_the_directory_it_was_asked_about(self):
        target = os.path.join(self.tmp, "a", "b")
        self.assertTrue(launch.write_probe(target)[0])
        self.assertTrue(os.path.isdir(target))

    def test_a_file_where_a_directory_should_be_says_no(self):
        """Windows 上 chmod 不给结论，所以用「那儿是个文件」造出必然的失败。"""
        path = os.path.join(self.tmp, "not-a-dir")
        open(path, "w").close()
        okay, why = launch.write_probe(path)
        self.assertFalse(okay)
        self.assertTrue(why)

    def test_no_directory_at_all_says_no(self):
        self.assertFalse(launch.write_probe("")[0])


class ConfinedLayout(LaunchBase):
    """受限环境里的落点决策。**探测说了算，说的与做的必须一致。**

    2026-10-01 的现场：工作台自己跑在一个只能写工作区的环境里（实测 Low 完整性、
    子进程继承），它派生的 ComfyUI 于是在 `import torch` 就死。这一组守三件事：

    1. **不漏** —— 探到写不了就把落点搬走，argv 与环境都得真的改；
    2. **不乱改** —— 探得到就一个开关都不加，否则所有正常机器都被牵连；
    3. **搬不动就别起** —— 当场说清楚，而不是再递一段 `import torch` 的堆栈。
    """

    def setUp(self):
        super().setUp()
        self.writable = os.path.join(self.tmp, "writable")
        self.bad_temp = os.path.join(self.tmp, "bad-temp")
        self.bad_root = os.path.join(self.tmp, "bad-root")

    def decide(self, bad_temp=False, bad_install=False):
        """按需要把两处探成「写不了」，返回 `(方案, 探测器)`。"""
        denied = set()
        if bad_temp:
            denied.add(self.bad_temp)
        if bad_install:
            denied.add(self.bad_root)
        probe = denying_probe(denied)
        layout = launch.choose_layout(
            temp_dir=self.bad_temp if bad_temp else self.writable,
            comfy_root=self.bad_root if bad_install else self.writable,
            probe=probe)
        return layout, probe

    def confined_launcher(self, temp_denied=False, install_denied=False,
                          extra_denied=None):
        """假 root + 打桩过的 config + 一个按需要说「写不了」的 Launcher。"""
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        denied = set(extra_denied or ())
        if temp_denied:
            denied.add(launch.env_temp_dir())
        if install_denied:
            denied.add(root)
        launcher = launch.Launcher(
            self.client, log_path=self.log, ready_timeout=2.0,
            probe=denying_probe(denied))
        return root, launcher

    def test_a_writable_machine_gets_no_overrides_at_all(self):
        """**大多数机器**的情形：一个开关、一个环境变量都不该多出来。"""
        layout, _probe = self.decide()
        self.assertEqual(layout["mode"], "normal")
        self.assertEqual(layout["flags"], [])
        self.assertEqual(layout["env"], {})
        self.assertEqual(layout["note"], "")
        self.assertTrue(all(p["ok"] for p in layout["probes"]))

    def test_unwritable_temp_moves_only_the_temp_dir(self):
        layout, _probe = self.decide(bad_temp=True)
        self.assertEqual(layout["mode"], "confined")
        self.assertEqual([f for f in layout["flags"] if f.startswith("--temp")],
                         ["--temp-directory"])
        self.assertNotIn("--user-directory", layout["flags"])
        # 三个变量都设：tempfile 按 TMPDIR → TEMP → TMP 找，少一个就少一条路
        self.assertEqual(set(layout["env"]),
                         {"TMPDIR", "TEMP", "TMP", "TORCHINDUCTOR_CACHE_DIR"})
        for name in launch.TEMP_ENV_ORDER:
            self.assertEqual(layout["env"][name], config.confined_layout()["tmp"])
        self.assertEqual(layout["env"]["TORCHINDUCTOR_CACHE_DIR"],
                         config.confined_layout()["torch"])
        self.assertEqual(list(layout["paths"]), ["临时目录"])

    def test_unwritable_install_moves_user_and_input(self):
        """`user/` 写不了，**同一层**的 `input/` 也一定写不了 —— 一起搬。"""
        layout, _probe = self.decide(bad_install=True)
        self.assertEqual(layout["mode"], "confined")
        self.assertIn("--user-directory", layout["flags"])
        self.assertIn("--input-directory", layout["flags"])
        self.assertNotIn("--temp-directory", layout["flags"])
        self.assertEqual(layout["env"], {}, "临时目录能写就别动它")
        self.assertEqual(sorted(layout["paths"]), ["用户目录", "输入目录"])

    def test_both_unwritable_moves_everything(self):
        layout, _probe = self.decide(bad_temp=True, bad_install=True)
        for flag in ("--temp-directory", "--user-directory", "--input-directory"):
            self.assertIn(flag, layout["flags"])
        for value in layout["paths"].values():
            self.assertTrue(value.startswith(config.BACKEND_CACHE), value)

    def test_the_probe_result_is_reported_not_guessed(self):
        """页面要能说出「哪一处、探的是哪儿、为什么写不了」。"""
        layout, _probe = self.decide(bad_temp=True)
        temp = layout["probes"][0]
        self.assertEqual(temp["path"], self.bad_temp)
        self.assertFalse(temp["ok"])
        self.assertEqual(temp["reason"], "Permission denied")
        self.assertTrue(layout["probes"][1]["ok"])
        self.assertIn("受限模式", layout["note"])
        self.assertIn("模型下载与节点安装", layout["note"])

    def test_the_temp_probe_does_not_ask_tempfile_gettempdir(self):
        """**踩过的坑**：`tempfile.gettempdir()` 的候选表最后一项是 `os.getcwd()`。

        工作台的 cwd 是仓库目录（可写）、子进程的 cwd 是 ComfyUI 安装目录（不可写），
        所以照它探会得出「临时目录没问题」，而子进程照样死于
        `No usable temporary directory`（实测：受限解释器里 gettempdir() 返回工作区）。
        """
        probe = denying_probe({self.bad_temp})
        with mock.patch.dict(os.environ, {"TMPDIR": "", "TEMP": self.bad_temp, "TMP": ""}), \
                mock.patch.object(tempfile, "gettempdir", lambda: self.writable):
            layout = launch.choose_layout(comfy_root=self.writable, probe=probe)
        self.assertEqual(layout["mode"], "confined")
        self.assertEqual(layout["probes"][0]["path"], self.bad_temp)

    def test_the_spawned_command_carries_the_overrides(self):
        _root, launcher = self.confined_launcher(temp_denied=True, install_denied=True)
        holder = self.spawn(None, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            launcher.start()
        argv = holder["argv"]
        for flag in ("--temp-directory", "--user-directory", "--input-directory"):
            self.assertIn(flag, argv)
            value = argv[argv.index(flag) + 1]
            # **绝对路径**：相对路径会被 ComfyUI 按自己的 cwd（安装目录）解析，
            # 而那正是写不进去的地方。
            self.assertTrue(os.path.isabs(value), value)
            self.assertTrue(value.startswith(config.BACKEND_CACHE), value)

    def test_the_command_we_hand_the_user_is_not_touched(self):
        """给用户复制的那条命令保持铁律 1 原样。

        他自己在 PowerShell 里跑，**不受**这个限制，该用正常的落点；搬落点只是
        子进程的事。两边悄悄分家过一次（文档说写工作区、页面却递出另一条），
        所以这里两个方向都钉住。
        """
        display = config.start_command()["display"]
        self.assertNotIn("--user-directory", display)
        self.assertNotIn("--temp-directory", display)
        self.assertIn("--output-directory", display)

    def test_the_subprocess_environment_gets_the_overrides(self):
        _root, launcher = self.confined_launcher(temp_denied=True)
        holder = self.spawn(None, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            launcher.start()
        env = holder["kw"]["env"]
        self.assertEqual(env["TEMP"], config.confined_layout()["tmp"])
        self.assertEqual(env["TMP"], env["TEMP"])
        self.assertEqual(env["TMPDIR"], env["TEMP"])
        self.assertEqual(env["TORCHINDUCTOR_CACHE_DIR"],
                         config.confined_layout()["torch"])

    def test_a_normal_machine_leaves_the_environment_alone(self):
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        launcher = launch.Launcher(self.client, log_path=self.log,
                                   ready_timeout=2.0, probe=ok_probe)
        holder = self.spawn(None, proc=FakeProc())
        with mock.patch.object(self.client, "probe", lambda: False):
            launcher.start()
        env = holder["kw"]["env"]
        self.assertEqual(env.get("TEMP"), os.environ.get("TEMP"))
        self.assertNotIn("TORCHINDUCTOR_CACHE_DIR", env,
                         "不受限的机器不该多出这个变量")
        self.assertNotIn("--user-directory", holder["argv"])

    def test_the_probe_runs_once_not_on_every_request(self):
        """`/api/backend` 每次轮询都会带上落点 —— 但那是**读**，不该反复建删文件。"""
        _root, launcher = self.confined_launcher(temp_denied=True)
        first = launcher.layout()
        launcher.status()
        self.assertIs(launcher.layout(), first)
        self.assertEqual(len(launcher._probe.calls), 2, launcher._probe.calls)

    def test_it_refuses_to_spawn_when_the_layout_itself_cannot_be_written(self):
        """连工作区都写不了：**一个字都不该起**，而要说清为什么。"""
        _root, launcher = self.confined_launcher(
            temp_denied=True, install_denied=True,
            extra_denied=set(config.confined_layout().values()))
        captured = []
        self.spawn(None, proc=FakeProc(), captured=captured)
        with mock.patch.object(self.client, "probe", lambda: False):
            with self.assertRaises(launch.LaunchError) as ctx:
                launcher.start()
        self.assertIn("只能写工作区", str(ctx.exception))
        self.assertEqual(captured, [], "注定起不来的进程不要起")

    def test_the_layout_is_part_of_the_status_the_page_reads(self):
        """页面要在**点之前**就知道这次会以什么方式起。"""
        _root, launcher = self.confined_launcher(temp_denied=True)
        st = launcher.status()
        self.assertEqual(st["layout"]["mode"], "confined")
        self.assertTrue(st["layout"]["note"])


class Outcomes(LaunchBase):
    def ready_probe(self, after=0, state=None):
        """探活替身：按调用次数决定什么时候变成「就绪」。"""
        holder = {"n": 0}

        def probe():
            holder["n"] += 1
            if state is not None:
                return state
            return True if holder["n"] > after else False
        return probe

    def wait_done(self, timeout=6.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.launcher.status()
            if st["phase"] in ("ready", "failed", "exited"):
                return st
            time.sleep(0.05)
        return self.launcher.status()

    def test_ready_is_detected_and_reported_with_timing(self):
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        self.spawn(None, proc=FakeProc())
        with mock.patch.object(self.client, "probe", self.ready_probe(after=1)):
            self.launcher.start()
            st = self.wait_done()
        self.assertEqual(st["phase"], "ready")
        self.assertTrue(st["ready"])
        self.assertIsNotNone(st["ready_ms"])
        self.assertTrue(any(e["type"] == "backend_ready" for e in self.events))

    def test_early_exit_reports_the_code_and_the_log(self):
        """最常见的失败：进程起来又立刻退了（端口被占 / yaml 不对）。

        这时**必须**把退出码和日志尾部给出来，否则用户只能看到一个 pid。
        """
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("Traceback (most recent call last):\n")
            f.write("OSError: [Errno 10048] address already in use\n")
        self.spawn(None, proc=FakeProc(exit_code=1))
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
            st = self.wait_done()
        self.assertEqual(st["phase"], "exited")
        self.assertEqual(st["exit_code"], 1)
        # 日志里认得出原因时**要说原因**，不是给一句泛泛的「进程退出了」——
        # 第一版就是泛泛的那种，把实测撞到的「没有可写临时目录」指成了「端口被占」。
        self.assertIn("端口被占", st["error"])
        self.assertTrue(st["error_short"], "要有一句能进状态行的短话")
        self.assertTrue(any("address already in use" in ln
                            for ln in st["log_tail"]), st["log_tail"])
        failed = [e for e in self.events if e["type"] == "backend_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["exit_code"], 1)

    def test_timeout_says_so_and_still_hands_over_the_log(self):
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("loading custom nodes...\n")
        self.spawn(None, proc=FakeProc(exit_code=None))     # 一直活着
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
            st = self.wait_done(timeout=8.0)
        self.assertEqual(st["phase"], "failed")
        self.assertIn("还没就绪", st["error"])
        self.assertIn("loading custom nodes...", st["log_tail"])

    def test_log_lines_are_pushed_as_events_while_starting(self):
        """日志经 SSE 回显 —— **边起边推**，不是等结束了才一起给。

        所以这里在启动**过程当中**往日志里追加内容，再看它有没有被推出来。
        （启动前就写在文件里的那些属于上一次启动，按设计不推 —— 那是
        `/api/backend/log` 的活。）
        """
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        self.spawn(None, proc=FakeProc(exit_code=None))
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
            time.sleep(0.3)
            with open(self.log, "a", encoding="utf-8") as f:
                f.write("loading custom nodes\n")
            time.sleep(1.5)
            with open(self.log, "a", encoding="utf-8") as f:
                f.write("importing node_probe\n")
            self.wait_done(timeout=8.0)
        logs = [e for e in self.events if e["type"] == "backend_log"]
        self.assertTrue(logs, self.events)
        flat = [ln for e in logs for ln in e["lines"]]
        self.assertIn("loading custom nodes", flat)
        self.assertIn("importing node_probe", flat)
        # 第一段是**在**就绪之前被推出来的（不是攒到最后）
        first_at = None
        for i, e in enumerate(self.events):
            if e["type"] == "backend_log" and "loading custom nodes" in e["lines"]:
                first_at = i
                break
        self.assertIsNotNone(first_at)
        self.assertNotIn("backend_ready", [x["type"] for x in self.events[:first_at]])

    def test_log_pump_only_pushes_what_this_launch_produced(self):
        """推给别人看的只有**这一次**启动之后的新行。

        上一次启动留下的那些**不重推**（否则每秒把历史重发一遍），
        但它们仍然能从 `/api/backend/log` 读到 —— 那条读的是整份尾部。
        这两个行为必须分开，否则要么刷屏要么看不到上次为什么失败。
        """
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("from the previous launch\n")
        self.spawn(None, proc=FakeProc(exit_code=None))
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
            time.sleep(1.6)                      # 至少跑两轮 pump
            with open(self.log, "a", encoding="utf-8") as f:
                f.write("from this launch\n")
            time.sleep(1.6)
            self.launcher.cancel()

        pushed = [ln for e in self.events if e["type"] == "backend_log"
                  for ln in e["lines"]]
        self.assertEqual(pushed.count("from this launch"), 1, pushed)
        self.assertEqual(pushed.count("from the previous launch"), 0, pushed)
        # 历史没丢 —— 只是不经事件推，改由 /api/backend/log 提供
        tail = launch.read_tail(self.log)
        self.assertIn("from the previous launch", tail)
        self.assertIn("from this launch", tail)

    def test_each_launch_appends_a_separator_instead_of_wiping_the_log(self):
        """起一次失败、改点东西再起一次是常态 —— 上一次那段**必须**留着。"""
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("WHY THE FIRST ATTEMPT FAILED\n")
        self.spawn(None, proc=FakeProc(exit_code=None))
        with mock.patch.object(self.client, "probe", lambda: False):
            self.launcher.start()
            self.launcher.cancel()
        tail = launch.read_tail(self.log)
        self.assertIn("WHY THE FIRST ATTEMPT FAILED", tail)
        self.assertTrue(any(ln.startswith("=====") for ln in tail), tail)


class ExplainFailure(support.LeakCheckedTestCase):
    """起不来时给的那句话，必须**照着日志**说，而不是照着猜测说。

    第一版一律说「多半是端口被占或 yaml 不对」，而实测撞上的是
    「没有可写的临时目录」—— 那句话把人指去了完全错误的方向。
    """

    TEMP_TAIL = ["  File \"torch/_inductor/runtime/cache_dir_utils.py\", line 25",
                 "    tempfile.gettempdir() if not is_fbcode() else \"/var/tmp\",",
                 "FileNotFoundError: [Errno 2] No usable temporary directory found in",
                 "['C:\\\\Temp', 'C:\\\\Windows\\\\Temp']"]

    def test_temp_dir_signature_is_recognised(self):
        short, detail = launch.explain_failure(self.TEMP_TAIL, exit_code=1)
        self.assertIn("临时目录", short)
        self.assertIn("临时目录", detail)
        # 办法必须是**能照做**的：去你自己的 PowerShell 里跑那条命令
        self.assertIn("PowerShell", detail)
        # 而「双击 run.bat」只能作为**别做**出现 —— 它起的是工作台，不是后端。
        # 第一版正相反（把它当成办法），实测照做只会原地打转。
        self.assertIn("不要", detail)
        self.assertNotIn("自己双击", detail)
        self.assertNotIn("别的程序", detail)

    def test_the_temp_advice_depends_on_whether_we_already_moved_the_layout(self):
        """两种情形**指的路不一样**，因为事实不一样。

        受限模式已经搬过落点还写不进去 = 连工作区都写不了，那是另一回事。
        """
        _short, plain = launch.explain_failure(self.TEMP_TAIL, layout={"mode": "normal"})
        self.assertIn("候选目录", plain)
        self.assertNotIn("已经", plain)
        self.assertNotIn("继承", plain)          # 旧文案里那句错的因果
        _short, confined = launch.explain_failure(self.TEMP_TAIL,
                                                  layout={"mode": "confined"})
        self.assertIn("已经", confined)
        self.assertIn("救不回来", confined)

    def test_port_in_use_signature(self):
        tail = ["OSError: [Errno 10048] error while attempting to bind on address"]
        _short, detail = launch.explain_failure(tail)
        self.assertIn("端口被占", detail)

    def test_winerror_10048_also_counts(self):
        _short, detail = launch.explain_failure(["WinError 10048"])
        self.assertIn("端口被占", detail)

    def test_unknown_failure_does_not_invent_a_cause(self):
        """认不出来就**说认不出来**，别硬套一个最像的。"""
        tail = ["something nobody has seen before"]
        short, detail = launch.explain_failure(tail, exit_code=7)
        self.assertIn("退出码 7", detail)
        self.assertIn("第一段 Traceback", detail)
        self.assertNotIn("端口被占", detail)
        self.assertNotIn("临时目录", detail)

    def test_unknown_failure_still_points_at_the_log(self):
        _short, detail = launch.explain_failure([], exit_code=1)
        self.assertIn("日志", detail)

    def test_exit_code_is_optional(self):
        short, _detail = launch.explain_failure([])
        self.assertIn("退出了", short)


class StopOnlyTouchesOurOwnProcess(LaunchBase):
    """停后端（P20i）：这是 `cancel()` 的反面 —— 那个只打断等待，这个真要进程走。

    红线与 `start()` 那条是同一件事的两面：起的时候不抢别人的端口，停的时候
    也不杀认不出的进程。**认不出的两种情况都要说清怎么办** —— 「点了没反应」
    是本工作区最忌讳的那种错。
    """

    def start_one(self, proc=None):
        """真的走一遍 start()，让 launcher 手里有一个「自己起的」进程。"""
        root, exe, ypath = self.fake_root()
        self.patch_env(root, exe, ypath)
        proc = proc or FakeProc()
        self.spawn(None, proc=proc)
        self.launcher.start()
        return proc

    def test_it_refuses_when_it_never_started_anything(self):
        with self.assertRaises(launch.LaunchError) as cm:
            self.launcher.stop()
        self.assertIn("不是工作台这次起的", str(cm.exception))
        # 拒绝要给出路：光说「不行」等于没说
        self.assertIn("任务管理器", str(cm.exception))

    def test_it_stops_the_one_it_started(self):
        proc = self.start_one()
        state = self.launcher.stop()
        self.assertTrue(proc.terminated, "自己起的那个要真的收到终止信号")
        self.assertEqual(state["phase"], "stopped")
        self.assertIsNotNone(state["stopped_ms"])
        # 页面那颗开关按 `process_alive` 画；停完必须是 False
        st = self.launcher.status(liveness="down")
        self.assertFalse(st["process_alive"])
        self.assertFalse(st["ready"])
        # **不是**「启动失败」：这两件事在页面上的处置完全相反（一个是灰、一个是红）
        self.assertNotEqual(st["phase"], "exited")
        self.assertIsNone(st["error"])

    def test_the_waiting_thread_does_not_call_our_stop_a_startup_failure(self):
        """停掉之后，那个还在轮询「就绪」的线程**不许**报「启动失败」。

        它就是为这件事存在的：`_wait_ready` 看到进程没了会走「早退」那条路
        （报错 + 推 `backend_failed`），而进程明明是我们自己停的。
        不拦的话，用户点一次「关」，页面上得到的是一条红色的「后端启动失败」。
        """
        self.start_one()
        self.launcher.stop()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if any(e["type"] == "backend_failed" for e in self.events):
                self.fail("自己停掉的那次被报成了启动失败：%s" % self.events)
            time.sleep(0.05)
        self.assertEqual(self.launcher.status(liveness="down")["phase"], "stopped")

    def test_a_process_we_cannot_identify_is_not_touched(self):
        """工作台重启过的那种：句柄没了，但启动记录还在 —— 认不出就不杀。"""
        proc = self.start_one()
        self.launcher._proc = None          # 模拟「本进程没有那个句柄」
        with self.assertRaises(launch.LaunchError) as cm:
            self.launcher.stop()
        self.assertFalse(proc.terminated, "认不出的进程一个都不许碰")
        self.assertIn("认不出", str(cm.exception))

    def test_a_process_that_will_not_die_is_reported_not_faked(self):
        """收到信号也不退：如实说没停掉，**不许**当成功。

        假成功的后果是那颗开关画成灰的（= 已停），而端口上还挂着一个后端 ——
        下一次点「开」会撞端口被拒，用户只会看到「点不动」。
        """
        proc = self.start_one(proc=FakeProc(stubborn=True))
        with self.assertRaises(launch.LaunchError) as cm:
            self.launcher.stop()
        self.assertIn("还没退出", str(cm.exception))
        st = self.launcher.status(liveness="ok")
        self.assertTrue(st["process_alive"], "没停掉就必须还说它活着")
        self.assertNotEqual(st["phase"], "stopped")

    def test_a_finished_process_is_not_mine_to_stop_either(self):
        """自己起过，但它已经退出了 —— 这时候也没有可停的东西。"""
        proc = self.start_one(proc=FakeProc(exit_code=1))
        time.sleep(0.2)
        with self.assertRaises(launch.LaunchError):
            self.launcher.stop()
        self.assertFalse(proc.terminated)


class Status(LaunchBase):
    def test_before_any_launch_it_says_so_plainly(self):
        st = self.launcher.status()
        self.assertIsNone(st["launch_id"])
        self.assertEqual(st["phase"], "idle")
        self.assertFalse(st["process_alive"])

    def test_readiness_always_comes_from_the_backend_not_our_memory(self):
        """后端可能是用户自己开的，也可能工作台重启过 —— 就绪看后端本身。"""
        st = self.launcher.status()
        self.assertFalse(st["backend_ok"])
        self.client.running_flag = True
        st = self.launcher.status()
        self.assertTrue(st["backend_ok"])
        self.assertTrue(st["backend_up"])

    def test_unclear_backend_counts_as_up_but_not_ok(self):
        self.client.unclear = True
        st = self.launcher.status()
        self.assertTrue(st["backend_up"])
        self.assertFalse(st["backend_ok"])

    def test_status_carries_the_ready_timeout_so_the_ui_can_say_it(self):
        self.assertEqual(self.launcher.status()["ready_timeout_s"], 2)


class LogTail(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-tail-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_file_is_an_empty_list_not_an_error(self):
        self.assertEqual(launch.read_tail(os.path.join(self.tmp, "nope.log")), [])

    def test_reads_the_last_lines(self):
        p = os.path.join(self.tmp, "a.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("".join("line %d\n" % i for i in range(500)))
        got = launch.read_tail(p, max_bytes=200)
        self.assertTrue(got)
        self.assertLess(len(got), 500)
        self.assertEqual(got[-1], "line 499")
        # 从中间截起时第一行可能是半截 —— 必须丢掉
        self.assertTrue(all(ln.startswith("line ") for ln in got), got[:3])

    def test_only_reads_the_tail_of_a_big_file(self):
        """日志会一直长；整读是 O(大小)，而这里每次请求都要跑。"""
        p = os.path.join(self.tmp, "big.log")
        with open(p, "wb") as f:
            f.write(b"x" * (5 * 1024 * 1024))
            f.write(b"\nTHE END\n")
        got = launch.read_tail(p, max_bytes=4096)
        self.assertIn("THE END", got[-1])

    def test_non_utf8_bytes_do_not_explode(self):
        """ComfyUI 的日志里什么字节都可能有（Windows 控制台编码混着来）。"""
        p = os.path.join(self.tmp, "weird.log")
        with open(p, "wb") as f:
            f.write(b"ok line\n\xff\xfe garbage\nlast\n")
        got = launch.read_tail(p)
        self.assertEqual(got[-1], "last")

    def test_ready_line_finds_the_marker(self):
        lines = ["booting", "To see the GUI go to: http://127.0.0.1:8188", "x"]
        self.assertIn("To see the GUI", launch.readiness_line(lines))

    def test_ready_line_is_none_when_absent(self):
        self.assertIsNone(launch.readiness_line(["still loading"]))
        self.assertIsNone(launch.readiness_line([]))


if __name__ == "__main__":
    unittest.main()
