#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基准图的落点规则：**环境变量 → 仓库自带的 `workflows/` → 工作区**。

为什么这值得一条判据：三张基准图分别被三个模块在**导入时**读走，而选错一份的表现是
**构建出另一张图** —— 不报错、不崩溃、耗时正常。

「仓库自带的那份」排在第二位而不是最后，正是让 `git clone` 下来就能用的那一环：
克隆下来的人没有「工作区」（那是原作者机器上的布局），把仓库自带那份排在后面，
他会拿到一句「缺少基准 API 图」，看起来像仓库漏了文件。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import project_paths  # noqa: E402

NAME = "qwen21_api_base.json"
ENV = "QWEN21_BASE"


class Precedence(unittest.TestCase):
    """三条来源各命中一次，外加「环境变量给了就不回落」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbpaths-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.workspace = os.path.join(self.tmp, "workspace")
        self.bundled = os.path.join(self.repo, project_paths.WORKFLOWS_DIRNAME)
        os.makedirs(self.bundled)
        os.makedirs(self.workspace)
        # 环境变量要**真的不在**，否则判的是「跑测试那台机器的环境」。
        saved = os.environ.pop(ENV, None)
        self.addCleanup(lambda: (os.environ.__setitem__(ENV, saved) if saved
                                 is not None else os.environ.pop(ENV, None)))

    def touch(self, *parts):
        path = os.path.join(*parts)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        return path

    def ask(self):
        return project_paths.base_file(ENV, NAME, repo=self.repo,
                                       workspace=self.workspace)

    def test_the_bundled_copy_wins_over_the_workspace(self):
        bundled = self.touch(self.bundled, NAME)
        self.touch(self.workspace, NAME)
        self.assertEqual(self.ask(), bundled)

    def test_the_workspace_is_the_fallback(self):
        """仓库里没有自带那份时，回落到工作区 —— 旧的现场布局不能因此失效。"""
        ws = self.touch(self.workspace, NAME)
        self.assertEqual(self.ask(), ws)

    def test_the_env_var_beats_the_bundled_copy(self):
        self.touch(self.bundled, NAME)
        self.touch(self.workspace, NAME)
        os.environ[ENV] = os.path.join(self.tmp, "explicit.json")
        self.assertEqual(self.ask(), os.environ[ENV])

    def test_a_wrong_env_path_is_not_silently_replaced(self):
        """给了环境变量就以它为准 —— **不做存在性检查、不悄悄回落**。

        回落的后果是「我指了另一张图，它却构建了这张」：出图能出、耗时正常，
        只是图和你要的不是同一张。
        """
        self.touch(self.bundled, NAME)
        missing = os.path.join(self.tmp, "nope.json")
        os.environ[ENV] = missing
        self.assertEqual(self.ask(), missing)
        self.assertFalse(os.path.exists(missing))

    def test_nothing_anywhere_still_returns_a_predictable_path(self):
        """哪个来源都没有时返回工作区那个路径（由调用方去报「缺少基准 API 图」）。"""
        self.assertEqual(self.ask(), os.path.join(self.workspace, NAME))


class TheThreeModulesAskTheSameRule(unittest.TestCase):
    """三个模块**必须**都去问 `project_paths`，不许各自再拼一份路径。

    它们是刻意平级的（互不 import），所以「各写一份」是最自然的写法 —— 也正因此
    才需要在文本上守一道：两份规则一旦分家，同一个仓库里三条链路会去两个地方找图。
    """

    SOURCES = ("qwen21_build.py", "h3_build.py", "layers_build.py")

    def read(self, name):
        with open(os.path.join(REPO, name), encoding="utf-8") as handle:
            return handle.read()

    def test_each_module_calls_the_shared_rule(self):
        for name in self.SOURCES:
            with self.subTest(module=name):
                self.assertIn("project_paths.base_file(", self.read(name))

    def test_none_of_them_builds_its_own_path_again(self):
        for name in self.SOURCES:
            with self.subTest(module=name):
                src = self.read(name)
                self.assertNotIn('os.path.join(WORKSPACE, "', src)
                self.assertNotIn('os.path.join(WORKSPACE, \'', src)

    def test_the_bundled_dir_name_is_the_one_the_publisher_copies_into(self):
        """发布脚本要往这个目录名里放图 —— 两边必须是同一个字符串。"""
        self.assertEqual(project_paths.WORKFLOWS_DIRNAME, "workflows")


if __name__ == "__main__":
    unittest.main()
