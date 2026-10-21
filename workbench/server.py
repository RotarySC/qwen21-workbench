"""Starlette 路由 + uvicorn 入口。

同源是**刻意**的：浏览器直连 `:8188` 会跨域，而 ComfyUI 默认不开
`--enable-cors-header`，铁律 1 的启动命令里也没有它。让本服务代理一切，
就不必去改那个已经被验证过的启动命令。

ComfyUI 客户端是同步的（`httpx.Client`），所有会碰它的处理器都用
`asyncio.to_thread` 包一层 —— 否则一次 `/view` 取 10 MB 图会把 SSE 推流
也一起卡住。快调用（本机 5 ms 级）多这一点线程开销，换的是「事件循环里
不许有慢调用」这条不用记的规矩。
"""
import asyncio
import logging
import os
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import (FileResponse, JSONResponse, PlainTextResponse,
                                 Response, StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import (artifacts, comfy, config, cost, gallery, jobs, launch, llm, logbuf,
               pngmeta, prompts, sweep, thumbs, timings, trash, upload, util,
               vision)
from .events import Broker, sse_frame
from .jobs import JobStore
from .prompts import PromptLibrary
from .watcher import Watcher

# 这个 logger 是**有去处的**：`logbuf.install()`（`create_app` 里调）给 root
# 装了一个 handler，把记录收进日志台那个环里。在此之前这个包一句日志都没有，
# 于是后台路径上的失败全都没了痕迹。
logger = logging.getLogger("workbench.server")


# 三个构建器都在仓库根目录（CLI 包装脚本也用它，不能挪进包内）。
# `qwen21_build` 是 Qwen-Image-2.1 那两条链路，另两个与它**平级但不相干**。
if config.REPO not in sys.path:
    sys.path.insert(0, config.REPO)
import h3_build as h3b        # noqa: E402
import layers_build as lb     # noqa: E402
import qwen21_build as qb     # noqa: E402

#: 五条链路 —— 协议层**只有一个 `mode` 字段**。
#:
#: 页面上是两级的（先选链路、再在 Qwen 那一档里选形状），线路上不是：任务记录、
#: 画廊项、`MODE_LABEL`、`_warnings_for` 全都只认这一个值。刻意不加一个平行的
#: `chain` 字段 —— 那会让「哪条链路」有两份真源，而两份真源迟早分家。
#:
#: 前三个（`t2i` / `edit` / `upscale`）是同一条 Qwen-Image-2.1 链路的三种**形状**，
#: 它们的真源是 `qwen21_build`；后两个是与它**平级**的另外两条链路。
ALL_MODES = qb.ALL_MODES + (h3b.MODE, lb.MODE)


# ---------------------------------------------------------------------------
# 应用状态
# ---------------------------------------------------------------------------
class Workbench:
    """单进程单例。刻意不做多用户 —— spec §11 明确不做鉴权。"""

    def __init__(self, state_dir=None, client=None):
        # 显式传目录，而不是改 config.STATE_DIR —— 后者是模块级常量，
        # JOBS_DIR / RUNS_FILE 在 import 时就算好了，改它不会有任何效果。
        if state_dir:
            self.store = JobStore(jobs_dir=os.path.join(state_dir, "jobs"),
                                  runs_file=os.path.join(state_dir, "runs.jsonl"))
            self.library = PromptLibrary(
                path=os.path.join(state_dir, "prompts.json"))
            self.state_dir = state_dir
        else:
            self.store = JobStore()
            self.library = PromptLibrary()
            self.state_dir = config.STATE_DIR
        # 日志台的时间线由 `Broker` 的**一个 tap** 喂进来：一处钩子覆盖全部已有
        # 事件，不必去改那十几个 `publish()` 调用点。
        self.broker = Broker(on_event=logbuf.note_event)
        # client 可注入：测试用它替代真实 ComfyUI，不必起后端
        self.client = client or comfy.ComfyClient()
        self.watcher = Watcher(
            self.store, self.broker, self.client,
            client_id_file=os.path.join(self.state_dir, "client_id.txt"),
            # 出图后自动放大：链的钩子由**这里**注入（watcher 不认识 server）。
            # 见 `_schedule_chain` —— 它会把协程排回事件循环。
            on_done=self._schedule_chain)
        # 提交那个协程要跑在事件循环上，而 watcher 在自己的线程里收尾；
        # 这个 loop 在 `start()` 里抓（那时已经在循环里了）。
        self.loop = None
        # P6：一键拉起后端。日志落在 state/ 下，页面直接回显它的尾部。
        # 回调走 `publish_threadsafe` —— 启动流程跑在普通线程里，不是事件循环。
        self.launcher = launch.Launcher(
            self.client, log_path=os.path.join(self.state_dir, "backend.log"),
            on_event=self.broker.publish_threadsafe)
        self.started_at = None
        # 日志台：进程启动时刻。页面靠它认出「服务重启过」—— 那时环里的 seq
        # 从 1 重来，页面手里的游标必须一起清掉，否则它永远收到空列表。
        self.started_at_ms = util.now_ms()
        # LLM 提示词增强的设置。凭据落在 state/llm.json（`.gitignore` 忽略整个
        # state/）。**这里只建对象，不读不写** —— 缺文件就是「还没配」。
        self.llm = llm.LLMSettings(path=os.path.join(self.state_dir, "llm.json"))

    def start(self):
        # 抓事件循环：链的钩子要在 watcher 那个线程里把协程排回来。
        self.loop = asyncio.get_running_loop()
        self.watcher.start()

    def stop(self):
        self.watcher.stop()
        self.client.close()

    def _schedule_chain(self, pid):
        """watcher 收尾时喊的这一句：把「出图后自动放大」排回事件循环。

        跑在 watcher 的线程里（不是事件循环里），所以必须
        `run_coroutine_threadsafe` —— 直接 `await` 会在这里抛
        「no running event loop」，而那句错会**吞掉收尾**。
        """
        if self.loop is None or self.loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(_chain_upscale(self, pid), self.loop)


# ---------------------------------------------------------------------------
# 后端状态（判据 6）
# ---------------------------------------------------------------------------
def _backend_state(wb, running, stats, models, liveness="ok"):
    missing = sorted(k for k, v in models.items() if v.get("present") is False)
    unknown = sorted(k for k, v in models.items() if v.get("present") is None)
    device = None
    if isinstance(stats, dict):
        devs = stats.get("devices") or []
        if devs and isinstance(devs[0], dict):
            d = devs[0]
            device = {
                "name": d.get("name"),
                "vram_total": d.get("vram_total"),
                "vram_free": d.get("vram_free"),
            }
    command = config.start_command()
    # 开机脚本的对照（P22）。后端有**三条**起来的路：工作台那颗开关、页面上那条
    # 可复制命令、开机时那个计划任务。前两条同源（都在 `config` 里），第三条跑的是
    # 安装目录里那个 `.bat` —— 它不在本仓库里，所以只能读出来对照。
    boot = config.boot_script()
    if not boot["present"]:
        # 这台机器没有这条开机路径（开发机就是这样）—— 没有可对照的东西，不画。
        boot_text = None
    elif boot["ok"]:
        boot_text = "与工作台同源（%d 个变量）" % len(config.BACKEND_ENV)
    else:
        bad = list(boot["missing"]) + ["%s（现在是 %s）" % (w["key"], w["got"])
                                       for w in boot["wrong"]]
        boot_text = ("缺 %s —— 开机起的那个后端跑在另一套环境里，而这里照样写着"
                     "「后端就绪」。" % "、".join(bad))
        # 这一行是**环境体检**里的一格，按纯文本画（`esc()` 会把 `**` 原样画出来，
        # 而那一栏别的行都没有那种记号）—— 所以这里刻意不写 markdown。
    if not running:
        # 这块话现在是**那颗开关的悬停提示**（2026-10-02 起，告警板撤掉了）。
        # 所以不能说「下面这条命令」—— 它下面什么都没有了，命令在「日志 → 环境体检」
        # 里。判据：这句话里必须点出它去哪儿了（`test_api.BackendStop`）。
        hint = ("后端未运行。点右上角那颗开关就按铁律 1 的命令拉起它；"
                "原样的命令在「日志 → 环境体检」里，起不来的原因也在日志台。")
    elif liveness == "unclear":
        # 「没答好」不是「没起」—— 冷加载 16 GiB 权重时实测能超过 2 秒不答
        hint = ("后端在跑，但刚才探活没干净答完：多半是正忙着出图或冷加载模型，"
                "也可能它在报错。**不用去拉后端** —— 要提交的任务会正常排队。")
    elif missing:
        hint = ("后端在跑，但缺 %s —— 说明 `--extra-model-paths-config` "
                "没生效或 yaml 指到了别处。别急着出图，会白跑。" % "、".join(missing))
    else:
        hint = "后端就绪，四个模型文件都可见。"
    return {
        "running": running,
        # ok / down / unclear。三者处置不同：拉后端 / 等一下 / 拉后端
        "liveness": liveness,
        # P6：一键启动的状态。liveness 传进去复用，别再探一次后端
        "launch": wb.launcher.status(liveness=liveness),
        "url": config.COMFY_BASE,
        "comfyui_version": ((stats or {}).get("system") or {}).get("comfyui_version"),
        "device": device,
        "models": models,
        "models_ok": bool(running and not missing and not unknown),
        "missing_models": missing,
        "unknown_models": unknown,
        "start_command": command,
        "boot_script": {**boot, "summary": boot_text},
        # 工作台**自己**这一侧的事实（P20 日志台要用）。放在这里而不是写进启动
        # 那条日志里：一条带两个绝对路径的日志行会把时间线那一栏撑成五行折行，
        # 而这两件事本来就是「环境」，不是「某时刻发生的一件事」。
        "runtime": {
            "state_dir": wb.state_dir,
            "python": sys.executable,
            "repo": config.REPO,
        },
        "hint": hint,
        "cost_model": cost.cost_model(),
        "watcher": wb.watcher.state(),
        # 输出目录有**两个**（Desktop 启动 / 手动启动各一个，两个都有图），
        # 所以这里全部列出来，页面能解释「为什么这里有别的模型的图」。
        "output_roots": [dict(r) for r in gallery.candidate_roots(None, stats=stats)],
        # 探针在不在线 + 报告本身的状态。「文件在 ≠ 探针在」—— 判活一起给。
        "probe": {**timings.probe_state(), "report": timings.report_overview()},
        "prompts": wb.library.count(),
        "jobs": {"total": wb.store.count(), "active": len(wb.store.active())},
    }


async def get_backend(request):
    wb = request.app.state.wb
    # 三态：只有**连接被拒**才算没起。「没答好」也算在跑 —— 进程确实活着。
    liveness = await asyncio.to_thread(wb.client.liveness)
    running = liveness != "down"
    stats = None
    models = {}
    if running:
        stats = await asyncio.to_thread(wb.client.system_stats)
        models = await asyncio.to_thread(wb.client.models_report)
    else:
        models = {key: {"node_class": nc, "field": field, "filename": fn,
                        "present": None, "option_count": None,
                        "note": "后端未运行，无法体检"}
                  for key, nc, field, fn in config.REQUIRED_MODELS}
    return JSONResponse(_backend_state(wb, running, stats, models,
                                       liveness=liveness))


# ---------------------------------------------------------------------------
# 构建（表单预检 + 成本估算，只构建不提交）
# ---------------------------------------------------------------------------
def _int_or_none(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("期望整数，收到 %r" % (value,))


def _float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("期望数字，收到 %r" % (value,))


def _build_upscale_body(body, chain=None):
    """放大模式的表单 → 图。

    与出图那四个模式的关键差别：这里**必填的是源图与倍数**，提示词/步数/cfg/
    采样器一个都不用（VOSR2 是 one-step 模型）。所以它走 `qb.build_upscale()`，
    而不是给 `qb.build()` 加分支 —— 后者要求非空提示词。
    """
    source = body.get("source_image")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("放大模式必须选一张源图（ComfyUI input/ 里的文件名）")
    source = source.strip()
    # 布尔不算数字。`_int_or_none` 刻意把 bool 当 None（免得 `True` 变成 1），
    # 但那对放大是**错的**：`scale=true` 会被当成「没给」，静默退化成默认 ×2，
    # 而用户以为他设了个倍数 —— 正是那种没有症状的错。所以先当场拦下。
    if isinstance(body.get("scale"), bool) or isinstance(body.get("seed"), bool):
        raise ValueError("scale / seed 必须是整数，布尔不算")
    # 字段名要出现在报错里：`_int_or_none` 只会说「期望整数」，而表单里
    # scale 与 seed 挨着，不点名等于没说。
    try:
        scale = _int_or_none(body.get("scale"))
    except ValueError:
        raise ValueError("scale 必须是整数（放大倍数 %d..%d）"
                         % (qb.UPSCALE_MIN_SCALE, qb.UPSCALE_MAX_SCALE))
    try:
        seed = _int_or_none(body.get("seed"))
    except ValueError:
        raise ValueError("seed 必须是 >= 0 的整数")
    params = {
        "mode": qb.UPSCALE_MODE,
        "source_image": source,
        # 放大**只吃一张**（VOSR2 的那个节点就一个 `images` 输入）。仍然把复数键
        # 填成单元素表，只为让记录/画廊项/表单回显三处的**字段形状与出图一致** ——
        # 「键缺失」和「键是空表」在前端是两种待遇。
        "source_images": [source],
        "scale": qb.UPSCALE_DEFAULT_SCALE if scale is None else scale,
        "seed": qb.UPSCALE_DEFAULT_SEED if seed is None else seed,
        # 下面这些在放大的图里**根本不存在**。仍然写出来（而不是省略），
        # 是为了让任务记录 / 画廊项 / 表单回显三处的字段形状与出图一致 ——
        # 「键缺失」和「键是 null」在前端是两种待遇。
        #
        # ⚠ **`prompt` 不在这里，而且五条链路都不该有它。** 提示词的落点是记录自己的
        # `prompt_text`（`_submit_one` 写进去的那个），在 `params` 里再放一份就是
        # 同一件事有两份真源。2026-10-12 之前只有放大这一格有 `"prompt": ""`，
        # 于是「五条链路键集一致」这条规矩**早就悄悄破了个口子**，
        # 而它一直没人发现（加 H3 时那条新判据一上来就抓到了）。
        "negative_prompt": "",
        "steps": None,
        "width": None,
        "height": None,
        "cfg": None,
        "sampler": None,
        "scheduler": None,
        "resolution": None,
        # 这两格在放大图里**不存在**（H3 的时长、拆层的深度分辨率）。写出来是为了
        # 让五条链路的 `params` **键集完全一致** —— 「键缺失」与「键是 null」
        # 在前端是两种待遇，分家之后页面会静默少画一格。
        "seconds": None,
        "resolution_depth": None,
        "lazy_cache": False,
        "te_speed": False,
        # 同上：VOSR2 走的是另一套权重，没有这个旋钮。**键必须写出来** ——
        # 记录 / 画廊项 / 表单回显三处都按同一套字段形状读（见上面那段注释）。
        "turbo": False,
        # 改写器同理：放大的图里没有文本编码器，这个键恒为 False。
        # 但**必须写出来** —— 成本预估与确认策略都要读它（缺键会 KeyError，
        # 而把键省掉正是那种「两条链的字段形状分家」的源头）。
        "rewrite": False,
        # 「出图后自动放大」：放大自己就是那条链，所以这里恒为关。
        # 用户真递了 enabled 的话在 `_auto_upscale` 里已经被 400 挡掉了。
        "auto_upscale": chain or {"enabled": False,
                                  "scale": qb.UPSCALE_DEFAULT_SCALE},
    }
    workflow = qb.build_upscale(source, scale=params["scale"], seed=params["seed"])
    return workflow, params, ""


def _source_images_or_none(body, refs):
    """把参考图那一列同时写进单数/复数两个键（两个键**恒等**是成文规矩）。"""
    return {
        "source_image": (refs[0] if refs else None),
        "source_images": refs,
    }


def _build_h3_body(body, chain=None):
    """H3（MiniMax 图生视频）的表单 → 图。

    与出图那几条的关键差别：**提示词与首帧都必填**，而步数 / cfg / 采样器 /
    分辨率一个都不用 —— 4 步是 `lightx2v_turbo_4step` 蒸馏 LoRA 的固定档，
    采样器与尺寸都冻结在基准图里（见 `h3_build` 的模块文档）。
    """
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("提示词不能为空")
    refs = _reference_images(body)
    if not refs:
        raise ValueError("H3 必须给一张首帧图（ComfyUI input/ 里的文件名）")
    if len(refs) > 1:
        # 基准图里只有一个 `LoadImage`。收下第二张会让用户以为它也参与了 ——
        # 而它不会，且**没有任何症状**（视频照出，只是没用上那张图）。
        raise ValueError("H3 只用一张首帧图，收到 %d 张；要换请先「清空」再重选。"
                         % len(refs))
    if isinstance(body.get("seconds"), bool) or isinstance(body.get("seed"), bool):
        raise ValueError("seconds / seed 必须是数字，布尔不算")
    seconds = _int_or_none(body.get("seconds"))
    seed = _int_or_none(body.get("seed"))

    params = {
        "mode": h3b.MODE,
        # 提示词**不进 `params`** —— 它的落点是记录自己的 `prompt_text`。
        # 五条链路的键集必须一致，见 `_build_upscale_body` 里那段注释。
        # 这条链路没有反向词通路（图里没有负向分支），写死空串而不是把表单里
        # 那个框原样带上 —— 记录里留一条不起作用的反向词，回看时会以为它当初生效过。
        "negative_prompt": "",
        "steps": h3b.STEPS,
        "width": None,
        "height": None,
        "seed": seed,
        "cfg": None,
        "sampler": None,
        "scheduler": None,
        "resolution": None,
        "scale": None,
        "seconds": seconds,
        "resolution_depth": None,
        # 三个加速/缓存旋钮在这条图里**不存在**：图里没有 QwenImage21Cache、
        # 没有 LoraLoaderModelOnly 之外的位置（那个 LoRA 是 H3 自己的 turbo），
        # 也没有 TE-Speed 那条线。恒 False，且必须写出来（键集恒定）。
        "lazy_cache": False,
        "te_speed": False,
        "turbo": False,
        "rewrite": False,
        "auto_upscale": chain or {"enabled": False,
                                  "scale": qb.UPSCALE_DEFAULT_SCALE},
    }
    params.update(_source_images_or_none(body, refs))
    workflow = h3b.build(prompt=prompt, first_frame=refs[0],
                         seconds=seconds, seed=seed)
    return workflow, params, prompt


def _build_layers_body(body, chain=None):
    """See-through（单图拆层）的表单 → 图。

    这条链路**没有提示词**（图里没有文本编码器），必填的是源图。所以
    `prompt` 恒为空串，与放大那条同一个形状。
    """
    refs = _reference_images(body)
    if not refs:
        raise ValueError("拆层必须选一张源图（ComfyUI input/ 里的文件名）")
    if len(refs) > 1:
        raise ValueError("拆层只用一张源图，收到 %d 张；要换请先「清空」再重选。"
                         % len(refs))
    if (isinstance(body.get("seed"), bool)
            or isinstance(body.get("steps"), bool)
            or isinstance(body.get("resolution_depth"), bool)):
        raise ValueError("seed / steps / resolution_depth 必须是整数，布尔不算")
    seed = _int_or_none(body.get("seed"))
    steps = _int_or_none(body.get("steps"))
    # `resolution` 在这条链路里是**拆层分辨率**，与图生图那个「参考图缩放边长」
    # 含义不同 —— 但两者都是「这条链路的分辨率」，共用一个键比另起一个
    # `layers_resolution` 好：任务记录 / 画廊项 / 表单回显三处共用同一份形状。
    resolution = _int_or_none(body.get("resolution"))

    params = {
        "mode": lb.MODE,
        # 提示词**不进 `params`**（落点是 `prompt_text`）—— 这条链路本来也没有
        # 文本输入。见 `_build_upscale_body` 里那段关于键集的注释。
        "negative_prompt": "",
        "steps": steps,
        "width": None,
        "height": None,
        "seed": seed,
        "cfg": None,
        "sampler": None,
        "scheduler": None,
        "resolution": resolution,
        "scale": None,
        "seconds": None,
        "resolution_depth": _int_or_none(body.get("resolution_depth")),
        # 见 `_build_h3_body` 里同一段：这五个在这条图里都不存在，恒 False 但键要在。
        "lazy_cache": False,
        "te_speed": False,
        "turbo": False,
        "rewrite": False,
        "auto_upscale": chain or {"enabled": False,
                                  "scale": qb.UPSCALE_DEFAULT_SCALE},
    }
    params.update(_source_images_or_none(body, refs))
    workflow = lb.build(source_image=refs[0], seed=seed, resolution=resolution,
                        steps=steps, resolution_depth=params["resolution_depth"])
    return workflow, params, ""


def _reference_images(body):
    """表单里的参考图 / 源图 → **一个列表**（0 张就是空表）。

    两种写法都收，而且它们指的是同一件事：

        source_image   "a.png"               老客户端、curl、历史回填
        source_images  ["a.png", "b.png"]    多张参考图

    两个都给时**必须一致**（`source_images[0]` 就是 `source_image`），否则当场
    报错。理由与反向词那里一样：同一件事有两份真源，迟早会分家，而分家之后
    谁也不知道该信哪个 —— 这种错没有症状，只是结果和你以为的不一样。

    张数上限由**构建器**判（`qb.build` → `qb.MAX_REFERENCE_IMAGES`），那里才是
    「这台后端最多挂几张」这条事实的所在；这里不重复一个同样的字面量。
    """
    single = body.get("source_image")
    multi = body.get("source_images")
    if single is not None and not isinstance(single, str):
        raise ValueError("source_image（参考图）必须是字符串，收到 %r" % (single,))
    if multi is not None and not isinstance(multi, (list, tuple)):
        raise ValueError("source_images（参考图列表）必须是数组，收到 %r" % (multi,))

    refs = []
    if multi is not None:
        for item in multi:
            if not isinstance(item, str):
                raise ValueError("source_images 里每一项都必须是字符串，收到 %r"
                                 % (item,))
            if item.strip():
                refs.append(item.strip())
    if single and single.strip():
        one = single.strip()
        if refs and refs[0] != one:
            raise ValueError("source_image=%r 与 source_images[0]=%r 不一致 —— "
                             "同一件事不给两份真源，请只给一处" % (one, refs[0]))
        # 走到这里两者已经一致（不一致上面就抛了），所以把第一项写回去即可；
        # 只给了单数时整表就是它。
        if refs:
            refs[0] = one
        else:
            refs = [one]
    return refs


def _auto_upscale(body, mode):
    """「出图后自动放大」那条链的规格。返回 `{"enabled": bool, "scale": int}`。

    与 `scale`（放大模式自己的倍数）**刻意不共用字段**：那个是「这次提交就是
    一次放大」，这个是「出完图之后要不要再接一段放大」。合并写法会让
    「t2i + scale=2 但链关着」变成一个含义不明的状态。

    **放大模式不能挂它**：VOSR2 是 one-step 模型，再叠一次只是换个更大的倍数 ——
    当场 400，而不是给用户一个「点了没用」的开关。
    """
    spec = {"enabled": False, "scale": qb.UPSCALE_DEFAULT_SCALE}
    raw = body.get("auto_upscale")
    if raw is None or raw is False:
        return spec
    if not isinstance(raw, dict):
        raise ValueError('auto_upscale 必须是对象，例如 {"enabled": true, '
                         '"scale": 2}')
    if not _truthy(raw.get("enabled")):
        return spec
    if mode == qb.UPSCALE_MODE:
        raise ValueError(
            "放大超分不能再挂「出图后自动放大」—— 它自己就是放大那条链路"
            "（VOSR2 是 one-step 模型，再叠一次等于换个更大的倍数）。"
            "要更大的输出请直接调放大倍数。")
    if mode == h3b.MODE:
        # 两条理由都写出来：一条是「不适用」，一条是**会炸**。
        raise ValueError(
            "H3 出的是视频，挂不了「出图后自动放大」—— 放大那条链路只吃静态图。"
            "而且 H3 峰值显存实测 21.24 / 22 GiB（96.5%），紧接着再放一次必 OOM。")
    if mode == lb.MODE:
        raise ValueError(
            "拆层的产物是**几十张层图加一份清单**，不是一张图，挂不了"
            "「出图后自动放大」。要放大某一张层，把它拖成源图单独跑一次放大。")
    scale = raw.get("scale")
    if scale is None:
        return {"enabled": True, "scale": qb.UPSCALE_DEFAULT_SCALE}
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise ValueError("auto_upscale.scale 必须是整数（%d..%d），布尔不算"
                         % (qb.UPSCALE_MIN_SCALE, qb.UPSCALE_MAX_SCALE))
    if not (qb.UPSCALE_MIN_SCALE <= scale <= qb.UPSCALE_MAX_SCALE):
        raise ValueError("auto_upscale.scale 必须在 %d..%d，收到 %d"
                         % (qb.UPSCALE_MIN_SCALE, qb.UPSCALE_MAX_SCALE, scale))
    return {"enabled": True, "scale": scale}


def build_from_body(body):
    """把表单字典变成一份 API 图 + 参数摘要。校验失败抛 ValueError（中文）。"""
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    mode = (body.get("mode") or "t2i").strip()
    # 模式**先判**，而且要在提示词之前：撤掉的那两个值（`2k` / `refine`）的请求
    # 往往连提示词的形状都不一样，先回一句「未知模式 + 该怎么改」比回一句
    # 「提示词不能为空」有用得多。这条判据的字面量来自构建器（一处真源）。
    if mode not in ALL_MODES:
        # 撤掉的那两个值有专门的改法（`qwen21_build.unknown_mode_message`）；
        # 其余的报出**五条**合法值 —— 只报 `qb.MODES` 会把 `upscale` 与两条
        # 新链路一起漏掉，而那句提示正是用户唯一的线索。
        raise ValueError("%s（现在可选：%s）"
                         % (qb.unknown_mode_message(mode), "、".join(ALL_MODES)))
    # 串联规格先解析：放大 / H3 / 拆层下它会**当场被拒**（见 `_auto_upscale`）。
    # 五条链路共用同一个键，所以它们也都要写进去（值恒为关）—— 记录 / 画廊项 /
    # 表单回显三处的字段形状必须一致，缺键与 false 在前端是两种待遇。
    chain = _auto_upscale(body, mode)
    if mode == qb.UPSCALE_MODE:
        return _build_upscale_body(body, chain)
    if mode == h3b.MODE:
        return _build_h3_body(body, chain)
    if mode == lb.MODE:
        return _build_layers_body(body, chain)

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("提示词不能为空")
    # 反向词：缺字段 = None = **一个字都不动**（保持基准里的空串），
    # 所以老客户端与 curl 的行为与从前完全一致。
    # 存**原样**（它是提示词，前后的空白由用户决定），只在实际判「生不生效」时
    # 才 strip —— 判据看"有没有内容"，写进去的是他打的那串字。
    negative = body.get("negative_prompt")
    if negative is not None and not isinstance(negative, str):
        raise ValueError("negative_prompt（反向词）必须是字符串，收到 %r"
                         % (negative,))
    refs = _reference_images(body)
    width = _int_or_none(body.get("width"))
    height = _int_or_none(body.get("height"))
    params = {
        "mode": mode,
        "steps": _int_or_none(body.get("steps")),
        "width": width,
        "height": height,
        "seed": _int_or_none(body.get("seed")),
        "cfg": _float_or_none(body.get("cfg")),
        "sampler": (body.get("sampler") or None),
        "scheduler": (body.get("scheduler") or None),
        # 源图：单数那个键**照旧**填第一张（画廊、历史回填、记录形状都不变），
        # 复数那个是新的真相。两者恒等，因为上面已经核过一致性。
        "source_image": (refs[0] if refs else None),
        "source_images": refs,
        "resolution": _int_or_none(body.get("resolution")),
        # 放大倍数（`upscale` 用）与下面两格一样：出图这两条链路上**不存在**，
        # 恒 None。**键必须写出来** —— 五条链路共用同一份形状，
        # 「键缺失」与「键是 null」在前端是两种待遇。
        "scale": None,
        # 同上：出图这两条没有时长与深度分辨率。
        "seconds": None,
        "resolution_depth": None,
        "negative_prompt": negative,
        # 缓存加速。**默认关** —— 缺字段就是 False，老客户端不受影响。
        # 用 _truthy 而不是 bool()：表单/curl 都可能递字符串 "true"。
        "lazy_cache": _truthy(body.get("lazy_cache")),
        "te_speed": _truthy(body.get("te_speed")),
        # 官方 Turbo LoRA。**默认关** —— 同上，缺字段就是 False，老客户端不受影响。
        # 它与上面两个**不互斥**（权重补丁，位置在它们之前），所以这里不做「二选一」。
        "turbo": _truthy(body.get("turbo")),
        # 提示词改写器。**默认关**，与缓存加速同理；2026-10-01 之前它是模式
        # `refine`（同一张图的配置被塞进了模式下拉框，见 qwen21_build.MODES 的注释）。
        "rewrite": _truthy(body.get("rewrite")),
        # 出图后自动放大（串联）。**默认关**。
        "auto_upscale": chain,
    }
    workflow = qb.build(mode, prompt=prompt, steps=params["steps"],
                        width=params["width"], height=params["height"],
                        seed=params["seed"], cfg=params["cfg"],
                        sampler=params["sampler"], scheduler=params["scheduler"],
                        rewrite=params["rewrite"],
                        source_image=params["source_image"],
                        source_images=params["source_images"] or None,
                        resolution=params["resolution"],
                        lazy_cache=params["lazy_cache"],
                        te_speed=params["te_speed"],
                        turbo=params["turbo"],
                        negative_prompt=params["negative_prompt"])
    return workflow, params, prompt


def _chain_estimate(params, wide, tall):
    """出图后自动放大那一段的耗时与门槛。返回 (秒数, 分解, confirm 策略)。

    与单独放大走**同一套公式**（`cost.estimate_upscale_seconds`）：链上的放大
    就是一次普通放大，输出尺寸 = 这次的输出尺寸 × 倍数。所以预检里那个总数
    把两段都算进去 —— 用户点一次「确认」覆盖的是**整条链**，链上不再二次打断。

    没开链就返回 `(0.0, {}, None)`：调用方可以无条件相加、无条件合并。
    """
    spec = params.get("auto_upscale") or {}
    if not spec.get("enabled"):
        return 0.0, {}, None
    scale = spec.get("scale") or qb.UPSCALE_DEFAULT_SCALE
    out_w = (wide or config.DEFAULT_1MP) * scale
    out_h = (tall or config.DEFAULT_1MP) * scale
    seconds, parts = cost.estimate_upscale_seconds(out_w, out_h)
    # 分解表的键要带前缀，否则与出图那几段撞名（`cold_load_s` 两段都有）。
    marked = {"chain_" + k: v for k, v in parts.items()}
    marked["chain_output"] = [out_w, out_h]
    marked["chain_scale"] = scale
    return seconds, marked, cost.confirm_policy("upscale", seconds, out_w, out_h)


def _source_png_size(wb, params, running):
    """读**源图 / 首帧**的 IHDR 尺寸。返回 `(宽, 高, 原因)`，读到了原因就是 None。

    只管**读**，不做任何换算 —— 放大要把它换成「输出尺寸」，H3 要拿它的长宽比
    判首帧会不会被裁。两个调用方共用这一条读盘路（`upload.resolve` 要问后端，
    是个网络调用，所以别在两处各写一遍）。

    为什么非得去读文件：图里只有「源图文件名」，**没有尺寸**。读法是只看 PNG 的
    IHDR（前 24 字节），不把整幅图拉进内存。

    这个函数会碰网络，所以调用方必须用 `asyncio.to_thread` 包着调 —— 与这个
    文件里所有碰 ComfyUI 的调用一样。
    """
    src = params.get("source_image")
    if not src:
        return None, None, "没给源图"
    if not running:
        return None, None, "后端没起，读不到 input/ 目录"
    try:
        root = upload.resolve(client=wb.client)
    except Exception as exc:                           # noqa: BLE001
        # 这一句会一路走到页面上的警告里，但**堆栈只在这里留得下** ——
        # 「推不出 input 目录」有好几种原因（探不到后端、目录没权限、
        # 相对路径算错），只给一句话等于让人自己猜。
        logger.warning("推不出后端在读的 input/ 目录：%s", exc, exc_info=True)
        return None, None, "推不出后端在读的 input/ 目录"
    path = root.get("path")
    if not path:
        return None, None, "推不出后端在读的 input/ 目录"
    size = pngmeta.png_size(os.path.join(path, src))
    if not size:
        return None, None, "读不到 `%s` 的尺寸" % src
    return size[0], size[1], None


def _upscale_dims(wb, params, running):
    """放大模式的**输出**尺寸。返回 `(宽, 高, 原因)`，读到了原因就是 None。

    为什么非得去读文件：图里只有「源图文件名 + scale」，**没有源图尺寸**；
    而 VOSR2 的代价只随**输出**尺寸走，所以不读它就报不出耗时。
    """
    wide, tall, why = _source_png_size(wb, params, running)
    if why:
        return None, None, why
    scale = params.get("scale") or qb.UPSCALE_DEFAULT_SCALE
    return wide * scale, tall * scale, None


def _upscale_warnings(params, nodes, bundle, sources, out_dims):
    """放大模式提交前的体检。

    四件事，每件都对应一次「白等」或一次「必然失败」：
      1. 插件没装 → 节点类不存在，提交必然失败
      2. 权重没下 → bundle 候选表里没有它，提交必然失败
      3. 源图不在 input/ → 校验阶段打回 value_not_in_list
      4. 输出多大、大概多久 → 让用户知道自己在等什么
    """
    out = []
    info = (nodes or {}).get("upscale") or {}
    if info.get("present") is False:
        out.append({"level": "error",
                    "text": "后端没有 `%s` 这个节点类 —— 插件没装，或装了没重启。"
                            "提交必然失败。装法：`git clone` 到 "
                            "`<ComfyUI>/custom_nodes/%s` 再重启后端。"
                            % (info.get("node_class"), info.get("folder"))})
    elif info.get("present") is None:
        out.append({"level": "warn",
                    "text": "读不到 `TESpeedVOSR2Image` 的节点信息（后端没起？），"
                            "没法确认插件在不在。"})

    if bundle is None:
        if info.get("present") is not False:
            out.append({"level": "warn",
                        "text": "读不到 VOSR2 的权重清单（后端没起？），"
                                "没法确认 `%s` 这套权重在不在。" % qb.UPSCALE_BUNDLE})
    elif qb.UPSCALE_BUNDLE not in bundle:
        # 这张候选表是插件扫 `models/vosr2/` 扫出来的 —— 表里没有就是没下权重。
        # 落点是**安装目录**的 models/，不是共享库（extra_model_paths 里没有
        # vosr2 这个键），所以这里把落点一并写出来，省掉一轮排查。
        out.append({"level": "error",
                    "text": "权重 `%s` 不在（候选表里有 %s）—— 提交必然失败。"
                            "它要放在 **<ComfyUI>\\models\\vosr2\\%s\\**，"
                            "注意是**安装目录**的 models\\，不是共享模型库。"
                            % (qb.UPSCALE_BUNDLE, "、".join(bundle) or "空",
                               qb.UPSCALE_BUNDLE)})
    else:
        out.append({"level": "info",
                    "text": "权重 `%s` 在。放大是**生成式**超分 —— 按源图重画一遍，"
                            "不是插值放大；源图细节越密，它改得越多"
                            "（实测纹理图 round-trip 漂移 mean_abs 6.64，"
                            "光滑图只有 1.02）。" % qb.UPSCALE_BUNDLE})

    src = params.get("source_image")
    if sources is None:
        out.append({"level": "warn",
                    "text": "读不到后端 input/ 的文件列表，没法核对源图 `%s` "
                            "在不在。后端没起时就是这个样子。" % src})
    elif src not in sources:
        out.append({"level": "error",
                    "text": "后端 input/ 里没有 `%s`（现有 %d 个：%s）—— "
                            "提交必然失败。先上传一张，或从列表里选一个。"
                            % (src, len(sources), "、".join(sources[:6]) or "空")})
    else:
        scale = params.get("scale")
        if out_dims and out_dims[0] and out_dims[1]:
            out.append({"level": "info",
                        "text": "输出 %d × %d（`%s` ×%s）。实测锚点：2048² 输出约 14 秒、"
                                "4096² 输出约 41 秒（峰值显存约 7.5 GB）。"
                                % (out_dims[0], out_dims[1], src, scale)})
        else:
            # 2026-10-01：页面上不再显示估时（按用户要求去掉），所以这里不能再说
            # 「上面的耗时是按 1024² 假设的」—— 那句会指着一个不存在的元素。
            out.append({"level": "warn",
                        "text": "源图 `%s` 在，但**读不到它的尺寸** —— 算不出输出"
                                "多大，也没法估耗时。" % src})
    return out


def negative_prompt_state(desc):
    """反向词的**结构化**状态 —— 判断只此一处。

    `_warnings_for` 用它生成人话，`/api/build` 把同一份结果交给前端，让页面
    决定要不要摆出「让它生效」那个按钮。**不各算一份**：两者判若不同，页面就会
    顶着「生效中」而 warnings 说「不会生效」，用户没法解释哪句是真的。

    实测背景（数字与出处见 `config.py`）：CFG≈1 时采样器把负向分支整个丢掉，
    所以 `filled=True` 而 `live=False` 的那一格是**一个没有症状的错**。
    """
    cfg = desc.get("cfg")
    cfg_value = float(cfg) if cfg is not None else 1.0
    text = (desc.get("negative_prompt") or "").strip()
    live = cfg_value > config.NEGATIVE_PROMPT_CFG_CEILING
    return {
        "filled": bool(text),
        "live": live,
        "cfg": cfg_value,
        "ceiling": config.NEGATIVE_PROMPT_CFG_CEILING,
        "suggest": config.NEGATIVE_PROMPT_CFG_SUGGEST,
        # 该不该提示"你填了，但它不生效"
        "inert": bool(text) and not live,
    }


def _missing_models_warning(models):
    """「后端看不到这些模型文件」那条 error —— **五条链路共用一份说法**。

    连文件名一起报：只报 key 是不够的（`dit`/`vae` 还算能猜，而
    `h3_text_encoder` 光看 key 根本不知道该去下哪个文件），而这条 error 的全部
    价值就是让用户知道下一步做什么。
    """
    if not models:
        return None
    missing = sorted(k for k, v in (models or {}).items()
                     if v.get("present") is False)
    if not missing:
        return None
    return {"level": "error",
            "text": "后端看不到这些模型文件：%s —— 提交必然失败。"
                    % "、".join(
                        "%s（%s）" % (k, (models.get(k) or {}).get("filename")
                                     or "文件名未知")
                        for k in missing)}


def _node_warnings(nodes, entries, core_hint):
    """按 `(key, 节点类, 目录名)` 那张表刷节点类的 error / warn。

    `core_hint` 是目录名为空（ComfyUI 自带）时那句替换文案 —— **两者处置完全不同**：
    一个去装包，一个去升后端。混成一句话会把人指错方向。
    """
    out = []
    for key, node_class, folder in entries:
        info = (nodes or {}).get(key) or {}
        if info.get("present") is False:
            how = ("装法：把插件放进 `<ComfyUI>/custom_nodes/%s` 再重启后端。"
                   % folder) if folder else core_hint
            out.append({"level": "error",
                        "text": "后端没有 `%s` 这个节点类 —— 提交必然失败。%s"
                                % (node_class, how)})
        elif info.get("present") is None:
            out.append({"level": "warn",
                        "text": "读不到 `%s` 的节点信息（后端没起？），"
                                "没法确认它在不在。" % node_class})
    return out


def _source_warning(params, sources, what):
    """源图 / 首帧在不在后端 input/ 里。返回 (warning_or_None, 在不在)。"""
    src = params.get("source_image")
    if sources is None:
        return {"level": "warn",
                "text": "读不到后端 input/ 的文件列表，没法核对%s `%s` 在不在。"
                        "后端没起时就是这个样子。" % (what, src)}, False
    if src not in sources:
        return {"level": "error",
                "text": "后端 input/ 里没有 `%s`（现有 %d 个：%s）—— "
                        "提交必然失败。先上传一张，或从列表里选一个。"
                        % (src, len(sources),
                           "、".join(sources[:6]) or "空")}, False
    return None, True


def _h3_warnings(desc, params, nodes, models, sources, src_dims):
    """H3 提交前的体检。

    六件事，每件都对应一次「白等」「必然失败」或一次**没有症状的错**：
      1. 显存 —— 峰值 96.5%，不先腾就是跑一半炸
      2. 节点类 —— 两个插件 + 一个版本判据
      3. 模型文件
      4. 首帧在不在 input/
      5. 首帧长宽比 —— 与 9:16 差太远会被 crop/center 裁掉脸
      6. 时长吸附到 17 帧网格（用户给的那个「秒」是近似值）

    ⚠ 时长读的是 **`desc`（图里那个值）而不是 `params`**。`params["seconds"]` 在
    用户没动过那个下拉时是 `None`（= 「不改动基准里的原值」），而图里是 5 ——
    拿 `None` 去报会说出「时长 None 秒」，还会把一句假话写进警告栏。
    """
    out = []
    # --- 1. 显存（放最前：它是唯一一条「不照做就白等 5 分钟」的）---------
    out.append({"level": "warn",
                "text": "H3 峰值显存实测 **21.24 / 22 GiB（96.5%）** —— 跑之前先腾"
                        "显存（`/free`），也别和别的作业并排：那两条都会 OOM。"})
    # --- 2/3. 节点与模型 --------------------------------------------------
    out.extend(_node_warnings(
        nodes, h3b.REQUIRED_NODES,
        "这是 ComfyUI 自带的节点类 —— 缺了说明**后端版本太旧**"
        "（MiniMax H3 要 0.39 以上）。"))
    missing = _missing_models_warning(models)
    if missing:
        out.append(missing)
    # --- 4. 首帧 ----------------------------------------------------------
    warn, present = _source_warning(params, sources, "首帧图")
    if warn:
        out.append(warn)
    # --- 5. 长宽比 --------------------------------------------------------
    if present and src_dims and src_dims[0] and src_dims[1]:
        wide, tall = src_dims[0], src_dims[1]
        ratio = wide / float(tall)
        want = 9 / 16.0                     # 基准图的 `115` 定的是 9:16
        target = "%d × %d" % (h3b.TARGET_WIDTH, h3b.TARGET_HEIGHT)
        if abs(ratio - want) > 0.15:
            out.append({"level": "warn",
                        "text": "首帧是 %d × %d（长宽比 %.2f），而这条链路按 **9:16**"
                                " 出图 —— `ImageResizeKJv2` 会用 crop/center 把它裁成 "
                                "%s，**两端会被切掉**，脸可能出框。想保住构图请先裁好再传。"
                                % (wide, tall, ratio, target)})
        else:
            out.append({"level": "info",
                        "text": "首帧 %d × %d 长宽比与 9:16 接近，会被裁成 %s。"
                                % (wide, tall, target)})
    elif present:
        out.append({"level": "warn",
                    "text": "首帧 `%s` 在，但**读不到它的尺寸** —— 没法判它会不会"
                            "被裁得太多。" % params.get("source_image")})
    # --- 6. 时长的网格吸附 ------------------------------------------------
    seconds = desc.get("seconds")
    frames, real = desc.get("frames"), desc.get("duration_s")
    if seconds is not None and frames:
        out.append({"level": "info",
                    "text": "时长 %s 秒 → **%d 帧 / %.2f 秒**（会吸附到 17 帧网格，"
                            "这才是真正会跑的那一档）。" % (seconds, frames, real)})
        if abs(float(seconds) - 5) > 1e-6:
            out.append({"level": "warn",
                        "text": "这个时长**没实测过** —— 只跑过 5 秒那一档"
                                "（124 帧 / 5.17 秒，310 秒）。帧数由基准图里那条公式"
                                "算得，但耗时没有读数。"})
    return out


def _lama_checkpoint():
    """LaMa 检查点在不在。返回 `(在不在, 实际字节数或 None)`。

    它走 `torch.hub.load_state_dict_from_url` 一个**写死的完整 URL**，
    `HF_ENDPOINT` 救不了它 —— 只能把文件预放进 torch 自己的缓存。
    **缺了不会崩**，只会静默地不拆前后发（`cluster_inpaint_part` 外层有 try/except），
    所以它是一条体检项，而不是一个复选框。

    **缓存根必须读 `TORCH_HOME`**（那就是 torch 自己认的变量）：生产后端由开机
    脚本以 SYSTEM 拉起，按 `~` 拼会指到 SYSTEM 的 profile ⇒ 检查点明明在也报
    「不在」（实测 2026-10-12）。误报的代价不是一句错话 —— 它会让人去重下
    195 MB，或者以为前后发本来就拆不开。

    **进程里没有 `TORCH_HOME` 时**（工作台自己也是开机任务以 SYSTEM 起的，它的
    环境里同样没有这个变量）退到 `config.BACKEND_ENV` —— 那正是**后端会拿到的那
    个值**，也就是检查点真正该在的地方。两头都对不上才回落到 `~`。
    """
    base = (os.environ.get("TORCH_HOME")
            or config.BACKEND_ENV.get("TORCH_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache", "torch"))
    path = os.path.join(base, lb.LAMA_CKPT_REL)
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, None
    return size == lb.LAMA_CKPT_BYTES, size


def _layers_warnings(desc, params, nodes, models, sources):
    """拆层提交前的体检。

    五件事：
      1. 插件节点类（少了提交必然失败）
      2. 两个本地模型目录（少了同上）
      3. 源图在不在 input/
      4. 分辨率 / 深度分辨率 —— 把**实际会跑的两个数**说出来
      5. LaMa 检查点 —— 缺了会静默地不拆前后发

    ⚠ 分辨率与深度分辨率读的是 **`desc`（图里的值）而不是 `params`**：
    用户没动过那两个控件时 `params` 里是 `None`（= 「不改动基准里的原值」），
    而图里是 1024 与 `-1`。拿 `params` 去算，「深度跟随」会被算成 `None`，
    那句话就成了假话 —— 而且 `%d` 会当场炸。
    """
    out = []
    out.extend(_node_warnings(
        nodes, lb.REQUIRED_NODES,
        "这是插件自带的节点类 —— 缺了说明插件没装。"))
    missing = _missing_models_warning(models)
    if missing:
        out.append(missing)
    warn, present = _source_warning(params, sources, "源图")
    if warn:
        out.append(warn)
    if present:
        res = desc.get("resolution") or lb.DEFAULT_RESOLUTION
        depth = desc.get("resolution_depth")
        effective = res if depth == lb.DEPTH_FOLLOW else depth
        if depth == lb.DEPTH_FOLLOW:
            out.append({"level": "info",
                        "text": "拆层跑在 **%d**，深度那一段跟随它（也跑 %d）。"
                                "实测：1024 约 252 秒 / 23 层，1280 约 403 秒 / 24 层。"
                                % (res, effective)})
        else:
            out.append({"level": "warn",
                        "text": "深度分辨率显式设成了 %d（拆层仍是 %d）—— 比跟随更省"
                                "显存，但深度那一段的细节会少一些。"
                                % (effective, res)})
        out.append({"level": "info",
                    "text": "**层数不预判**：它会随分辨率与种子变（实测 1024 得 23 层、"
                            "1280 得 24 层），跑完按产物如实列。整幅会被重新解读，"
                            "不是逐像素抠图。"})
    ok, size = _lama_checkpoint()
    if not ok:
        out.append({"level": "warn",
                    "text": "LaMa 检查点不在（或大小不对：期望 %d 字节，实际 %s）—— "
                            "**前后发不会拆**，其余照常。它不是网络依赖而是 torch 缓存里"
                            "的一个文件，缺了不会报错，只是少一层。"
                            % (lb.LAMA_CKPT_BYTES,
                               "没有" if size is None else size)})
    return out


def _warnings_for(desc, params, models, sources=None, nodes=None, bundle=None,
                  out_dims=None, src_dims=None):
    """提交前的体检。`sources` = 后端 input 里现有的文件名（None = 读不到）。
    `nodes` = 该链路的节点类体检（None = 没查，通常是这个开关没开）。
    `bundle` = `TESpeedVOSR2Loader.model_bundle` 的候选表（只在放大模式查）。
    `out_dims` = 放大模式的**输出**尺寸 `(宽, 高)`，读不到是 `(None, None)`。
    `src_dims` = 源图 / 首帧的**自身**尺寸 `(宽, 高, 原因)`，H3 用来看长宽比。

    图生图这条**必须**在这里拦：参考图不在 input 里时，ComfyUI 会在校验阶段
    直接打回 `value_not_in_list`。与其让用户看着一次 111 秒的运行失败，不如
    在按下按钮之前就说清楚（和「四个模型文件在不在」同一个思路）。

    **五条链路各自整段换掉**，不在下面那些 `if` 之间穿插：拿出图的体检项去体检
    一张 H3 的图或一次拆层，刷出来的全是假警报，而假警报会让真警报失效。
    """
    if params["mode"] == qb.UPSCALE_MODE:
        return _upscale_warnings(params, nodes, bundle, sources, out_dims)
    if params["mode"] == h3b.MODE:
        return _h3_warnings(desc, params, nodes, models, sources, src_dims)
    if params["mode"] == lb.MODE:
        return _layers_warnings(desc, params, nodes, models, sources)

    out = []
    if params["rewrite"]:
        out.append({"level": "warn",
                    "text": "改写器要多加载 8.82 GiB（约 +113 秒）。"
                            "跑之前先用 /free 腾掉显存更稳。"})
    # 2048² 那条**只看尺寸**（2026-10-01 起更清楚了：那个 `2k` 模式已经撤销，
    # 分辨率就在「基础参数 → 尺寸」里调）。所以不再有「模式与尺寸矛盾」那一支。
    if desc.get("is_2k"):
        out.append({"level": "warn",
                    "text": "2048² 约 11 分钟一张、峰值显存约 18.9 GB。"
                            "提交后可以关页面，任务在后端继续跑。"})
    # --- 反向词 / CFG：这是**同一件事的两面**，所以合成一段判 --------------
    # 接线本来就是通的（`TextEncodeQwenImage21` 的输出 slot 1 就是 KSampler 的
    # `negative`），但 cfg≈1 时采样器把负向分支整个丢掉（`comfy/samplers.py:610`）。
    # 实测同一 seed、两段**完全不同**的反向词，出图**逐字节相同**
    # （0 / 4 194 304 字节）—— 所以「填了反向词却没生效」正是本工作区最想消灭的
    # 那种错：页面接受、记录留存、画廊回显，而像素一个没变。
    # 数字与出处见 `config.py` 那一节，这里只负责把它说出来。
    #
    # 四个分支互斥且穷尽（填没填 × 生效没生效）。**不并列两条**：同一次提交里
    # 说两遍「CFG 不是 1.0」，用户就得自己判断哪句是真话。
    st = negative_prompt_state(desc)
    cfg_value, negative, live = st["cfg"], st["filled"], st["live"]
    lo = config.NEGATIVE_PROMPT_SECONDS_AT_CEILING
    hi = config.NEGATIVE_PROMPT_SECONDS_AT_SUGGEST
    price = ("耗时约翻倍（实测 %.1f/%.1f 秒 → %.1f/%.1f 秒）"
             % (lo[0], lo[1], hi[0], hi[1]))
    if st["inert"]:
        # **一句话**（2026-10-01 按用户要求）：这是警告，不是论文 —— 说清「不会生效」
        # 与「怎么才能生效」两件事就够。支撑它的实测证据（同 seed 两段反向词出图
        # 逐字节相同、0/4194304 字节）在 `config.py` 与 `docs/details.md` 里，不再
        # 往页面上搬 —— 页面上那句话原本要读四行才读到「把 CFG 提到 2.0」。
        out.append({"level": "warn",
                    "text": "反向词**不会生效**：CFG=%.2f 时负向分支根本不被求值；"
                            "要让它生效，把 CFG 提到 %.1f。"
                            % (cfg_value, st["suggest"])})
    elif negative:
        out.append({"level": "info",
                    "text": "反向词已生效：CFG=%.2f > %.1f，负向分支会真的跑起来，%s。"
                            "实测这一档画面比 CFG=1.0 略平滑。"
                            % (cfg_value, st["ceiling"], price)})
    elif live:
        # 没填反向词却抬了 CFG：负向分支**照样**会跑（算的是空字符串），出图也
        # 真的会变 —— 实测两版都空反向词时 74.3% 的字节不同。所以这里不能说
        # 「白花时间」：变的是 cfg 本身，只是它不受反向词控制。
        # 同样是**一句话**（2026-10-01）：代价只说「约翻倍」这个量级，秒数不上页面。
        out.append({"level": "warn",
                    "text": "cfg=%.2f 不是 1.0：负向分支会真的跑起来（耗时约翻倍），"
                            "但没有填反向词，所以那一遍算的是空字符串。"
                            % cfg_value})
    steps = desc.get("steps")
    if steps and steps > 60:
        out.append({"level": "info",
                    "text": "%d 步偏多；1024² 下实测 20~40 步已足够。" % steps})
    if params.get("lazy_cache"):
        # 数字全部来自 2026-09-30 那批 A/B 实测（见 qwen21_build.LAZY_CACHE_* 的注释）。
        # 说不清楚代价的加速开关不配默认打开 —— 这里连「估算是保守的」都写出来，
        # 免得用户看到预计 65 秒、实际 40 秒，以为工作台算错了。
        out.append({"level": "info",
                    "text": "缓存加速开着：步复用，采样段实测省 47%~57%"
                            "（1024²/20 步 45.3 → 19.5~24.1 秒；编辑 16 步 62.4 → 28.4 秒）。"
                            "代价是高频细节被抹平 —— 编辑链路像素只变 19.6%，"
                            "而 t2i 那张真实照片变了 77%。估时（`estimate`）**刻意"
                            "没有**把它算进去，所以那个数偏保守。"})
    if params.get("te_speed"):
        # 先看插件在不在 —— 缺节点类是**硬错误**（ComfyUI 校验阶段就打回），
        # 和缺模型文件一个性质，所以与它同级，用 error 而不是 warn。
        info = (nodes or {}).get("te_speed") or {}
        if info.get("present") is False:
            out.append({"level": "error",
                        "text": "后端没有 `%s` 这个节点类 —— 插件没装，或装了没重启。"
                                "提交必然失败。装法：把插件放进 "
                                "`<ComfyUI>/custom_nodes/%s` 再重启后端。"
                                % (info.get("node_class"), info.get("folder"))})
        elif info.get("present") is None:
            out.append({"level": "warn",
                        "text": "读不到 `TESpeedQwenImage21` 的节点信息（后端没起？），"
                                "没法确认插件在不在。"})
        else:
            # 数字全部来自 2026-09-30 那批逐格实测（见 qwen21_build.TE_SPEED 的注释）。
            # 估时器**刻意**不把加速算进去，所以这里必须说出来 —— 否则用户看到
            # 预计 60 秒、实际 16 秒，只会以为工作台算错了。
            out.append({"level": "info",
                        "text": "TE-Speed 开着：1024²/20 步实测采样 45.3 → 14.3 秒"
                                "（省 69.6%），编辑 16 步 64.6 → 21.6 秒（省 66.6%）。"
                                "画质代价是「广而浅」的低幅重掷 —— 相邻像素梯度 "
                                "1.956 → 1.956，细节没被抹平（对比 LazyCache 会把"
                                "梯度砍到 0.953）。估时（`estimate`）**刻意没有**"
                                "把它算进去，所以那个数偏保守。"})
    if params.get("turbo"):
        # **权重缺了不在这里报**：它作为可选权重已经并进了 `models` 那份清单
        # （见 `_preflight_extras`），会走「后端看不到这些模型文件」那条 error ——
        # 与那四个模型文件同一个说法，不另写一份。这里只补两种「问不到」的情形，
        # 以及说清它**是什么**、代价是什么。
        info = (models or {}).get("turbo_lora") or {}
        if info.get("present") is None:
            # 后端没起时为真。**不能**说成「权重不在」—— 一个去拉后端，一个去下权重，
            # 处置完全不同（与那四个模型文件同一条规矩）。
            out.append({"level": "warn",
                        "text": "读不到 `%s` 的 `%s` 候选表（后端没起？），"
                                "没法确认 Turbo 权重在不在。"
                                % config.TURBO_LORA_NODE})
        # 数字全部来自 2026-10-11 那批实测（见 qwen21_build.TURBO_* 的注释）。
        out.append({"level": "info",
                    "text": "Turbo 开着（官方 8 步蒸馏 LoRA）：1024² 实测 8 步 "
                            "7.7 秒、16 步 12.9 秒、20 步 15.1 秒，对照"
                            "「TE-Speed + 25 步」的 19.9 秒。代价是背景的暖调与"
                            "大尺度明暗会变淡 —— 8 步最明显，16 步基本追平 25 步"
                            "基线。**步数与画面是单调的：8 步是这条曲线上最差的"
                            "一端**，别为了快就取 8。它与 TE-Speed 可以叠加；"
                            "与缓存加速的组合没测过。估时（`estimate`）**刻意没有**"
                            "把它算进去，所以那个数偏保守。"})
    if models:
        # 说法与 H3 / 拆层那两条**共用一份**（`_missing_models_warning`）——
        # 各写一份就迟早分家，而用户没法解释哪句是真的。
        missing = _missing_models_warning(models)
        if missing:
            out.append(missing)
    if params["mode"] == "edit":
        # 「没给参考图」到不了这里：qb.build() 会当场抛 ValueError → 400。
        # 所以这个分支只管「给了、但在不在 input/ 里」——**每一张都要核**。
        # 只核第一张的话，第二张不在 input 里就会变成一次 111 秒后失败的白跑，
        # 而那种失败正是这一层存在的理由。
        refs = params.get("source_images") or ([params.get("source_image")]
                                               if params.get("source_image") else [])
        missing = [r for r in refs if sources is not None and r not in sources]
        if sources is None:
            out.append({"level": "warn",
                        "text": "读不到后端 input/ 的文件列表，没法核对参考图 "
                                "%s 在不在。后端没起时就是这个样子。"
                                % "、".join("`%s`" % r for r in refs)})
        elif missing:
            out.append({"level": "error",
                        "text": "后端 input/ 里没有 %s（现有 %d 个：%s）—— "
                                "提交必然失败。"
                                % ("、".join("`%s`" % r for r in missing),
                                   len(sources),
                                   "、".join(sources[:6]) or "空")})
        else:
            out.append({"level": "info",
                        "text": "参考图 %s 已在后端 input/ 里，"
                                "整幅会被重新生成（约 2/3 像素会变），"
                                "指令要具体、单目标。"
                                % "、".join("`%s`" % r for r in refs)})
            if len(refs) > 1:
                # 多张参考图**没有实测过的耗时锚点**，所以这里不打数字，只把
                # 已知的机制说清楚：每张都要过视觉塔 + VAE 编码。
                # 2026-10-01：页面上不再显示估时（按用户要求去掉），这句话原来
                # 靠「上面那个估时」指路 —— 一句话指着**页面上已经不存在的元素**，
                # 读的人只会以为页面坏了。改成自己把代价说全。
                out.append({"level": "warn",
                            "text": "这次有 %d 张参考图，会比一张慢：每多一张都要"
                                    "多走一遍视觉塔与 VAE 编码，采样序列也更长"
                                    "（每张按 resolution 缩放到约同一面积）。"
                                    "本工作区还没量过多张的耗时，所以不给数字。"
                                    % len(refs)})
    return out


async def _input_sources(wb, params, running):
    """图生图与放大才去问后端要 input 清单 —— 别的模式没必要为它多打一次往返。

    放大也需要它：源图**必须**在后端的 input/ 里（插件只读得到那儿）。判据与
    图生图完全一样，所以共用这一条路，不另开一份。

    用的是**后端候选表**（`backend_files`），不是我们自己在磁盘上数出来的
    那几个名字：两个 input 目录可能同时存在，我们数出来的那个未必是后端读的那个
    （见 upload.py）。拿自己数出来的当真，就会把「后端能读到」误报成「没有这个
    文件」—— 那正是这一层要消灭的那种假自信。
    """
    if params.get("mode") not in ("edit", qb.UPSCALE_MODE, h3b.MODE, lb.MODE) \
            or not running:
        return None
    info = await asyncio.to_thread(upload.list_inputs, wb.client)
    return info.get("backend_files")


async def _preflight_extras(wb, params, running):
    """`/api/build` 与 `/api/run` **共用**的几样体检输入。

    为什么必须共用：两处各问一遍就会分家 —— 页面说「权重不在」，
    提交后同一台后端同一份参数却回一句「读不到权重清单」。用户没法解释哪句是真的。

    返回 `(models, nodes, bundle, out_dims)`。除了出图那四个模型文件之外，
    其余三项都只在**用得上的模式**里才去问（少一次往返，也少一次误导）。
    """
    upscale = params["mode"] == qb.UPSCALE_MODE
    h3 = params["mode"] == h3b.MODE
    layers = params["mode"] == lb.MODE
    # 铁律 1 那四个 Qwen 模型只跟出图有关 —— 放大用的是 VOSR2，H3 与 See-through
    # 各有自己那套权重。问它们纯属白跑，而且缺了还会误报成「提交必然失败」。
    # 所以 H3 / 拆层是**整份替换**（`h3b.REQUIRED_MODELS` / `lb.REQUIRED_MODELS`），
    # 不是往那四个后面追加。
    #
    # Turbo LoRA 是**可选权重**：默认关着，缺了也只影响那一条路，所以它不能写进
    # `REQUIRED_MODELS`（那四个缺了整条链路都出不了图）—— 写进去会让没下 LoRA 的人
    # 每次看体检都是琥珀色。这里**在勾上时**把它并进同一份清单，于是「后端看不到
    # 这些模型文件」那条 error 与它的文案全都复用，不必再写第二份。
    #
    # ⚠ `models_report` 收的是**一张表的列表**（`(key, 节点类, 字段, 文件名)` 的
    # 序列），而且那份列表是**整份替换**、不是「追加几个」。写错形状的后果是
    # 到里面 `for k, nc, f, fn in entries` 以「too many values to unpack」炸掉；
    # 当成追加用的后果更隐蔽 —— 拆层的表后面悄悄跟着 Qwen 那四个，
    # 于是每一次拆层体检都报「后端看不到这些模型文件」。
    turbo_extra = ((("turbo_lora",) + config.TURBO_LORA_NODE + (qb.TURBO_LORA_FILE,),)
                   if (running and not upscale and not h3 and not layers
                       and params.get("turbo")) else ())
    if h3:
        model_entries = h3b.REQUIRED_MODELS
    elif layers:
        model_entries = lb.REQUIRED_MODELS
    elif upscale:
        model_entries = ()
    else:
        model_entries = config.REQUIRED_MODELS + turbo_extra
    models = (await asyncio.to_thread(wb.client.models_report, model_entries)
              if running else {})
    # 可选插件只在相关时才去问 —— 用不到的人不该为此多付一次往返。
    # 节点表同样**按链路换**：拿 `te_speed` / `upscale` 那张表去体检 H3，
    # 刷出来的全是假警报，而假警报会让真警报失效。
    if h3:
        node_entries = h3b.REQUIRED_NODES
    elif layers:
        node_entries = lb.REQUIRED_NODES
    elif upscale:
        node_entries = config.REQUIRED_NODES
    else:
        node_entries = (config.REQUIRED_NODES if params.get("te_speed") else None)
    nodes = (await asyncio.to_thread(wb.client.nodes_report, node_entries)
             if (running and node_entries) else None)
    # 放大：权重的判据是插件自己的 bundle 候选表；输出尺寸只能去读源图的 IHDR。
    bundle = (await asyncio.to_thread(wb.client.combo_options,
                                      *config.UPSCALE_BUNDLE_NODE)
              if (running and upscale) else None)
    # 源图尺寸：放大要拿它算**输出**尺寸，H3 要拿它判**首帧长宽比**。
    # 两个用同一条读盘路（`_source_png_size`）—— 所以这里**只读一次**，
    # 放大那个结果由它乘上倍数推出来，不再单独调 `_upscale_dims`。
    src_dims = (await asyncio.to_thread(_source_png_size, wb, params, running)
                if (running and (upscale or h3)) else None)
    out_dims = None
    if upscale:
        if src_dims and src_dims[0]:
            scale = params.get("scale") or qb.UPSCALE_DEFAULT_SCALE
            out_dims = (src_dims[0] * scale, src_dims[1] * scale, None)
        else:
            out_dims = (None, None,
                        (src_dims or (None, None, "后端没起，读不到 input/ 目录"))[2])
    return models, nodes, bundle, out_dims, src_dims


def _describe_any(workflow):
    """按**节点类**分流到那条链路的反解。

    三个构建器各有一个 `describe()`，键集本来就不一样（H3 有 `frames` /
    `resize_matches`，拆层有 `resolution_depth_effective` / `depth_follows`）。
    所以这里只问「这是哪条链路的图」—— 按 id 认是错的，id 只在我们自己那份基准
    图里成立，而记录里的图可能来自任何地方。

    **抽出来是因为两处都要用**（`/api/build` 与 `_submit_one`）。各写一遍就会分家：
    页面按 H3 的说法显示，记录里却存着 Qwen 那份反解 —— 用户没法解释哪句是真的。
    """
    if h3b.is_h3(workflow):
        return h3b.describe(workflow)
    if lb.is_layers(workflow):
        return lb.describe(workflow)
    return qb.describe(workflow)


async def post_build(request):
    wb = request.app.state.wb
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    try:
        workflow, params, prompt = build_from_body(body)
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    desc = _describe_any(workflow)
    upscale = params["mode"] == qb.UPSCALE_MODE
    h3 = params["mode"] == h3b.MODE
    layers = params["mode"] == lb.MODE
    running = await asyncio.to_thread(wb.client.running)
    models, nodes, bundle, out_dims, src_dims = await _preflight_extras(
        wb, params, running)

    if upscale:
        # 输出尺寸得去问磁盘（图里没有源图尺寸），而放大的成本模型**只认输出尺寸**。
        # 读不到也照常出估算 —— 那时 cost 会按 1024² 输出假设并把它标出来。
        wide, tall = out_dims[0], out_dims[1]
        total, parts = cost.estimate_upscale_seconds(wide, tall)
    elif h3:
        # 单锚点：不吃尺寸、不吃步数（4 步冻结），只按时长判「要不要标成外推」。
        wide = tall = None
        total, parts = cost.estimate_h3_seconds(params.get("seconds"))
    elif layers:
        wide = tall = None
        total, parts = cost.estimate_layers_seconds(
            params.get("resolution"), params.get("steps"),
            params.get("resolution_depth"))
    else:
        wide = params["width"] or desc.get("width") or config.DEFAULT_1MP
        tall = params["height"] or desc.get("height") or config.DEFAULT_1MP
        total, parts = cost.estimate_seconds(params["mode"], params["steps"] or 20,
                                             wide, tall,
                                             rewrite=params["rewrite"])

    # 出图后自动放大：**算进同一次预检**（见 `_chain_estimate`），
    # 于是「贵的那档」不会因为「链上的放大」而漏掉 —— 一次 ×4 到 8192² 照样拦。
    chain_s, chain_parts, chain_policy = _chain_estimate(params, wide, tall)
    total += chain_s
    parts.update(chain_parts)

    sources = await _input_sources(wb, params, running)
    return JSONResponse({
        "describe": desc,
        "workflow_nodes": len(workflow),
        "estimate": {"seconds": round(total, 1), "human": cost.humanize(total),
                     "parts": parts,
                     "confirm": cost.merge_policies(
                         cost.confirm_policy(params["mode"], total, wide, tall,
                                             rewrite=params["rewrite"]),
                         chain_policy)},
        "warnings": _warnings_for(desc, params, models, sources, nodes, bundle,
                                  out_dims, src_dims),
        # 反向词的结构化状态：前端拿它决定折叠标题上那个「未生效 / 生效中」的
        # 状态字、以及要不要摆出「让它生效」那个按钮。长解释仍在上面的 warnings
        # 里 —— 同一份判据的文字版（`negative_prompt_state`），不各算一份。
        "negative_prompt": negative_prompt_state(desc),
        # 放大特有：真实的输出尺寸（读源图 IHDR 得来的），以及读不到时的原因。
        # 单独给一个键而不是塞进 describe —— describe 是「图里说了什么」，
        # 这个是「图外面查到什么」，两者混在一起以后就分不清谁在撒谎了。
        "output_size": ({"width": out_dims[0], "height": out_dims[1],
                         "note": out_dims[2]} if upscale else None),
        # 只回摘要，不回整张图：15 个节点、含系统提示词，几 KB 但没必要
        "node_classes": {nid: n.get("class_type") for nid, n in workflow.items()},
    })


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------
async def _submit_one(wb, body, sweep_id=None, cell=None, cell_index=None,
                      parent_job_id=None):
    """一次提交的全部动作，`/api/run` 与 `/api/sweep` 共用这一段。

    返回 `{"ok": True, ...}` 或 `{"ok": False, "status": 4xx/5xx, "body": {...}}`。
    **刻意不抛异常**：扫描要能对「第 3 格失败了」做出决定（见 `post_sweep`），
    而不是整条请求变成一个 500。
    """
    try:
        workflow, params, prompt = build_from_body(body)
    except (ValueError, FileNotFoundError) as exc:
        return {"ok": False, "status": 400, "body": {"error": str(exc)}}

    # job_id 就是我们铸的 prompt_id —— PNG 元数据、/history、探针报告、
    # runs.jsonl 天然共用这一个键（见 jobs.py 的说明）。
    job_id = str(uuid.uuid4())
    # **每次提交都要写得出一个新文件**（长注释在 `qb.unique_save_prefix` 里）：
    # 一字不改地再提一遍时 ComfyUI 一个节点都不会跑，也就不会写文件 —— 表现是
    # 「秒完成、却没有图」。把收尾节点的前缀带上本次作业的短标记，它就会把缓存里
    # 那一份**重新写一遍**（上游仍然不重算，实测毫米级）。
    # 这一步**只在这一条提交路径上做**：`/api/build` 预检拿到的仍是原样的名字，
    # 黄金快照（`golden_refine.json` / `golden_2k.json`）也就一直逐字节相同。
    try:
        prefix = qb.unique_save_prefix(workflow, job_id[:8])
    except ValueError as exc:            # 构建器的 bug，不是用户的输入问题
        logger.error("提交 %s 前改收尾前缀失败：%s", job_id[:8], exc)
        return {"ok": False, "status": 500, "body": {"error": str(exc)}}
    logger.info("提交 %s：收尾前缀 %s（同一份参数再提一次时，靠它才写得下新文件）",
                job_id[:8], prefix)
    # 反解按链路分流（见 `_describe_any`）—— 与 `/api/build` 共用同一份，
    # 否则页面与记录会用两套说法描述同一次提交。
    desc = _describe_any(workflow)
    if params["mode"] == qb.UPSCALE_MODE:
        # 与 /api/build 走**同一条**尺寸解析：输出尺寸只认磁盘上的源图 IHDR。
        # 这里读不到就按 1024² 输出估 —— 与 /api/build 展示的数字必须一致，
        # 否则「页面说 14 秒、记录里写 5 秒」这种事就会冒出来。
        running = await asyncio.to_thread(wb.client.running)
        out_dims = await asyncio.to_thread(_upscale_dims, wb, params, running)
        wide, tall = out_dims[0], out_dims[1]
        total, _ = cost.estimate_upscale_seconds(wide, tall)
    elif params["mode"] == h3b.MODE:
        wide = tall = None
        total, _ = cost.estimate_h3_seconds(params.get("seconds"))
    elif params["mode"] == lb.MODE:
        wide = tall = None
        total, _ = cost.estimate_layers_seconds(
            params.get("resolution"), params.get("steps"),
            params.get("resolution_depth"))
    else:
        wide = params["width"] or desc.get("width") or config.DEFAULT_1MP
        tall = params["height"] or desc.get("height") or config.DEFAULT_1MP
        total, _ = cost.estimate_seconds(params["mode"], params["steps"] or 20,
                                         wide, tall, rewrite=params["rewrite"])

    # 链上的放大算进**同一次**预检与同一次确认（见 `_chain_estimate`）。
    chain_s, _, chain_policy = _chain_estimate(params, wide, tall)
    total += chain_s

    # 成本预警**真的拦一道**，不只是页面上写着：一次 11 分钟的任务（或者会多占
    # 8.82 GiB 显存的那种）不能靠自觉。前端点两下，curl 多带一个 confirm 字段。
    # 扫描走同一条路 —— 否则「排 3 个 2K」就成了绕过预警的后门。
    policy = cost.merge_policies(
        cost.confirm_policy(params["mode"], total, wide, tall,
                            rewrite=params["rewrite"]),
        chain_policy)
    if policy and not _truthy(body.get("confirm")):
        return {"ok": False, "status": 409,
                "body": {"error": policy["text"], "confirm": policy,
                         "estimate_s": round(total, 1)}}

    wb.store.create(job_id=job_id, mode=params["mode"], params=params,
                    prompt_text=prompt, workflow=workflow,
                    estimate_s=round(total, 1), sweep_id=sweep_id, cell=cell,
                    cell_index=cell_index, parent_job_id=parent_job_id)

    try:
        data = await asyncio.to_thread(wb.client.submit, workflow, job_id,
                                       wb.watcher.client_id)
    except comfy.ComfyRejected as exc:
        wb.store.finish(job_id, "error", error={
            "message": str(exc), "code": "rejected",
            "node_errors": exc.node_errors})
        wb.broker.publish({"type": "job_error", "job_id": job_id,
                           "prompt_id": job_id, "message": str(exc)})
        return {"ok": False, "status": 400,
                "body": {"error": str(exc), "job_id": job_id,
                         "node_errors": exc.node_errors}}
    except comfy.ComfyUnreachable as exc:
        wb.store.finish(job_id, "error", error={
            "message": str(exc), "code": "unreachable"})
        wb.broker.publish({"type": "job_error", "job_id": job_id,
                           "prompt_id": job_id, "message": str(exc)})
        return {"ok": False, "status": 503,
                "body": {"error": str(exc), "job_id": job_id}}

    rec = wb.store.get(job_id)
    wb.broker.publish({"type": "job_queued", "job_id": job_id,
                       "prompt_id": job_id, "job": rec})
    return {"ok": True, "job_id": job_id, "rec": rec, "desc": desc,
            "params": params, "prompt": prompt,
            "queue_number": data.get("number"),
            "prompt_id": data.get("prompt_id", job_id)}


def _output_file(wb, outputs):
    """在输出目录候选里找到产物 PNG 的**磁盘路径**。返回 (路径 or None, 文件名)。

    链要把刚出的图喂给 VOSR2，而 VOSR2 只读得到 ComfyUI 的 `input/` ——
    所以第一步是「把这张图先读出来」。走的是画廊那套 `candidate_roots()`：
    本机的落点搬过家（`--output-directory`），只认一个目录会**静默找不到**。
    """
    first = next((o for o in (outputs or []) if o.get("filename")), None)
    if not first:
        return None, None
    name = first["filename"]
    sub = (first.get("subfolder") or "").strip("/\\")
    rel = ("%s/%s" % (sub, name)) if sub else name
    for root in gallery.candidate_roots(wb.client):
        path = gallery.safe_join(root.get("path") or "", rel)
        if path and os.path.isfile(path):
            return path, name
    return None, name


async def _chain_upscale(wb, pid):
    """出图完成后，把产物喂给 VOSR2 再跑一次（`params.auto_upscale`）。

    三步，每一步都复用**已有的唯一入口**：

      1. 从磁盘读出这次的第一张产物（`candidate_roots` + `safe_join`，与画廊同源）；
      2. 交给 `upload.upload_any()` 放进后端 `input/` —— 参考图上传走的就是它，
         **不新开第二条写路径**；名字**以回执为准**（撞名时后端会改名，
         拿自己以为的名字去提交就会失败，而那是 P14 刚栽过的同一个坑）；
      3. `_submit_one()` 提交第二个作业，带 `parent_job_id`。

    **链失败不牵连第一个作业**：它的错写进父记录的 `chain.error` 并推给页面。
    第一个作业那时候已经 `done` 了 —— 它的产物、耗时、记录都不该被这段覆盖。
    """
    rec = wb.store.get(pid) or {}
    spec = (rec.get("params") or {}).get("auto_upscale") or {}
    scale = spec.get("scale") or qb.UPSCALE_DEFAULT_SCALE
    chain = {"status": "failed", "job_id": None, "error": None, "scale": scale,
             "source": None}
    try:
        path, name = await asyncio.to_thread(_output_file, wb, rec.get("outputs"))
        if not path:
            raise RuntimeError("找不到这次的产物文件（它在哪个输出目录里？）")
        data = await asyncio.to_thread(_read_bytes, path)
        up = await asyncio.to_thread(upload.upload_any, wb.client, name, data)
        chain["source"] = up.get("filename") or name
        body = {"mode": qb.UPSCALE_MODE, "source_image": chain["source"],
                "scale": scale, "confirm": True}
        # seed 跟着父作业走：链是可复现的 ——「这张图的 ×N」用同一个 seed 再跑一次，
        # 得到的是同一张。没有 seed（老记录）就交给构建器的默认值。
        seed = (rec.get("params") or {}).get("seed")
        if seed is not None:
            body["seed"] = seed
        out = await _submit_one(wb, body, parent_job_id=pid)
        if not out.get("ok"):
            raise RuntimeError((out.get("body") or {}).get("error")
                               or "提交放大作业失败")
        chain["status"] = "submitted"
        chain["job_id"] = out["job_id"]
    except Exception as exc:                      # noqa: BLE001
        # 这里**必须**兜住一切：钩子跑在 watcher 的收尾路径上，抛出去会让
        # 「一个已经成功的作业收不了尾」—— 而链失败本身是小事。
        chain["error"] = "%s: %s" % (type(exc).__name__, exc)
        # 但「小事」不等于「不留痕迹」：链坏掉之后，页面上看起来只是
        # 「勾了自动放大却没放大」。堆栈只在这里留得下。
        logger.exception("作业 %s 的出图后自动放大失败", pid)
    wb.store.patch(pid, chain=chain)
    wb.broker.publish({"type": "job_chain", "job_id": pid, "prompt_id": pid,
                       "chain": chain})
    return chain


async def post_run(request):
    wb = request.app.state.wb
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)

    # 只有**确定连不上**才拒收。忙的时候照样提交 —— ComfyUI 自己会排队，
    # 而「忙」恰恰是用户最想提交的时候（早先会在这里给一个误导性的 503）。
    if await asyncio.to_thread(wb.client.probe) is False:
        return JSONResponse({"error": "后端未运行 —— 点右上角那颗开关拉起它"
                                       "（命令原文与失败原因都在「日志」里）"},
                            status_code=503)

    out = await _submit_one(wb, body)
    if not out["ok"]:
        return JSONResponse(out["body"], status_code=out["status"])
    # 走到这里后端一定活着，所以下面问出来的都是**真读到的**。与 /api/build
    # 共用同一条装配 —— 否则同一个请求在页面上说「权重不在」、在响应里说
    # 「读不到权重清单」，用户没法判断哪句是真的。
    models, nodes, bundle, out_dims, src_dims = await _preflight_extras(
        wb, out["params"], True)
    return JSONResponse({
        "job_id": out["job_id"],
        "prompt_id": out["prompt_id"],
        "queue_number": out["queue_number"],
        "describe": out["desc"],
        "estimate_s": out["rec"]["estimate_s"],
        "warnings": _warnings_for(out["desc"], out["params"], models,
                                  await _input_sources(wb, out["params"], True),
                                  nodes, bundle, out_dims, src_dims),
    })


# ---------------------------------------------------------------------------
# 任务查询与取消
# ---------------------------------------------------------------------------
def _job_output_state(rec, roots, archived):
    """一条记录现在算什么 → `{on_disk, archived, restorable, gone}`（见 gallery）。

    **只读**，别在这里往记录里写东西 —— `store.list()` 给的是它自己那份对象，
    就地写一个键就变成了「记住的」：它会在图还原回来之后依然说「已归档」，
    而且没有任何东西会去纠正它。要写就写调用方自己 `dict()` 出来的那一份。
    """
    return gallery.output_verdict(rec.get("outputs"), roots, archived)


def _cleanable(rec, roots, archived):
    """这条记录该不该被「清理」掉（P20j 的射程，用户 2026-10-03 定的）。

    产物**有**、但现在既不在候选输出目录里、也不在归档目录里 —— 也就是
    **永远回不来了**。三个条件各挡一类错：

      * 「产物有」把排队/在跑/失败/取消挡在外面 —— 还没有产物的记录不是
        「无效记录」，把失败藏起来是这个工作区最不能接受的坏法之一；
      * 「不在归档目录里」是**免死金牌**：归档里的东西还能还原回来，而还原
        回来那一行必须带着耗时与参数回来（P20h 第④条）—— 记不清掉，它就
        永远回不来了；
      * 「在跑的绝不动」：`store.forget()` 里还有同一道（不可逆的事值得说两遍）。
    """
    if rec.get("status") in jobs.ACTIVE:
        return False
    state = _job_output_state(rec, roots, archived)
    return bool(state["gone"]) and not state["restorable"]


async def get_jobs(request):
    wb = request.app.state.wb
    limit = _int_or_none(request.query_params.get("limit")) or 50
    offset = _int_or_none(request.query_params.get("offset")) or 0
    mode = request.query_params.get("mode") or None

    def work():
        # 候选输出目录与归档目录各只取**一次**：前者第一条来自后端
        # `/system_stats`（一次真网络往返），后者是一次 listdir。逐条去问磁盘
        # 等于给每个任务打一次后端 —— 29 条就是 29 次。
        roots = gallery.candidate_roots(wb.client)
        archived = gallery.archived_names()
        out = []
        for rec in wb.store.list(limit=min(limit, 500), offset=offset, mode=mode):
            # **必须复制**（`dict(rec)`）：`store.list()` 给的是它自己那份记录
            # 对象，就地写一个键会写进它在内存里的状态 —— 于是一个「现算」的东西
            # 变成了「记住的」（同 `_job_output_state` 那段）。
            row = dict(rec)
            row.update(_job_output_state(rec, roots, archived))
            out.append(row)
        # 可清理的条数是**整本账**上的，不是这一页上的：它是那颗「清理」按钮上
        # 那个数字，只数这一页的话，比页面长的那些会被漏掉 —— 按钮写着 3 条、
        # 点下去却删掉 20 条，账就不像账了。
        stale = sum(1 for rec in wb.store.list(limit=100000)
                    if _cleanable(rec, roots, archived))
        return {"jobs": out, "total": wb.store.count(),
                "active": [r["job_id"] for r in wb.store.active()],
                "stale": stale}

    return JSONResponse(await asyncio.to_thread(work))


async def post_jobs_clean(request):
    """清掉「产物已经找不回来」的任务记录（P20j）。

    **只删任务记录本身**（`state/jobs/<id>.json` 那一个文件），**产物一张不动** ——
    这句话要同时写在回执与日志里：紧挨着「清理」这个词的通常是「删东西」，而这里
    删掉的东西进不了回收站，用户得知道被删的到底是什么。

    `runs.jsonl` 也不动（理由见 `JobStore.forget`）：那是历史账，画廊按文件名跟它
    对账；划掉它等于让「这张图是哪次跑出来的」永远没有答案。
    """
    wb = request.app.state.wb

    def work():
        roots = gallery.candidate_roots(wb.client)
        archived = gallery.archived_names()
        picked = [rec["job_id"] for rec in wb.store.list(limit=100000)
                  if _cleanable(rec, roots, archived)]
        return {"removed": wb.store.forget(picked)}

    data = await asyncio.to_thread(work)
    data["count"] = len(data["removed"])
    logger.info("清理任务记录：清掉 %d 条（产物已经找不回来；产物文件一张没动，"
                "runs.jsonl 也没动）", data["count"])
    return JSONResponse(data)


async def get_job(request):
    """一个作业的完整记录 + **现算的**产物描述。

    `artifact` 是 H3 的视频与拆层那几十个层文件的落点。它**现算在响应里、
    绝不写进 store** —— 与 `on_disk` / `archived` / `restorable` / `gone` 四个字段
    同一条规矩：写进 store 之后，产物被挪走、被还原，那个字段都不会跟着变，
    而它会一脸笃定地说着过时的话。

    **只有单个作业这条路算它**（一次 glob + 一次读 json）。`/api/jobs` 列表不算：
    列表只画状态行，一屏几十条乘以那两次 IO 就是几十次，而它们一条都用不上。
    """
    wb = request.app.state.wb
    job_id = request.path_params["job_id"]
    rec = wb.store.get(job_id)
    if rec is None:
        return JSONResponse({"error": "没有这个任务"}, status_code=404)
    out = dict(rec)
    mode = rec.get("mode")
    if mode in (h3b.MODE, lb.MODE):
        token = artifacts.token_of(job_id)
        roots = await asyncio.to_thread(gallery.candidate_roots, wb.client)
        out["artifact"] = await asyncio.to_thread(artifacts.resolve, mode, roots, token)
    else:
        # 键恒定：别的链路也有这个键，只是恒为 None。「键缺失」与「键是 null」
        # 在前端是两种待遇 —— 少一个键，页面就静默少画一格。
        out["artifact"] = None
    return JSONResponse(out)


async def get_job_timings(request):
    """逐节点耗时（判据 4）。

    探针报告里没有这次任务时**不是 404** —— 那是「后端没带探针」这个正常
    状态，不是「任务不存在」。返回 `available: false` + 一句人话原因，
    页面照实显示，出图完全不受影响。
    """
    wb = request.app.state.wb
    job_id = request.path_params["job_id"]
    if wb.store.get(job_id) is None:
        return JSONResponse({"error": "没有这个任务"}, status_code=404)
    return JSONResponse(await asyncio.to_thread(timings.for_job, job_id))


async def post_cancel(request):
    wb = request.app.state.wb
    job_id = request.path_params["job_id"]
    rec = wb.store.get(job_id)
    if rec is None:
        return JSONResponse({"error": "没有这个任务"}, status_code=404)
    if rec["status"] not in jobs.ACTIVE:
        return JSONResponse({"ok": False, "status": rec["status"],
                             "reason": "任务已经结束了"})

    qids = await asyncio.to_thread(wb.client.queue_ids) or {}
    running = qids.get(job_id) == "running"
    did = await asyncio.to_thread(wb.client.cancel, job_id, running)
    # 已经被 interrupt 的任务，history 里很快会出现 execution_interrupted；
    # 给它几秒走正规收尾路径，拿不到就按用户意图直接判取消。
    got = await asyncio.to_thread(wb.watcher.wait_finish, job_id,
                                  config.CANCEL_WAIT_S)
    rec = wb.store.get(job_id)
    if not got and rec["status"] in jobs.ACTIVE:
        wb.store.finish(job_id, "cancelled")
        wb.broker.publish({"type": "job_cancelled", "job_id": job_id,
                           "prompt_id": job_id})
    return JSONResponse({"ok": True, "actions": did,
                         "job": wb.store.get(job_id)})


# ---------------------------------------------------------------------------
# 扫描（P5）：seed × steps 的笛卡尔积，一格就是一次普通出图
# ---------------------------------------------------------------------------
async def post_sweep(request):
    """提交一次网格扫描。

    **每一格就是一次普通的出图** —— 复用 `/api/run` 那条路（`_submit_one`），
    于是运行记录、耗时、画廊、判据 1 的进度全都不用另做一套。
    扫描只是「一次提交多个」。
    """
    wb = request.app.state.wb
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)

    try:
        plan = sweep.parse_grid(body.get("grid"), body.get("max_jobs"))
    except sweep.GridError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if await asyncio.to_thread(wb.client.probe) is False:
        return JSONResponse({"error": "后端未运行 —— 点右上角那颗开关拉起它"
                                       "（命令原文与失败原因都在「日志」里）"},
                            status_code=503)

    sweep_id = str(uuid.uuid4())
    cells = plan["cells"]
    # `expand` 也会抛 GridError（例：放大模式没有步数，扫步数会被它拦下），
    # 所以它和 parse_grid 一样要变成 400 —— 漏了这一层，用户看到的是一个 500，
    # 而 500 里那句人话就白写了。
    try:
        bodies = sweep.expand(body, cells)
    except sweep.GridError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # 先把**第一格**单独跑一遍再继续 —— 成本预警与参数校验都在里面，
    # 所以「12 张全是同一个错」的状况在排第二张之前就会停下。
    done = []
    for i, (cell, one) in enumerate(zip(cells, bodies)):
        out = await _submit_one(wb, one, sweep_id=sweep_id, cell=cell,
                               cell_index=i)
        if out["ok"]:
            done.append({"job_id": out["job_id"], "cell": cell,
                         "cell_index": i, "estimate_s": out["rec"]["estimate_s"]})
            continue

        # 中途失败 → **把已经排上的撤掉，整次扫描作废**。
        # 半个扫描是没用的对照：它会在不同条件下跑出不同的失败方式，
        # 而用户拿它比对参数影响就会得出错的结论。宁可不做。
        cancelled = await _cancel_many(wb, [d["job_id"] for d in done])
        wb.broker.publish({"type": "sweep_failed", "sweep_id": sweep_id,
                           "at": i, "total": len(cells),
                           "message": out["body"].get("error")})
        # 失败的那一格**原样带回它的负载**，只把 error 换成带上下文的说法。
        # 直接丢掉的话，客户端就看不到 `confirm` 这类字段了 ——
        # 明明是「成本预警拦住了」，却只收到一句「第 1/3 格失败」。
        detail = dict(out["body"])
        return JSONResponse({
            **detail,
            "error": "第 %d/%d 格失败：%s（已撤销前面 %d 格）"
                     % (i + 1, len(cells), out["body"].get("error"),
                        len(cancelled["cancelled"])),
            "cell_error": out["body"].get("error"),
            "sweep_id": sweep_id, "cell": cell, "cell_index": i,
            **cancelled,
        }, status_code=out["status"])

    wb.broker.publish({"type": "sweep_queued", "sweep_id": sweep_id,
                       "count": len(done)})
    return JSONResponse({
        "sweep_id": sweep_id,
        "count": len(done),
        "cells": done,
        "dropped_dupes": plan["dropped_dupes"],
        # 「出图后自动放大」在扫描里被**剥掉了**（见 `sweep.expand`）。报出来，
        # 别静默降级：用户勾着它按下扫描，结果拿到 N 张没放大的图却不知道为什么。
        "auto_upscale_dropped": bool(
            (body.get("auto_upscale") or {}).get("enabled")),
        "grid": {"seed": [c["seed"] for c in cells],
                 "steps": [c["steps"] for c in cells]},
        "max_jobs": plan.get("requested"),
    }, status_code=201)


async def _cancel_many(wb, job_ids):
    """撤销一批任务。返回 `{"cancelled": [...], "unconfirmed": [...]}`。

    正在跑的那个要 `interrupt`，还排着的用 `queue delete` —— 这两件事
    在 ComfyUI 里是**两个不同的接口**（见 `comfy.cancel`）。

    **整批共用一个等待预算**（`config.CANCEL_BATCH_BUDGET_S`）。单张等 5 秒是
    合理的，但 12 格的扫描若每格都等，一次撤销请求要挂 60 秒 —— 页面像死了一样。
    预算用完就按用户意图直接收尾，并把**没等到真实终态**的那几个 id 放进
    `unconfirmed`：宁可让调用方看见「这几个是猜的」，也不让请求无限拖长。
    """
    deadline = time.monotonic() + config.CANCEL_BATCH_BUDGET_S
    cancelled, unconfirmed = [], []
    for job_id in job_ids:
        rec = wb.store.get(job_id)
        if rec is None or rec["status"] not in jobs.ACTIVE:
            continue
        qids = await asyncio.to_thread(wb.client.queue_ids)
        state = (qids or {}).get(job_id)
        running = state == "running"
        try:
            did = await asyncio.to_thread(wb.client.cancel, job_id, running)
        except Exception as exc:                   # noqa: BLE001
            # 撤不掉也要继续撤后面的：这是收尾路径，不能因为一张卡住就全停。
            # 但**这一张要留下名字** —— 否则「撤销」回执看着像全撤了，
            # 而实际上有一张还在跑。
            did = []
            logger.warning("撤销 %s 失败，继续撤后面的：%s", job_id, exc)
        # 只在**确知**它还排着、而且真从队列里删掉了的时候直接收尾：
        # 排着的任务不会有 execution_* 事件，为它等 5 秒纯属白等。
        # 读不到队列（None）时**必须等** —— 那种情况下我们不知道它到底跑没跑，
        # 抢着标 cancelled 会把一张真出了的图记成「已取消」。
        certain = (qids is not None and state == "pending"
                   and "queue-delete" in did)
        left = deadline - time.monotonic()
        got = False
        if not certain and left > 0.05:
            got = await asyncio.to_thread(wb.watcher.wait_finish, job_id, left)
        rec = wb.store.get(job_id)
        if rec and rec["status"] in jobs.ACTIVE:
            # 「确认过」有两条路：确知它还在队列里（删掉就完事，没有事件可等），
            # 或者等到了真实终态。**两者都不满足才算没确认** ——
            # 一开始这里写成「没等到就算没确认」，于是走了捷径的那批
            # 反而被标成「没确认」，正好把话说反了。
            if not (certain or got):
                unconfirmed.append(job_id)
            wb.store.finish(job_id, "cancelled")
            wb.broker.publish({"type": "job_cancelled", "job_id": job_id,
                               "prompt_id": job_id})
        cancelled.append(job_id)
    return {"cancelled": cancelled, "unconfirmed": unconfirmed}


async def get_sweep(request):
    """一次扫描的并排数据：每格的参数、状态、`wall_ms` 与**每步耗时**。

    `per_step` 一定同时带 `source` —— 探针读数与 `wall_ms ÷ 步数` 是两个
    不同的量（后者含加载与解码，偏大），页面必须能区分，不能鱼目混珠。
    """
    wb = request.app.state.wb
    sweep_id = request.path_params["sweep_id"]
    rows = await asyncio.to_thread(wb.store.sweep, sweep_id)
    if not rows:
        return JSONResponse({"error": "没有这次扫描（或它已被裁掉）"},
                            status_code=404)

    cells = []
    for rec in rows:
        timing = None
        if rec["status"] == "done":
            timing = await asyncio.to_thread(timings.for_job, rec["job_id"])
        params = rec.get("params") or {}
        cells.append({
            "job_id": rec["job_id"],
            "cell": rec.get("cell"),
            "cell_index": rec.get("cell_index"),
            "status": rec["status"],
            "mode": rec["mode"],
            "prompt_text": rec.get("prompt_text"),
            "params": params,
            "wall_ms": rec.get("wall_ms"),
            "estimate_s": rec.get("estimate_s"),
            "outputs": rec.get("outputs") or [],
            "error": rec.get("error"),
            "per_step": sweep.per_step(rec.get("wall_ms"), params.get("steps"),
                                      timing),
            "timing": {"available": (timing or {}).get("available"),
                       "coverage_pct": (timing or {}).get("coverage_pct"),
                       "warm_start": (timing or {}).get("warm_start")},
        })

    terminal = [c for c in cells if c["status"] in jobs.TERMINAL]
    walls = [c["wall_ms"] for c in cells if c["wall_ms"] is not None]
    return JSONResponse({
        "sweep_id": sweep_id,
        "cells": cells,
        "count": len(cells),
        "done": sum(1 for c in cells if c["status"] == "done"),
        "failed": sum(1 for c in cells if c["status"] == "error"),
        "cancelled": sum(1 for c in cells if c["status"] == "cancelled"),
        "finished": len(terminal),
        "wall_ms_total": sum(walls) if walls else None,
        "per_step_note": sweep.label("probe") + "优先；探针不在时用 "
                         + sweep.label("wall_over_steps"),
    })


async def post_sweep_cancel(request):
    """撤销一次扫描里所有还在跑/排队的格子。"""
    wb = request.app.state.wb
    sweep_id = request.path_params["sweep_id"]
    rows = await asyncio.to_thread(wb.store.sweep, sweep_id)
    if not rows:
        return JSONResponse({"error": "没有这次扫描"}, status_code=404)
    todo = [r["job_id"] for r in rows if r["status"] in jobs.ACTIVE]
    out = await _cancel_many(wb, todo)
    return JSONResponse({"sweep_id": sweep_id,
                         "count": len(out["cancelled"]), **out})


# ---------------------------------------------------------------------------
# 一键启动后端（P6）
# ---------------------------------------------------------------------------
async def post_backend_start(request):
    """按铁律 1 的命令拉起 ComfyUI，**不等就绪**就返回。

    冷启动十几秒到几十秒，让 HTTP 请求挂着不合适。就绪与否由
    `/api/backend/launch` 轮询（页面那边还有 SSE 推）。
    """
    wb = request.app.state.wb
    try:
        state = await asyncio.to_thread(wb.launcher.start)
    except launch.LaunchError as exc:
        # 「被拒绝」也要留痕（P20i）。页面上那颗开关只有一个颜色和一个悬停提示，
        # 拒绝的原文在这里写进日志台的时间线 —— 否则用户一刷新就再也找不到它，
        # 而「点了没反应」正是最费人的那种失败。
        logger.warning("起后端被拒：%s", str(exc).splitlines()[0])
        return JSONResponse({"error": str(exc)}, status_code=409)
    wb.broker.publish({"type": "backend_launching", "pid": state.get("pid"),
                       "launch_id": state.get("launch_id"),
                       "port": state.get("port")})
    return JSONResponse(state, status_code=202)


async def post_backend_stop(request):
    """停掉**工作台自己起的**那个后端（页面上那颗开关的「关」）。

    两道闸都是**决策**，不是啰嗦：

    1. **有作业没结束就拒绝。** ComfyUI 的队列在它自己进程的内存里，后端一停
       那条队列就没了 —— 排在里面的作业会永远停在「排队中」，而我们记的那条
       记录不会有任何东西来纠正它。一次 2K 是 11 分钟，撞上就是在丢用户的东西。
       所以先让用户自己取消或等完；他要是说「就是卡住了」，那条拒绝里也给了
       任务管理器这条路。
    2. **不是自己起的就拒绝**（`Launcher.stop()` 判，这里转述）。工作台重启过
       的那种情况也算在里面：句柄没了就认不出，认不出就不杀。

    两条都写日志（时间线上看得到原文），并原样返回给页面当悬停提示。
    """
    wb = request.app.state.wb
    active = wb.store.active()
    if active:
        head = active[0]
        took = ("，已跑 %d 秒" % max(0, (util.now_ms() - head["started_at"]) / 1000)
                if head.get("started_at") else "，还没轮到它跑")
        msg = ("还有作业没结束（%s %s%s）—— 后端一停，ComfyUI 内存里那条队列就"
               "没了，这次就是白跑。先取消它（作业卡片上那颗「取消」）或等它跑完。"
               "\n要是它已经卡住、取消也取消不掉：任务管理器里结束那个 python.exe。"
               % (logbuf.short_id(head.get("job_id")), head.get("status") or "?", took))
        logger.warning("停后端被拒：%s", msg.splitlines()[0])
        return JSONResponse({"error": msg}, status_code=409)
    try:
        state = await asyncio.to_thread(wb.launcher.stop)
    except launch.LaunchError as exc:
        logger.warning("停后端被拒：%s", str(exc).splitlines()[0])
        return JSONResponse({"error": str(exc)}, status_code=409)
    wb.broker.publish({"type": "backend_stopped", "pid": state.get("pid"),
                       "exit_code": state.get("exit_code"),
                       "derived": state.get("derived"),
                       "stopped_ms": state.get("stopped_ms")})
    return JSONResponse(state)


async def get_backend_launch(request):
    """启动状态 + 日志尾部。就绪**永远看后端本身**，不看我们记的状态。"""
    wb = request.app.state.wb
    return JSONResponse(await asyncio.to_thread(wb.launcher.status))


async def get_backend_log(request):
    """只要日志尾部（页面滚动看日志时反复拉这一条）。"""
    wb = request.app.state.wb
    q = request.query_params
    try:
        limit = min(int(q.get("lines") or 200), 2000)
    except ValueError:
        limit = 200
    lines = await asyncio.to_thread(launch.read_tail, wb.launcher.log_path)
    return JSONResponse({"path": wb.launcher.log_path,
                         "lines": lines[-limit:],
                         "total": len(lines),
                         "ready_line": launch.readiness_line(lines)})


# ---------------------------------------------------------------------------
# 日志台（P20）
# ---------------------------------------------------------------------------
MAX_DETAIL_CHARS = 6000
TIMELINE_TAIL = 500
WORKBENCH_TAIL = 200


def _clip(text, limit=MAX_DETAIL_CHARS):
    """详情太长就从**中间**截：堆栈的头（哪个函数）与尾（真正的异常）都要留。"""
    text = text or ""
    if len(text) <= limit:
        return text or None
    head = limit // 2
    return "%s\n…（这里省略 %d 字）…\n%s" % (
        text[:head], len(text) - limit, text[-(limit - head):])


def _rows_for_api(rows):
    out = []
    for row in rows:
        item = dict(row)
        item["detail"] = _clip(item.get("detail"))
        out.append(item)
    return out


def _backend_log_block(wb, lines=300):
    """「后端日志」那一块：选一份**最近写入**的，并把全部候选列出来。

    为什么要列全部候选：**陈旧的日志比没有更坏** —— 它看起来像「后端死了」。
    实测（2026-10-01）本机后端在跑但空闲了一个半小时，它自己那份日志最后写入
    停在 1.5 小时前；光看文件会以为它早崩了，所以 `stale_s` 一起给出去。
    """
    extra = [(wb.launcher.log_path, "工作台一键启动时抄的 stdout")]
    cands = launch.backend_log_candidates(extra=extra)
    if not cands:
        return {"path": None, "label": None, "lines": [], "total": 0,
                "stale_s": None, "candidates": [],
                "note": "没找到后端日志。ComfyUI 还没起过，或者它写日志的 "
                        "user/ 不在预期位置（正常模式在安装目录，受限模式在 "
                        "工作区 .cache/comfy-backend/）。"}
    first = cands[0]
    tail = launch.read_tail(first["path"])
    return {
        "path": first["path"], "label": first["label"],
        "lines": tail[-int(lines):], "total": len(tail), "note": None,
        "stale_s": max(0, int((util.now_ms() - first["mtime_ms"]) / 1000)),
        "candidates": [{"path": c["path"], "label": c["label"],
                        "size": c["size"],
                        "stale_s": max(0, int((util.now_ms() - c["mtime_ms"]) / 1000))}
                       for c in cands],
    }


async def get_logs(request):
    """日志台唯一的数据源。

    **只读、不碰后端**：环境体检走现成的 `/api/backend`（那里已经有一整套推导
    与三态探活），这里只给日志与时间线。分开的理由是页面每秒轮询它 —— 让这个
    轮询去探后端，等于每秒花一次 2~6 秒的连接预算去问一件没变的事。

    **游标语义**：`?since=N` 只回比 N 新的行；不给 `since` 就是首次，回一份
    尾部快照（含工作台日志那一栏）。页面离开视图就停轮询。
    """
    wb = request.app.state.wb
    since = _int_or_none(request.query_params.get("since"))
    last = logbuf.BUFFER.last_seq
    # 游标比当前还大 = 服务重启过（seq 从 1 重来）。这时**回全量并报 reset**，
    # 否则页面永远收到空列表 —— 而空列表看起来就是「这段时间什么都没发生」。
    reset = since is not None and since > last
    first = since is None or reset
    rows = logbuf.BUFFER.rows(limit=TIMELINE_TAIL) if first \
        else logbuf.BUFFER.rows(since=since)
    return JSONResponse({
        "epoch": wb.started_at_ms,
        "cursor": last,
        "reset": reset,
        "now": util.now_ms(),
        "timeline": _rows_for_api(rows),
        # 工作台日志那一栏只有首次（或重启后）给：它是一份「尾部」而不是增量，
        # 之后新增的记录会跟着 timeline 一起下来。
        "workbench": _rows_for_api(
            logbuf.BUFFER.rows(kind=logbuf.KIND_LOG, limit=WORKBENCH_TAIL))
        if first else None,
        "backend": await asyncio.to_thread(_backend_log_block, wb),
    })


# ---------------------------------------------------------------------------
# LLM 提示词增强（P20）
# ---------------------------------------------------------------------------
def _llm_public(wb):
    """给页面看的完整设置块。**里面没有 api_key**（见 `LLMSettings.public`）。

    `targets` 与 `actions` 都是**后端那张表的唯一真源**（`llm.TARGETS_INFO` /
    `llm.ACTIONS_INFO`）：页面拿它们去建顶栏那两个下拉框，所以「加一个可处理的
    东西」或「加一个动作」都不必去页面里加按钮或加 id。
    """
    data = wb.llm.public()
    return {"settings": data, "defaults": llm.SYSTEM_BASE,
            "targets": [dict(t) for t in llm.TARGETS_INFO],
            "actions": [dict(a) for a in llm.ACTIONS_INFO]}


async def get_llm(request):
    return JSONResponse(_llm_public(request.app.state.wb))


async def post_llm(request):
    """保存设置。

    **密钥留空 = 保持原样**：页面拿到的是掩码，用户改个模型名再保存时那个字段
    是空的 —— 当成「清空」的话，他下一次增强才失败，而当时他什么都没删。
    真要清就传 `{"api_key": null}`。
    """
    wb = request.app.state.wb
    try:
        body = await request.json()
    except Exception:                                  # noqa: BLE001
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "请求体必须是对象"}, status_code=400)
    try:
        wb.llm.save(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    raw = wb.llm.raw()
    # **只记「配没配」，不记密钥本身、也不记它的任何一段。**
    logger.info("LLM 设置已更新：base_url=%s model=%s timeout=%ss 密钥=%s",
                raw.get("base_url") or "（空）", raw.get("model") or "（空）",
                raw.get("timeout_s"), "已配置" if raw.get("api_key") else "未配置")
    return JSONResponse(_llm_public(wb))


async def post_llm_enhance(request):
    """把一段文本交给 LLM 处理一次，回**稿子**（增强稿 / 译稿）。

    `body.refs`（可选，文件名数组）是**参考图** —— 有就把它们也发给模型看
    （它写提示词时要照着那几张图说事）。三条不能忘的：

    ① **读不到就不发**：任何一张取不回来就整次 400，**绝不退回纯文本**。
       理由不是洁癖，是实测 —— 把「这张图里有什么」问一个**没有图**的模型，
       它会理直气壮地编一张出来（两次跑编得还不一样，见
       `logs/llm_vision_probe.log`），而那段编出来的描述会被「采用」写进提示词。
       出图能出、耗时正常、日志干净：**没有症状**。
    ② **参考图在本机是几 MB 的 PNG**（实测 1.6~8.4 MB），先过 `vision.prepare`
       收成「长边 ≤1024 的 JPEG」再出门：远端端点、上行带宽、图片 token 三样都省。
    ③ 回执与时间线都要说**带了几张**（`images`）。页面自己数不算数 —— 服务端
       这一步失败了的时候，页面会以为带上了。

    **不写任何文件**（写盘的只有 `POST /api/llm`），也不碰任何作业状态 ——
    这是一次纯粹的「文本进、文本出」。采纳与否由页面决定（对照那一步）。

    `body.action` 决定做哪件事（`llm.ACTIONS_INFO`：增强 / 译成英文 / 译成中文），
    缺省是增强。**路径名里的 enhance 现在读作「把这段交给 LLM 处理一次」** ——
    它不是「只做增强」；改名除了把页面上/文档里所有引用一起搅动之外没有别的作用。
    """
    wb = request.app.state.wb
    try:
        body = await request.json()
    except Exception:                                  # noqa: BLE001
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "请求体必须是对象"}, status_code=400)
    text = body.get("text")
    target = body.get("target") or llm.TARGET_PROMPT
    action = body.get("action") or llm.ACTION_ENHANCE
    refs = body.get("refs")
    if refs is None:
        refs = []
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        return JSONResponse({"error": "refs 必须是文件名数组", "kind": "input"},
                            status_code=400)
    if len(refs) > llm.MAX_IMAGES:
        return JSONResponse({"error": "一次最多带 %d 张参考图（这次 %d 张）"
                                      % (llm.MAX_IMAGES, len(refs)),
                             "kind": "input"}, status_code=400)
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": "这一栏是空的，没有可处理的内容",
                             "kind": "input"}, status_code=400)
    if target not in llm.TARGETS:
        return JSONResponse({"error": "target 只能是 %s" % " 或 ".join(llm.TARGETS),
                             "kind": "input"}, status_code=400)
    if action not in llm.ACTIONS:
        # **动作必须在这里挡掉**：漏过去的话 `rule_for()` 会 KeyError，页面收到
        # 一个 500，而 500 看起来像「服务端坏了」，实际上只是页面发错了。
        return JSONResponse({"error": "action 只能是 %s" % " 或 ".join(llm.ACTIONS),
                             "kind": "input"}, status_code=400)
    # 标签一律从后端那张表取 —— 页面里没有第二份，日志台里也不该有。
    act = llm.action_label(action)
    what = llm.target_label(target)

    # 参考图：读盘 + 缩图都是阻塞活（8 MB 的 PNG 解一遍约 0.3 秒），放线程里。
    shots = []
    try:
        for name in refs:
            got = await asyncio.to_thread(upload.read_reference, name, wb.client)
            shots.append(await asyncio.to_thread(vision.prepare, got["name"],
                                                 got["data"]))
    except (upload.UploadError, vision.ImageError) as exc:
        # **算在「这次没做」上**：面板里那条状态行要说得出是哪一张、为什么。
        note = "LLM %s要带的参考图取不到（%s）" % (act, what)
        logbuf.note("%s：%s" % (note, exc), level="error", source="llm")
        logger.warning("%s：%s", note, exc)
        return JSONResponse({"error": str(exc), "kind": "image"}, status_code=400)

    try:
        out = await llm.enhance(text, target=target, action=action, settings=wb.llm,
                                images=shots)
    except llm.LLMError as exc:
        # 进时间线 —— 失败必须在日志台里看得见（它不在任何作业记录里）。
        logbuf.note("LLM %s失败（%s）：%s" % (act, what, exc), level="error",
                    source="llm", detail=exc.detail)
        logger.warning("LLM %s 失败（%s）：%s", act, exc.kind, exc)
        return JSONResponse({"error": str(exc), "kind": exc.kind},
                            status_code=502)
    logbuf.note("LLM %s（%s）：%d 字 → %d 字（%s，%.1f 秒%s）" % (
        act, what, len(text.strip()), len(out["text"]), out["model"],
        out["elapsed_ms"] / 1000.0,
        "，随附 %d 张参考图" % out["images"] if out["images"] else ""),
        source="llm")
    return JSONResponse({"ok": True, **out})


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def _snapshot(wb):
    return {
        "type": "snapshot",
        "jobs": wb.store.list(limit=60),
        "active": [r["job_id"] for r in wb.store.active()],
        "watcher": wb.watcher.state(),
        "cost_model": cost.cost_model(),
        "now": int(time.time() * 1000),
    }


async def event_stream(wb, last_id=None):
    """SSE 帧生成器。抽成独立函数是为了能直接单测 —— 走 TestClient 读流会卡在
    长连接上，而这里只要前几帧就够验证「重放 → 快照 → 实时」这个顺序。"""
    sid, queue = wb.broker.subscribe()
    try:
        # 让浏览器断线后 1 秒就重连（默认是 3 秒），刷新体验更跟手。
        yield "retry: 1000\n\n"
        # 顺序有意为之：先补断线期间漏掉的，再给权威快照，最后接实时流。
        # 这样客户端看到的状态单调前进，不会因为快照比重放旧而回退。
        if last_id is not None:
            for eid, event in wb.broker.replay_since(last_id):
                yield sse_frame(eid, event)
        yield sse_frame(None, _snapshot(wb))
        while True:
            try:
                eid, event = await asyncio.wait_for(queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield sse_frame(eid, event)
    finally:
        wb.broker.unsubscribe(sid)


async def get_events(request):
    wb = request.app.state.wb
    last_id = None
    raw = request.headers.get("last-event-id")
    if raw:
        try:
            last_id = int(raw)
        except ValueError:
            last_id = None
    return StreamingResponse(event_stream(wb, last_id),
                             media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# 图片
# ---------------------------------------------------------------------------
async def get_image(request):
    """走 ComfyUI 的 `/view` 转发，而不是自己拼磁盘路径。

    好处是不必猜 output 目录在哪 —— Desktop 版与手动启动不一样，
    猜错就是白屏。代价是多一跳，本机无所谓。
    """
    wb = request.app.state.wb
    q = request.query_params
    filename = q.get("filename")
    subfolder = q.get("subfolder", "") or ""
    type_ = q.get("type", "output") or "output"
    if not filename:
        job_id = q.get("job_id")
        rec = wb.store.get(job_id) if job_id else None
        picks = (rec or {}).get("outputs") or []
        if not picks:
            return PlainTextResponse("找不到这个任务的产物", status_code=404)
        pick = picks[-1]
        filename = pick["filename"]
        subfolder = pick.get("subfolder", "")
        type_ = pick.get("type", "output")
    try:
        ctype, blob = await asyncio.to_thread(wb.client.fetch_view, filename,
                                              subfolder, type_)
    except comfy.ComfyUnreachable as exc:
        return PlainTextResponse(str(exc), status_code=503)
    # no-store：ComfyUI 的序号会在后端重启后从头开始，同一个文件名可能被覆盖，
    # 长缓存会让浏览器拿旧图。单用户本机，正确性优先（P2 再按 mtime 做 ETag）。
    return Response(blob, media_type=ctype,
                    headers={"Cache-Control": "no-store"})


async def get_media(request):
    """视频产物的取物口（H3 那条链路）。

    与 `/api/image` 的两个关键差别：

    ① **支持 `Range`** —— `<video>` 拖进度条靠它。`/api/image` 那条走 ComfyUI 的
       `/view` 转发，把整段字节一次读完；对一个 2~20 MB 的 mp4 来说，那意味着
       每拖一次进度条就重下一整段。所以这里**直接读磁盘**并交给 `FileResponse`
       （它自己处理 `Range` 与 `Content-Range`）。
    ② **落点在子目录里** —— `SaveVideo` 的前缀是 `video/MiniMax_H3`，所以文件在
       `output/video/` 下，而 `/api/image` 的 `resolve_output_file` 只在根目录找。

    边界**不比 `/api/image` 宽**：只认 `artifacts.VIDEO_EXT` 那几个后缀、
    `..` 一律挡（`gallery.safe_join`），而且只在 `candidate_roots()` 给出的
    输出目录里找 —— 名字是客户端给的，不设边界就是任意文件读。
    """
    wb = request.app.state.wb
    q = request.query_params
    name = q.get("filename")
    sub = (q.get("subfolder") or "").strip("/\\")
    if not name:
        return PlainTextResponse("缺少 filename", status_code=400)
    if not name.lower().endswith(artifacts.VIDEO_EXT):
        return PlainTextResponse(
            "这里只发视频产物（%s）—— 图片走 /api/image"
            % "、".join(artifacts.VIDEO_EXT), status_code=400)
    rel = ("%s/%s" % (sub, name)) if sub else name
    roots = await asyncio.to_thread(gallery.candidate_roots, wb.client)
    for root in roots:
        path = gallery.safe_join(root.get("path") or "", rel)
        if path and os.path.isfile(path):
            # 不给长缓存：序号会在后端重启后从头开始，同一个文件名可能被覆盖。
            # 单用户本机，正确性优先。
            return FileResponse(path, headers={"Cache-Control": "no-store"})
    return PlainTextResponse("找不到这个视频", status_code=404)


# ---------------------------------------------------------------------------
# 画廊与提示词库（P2）
# ---------------------------------------------------------------------------
async def _json_body(request):
    """统一的 JSON 体解析。解析不了就抛，由调用方转成 400。"""
    try:
        return await request.json()
    except Exception:
        raise ValueError("请求体不是合法 JSON")


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _tri_bool(value):
    """`""` → None（不过滤）；`"1"` → True；`"0"` → False。别的当 None。"""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def _gallery_roots(wb, stats=None):
    """输出目录候选 + runs.jsonl 的文件名索引。两处都要摸磁盘，外部用 to_thread。"""
    roots = gallery.candidate_roots(wb.client, stats=stats)
    index = gallery.runs_index(wb.store.runs(limit=2000))
    return roots, index


async def get_gallery(request):
    wb = request.app.state.wb
    q = request.query_params
    # 「已归档」是**另一档**，不是又一颗筛选 chip：筛选是在**扫出来的那一堆**里挑，
    # 而这一档换的是**扫哪个目录**。所以它是服务端换一个根，不是往 `scan()` 里加参数。
    archived = _truthy(q.get("archived"))

    def work():
        if archived:
            # 归档目录**不在** `candidate_roots()` 里（正是靠这个，正常档天然看不见
            # 已归档的图）—— 所以这一档由这里把它单独当一个根递进去。runs 索引照旧：
            # 归档不改名，于是那批图的耗时在归档档里照样显示。
            roots = [gallery.archive_root()]
            index = gallery.runs_index(wb.store.runs(limit=2000))
        else:
            roots, index = _gallery_roots(wb)
        data = gallery.scan(
            roots, index,
            limit=min(_int_or_none(q.get("limit")) or 120, 500),
            offset=_int_or_none(q.get("offset")) or 0,
            mode=q.get("mode") or None,
            query=q.get("q") or None,
            recognized=_tri_bool(q.get("recognized")),
            two_k=_truthy(q.get("is_2k")),
            # 「改写」那一颗 chip：与 mode **正交**（一张图可以既是 T2I 又是改写过的），
            # 所以是独立参数，不是 mode 的一个取值。
            rewrite=_truthy(q.get("rewrite")),
        )
        # 「这台机器现在能不能删」跟着清单一起发出去 —— 页面该在**点之前**就
        # 把删除那颗按钮禁用并说明理由，而不是让用户点完收到一句报错。
        # 放在这个线程里：`_load()` 是一次 import，别搁在事件循环上。
        data["can_delete"], data["delete_hint"] = trash.available()
        # 这是**哪一档**：页面据此决定格子上是「归档」还是「还原 / 彻底删除」。
        data["archived"] = archived
        if not archived:
            # 正常档里逐张标「这一张能不能归档」。判据在**后端**（只认工作区
            # `output/` 那一个目录，见 gallery.py 归档那一段）——页面不该自己比对
            # 路径，更不该猜：猜错的表现是「点了没反应」或者「归档到了别的目录」。
            for it in data["items"]:
                idx = it.get("root")
                it["archivable"] = gallery.archivable(
                    roots, idx if isinstance(idx, int) else -1,
                    it.get("filename"))[0] is not None
        return data

    return JSONResponse(await asyncio.to_thread(work))


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


async def _gallery_image_roots(wb, archived):
    """取图那条路的根 —— 正常档与归档档各是哪一批目录。

    归档档用的是**另一个根**：`candidate_roots()` 里没有归档目录（正常档正是
    靠这一点看不见已归档的图）。缺了这一支，归档档的每一格都会 404 —— 而更坏
    的可能是**取到 output/ 里同名的另一张**（同名在这里是真实会发生的，见
    `gallery.restore_target`），那一张还画得出来，没有任何症状。
    """
    if archived:
        return [gallery.archive_root()]
    return await asyncio.to_thread(gallery.candidate_roots, wb.client)


async def _png_response(request, path):
    """把磁盘上那张 PNG 原样发出去。

    `/api/gallery/image` 与缩略图那条**兜底**共用这一份 —— 兜底要是另写一遍，
    两条路的 ETag / 404 文案迟早会分家，而那种分家只在「没装 Pillow」的机器上
    才看得见。
    """
    try:
        st = os.stat(path)
    except OSError:
        return PlainTextResponse("图片不在了", status_code=404)

    # 用 mtime+size 做 ETag：ComfyUI 的序号会在后端重启后从头开始，同名文件
    # 可能被覆盖，但覆盖一定改 mtime —— 所以这个 ETag 不会骗人，而 no-store
    # 那种做法会让画廊退回每帧重下 10 MB。
    etag = '"%d-%d"' % (int(st.st_mtime * 1000), st.st_size)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    try:
        blob = await asyncio.to_thread(_read_bytes, path)
    except OSError as exc:
        return PlainTextResponse("读不了这个文件：%s" % exc, status_code=500)
    return Response(blob, media_type="image/png",
                    headers={"Cache-Control": "no-cache", "ETag": etag})


async def get_gallery_image(request):
    """按「根序号 + 文件名」从**磁盘**取图，不走 ComfyUI 的 `/view`。

    为什么必须走磁盘：`/view` 只认**当前进程**的输出目录。而画廊要同时展示
    两个目录里的图（见 gallery.py 顶部），另一边的图经 `/view` 要么 404，
    要么——更糟——命中**同名的另一张图**。

    根序号是索引不是路径，文件名过 `safe_join`，客户端没法拿它穿越目录。

    **这条路发的是原图**（放大视图、1:1、拖去做参考图都靠它）。格子上那张
    小图是另一条路：`/api/gallery/thumb`。
    """
    wb = request.app.state.wb
    q = request.query_params
    try:
        root_index = int(q.get("root", "0"))
    except (TypeError, ValueError):
        return PlainTextResponse("root 必须是整数", status_code=400)

    roots = await _gallery_image_roots(wb, _truthy(q.get("archived")))
    # 路径解析与「删除」**逐字共用**（`gallery.resolve_output_file`）—— 两边各写
    # 一份的话，会分出「画得出来却删不掉」那种没有症状的分叉。
    path, why, code = gallery.resolve_output_file(roots, root_index, q.get("name"))
    if path is None:
        return PlainTextResponse(why, status_code=code)
    return await _png_response(request, path)


async def get_gallery_thumb(request):
    """格子上那张小图（P20l）—— 与 `/api/gallery/image` 同一套参数，发的是缩略图。

    为什么要多这一条路：现场一页 18 格的原图合计 **65.4 MB**，而且每一张都要
    整份解码（4096² 一张 = 64 MB 解码内存）—— 用户报的卡顿就是它。缩到长边
    512 之后同一页是 0.71 MB。取舍与缓存键在 `thumbs.py` 顶部。

    两条纪律：

    1. **缩不出来就发原图**，绝不报错。没装 Pillow、或者这一张解不开，用户看到的
       应该仍是「一张图」，只是慢 —— 缩略图是装饰，它不该让图消失。
    2. `X-Thumb: jpeg|original` 说清是哪一支 —— 探针与真浏览器那一步靠它判，
       而不是靠「看大小猜」。
    """
    wb = request.app.state.wb
    q = request.query_params
    try:
        root_index = int(q.get("root", "0"))
    except (TypeError, ValueError):
        return PlainTextResponse("root 必须是整数", status_code=400)

    roots = await _gallery_image_roots(wb, _truthy(q.get("archived")))
    path, why, code = gallery.resolve_output_file(roots, root_index, q.get("name"))
    if path is None:
        return PlainTextResponse(why, status_code=code)
    try:
        st = os.stat(path)
    except OSError:
        return PlainTextResponse("图片不在了", status_code=404)

    # ETag 按**源图**算（mtime_ns + 字节数）：同一张图重建出来的缩略图，ETag 不变，
    # 于是重画画廊时 18 格全是 304 —— 换成按缩略图文件本身算，收尾清掉缓存之后
    # ETag 会变，页面就白重下一遍。
    etag = '"t%d-%d"' % (st.st_mtime_ns, st.st_size)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    thumb, note = await asyncio.to_thread(
        thumbs.ensure, path, thumbs.cache_dir(wb.state_dir))
    if thumb is None:
        resp = await _png_response(request, path)
        resp.headers["X-Thumb"] = "original"
        return resp
    try:
        blob = await asyncio.to_thread(_read_bytes, thumb)
    except OSError:
        # 缓存文件刚被别人（收尾）删掉了：这一张发原图，别报错。
        resp = await _png_response(request, path)
        resp.headers["X-Thumb"] = "original"
        return resp
    return Response(blob, media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache", "ETag": etag,
                             "X-Thumb": "jpeg", "X-Thumb-Note": note})


# 一次能删多少张。**不是**怕删得多，是怕一个手滑、或者一个坏客户端把整个输出
# 目录一次点掉。500 比任何真实页面都宽（一页 3 行，几十张），够用而不失控。
MAX_DELETE_ITEMS = 500


def _root_index(raw):
    """客户端给的根序号 → int；不合法返回 None。

    刻意**不**用 `int(raw)` 一把梭：`int(3.7)` 会悄悄截成 3、`int(True)` 是 1，
    而这里的下一步就是**按这个序号去删磁盘上的文件** —— 一个畸形的请求会去动
    另一个目录里的同名图，而且没有症状。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return None


async def post_gallery_delete(request):
    """把画廊里的图**移进回收站**（P20f）。

    三条形状上的决定，都是刻意的：

    1. **只有结构性错误才 400；某一项删不掉不牵连整批。** 与画廊扫描「逐项降级」
       同一套：12 张里 1 张找不到，另外 11 张该删还得删 —— 整批回滚反而什么都不
       说明（文件是各自独立的，没有事务可言）。
    2. **只删 `.png`、只在扫到的输出目录里**：判据是 `gallery.deletable()`，与取图
       共用同一份路径解析。客户端能点名删什么，所以这一层必须自己挡。
    3. **回执是账**：`trashed` 里每一张都是真的进了回收站的，`failed` 逐条带理由。
       页面照着它说话 —— 它不许比磁盘上真实发生的事多。
    """
    wb = request.app.state.wb
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return JSONResponse({"error": "items 必须是一个数组"}, status_code=400)
    if not items:
        return JSONResponse({"error": "没有要删的图"}, status_code=400)
    if len(items) > MAX_DELETE_ITEMS:
        return JSONResponse(
            {"error": "一次最多删 %d 张（这次收到 %d 张）"
                      % (MAX_DELETE_ITEMS, len(items))}, status_code=400)

    def work():
        # 候选目录只取一次：它的第一条来自后端 `/system_stats`（一次真网络往返），
        # 逐项去取等于删 20 张打 20 次后端。
        roots = gallery.candidate_roots(wb.client)
        trashed, failed, total = [], [], 0
        for raw in items:
            entry = raw if isinstance(raw, dict) else {}
            name = entry.get("name")
            root_index = _root_index(entry.get("root"))
            if not isinstance(name, str) or not name:
                failed.append({"root": entry.get("root"), "name": name,
                               "why": "没给文件名"})
                continue
            if root_index is None:
                failed.append({"root": entry.get("root"), "name": name,
                               "why": "root 必须是整数"})
                continue
            path, why = gallery.deletable(roots, root_index, name)
            if path is None:
                failed.append({"root": root_index, "name": name, "why": why})
                continue
            try:
                size = os.path.getsize(path)
                trash.trash(path)
            except trash.TrashError as exc:
                failed.append({"root": root_index, "name": name, "why": str(exc)})
                continue
            except OSError as exc:
                failed.append({"root": root_index, "name": name,
                               "why": "读不到它的大小：%s" % exc})
                continue
            total += size
            trashed.append({"root": root_index, "name": name, "bytes": size})
        return {"trashed": trashed, "failed": failed,
                "bytes": total, "count": len(trashed)}

    data = await asyncio.to_thread(work)
    # 破坏性动作必须留痕：出图有记录、删除没有的话，几天后看着少掉的那 40 张图
    # 没有任何东西能解释它们去哪了（这条进日志台的时间线）。
    logger.info("画廊删除：%d 张进了回收站（%.1f MB），%d 张没删成%s",
                data["count"], data["bytes"] / 1048576.0, len(data["failed"]),
                ("：" + "、".join((f.get("name") or "?")
                                  for f in data["failed"][:5]))
                if data["failed"] else "")
    return JSONResponse(data)


# ---------------------------------------------------------------------------
# 归档（P20h）：画廊那颗「删除」把图挪进 `config.ARCHIVE_DIR`，不扔回收站
#
# 为什么把「删了还能回来」从回收站挪回我们自己手里：回收站确实是这台机器上唯一一个
# 能撤销的地方，但它**看不见名字**（进去之后是一堆 `$R…`，要还原得开资源管理器、
# 再配 `$I` 侧车才知道它原来叫什么）。归档这一层是我们自己的：页面里一张张列着、
# 点一下就还原，而且**同一批图在「最近任务」里也跟着消失**。回收站退回它该待的位置
# —— 在「已归档」那一档里点「彻底删除」才轮到它（那一步仍然不硬删，见 trash.py）。
#
# 三个端点共用的形状与删除那一支逐条相同：只有结构性错误才 400、某一项搬不动不牵连
# 整批、**回执是账**（不许比磁盘上真实发生的事多）。
# ---------------------------------------------------------------------------
async def _gallery_batch(request, what):
    """解析 `{items:[…]}` → `(items, 错误回执)`（正常时后者是 None）。"""
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return None, JSONResponse({"error": str(exc)}, status_code=400)
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None, JSONResponse({"error": "items 必须是一个数组"}, status_code=400)
    if not items:
        return None, JSONResponse({"error": "没有要%s的图" % what}, status_code=400)
    if len(items) > MAX_DELETE_ITEMS:
        return None, JSONResponse(
            {"error": "一次最多处理 %d 张（这次收到 %d 张）"
                      % (MAX_DELETE_ITEMS, len(items))}, status_code=400)
    return items, None


def _move_file(src, dst):
    """把 `src` 挪到 `dst`。**先查后搬，绝不覆盖。**

    同卷上这本是一次改名，但 `os.replace` 会**静默覆盖**目标，而这里的目标名来自一个
    共享目录 —— 撞名有真实来源：ComfyUI 算下一个序号是**扫现存文件名**的，归档走一张
    `…_00088.png` 之后 CLI 那条路会**再写一张同名的**（见 `gallery.restore_target`）。
    静默覆盖是这里唯一「看起来像成功」的坏法，所以宁可多一次 stat。
    """
    if os.path.exists(dst):
        raise OSError("目标已经有同名的文件")
    shutil.move(src, dst)


async def post_gallery_archive(request):
    """把画廊里的图**挪进归档目录**。"""
    items, bad = await _gallery_batch(request, "归档")
    if bad is not None:
        return bad
    wb = request.app.state.wb

    def work():
        try:
            os.makedirs(config.ARCHIVE_DIR, exist_ok=True)
        except OSError as exc:
            # 整个动作的前提没了 → 这不是「某一项没成」，是一条 500 的实话。
            return {"error": "建不了归档目录 %s：%s" % (config.ARCHIVE_DIR, exc)}
        # 候选目录只取一次（它第一条来自后端 `/system_stats`，一次真往返）
        roots = gallery.candidate_roots(wb.client)
        archived, failed, total = [], [], 0
        for raw in items:
            entry = raw if isinstance(raw, dict) else {}
            name = entry.get("name")
            root_index = _root_index(entry.get("root"))
            if not isinstance(name, str) or not name:
                failed.append({"root": entry.get("root"), "name": name,
                               "why": "没给文件名"})
                continue
            if root_index is None:
                failed.append({"root": entry.get("root"), "name": name,
                               "why": "root 必须是整数"})
                continue
            path, why = gallery.archivable(roots, root_index, name)
            if path is None:
                failed.append({"root": root_index, "name": name, "why": why})
                continue
            base = os.path.basename(path)
            try:
                size = os.path.getsize(path)
                _move_file(path, os.path.join(config.ARCHIVE_DIR, base))
            except OSError as exc:
                failed.append({"root": root_index, "name": name,
                               "why": "搬不动：%s" % exc})
                continue
            total += size
            archived.append({"root": root_index, "name": base, "bytes": size})
        return {"archived": archived, "failed": failed, "bytes": total,
                "count": len(archived)}

    data = await asyncio.to_thread(work)
    if "error" in data:
        return JSONResponse(data, status_code=500)
    logger.info("画廊归档：%d 张挪进 %s（%.1f MB），%d 张没成%s",
                data["count"], config.ARCHIVE_DIR, data["bytes"] / 1048576.0,
                len(data["failed"]),
                ("：" + "、".join((f.get("name") or "?")
                                  for f in data["failed"][:5]))
                if data["failed"] else "")
    return JSONResponse(data)


async def post_gallery_restore(request):
    """把归档里的图搬回 `output/`。

    请求体是 `{items:[{name}]}` —— **没有 root**。归档里的东西只有一个来源，多一个
    能被伪造成数字的参数，就是多一条「按它去动另一个目录」的路（同 `_root_index`
    那段注释的理由）。撞名整项失败，理由里点名是谁挡着。
    """
    items, bad = await _gallery_batch(request, "还原")
    if bad is not None:
        return bad

    def work():
        restored, failed, total = [], [], 0
        for raw in items:
            entry = raw if isinstance(raw, dict) else {}
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                failed.append({"name": name, "why": "没给文件名"})
                continue
            src, why = gallery.resolve_archived(name)
            if src is None:
                failed.append({"name": name, "why": why})
                continue
            dest, why = gallery.restore_target(name)
            if dest is None:
                failed.append({"name": name, "why": why})
                continue
            try:
                size = os.path.getsize(src)
                _move_file(src, dest)
            except OSError as exc:
                failed.append({"name": name, "why": "搬不动：%s" % exc})
                continue
            total += size
            restored.append({"name": name, "bytes": size})
        return {"restored": restored, "failed": failed, "bytes": total,
                "count": len(restored)}

    data = await asyncio.to_thread(work)
    logger.info("画廊还原：%d 张搬回 %s（%.1f MB），%d 张没成%s",
                data["count"], config.OUTPUT_DIR, data["bytes"] / 1048576.0,
                len(data["failed"]),
                ("：" + "、".join((f.get("name") or "?")
                                  for f in data["failed"][:5]))
                if data["failed"] else "")
    return JSONResponse(data)


async def post_gallery_purge(request):
    """从归档里**彻底删除** —— 这一步才轮到回收站。

    为什么最后这一步仍然不硬删：`trash.py` 顶部那段话一个字没变 —— 这些图身上带着
    自己的提示词与参数，而「删掉」在这台机器上唯一一个能撤回的地方就是回收站。
    归档只是把「撤回」**提前**到更好找的一层，不是把它拿掉。
    """
    items, bad = await _gallery_batch(request, "彻底删除")
    if bad is not None:
        return bad

    def work():
        purged, failed, total = [], [], 0
        for raw in items:
            entry = raw if isinstance(raw, dict) else {}
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                failed.append({"name": name, "why": "没给文件名"})
                continue
            path, why = gallery.resolve_archived(name)
            if path is None:
                failed.append({"name": name, "why": why})
                continue
            try:
                size = os.path.getsize(path)
                trash.trash(path)
            except trash.TrashError as exc:
                failed.append({"name": name, "why": str(exc)})
                continue
            except OSError as exc:
                failed.append({"name": name, "why": "读不到它的大小：%s" % exc})
                continue
            total += size
            purged.append({"name": name, "bytes": size})
        return {"purged": purged, "failed": failed, "bytes": total,
                "count": len(purged)}

    data = await asyncio.to_thread(work)
    logger.info("画廊彻底删除：%d 张从归档进了回收站（%.1f MB），%d 张没成%s",
                data["count"], data["bytes"] / 1048576.0, len(data["failed"]),
                ("：" + "、".join((f.get("name") or "?")
                                  for f in data["failed"][:5]))
                if data["failed"] else "")
    return JSONResponse(data)


# ---------------------------------------------------------------------------
# 参考图（P4）
# ---------------------------------------------------------------------------
async def get_inputs(request):
    """参考图清单：后端**真正能读到**的文件，外加落点与它是否已验证。"""
    wb = request.app.state.wb
    data = await asyncio.to_thread(upload.list_inputs, wb.client)
    # 一次能挂几张，是**图**的约束（TextEncodeQwenImage21 的 autogrow 到 image_16），
    # 不是上传的约束 —— 所以它从构建器来，跟着这份清单一起交给页面，
    # 让前端在选第 17 张之前就能拦住，而不是等提交后收一句 400。
    data["max_references"] = qb.MAX_REFERENCE_IMAGES
    return JSONResponse(data)


async def post_upload(request):
    """上传一张参考图。收的是**原始字节**，不是 multipart。

    刻意不用 `request.form()`：它要 `python-multipart`，而那个包不在
    requirements.txt 里。为传一个文件把技术底座从「零安装」变成「要装」不划算 ——
    前端 `fetch(url, {method:'POST', body: file})` 天然就是原始字节，两边都省事。

    真正落盘的是 `upload.upload_any`：**先请 ComfyUI 自己写**（`POST /upload/image`），
    它不认或没起才退回本地写。为什么必须这样，见 `workbench/upload.py` 顶部 ——
    实测工作区解释器被那条按可执行文件位置判定的沙箱规则拒写过。
    """
    wb = request.app.state.wb
    name = (request.query_params.get("filename")
            or request.headers.get("x-filename") or "")
    overwrite = _truthy(request.query_params.get("overwrite") or "1")
    # 先看 Content-Length 再读体：否则一个 2 GB 的请求会先进内存，再被我们拒绝
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > upload.MAX_BYTES:
        return JSONResponse(
            {"error": "文件太大：%s 字节，上限 %d 字节" % (clen, upload.MAX_BYTES)},
            status_code=413)
    try:
        raw = await request.body()
    except Exception as exc:                       # 客户端半途断了
        return JSONResponse({"error": "读请求体失败：%s" % exc}, status_code=400)
    try:
        result = await asyncio.to_thread(upload.upload_any, wb.client, name, raw,
                                         overwrite)
    except upload.UploadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    # 新名字 201、覆盖同名 200 —— 两者都是「写成功」，但语义不同。
    # `existed` 是 None 时（后端那条路读不到候选表，无从判断）按 201 走：
    # 「不知道」时选那个**信息量更小**的状态码，别把没影的事说成「覆盖了」。
    return JSONResponse(result, status_code=200 if result.get("existed") else 201)


async def get_prompts(request):
    wb = request.app.state.wb
    q = request.query_params
    rows = await asyncio.to_thread(wb.library.list, q.get("q"), q.get("tag"))
    return JSONResponse({"prompts": rows, "tags": wb.library.tags(),
                         "total": wb.library.count()})


async def post_prompt(request):
    wb = request.app.state.wb
    try:
        body = await _json_body(request)
        row = await asyncio.to_thread(wb.library.create, body.get("text"),
                                      body.get("title"), body.get("tags"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(row, status_code=201)


async def put_prompt(request):
    wb = request.app.state.wb
    try:
        body = await _json_body(request)
        row = await asyncio.to_thread(
            wb.library.update, request.path_params["prompt_id"],
            body.get("text"), body.get("title"), body.get("tags"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if row is None:
        return JSONResponse({"error": "没有这条提示词"}, status_code=404)
    return JSONResponse(row)


async def delete_prompt(request):
    wb = request.app.state.wb
    ok = await asyncio.to_thread(wb.library.delete,
                                 request.path_params["prompt_id"])
    if not ok:
        return JSONResponse({"error": "没有这条提示词"}, status_code=404)
    return Response(status_code=204)


async def post_prompt_use(request):
    wb = request.app.state.wb
    row = await asyncio.to_thread(wb.library.touch,
                                  request.path_params["prompt_id"])
    if row is None:
        return JSONResponse({"error": "没有这条提示词"}, status_code=404)
    return JSONResponse(row)


# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------
async def get_index(request):
    return FileResponse(config.INDEX_HTML,
                        headers={"Cache-Control": "no-store"})


async def get_health(request):
    wb = request.app.state.wb
    return JSONResponse({"ok": True, "jobs": wb.store.count(),
                         "subscribers": wb.broker.subscriber_count,
                         "watcher": wb.watcher.state()})


class StaticNoCache:
    """给 `/static/*` 补一条 `Cache-Control: no-cache`（P20k）。

    **为什么必须补**：HTML 那条路是 `no-store`（见 `get_index`），而静态文件
    **一条缓存指令都没有**（本机 starlette 是 1.7.0，`StaticFiles` 已经没有
    `max_age` 这个参数了 —— 实测响应头里只有 ETag 与 Last-Modified）。两边各按
    各的规矩来，浏览器就可能拿**上一版**的 `app.js` 配**这一版**的 `index.html`。
    那个组合会让 `init()` 在第一行就抛（新 HTML 里没有旧 JS 要摸的那个元素），
    症状是「整页像冻住了、点哪儿都没反应」，而且**没有任何提示** ——
    2026-10-03 用户报的就是它（`logs/ui_hybrid_probe.py` 把两种坏法都复现了）。

    `no-cache` = **每次都回源校验**（带 ETag，通常是一个 304，几毫秒）——要的是
    「不许不问就用旧的」，不是「不许缓存」。别改成 `no-store`：那会把每次刷新都
    变成一次完整的重传（174 KB 的 app.js）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message["headers"] = headers
            await send(message)

        return await self.app(scope, receive, send_wrapper)


def create_app(wb=None):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    # 日志台那个环：**在建 Workbench 之前**装 —— 建对象过程中写下的记录也要收得到。
    # `install()` 是幂等的：测试会反复调 create_app，不幂等的话一行会记成好几行。
    logbuf.install()
    wb = wb or Workbench()

    @asynccontextmanager
    async def lifespan(app):
        # 事件循环在这里才拿得到，Broker 的跨线程发布靠它。
        # 用 lifespan 而不是 `@app.on_event` —— 本机装的 starlette 1.7.0
        # 已经**移除**了 on_event（实测 hasattr 为 False），照着旧文档写会直接崩。
        wb.broker.bind_loop(asyncio.get_running_loop())
        wb.start()
        # 没装 Pillow = 画廊格子直接发原图（一页可能几十 MB）。这件事**只有日志台
        # 看得出来** —— 页面上仍旧有图，只是慢，属于「没有症状」的那一类。
        if not thumbs.available():
            logger.warning("没装 Pillow（%s）—— 画廊格子会直接发原图。"
                           "装上它就快：pip install -r requirements-lock.txt",
                           thumbs.why_unavailable())
        # uvicorn 到这一步才配完日志（`dictConfig` 会把先前挂在它那两个 logger
        # 上的 handler 清掉），所以**这一句必须放在这里**，不能放进 `create_app`。
        # 少了它，工作台里抛异常返回 500 时，日志台上一个字都不会有。
        logbuf.attach_to_uvicorn()
        try:
            yield
        finally:
            wb.stop()

    app = Starlette(routes=[
        Route("/", get_index),
        Route("/api/health", get_health),
        Route("/api/backend", get_backend),
        Route("/api/backend/start", post_backend_start, methods=["POST"]),
        Route("/api/backend/stop", post_backend_stop, methods=["POST"]),
        Route("/api/backend/launch", get_backend_launch),
        Route("/api/backend/log", get_backend_log),
        # 日志台（P20）：一个只读的数据源 + 三个 LLM 端点。
        # `/api/logs` 独立于 SSE 是**刻意的** —— SSE 断了的时候正是最需要看日志
        # 的时候，那时一个挂在事件流上的日志视图会跟着一起瞎掉。
        Route("/api/logs", get_logs),
        Route("/api/llm", get_llm),
        Route("/api/llm", post_llm, methods=["POST"]),
        Route("/api/llm/enhance", post_llm_enhance, methods=["POST"]),
        Route("/api/build", post_build, methods=["POST"]),
        Route("/api/run", post_run, methods=["POST"]),
        Route("/api/sweep", post_sweep, methods=["POST"]),
        Route("/api/sweep/{sweep_id}", get_sweep),
        Route("/api/sweep/{sweep_id}/cancel", post_sweep_cancel,
              methods=["POST"]),
        Route("/api/jobs", get_jobs),
        # 放在 `{job_id}` 前面：路径一样是 `/api/jobs/…`，靠「POST 与 GET 不同」
        # 也能落到对的那条（Starlette 会记住部分匹配），但那是**依赖路由器的
        # 一个细节**，而顺序写对是零成本的。
        Route("/api/jobs/clean", post_jobs_clean, methods=["POST"]),
        Route("/api/jobs/{job_id}", get_job),
        Route("/api/jobs/{job_id}/timings", get_job_timings),
        Route("/api/jobs/{job_id}/cancel", post_cancel, methods=["POST"]),
        Route("/api/events", get_events),
        Route("/api/image", get_image),
        # 视频产物单独一个口：它要 `Range`（拖动进度条），而且落在
        # `output/video/` 子目录里 —— 那两点 `/api/image` 都办不到。
        Route("/api/media", get_media),
        Route("/api/inputs", get_inputs),
        Route("/api/upload", post_upload, methods=["POST"]),
        Route("/api/gallery", get_gallery),
        Route("/api/gallery/image", get_gallery_image),
        # 缩略图（P20l）：格子上的小图。与 image 同一套参数，只是先缩一道。
        Route("/api/gallery/thumb", get_gallery_thumb),
        Route("/api/gallery/delete", post_gallery_delete, methods=["POST"]),
        # 归档（P20h）：正常档那颗按钮走 archive，归档档那两颗走 restore / purge。
        # 三条都走同一个批量形状，判据也共用 `gallery.resolve_output_file` 那一层。
        Route("/api/gallery/archive", post_gallery_archive, methods=["POST"]),
        Route("/api/gallery/restore", post_gallery_restore, methods=["POST"]),
        Route("/api/gallery/purge", post_gallery_purge, methods=["POST"]),
        Route("/api/prompts", get_prompts),
        Route("/api/prompts", post_prompt, methods=["POST"]),
        Route("/api/prompts/{prompt_id}", put_prompt, methods=["PUT"]),
        Route("/api/prompts/{prompt_id}", delete_prompt, methods=["DELETE"]),
        Route("/api/prompts/{prompt_id}/use", post_prompt_use, methods=["POST"]),
        Mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static"),
    ], lifespan=lifespan)
    # 静态文件不许「不问就用旧的」（P20k）：HTML 是 `no-store`，静态文件也得回源
    # 校验，否则两份可能来自不同的版本，而那种坏法整页没有任何提示。
    app.add_middleware(StaticNoCache)
    app.state.wb = wb
    return app


def main(argv=None):
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="Qwen-Image-2.1 工作台")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--no-watch", action="store_true",
                    help="不起 ComfyUI 监听线程（只调前端时用）")
    a = ap.parse_args(argv)
    app = create_app()
    if a.no_watch:
        app.state.wb.start = lambda: None
    print("工作台 → http://%s:%d" % (a.host, a.port))
    # 自己给自己在时间线上留一行「什么时候起的」—— 刚启动时那一栏如果空空如也，
    # 看起来像「没接上」；而「刚才重启过没有」恰恰是排查时第一个要问的问题。
    # 解释器与状态目录**不写在这里**（两个绝对路径会把时间线那一栏撑成五行折行），
    # 它们在 `/api/backend` 的 `runtime` 里，由「环境体检」那一块显示。
    logger.info("工作台已启动 → http://%s:%d", a.host, a.port)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
