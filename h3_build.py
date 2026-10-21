#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax H3 图生视频 —— 从基准 API 图构建一份可直接提交给 ComfyUI 的图。

与 `qwen21_build.py` **平级但不相干**：那是 Qwen-Image-2.1 那条链路，它被五份黄金
快照逐字节钉死。两条链路只有一处共用代码（`qwen21_build.unique_save_prefix`），
理由写在那边。

## 基准图从哪来

`h3_api_base.json` 是 `generated/minimaxh3_browser_api.json` 的**逐字副本** ——
后者是 2026-10-10 08:21 实测 `success / 316.9 秒` 的那份 20 节点图。节点 id
**刻意不重排**，这样它能继续与那次已验证的产物对 diff；生成脚本
`logs/p21_make_h3_base.py` 里带着冻结值表，改错一个值就当场炸。

## 冻结在基准图里的东西（本次**不**放开，理由都在下面）

| 项 | 值 | 为什么不放开 |
|---|---|---|
| `126 BasicScheduler.steps` | **4** | 这是 `lightx2v_turbo_4step` 蒸馏 LoRA 的固定档，改了就换了条曲线 |
| `140 LoraLoaderModelOnly.strength_model` | **0.75** | 只有一次实测，没有第二档数据 |
| `115 ResolutionSelector` | 9:16 / 0.4 MP / ×32 | 见下一条 |
| `340 ImageResizeKJv2` | 480 × 864 | 与 `115` 是**同一个尺寸的两处真源** |
| `125 KSamplerSelect` / `129` / `130` / `121` / `122` | — | 模型与采样器 |

### `115` 与 `340` 为什么必须一起看

`ResolutionSelector(115)` 算出 `width`/`height` 喂给 `MiniMaxH3ImageToVideo`，而
`ImageResizeKJv2(340)` 把首帧裁成 480×864 喂它的 `first_frame`。两处不一致时画面
会被裁两次，而**没有症状**（照样出一个 480×864 的视频，只是构图不是你以为的那个）。

2026-10-12 把这条算清了：`ResolutionSelector` 的实现是
`comfy_extras/nodes_resolution.py:80-86`，用的是 **`megapixels × 1024²`**（不是
`× 1e6`）：

    scale  = sqrt(0.4 × 1024² / (9 × 16)) = 53.9695
    width  = round(9  × scale / 32) × 32 = 15 × 32 = 480
    height = round(16 × scale / 32) × 32 = 27 × 32 = 864

⇒ `(480, 864)`，与 `340` **一致**。（`logs/wf_analyze3_out.txt` 里记的 `480×832`
是按 `× 1e6` 算的，那个读数是错的 —— 原始 UI 文件里 `340` 的陈旧 `832` 也是同一个
来源。）`tests/test_h3_build.py` 用 `resolution_for()` 把这条钉住。

## 时长会被吸附到 17 帧网格

`134 ComfyMathExpression` 的表达式是
`max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17`，
`a` 来自 `135 PrimitiveFloat.value`。所以用户给的「秒」是**近似**：
5 秒 → 124 帧 → 5.167 秒；10 秒 → 243 帧 → 10.125 秒。
`duration_frames()` / `duration_seconds()` 把这条算出来给页面显示 ——
**页面不许自己写死那几个数**，否则它与基准图里的公式迟早分家。
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # …/qwen21-workbench
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # `project_paths` 与本模块同级

import project_paths  # noqa: E402

WORKSPACE = os.environ.get("QWEN21_WORKSPACE") or os.path.dirname(HERE)
# 与 `qwen21_build.BASE_FILE` 同一条规矩 —— 定义在 `project_paths.base_file()`：
# 环境变量 → 仓库自带的 `workflows/` → 工作区根。
BASE_FILE = project_paths.base_file("QWEN21_H3_BASE", "h3_api_base.json",
                                    repo=HERE, workspace=WORKSPACE)

# 这条链路的模式名。`qwen21_build.MODES` **不含它**（那是 Qwen 那三个形状的列表）。
MODE = "h3"

# ---------------------------------------------------------------------------
# 节点 id —— 从基准图抄来，**不重排**（按 id 认是错的，见 `is_h3()`）
# ---------------------------------------------------------------------------
LOAD_IMAGE = "114"        # LoadImage            ← 首帧
RESIZE = "340"            # ImageResizeKJv2      ← 裁首帧到 480×864
RESOLUTION = "115"        # ResolutionSelector   ← 给 I2V 的 width/height
I2V = "133"               # MiniMaxH3ImageToVideo
DURATION = "135"          # PrimitiveFloat       ← 用户给的秒数（近似）
MATH = "134"              # ComfyMathExpression   ← 吸附到 17 帧网格
NOISE = "131"             # RandomNoise          ← 种子
SCHEDULER = "126"         # BasicScheduler
SAMPLER = "125"           # KSamplerSelect
LORA = "140"              # LoraLoaderModelOnly
UNET = "129"              # UNETLoader
CLIP = "130"              # CLIPLoader
SAVE_VIDEO = "189"        # SaveVideo            ← 收尾

#: 收尾节点**按类名认**。`qwen21_build.SAVE_CLASSES` 也认它（`unique_save_prefix`
#: 要能改它的 `filename_prefix`，否则同参数再提一次会「秒完成 + 没有产物」）。
SAVE_CLASS = "SaveVideo"

# ---------------------------------------------------------------------------
# 体检表
# ---------------------------------------------------------------------------
# 形状与 `config.REQUIRED_MODELS` 逐字相同（`(key, 节点类, 字段, 文件名)`），
# 因为 `comfy.ComfyClient.models_report(entries=…)` 走的就是那一条路 —— 报错文案
# 与那四个 Qwen 模型**共用一份**，不另写第二套说法。
#
# **刻意不写进 `config.REQUIRED_MODELS`**：那四个是 Qwen 出图链路的一部分，
# 缺了那条链路整条出不了图；这五个只影响 H3。写进去会让没下 H3 权重的人
# **每次看出图体检都是琥珀色**。所以它由 `server._preflight_extras` 在选中 H3 时
# **整份替换**掉那四个（H3 一个都不用 Qwen 的模型）。
MODEL_DIT = "Minimax_H3\\minimax_h3_fl2va_pruned_int8_convrot.safetensors"
MODEL_TEXT_ENCODER = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
MODEL_VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
MODEL_AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
MODEL_LORA = ("minimax_h3\\minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy"
              ".safetensors")

#: ⚠ 这两条路径**待出图主机回来后用组合框候选表核实**（计划 §7 第 0 步）。
#: `UNETLoader.unet_name` 与 `LoraLoaderModelOnly.lora_name` 的候选表里带不带
#: `Minimax_H3\` 那层子目录，决定这里要不要写反斜杠。判据是后端自己的候选表。
REQUIRED_MODELS = (
    ("h3_dit", "UNETLoader", "unet_name", MODEL_DIT),
    ("h3_text_encoder", "CLIPLoader", "clip_name", MODEL_TEXT_ENCODER),
    ("h3_video_vae", "VAELoader", "vae_name", MODEL_VIDEO_VAE),
    ("h3_audio_vae", "VAELoader", "vae_name", MODEL_AUDIO_VAE),
    ("h3_lora", "LoraLoaderModelOnly", "lora_name", MODEL_LORA),
)

#: ComfyUI 自带的节点类。目录名留空 —— 缺了它们不是「插件没装」，
#: 而是「后端版本太旧」，两者的处置完全不同，所以文案要分得开。
CORE_NODE = ""

#: 可选插件的节点类，形状同 `config.REQUIRED_NODES`：`(key, 节点类, 目录名)`。
#:
#: **只列这三个**：前两个是插件节点（缺了要去装包），第三个是版本判据
#: （`MiniMaxH3ImageToVideo` 是 0.39 才有的）。`CreateVideo` / `SaveVideo` /
#: `VAEDecodeAudio` 与它同批发布，真缺了的话提交时 ComfyUI 自己会打回
#: `missing_node_type` —— 多问三次 `/object_info` 换不来多一分可操作性。
REQUIRED_NODES = (
    ("h3_i2v", "MiniMaxH3ImageToVideo", CORE_NODE),
    ("h3_resize", "ImageResizeKJv2", "ComfyUI-KJNodes"),
    ("h3_math", "ComfyMathExpression", "ComfyMath"),
)

# ---------------------------------------------------------------------------
# 时长：17 帧网格
# ---------------------------------------------------------------------------
FPS = 24
FRAME_GRID = 17
#: 与基准图 `134.expression` **逐字相同**。改这里必须同时改那份 JSON，反之亦然 ——
#: `tests/test_h3_build.py` 逐字比这两处。
MATH_EXPRESSION = ("max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17")

DEFAULT_SECONDS = 5
#: **冻结**的采样步数 —— `lightx2v_turbo_4step` 蒸馏 LoRA 的固定档，页面上没有
#: 这个旋钮。它写出来只为了两件事：任务记录里那一栏有值（而不是一个空洞），
#: 以及参数扫描能按它拒掉一条 steps 轴（扫了只会得到几段一样的视频）。
STEPS = 4

#: **冻结**的输出尺寸（`115` 算出、`340` 裁成）。两处必须一致，见下面
#: 「`115` 与 `340` 为什么必须一起看」。写成常数只是为了体检文案能引用它，
#: **真源仍然是基准图** —— `tests/test_h3_build.py` 拿算法算一遍再比。
TARGET_WIDTH = 480
TARGET_HEIGHT = 864
#: 页面上给的那几档。**10 秒这一档没跑过**（只实测过 5 秒），所以它在新页面上
#: 先带「未实测」标注，跑通后再去掉。
SECONDS_CHOICES = (5, 10)
MIN_SECONDS = 1
MAX_SECONDS = 30

# ---------------------------------------------------------------------------
# 分辨率：`ResolutionSelector` 的算法（逐字复刻）
# ---------------------------------------------------------------------------
#: 取自 `comfy_extras/nodes_resolution.py:18-27`（本机 v0.37.4；出图主机 v0.39.1 的
#: `/object_info` 里 `aspect_ratio` 候选表与它逐项相同，所以这份表是稳的）。
ASPECT_RATIO_TABLE = {
    "1:1 (Square)": (1, 1),
    "2:3 (Portrait Photo)": (2, 3),
    "3:2 (Photo)": (3, 2),
    "3:4 (Portrait Standard)": (3, 4),
    "4:3 (Standard)": (4, 3),
    "9:16 (Portrait Widescreen)": (9, 16),
    "16:9 (Widescreen)": (16, 9),
    "21:9 (Ultrawide)": (21, 9),
}


def resolution_for(aspect_ratio, megapixels, multiple):
    """`ResolutionSelector` 实算 —— 与 `comfy_extras/nodes_resolution.py:80-86` 同式。

    **注意是 `megapixels × 1024²` 而不是 `× 1e6`** —— 差这一个常数就是 480×864
    与 480×832 的差别，而后者会让 `ImageResizeKJv2` 的裁剪比例偏离。

    `round()` 用的是 Python 的银行家舍入（四舍六入五成双），**必须照抄**：
    换成 `math.floor(x + 0.5)` 会在 `.5` 的档位上给出不同的边长。
    """
    try:
        w_ratio, h_ratio = ASPECT_RATIO_TABLE[aspect_ratio]
    except KeyError:
        raise ValueError("未知宽高比 %r，可选：%s"
                         % (aspect_ratio, "、".join(ASPECT_RATIO_TABLE)))
    if not isinstance(multiple, int) or isinstance(multiple, bool) or multiple <= 0:
        raise ValueError("multiple 必须是正整数，收到 %r" % (multiple,))
    total_pixels = float(megapixels) * 1024 * 1024
    scale = math.sqrt(total_pixels / (w_ratio * h_ratio))
    width = round(w_ratio * scale / multiple) * multiple
    height = round(h_ratio * scale / multiple) * multiple
    return width, height


def duration_frames(seconds):
    """把「秒」换算成基准图那条表达式真正算出的帧数。

    `seconds=None` 按 `DEFAULT_SECONDS` 算 —— 这条换算必须**总是**给得出一个数，
    因为页面要在用户还没动过那个下拉时就把「实际多少帧」显示出来。
    """
    a = DEFAULT_SECONDS if seconds is None else _check_seconds(seconds)
    base = max(5, round(a * FPS))
    return base + (5 - (base % FRAME_GRID)) % FRAME_GRID


def duration_seconds(seconds):
    """真实时长（秒，浮点）。用户给的那个数是近似的，这个才是产物里的那个。"""
    return duration_frames(seconds) / float(FPS)


def _check_seconds(value):
    """校验并**原样返回**（int 还是 int）。

    刻意不做 `float(value)` 归一：`5` 与 `5.0` 在这份图里都能跑，但把它们来回换会
    让「`build()` 只改我传的那几处」这条判据变成假绿 —— 那种判据一旦松掉，
    就再也发现不了「悄悄改了别的东西」。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds 必须是数字，收到 %r" % (value,))
    if not (MIN_SECONDS <= value <= MAX_SECONDS):
        raise ValueError("seconds 必须在 %d..%d 秒之间，收到 %r"
                         % (MIN_SECONDS, MAX_SECONDS, value))
    return value


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
def load_base(path=None):
    """读基准 API 图。每次返回全新对象，调用方不必担心改到别人。"""
    path = path or BASE_FILE
    if not os.path.exists(path):
        raise FileNotFoundError("缺少 H3 基准 API 图: %s" % path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_h3(wf):
    """这张图是不是 H3 图（按**节点类**认，不按 id —— id 是我们铸的，见 `upscale`）。"""
    for node in (wf or {}).values():
        if isinstance(node, dict) and node.get("class_type") == "MiniMaxH3ImageToVideo":
            return True
    return False


def build(*, prompt, first_frame, seconds=None, seed=None, base=None):
    """从基准图构建一份 H3 图。

    只动四处：`114.image`（首帧）、`135.value`（秒数）、`131.noise_seed`（种子）、
    `133.prompt`（提示词）。其余一个字节不改 —— 那次 316.9 秒的成功就是这份图给的。

    参数为 None 表示「不改动基准里的原值」，而不是「设成 None」（与 `qwen21_build.build`
    同一条规矩）。
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 必须是非空字符串")
    if not isinstance(first_frame, str) or not first_frame.strip():
        raise ValueError("first_frame 必须是非空字符串（ComfyUI input/ 里的文件名）")
    if seed is not None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed 必须是 >= 0 的整数，收到 %r" % (seed,))
    secs = _check_seconds(seconds)

    wf = load_base(base)
    _require(wf, I2V, "MiniMaxH3ImageToVideo")
    _require(wf, SAVE_VIDEO, "SaveVideo")

    wf[LOAD_IMAGE]["inputs"]["image"] = first_frame.strip()
    wf[I2V]["inputs"]["prompt"] = prompt
    # 只为「不改动基准里的原值」这条规矩才判 None：基准图自己那份 5 秒是那次
    # 316.9 秒实测用的值，没表态就别动它。
    if secs is not None:
        wf[DURATION]["inputs"]["value"] = secs
    if seed is not None:
        wf[NOISE]["inputs"]["noise_seed"] = seed
    return wf


def _require(wf, node_id, class_type):
    """少一个关键节点就是「这份基准图不是 H3 图」—— 当场说，别让它跑成一次白等。"""
    node = wf.get(node_id) or {}
    if node.get("class_type") != class_type:
        raise ValueError("基准图里节点 %s 应当是 %s，实际是 %r"
                         % (node_id, class_type, node.get("class_type")))


def describe(wf):
    """从 H3 图反解出参数 —— 任务记录与页面回显用。"""
    i2v = (wf.get(I2V) or {}).get("inputs") or {}
    sched = (wf.get(SCHEDULER) or {}).get("inputs") or {}
    res = (wf.get(RESOLUTION) or {}).get("inputs") or {}
    rz = (wf.get(RESIZE) or {}).get("inputs") or {}
    dur = (wf.get(DURATION) or {}).get("inputs") or {}

    width = height = resize_w = resize_h = None
    if res:
        try:
            width, height = resolution_for(res.get("aspect_ratio"),
                                           res.get("megapixels"),
                                           res.get("multiple"))
        except (ValueError, TypeError):
            # 基准图被改坏了也要能反解出别的字段 —— 这里是外部数据，
            # 不该让整个画廊/记录渲染崩掉（与 `comfy.outputs_from_history` 同一条）。
            pass
    if rz.get("width") is not None:
        resize_w, resize_h = rz.get("width"), rz.get("height")

    seconds = dur.get("value")
    try:
        frames = duration_frames(seconds)
        real_s = duration_seconds(seconds)
    except (ValueError, TypeError):
        frames, real_s = None, None

    return {
        "mode": MODE,
        "prompt": i2v.get("prompt", ""),
        "first_frame": (wf.get(LOAD_IMAGE) or {}).get("inputs", {}).get("image"),
        "seconds": seconds,
        "frames": frames,
        "duration_s": None if real_s is None else round(real_s, 3),
        "seed": (wf.get(NOISE) or {}).get("inputs", {}).get("noise_seed"),
        "steps": sched.get("steps"),
        "scheduler": sched.get("scheduler"),
        # `115` 算出来的那个尺寸（喂给 I2V 的 width/height）
        "width": width,
        "height": height,
        # `340` 裁首帧用的那个尺寸。**两处必须一致**，不一致就是裁剪比例会偏。
        "resize_width": resize_w,
        "resize_height": resize_h,
        "resize_matches": (width is not None and resize_w is not None
                           and (width, height) == (resize_w, resize_h)),
        "lora_strength": (wf.get(LORA) or {}).get("inputs", {}).get("strength_model"),
        "save_prefix": (wf.get(SAVE_VIDEO) or {}).get("inputs", {}).get(
            "filename_prefix"),
    }
