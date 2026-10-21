#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""See-through 单图拆层 —— 从基准 API 图构建一份可直接提交给 ComfyUI 的图。

与 `qwen21_build.py` 平级但不相干，理由同 `h3_build.py`。

## 基准图从哪来

`seethrough_api_base.json` 是**手写**的 7 节点 API 图，接线照官方
`workflows/seethrough-basic.json`，参数照 `docs/seethrough-deploy.md` 第 6 步
**已实测**的那一套。

**刻意不从官方那份 UI 工作流转**：它用的是在线 repo id ＋ `auto_download: true`，
会去够 `huggingface.co`，而出图主机连不上那里（15 秒超时黑洞）——
整次跑会在退避 5 次后死掉，报一个看不懂的错。见下面「冻结」那一节。

## 冻结在基准图里的东西（本次**不**放开）

| 项 | 值 | 为什么不放开 |
|---|---|---|
| `2/3 model` | 本地目录名 | 选目录名 ⇒ 插件 `is_local=True` ⇒ `local_files_only=True`，**全程不碰网络** |
| `2/3 quant_mode` | **`nf4`** | **必须与目录配套**：`none` 会拿 `.to(bfloat16)` 去怼 bnb 的 4-bit 参数 |
| `2/3 auto_download` | **`false`** | 兜底保险。留 `true` 就会去够那个死黑洞 |
| `2/3 cache_tag_embeds` | `true` | 省约 2 GB 显存、**零速度代价** |
| `2/3 group_offload` | `false` | 22 GB 不需要；开了慢 **2.8×** |
| `6 tblr_split` | `true` | 对称部件拆左右 |
| `6 use_lama` | `true` | 前后发拆分。**缺检查点时会静默退化成「不拆」** ⇒ 由体检警告，不做成复选框 |

这些做成复选框，每一个都是一处**没有症状的错**：用户勾了、跑得通、结果不对。

## 三个可调项

`resolution`（512..2048，64 的倍数）、`num_inference_steps`（1..100）、
`resolution_depth`（`-1` = 跟随 `resolution`，或 64 的倍数）。

### `resolution_depth` 的默认值为什么是 `-1`

插件默认就是 `-1`（跟随 `resolution`），但在 **bf16** 下那会 OOM
（实测 1024 跟随必炸，降到 512 才活）。2026-10-11 把 compute dtype 换成 **fp16**
之后 `-1` 在 1024 与 1280 上都跑得动（252.40 / 403.20 秒），所以默认跟着走。

**`720` 不在候选里**：它是 bf16 时代那个「降到 720 仍然 OOM」的临时档，而 720 不是
节点 `step=64` 的整数倍。fp16 之后那一档的用处整个消失了。

## 产物命名（`artifacts.py` 与画廊的排除规则都靠它）

`SeeThrough_SavePSD.save()` 的格式是
`f"{filename_prefix}_{%Y%m%d_%H%M%S}_{uuid4()[:8]}_{tag}.png"`，深度图多一个
`_depth` 后缀，另有一份 `…_layers.json` 列出全部层与它们的 bbox / `depth_median`。

**收尾前缀会被 `qwen21_build.unique_save_prefix` 再插一段 `job_id[:8]`**，所以真实
文件名里有两组八位十六进制。`LAYER_RE` 因此只认**尾巴**的形状，不认开头 ——
这样前缀怎么变都不会让它失配。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # …/qwen21-workbench
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # `project_paths` 与本模块同级

import project_paths  # noqa: E402

WORKSPACE = os.environ.get("QWEN21_WORKSPACE") or os.path.dirname(HERE)
# 与 `qwen21_build.BASE_FILE` 同一条规矩 —— 定义在 `project_paths.base_file()`：
# 环境变量 → 仓库自带的 `workflows/` → 工作区根。
BASE_FILE = project_paths.base_file("QWEN21_LAYERS_BASE", "seethrough_api_base.json",
                                    repo=HERE, workspace=WORKSPACE)

MODE = "layers"

# ---------------------------------------------------------------------------
# 节点 id
# ---------------------------------------------------------------------------
LOAD_IMAGE = "1"          # LoadImage
LOAD_LAYERDIFF = "2"      # SeeThrough_LoadLayerDiffModel
LOAD_DEPTH = "3"          # SeeThrough_LoadDepthModel
GEN_LAYERS = "4"          # SeeThrough_GenerateLayers
GEN_DEPTH = "5"          # SeeThrough_GenerateDepth
POST = "6"                # SeeThrough_PostProcess
SAVE = "7"                # SeeThrough_SavePSD   ← 收尾

SAVE_CLASS = "SeeThrough_SavePSD"
SAVE_PREFIX = "seethrough"
#: 收尾节点产出的那个清单文件的后缀。
LAYERS_JSON_SUFFIX = "_layers.json"

#: 层文件名（含深度图）的形状，**带命名分组** —— `artifacts.py` 要靠
#: `prefix` 那一组把「这一批」与别的作业区分开。
#:
#:   …_<YYYYmmdd>_<HHMMSS>_<uuid8>_<tag>.png
#:
#: `prefix` 用**非贪婪**：`SeeThrough_SavePSD` 是按 `prefix_ts_uid_tag` 的顺序拼的，
#: 所以最早的那个合法切分才是真的那个。
LAYER_RE = re.compile(
    r"^(?P<prefix>.+?)_(?P<ts>\d{8})_(?P<time>\d{6})_(?P<uid>[0-9a-f]{8})_"
    r"(?P<tag>.+)\.png$")

#: 那份清单：`…_<uid>_layers.json`（注意这里 `_layers` 占了 tag 的位置）。
LAYERS_JSON_RE = re.compile(
    r"^(?P<prefix>.+?)_(?P<ts>\d{8})_(?P<time>\d{6})_(?P<uid>[0-9a-f]{8})_"
    r"layers\.json$")


def token_in_prefix(prefix, token):
    """`token` 是不是这个前缀里**独立的一段**。

    独立成段（而不是子串）才能把 `a1b2c3d4` 与 `xa1b2c3d4y` 分开 ——
    `unique_save_prefix` 插进去的那段永远夹在下划线之间，所以这条判据是精确的。
    """
    if not token:
        return False
    return token in str(prefix or "").split("_")

# ---------------------------------------------------------------------------
# 冻结值 —— 体检与单测都读这几份，页面不许自己再抄一遍
# ---------------------------------------------------------------------------
#: 本地目录名。**待出图主机回来后用组合框候选表核实**（计划 §7 第 0 步）：
#: 现在这两个值来自 `docs/seethrough-deploy.md`，而那边后来把目录改名带上了 `_fp16`。
#: 判据是后端自己的 `SeeThrough_LoadLayerDiffModel.model` 候选表，不是猜。
DIR_LAYERDIFF = "seethroughv0.0.2_layerdiff3d_nf4_fp16"
DIR_DEPTH = "seethroughv0.0.1_marigold_nf4_fp16"

QUANT_MODE = "nf4"
AUTO_DOWNLOAD = False
CACHE_TAG_EMBEDS = True
GROUP_OFFLOAD = False
TBRL_SPLIT = True
USE_LAMA = True

#: LaMa 是插件里唯一一个**写死完整 URL** 的外部依赖，`HF_ENDPOINT` 救不了它 ——
#: 只能把文件预放进 torch 自己的缓存。缺了不会崩，只会**静默地不拆前后发**，
#: 所以它是一条体检项而不是一个复选框。
LAMA_CKPT_NAME = "lama_large_512px.ckpt"

#: 相对 **torch 缓存根**（就是 `$TORCH_HOME`，默认 `~/.cache/torch`）的路径。
#: 刻意**不含** `.cache/torch` 那两段：根在哪由 `TORCH_HOME` 说了算，而生产后端是
#: 开机任务以 **SYSTEM** 起的 —— 按 `~` 去拼会指到 SYSTEM 的 profile，检查点明明
#: 在也报「不在」（实测 2026-10-12：体检那条警告就是这么凭空冒出来的）。
LAMA_CKPT_REL = os.path.join("hub", "checkpoints", LAMA_CKPT_NAME)
LAMA_CKPT_BYTES = 204544673

# ---------------------------------------------------------------------------
# 体检表
# ---------------------------------------------------------------------------
# 形状与 `config.REQUIRED_MODELS` / `config.REQUIRED_NODES` 逐字相同，因为
# `comfy.ComfyClient.models_report(entries=…)` 与 `node_presence(entries=…)` 走的就是
# 那两条路 —— 报错文案与出图那套**共用一份**，不另写第二套说法。
#
# 与 H3 同理：**不写进 `config.REQUIRED_MODELS`**，否则没下这套权重的人每次看
# 出图体检都是琥珀色。选中拆层时由 `server._preflight_extras` **整份替换**。
#
# ⚠ 两个目录名**待出图主机回来后用组合框候选表核实**（计划 §7 第 0 步）。
REQUIRED_MODELS = (
    ("layers_layerdiff", "SeeThrough_LoadLayerDiffModel", "model", DIR_LAYERDIFF),
    ("layers_depth", "SeeThrough_LoadDepthModel", "model", DIR_DEPTH),
)

PLUGIN_FOLDER = "ComfyUI-See-through"

#: **只列两个**：一个证明插件装了，一个是收尾（少了它就写不出层文件）。
#: 另外四个与它们出自同一个 `nodes.py`，真缺了提交时 ComfyUI 会自己打回 ——
#: 多问四次 `/object_info` 换不来多一分可操作性。
REQUIRED_NODES = (
    ("layers_generate", "SeeThrough_GenerateLayers", PLUGIN_FOLDER),
    ("layers_save", "SeeThrough_SavePSD", PLUGIN_FOLDER),
)

# ---------------------------------------------------------------------------
# 可调项的取值域（与节点的 INPUT_TYPES 对齐）
# ---------------------------------------------------------------------------
DEFAULT_RESOLUTION = 1024
RESOLUTION_CHOICES = (1024, 1280)
MIN_RESOLUTION = 512
MAX_RESOLUTION = 2048
RESOLUTION_STEP = 64

DEFAULT_STEPS = 30
MIN_STEPS = 1
MAX_STEPS = 100

#: `-1` 表示跟随 `resolution`。
DEPTH_FOLLOW = -1
DEPTH_CHOICES = (-1, 512, 640, 768)


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
def load_base(path=None):
    """读基准 API 图。每次返回全新对象，调用方不必担心改到别人。"""
    path = path or BASE_FILE
    if not os.path.exists(path):
        raise FileNotFoundError("缺少 See-through 基准 API 图: %s" % path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_layers(wf):
    """这张图是不是拆层图（按**节点类**认，不按 id）。"""
    for node in (wf or {}).values():
        if isinstance(node, dict) and node.get("class_type") == "SeeThrough_GenerateLayers":
            return True
    return False


def _check_resolution(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("resolution 必须是整数，收到 %r" % (value,))
    if not (MIN_RESOLUTION <= value <= MAX_RESOLUTION):
        raise ValueError("resolution 必须在 %d..%d，收到 %d"
                         % (MIN_RESOLUTION, MAX_RESOLUTION, value))
    if value % RESOLUTION_STEP:
        raise ValueError("resolution=%d 不是 %d 的倍数（模型要求）"
                         % (value, RESOLUTION_STEP))
    return value


def _check_depth(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("resolution_depth 必须是整数，收到 %r" % (value,))
    if value == DEPTH_FOLLOW:
        return value
    if not (MIN_RESOLUTION <= value <= MAX_RESOLUTION):
        raise ValueError("resolution_depth 必须是 -1（跟随分辨率）或 %d..%d，收到 %d"
                         % (MIN_RESOLUTION, MAX_RESOLUTION, value))
    if value % RESOLUTION_STEP:
        raise ValueError("resolution_depth=%d 不是 %d 的倍数"
                         % (value, RESOLUTION_STEP))
    return value


def build(*, source_image, seed=None, resolution=None, steps=None,
          resolution_depth=None, base=None):
    """从基准图构建一份拆层图。

    只动四处：`1.image`（源图）、`4.seed`、`4.resolution` / `4.num_inference_steps`、
    `5.resolution_depth`。

    **`5.seed` 跟着 `4.seed` 一起写**：两个种子各自独立会让「同一个 seed 重跑」
    这件事对用户不成立 —— 他会以为没换种子就没变，而深度那一段其实换了。
    """
    if not isinstance(source_image, str) or not source_image.strip():
        raise ValueError("source_image 必须是非空字符串（ComfyUI input/ 里的文件名）")
    if seed is not None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed 必须是 >= 0 的整数，收到 %r" % (seed,))
    if steps is not None:
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise ValueError("steps 必须是整数，收到 %r" % (steps,))
        if not (MIN_STEPS <= steps <= MAX_STEPS):
            raise ValueError("steps 必须在 %d..%d，收到 %d"
                             % (MIN_STEPS, MAX_STEPS, steps))
    res = _check_resolution(resolution)
    depth = _check_depth(resolution_depth)

    wf = load_base(base)
    _require(wf, GEN_LAYERS, "SeeThrough_GenerateLayers")
    _require(wf, SAVE, SAVE_CLASS)

    wf[LOAD_IMAGE]["inputs"]["image"] = source_image.strip()
    if seed is not None:
        wf[GEN_LAYERS]["inputs"]["seed"] = seed
        wf[GEN_DEPTH]["inputs"]["seed"] = seed
    if res is not None:
        wf[GEN_LAYERS]["inputs"]["resolution"] = res
    if steps is not None:
        wf[GEN_LAYERS]["inputs"]["num_inference_steps"] = steps
    if depth is not None:
        wf[GEN_DEPTH]["inputs"]["resolution_depth"] = depth
    return wf


def _require(wf, node_id, class_type):
    node = wf.get(node_id) or {}
    if node.get("class_type") != class_type:
        raise ValueError("基准图里节点 %s 应当是 %s，实际是 %r"
                         % (node_id, class_type, node.get("class_type")))


def describe(wf):
    """从拆层图反解出参数 —— 任务记录与页面回显用。"""
    gen = (wf.get(GEN_LAYERS) or {}).get("inputs") or {}
    dep = (wf.get(GEN_DEPTH) or {}).get("inputs") or {}
    post = (wf.get(POST) or {}).get("inputs") or {}
    raw_depth = dep.get("resolution_depth")
    resolution = gen.get("resolution")
    return {
        "mode": MODE,
        "source_image": (wf.get(LOAD_IMAGE) or {}).get("inputs", {}).get("image"),
        "source_images": [(wf.get(LOAD_IMAGE) or {}).get("inputs", {}).get("image")]
        if (wf.get(LOAD_IMAGE) or {}).get("inputs", {}).get("image") else [],
        # 这条链路没有文本输入 —— 三个键恒为空，只为让字段形状与别的链路一致。
        "prompt": "",
        "negative_prompt": "",
        "seed": gen.get("seed"),
        "steps": gen.get("num_inference_steps"),
        "resolution": resolution,
        # 跟随时的**实际**取值如实报出来，别让页面自己再推一遍
        "resolution_depth": raw_depth,
        "resolution_depth_effective": (resolution if raw_depth == DEPTH_FOLLOW
                                       else raw_depth),
        "depth_follows": raw_depth == DEPTH_FOLLOW,
        "layerdiff_model": (wf.get(LOAD_LAYERDIFF) or {}).get("inputs", {}).get("model"),
        "depth_model": (wf.get(LOAD_DEPTH) or {}).get("inputs", {}).get("model"),
        "quant_mode": (wf.get(LOAD_LAYERDIFF) or {}).get("inputs", {}).get("quant_mode"),
        "auto_download": (wf.get(LOAD_LAYERDIFF) or {}).get("inputs", {}).get(
            "auto_download"),
        "tblr_split": post.get("tblr_split"),
        "use_lama": post.get("use_lama"),
        "save_prefix": (wf.get(SAVE) or {}).get("inputs", {}).get("filename_prefix"),
    }
