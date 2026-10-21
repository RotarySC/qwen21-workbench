#!/usr/bin/env python3
"""端到端：真正跑一遍四个 CLI 包装脚本。

`test_build_golden.py` 是直接 `import qwen21_build` 的 —— 它证明不了
**搬家之后四个包装脚本还能找到那个模块**。这个文件补上这一段：每个脚本都用
子进程真跑一次（cwd = 工作区根目录，模拟用户的日常用法），再把产物和重构前的
快照对比。

断言分两层：

* **语义等价**（键排序后逐字相等）—— 硬要求，四个脚本都必须满足。
* **逐字节相同** —— 更强的要求，四个脚本当前都满足。

两层都断言是有意的：语义等价保证功能没变，逐字节相同保证连序列化都没变。
将来若出现"语义等价通过、逐字节不同"，几乎一定是基准图被重新序列化导致键序变化，
不是功能回归 —— 报错信息里会这么提示。

**比的是「快照 + TE-Speed 那个节点」**：快照是重构前旧脚本的产物，而 `build()`
从 2026-10-06 起**默认就接 TE-Speed**，所以两边差的那一个节点要先补出来
（`support.with_default_te_speed`，用的是构建器自己的接线函数）。

`build_refine.py` / `build_2k.py` 会把结果写回工作区里**固定的文件名**，
所以测试先备份、跑完再还原，不把现场弄脏。

    python tests/test_legacy_cli.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))          # …/qwen21-workbench/tests
REPO = os.path.dirname(HERE)                               # …/qwen21-workbench
WORKSPACE = os.path.dirname(REPO)                          # …/ComfyUI 运维现场
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # support.py 就在 tests/ 里
import support  # noqa: E402  (测试基类 / 临时目录)

FIXTURES = os.path.join(HERE, "fixtures")

T2I_PROMPT = "a small brown fox sitting in tall grass"
EDIT_PROMPT = "make the apple bright blue"

# 这两个脚本没有 -o，会写回工作区里的固定文件名
SIDE_EFFECTING = ("qwen21_refine_api.json", "qwen21_fashion_2k_api.json")


def canon(path):
    with open(path, encoding="utf-8") as f:
        return json.dumps(json.load(f), sort_keys=True, ensure_ascii=False)


class LegacyCLIEndToEnd(support.LeakCheckedTestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(os.path.join(WORKSPACE, "qwen21_api_base.json")):
            raise unittest.SkipTest(f"找不到上层运维现场: {WORKSPACE}")
        cls.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-cli-")
        cls.backup = {}
        for name in SIDE_EFFECTING:
            src = os.path.join(WORKSPACE, name)
            if os.path.exists(src):
                dst = os.path.join(cls.tmp, name + ".orig")
                shutil.copy2(src, dst)
                cls.backup[name] = dst

    @classmethod
    def tearDownClass(cls):
        for name, dst in cls.backup.items():
            shutil.copy2(dst, os.path.join(WORKSPACE, name))
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- 工具 -----------------------------------------------------------
    def run_cli(self, *args):
        # 编码**两头都钉住**：这几个 CLI 会打印中文，而子进程默认按控制台代码页
        # （本机 GBK）写管道。父进程要是按 utf-8 解，失败信息就会变成乱码
        # （更坏的情况是 reader 线程直接抛 UnicodeDecodeError —— 见
        # test_support_guards.py 里那段说明）。给子进程指定 PYTHONIOENCODING，
        # 父进程这边显式按 utf-8 解，两边同一个值。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, *args], cwd=WORKSPACE, env=env,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0,
                         f"{args[0]} 退出码 {proc.returncode}\n"
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def assertMatchesFixture(self, produced, fixture_name, label):
        """产出 == **历史快照 + 一个新默认**（TE-Speed 那个节点）。

        快照是**重构之前**的旧脚本产物 —— 那时还没有「TE-Speed 默认接」这回事，
        所以现在要先把那个**已知的差**补上再比。补的方式是调构建器自己的
        `_wire_te_speed`（见 `support.with_default_te_speed`），不在这边手抄一份
        接线。于是这一层守住的仍然是「**其余接线一字未变、连序列化都没变**」；
        TE-Speed 自己的接线由 `test_build_golden.py::TeSpeedWiring` 那一组守。
        """
        with open(os.path.join(FIXTURES, fixture_name), encoding="utf-8") as f:
            want = support.with_default_te_speed(json.load(f))

        self.assertTrue(os.path.exists(produced), f"{label}: 没产出 {produced}")
        with open(produced, encoding="utf-8") as f:
            got = json.load(f)

        # 1) 语义等价
        if (json.dumps(got, sort_keys=True, ensure_ascii=False)
                != json.dumps(want, sort_keys=True, ensure_ascii=False)):
            diffs = [n for n in sorted(set(got) | set(want))
                     if json.dumps(got.get(n), sort_keys=True)
                     != json.dumps(want.get(n), sort_keys=True)]
            self.fail(f"{label} 与快照语义不一致（功能回归）；有差异的节点: {diffs}")

        # 2) 逐字相同（换行按平台归一）
        #    文本模式下 Windows 会把 \n 写成 \r\n —— 那是平台行为，不是「序列化
        #    变了」。期望值是在内存里生成的（只有 \n），所以两边都要走到同一层
        #    归一再比，否则这条判据会因为一个不可移植的差别每次都红。
        want_text = json.dumps(want, ensure_ascii=False, indent=2)
        with open(produced, encoding="utf-8") as f:       # 通用换行：\r\n → \n
            got_text = f.read()
        if got_text != want_text:
            self.fail(
                f"{label} 语义等价但逐字不同（换行已归一）。最可能的原因是 "
                f"qwen21_api_base.json 被重新序列化过（键序变化），"
                f"不是功能回归 —— 但先确认一下再放行。")

    # --- 四个脚本 -------------------------------------------------------
    def test_t2i(self):
        out = os.path.join(self.tmp, "t2i.json")
        self.run_cli("qwen21_run.py", T2I_PROMPT, "--steps", "30",
                     "--seed", "111", "-o", out)
        self.assertMatchesFixture(out, "golden_t2i.json", "qwen21_run.py")

    def test_t2i_sized(self):
        out = os.path.join(self.tmp, "t2i_sized.json")
        self.run_cli("qwen21_run.py", T2I_PROMPT, "--steps", "30", "--seed", "111",
                     "--width", "768", "--height", "512", "-o", out)
        self.assertMatchesFixture(out, "golden_t2i_sized.json",
                                  "qwen21_run.py --width")

    def test_edit(self):
        out = os.path.join(self.tmp, "edit.json")
        self.run_cli("build_edit.py", EDIT_PROMPT, "--src", "ref.png",
                     "--resolution", "1024", "--seed", "7", "--steps", "25",
                     "-o", out)
        self.assertMatchesFixture(out, "golden_edit.json", "build_edit.py")

    def test_refine(self):
        self.run_cli("build_refine.py")
        self.assertMatchesFixture(os.path.join(WORKSPACE, "qwen21_refine_api.json"),
                                  "golden_refine.json", "build_refine.py")

    def test_2k(self):
        self.run_cli("build_2k.py")
        self.assertMatchesFixture(
            os.path.join(WORKSPACE, "qwen21_fashion_2k_api.json"),
            "golden_2k.json", "build_2k.py")

    def test_side_effecting_scripts_leave_same_content(self):
        """跑完 build_refine/build_2k 后，工作区里那两个文件的内容不变。"""
        for name in SIDE_EFFECTING:
            orig = self.backup.get(name)
            if not orig:
                continue
            with self.subTest(file=name):
                self.assertEqual(canon(os.path.join(WORKSPACE, name)), canon(orig))


class ModuleLayout(support.LeakCheckedTestCase):
    def test_module_lives_in_repo(self):
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        import qwen21_build as qb
        self.assertEqual(os.path.dirname(os.path.abspath(qb.__file__)),
                         os.path.abspath(REPO))

    def test_workspace_resolves_to_parent(self):
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        import qwen21_build as qb
        self.assertEqual(os.path.abspath(qb.WORKSPACE), os.path.abspath(WORKSPACE))
        self.assertTrue(os.path.exists(qb.BASE_FILE),
                        f"基准图不存在: {qb.BASE_FILE}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
