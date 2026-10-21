"""盯住测试设施本身：临时目录泄漏检查器必须**真的在跑**。

为什么值得单独一个文件 —— 这个检查器前后**三次**都是沉默失效：

1. 把 `def stopTest(self)` 写在 TestCase 上。`TestCase` **没有**这个方法
   （那是 TestResult 的），于是它是个永远不会被调用的死方法。
2. 覆盖 `TestCase.run()` 去读 `self._outcome`。它只在 `run()` **内部**才被设置，
   在外面读永远是 `None`，于是每次都走「拿不到内部状态」的退路。
3. 把检查写在 `tearDown` 里。`addCleanup` 注册的清理跑在 `tearDown` **之后**，
   于是每一条都被误报成泄漏。

三次都是同一类错误：**钩子的时点与存在性没被验证过**。而三次的表象都一样 ——
测试全绿、防护是摆设。所以这里用反面控制把它钉死：故意在仓库根造一个不清理的
临时目录，**要求检查器报 FAIL**。这条测试再失效，我们就不会再看到一片绿。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import support  # noqa: E402  (测试基类 / 临时目录)

from workbench import config  # noqa: E402

# 跑在**子进程**里的反面控制：故意在仓库根建临时目录且不清理。
# 用子进程而不是同进程，是因为被测的就是运行器本身，隔离了才看得清。
PROBE = '''
import os, sys, tempfile
sys.path.insert(0, {here!r})
import support, unittest

class Leaky(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()
        self.bad = tempfile.mkdtemp(prefix="qwen21-LEAKPROBE-", dir=support.REPO)
    def test_x(self):
        raise SystemExit(0) if os.environ.get("QWEN21_PROBE_HARD") else None

r = unittest.TextTestRunner(verbosity=0).run(
        unittest.TestLoader().loadTestsFromTestCase(Leaky))
print("PROBE %d %d" % (r.testsRun, len(r.failures)))
'''


def tearDownModule():
    """整套测试跑完，把 scratch 目录整体收掉。

    `LeakCheckedTestCase` 逐条测试时已经在尽力清了，但有些目录在测试刚结束的
    那一刻还被占着（后端日志句柄未释放），那时删不掉。到这里它们都自由了。
    """
    shutil.rmtree(support.TEST_TMP_ROOT, ignore_errors=True)


class LeakCheckSelfTest(unittest.TestCase):
    """反面控制。它失败 = 泄漏检查器已经失效，别信套件的其它绿灯。"""

    def test_leak_in_repo_root_is_caught(self):
        # 探针自己起一个空白解释器，免得污染本进程的 TRACKER 状态。
        #
        # **编码必须两头都钉住**，不能靠 locale 猜（实测踩过）：子进程（没有
        # `-X utf8`）把中文按控制台代码页 GBK 写进管道，而父进程若跑在
        # UTF-8 模式下，`text=True` 会拿 utf-8 去解那几个字节 → 读 stderr 的
        # 线程抛 `UnicodeDecodeError: 0xd6`。症状是套件中段冒出一段看着像
        # 「探针挂了」的 traceback，**而测试照样通过**（它发生在 stderr 上）。
        # 一边给子进程指定 `PYTHONIOENCODING`，一边显式指定解码，两边同一个值。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", PROBE.format(here=HERE)],
            cwd=REPO, capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")

        # 不管成败，先把探针可能留下的目录收掉（它**故意**不清理）
        probes = [os.path.join(REPO, n) for n in os.listdir(REPO)
                  if n.startswith("qwen21-LEAKPROBE-")]
        for p in probes:
            shutil.rmtree(p, ignore_errors=True)

        self.assertRegex(out, r"PROBE \d+ \d+",
                         "反面控制没跑起来：\n" + out)
        ran, failed = (int(x) for x in
                       out.split("PROBE")[1].split()[:2])
        self.assertEqual(ran, 1, "反面控制没执行到测试：\n" + out)
        self.assertEqual(
            failed, 1,
            "泄漏检查器**没有**抓住仓库根里的临时目录残留 —— 钩子又失效了。\n"
            "这正是它前三次栽掉的方式：套件全绿，防护是摆设。\n" + out)
        self.assertTrue(
            probes,
            "探针目录没被造出来，这个反面控制本身不成立（先修探针）")


class RealOutputFixtureSelfTest(support.LeakCheckedTestCase):
    """`support.real_output_pngs()` 必须**跟着落点走**。

    它是「拿真图当夹具」那几条（`test_pngpix` / `test_pngmeta` / `test_build_golden`）
    的查找口。写死安装目录的那一版，在落点搬去工作区之后让整整 **10 条测试静默跳过**：
    套件仍然是绿的，那 10 条却一条都没跑，跳过理由还写着「output/ 里没有 PNG」——
    而磁盘上明明有 28 张，只是换了地方。

    **跳过不是失败**，所以这类失效没法靠「看结果」发现，只能靠一条正面断言钉住：
    往落点里放一张图，助手就必须看得见它。
    """

    def test_workspace_output_dir_is_searched(self):
        tmp = tempfile.mkdtemp(dir=support.scratch_dir(),
                               prefix="qwen21-realfix-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # 只按扩展名收集，不解析内容 —— 空文件就够
        open(os.path.join(tmp, "fake_00001.png"), "wb").close()
        with mock.patch.object(config, "OUTPUT_DIR", tmp):
            found = [os.path.basename(p) for p in support.real_output_pngs()]
        self.assertIn("fake_00001.png", found,
                      "落点里的图没被找到 —— 夹具查找又写死了安装目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
