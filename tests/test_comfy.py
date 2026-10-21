#!/usr/bin/env python3
"""`comfy.py` 的解析层与 HTTP 层。

HTTP 层用 `httpx.MockTransport` 拦在传输层 —— 测的是我们自己拼的请求体
和我们对响应的处理，不碰真实端口。有两处是**踩过坑才写下的断言**，别删：

- 提交体里必须同时有 `client_id` 和 `prompt_id`。前者决定 ComfyUI 把
  `execution_*` 事件发给谁（`execution.py:736`），后者决定 PNG 元数据、
  `/history`、探针报告能不能共用一个键。
- `ComfyRejected` 与 `ComfyUnreachable` 必须可区分：一个是「后端在、图不合法」
  （要报 node_errors 给用户改），一个是「后端没起」（要提示拉起）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import httpx  # noqa: E402
import support  # noqa: E402

from workbench import comfy, config  # noqa: E402


def client_with(handler):
    """把 ComfyClient 的传输层换成 MockTransport。"""
    cc = comfy.ComfyClient(base="http://testserver")
    cc._client.close()
    cc._client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="http://testserver", trust_env=False)
    return cc


def json_response(payload, status=200):
    return httpx.Response(status, json=payload)


class QueueIds(support.LeakCheckedTestCase):
    def test_parses_running_and_pending(self):
        cc = client_with(lambda r: json_response({
            "queue_running": [[0, "run-1", {}, {}, []]],
            "queue_pending": [[1, "wait-1", {}, {}, []], [2, "wait-2", {}, {}, []]],
        }))
        self.assertEqual(cc.queue_ids(),
                         {"run-1": "running", "wait-1": "pending", "wait-2": "pending"})

    def test_running_wins_over_pending(self):
        cc = client_with(lambda r: json_response({
            "queue_running": [[0, "same", {}, {}, []]],
            "queue_pending": [[1, "same", {}, {}, []]],
        }))
        self.assertEqual(cc.queue_ids(), {"same": "running"})

    def test_unreachable_returns_none(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        self.assertIsNone(client_with(boom).queue_ids())


class Probe(support.LeakCheckedTestCase):
    """三态探活：忙 ≠ 没起。

    实测背景：ComfyUI 冷加载 16 GiB 权重时 `/system_stats` 能超过 2 秒不答。
    早先一律返回 False，于是**正在跑任务**的后端被报成「未运行」，
    页面还提示用户去启动一个已经启动着的东西。
    """

    def test_true_on_200(self):
        self.assertIs(client_with(lambda r: json_response({})).probe(), True)

    def test_false_on_connect_error(self):
        """连接被拒 = 真没起。这是唯一可以说「未运行」的情形。"""
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        self.assertIs(client_with(boom).probe(), False)

    def test_none_on_read_timeout(self):
        """读超时 = 连上了但没答完 = 忙。**不能**当成没起。"""
        def slow(request):
            raise httpx.ReadTimeout("too slow", request=request)
        self.assertIsNone(client_with(slow).probe())

    def test_none_on_connect_timeout(self):
        def slow(request):
            raise httpx.ConnectTimeout("slow connect", request=request)
        self.assertIsNone(client_with(slow).probe())

    def test_none_on_500(self):
        """500 说明它在答话 —— 服务是活的，只是这个请求不行。"""
        self.assertIsNone(client_with(lambda r: json_response({}, 500)).probe())

    def test_probe_gives_connect_a_wider_budget_than_read(self):
        """**实测**：本机连一个没人监听的回环端口，`WSAECONNREFUSED` 要 ~2.1 秒
        才回来（raw socket / .NET / httpx 三路一致）。所以连接的预算必须比读的宽 ——
        否则会卡在拒绝到达之前超时，把「连接被拒」误判成「没答好」，
        而后者不算没起：结果是 probe() 永远不返回 False，
        页面永远说不出「后端未运行」，一键启动也永远拒绝。
        """
        seen = {}

        def handler(request):
            seen["timeout"] = request.extensions.get("timeout")
            return json_response({})

        cc = client_with(handler)
        cc.probe()
        t = seen["timeout"]
        self.assertIsNotNone(t, "应当给这次探测单独传一个超时")
        self.assertGreater(t["connect"], t["read"],
                           "连接的预算必须比读的宽（拒绝要 2 秒才到）")
        self.assertGreaterEqual(t["connect"], config.PROBE_CONNECT_TIMEOUT)
        self.assertEqual(t["read"], config.PROBE_TIMEOUT)

    def test_liveness_strings(self):
        """三种状态各有各的处置：拉后端 / 等一下 / 正常。"""
        self.assertEqual(client_with(lambda r: json_response({})).liveness(), "ok")

        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        self.assertEqual(client_with(boom).liveness(), "down")

        def slow(request):
            raise httpx.ReadTimeout("too slow", request=request)
        self.assertEqual(client_with(slow).liveness(), "unclear")
        self.assertEqual(
            client_with(lambda r: json_response({}, 500)).liveness(), "unclear")

    def test_running_treats_busy_as_running(self):
        def slow(request):
            raise httpx.ReadTimeout("too slow", request=request)
        cc = client_with(slow)
        self.assertTrue(cc.running())
        self.assertIsNone(cc.probe())

    def test_running_still_false_when_refused(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        self.assertFalse(client_with(boom).running())


class Running(support.LeakCheckedTestCase):
    def test_true_on_200(self):
        self.assertTrue(client_with(lambda r: json_response({})).running())

    def test_false_on_connect_error(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        self.assertFalse(client_with(boom).running())

    def test_true_on_500_because_the_process_is_alive(self):
        """**行为变更（P4）**：500 说明它在答话，进程活着。

        早先这里是 False，于是「后端活着但在报错」被报成「未运行」，
        页面会叫用户去启动一个已经在跑的东西，提交也被自己的检查挡掉。
        要判断「能不能提交」，5xx 与「没有监听」是两件事。
        """
        self.assertTrue(client_with(lambda r: json_response({}, 500)).running())


class Submit(support.LeakCheckedTestCase):
    def test_timeout_says_busy_not_unreachable(self):
        """**实测**：跑 2048² 时 ComfyUI 自己的 `/queue` 五秒都答不出来，
        而 GPU 是 100% 在算。这时说「连不上」会把用户指去查网络/重启后端，
        正确的做法是等这一张跑完。"""
        def slow(request):
            raise httpx.ReadTimeout("timed out", request=request)
        cc = client_with(slow)
        with self.assertRaises(comfy.ComfyUnreachable) as ctx:
            cc.submit({"1": {}}, "job-1", "cid")
        msg = str(ctx.exception)
        self.assertIn("多半正忙着", msg)
        self.assertNotIn("连不上", msg)

    def test_connect_error_says_unreachable(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        cc = client_with(boom)
        with self.assertRaises(comfy.ComfyUnreachable) as ctx:
            cc.submit({"1": {}}, "job-1", "cid")
        self.assertIn("连不上", str(ctx.exception))

    def capture(self, payload=None, status=200):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = __import__("json").loads(request.content)
            return json_response(payload if payload is not None else
                                 {"prompt_id": seen["body"]["prompt_id"],
                                  "number": 7}, status)
        return client_with(handler), seen

    def test_body_carries_client_id_and_prompt_id(self):
        cc, seen = self.capture()
        out = cc.submit({"459:458": {"class_type": "KSampler"}}, "job-abc", "cid-xyz")
        self.assertEqual(seen["body"]["client_id"], "cid-xyz")
        self.assertEqual(seen["body"]["prompt_id"], "job-abc")
        self.assertEqual(seen["body"]["prompt"], {"459:458": {"class_type": "KSampler"}})
        self.assertEqual(out["number"], 7)

    def test_rejection_raises_with_node_errors(self):
        cc, _ = self.capture({"error": {"type": "prompt_outputs_failed_validation",
                                        "message": "Prompt outputs failed validation",
                                        "details": "值不在列表里"},
                              "node_errors": {"459:451": {"errors": [{"message": "x"}]}}},
                             status=400)
        with self.assertRaises(comfy.ComfyRejected) as ctx:
            cc.submit({}, "job-1", "cid-1")
        self.assertIn("值不在列表里", str(ctx.exception))
        self.assertIn("459:451", ctx.exception.node_errors)

    def test_connect_error_is_unreachable_not_rejected(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        with self.assertRaises(comfy.ComfyUnreachable):
            client_with(boom).submit({}, "job-1", "cid-1")

    def test_non_json_body_is_unreachable(self):
        cc = client_with(lambda r: httpx.Response(200, text="<html>nope</html>"))
        with self.assertRaises(comfy.ComfyUnreachable):
            cc.submit({}, "job-1", "cid-1")

    def test_200_without_prompt_id_is_rejected(self):
        cc = client_with(lambda r: json_response({"number": 3}))
        with self.assertRaises(comfy.ComfyRejected):
            cc.submit({}, "job-1", "cid-1")


class ComboOptions(support.LeakCheckedTestCase):
    def object_info(self, node_class, field, options, wrap_meta=True):
        spec = [options, {"tooltip": "t"}] if wrap_meta else [options]
        return {node_class: {"input": {"required": {field: spec}}}}

    def test_reads_options_from_wrapped_spec(self):
        payload = self.object_info("UNETLoader", "unet_name", ["a.safetensors", "b"])
        cc = client_with(lambda r: json_response(payload))
        self.assertEqual(cc.combo_options("UNETLoader", "unet_name"),
                         ["a.safetensors", "b"])

    def test_reads_options_from_bare_spec(self):
        payload = self.object_info("UNETLoader", "unet_name", ["a", "b"], wrap_meta=False)
        cc = client_with(lambda r: json_response(payload))
        self.assertEqual(cc.combo_options("UNETLoader", "unet_name"), ["a", "b"])

    def test_unknown_node_returns_none_not_empty(self):
        cc = client_with(lambda r: json_response({}, 404))
        self.assertIsNone(cc.combo_options("Nope", "x"))


class ModelsReport(support.LeakCheckedTestCase):
    def test_all_present(self):
        listing = {
            "UNETLoader": ["qwen_image_2.1_int8_convrot.safetensors"],
            "CLIPLoader": ["qwen3vl_8b_int8_convrot.safetensors",
                           "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"],
            "VAELoader": ["qwen_image_2.1_vae_bf16.safetensors"],
        }

        def handler(request):
            name = request.url.path.rsplit("/", 1)[-1]
            if name == "UNETLoader":
                return json_response({"UNETLoader": {"input": {"required": {
                    "unet_name": [listing["UNETLoader"], {}]}}}})
            if name == "CLIPLoader":
                return json_response({"CLIPLoader": {"input": {"required": {
                    "clip_name": [listing["CLIPLoader"], {}]}}}})
            if name == "VAELoader":
                return json_response({"VAELoader": {"input": {"required": {
                    "vae_name": [listing["VAELoader"], {}]}}}})
            return json_response({}, 404)

        report = client_with(handler).models_report()
        self.assertEqual(set(report), {"dit", "text_encoder", "rewriter", "vae"})
        for key, entry in report.items():
            self.assertIs(entry["present"], True, key)

    def test_missing_file_is_false_not_unknown(self):
        def handler(request):
            if request.url.path.endswith("VAELoader"):
                return json_response({"VAELoader": {"input": {"required": {
                    "vae_name": [["something_else.safetensors"], {}]}}}})
            return json_response({}, 404)

        report = client_with(handler).models_report()
        self.assertIs(report["vae"]["present"], False)
        # 读不到候选表的那些是「未知」，不是「缺失」—— 两种病要分开报
        self.assertIsNone(report["dit"]["present"])

    def test_extra_entries_ride_along_with_the_same_shape(self):
        """可选权重（Turbo LoRA）与那四个**走同一条路**，形状必须一模一样。

        它在 server 那边会走同一句 error 文案（「后端看不到这些模型文件：…」），
        形状分家就会变成两种说法。另外这里钉住**参数形状**：传的是一张表的**列表**。

        ⚠ `entries` 是**整份替换**，不是追加 —— 所以「并进那四个」这件事发生在
        **调用方**（server 里 `config.REQUIRED_MODELS + turbo_extra`）。
        把 `entries` 当追加用会静默多查一遍 Qwen 那四个，而那正是这次改动踩到的坑：
        拆层体检会因此报「后端看不到这些模型文件」，全是假警报。
        """
        extra = (("turbo_lora", "LoraLoaderModelOnly", "lora_name",
                  "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"),)
        entries = tuple(config.REQUIRED_MODELS) + extra

        def handler(request):
            if request.url.path.endswith("LoraLoaderModelOnly"):
                return json_response({"LoraLoaderModelOnly": {"input": {"required": {
                    "lora_name": [["qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"],
                                  {}]}}}})
            return json_response({}, 404)

        report = client_with(handler).models_report(entries)
        self.assertIn("turbo_lora", report)
        self.assertEqual(sorted(report), ["dit", "rewriter", "text_encoder",
                                          "turbo_lora", "vae"])
        entry = report["turbo_lora"]
        self.assertIs(entry["present"], True)
        self.assertEqual(entry["filename"],
                         "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors")
        # 那四个的条目形状一字未变
        for key in ("dit", "text_encoder", "rewriter", "vae"):
            self.assertEqual(sorted(report[key]),
                             ["field", "filename", "node_class", "note",
                              "option_count", "present"])

    def test_entries_replaces_the_default_table_entirely(self):
        """★ **整份替换**：给了 `entries` 就**不再**查 `REQUIRED_MODELS`。

        这是 H3 与拆层那条路依赖的语义 —— 它们各自那套权重里没有一个是 Qwen 的，
        带上那四个就是四条假警报。
        """
        entries = (("h3_dit", "UNETLoader", "unet_name", "h3.safetensors"),)

        def handler(request):
            if request.url.path.endswith("UNETLoader"):
                return json_response({"UNETLoader": {"input": {"required": {
                    "unet_name": [["h3.safetensors"], {}]}}}})
            return json_response({}, 404)

        report = client_with(handler).models_report(entries)
        self.assertEqual(sorted(report), ["h3_dit"])
        self.assertIs(report["h3_dit"]["present"], True)

    def test_empty_entries_means_check_nothing(self):
        # 放大那条就是靠这个：VOSR2 的权重判据是插件自己的候选表，
        # 四个 Qwen 模型一个都不用 —— 传空表就是为了「一个都不查」。
        report = client_with(lambda r: json_response({}, 404)).models_report(())
        self.assertEqual(report, {})

    def test_extra_entry_missing_is_false_not_unknown(self):
        """权重下没下是**两个不同的答案**：`False` 去下文件，`None` 去拉后端。"""
        entries = (("turbo_lora", "LoraLoaderModelOnly", "lora_name",
                    "turbo.safetensors"),)

        def handler(request):
            if request.url.path.endswith("LoraLoaderModelOnly"):
                return json_response({"LoraLoaderModelOnly": {"input": {"required": {
                    "lora_name": [["别的.safetensors"], {}]}}}})
            return json_response({}, 404)

        report = client_with(handler).models_report(entries)
        self.assertIs(report["turbo_lora"]["present"], False)

    def test_no_extra_leaves_the_report_exactly_as_before(self):
        """不勾的时候就该**一个字都不多** —— 没下 LoRA 的人不该因为这个旋钮
        多出一项体检（那会让他的体检永远显示缺一个文件）。"""
        report = client_with(lambda r: json_response({}, 404)).models_report()
        self.assertEqual(sorted(report),
                         ["dit", "rewriter", "text_encoder", "vae"])

    def test_unreachable_backend_reports_unknown_everywhere(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        report = client_with(boom).models_report()
        self.assertTrue(all(v["present"] is None for v in report.values()))


class HistoryParsing(support.LeakCheckedTestCase):
    def test_outputs_from_history(self):
        entry = support.history_entry("p1")
        out = comfy.outputs_from_history(entry)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["filename"], "Qwen_image_2.1_00042.png")
        self.assertEqual(out[0]["type"], "output")

    def test_outputs_ignores_non_image_nodes(self):
        entry = {"outputs": {"459:472": {"text": ["hi"]},
                             "461": {"images": [{"filename": "a.png"}]}}}
        self.assertEqual([o["filename"] for o in comfy.outputs_from_history(entry)],
                         ["a.png"])

    def test_outputs_empty_on_garbage(self):
        self.assertEqual(comfy.outputs_from_history(None), [])
        self.assertEqual(comfy.outputs_from_history({"outputs": "nope"}), [])

    def test_status_done(self):
        status, err = comfy.history_status(support.history_entry("p1"))
        self.assertEqual(status, "done")
        self.assertIsNone(err)

    def test_status_error_keeps_node_type_from_prompt(self):
        entry = support.history_entry("p1", error={"message": "CUDA out of memory"})
        status, err = comfy.history_status(entry)
        self.assertEqual(status, "error")
        self.assertIn("CUDA out of memory", err["message"])

    def test_status_pending_is_none(self):
        status, err = comfy.history_status(
            {"status": {"completed": False, "status_str": "success",
                        "messages": [["execution_start", {"timestamp": 1}]]}})
        self.assertIsNone(status)
        self.assertIsNone(err)

    def test_status_interrupted(self):
        status, _ = comfy.history_status(
            support.history_entry("p1", interrupted=True))
        self.assertEqual(status, "cancelled")

    def test_timestamps_use_official_clock(self):
        start, end = comfy.history_timestamps(
            support.history_entry("p1", start=1000, end=51000))
        self.assertEqual((start, end), (1000, 51000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
