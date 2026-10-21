#!/usr/bin/env python3
"""`gallery.py` —— 扫磁盘、读元数据、与运行记录对账。

两条最容易错的断言在这里守着：

1. **`safe_join` 必须挡住路径穿越。** 文件名来自客户端（`/api/gallery/image`），
   不能指望调用方替我们检查。
2. **对账必须拒绝假匹配。** ComfyUI 的序号每次启动重置，同名文件可能属于两次
   不同的运行；直接按文件名认会让画廊显示**别人的耗时**。
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench import config, gallery  # noqa: E402


def png_with(prompt_workflow=None, extra=None):
    """造一张带 tEXt 的最小 PNG。"""
    chunks = []
    if prompt_workflow is not None:
        chunks.append((b"tEXt", b"prompt\x00" +
                       json.dumps(prompt_workflow).encode("utf-8")))
    for k, v in (extra or {}).items():
        chunks.append((b"tEXt", k.encode("latin-1") + b"\x00" + v.encode("latin-1")))
    out = [b"\x89PNG\r\n\x1a\n"]
    for ctype, body in chunks:
        out.append(struct.pack(">I", len(body)) + ctype + body)
        out.append(struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))
    out.append(struct.pack(">I", 0) + b"IEND" + struct.pack(">I", 0))
    return b"".join(out)


def qwen_graph(steps=20, seed=12345, width=1024, height=1024, switch=False,
               prompt="a green apple"):
    return {
        "459:474": {"class_type": "ComfySwitchNode",
                    "inputs": {"switch": switch, "on_false": prompt}},
        "459:471": {"class_type": "TextGenerate", "inputs": {"prompt": prompt}},
        "459:452": {"class_type": "TextEncodeQwenImage21",
                    "inputs": {"prompt": prompt, "negative_prompt": "",
                               "resolution": 1024}},
        "459:458": {"class_type": "KSampler",
                    "inputs": {"steps": steps, "seed": seed, "cfg": 1.0,
                               "sampler_name": "euler", "scheduler": "simple"}},
        "459:456": {"class_type": "EmptyLatentImage",
                    "inputs": {"width": width, "height": height}},
        "459:451": {"class_type": "UNETLoader",
                    "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors"}},
    }


def foreign_graph():
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors"}},
        "2": {"class_type": "KSampler", "inputs": {"steps": 8, "seed": 1}},
    }


class GalleryBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-gal-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, root, name, blob, mtime=None):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def root(self, name="out", label="测试根"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return {"path": path, "label": label, "kind": "test", "exists": True}


class SafeJoin(GalleryBase):
    def test_accepts_a_plain_name(self):
        root = os.path.join(self.tmp, "out")
        self.assertEqual(gallery.safe_join(root, "a.png"),
                         os.path.join(root, "a.png"))

    def test_accepts_a_subdirectory(self):
        root = os.path.join(self.tmp, "out")
        self.assertEqual(gallery.safe_join(root, "sub/a.png"),
                         os.path.join(root, "sub", "a.png"))

    def test_rejects_traversal(self):
        root = os.path.join(self.tmp, "out")
        for bad in ("../secret.png", "..\\secret.png", "sub/../../x.png",
                    "a/../../b.png"):
            self.assertIsNone(gallery.safe_join(root, bad), bad)

    def test_rejects_absolute_and_drive_paths(self):
        root = os.path.join(self.tmp, "out")
        for bad in ("/etc/passwd", "\\windows\\x.png", "C:/x.png",
                    "c:\\x.png"):
            self.assertIsNone(gallery.safe_join(root, bad), bad)

    def test_rejects_empty(self):
        self.assertIsNone(gallery.safe_join(self.tmp, ""))
        self.assertIsNone(gallery.safe_join(self.tmp, None))
        self.assertIsNone(gallery.safe_join(None, "a.png"))


class ResolveOutputFile(GalleryBase):
    """「根序号 + 文件名」→ 磁盘路径。

    取图与删图**共用**这一份判断（`/api/gallery/image` 与 `/api/gallery/delete`）：
    两边要是各写一份，就会长出「页面上画得出来、却删不掉」，或者更糟的
    「删掉的是另一个」—— 这两种都没有症状。
    """

    def setUp(self):
        super().setUp()
        self.r0 = self.root("out")
        self.r1 = self.root("out2")
        self.roots = [self.r0, self.r1]

    def test_resolves_a_plain_name(self):
        path = self.write(self.r0["path"], "a.png", b"x")
        self.assertEqual(gallery.resolve_output_file(self.roots, 0, "a.png"),
                         (path, None, None))

    def test_picks_the_root_it_was_told(self):
        self.write(self.r0["path"], "same.png", b"0")
        two = self.write(self.r1["path"], "same.png", b"1")
        self.assertEqual(gallery.resolve_output_file(self.roots, 1, "same.png")[0],
                         two)

    def test_out_of_range(self):
        for bad in (-1, 2, 99):
            self.assertEqual(gallery.resolve_output_file(self.roots, bad, "a.png"),
                             (None, "root 越界", 404), bad)

    def test_illegal_name(self):
        self.assertEqual(gallery.resolve_output_file(self.roots, 0, "../x.png"),
                         (None, "非法的文件名", 400))

    def test_missing_file(self):
        self.assertEqual(gallery.resolve_output_file(self.roots, 0, "nope.png"),
                         (None, "图片不在了", 404))

    def test_a_directory_is_not_a_file(self):
        os.makedirs(os.path.join(self.r0["path"], "sub.png"), exist_ok=True)
        self.assertIsNone(gallery.resolve_output_file(self.roots, 0, "sub.png")[0])


class Deletable(GalleryBase):
    """删除比取图**多两条**。

    名单是客户端给的（`POST /api/gallery/delete` 的 body），所以「能删什么」
    必须在这一层收窄 —— 不挡的话，同一个目录里的任何文件都能被点名删掉。
    """

    def setUp(self):
        super().setUp()
        self.r0 = self.root("out")
        self.roots = [self.r0]

    def test_a_png_can_be_deleted(self):
        path = self.write(self.r0["path"], "a.png", b"x")
        self.assertEqual(gallery.deletable(self.roots, 0, "a.png"), (path, None))

    def test_uppercase_extension_counts_too(self):
        path = self.write(self.r0["path"], "A.PNG", b"x")
        self.assertEqual(gallery.deletable(self.roots, 0, "A.PNG"), (path, None))

    def test_any_other_file_is_refused(self):
        for name in ("notes.txt", "prompt.json", "a.png.bak", "noext"):
            self.write(self.r0["path"], name, b"x")
            path, why = gallery.deletable(self.roots, 0, name)
            self.assertIsNone(path, name)
            self.assertIn("不是 PNG", why, name)

    def test_the_temp_files_comfyui_is_writing_are_refused(self):
        name = "_temp_00001_.png"
        self.write(self.r0["path"], name, b"x")
        path, why = gallery.deletable(self.roots, 0, name)
        self.assertIsNone(path)
        self.assertIn("中间产物", why)

    def test_the_same_refusals_as_the_reading_side(self):
        self.assertEqual(gallery.deletable(self.roots, 5, "a.png")[1], "root 越界")
        self.assertEqual(gallery.deletable(self.roots, 0, "../a.png")[1],
                         "非法的文件名")
        self.assertEqual(gallery.deletable(self.roots, 0, "nope.png")[1],
                         "图片不在了")


class RunsIndex(support.LeakCheckedTestCase):
    def test_only_done_runs_are_indexed(self):
        index = gallery.runs_index([
            {"status": "done", "job_id": "a",
             "outputs": [{"filename": "x.png"}]},
            {"status": "error", "job_id": "b",
             "outputs": [{"filename": "y.png"}]},
            {"status": "cancelled", "job_id": "c",
             "outputs": [{"filename": "z.png"}]},
        ])
        self.assertEqual(list(index), ["x.png"])

    def test_collects_multiple_runs_per_filename(self):
        """同名的可能不止一条 —— 序号会在后端重启后重置。"""
        index = gallery.runs_index([
            {"status": "done", "job_id": "a", "outputs": [{"filename": "same.png"}]},
            {"status": "done", "job_id": "b", "outputs": [{"filename": "same.png"}]},
        ])
        self.assertEqual([r["job_id"] for r in index["same.png"]], ["a", "b"])

    def test_survives_garbage(self):
        index = gallery.runs_index([
            None, {"status": "done"}, {"status": "done", "outputs": None},
            {"status": "done", "outputs": [None, {}, {"filename": ""}]},
            {"status": "done", "outputs": [{"filename": "ok.png"}]},
        ])
        self.assertEqual(list(index), ["ok.png"])


class Reconcile(support.LeakCheckedTestCase):
    def test_agrees_when_comparable_fields_match(self):
        info = {"steps": 20, "seed": 7, "width": 1024, "height": 1024}
        self.assertIs(gallery._agrees(info, {"params": info}), True)

    def test_disagrees_on_any_mismatch(self):
        info = {"steps": 20, "seed": 7, "width": 1024, "height": 1024}
        run = {"params": {"steps": 40, "seed": 7, "width": 1024, "height": 1024}}
        self.assertIs(gallery._agrees(info, run), False)

    def test_uncomparable_returns_none_not_false(self):
        """width 可能是「链接到 ResolutionSelector」而不是字面量，两边都没有
        就没得比 —— 那叫「不知道」，不叫「矛盾」。"""
        self.assertIsNone(gallery._agrees(
            {"steps": None, "seed": None, "width": None, "height": None},
            {"params": {"steps": 20, "seed": 7}}))
        self.assertIsNone(gallery._agrees({}, {"params": {}}))

    def test_matching_a_subset_is_enough_to_confirm(self):
        """能比几个比几个：steps 对得上就该认，不必等四个字段全有。"""
        self.assertIs(gallery._agrees(
            {"steps": 20, "seed": None, "width": None, "height": None},
            {"params": {"steps": 20}}), True)

    def test_ignores_mode_because_legacy_records_say_2k(self):
        """**老记录**里 mode 写的是 "2k"，而 PNG 里反解出来是 "t2i" —— 拿 mode 比会假报警。

        `2k` 那个模式 2026-10-01 撤销了，但历史记录不会跟着改；这一条钉的就是
        「读老数据要宽容」。
        """
        info = {"mode": "t2i", "steps": 20, "seed": 7}
        self.assertIs(gallery._agrees(info, {"params": {"mode": "2k",
                                                        "steps": 20, "seed": 7}}),
                      True)

    def test_match_run_prefers_the_agreeing_candidate(self):
        info = {"steps": 20, "seed": 7, "width": 1024, "height": 1024}
        good = {"job_id": "good", "params": {"steps": 20, "seed": 7,
                                             "width": 1024, "height": 1024}}
        bad = {"job_id": "bad", "params": {"steps": 40, "seed": 7,
                                           "width": 1024, "height": 1024}}
        run, verdict = gallery.match_run(info, [bad, good])
        self.assertEqual(run["job_id"], "good")
        self.assertEqual(verdict, "confirmed")

    def test_match_run_flags_conflict_when_nothing_agrees(self):
        info = {"steps": 20, "seed": 7, "width": 1024, "height": 1024}
        bad = {"job_id": "bad", "params": {"steps": 40, "seed": 9,
                                           "width": 1024, "height": 1024}}
        run, verdict = gallery.match_run(info, [bad])
        self.assertEqual(verdict, "conflict")

    def test_match_run_none_when_no_candidates(self):
        self.assertEqual(gallery.match_run({}, []), (None, "none"))
        self.assertEqual(gallery.match_run({}, None), (None, "none"))


class Scan(GalleryBase):
    def setUp(self):
        super().setUp()
        self.root = self.root()
        self.write(self.root["path"], "a.png", png_with(qwen_graph(seed=1)))
        self.write(self.root["path"], "b.png", png_with(qwen_graph(seed=2, steps=40)))
        self.write(self.root["path"], "c.png", png_with(foreign_graph()))
        self.write(self.root["path"], "notpng.txt", b"ignore me")
        self.root["exists"] = True

    def scan(self, **kw):
        return gallery.scan([self.root], **kw)

    def test_reads_metadata_without_any_run_record(self):
        res = self.scan()
        self.assertEqual(res["scanned"], 3)          # .txt 不算
        by = {i["filename"]: i for i in res["items"]}
        self.assertEqual(by["a.png"]["seed"], 1)
        self.assertEqual(by["a.png"]["steps"], 20)
        self.assertEqual(by["a.png"]["mode"], "t2i")
        self.assertTrue(by["a.png"]["recognized"])
        self.assertFalse(by["a.png"]["has_timings"])
        self.assertEqual(by["a.png"]["run_match"], "none")

    def test_foreign_graph_is_flagged_not_faked(self):
        by = {i["filename"]: i for i in self.scan()["items"]}
        self.assertFalse(by["c.png"]["recognized"])
        self.assertIsNone(by["c.png"]["mode"])

    def test_counts(self):
        counts = self.scan()["counts"]
        self.assertEqual(counts["all"], 3)
        self.assertEqual(counts["recognized"], 2)
        self.assertEqual(counts["unrecognized"], 1)
        # MINIMAX 那张也有 KSampler，但不在 459:458 上 —— 所以它不是本链路，
        # 也就没有 mode。这正是「不认识就说不认识」该有的样子。
        self.assertEqual(counts["t2i"], 2)

    def test_mode_filter(self):
        # 模式只表达形状：`refine` 不再是一个模式（它是额外功能），所以拿它当
        # 模式筛**什么都筛不到** —— 这一条同时守着「没有谁把改写又塞回 mode 里」。
        self.assertEqual(self.scan(mode="refine")["total"], 0)
        self.assertEqual(self.scan(mode="t2i")["total"], 2)

    def test_rewrite_filter_is_orthogonal_to_mode(self):
        """改写是**额外功能**：它有自己的筛选项，与 mode 正交。

        同一个模式下既有改写过、也有没改写过的 —— 所以「按 rewrite 筛」必须
        真的少一半，而不是等于「按 mode 筛」。（`Scan` 的基线三张都没开改写，
        所以这里现写一张开着 `switch` 的 —— 老 refine 产物就是那个形状。）
        """
        self.write(self.root["path"], "rw.png",
                   png_with(qwen_graph(seed=3, switch=True)))
        self.assertEqual(self.scan(rewrite=True)["total"], 1)
        self.assertEqual(self.scan(mode="t2i")["total"], 3)      # 四张都是 t2i
        self.assertEqual(self.scan(mode="t2i", rewrite=True)["total"], 1)
        # 缺参数 = **不筛**（而不是「只要没改写的」）—— 这条是防呆：
        # 上一版把 `rewrite` 写成三态，缺参数时会把大半屋子图筛没。
        self.assertEqual(self.scan(rewrite=False)["total"], 4)

    def test_recognized_filter_is_tri_state(self):
        self.assertEqual(self.scan(recognized=True)["total"], 2)
        self.assertEqual(self.scan(recognized=False)["total"], 1)
        self.assertEqual(self.scan(recognized=None)["total"], 3)

    def test_two_k_filter(self):
        self.write(self.root["path"], "big.png",
                   png_with(qwen_graph(width=2048, height=2048)))
        self.assertEqual(self.scan(two_k=True)["total"], 1)
        self.assertEqual(self.scan()["counts"]["is_2k"], 1)

    def test_query_matches_prompt_and_filename(self):
        self.assertEqual(self.scan(query="green apple")["total"], 2)
        self.assertEqual(self.scan(query="a.png")["total"], 1)
        self.assertEqual(self.scan(query="nothing here")["total"], 0)

    def test_query_survives_a_graph_whose_prompt_is_a_link(self):
        """搜索是 `needle in prompt.lower()` —— 只要有一张图的 prompt 不是字符串，
        **整个搜索**就 500（实测：本机两张断边年代的老图让「搜任何字」都打不开）。

        所以这一条钉的是「搜索永远答得出来」，而不是「那一张显示得好看」：它照样
        在列表里（如实标成没有提示词），另外两张该匹配的照旧匹配。
        """
        graph = qwen_graph(seed=9)
        for node in graph.values():
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            if inputs.get("on_false"):
                inputs["on_false"] = ["459:472", 0]
            if inputs.get("prompt"):
                inputs["prompt"] = ["459:472", 0]
        self.write(self.root["path"], "link.png", png_with(graph))
        by = {i["filename"]: i for i in self.scan()["items"]}
        self.assertEqual(by["link.png"]["prompt"], "")      # 不是 list，是空串
        res = self.scan(query="green apple")
        names = [i["filename"] for i in res["items"]]
        self.assertEqual(sorted(names), ["a.png", "b.png"])
        self.assertNotIn("link.png", names)                 # 它身上没有生效过的提示词
        self.assertEqual(self.scan(query="zzz")["total"], 0)

    def test_newest_first(self):
        # mtime 要相对「现在」—— 绝对小数字是 1970 年，比刚写下的文件还旧
        now = time.time()
        self.write(self.root["path"], "old.png",
                   png_with(qwen_graph(seed=9)), mtime=now - 1000)
        self.write(self.root["path"], "new.png",
                   png_with(qwen_graph(seed=10)), mtime=now)
        names = [i["filename"] for i in self.scan()["items"]]
        # 失败时**把整份顺序报出来**：只报「谁不是第一个」的话，「排序不对」与
        # 「那一张根本没扫进来」在报告里长得一模一样（2026-10-02 全量套件里红过一次，
        # 报告只有 `'c.png' != 'new.png'`，看不出是哪一种）。
        self.assertEqual(names[0], "new.png", "实际顺序：%s" % names)
        self.assertEqual(names[-1], "old.png", "实际顺序：%s" % names)

    def test_paging(self):
        page = self.scan(limit=2, offset=1)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["total"], 3)

    def test_unreadable_png_is_listed_not_dropped(self):
        self.write(self.root["path"], "junk.png", b"\x89PNG\r\n\x1a\nGARBAGE")
        res = self.scan()
        by = {i["filename"]: i for i in res["items"]}
        self.assertIn("junk.png", by)
        self.assertTrue(by["junk.png"]["unreadable"])
        self.assertFalse(by["junk.png"]["recognized"])
        self.assertEqual(res["scanned"], 4)

    def test_missing_root_is_skipped_not_fatal(self):
        missing = {"path": os.path.join(self.tmp, "nope"), "label": "x",
                   "kind": "test", "exists": False}
        res = gallery.scan([missing, self.root])
        self.assertEqual(res["scanned"], 3)
        self.assertEqual(len(res["roots"]), 2)

    def test_run_record_joins_by_filename_and_confirms(self):
        index = gallery.runs_index([{
            "status": "done", "job_id": "job-1", "wall_ms": 49085,
            "params": {"steps": 20, "seed": 1, "width": 1024, "height": 1024},
            "outputs": [{"filename": "a.png"}]}])
        by = {i["filename"]: i
              for i in gallery.scan([self.root], index)["items"]}
        self.assertEqual(by["a.png"]["run_match"], "confirmed")
        self.assertTrue(by["a.png"]["has_timings"])
        self.assertEqual(by["a.png"]["run"]["wall_ms"], 49085)
        # 没被引用的那些照旧
        self.assertEqual(by["b.png"]["run_match"], "none")

    def test_conflicting_run_record_does_not_leak_wall_ms(self):
        """同名的另一张图（后端重启后序号重置）—— 绝不能把它的耗时显示出来。"""
        index = gallery.runs_index([{
            "status": "done", "job_id": "job-other", "wall_ms": 999,
            "params": {"steps": 40, "seed": 777, "width": 1024, "height": 1024},
            "outputs": [{"filename": "a.png"}]}])
        by = {i["filename"]: i
              for i in gallery.scan([self.root], index)["items"]}
        self.assertEqual(by["a.png"]["run_match"], "conflict")
        self.assertFalse(by["a.png"]["has_timings"])
        self.assertIsNone(by["a.png"]["run"])

    def test_skips_comfyui_temp_files(self):
        self.write(self.root["path"], "_temp_out.png", png_with(qwen_graph()))
        names = [i["filename"] for i in self.scan()["items"]]
        self.assertNotIn("_temp_out.png", names)


class CandidateRoots(support.LeakCheckedTestCase):
    def test_dedupes_and_reports_existence(self):
        roots = gallery.candidate_roots(None)
        paths = [os.path.normcase(os.path.normpath(r["path"])) for r in roots]
        self.assertEqual(len(paths), len(set(paths)), "候选目录有重复")
        for r in roots:
            self.assertIsInstance(r["exists"], bool)
            self.assertTrue(r["label"])

    def test_the_dir_the_launcher_writes_to_is_scanned(self):
        """写者与读者共用一个真源（`config.OUTPUT_DIR`）。

        改了 `--output-directory` 却忘了画廊这一侧，症状是**最难发现的那种**：
        图一张接一张好好出着，画廊却说「一张都没有」。
        """
        roots = gallery.candidate_roots(None)
        paths = [os.path.normcase(os.path.normpath(r["path"])) for r in roots]
        want = os.path.normcase(os.path.normpath(config.OUTPUT_DIR))
        self.assertIn(want, paths, "落盘目录不在画廊的候选里 —— 新图会看不见")

    def test_argv_output_directory_wins(self):
        class Fake:
            def system_stats(self):
                return {"system": {"argv": ["main.py", "--output-directory",
                                            r"D:\somewhere\out"]}}
        roots = gallery.candidate_roots(Fake())
        self.assertEqual(roots[0]["kind"], "argv")
        self.assertEqual(roots[0]["path"], r"D:\somewhere\out")

    def test_argv_equals_form(self):
        class Fake:
            def system_stats(self):
                return {"system": {"argv": ["main.py",
                                            "--output-directory=D:/x/out"]}}
        self.assertEqual(gallery.candidate_roots(Fake())[0]["path"], "D:/x/out")

    def test_accepts_precomputed_stats(self):
        """`/api/backend` 已经取过一次 /system_stats，不该为同一件事再打一次。"""
        calls = []

        class Fake:
            def system_stats(self):
                calls.append(1)
                return {}

        gallery.candidate_roots(Fake(), stats={"system": {"argv": []}})
        self.assertEqual(calls, [])


class DesktopConfig(GalleryBase):
    def test_reads_output_dir_from_settings(self):
        path = os.path.join(self.tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"outputDir": r"C:\shared\output", "inputDir": "x"}, f)
        self.assertEqual(gallery.desktop_output_dir(path), r"C:\shared\output")

    def test_missing_or_broken_settings_is_none(self):
        self.assertIsNone(gallery.desktop_output_dir(
            os.path.join(self.tmp, "nope.json")))
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(gallery.desktop_output_dir(bad))

    def test_reads_install_path(self):
        path = os.path.join(self.tmp, "installations.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"id": "cloud", "remoteUrl": "https://x"},
                       {"id": "inst-1", "installPath": r"C:\inst\ComfyUI"}], f)
        self.assertEqual(gallery.desktop_install_root(path), r"C:\inst\ComfyUI")

    def test_install_path_missing_is_none(self):
        self.assertIsNone(gallery.desktop_install_root(
            os.path.join(self.tmp, "nope.json")))


class RealMachine(support.LeakCheckedTestCase):
    """对这台机器的真实输出目录做一次只读冒烟。找不到就跳过。"""

    def test_real_pngs_scan_cleanly(self):
        roots = [r for r in gallery.candidate_roots(None) if r["exists"]]
        if not roots:
            self.skipTest("没有任何存在的输出目录")
        res = gallery.scan(roots, gallery.runs_index([]), limit=50)
        if not res["scanned"]:
            self.skipTest("输出目录里没有 PNG")
        self.assertGreater(res["counts"]["recognized"], 0)
        for item in res["items"][:20]:
            self.assertIsNotNone(item["id"])
            self.assertTrue(item["filename"].lower().endswith(".png"))
            # 路径拼接必须能落到一个真实文件上
            path = gallery.safe_join(roots[item["root"]]["path"], item["filename"])
            self.assertTrue(os.path.isfile(path), item["filename"])


class LayerSetsStayOutOfTheGallery(GalleryBase):
    """★ 一次拆层会在输出目录顶层丢下 40~50 个 PNG。

    不挡的话每跑一次就多 48 个「作品」（`pngmeta` 解不出参数，全是「未识别」），
    一屏就被它们占满 —— 而那正是「画廊坏了」的样子。

    排除规则是**两件条件同时成立**：形状对 ∧ 同根目录里有配套的 `*_layers.json`。
    这一节把两半和三种取反都钉住。
    """

    TS = "20261012"
    TIME = "101530"
    UID = "ab12cd34"

    def setUp(self):
        super().setUp()
        # `GalleryBase.root()` 是个**方法**；这里把它换成实例属性（基类那几条
        # 测试都是这么用的）。不换的话 `self.root["path"]` 会撞上「方法不可下标」。
        self.root = self.root()
        self.root["exists"] = True

    def layer(self, tag, token="a1b2c3d4"):
        return "seethrough_%s_%s_%s_%s_%s.png" % (token, self.TS, self.TIME,
                                                  self.UID, tag)

    def info(self, token="a1b2c3d4"):
        return "seethrough_%s_%s_%s_%s_layers.json" % (token, self.TS,
                                                       self.TIME, self.UID)

    def write_set(self, tags=("tail", "face"), token="a1b2c3d4", info=True):
        for tag in tags:
            self.write(self.root["path"], self.layer(tag, token),
                       png_with(qwen_graph(seed=1)))
        if info:
            self.write(self.root["path"], self.info(token),
                       json.dumps({"layers": [], "width": 8, "height": 8}).encode())

    def scan(self):
        return gallery.scan([self.root])

    def test_all_members_are_dropped(self):
        self.write_set(tags=("tail", "face", "front hair"))
        res = self.scan()
        self.assertEqual(res["items"], [])
        self.assertEqual(res["scanned"], 0)

    def test_a_lookalike_without_a_layers_json_is_kept(self):
        """★ 只按形状排会**静默藏掉真图** —— 这个名字的用户图完全可能存在。"""
        loose = "photo_%s_%s_deadbeef_cat.png" % (self.TS, self.TIME)
        self.write(self.root["path"], loose, png_with(qwen_graph(seed=3)))
        res = self.scan()
        self.assertEqual([i["filename"] for i in res["items"]], [loose])

    def test_a_lookalike_with_someone_elses_json_is_kept(self):
        """前缀对不上就不算这一批 —— `_layers.json` 也在才算，不是「有这个文件就行」。"""
        loose = "photo_%s_%s_deadbeef_cat.png" % (self.TS, self.TIME)
        self.write(self.root["path"], loose, png_with(qwen_graph(seed=3)))
        self.write(self.root["path"], self.info("a1b2c3d4"),
                   json.dumps({"layers": []}).encode())
        res = self.scan()
        self.assertEqual([i["filename"] for i in res["items"]], [loose])

    def test_a_qwen_image_is_never_dropped(self):
        self.write_set()
        self.write(self.root["path"], "Qwen_image_2.1_00001.png",
                   png_with(qwen_graph(seed=9)))
        res = self.scan()
        self.assertEqual([i["filename"] for i in res["items"]],
                         ["Qwen_image_2.1_00001.png"])

    def test_the_json_itself_never_shows_up_as_an_item(self):
        self.write_set()
        res = self.scan()
        self.assertFalse([i for i in res["items"] if i["filename"].endswith(".json")])

    def test_members_are_dropped_per_root_not_globally(self):
        """★ 排除是**逐根**判的：另一个根里那份 `_layers.json` 不该管到这个根。

        判据落在 root 上而不是全屋一次，否则两个输出目录并存时（这台机器上
        确实有两个）会把一个根里的真图按另一个根的证据藏掉。
        """
        other_path = os.path.join(self.tmp, "other")
        os.makedirs(other_path, exist_ok=True)
        other = {"path": other_path, "label": "另一个根", "kind": "test",
                 "exists": True}
        self.write(other_path, self.info(), json.dumps({"layers": []}).encode())
        # 这个根里只有一张形状像层集的图，**没有**配套的 json
        loose = self.layer("tail")
        self.write(self.root["path"], loose, png_with(qwen_graph(seed=1)))
        res = gallery.scan([self.root, other])
        names = [i["filename"] for i in res["items"]]
        self.assertIn(loose, names)

    def test_h3_videos_never_show_up(self):
        """H3 的视频落在 `output/video/`，而画廊只扫每个根的**顶层**。

        这是**不变量**，不是巧合 —— 哪天有人给画廊加上递归，这条会当场炸。
        """
        sub = os.path.join(self.root["path"], "video")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "MiniMax_H3_a1b2c3d4_00001.mp4"), "wb") as f:
            f.write(b"x" * 32)
        self.write(self.root["path"], "keep.png", png_with(qwen_graph(seed=1)))
        res = self.scan()
        self.assertEqual([i["filename"] for i in res["items"]], ["keep.png"])
        self.assertEqual(res["scanned"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
