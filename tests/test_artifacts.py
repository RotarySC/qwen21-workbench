#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`workbench/artifacts.py` 的判据 —— 作业 ↔ 产物那条键。

全是纯函数，所以判据就落在**磁盘状态**上：造一棵假的输出树，看它认不认得出
「这个作业的那一批」。要覆盖的坏情况有四类，每一类在页面上长得都像「取不到产物」：

  1. 视频在 `video/` 子目录里（真正的落点），不是在顶层
  2. 同一份参数再提一次 ⇒ 目录里有两个视频，要取**新的**那个
  3. 层清单列了二十几张，其中一张被归档/搬走了 ⇒ 要**说出缺哪张**，不许装作齐了
  4. 层清单本身读不出来（半个 JSON）⇒ 要说得出，不许抛

    python tests/test_artifacts.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))          # …/qwen21-workbench/tests
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # support.py 就在 tests/ 里

import layers_build as lb       # noqa: E402
import support                  # noqa: E402
from workbench import artifacts as art  # noqa: E402

TOKEN = "a1b2c3d4"
OTHER = "ffffffff"


class ArtifactCase(support.LeakCheckedTestCase):
    def setUp(self):
        # 浅拷贝的假输出树。它必须是**唯一**的根，否则别的候选根里真存在的文件
        # 会把判据搅浑（`candidate_roots()` 平时会给出好几个）。
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(),
                                    prefix="qwen21-art-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "output")
        os.makedirs(self.out)

    # --- 造数据 -----------------------------------------------------------
    def roots(self):
        return [{"path": self.out, "label": "fake", "kind": "argv"}]

    def two_roots(self):
        """两个候选根，产物放**第二个**里。

        只造一个根的话 `root` 恒为 0 也能过 —— 而那正是前端拿错图的那种错。
        """
        second = os.path.join(self.tmp, "output2")
        os.makedirs(second, exist_ok=True)
        return [{"path": self.out, "label": "first", "kind": "argv"},
                {"path": second, "label": "second", "kind": "workspace"}], second

    def write_at(self, base, rel, data=b"x"):
        path = os.path.join(base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode) as f:
            f.write(data)
        return path

    def write(self, rel, data=b"x"):
        return self.write_at(self.out, rel, data)

    def write_layers_json(self, name, layers, width=1024, height=1024,
                          raw=None):
        payload = raw if raw is not None else {
            "prefix": "seethrough", "timestamp": "20261012_101530",
            "layers": layers, "width": width, "height": height}
        return self.write(name, json.dumps(payload, ensure_ascii=False))

    # --- 层集的命名（`FindLayerSet` 与 `GalleryExclusion` 共用）------------
    def info_name(self, token=TOKEN):
        return "seethrough_%s_20261012_101530_ab12cd34_layers.json" % token

    def layer_name(self, tag, token=TOKEN):
        return "seethrough_%s_20261012_101530_ab12cd34_%s.png" % (token, tag)

    def make_set(self, tags=("tail", "front hair"), with_files=True):
        layers = []
        for tag in tags:
            entry = {"name": tag, "filename": self.layer_name(tag),
                     "left": 0, "top": 0, "right": 100, "bottom": 200,
                     "depth_median": 0.5}
            if with_files:
                self.write(entry["filename"])
            layers.append(entry)
        self.write_layers_json(self.info_name(), layers)
        return layers


class TokenOf(support.LeakCheckedTestCase):
    def test_takes_the_first_eight_characters(self):
        self.assertEqual(art.token_of("a1b2c3d4-1111-2222-3333-444455556666"),
                         "a1b2c3d4")

    def test_short_and_missing_ids_do_not_explode(self):
        self.assertEqual(art.token_of("abc"), "abc")
        self.assertEqual(art.token_of(""), "")
        self.assertEqual(art.token_of(None), "")


class FindVideo(ArtifactCase):
    def test_finds_it_in_the_video_subfolder(self):
        # ★ 真实的落点：`SaveVideo` 的前缀是 `video/MiniMax_H3`。
        # 只扫顶层的话永远找不到 —— 而页面只会说「取不到产物」。
        self.write("video/MiniMax_H3_%s_00001.mp4" % TOKEN, b"0123456789")
        got = art.find_video(self.roots(), TOKEN)
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "video")
        self.assertEqual(got["filename"], "MiniMax_H3_%s_00001.mp4" % TOKEN)
        self.assertEqual(got["subfolder"], "video")
        self.assertEqual(got["bytes"], 10)

    def test_finds_it_in_the_top_level_too(self):
        self.write("MiniMax_H3_%s.mp4" % TOKEN)
        self.assertIsNotNone(art.find_video(self.roots(), TOKEN))

    def test_ignores_another_job(self):
        self.write("video/MiniMax_H3_%s_00001.mp4" % OTHER)
        self.assertIsNone(art.find_video(self.roots(), TOKEN))

    def test_the_token_must_be_its_own_segment(self):
        # `xa1b2c3d4y` 里含 `a1b2c3d4` 这个子串，但它不是这个作业的文件。
        self.write("video/MiniMax_H3_x%sy_00001.mp4" % TOKEN)
        self.assertIsNone(art.find_video(self.roots(), TOKEN))

    def test_takes_the_newest_when_runs_are_repeated(self):
        # 同一份参数再提一次 ⇒ 收尾前缀相同、编号递增。用户要看的是刚跑的那个。
        old = self.write("video/MiniMax_H3_%s_00001.mp4" % TOKEN, b"old")
        new = self.write("video/MiniMax_H3_%s_00002.mp4" % TOKEN, b"new")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(new, (1_700_000_000, 1_700_000_000))
        got = art.find_video(self.roots(), TOKEN)
        self.assertEqual(got["filename"], "MiniMax_H3_%s_00002.mp4" % TOKEN)

    def test_ignores_non_video_extensions(self):
        self.write("MiniMax_H3_%s_00001.png" % TOKEN)
        self.write("MiniMax_H3_%s_00001.txt" % TOKEN)
        self.assertIsNone(art.find_video(self.roots(), TOKEN))

    def test_no_token_no_answer(self):
        self.write("video/MiniMax_H3_%s_00001.mp4" % TOKEN)
        self.assertIsNone(art.find_video(self.roots(), ""))
        self.assertIsNone(art.find_video(self.roots(), None))

    def test_missing_root_is_not_an_error(self):
        self.assertIsNone(art.find_video([{"path": os.path.join(self.tmp, "nope")}],
                                         TOKEN))
        self.assertIsNone(art.find_video([], TOKEN))
        self.assertIsNone(art.find_video(None, TOKEN))


class FindLayerSet(ArtifactCase):
    def test_finds_the_set_and_verifies_every_layer(self):
        self.make_set()
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "layers")
        self.assertEqual(got["info"], self.info_name())
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["missing"], [])
        self.assertIsNone(got["error"])
        self.assertEqual(got["width"], 1024)
        self.assertEqual(got["height"], 1024)
        self.assertEqual([lay["name"] for lay in got["layers"]],
                         ["tail", "front hair"])

    def test_reports_missing_layers_instead_of_pretending(self):
        """★ 一张层被归档/搬走之后，「少了一张」与「都还在」在页面上长得一样。"""
        self.make_set(tags=("tail", "front hair", "face"), with_files=False)
        self.write(self.layer_name("tail"))
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertEqual(got["count"], 3)
        self.assertEqual(sorted(got["missing"]),
                         sorted([self.layer_name("front hair"),
                                 self.layer_name("face")]))

    def test_ignores_another_job(self):
        self.make_set()
        renamed = self.info_name().replace(TOKEN, OTHER, 1)
        os.rename(os.path.join(self.out, self.info_name()),
                  os.path.join(self.out, renamed))
        self.assertIsNone(art.find_layer_set(self.roots(), TOKEN))

    def test_survives_a_truncated_json(self):
        self.write(self.info_name(), '{"layers": [')
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertIsNotNone(got)
        self.assertIsNotNone(got["error"])
        self.assertEqual(got["layers"], [])

    def test_survives_a_json_that_is_not_an_object(self):
        self.write(self.info_name(), "[1, 2, 3]")
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertIn("对象", got["error"])

    def test_survives_a_missing_layers_array(self):
        self.write_layers_json(self.info_name(), [], raw={"width": 8})
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertIn("layers", got["error"])

    def test_skips_entries_without_a_filename(self):
        self.write_layers_json(self.info_name(),
                               [{"name": "tail"}, {"name": "face"}])
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertEqual(got["layers"], [])
        self.assertEqual(got["count"], 0)

    def test_picks_the_newest_set(self):
        old = self.write(
            "seethrough_%s_20261001_010101_11111111_layers.json" % TOKEN,
            json.dumps({"layers": [], "width": 1, "height": 1}))
        new = self.write_layers_json(self.info_name(), [])
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(new, (1_700_000_000, 1_700_000_000))
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertEqual(got["info"], self.info_name())

    def test_depth_files_are_verified_too(self):
        tag = "front hair"
        entry = {"name": tag, "filename": self.layer_name(tag),
                 "depth_filename":
                     "seethrough_%s_20261012_101530_ab12cd34_%s_depth.png"
                     % (TOKEN, tag.replace(" ", "_")),
                 "left": 0, "top": 0, "right": 1, "bottom": 1,
                 "depth_median": 0.1}
        self.write(entry["filename"])
        self.write_layers_json(self.info_name(), [entry])
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertEqual(got["missing"], [entry["depth_filename"]])
        self.assertEqual(got["layers"][0]["depth_filename"],
                         entry["depth_filename"])

    def test_no_token_no_answer(self):
        self.make_set()
        self.assertIsNone(art.find_layer_set(self.roots(), ""))


class RootIndex(ArtifactCase):
    """★ 产物底下那个 `root` 必须是**真的那个下标**，不是恒为 0。

    它是给前端寻址用的：层的缩略图走 `/api/gallery/thumb?root=<下标>&name=<文件名>`。
    恒为 0 的症状是**取到另一个根里的同名文件**（画廊的取图口就是按 `(root, name)`
    找的），或者干脆 404 —— 两种都不报错，都是「图不对」，而且画面上看不出来。
    """

    def test_video_reports_the_root_it_was_found_in(self):
        roots, second = self.two_roots()
        self.write_at(second, "video/MiniMax_H3_%s_00001.mp4" % TOKEN, b"v")
        got = art.find_video(roots, TOKEN)
        self.assertIsNotNone(got)
        self.assertEqual(got["root"], 1)
        self.assertEqual(got["filename"], "MiniMax_H3_%s_00001.mp4" % TOKEN)

    def test_video_in_the_first_root_is_index_zero(self):
        roots, _ = self.two_roots()
        self.write("video/MiniMax_H3_%s_00001.mp4" % TOKEN, b"v")
        self.assertEqual(art.find_video(roots, TOKEN)["root"], 0)

    def test_layers_report_the_root_it_was_found_in(self):
        roots, second = self.two_roots()
        tag = "tail"
        self.write_at(second, self.layer_name(tag))
        self.write_at(second, self.info_name(), json.dumps({
            "layers": [{"name": tag, "filename": self.layer_name(tag),
                        "left": 0, "top": 0, "right": 1, "bottom": 1}],
            "width": 4, "height": 4}, ensure_ascii=False))
        got = art.find_layer_set(roots, TOKEN)
        self.assertIsNotNone(got)
        self.assertEqual(got["root"], 1)
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["missing"], [])

    def test_a_skipped_root_does_not_shift_the_index(self):
        """**没有 `path` 的根也要占一个下标** —— 下标是**位置**，不是「第几个能用的根」。

        跟着「能用的根」数，下标就与 `candidate_roots()` 的顺序错位，
        而错位的症状同样是取到别的根里的同名图。

        ⚠️ 别拿「目录不存在」当这个用例：`_candidates()` 只跳过**没有 path 键**的根，
        目录不存在它照样占位（`_walk` 自己吞掉 `OSError`）—— 用那种根做判据，
        两种实现给出同一个下标，**判据是空的**（这一版的第二稿就是这么假绿的）。
        """
        roots, second = self.two_roots()
        roots.insert(1, {"label": "no-path", "kind": "fallback"})
        self.write_at(second, "video/MiniMax_H3_%s_00001.mp4" % TOKEN, b"v")
        got = art.find_video(roots, TOKEN)
        self.assertIsNotNone(got)
        self.assertEqual(got["root"], 2)

    def test_a_nonexistent_directory_still_holds_its_slot(self):
        """目录不存在的根同样占位（真实的 `candidate_roots()` 会给这种根）。"""
        roots, second = self.two_roots()
        roots.insert(1, {"path": os.path.join(self.tmp, "missing-root"),
                         "label": "gone", "kind": "fallback"})
        self.write_at(second, "video/MiniMax_H3_%s_00001.mp4" % TOKEN, b"v")
        self.assertEqual(art.find_video(roots, TOKEN)["root"], 2)


class GalleryExclusion(ArtifactCase):
    """★ 画廊排除层集的两件条件：**形状** ＋ **同根有配套的 json**。

    单靠形状会静默藏掉真图（`SeeThrough_SavePSD` 的命名形状并不唯一属于它），
    所以这里把两半都钉住。
    """
    def test_a_layer_shaped_png_with_a_sibling_json_is_a_set(self):
        self.make_set(tags=("tail",))
        got = art.find_layer_set(self.roots(), TOKEN)
        self.assertIsNotNone(got)

    def test_a_layer_shaped_png_without_a_sibling_json_is_not(self):
        # 用户自己丢进 output/ 的一张图完全可能长这个形状。
        loose = "photo_20260101_120000_deadbeef_cat.png"
        self.assertTrue(lb.LAYER_RE.match(loose))
        self.write(loose)
        self.assertIsNone(art.find_layer_set(self.roots(), TOKEN))
        self.assertIsNone(art.find_layer_set(self.roots(), "deadbeef"))

    def test_a_real_qwen_image_is_never_a_set(self):
        for name in ("Qwen_image_2.1_00001.png", "qwen21_apple_00007.png"):
            with self.subTest(name=name):
                self.assertIsNone(lb.LAYER_RE.match(name))
                self.write(name)
        self.assertIsNone(art.find_layer_set(self.roots(), "00001"))

    def test_the_layers_json_itself_is_not_mistaken_for_a_layer(self):
        # `_layers.json` 不是 `.png`，所以它不会进画廊、也不该命中 `LAYER_RE`。
        self.assertIsNone(lb.LAYER_RE.match(self.info_name()))
        self.assertTrue(lb.LAYERS_JSON_RE.match(self.info_name()))


class Resolve(support.LeakCheckedTestCase):
    def test_dispatches_by_mode(self):
        empty = []
        self.assertIsNone(art.resolve("t2i", empty, TOKEN))
        self.assertIsNone(art.resolve("edit", empty, TOKEN))
        self.assertIsNone(art.resolve("upscale", empty, TOKEN))
        self.assertIsNone(art.resolve(lb.MODE, empty, TOKEN))
        self.assertIsNone(art.resolve("h3", empty, TOKEN))

    def test_mode_names_come_from_the_builders(self):
        # 字面量只此一处（两个构建器模块）—— 写死在 artifacts 里就迟早分家，
        # 而分家的症状是「作业永远找不到自己的产物」。
        import h3_build as h3b
        self.assertEqual(h3b.MODE, "h3")
        self.assertEqual(lb.MODE, "layers")


if __name__ == "__main__":
    unittest.main()
