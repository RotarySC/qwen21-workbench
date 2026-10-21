#!/usr/bin/env python3
"""参考图上传 —— 名字收束、落点判定、以及「后端到底读没读到」。

这一期的核心不是「文件写成功了」，而是**写对了目录**。所以这里最重的几项
都在测「对账」：候选表对得上才算 verified，对不上必须如实说，不许猜。
"""
import mimetypes
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402

from workbench import comfy, gallery, upload  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend-this-is-a-png" * 4


class UploadBase(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()   # 启动临时目录泄漏追踪
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-upload-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def input_dir(self, *names, name="input"):
        """造一个真的目录，放进真的文件 —— 对账是按文件名比的，得有真文件。

        `name` 可换，因为「两个候选目录」这种场景需要两个**不同**的目录。
        """
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        for n in names:
            with open(os.path.join(d, n), "wb") as f:
                f.write(PNG)
        return d

    def client_for(self, d, options=None, argv=None):
        """argv 里带 `--input-directory` —— 这是最权威的落点来源（同 output）。"""
        argv = argv if argv is not None else ["main.py", "--input-directory", d]
        return support.FakeComfy(
            input_dir=d,
            input_options=list(options) if options is not None else None,
            argv=argv)


class SafeName(UploadBase):
    def test_plain_name_passes(self):
        self.assertEqual(upload.safe_name("ref.png"), "ref.png")

    def test_uppercase_extension_passes(self):
        # ComfyUI 的 mimetypes 过滤会 lower() 之后比对，大写后缀照样能选
        self.assertEqual(upload.safe_name("REF.PNG"), "REF.PNG")

    def test_directory_part_is_stripped_not_rejected(self):
        # 浏览器偶尔会给出带路径的 name；剥掉目录即可，不该报错
        self.assertEqual(upload.safe_name("C:\\photos\\ref.png"), "ref.png")
        self.assertEqual(upload.safe_name("/home/me/ref.png"), "ref.png")

    def test_traversal_cannot_survive(self):
        # 断言的是**效果**（拼出来还在根里），不是子串 ——
        # `..⁄ref.png`（U+2044）不是路径分隔符，它会作为一个普通文件名留下来，
        # 里面含有 ".." 字样但完全无害。
        root = os.path.join(self.tmp, "root")
        for bad in ("../ref.png", "..\\ref.png", "a/../../ref.png",
                    "..\u2044ref.png", "..\\..\\..\\ref.png"):
            with self.subTest(bad=bad):
                got = upload.safe_name(bad)
                self.assertNotIn("/", got)
                self.assertNotIn("\\", got)
                full = os.path.normcase(os.path.abspath(os.path.join(root, got)))
                self.assertEqual(os.path.dirname(full),
                                 os.path.normcase(os.path.abspath(root)))

    def test_bare_dot_names_rejected(self):
        for bad in ("", "   ", ".", "..", "...", "a/b/", "/"):
            with self.assertRaises(upload.UploadError):
                upload.safe_name(bad)

    def test_non_string_rejected(self):
        for bad in (None, 7, ["ref.png"], {"a": 1}):
            with self.assertRaises(upload.UploadError):
                upload.safe_name(bad)

    def test_illegal_characters_rejected(self):
        for bad in ('a:b.png', "a?b.png", "a*b.png", "a|b.png",
                    'a"b.png', "a<b.png", "a\0b.png", "a\nb.png"):
            with self.assertRaises(upload.UploadError):
                upload.safe_name(bad)

    def test_trailing_dot_rejected_but_trailing_space_is_normalized(self):
        """两者的处理**不同**，而且是有理由的不同的。

        点：Windows 会静默去掉，于是「写进去的名字」和「读出来的名字」不是同一个，
        而这种不一致最难查 —— 所以在门口挡掉。
        空格：浏览器与用户都不会给，来了就 strip 掉即可，报错反而更烦人。
        """
        self.assertEqual(upload.safe_name("ref.png "), "ref.png")
        self.assertEqual(upload.safe_name("  ref.png  "), "ref.png")
        for bad in ("ref.png.", "ref.png. "):
            with self.assertRaises(upload.UploadError):
                upload.safe_name(bad)

    def test_overlong_name_rejected(self):
        with self.assertRaises(upload.UploadError):
            upload.safe_name("a" * (upload.MAX_NAME + 1) + ".png")
        # 刚好卡在上限里的要能过
        ok = "a" * (upload.MAX_NAME - len(".png")) + ".png"
        self.assertEqual(len(ok), upload.MAX_NAME)
        self.assertEqual(upload.safe_name(ok), ok)

    def test_non_image_extension_rejected(self):
        for bad in ("notes.txt", "weights.safetensors", "x.exe", "x.part", "noext"):
            with self.assertRaises(upload.UploadError):
                upload.safe_name(bad)


class ExtensionAgreementWithComfyUI(support.LeakCheckedTestCase):
    """我们的白名单必须与 ComfyUI 的过滤口径一致。

    ComfyUI 的 `LoadImage` 用 `filter_files_content_types(files, ["image"])`，
    它内部就是 `mimetypes.guess_type`。口径一旦漂开，症状是
    「上传成功、后端看不见」—— 正是这一期要消灭的那种静默失败。
    """

    def test_every_allowed_extension_is_image_per_mimetypes(self):
        for ext in upload.IMAGE_EXTS:
            with self.subTest(ext=ext):
                kind = mimetypes.guess_type("x" + ext, strict=False)[0]
                self.assertTrue(kind and kind.startswith("image/"),
                                "%s 在 mimetypes 里不是 image/*，与 ComfyUI 口径不一致"
                                % ext)

    def test_is_image_name_follows_mimetypes(self):
        self.assertTrue(upload.is_image_name("a.png"))
        self.assertTrue(upload.is_image_name("a.JPEG"))
        self.assertFalse(upload.is_image_name("a.txt"))
        self.assertFalse(upload.is_image_name("a.part"))
        self.assertFalse(upload.is_image_name("a"))
        self.assertFalse(upload.is_image_name(""))


class ResolveRoot(UploadBase):
    def test_argv_input_directory_wins_and_is_verified_by_reconciliation(self):
        d = self.input_dir("a.png", "b.png")
        c = self.client_for(d, options=["a.png", "b.png"])
        got = upload.resolve(client=c)
        self.assertEqual(os.path.normcase(got["path"]), os.path.normcase(d))
        self.assertTrue(got["verified"])
        self.assertIn("逐名一致", got["how"])

    def test_a_different_directory_is_not_claimed_as_verified(self):
        """候选表非空、但没有一个候选目录对得上 —— 后端读的是第三个目录。

        这时**任何**候选都是猜的，必须返回 verified=False 而不是「最像的那个」。
        """
        d = self.input_dir("a.png")
        c = self.client_for(d, options=["别的图.png"])
        got = upload.resolve(client=c)
        self.assertFalse(got["verified"])
        self.assertIn("第三个 input 目录", got["how"])

    def test_empty_candidate_table_is_not_laundered_into_verification(self):
        """空集对空集「相等」，但那是没信息，不是证据。"""
        d = self.input_dir()
        c = self.client_for(d, options=[])
        got = upload.resolve(client=c)
        self.assertFalse(got["verified"])
        self.assertIn("空", got["how"])

    def test_unreadable_table_says_so_and_falls_back_to_trust_order(self):
        d = self.input_dir("a.png")
        c = self.client_for(d, options=None)
        got = upload.resolve(client=c)
        self.assertEqual(os.path.normcase(got["path"]), os.path.normcase(d))
        self.assertFalse(got["verified"])
        self.assertIn("读不到", got["how"])

    def test_backend_down_does_not_raise(self):
        c = support.FakeComfy(running=False, input_options=None)
        got = upload.resolve(client=c)
        self.assertFalse(got["verified"])
        self.assertIsInstance(got["how"], str)

    def test_no_argv_uses_the_install_root_second(self):
        """没有 `--input-directory` 时，铁律 1 的安装目录是第二选择。"""
        fake_install = os.path.join(self.tmp, "ComfyUI-Installs", "ComfyUI")
        d = self.input_dir("a.png")
        with mock.patch.object(gallery, "desktop_install_root",
                               lambda path=None: fake_install):
            roots = upload.root_candidates(stats={"system": {"argv": ["main.py"]}})
        paths = [os.path.normcase(r["path"]) for r in roots]
        self.assertIn(os.path.normcase(os.path.join(fake_install, "ComfyUI", "input")),
                      paths)
        # argv 没给目录，所以第一项就是安装目录，不是 argv
        self.assertEqual(roots[0]["kind"], "manual")
        self.assertTrue(d)          # 用了 setUp 的 tmp，纯粹为了避免空跑告警

    def test_candidates_are_deduplicated(self):
        d = self.input_dir("a.png")
        same = os.path.join(d, ".", "")
        with mock.patch.object(gallery, "desktop_install_root", lambda path=None: d):
            roots = upload.root_candidates(stats={"system": {
                "argv": ["main.py", "--input-directory", same]}})
        normed = [os.path.normcase(os.path.normpath(r["path"])) for r in roots]
        self.assertEqual(len(normed), len(set(normed)))

    def test_equals_form_of_the_flag_is_read(self):
        d = self.input_dir("a.png")
        roots = upload.root_candidates(
            stats={"system": {"argv": ["main.py", "--input-directory=" + d]}})
        self.assertEqual(os.path.normcase(roots[0]["path"]), os.path.normcase(d))

    def test_underscore_spelling_of_the_flag_is_read(self):
        d = self.input_dir("a.png")
        roots = upload.root_candidates(stats={"system": {
            "argv": ["main.py", "--input_directory", d]}})
        self.assertEqual(os.path.normcase(roots[0]["path"]), os.path.normcase(d))


class ListInputs(UploadBase):
    def test_lists_only_images_and_reports_where(self):
        d = self.input_dir("a.png", "b.jpg")
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("not an image")
        c = self.client_for(d, options=["a.png", "b.jpg"])
        data = upload.list_inputs(client=c)
        self.assertEqual(data["files"], ["a.png", "b.jpg"])
        self.assertTrue(data["verified"])
        self.assertEqual(data["backend_files"], ["a.png", "b.jpg"])

    def test_missing_directory_is_none_not_empty(self):
        """「读不到目录」和「目录是空的」是两种病，不能都报成 0 张。"""
        gone = os.path.join(self.tmp, "nope")
        c = support.FakeComfy(input_options=None,
                              argv=["main.py", "--input-directory", gone])
        data = upload.list_inputs(client=c)
        self.assertIsNone(data["files"])
        self.assertFalse(data["root_exists"])

    def test_candidate_list_is_exposed_for_diagnosis(self):
        d = self.input_dir("a.png")
        c = self.client_for(d, options=["a.png"])
        data = upload.list_inputs(client=c)
        self.assertTrue(data["candidates"])
        self.assertIn("path", data["candidates"][0])
        self.assertIn(".png", data["extensions"])


class Save(UploadBase):
    def test_writes_and_reports_size(self):
        d = self.input_dir()
        out = upload.save(d, "ref.png", PNG)
        self.assertEqual(out["filename"], "ref.png")
        self.assertEqual(out["bytes"], len(PNG))
        self.assertFalse(out["existed"])
        self.assertFalse(out["replaced"])
        with open(os.path.join(d, "ref.png"), "rb") as f:
            self.assertEqual(f.read(), PNG)

    def test_creates_a_missing_directory(self):
        d = os.path.join(self.tmp, "made", "up")
        out = upload.save(d, "ref.png", PNG)
        self.assertTrue(os.path.isfile(out["path"]))

    def test_replace_is_reported(self):
        d = self.input_dir()
        upload.save(d, "ref.png", PNG)
        out = upload.save(d, "ref.png", PNG + b"different")
        self.assertTrue(out["existed"])
        self.assertTrue(out["replaced"])
        self.assertEqual(out["old_bytes"], len(PNG))

    def test_refuses_replace_when_overwrite_is_off(self):
        d = self.input_dir()
        upload.save(d, "ref.png", PNG)
        with self.assertRaises(upload.UploadError):
            upload.save(d, "ref.png", PNG, overwrite=False)

    def test_empty_body_rejected(self):
        with self.assertRaises(upload.UploadError):
            upload.save(self.input_dir(), "ref.png", b"")

    def test_oversize_rejected_before_writing_anything(self):
        d = self.input_dir()
        with mock.patch.object(upload, "MAX_BYTES", 64):
            with self.assertRaises(upload.UploadError):
                upload.save(d, "ref.png", b"x" * 65)
        self.assertFalse(os.path.exists(os.path.join(d, "ref.png")))

    def test_traversal_never_escapes_the_root(self):
        d = self.input_dir()
        with self.assertRaises(upload.UploadError):
            # safe_name 会把它剥成 ref.png，所以这里换一个能穿过第一道防线的
            upload.save(d, ".." , PNG)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(d), "ref.png")))

    def test_atomic_write_leaves_no_temp_files(self):
        d = self.input_dir()
        upload.save(d, "ref.png", PNG)
        leftovers = [n for n in os.listdir(d) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_root_none_is_an_error_not_a_crash(self):
        with self.assertRaises(upload.UploadError):
            upload.save(None, "ref.png", PNG)


class UploadEndToEnd(UploadBase):
    def test_verified_only_when_the_backend_lists_it(self):
        d = self.input_dir("old.png")
        c = self.client_for(d, options=["old.png"])
        self.assertFalse(upload.upload(c, "new.png", PNG)["verified"])
        # 后端「读到」新文件之后才算数
        c.input_options = ["old.png", "new.png"]
        out = upload.upload(c, "new.png", PNG)
        self.assertTrue(out["verified"])
        self.assertIsNone(out["warning"])

    def test_writes_into_the_directory_the_backend_reads(self):
        """写错目录是静默的 —— 这条测的就是「写对了」。"""
        d = self.input_dir("a.png")
        other = self.input_dir("b.png", name="input-other")   # 另一个候选目录
        c = self.client_for(d, options=["a.png"])
        out = upload.upload(c, "new.png", PNG)
        self.assertEqual(os.path.normcase(out["root"]), os.path.normcase(d))
        self.assertTrue(os.path.isfile(os.path.join(d, "new.png")))
        self.assertFalse(os.path.exists(os.path.join(other, "new.png")))

    def test_unverified_upload_explains_itself(self):
        other = self.input_dir("b.png")
        c = support.FakeComfy(input_options=["something-else.png"],
                              argv=["main.py", "--input-directory", other])
        out = upload.upload(c, "new.png", PNG)
        self.assertFalse(out["verified"])
        self.assertIn("候选表", out["warning"])
        # 文件本身确实写下来了 —— 这不是「失败」，是「没能证明」
        self.assertTrue(os.path.isfile(os.path.join(other, "new.png")))

    def test_backend_unreachable_still_writes_and_says_it_could_not_check(self):
        d = self.input_dir()
        c = self.client_for(d, options=None)
        out = upload.upload(c, "new.png", PNG)
        self.assertFalse(out["verified"])
        self.assertIn("无法证明", out["warning"])
        self.assertTrue(os.path.isfile(os.path.join(d, "new.png")))

    def test_bad_name_raises_before_touching_the_disk(self):
        d = self.input_dir()
        c = self.client_for(d, options=[])
        with self.assertRaises(upload.UploadError):
            upload.upload(c, "notes.txt", PNG)
        self.assertEqual(os.listdir(d), [])


class UploadAny(UploadBase):
    """`upload_any` —— 默认入口：**先请后端写**，不行才退回本地写。

    这两条路的分工不是「哪个快」，而是**哪个能写**：工作台跑在工作区解释器上，
    那条按可执行文件位置判定的沙箱规则会让它写不了 ComfyUI 的 `input/`（实测
    `PermissionError`）。所以「请后端写」是主路，本地写是兜底 —— 而不是反过来。
    """

    def test_the_backend_does_the_write_by_default(self):
        d = self.input_dir()
        c = self.client_for(d, options=[])
        out = upload.upload_any(c, "new.png", PNG)
        self.assertEqual(out["via"], "backend")
        self.assertTrue(os.path.isfile(os.path.join(d, "new.png")))
        # 落点**不报**：文件在 ComfyUI 进程的目录里，我们只有一个文件名。
        # 猜一个填进去就是假自信（这个模块一直在消灭的东西）。
        self.assertIsNone(out["root"])
        self.assertIsNone(out["path"])
        self.assertEqual([n for n, _ in c.uploaded], ["new.png"])

    def test_the_backend_path_still_proves_the_backend_can_see_it(self):
        d = self.input_dir()
        c = self.client_for(d, options=[])
        self.assertFalse(upload.upload_any(c, "new.png", PNG)["verified"])
        c.input_options = ["new.png"]
        out = upload.upload_any(c, "new.png", PNG)
        self.assertTrue(out["verified"])
        self.assertIsNone(out["warning"])

    def test_replacing_an_existing_name_is_reported_from_the_before_table(self):
        """`existed` 是拿**上传之前**那张候选表算的 —— 替身刻意不更新它，
        因为真后端也不会在你刚写完的那一刻就改 `object_info`。"""
        d = self.input_dir("old.png")
        c = self.client_for(d, options=["old.png"])
        out = upload.upload_any(c, "old.png", PNG)
        self.assertEqual(out["via"], "backend")
        self.assertTrue(out["replaced"])

    def test_an_unknown_existence_is_none_not_a_guess(self):
        """候选表读不到时 `existed` 是 None（不知道），不是 False（没这个文件）。"""
        d = self.input_dir()
        c = self.client_for(d, options=None)
        out = upload.upload_any(c, "new.png", PNG)
        self.assertIsNone(out["existed"])
        self.assertFalse(out["replaced"])

    def test_it_falls_back_to_the_local_write_when_the_backend_is_down(self):
        d = self.input_dir()
        c = support.FakeComfy(running=False, input_dir=d, input_options=None,
                              argv=["main.py", "--input-directory", d])
        out = upload.upload_any(c, "new.png", PNG)
        self.assertEqual(out["via"], "local")
        self.assertTrue(os.path.isfile(os.path.join(d, "new.png")))
        # 失败的那条路的原因要带回来：否则用户只知道「写不了 input 目录」，
        # 不知道我们其实先请后端写过一遍。
        self.assertIn("没起", out["backend_error"])

    def test_it_falls_back_when_the_backend_has_no_such_endpoint(self):
        d = self.input_dir()
        c = self.client_for(d, options=[])
        c.upload_image = mock.Mock(
            side_effect=comfy.ComfyUploadFailed("HTTP 404"))
        out = upload.upload_any(c, "new.png", PNG)
        self.assertEqual(out["via"], "local")
        self.assertIn("404", out["backend_error"])
        self.assertTrue(os.path.isfile(os.path.join(d, "new.png")))

    def test_when_both_attempts_fail_the_message_carries_both_reasons(self):
        """两条都没成时，只报本地那条会把第一现场藏起来。"""
        d = self.input_dir()
        c = self.client_for(d, options=[])
        c.upload_image = mock.Mock(
            side_effect=comfy.ComfyUnreachable("后端没起"))
        with mock.patch.object(upload, "save",
                               side_effect=PermissionError("拒写")):
            with self.assertRaises(OSError) as cm:
                upload.upload_any(c, "new.png", PNG)
        msg = str(cm.exception)
        self.assertIn("拒写", msg)
        self.assertIn("后端没起", msg)

    def test_refusing_overwrite_still_refuses_on_the_backend_path(self):
        """「不许覆盖」只有我们实现得了：ComfyUI 撞上同名会**改名**。
        所以先拿候选表判一道，判到了就当场拒。"""
        d = self.input_dir("a.png")
        c = self.client_for(d, options=["a.png"])
        with self.assertRaises(upload.UploadError) as cm:
            upload.upload_any(c, "a.png", PNG, overwrite=False)
        self.assertIn("同名", str(cm.exception))
        self.assertEqual([n for n, _ in getattr(c, "uploaded", [])], [])

    def test_a_stale_table_lets_the_backend_rename_and_we_report_it(self):
        """候选表**陈旧**（文件在磁盘上、表里没有）时走后端那条路，
        而它的处理是改名 —— 这时必须以后端回执里的名字为准。"""
        d = self.input_dir("a.png")
        c = self.client_for(d, options=[])
        out = upload.upload_any(c, "a.png", PNG, overwrite=False)
        self.assertEqual(out["filename"], "a (1).png")
        self.assertEqual(out["renamed_from"], "a.png")

    def test_bad_names_are_rejected_before_any_write(self):
        d = self.input_dir()
        c = self.client_for(d, options=[])
        with self.assertRaises(upload.UploadError):
            upload.upload_any(c, "notes.txt", PNG)
        self.assertEqual(os.listdir(d), [])
        self.assertEqual(getattr(c, "uploaded", []), [])

    def test_empty_and_oversize_are_rejected_on_both_paths(self):
        d = self.input_dir()
        c = self.client_for(d, options=[])
        with self.assertRaises(upload.UploadError):
            upload.upload_any(c, "new.png", b"")
        with mock.patch.object(upload, "MAX_BYTES", 8):
            with self.assertRaises(upload.UploadError):
                upload.upload_any(c, "new.png", b"x" * 64)
        self.assertEqual(os.listdir(d), [])


class MultipartBody(support.LeakCheckedTestCase):
    """自己拼的那个 multipart —— 只有几行，但拼错了后端会当场 400。"""

    def test_fields_and_file_are_wrapped(self):
        ctype, body = comfy.multipart_body(
            fields=[("overwrite", "true")],
            files=[("image", "a.png", b"BYTES", "image/png")],
            boundary="BOUND")
        self.assertEqual(ctype, "multipart/form-data; boundary=BOUND")
        text = body.decode("utf-8", "replace")
        self.assertIn('Content-Disposition: form-data; name="overwrite"', text)
        self.assertIn("true", text)
        self.assertIn('name="image"; filename="a.png"', text)
        self.assertIn("Content-Type: image/png", text)
        self.assertIn("BYTES", text)
        self.assertTrue(text.endswith("--BOUND--\r\n"))

    def test_the_boundary_is_fresh_every_call(self):
        """固定串万一出现在图片字节里就会把这一段截断。"""
        _, a = comfy.multipart_body(files=[("image", "a.png", b"x", "image/png")])
        _, b = comfy.multipart_body(files=[("image", "a.png", b"x", "image/png")])
        self.assertNotEqual(a, b)

    def test_a_utf8_filename_goes_out_verbatim(self):
        """实测（curl -F）：aiohttp 那边收得到中文名，磁盘上也是原名，
        所以不用 RFC 2231 那套 `filename*=` 绕法。"""
        _, body = comfy.multipart_body(
            files=[("image", "探针.png", b"x", "image/png")], boundary="B")
        self.assertIn('filename="探针.png"', body.decode("utf-8"))


class ReadReference(UploadBase):
    """把参考图的**字节**取回来给 LLM 看（P20e）—— 三条判定 + 两种不许猜的情形。

    这一层的每一个 `raise` 都在挡同一件事：让「图没上车」的那一次请求**发出去**。
    实测（`logs/llm_vision_probe.log`）问一个**没有图**的模型「这张图里有什么」，
    它会理直气壮地编一张出来，而那段编出来的描述会被「采用」写进提示词。
    """

    def test_a_verified_root_is_read_straight(self):
        d = self.input_dir("ref.png")
        c = self.client_for(d, options=["ref.png"])
        got = upload.read_reference("ref.png", client=c)
        self.assertEqual(got["data"], PNG)
        self.assertTrue(got["verified"])
        self.assertEqual(os.path.normcase(got["root"]), os.path.normcase(d))

    def test_a_unique_hit_is_read_but_never_called_verified(self):
        """后端读不到候选表（没起 / 老版本）时这条路仍然要能用：
        **只有一个**候选目录里有它，就读它 —— 但如实标成未验证，
        不把推断说成事实（`resolve` 的规矩，这里照旧）。"""
        inst = os.path.join("inst", "ComfyUI", "input")
        d = self.input_dir("only-here.png", name=inst)
        other = self.input_dir(name="argvinput")          # argv 指着它，里面没有它
        c = self.client_for(other, options=None)
        with mock.patch.object(gallery, "desktop_install_root",
                               lambda path=None: os.path.join(self.tmp, "inst")):
            got = upload.read_reference("only-here.png", client=c)
        self.assertEqual(got["data"], PNG)
        self.assertFalse(got["verified"])
        self.assertEqual(os.path.normcase(got["root"]), os.path.normcase(d))

    def test_two_copies_of_the_same_name_refuse_to_guess(self):
        """`input/` 实测有双胞胎，两边都有这个名字时**必须抛**。

        猜错的后果不是「报错」，是**让模型看着另一张图写提示词** ——
        出图能出、图也对，只是描述的是另一张：没有症状。
        """
        d1 = self.input_dir("same.png", name=os.path.join("inst", "ComfyUI", "input"))
        d2 = self.input_dir("same.png", name="argvinput")
        c = self.client_for(d2, options=None)
        with mock.patch.object(gallery, "desktop_install_root",
                               lambda path=None: os.path.join(self.tmp, "inst")):
            with self.assertRaises(upload.UploadError) as cm:
                upload.read_reference("same.png", client=c)
        msg = str(cm.exception)
        self.assertIn("same.png", msg)
        self.assertIn("2 个 input 目录", msg)
        self.assertTrue(d1 and d2)          # 两个目录都真的建出来了

    def test_a_missing_file_names_the_file_it_could_not_find(self):
        """面板里那一行要说得出**是哪一张** —— 参考图常常挂着三五张。"""
        d = self.input_dir("a.png")
        c = self.client_for(d, options=["a.png"])
        with self.assertRaises(upload.UploadError) as cm:
            upload.read_reference("nope.png", client=c)
        self.assertIn("nope.png", str(cm.exception))

    def test_a_missing_file_with_nothing_verified_still_says_which_one(self):
        c = support.FakeComfy(running=False, input_options=None)
        with self.assertRaises(upload.UploadError) as cm:
            upload.read_reference("nowhere.png", client=c)
        self.assertIn("nowhere.png", str(cm.exception))

    def test_a_non_image_name_is_refused_at_the_door(self):
        """与上传走同一道 `safe_name` —— 两条路的「什么算图」不许分家。"""
        c = self.client_for(self.input_dir("a.png"), options=["a.png"])
        with self.assertRaises(upload.UploadError):
            upload.read_reference("notes.txt", client=c)

    def test_an_oversize_file_is_refused(self):
        d = self.input_dir("big.png")
        c = self.client_for(d, options=["big.png"])
        with mock.patch.object(upload, "MAX_BYTES", 8):
            with self.assertRaises(upload.UploadError) as cm:
                upload.read_reference("big.png", client=c)
        self.assertIn("big.png", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
