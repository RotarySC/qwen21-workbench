"""View switching must preserve form state and coexist with the real SSE handlers."""
import json
import os
import shutil
import subprocess
import unittest

import support

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(HERE), "workbench", "static")


@unittest.skipUnless(shutil.which("node"), "没有 node，无法运行成果视图探针")
class ResultViews(support.LeakCheckedTestCase):
    @classmethod
    def setUpClass(cls):
        # `errors="replace"` 不是可选项：node 往 **stderr** 写的是控制台代码页
        # （本机 GBK）的字节，而这个子进程是按 UTF-8 解的。少了它，读 stderr 的
        # 那个线程会抛 UnicodeDecodeError —— 一行看着像「探针挂了」的 traceback，
        # 而测试其实照样通过（它就发生在 stderr 上）。那种噪声很贵：
        # 下次真的挂掉时，你会先去看这条已经看过三遍的假线索。
        result = subprocess.run(
            [shutil.which("node"), os.path.join(HERE, "views_probe.js"),
             os.path.join(STATIC, "app.js"), os.path.join(STATIC, "index.html")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.data = json.loads(result.stdout)

    def assert_view(self, result, expected):
        self.assertEqual(result["active"], expected)
        self.assertEqual(result["selected"], [expected])
        self.assertEqual(result["visible"], [expected])
        self.assertEqual(result["tabStops"], [expected])

    def test_default_and_click_have_exactly_one_active_panel(self):
        self.assert_view(self.data["initial"], "live")
        self.assert_view(self.data["click"], "gallery")

    def test_switching_retains_form_filters_and_scroll(self):
        self.assertEqual(self.data["retained"], {
            "prompt": "neon skyline", "search": "blue", "librarySearch": "saved",
            "scrollTop": 180, "query": "blue"})

    def test_keyboard_navigation_wraps_and_updates_focus(self):
        for result, expected in zip(self.data["keyboard"][:5], ["live", "jobs", "live", "jobs", "live"]):
            with self.subTest(expected=expected):
                self.assert_view(result, expected)
                self.assertEqual(result["focused"], "tab-" + expected)
                self.assertTrue(result["prevented"])
        self.assertFalse(self.data["keyboard"][-1]["prevented"])

    def test_snapshot_progress_and_completion_do_not_switch_the_view(self):
        self.assert_view(self.data["events"], "gallery")
        self.assertEqual(self.data["events"]["status"], "done")
        self.assertEqual(self.data["events"]["currentId"], "history-job")

    def test_selecting_a_job_opens_live_and_restores_parameters(self):
        self.assert_view(self.data["selectedJob"], "live")
        self.assertEqual(self.data["selectedJob"]["prompt"], "history prompt")
        self.assertEqual(self.data["selectedJob"]["seed"], 42)
        self.assertEqual(self.data["selectedJob"]["focused"], "tab-live")

    def test_gallery_load_keeps_gallery_and_ignores_foreign_parameters(self):
        """从画廊载入：视图不跳走、数值照拿、非本链路的项被拒。

        2026-10-01 起**提示词不再被覆盖**（按用户要求；契约测试见
        `test_api.py::test_loading_params_takes_the_numbers_and_leaves_the_rest_
        alone`，真浏览器里那条在 `page_shot` 第 10b 步）。所以这里等的是上一步
        `selectJob` 留下的 "history prompt"，而**不是**这张图的 "saved prompt" ——
        探针里那个 `prompt: "saved prompt"` 现在是**故意用来验「它没被用上」**的。
        seed 仍旧必须是这张图的 99：数值那几项照拿。
        """
        for name in ("galleryLoad", "foreign"):
            self.assert_view(self.data[name], "gallery")
            self.assertEqual(self.data[name]["prompt"], "history prompt")
        self.assertEqual(self.data["galleryLoad"]["seed"], 99)
        self.assertEqual(self.data["galleryLoad"]["search"], "blue")
        self.assertIn("不是 Qwen", self.data["foreign"]["message"])

    def test_invalid_or_failed_submission_keeps_the_current_view(self):
        self.assert_view(self.data["validation"], "gallery")
        self.assertFalse(self.data["validation"]["submitted"])
        self.assert_view(self.data["failedRun"], "gallery")
        self.assertFalse(self.data["failedRun"]["disabled"])

    def test_cost_confirmation_switches_only_after_actual_submission(self):
        self.assert_view(self.data["confirmation"], "gallery")
        self.assertFalse(self.data["confirmation"]["submitted"])
        self.assertTrue(self.data["confirmation"]["armed"])
        result = self.data["submitted"]
        self.assert_view(result, "live")
        self.assertEqual(result["currentId"], "submitted-job")
        self.assertIs(result["request"]["confirm"], True)
        self.assertEqual(result["request"]["prompt"], "neon skyline")
        self.assertFalse(result["disabled"])
        self.assertEqual(result["focused"], "tab-live")

    # --- 滑块与切换动画 ---------------------------------------------------
    def test_the_sliding_thumb_lands_on_the_selected_tab(self):
        """滑块的数值是 JS 从 offsetLeft/offsetWidth 算出来写进 CSS 的。

        假 DOM 没有布局引擎，所以位置是**喂**进去的（见 views_probe.js）。
        这条验的是 **JS → CSS 那一步写对了没有**（写的是不是当前那个标签）——
        三个标签的宽度刻意不同，写错标签会被抓到。
        「布局本身对不对」只有浏览器能回答：那一条走 `page_shot`，不在单测里。
        """
        self.assertEqual(self.data["initial"]["thumb"], "translateX(4px) / 76px")
        self.assertEqual(self.data["click"]["thumb"], "translateX(84px) / 60px")

    def test_switching_animates_in_the_direction_of_travel(self):
        """方向由 data-slide 表达，且**只有**新选中的那个面板带它。

        首次进入必须是 None —— 否则页面一打开就自己滑一下。
        """
        self.assertEqual(self.data["initial"]["slide"], [None, None, None])
        self.assertEqual(self.data["click"]["slide"], [None, "right", None])
        # 往左走（画廊 → 当前结果）方向必须跟着翻。
        self.assertEqual(self.data["keyboard"][0]["slide"], ["left", None, None])

    def test_the_preflight_column_drops_informational_lines_only(self):
        """提交前那一栏只画 warn / error（2026-10-01 按用户要求：估时那一行连同
        说明性提示一起去掉）。

        判据必须落在 `level` 上，而不是逐条列白名单 —— 白名单会长在文案上：
        后端改一句话，页面就悄悄少一条警告。所以这里喂进去三种 level，量的是
        「info 不画、warn 与 error 一条不少」这**一条规则**。后端 `/api/build`
        照旧把三种都给（那边是契约，没动）。
        """
        html = self.data["warningsRendered"]
        self.assertNotIn("INFO_MARKER_TE_SPEED", html)
        self.assertIn("WARN_MARKER_INERT", html)
        self.assertIn("ERROR_MARKER_MODELS", html)
        # 长相也要对：与体检项同一套管法（左边线颜色分好坏）
        self.assertIn('class="warn-line warn"', html)
        self.assertIn('class="warn-line error"', html)

    def test_a_dragged_name_comes_from_the_uri_not_from_the_browser(self):
        """从本页拖一张图时，名字必须从 `text/uri-list` 的 `name=` 取。

        为什么不能吃浏览器给的那个名字：实测它按「URL 末段 + MIME 后缀」合成，
        对 `/api/gallery/image?…` 就是 `image.png` —— 于是每拖一张都往同一个名字上
        覆盖（`/api/upload` 默认 overwrite=1），而选中集按名字去重，第二张永远
        进不来（用户的症状正是「拖第二张不显示」）。

        这条只覆盖**解析**那一半（纯函数、毫秒级）；「拖三张之后到底传上去了
        什么」由 `page_shot` 在真浏览器里按字节验。
        """
        self.assertEqual(self.data["refFromUri"], [
            "Qwen_a.png",          # 画廊格子的图
            "Qwen_b.png",          # name 里带路径 → 只取 basename
            "apple_1024.png",      # 已经选中的方块（/api/image?filename=）
            None,                  # 外站：不猜
            None,                  # file://：不猜
            None,                  # 没有 name 参数
            "after_comment.png",   # uri-list 允许注释行，跳过
            None,                  # 不是取图口（/api/upload）
            None,                  # 空串
        ])
