#!/usr/bin/env python3
"""日志台（P20）—— 环、事件分级、logging 桥、以及 `/api/logs` 的游标语义。

三层分开验：

- `LogBuffer` / `classify_event`：纯函数，不碰服务；
- `BufferHandler`：装到 root logger 上，验「记录进得来、访问日志进不来」；
- `/api/logs`：`TestClient` 打真路由，验**游标**与**重启**两件事
  （这两件错了都会表现为「页面上一片空白」，而空白看起来像「什么都没发生」）。

刻意**不进** `with TestClient(app)`：那会跑 lifespan、起 Watcher 线程去连真实
8188，于是测试期间会真有事件进环（本机后端确实在跑）—— 断言就会飘。
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import support  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from workbench import launch, logbuf, server  # noqa: E402


class LogBufferTest(unittest.TestCase):
    """环本身：满了丢最旧的、游标只给新的、空行不记。"""

    def test_it_drops_the_oldest_when_full(self):
        buf = logbuf.LogBuffer(capacity=3)
        for i in range(5):
            buf.append("info", "test", "第 %d 条" % i)
        rows = buf.rows()
        self.assertEqual([r["text"] for r in rows],
                         ["第 2 条", "第 3 条", "第 4 条"])
        # seq **只增不减**：页面手里的游标靠它比较，重编号会让旧游标突然有效
        self.assertEqual([r["seq"] for r in rows], [3, 4, 5])
        self.assertEqual(buf.last_seq, 5)

    def test_since_gives_only_newer_rows(self):
        buf = logbuf.LogBuffer()
        buf.append("info", "test", "一")
        buf.append("info", "test", "二")
        cursor = buf.last_seq
        buf.append("info", "test", "三")
        self.assertEqual([r["text"] for r in buf.rows(since=cursor)], ["三"])

    def test_a_cursor_past_the_end_gives_everything(self):
        """服务重启过：环里的 seq 从 1 重来，而页面手里还攥着旧的大号游标。

        这时**必须回全量**。只按 `seq > since` 过滤的话，页面会永远收到空列表，
        而那看起来就是「这段时间什么都没发生」—— 本工作区最忌讳的那种错。
        """
        buf = logbuf.LogBuffer()
        buf.append("info", "test", "重启之后的第一条")
        self.assertEqual(len(buf.rows(since=99999)), 1)

    def test_the_level_filter_keeps_the_heavier_ones(self):
        buf = logbuf.LogBuffer()
        for level in ("info", "warn", "error"):
            buf.append(level, "test", level)
        self.assertEqual([r["level"] for r in buf.rows(level="warn")],
                         ["warn", "error"])
        self.assertEqual([r["level"] for r in buf.rows(level="error")], ["error"])

    def test_an_empty_row_is_not_recorded(self):
        buf = logbuf.LogBuffer()
        self.assertIsNone(buf.append("info", "test", "   "))
        self.assertEqual(buf.rows(), [])


class EventTimelineTest(unittest.TestCase):
    """事件 → 时间线。级别表在后端，前端只负责画。"""

    def level(self, event):
        row = logbuf.classify_event(event)
        return None if row is None else row[0]

    def text(self, event):
        return logbuf.classify_event(event)[1]

    def test_progress_and_node_and_status_stay_out(self):
        """这三类**刻意不进**时间线。

        `progress` 每个采样步一条、`node` 一个作业几十条、`status` 是队列数变化。
        全收的话，时间线很快只剩这几种，而错误会被冲得看不见 ——
        那才是这个视图存在的理由。最新进度与队列数在别处一直看得见。
        """
        for etype in ("progress", "node", "status", "backend_log", "snapshot"):
            with self.subTest(etype=etype):
                self.assertIsNone(logbuf.classify_event({"type": etype}))

    def test_a_job_failure_is_an_error_naming_the_job(self):
        row = logbuf.classify_event({"type": "job_error", "job_id": "abcdef1234567890",
                                     "message": "后端拒了这次提交"})
        self.assertEqual(row[0], "error")
        self.assertIn("abcdef12", row[1])          # 短 id，认得出是哪一个
        self.assertIn("后端拒了这次提交", row[1])

    def test_a_node_failure_says_which_node(self):
        row = logbuf.classify_event({"type": "job_error_detail",
                                     "job_id": "abcdef1234567890",
                                     "node_id": "461", "node_type": "SaveImageAdvanced",
                                     "message": "写不进输出目录"})
        self.assertEqual(row[0], "error")
        self.assertIn("SaveImageAdvanced", row[1])

    def test_the_chain_uses_one_type_for_both_outcomes(self):
        """链的成功与失败是**同一个事件类型**（`job_chain`），靠 payload 分家。

        只按类型分级的话，一次失败的自动放大会在时间线上显示成蓝色的 info ——
        而它恰恰是「勾了自动放大却没放大」的唯一线索。
        """
        ok = {"type": "job_chain", "job_id": "abcdef1234567890",
              "chain": {"status": "done", "scale": 2}}
        bad = {"type": "job_chain", "job_id": "abcdef1234567890",
               "chain": {"status": "failed", "scale": 2,
                         "error": "OSError: 产物不见了"}}
        self.assertEqual(self.level(ok), "info")
        self.assertEqual(self.level(bad), "error")
        self.assertIn("×2", self.text(bad))         # 倍数要说出来
        self.assertIn("产物不见了", self.text(bad))

    def test_a_sweep_failure_says_which_cell(self):
        row = logbuf.classify_event({"type": "sweep_failed", "sweep_id": "abcd1234ef",
                                     "at": 2, "total": 3, "message": "第 2 格被成本预警拦住"})
        self.assertEqual(row[0], "error")
        self.assertIn("2/3", row[1])
        self.assertIn("整次作废", row[1])

    def test_an_unknown_type_still_lands_as_info(self):
        """不认识的事件也要进来 —— 宁可多一条，也不要静默丢掉一个将来新增的事件。"""
        self.assertEqual(self.level({"type": "某个新事件", "x": 1}), "info")

    def test_the_tap_never_raises_on_a_broken_payload(self):
        for bad in (None, [], {"type": None}, {"nope": 1}, "字符串"):
            with self.subTest(payload=bad):
                self.assertIsNone(logbuf.note_event(bad))


class LoggingBridgeTest(unittest.TestCase):
    """`logging` → 环。这是工作台**第一条真正的日志通路**。"""

    def setUp(self):
        super().setUp()
        self.buf = logbuf.LogBuffer()
        self.handler = logbuf.BufferHandler(self.buf)
        self.logger = logging.getLogger("workbench.test")
        self.logger.addHandler(self.handler)
        self.old_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.addCleanup(self.handler.close)
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.addCleanup(self.logger.setLevel, self.old_level)

    def test_a_record_lands_with_its_level(self):
        self.logger.warning("一句话")
        rows = self.buf.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "warn")
        self.assertEqual(rows[0]["source"], "workbench.test")
        self.assertEqual(rows[0]["text"], "一句话")
        self.assertEqual(rows[0]["kind"], "log")

    def test_an_exception_keeps_the_traceback(self):
        """**堆栈必须留住** —— 「链失败」那类路径上，页面只拿到一句话，
        真正能定位的那几行只能在日志里。"""
        try:
            raise ValueError("里面的原因")
        except ValueError:
            self.logger.exception("外面的一句话")
        rows = self.buf.rows()
        self.assertIn("外面的一句话", rows[0]["text"])
        self.assertIn("ValueError", rows[0]["detail"])
        self.assertIn("里面的原因", rows[0]["detail"])

    def test_access_logs_stay_out_of_the_ring(self):
        """`uvicorn.access` **不进环**。

        日志台自己每秒轮询一次 `/api/logs`。把访问日志记下来的话，环里很快就
        只剩「GET /api/logs 200」—— 日志台把自己冲掉，而且没有症状。
        """
        access = logging.getLogger("uvicorn.access")
        access.addHandler(self.handler)
        self.addCleanup(access.removeHandler, self.handler)
        access.info('127.0.0.1 - "GET /api/logs HTTP/1.1" 200 OK')
        self.assertEqual(self.buf.rows(), [])

    def test_the_handler_never_raises(self):
        """日志坏掉不能带崩业务。喂一个会让格式化炸掉的记录。

        `Handler.handleError` 是**只打印不抛**的（那是标准库的约定），所以
        这里同时钉住两件事：它被调用了，而且异常没有从这里漏出去。
        """
        handled = []
        self.handler.handleError = handled.append
        record = logging.LogRecord("x", logging.INFO, "p", 1, None, None, None)
        record.getMessage = lambda: (_ for _ in ()).throw(RuntimeError("炸"))
        self.handler.emit(record)          # 不抛就算过
        self.assertEqual(len(handled), 1, "炸掉的记录应当交给 handleError")


class TheBrokerTapTest(unittest.TestCase):
    """事件只在一处挂钩：`Broker.publish`。启动流程走的是 `publish_threadsafe`，
    它只是把 `publish` 排进事件循环，所以线程里的事件一样收得到。"""

    def test_a_publish_reaches_the_tap(self):
        from workbench.events import Broker
        seen = []
        broker = Broker(on_event=seen.append)
        broker.publish({"type": "job_queued", "job_id": "x"})
        self.assertEqual([e["type"] for e in seen], ["job_queued"])

    def test_a_broken_tap_cannot_stop_the_dispatch(self):
        """旁路坏掉**绝不能**影响正事：日志台出问题不该让出图停下来。"""
        from workbench.events import Broker
        got = []

        def boom(_event):
            raise RuntimeError("旁路炸了")

        broker = Broker(on_event=boom)
        sid, queue = broker.subscribe()
        broker.publish({"type": "job_done", "job_id": "x"})
        self.assertEqual(queue.qsize(), 1, "旁路抛异常把事件分发也带停了")

    def test_the_tap_is_outside_the_lock(self):
        """`publish` 是在**锁外**调 tap 的。

        握着那把锁去调外面的代码，只要 tap 里碰了别的锁就会死锁 —— 而那种
        卡死发生在事件循环线程上，表现为「整个页面不动了」。
        这里用一个「在 tap 里再发布一次」的旁路来证明可重入。
        """
        from workbench.events import Broker
        broker = Broker()

        def reenter(_event):
            broker.publish({"type": "sweep_failed", "sweep_id": "s"})

        broker.on_event = reenter
        broker.publish({"type": "job_queued", "job_id": "x"})   # 不超时即过


class LogsEndpointTest(unittest.TestCase):
    """`/api/logs`。判据落在**游标**与**重启**上 —— 这两件错了页面都会空着。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(dir=support.scratch_dir(), prefix="qwen21-logs-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        wb = server.Workbench(state_dir=self.tmp, client=support.FakeComfy())
        self.app = server.create_app(wb)
        self.wb = wb
        self.tc = TestClient(self.app)
        # 环是**进程级单例**（`logbuf.BUFFER`），别的测试与真实线程都会往里写。
        # 换一个干净的实例出来，断言才不会被无关的行搅乱 —— 端点读的正是这个
        # 模块属性，所以换掉它两端都跟着走。
        self.buf = logbuf.LogBuffer()
        patcher = mock.patch.object(logbuf, "BUFFER", self.buf)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_first_read_gives_a_snapshot_and_a_cursor(self):
        self.buf.append("info", "test", "第一条", kind=logbuf.KIND_LOG)
        data = self.tc.get("/api/logs").json()
        self.assertEqual([r["text"] for r in data["timeline"]], ["第一条"])
        self.assertEqual(data["cursor"], 1)
        self.assertFalse(data["reset"])
        self.assertIsInstance(data["epoch"], int)
        # 首次要给「工作台日志」那一栏的尾部快照；之后靠 timeline 增量
        self.assertEqual(len(data["workbench"]), 1)

    def test_the_second_read_carries_only_the_new_rows(self):
        self.buf.append("info", "test", "旧的")
        first = self.tc.get("/api/logs").json()
        self.buf.append("error", "test", "新的")
        second = self.tc.get("/api/logs?since=%d" % first["cursor"]).json()
        self.assertEqual([r["text"] for r in second["timeline"]], ["新的"])
        self.assertIsNone(second["workbench"],
                          "增量那一趟不该再送一遍尾部快照")

    def test_a_stale_cursor_resets_instead_of_going_silent(self):
        """服务重启过（seq 从 1 重来）时要**回全量并报 reset**。

        只回空列表的话，页面会一直空着 —— 而空着看起来跟「什么都没发生」
        一模一样，用户会在一个明明有内容的日志台上得出「没事」的结论。
        """
        self.buf.append("error", "test", "重启后的第一条")
        data = self.tc.get("/api/logs?since=99999").json()
        self.assertTrue(data["reset"])
        self.assertEqual(len(data["timeline"]), 1)

    def test_a_published_event_reaches_the_timeline(self):
        self.wb.broker.publish({"type": "job_error", "job_id": "abcdef1234567890",
                                "message": "测试"})
        data = self.tc.get("/api/logs").json()
        self.assertEqual(len(data["timeline"]), 1)
        self.assertEqual(data["timeline"][0]["level"], "error")
        self.assertEqual(data["timeline"][0]["source"], "job_error")

    def test_a_long_traceback_is_clipped_in_the_middle(self):
        """详情太长就从**中间**截：堆栈的头（哪个函数）与尾（真正的异常）都要留。
        只留头的话，最要紧的那一行正好没了。"""
        self.buf.append("error", "test", "一句话", detail="头" * 4000 + "真正的异常" + "尾" * 4000)
        row = self.tc.get("/api/logs").json()["timeline"][0]
        self.assertLess(len(row["detail"]), 6100)
        self.assertIn("头", row["detail"])
        self.assertIn("尾", row["detail"])
        self.assertIn("省略", row["detail"])

    def test_the_backend_block_names_the_file_and_how_stale_it_is(self):
        """**陈旧的日志比没有更坏**：实测本机后端在跑但空闲一个半小时，它那份
        日志最后写入停在 1.5 小时前 —— 不说清楚，一屏旧日志看起来就是后端死了。"""
        data = self.tc.get("/api/logs").json()
        be = data["backend"]
        self.assertTrue(be["path"], "本机有后端日志，应当挑出一份")
        self.assertTrue(be["label"])
        self.assertIsInstance(be["stale_s"], int)
        # 全部候选都要列出来（含陈旧的），免得「少了一个文件」没有任何痕迹
        self.assertGreaterEqual(len(be["candidates"]), 1)
        self.assertIn(be["path"], [c["path"] for c in be["candidates"]])

    def test_no_backend_log_says_why_instead_of_showing_nothing(self):
        """找不到日志时**要说出来**。一片空白会被读成「后端一切正常」。

        本机以前就是这样：手起后端（铁律 1 那条命令）时工作台自己没有那份
        `state/backend.log`，页面给一片空白。
        """
        with mock.patch.object(launch, "backend_log_candidates", return_value=[]):
            be = self.tc.get("/api/logs").json()["backend"]
        self.assertIsNone(be["path"])
        self.assertEqual(be["lines"], [])
        self.assertIn("user/", be["note"])

    def test_it_never_touches_the_backend(self):
        """`/api/logs` **不探后端** —— 页面每秒轮询它，让它去探等于每秒花一次
        2~6 秒的连接预算去问一件没变的事（环境体检走 `/api/backend`）。
        这里把探活换成一个一碰就炸的替身，比数调用次数更硬。
        """
        boom = mock.Mock(side_effect=AssertionError("/api/logs 不该探后端"))
        with mock.patch.object(self.wb.client, "liveness", boom):
            self.assertEqual(self.tc.get("/api/logs").status_code, 200)
        boom.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
