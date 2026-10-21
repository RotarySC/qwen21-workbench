#!/usr/bin/env python3
"""Qwen-Image-2.1 API 工作流构建器 —— 唯一真源。

为什么需要这个模块
------------------
在此之前有四个各写一份的脚本（`qwen21_run.py` / `build_refine.py` /
`build_2k.py` / `build_edit.py`），各自从某份 JSON 基准打补丁。两个问题：

1. **基准不统一。** `qwen21_run.py` 和 `build_edit.py` 用 `qwen21_api_base.json`，
   而 `build_refine.py` / `build_2k.py` 用 `qwen21_fashion_api.json`。两者其实只差
   一个 `459:474.on_false` 和提示词文本 —— 实测 `sort_keys` 逐字比较相等
   （见 `tests/test_build_golden.py`）。分散在两个文件里，改错一处没人会发现。

2. **断边修复只写在其中一个脚本里。** `qwen21_api_base.json` **本身带断边**
   （`459:474` 只有 `on_true`，没有 `on_false`），提示词根本进不了文本编码器，
   输出只由 seed 决定（实测两次完全不同的提示词，出图逐像素相同）。这里把
   修复做成 `build()` 里**无条件执行**的一步，谁调用都跑不掉。

四种出图模式（`build()`）
--------
    t2i     纯文生图（switch=false）
    refine  打开提示词改写器（switch=true，多加载 8.82 GiB，约 190 秒）
    2k      2048²（约 11 分钟，峰值显存约 18.9 GB）
    edit    图生图 / 局部编辑（拆掉改写器分支，改挂 LoadImage 与参考图 latent）

再加一种**放大模式**（`build_upscale()`，不走上面那条路）
--------
    upscale 用 VOSR2 把一张图放大 1~16 倍（实测 1024²→2048² 约 14 秒）

放大的特殊性值得写清楚：它**不是**上面四种的变体，而是**另一个模型的另一张图** ——
不读基准图、没有提示词、没有 KSampler、没有文本编码器。所以它有自己的构建函数，
理由与参数表见 `build_upscale()` 的注释。

注：`2k` 在结构上就是 `t2i` + 2048 尺寸，`describe()` 会如实回报为 `t2i`
并给出真实尺寸，另附 `is_2k` 供 UI 打标签。

用法
----
    import qwen21_build as qb
    wf   = qb.build("t2i", prompt="a red fox in snow", steps=20, seed=42)
    info = qb.describe(wf)      # 反解参数，画廊与表单回显用

    # 两种加速**默认就接 TE-Speed**（省约 2/3 采样时间、细节保住）；它们互斥
    wf2  = qb.build("edit", prompt="改成蓝色", source_image="ref.png",
                    lazy_cache=True, te_speed=False)  # LazyCache：省约一半，细节被抹平
    wf3  = qb.build("edit", prompt="改成蓝色", source_image="ref.png")  # 默认即 TE-Speed

命令行（可选）
--------------
    python qwen21_build.py t2i "a red fox" -o wf.json              # 默认接 TE-Speed
    python qwen21_build.py t2i "a red fox" --no-te-speed -o wf_plain.json
    python qwen21_build.py t2i "a red fox" --lazy-cache -o wf_cache.json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # …/qwen21-workbench
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # `project_paths` 与本模块同级

import project_paths  # noqa: E402

# 基准图（图的定义）在哪：**环境变量 → 仓库自带的 `workflows/` → 工作区**。
# 规则与理由只在 `project_paths.base_file()` 那一处，别在这儿重写一遍。
# 两个环境变量：
#   QWEN21_WORKSPACE  工作区根目录（默认 = 仓库的上一层；产物与提示词库在那儿）
#   QWEN21_BASE       基准图完整路径（优先级更高）
WORKSPACE = os.environ.get("QWEN21_WORKSPACE") or os.path.dirname(HERE)
BASE_FILE = project_paths.base_file("QWEN21_BASE", "qwen21_api_base.json",
                                    repo=HERE, workspace=WORKSPACE)

# 「模式」只表达**形状**：文生图 与 图生图。配置类的开关都有各自的布尔字段
# （`rewrite` / `lazy_cache` / `te_speed`），出图后自动放大是另一条链（见
# `workbench/watcher.py`），都不是模式。
#
# 2026-10-01 之前这里是 `("t2i", "refine", "2k", "edit")`，而那两个被撤掉的值
# **本来就不该在这里**：`refine` 只是「把 `SWITCH.switch` 打开」，`2k` 只是
# 「没给尺寸时补 2048」—— 同一张图的两种配置，却被塞进了同一个下拉框，于是
# 「模式」成了一个什么都能往里装的杂物箱。
# 放大（`UPSCALE_MODE`）**不在 `MODES` 里是对的**：它不是同一张图的配置，
# 是另一条链路（另一个构建函数、另一种模型、没有提示词），见 `build_upscale()`。
MODES = ("t2i", "edit")

# --- 节点 id（取自 qwen21_api_base.json，实测可用）-------------------------
LATENT = "459:456"        # EmptyLatentImage
TEXT_ENCODE = "459:452"   # TextEncodeQwenImage21
KSAMPLER = "459:458"      # KSampler
VAE_LOADER = "459:454"    # VAELoader
SWITCH = "459:474"        # ComfySwitchNode —— 断边的受害者
PE_PROMPT = "459:471"     # TextGenerate（提示词改写器）
PE_CLIP = "459:473"       # CLIPLoader（改写器专用，8.82 GiB）
PE_SYSTEM = "459:475"     # PrimitiveStringMultiline（改写器系统提示词）
PREVIEW = "459:472"       # PreviewAny
RESOLUTION = "13"         # ResolutionSelector（默认 1 MP 正方形 → 1024²）
UNET = "459:451"          # UNETLoader
CACHE = "459:480"         # QwenImage21Cache
VAE_DECODE = "459:457"    # VAEDecode
SAVE = "461"              # SaveImageAdvanced

LOAD_IMAGE = "900"        # 仅 edit 模式新增；多张参考图时依次是 900/901/902…
LAZY_CACHE = "990"        # 仅 lazy_cache=True 时新增（LazyCache）

# 一张图里最多挂几张参考图。这个数**不是我们定的**：`TextEncodeQwenImage21` 的
# `images` 是 autogrow，源码里写死的候选名就是 `image_1`..`image_16`
# （`comfy_extras/nodes_qwen.py` 的 `TemplateNames(..., names=[f"image_{i}" for i in
# range(1, 17)])`）。多给的会在 ComfyUI 校验阶段被 `value_not_in_list` 打回，
# 所以在**构建之前**就拦下，并把这个数的来处写进报错里。
MAX_REFERENCE_IMAGES = 16

# edit 模式要从基准里拆掉的节点：整条改写器分支 + 空 latent + ResolutionSelector
EDIT_DROP = (PE_PROMPT, PE_CLIP, SWITCH, PE_SYSTEM, PREVIEW, LATENT, RESOLUTION)

DEFAULT_EDIT_RESOLUTION = 1024
DEFAULT_2K = 2048
SIZE_MULTIPLE = 32

# --- 缓存加速（LazyCache）--------------------------------------------------
# 步复用：某些步直接拿上一步的模型输出顶上，省掉重算。接在 QwenImage21Cache
# **之后**、KSampler **之前** —— 缓存必须包住 KSampler 真正吃到的那份 MODEL。
#
# 2026-09-30 本机实测（RTX 2080 Ti 22GB / 1024² / seed 20260930 / cfg 1.0 / euler+simple）：
#
#   t2i 20 步    采样 45 344 → 24 051 / 21 793 / 19 539 ms
#                （阈值 0.05 / 0.10 / 0.20，即 −47% / −52% / −57%）
#   edit 16 步   采样 62 402 → 28 448 ms（−54.4%），像素只变 19.6%、mean_abs 1.74
#
# 代价如实写在明处：**高频细节被抹平**。t2i 那张真实照片变了 77%（mean_abs 4.7~6.7），
# 阈值越大越平滑（相邻像素梯度：基线 1.46 → 0.90 / 0.80 / 0.64）。所以默认**关闭**。
#
# 同批实验里 **EasyCache 不可用**：0.05 / 0.10 / 0.20 三档都把画面打烂
# （梯度 1.46 → 3.74 / 7.24 / 12.58，0.20 那张是纯噪声，肉眼确认过）。
# 结论只针对本链路，但**别顺手把这里换成 EasyCache**。
LAZY_CACHE_THRESHOLD = 0.2
LAZY_CACHE_START_PERCENT = 0.15
LAZY_CACHE_END_PERCENT = 0.95

# --- TE-Speed（第三方插件，装不装由用户决定）------------------------------
# 装在 `<ComfyUI>/custom_nodes/TE-Speed-QwenImage21`，节点类 `TESpeedQwenImage21`。
# 接法与 LazyCache **同位置**：`QwenImage21Cache → TE-Speed → KSampler`。
#
# 2026-09-30 逐格实测（RTX 2080 Ti 22GB / 1024² / 同一份底图只换旋钮；
# 采样耗时一律取探针的 `sampling.total_ms`）：
#
#   基线                  45 344~46 971 ms（20 步，2348.6 ms/步）
#   attention→int8        23 980 ms（20 步**全算**，1199.0 ms/步）→ **1.96×**
#                         编辑链路同测 64 641 → 32 392 ms（2.00×）
#   ＋步预测 thr=0.06     20 629 ms（跳 2/20）  ← 插件自己的默认档
#   ＋步预测 thr=0.20     14 300 ms（跳 7/20）  ← **本模块采用**
#   ＋speed  thr=0.20     12 033 ms（跳 10/20） ← 快，但画质垮掉，不采用
#
# **速度全部来自 attention**：`default` / `sdpa` 与原厂出图**逐像素相同**、
# 也一秒不省；换成 `kitchen_int8` 才拿到那 1.96×。README 说的「输出预测策略作为
# 核心实现」只值 11%（跳 2/20 那部分）。
#
# 画质代价（与基线比的 mean_abs）：纯 int8 1.077、纯跳 2 步 0.497、合计 1.203；
# thr 0.06→0.20 再多跳 5 步只 +0.147，而 speed 模式一步跳掉 +0.929。
# 相邻像素梯度：基线 1.956 / int8 1.966 / thr0.20 1.956 / speed 1.901 —— int8 是
# 「广而浅」的低幅重掷，**不抹细节**（对比 LazyCache 把梯度砍到 0.953）。
#
# 两个坑（改这段之前先读）：
#   1. **`0.0` 在这插件里是「自动」，不是「关」。** 实测 `reuse_threshold=0.0`
#      被改写成 0.018、`start/end_percent=0.0` 被改写成窗口 0.18-0.86；而
#      0.001/0.017/0.019 都**原样保留** —— 不是下限夹紧，是恰好 0.0 有特殊含义。
#      要真关掉步预测得传 0.001。下面的 start/end_percent 写 0.0 就是「用插件的
#      默认窗口」，别改。
#   2. **`predictor_error_limit` 实测完全无效**：0.02 / 0.08 / 0.32 三档出图
#      **逐像素相同**，跳步数也相同（七次运行 cooldown 与 deviation 一次没触发）。
#      留默认值只为写清楚「这个旋钮别去动它」。
TE_SPEED = "9900"                     # 仅 te_speed=True 时新增
TE_SPEED_ATTENTION = "kitchen_int8"   # 全部速度的来源
TE_SPEED_STEP_CACHE = "te_predictor"  # speed 只再多 5 个百分点，画质代价却跳 6 倍
TE_SPEED_REUSE_THRESHOLD = 0.20       # 实测甜点档；插件默认 0.06 在编辑链路上一步都不跳
TE_SPEED_ERROR_LIMIT = 0.08           # 实测无效，照抄插件默认

# --- Turbo（官方 8 步蒸馏检查点，**默认关**）---------------------------------
# 2026-10-11 实测（出图主机 ComfyUI 0.39.1 / RTX 2080 Ti 22 GB / 1024² /
# 一条提示词一个 seed；完整报告见 `docs/qwen-image-2.1-turbo.md`）。
#
# 它**不是新模型**：同架构同参数量（649 个张量名与形状逐一相同），
# 是**重新蒸馏**过的权重，把「走 25 小步」练成「走几大步」。
# 所以显存需求不变（峰值 17.04 GiB vs 基线 16.89 GiB），省的只是步数。
#
# **权重文件**：`Comfy-Org/Qwen-Image-2.1` 的
# `loras/qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors`（851 MiB）。
# 用 LoRA 而不是那个 6.76 GiB 的完整检查点，是为了**不复制一份基座**；
# 代价见下面第 3 条。
#
# 位置：`QwenImage21Cache → Turbo LoRA → TE-Speed / LazyCache → KSampler`。
# **必须在两个加速节点之前** —— 它是权重补丁，不抢那条线，把上游让给后面那个。
# 实测：LoRA 在前 exec 7.7 s、反过来 10.4 s，而**两种顺序出图逐位相同**
# ⇒ 顺序只影响耗时（差在采样循环之外的模型准备）。三次复跑稳定，不是噪声。
#
# 四条不能忘的（全部实测，改这段之前先读）：
#
#   1. **官方那个 8 步是曲线里最差的一端，不是甜点。** 步数与画面单调相关。
#      以「25 步 / 无任何加速」为基线（`grad_blur` 0.2272，暖调 R−B +3.17）：
#         8 步   7.68 s   −26.2% / +1.87    ← 最快，但背景明显发白发平
#        16 步  12.85 s    −6.0% / +2.97    ← **默认取这个**
#        20 步  15.13 s    +3.8% / +2.96    ← 结构超过基线，仍比生产快 1.3×
#        24 步  18.11 s   +12.6% / +2.89
#        32 步  23.60 s   +33.2% / +2.75
#      （生产配置 = TE-Speed + 25 步 + 无 turbo，实测 19.94 s，−4.7% / +1.48）
#      **别拿 `grad_raw` 单独判**：它随步数**下降**而 `grad_blur` **上升** ——
#      那是「粗结构变强、细颗粒变少」，两个同向上涨才该怀疑是加颗粒。
#
#   2. **调度器用 `linear_quadratic`。** ComfyUI **读不到** Qwen 那套
#      `sample_sigmas`（它在 `model_index.json` 里，单文件 repack 装不下），
#      而 ComfyUI 给 qwen_image21 的默认 shift 是 0.69、Turbo 想要的是 1.0。
#      实测 `linear_quadratic` 与官方表画面差别很小且略锐 —— 它**不读
#      `model_sampling`**，所以不受那个 shift 影响，是这一档里唯一不会被打偏的。
#      精确复现要走 `ManualSigmas` + `SamplerCustomAdvanced`，已证明与
#      `KSampler` **逐位相等**却没有画质回报，**别上**。
#
#   3. **挂了 LoRA 就没有 prefix KV cache**（`comfy/model_base.py:2689`：
#      `reset_prefix_cache(patcher is not None and len(patcher.hook_patches) == 0)`）。
#      想要 KV 缓存就换成那个 6.76 GiB 的完整检查点，改 `unet_name` 一个控件。
#      另外 LoRA 每次运行比裸基座多花约 3.2 s（200 个 patch 打进 int8 的代价）。
#
#   4. **它与 TE-Speed 可以叠加，而且叠得很好**（7.7 s 对生产 19.94 s）。
#      但与 `LazyCache` 的组合**没测过** —— 结构上不冲突（它不抢那条线），
#      所以这里不禁止，只是不知道画质会怎样。
TURBO_LORA = "9800"                   # 仅 turbo=True 时新增
TURBO_LORA_FILE = "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"
TURBO_STRENGTH = 1.0
TURBO_DEFAULT_STEPS = 16              # 见上面第 1 条 —— 8 是曲线最差的一端
TURBO_DEFAULT_SCHEDULER = "linear_quadratic"   # 见上面第 2 条

# --- 放大（TE-Speed-VOSR2）—— **另一个模型、另一张图**-----------------------
# 与上面四种模式**没有共同点**：不读基准图、没有提示词、没有 KSampler、
# 没有文本编码器。背后是 VOSR2（CVPR 2026，HK PolyU + OPPO，Apache-2.0）：
# 1.4B **one-step** DiT + DINOv2-L + Qwen VAE 的**生成式**超分 ——
# 输出是「重画一遍」，不是插值放大。这一条解释了它全部的速度与画质表现。
#
# 图是从**实测跑通的那一份**逐字搬的（连节点 id 都照搬，见
# `generated/vosr2_x2_api.json`）—— 照文档重搭第二份就等于引入第二个变量。
#
# 2026-09-30 实测（RTX 2080 Ti 22GB / hf-mirror 下的 VOSR2 bundle）：
#
#   输入        输出     tiles   墙钟       插件自报
#   512²  ×4    2048²      25    15.1 s     13.65 / 14.71 s
#   1024² ×2    2048²      25    14.2 s     13.24 / 13.26 / 13.28 s
#   1024² ×4    4096²      81    41.4 s     38.90 s
#   2048² ×2    4096²      81    41.2 s     37.29 s
#
# **代价只随输出尺寸变，与输入尺寸几乎无关。** 上面那个 2×2 矩阵里，
# 512²→2048² 与 1024²→2048² 的逐阶段耗时看不出差别（encode 6.0~6.8 /
# dit 3.4~3.8 / decode 3.0~3.3），2048²→4096² 也与 1024²→4096² 一致。
# 所以成本模型**只需要输出的百万像素数**（见 `workbench/cost.py`）。
#
# 三个阶段里最大的是 **VAE 往返**：2048² 输出时 encode 6.0 + decode 3.0 ≈ 9.0 s，
# 而 dit 只有 3.4 s —— 要再快该盯 VAE，盯 DiT 是盯错了地方。
# 载模型另计 7.0 s（`TESpeedVOSR2Loader` 实测 6963 ms，只在冷启动付一次）。
#
# 画质（同一批实测）：细密纹理上比 bicubic 清晰得多且**没进噪声区**
# （梯度 9.51 → 10.47，而 EasyCache 在同一刻度上是 3.74~12.58 的纯噪声）；
# 光滑主体上**不添噪**（梯度 0.347 反而低于 bicubic 的 0.372，平墙 std
# 12.4183 vs 12.4039）。代价是它**锐化一切** —— 源图里很淡的瑕疵会变明显。
UPSCALE_MODE = "upscale"
ALL_MODES = MODES + (UPSCALE_MODE,)

UP_LOAD = "1"          # LoadImage
UP_LOADER = "2"        # TESpeedVOSR2Loader
UP_SETTINGS = "3"      # TESpeedVOSR2Settings
UP_IMAGE = "4"         # TESpeedVOSR2Image
UP_SAVE = "5"          # SaveImage

UPSCALE_DEFAULT_SCALE = 2
UPSCALE_DEFAULT_SEED = 42
UPSCALE_MIN_SCALE = 1
UPSCALE_MAX_SCALE = 16                 # 插件自己的范围，照抄
UPSCALE_PREFIX = "vosr2"
# `TESpeedVOSR2Loader.model_bundle` 的候选值。**它是权重在不在的判据** ——
# 这个 COMBO 的选项来自插件对 `models/vosr2/` 的扫描，缺权重时表就是空的。
UPSCALE_BUNDLE = "VOSR2"


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def unknown_mode_message(mode):
    """未知模式时说清**怎么改**，而不是只回一句「不认识」。

    `2k` 与 `refine` 是 2026-10-01 撤掉的两个模式 —— 它们是同一张图的**配置**，
    不是形状：`2k` 就是「给 width=height=2048」，`refine` 就是 `rewrite=True`。
    老脚本撞上来时必须知道改成什么，否则只会看到一句「未知模式」然后去猜。

    **刻意不做静默映射**（把 `mode="2k"` 悄悄当成 t2i+2048）：那会把「我用的还是
    老写法」这件事藏起来，而它迟早会在别处咬人。
    """
    text = "未知模式 %r，可选：%s" % (mode, "、".join(MODES))
    hints = {
        "2k": "`2k` 已并入尺寸：给 width=height=2048（分辨率在「基础参数 → 尺寸」里调）",
        "refine": "`refine` 改成了额外开关：给 rewrite=True",
    }
    hint = hints.get(mode)
    return "%s；%s" % (text, hint) if hint else text


def _check_size(name, value):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}")
    if value <= 0:
        raise ValueError(f"{name} 必须为正数，收到 {value}")
    if value % SIZE_MULTIPLE:
        raise ValueError(f"{name}={value} 不是 {SIZE_MULTIPLE} 的倍数（模型要求）")


def _check_resolution(value):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"resolution 必须是 >= 0 的整数，收到 {value!r}")
    if value and value % SIZE_MULTIPLE:
        raise ValueError(f"resolution={value} 不是 {SIZE_MULTIPLE} 的倍数"
                         f"（0 表示保持参考图原尺寸）")


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
def load_base(path=None):
    """读基准 API 图。每次返回全新对象，调用方不必担心改到别人。"""
    path = path or BASE_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺少基准 API 图: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build(mode, *, prompt, steps=None, width=None, height=None, seed=None,
          cfg=None, sampler=None, scheduler=None, rewrite=False,
          source_image=None, source_images=None, resolution=None, base=None,
          lazy_cache=False, te_speed=True, turbo=False, negative_prompt=None):
    """从基准图构建一份可直接提交给 ComfyUI 的 API 工作流。

    参数为 None 表示"不改动基准里的原值"，而不是"设成 None"。

    `source_image`（单张）与 `source_images`（多张，edit 模式用）是**同一件事**的
    两种写法：`TextEncodeQwenImage21` 的 `images` 是 autogrow，每张参考图挂一个
    `LoadImage`、连到 `images.image_N`，最多 `MAX_REFERENCE_IMAGES` 张。
    两个都给时按列表走，但**必须一致**（`source_images[0] == source_image`），
    否则当场抛 —— 同一件事有两份真源，迟早会分家。

    `lazy_cache=True` 会在 `QwenImage21Cache` 与 `KSampler` 之间插一个 LazyCache
    （步复用，实测采样段省约一半）。**默认 False** —— 它拿细节换速度。

    `rewrite=True` 打开**提示词改写器**（`ComfySwitchNode.switch = True`，会去读
    那个 8.82 GiB 的 `pe_t2i`，实测 +113 秒）。**2026-10-01 起它取代了
    `mode="refine"`** —— 改写是**配置**，不是形状；把它做成模式会让「模式」这个
    下拉框继续当杂物箱，而且 `2k` 也早就证明了这一点（它只是「没给尺寸时补
    2048」，同样被塞进来过）。
    **只对 `mode="t2i"` 有意义**：`_wire_edit` 会把 `SWITCH` 与 `PE_PROMPT`
    （改写器）整段从图里删掉，传 True 也接不上 —— 所以这里**当场抛**，不静默
    忽略（静默忽略的症状是「勾了没反应」，最难查的那种）。

    `te_speed=True` 会在**同一个位置**插一个 TESpeedQwenImage21（实测采样段省约
    2/3，细节保住）。**默认 True**（2026-10-06 起）—— 它最快也最不伤画质，
    「每次出图都手动勾一下」是白费的手脚；要关请明确写 `te_speed=False`。
    两者**只能二选一**：都要就当场抛 ValueError，不会悄悄接错。

    `turbo=True` 挂上官方 Turbo LoRA，**并且**把 `steps` 与 `scheduler` 的默认
    换成实测那一组（16 步 / `linear_quadratic`）—— 显式给了 `steps` /
    `scheduler` 就以显式的为准，这里只补「没表态」的那两格。理由与实测数字
    全写在上面 `TURBO_*` 那一段里，**改之前先读它**。

    **默认 False**，原因是证据只有一条提示词一个 seed（见 `docs/qwen-image-2.1-turbo.md`），
    还不够格当默认值。它与 `te_speed` / `lazy_cache` **都不冲突**（它是权重补丁，
    插在两个加速节点之前，不抢那条线）；但与 `lazy_cache` 的组合**没测过**，
    所以不推荐，也不会替用户拦下。

    `negative_prompt` 写进 `TextEncodeQwenImage21.negative_prompt`（那个节点的
    输出 slot 1 就是 KSampler 的 negative，接法是现成的）。**默认 None = 保持基准
    里的空串**，所以现有 golden 图一个字节都不变。

    **但它在默认的 CFG=1.0 下完全不生效** —— 采样器会把负向分支整个丢掉
    （`comfy/samplers.py:610`），实测两段完全不同的反向词出图**逐字节相同**。
    详见 `workbench/config.py` 顶部那一节。所以这个模块只管**如实写进去**，
    「不生效」这件事由 `workbench/server.py` 在页面上直说（它是策略，不是构建）。
    """
    if mode not in MODES:
        raise ValueError(unknown_mode_message(mode))
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 必须是非空字符串")

    refs = _reference_list(source_image, source_images)

    _check_size("width", width)
    _check_size("height", height)
    _check_resolution(resolution)
    if steps is not None and not (1 <= steps <= 200):
        raise ValueError(f"steps 必须在 1..200，收到 {steps}")
    if mode == "edit" and not refs:
        raise ValueError("edit 模式必须给 source_image（或 source_images）")
    if not isinstance(lazy_cache, bool):
        raise ValueError(f"lazy_cache 必须是布尔值，收到 {lazy_cache!r}")
    if not isinstance(te_speed, bool):
        raise ValueError(f"te_speed 必须是布尔值，收到 {te_speed!r}")
    if not isinstance(turbo, bool):
        raise ValueError(f"turbo 必须是布尔值，收到 {turbo!r}")
    if not isinstance(rewrite, bool):
        raise ValueError(f"rewrite 必须是布尔值，收到 {rewrite!r}")
    if rewrite and mode != "t2i":
        # 图生图那条路把改写器整段删掉了（`EDIT_DROP` 含 `SWITCH` 与 `PE_PROMPT`），
        # 顺手打开的那个开关接不到任何东西。**当场说**，别让用户对着一个
        # 「勾了没反应」的复选框猜。
        raise ValueError(
            "改写器只用得上文生图：图生图那条链路会把 ComfySwitchNode 与改写器"
            "（pe_t2i）从图里删掉，本机也没有图生图版改写器（pe_i2i）。"
            "请取消「提示词改写器」，或改用文生图。")
    if negative_prompt is not None and not isinstance(negative_prompt, str):
        # 与 `_int_or_none` 那类"看着能转就转"不同：`True`/`0` 之类被静默写成
        # 字符串会在 PNG 里留下一条假的参数记录，而那正是本模块要消灭的东西。
        raise ValueError(f"negative_prompt 必须是字符串，收到 {negative_prompt!r}")
    if lazy_cache and te_speed:
        # 两者都插在 `QwenImage21Cache → KSampler` 这一条 model 连线上，同时开
        # 就是互相抢线。**不搞"自动挑一个"** —— 那会让用户以为两个都生效了。
        # 报错里带上实测数字，省得再去翻文档。
        raise ValueError(
            "LazyCache 与 TE-Speed 都插在 QwenImage21Cache 与 KSampler 之间，"
            "只能二选一。实测 TE-Speed 更快也更不伤画质："
            "t2i 采样 −69.6% 对 −56.9%，相邻像素梯度 1.956 对 0.953"
            "（LazyCache 把细节砍了一半），所以推荐 te_speed=True。")

    wf = load_base(base)

    # --- 1. 断边修复（无条件执行）------------------------------------
    # comfy-cli 把 UI 格式转 API 格式时，会丢掉「子图输入节点 → 纯 link 型
    # 输入」的边。本模板丢的正是 ComfySwitchNode.on_false，而它承载用户提示词。
    # 少了这一步：switch=false 分支悬空 → 提示词从未进文本编码器 →
    # 输出只由 seed 决定。
    # 提示词必须同时写两处：on_false 给 switch=false 走，471.prompt 给改写器读。
    wf[SWITCH]["inputs"]["on_false"] = prompt
    if PE_PROMPT in wf:
        wf[PE_PROMPT]["inputs"]["prompt"] = prompt

    # --- 1b. 反向词 ----------------------------------------------------
    # 写在**两种路径共用**的位置。edit 的 `_wire_edit` 原先把它写死成 ""，
    # 那会让图生图模式静默丢掉反向词 —— 一个没有症状的错，所以那里不再碰它。
    # 默认 None 就一个字都不动，基准里的空串原样留着（golden 图因此不变）。
    if negative_prompt is not None:
        wf[TEXT_ENCODE]["inputs"]["negative_prompt"] = negative_prompt

    # --- 2. 改写器开关（配置，不是模式；见 `build()` 的 docstring）---------
    wf[SWITCH]["inputs"]["switch"] = bool(rewrite)

    # --- 3. 模式专属接线 -----------------------------------------------
    if mode == "edit":
        _wire_edit(wf, prompt, refs, resolution)
    else:
        lat = wf[LATENT]["inputs"]
        # 基准里 width/height 默认是指向节点 13（ResolutionSelector）的链接，
        # 只有显式给了值才写成字面量。
        if width is not None:
            lat["width"] = width
        if height is not None:
            lat["height"] = height

    # --- 4. 采样参数（只覆盖给了值的）----------------------------------
    # Turbo 把「没表态」的那两格补成实测那一组（16 步 / linear_quadratic）——
    # 只挂 LoRA 而不动步数不是我们要的东西，那等于拿 25 步的配方喂一个按
    # 大步蒸馏过的模型。显式给了就以显式的为准。理由见上面 TURBO_* 那段。
    if turbo:
        if steps is None:
            steps = TURBO_DEFAULT_STEPS
        if scheduler is None:
            scheduler = TURBO_DEFAULT_SCHEDULER
    ks = wf[KSAMPLER]["inputs"]
    for key, value in (("steps", steps), ("seed", seed), ("cfg", cfg),
                       ("sampler_name", sampler), ("scheduler", scheduler)):
        if value is not None:
            ks[key] = value

    # --- 5. 加速 ----------------------------------------------------------
    # 顺序是**实测决定的**：Turbo LoRA 是权重补丁，插在两个加速节点**之前**。
    # 反过来每张图多花约 3 秒（LoRA 在前 7.7 s 对 10.4 s），而两种顺序出图
    # **逐位相同** —— 差的只是采样循环之外的模型准备。LazyCache 与 TE-Speed
    # 仍然互斥（见上），但 Turbo 与**两者都不冲突**：它不抢那条线。
    if turbo:
        _wire_turbo(wf)
    if lazy_cache:
        _wire_lazy_cache(wf)
    elif te_speed:
        _wire_te_speed(wf)

    return wf


# 两个加速节点都要求「接在 KSampler 当前吃的那份 MODEL 后面」。**合法的上游只有
# 两个**：`QwenImage21Cache`（没挂 turbo），或 Turbo LoRA（挂了，且它被刻意插在
# 两者之前）。别把这个检查放宽成「随便什么上游都行」—— 它拦的是「基准图换了接法
# 却没人发现」，那种错会让加速**静默失效**（能出图、一秒没省）。
_ACCELERATOR_UPSTREAM = (CACHE, TURBO_LORA)


def _accelerator_upstream(wf, who, hint=""):
    """取 KSampler 当前的上游，并确认它是我们自己插出来的那一个。

    返回的就是要写进新节点 `model` 的那条边。接不上当场抛 —— 见上面那句注释。
    """
    tail = (wf.get(KSAMPLER) or {}).get("inputs", {}).get("model")
    ok = (isinstance(tail, list) and len(tail) == 2 and tail[1] == 0
          and tail[0] in _ACCELERATOR_UPSTREAM and tail[0] in wf)
    if not ok:
        raise ValueError(
            f"基准图里 {KSAMPLER} 的 model 指向 {tail!r}，既不是 {CACHE!r} "
            f"也不是 Turbo LoRA（{TURBO_LORA!r}）；没法把 {who} 接在它后面。{hint}")
    return tail


def _wire_turbo(wf):
    """在 QwenImage21Cache 与它当前的消费者之间挂上 Turbo LoRA（就地把 wf 改掉）。

    **必须跑在两个加速节点之前**（`build()` 里就是这么排的）—— 实测顺序只影响
    耗时不影响画面，而 LoRA 在前快约 3 秒。理由与全部实测数字见上面 `TURBO_*`。
    """
    if TURBO_LORA in wf:                      # 幂等：重复调用不会插两个
        return
    tail = (wf.get(KSAMPLER) or {}).get("inputs", {}).get("model")
    if tail != [CACHE, 0]:
        raise ValueError(
            f"基准图里 {KSAMPLER} 的 model 指向 {tail!r}，不是 {CACHE!r}；"
            f"没法把 Turbo LoRA 接在 QwenImage21Cache 后面"
            f"（它必须**先于** TE-Speed / LazyCache 接）")
    wf[TURBO_LORA] = {
        "class_type": "LoraLoaderModelOnly",
        "_meta": {"title": "Qwen-Image 2.1 Turbo LoRA"},
        "inputs": {
            "model": [CACHE, 0],
            "lora_name": TURBO_LORA_FILE,
            "strength_model": TURBO_STRENGTH,
        },
    }
    wf[KSAMPLER]["inputs"]["model"] = [TURBO_LORA, 0]


def _wire_lazy_cache(wf):
    """在 QwenImage21Cache 与 KSampler 之间插一个 LazyCache（就地把 wf 改掉）。

    顺序不是随意的：LazyCache 复用**采样期**的模型输出，必须包住 KSampler 真正
    吃到的那份 MODEL；而 `459:480` 是给 Qwen 设 KV 缓存设备/量化用的，要在它之后。
    实测接法就是 `UNETLoader → QwenImage21Cache → LazyCache → KSampler`。
    """
    if LAZY_CACHE in wf:                      # 幂等：重复调用不会插两个
        return
    # 上游可能是 QwenImage21Cache，也可能是 Turbo LoRA（它被刻意插在更前面）。
    # 接不上却照跑，会变成「能跑、但缓存没生效」的静默失效 —— 那种错没有症状，
    # 正是这个模块最想消灭的东西。宁可当场炸。
    upstream = list(_accelerator_upstream(
        wf, "LazyCache", hint="（Turbo LoRA 若开着，它必须接在 LoRA 之后）"))
    wf[LAZY_CACHE] = {
        "class_type": "LazyCache",
        "_meta": {"title": "Lazy Cache"},
        "inputs": {
            "model": upstream,
            "reuse_threshold": LAZY_CACHE_THRESHOLD,
            "start_percent": LAZY_CACHE_START_PERCENT,
            "end_percent": LAZY_CACHE_END_PERCENT,
            "verbose": False,
        },
    }
    wf[KSAMPLER]["inputs"]["model"] = [LAZY_CACHE, 0]


def _wire_te_speed(wf):
    """在 QwenImage21Cache 与 KSampler 之间插一个 TESpeedQwenImage21（就地把 wf 改掉）。

    位置与 `_wire_lazy_cache` 一样、原因也一样：要包住 KSampler 真正吃到的那份
    MODEL。因为位置相同，两者**不能同时接**（见 `build()` 里的互斥检查）。

    参数全部来自实测常量，理由写在上面 TE_SPEED_* 那一段里 —— 改这里之前先读它。
    """
    if TE_SPEED in wf:                        # 幂等：重复调用不会插两个
        return
    # 上游可能是 QwenImage21Cache，也可能是 Turbo LoRA（它被刻意插在更前面）。
    # 接不上却照跑，会变成「能出图、但一秒没省」—— 那种错没有症状。宁可当场炸。
    upstream = list(_accelerator_upstream(
        wf, "TE-Speed",
        hint=f"（若那个节点是 LazyCache，两者只能二选一；"
             f"若 Turbo LoRA 开着，它必须接在 LoRA 之后）"))
    wf[TE_SPEED] = {
        "class_type": "TESpeedQwenImage21",
        "_meta": {"title": "TE-Speed Qwen Image 2.1"},
        "inputs": {
            "model": upstream,
            "attention": TE_SPEED_ATTENTION,
            "step_cache": TE_SPEED_STEP_CACHE,
            "reuse_threshold": TE_SPEED_REUSE_THRESHOLD,
            # 0.0 在这个插件里 = 「用它的默认窗口」（实测生效为 0.18-0.86）。
            # **别改成 0.18/0.86**：那对它是「显式数值」，与「默认窗口」是不是
            # 同一条路径**没验过**，而 0.0 正是实测那批用的值。
            "start_percent": 0.0,
            "end_percent": 0.0,
            "predictor_error_limit": TE_SPEED_ERROR_LIMIT,
            "verbose": False,   # True 会把每个采样步刷进后端日志（实测刷满过）
        },
    }
    wf[KSAMPLER]["inputs"]["model"] = [TE_SPEED, 0]


def _reference_list(source_image, source_images):
    """把「单张」与「多张」两种写法收成**一个有序列表**。

    顺序是有意义的：`TextEncodeQwenImage21` 是**按 `image_N` 的编号**遍历参考图的，
    而第一个通过的会决定输出尺寸（它给出的 `latent` 输出就是按第一张的尺寸建的，
    见 `nodes_qwen.py`）。所以这里保持调用方给的顺序，**不去重、不排序** ——
    悄悄替用户改顺序，会让「第一张决定尺寸」这条变得没法推理。
    """
    if source_images is not None:
        if isinstance(source_images, (str, bytes)):
            raise ValueError(f"source_images 必须是列表，收到 {source_images!r}"
                             "（单张请用 source_image=）")
        refs = list(source_images)
        bad = [r for r in refs if not isinstance(r, str) or not r.strip()]
        if bad:
            raise ValueError(f"source_images 里每一项都必须是非空字符串，"
                             f"收到 {bad!r}")
        refs = [r.strip() for r in refs]
        if source_image is not None:
            if not isinstance(source_image, str):
                raise ValueError(f"source_image 必须是字符串，收到 {source_image!r}")
            one = source_image.strip()
            if refs and one and refs[0] != one:
                # 两份真源里第一张不一样 —— 谁也不知道该信哪个，当场炸
                raise ValueError(
                    f"source_image={one!r} 与 source_images[0]={refs[0]!r} 不一致；"
                    "同一件事只给一处")
            if one and not refs:
                refs = [one]
    elif source_image is None:
        refs = []
    else:
        if not isinstance(source_image, str):
            raise ValueError(f"source_image 必须是字符串，收到 {source_image!r}")
        refs = [source_image.strip()] if source_image.strip() else []

    if len(refs) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"参考图最多 {MAX_REFERENCE_IMAGES} 张 —— `TextEncodeQwenImage21` 的 "
            f"images 是 autogrow，候选名就写到 image_{MAX_REFERENCE_IMAGES}"
            f"（见 comfy_extras/nodes_qwen.py），多给的会在 ComfyUI 校验阶段被 "
            f"value_not_in_list 打回。收到 {len(refs)} 张。")
    return refs


def _pick_id(index):
    """第 `index`（0 起）张参考图的 LoadImage 节点 id。

    **从 `LOAD_IMAGE` 连续往上**，不另起一段：`pngmeta` 与 `describe()` 都按
    「图里有没有 900 这个 LoadImage」来判「这是不是图生图」，把它们排成
    900/901/902… 就只靠一个判据，不必维护第二张表。
    """
    return str(int(LOAD_IMAGE) + index)


def _wire_edit(wf, prompt, source_images, resolution):
    """把文生图的图改接成图生图/编辑的图 —— **支持多张参考图**。

    为什么不用官方模板 image_qwen_image_2_1_image_edit：它要图生图版改写器
    `qwen3.5_9b_qwen_image_2.1_pe_i2i`（本机只有 t2i 版），而 ComfyUI 对**每个
    节点**做 COMBO 校验，缺文件是硬错误 value_not_in_list —— 哪怕该节点在
    switch=false 分支上根本不执行。它同样带断边结构。所以这里直接从实测可用的
    基准图手工搭。

    多张的接法（每张一个 LoadImage，连到 `images.image_N`，N 从 1 数）是照
    `TextEncodeQwenImage21` 的 schema 来的：那个 `images` 是 `Autogrow`，
    模板名就是 `image_1`..`image_16`。**注意 `resolution` 是逐张生效的** ——
    源码里每张图各自按 `resolution²` 的面积等比缩放（`nodes_qwen.py`），
    而不是总共分这么多像素。
    """
    for nid in EDIT_DROP:
        wf.pop(nid, None)

    te = wf[TEXT_ENCODE]["inputs"]
    # 先把基准里可能残留的 autogrow 键清掉：留着旧的那几个，图里就会出现
    # 指向已删节点的连线（或者指到我们刚建的另一个 id 上），而那是**静默**的
    # —— 它能跑，只是参考图不是你以为的那几张。
    for key in [k for k in te if k.startswith("images.")]:
        te.pop(key, None)

    for i, name in enumerate(source_images):
        nid = _pick_id(i)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        te["images.image_%d" % (i + 1)] = [nid, 0]

    te["prompt"] = prompt
    # 反向词**不在这里写**：它在 `build()` 里按两种路径共用的那一步统一处理。
    # 这里原本写死 `te["negative_prompt"] = ""`，那会让图生图静默丢掉反向词。
    te["resolution"] = (DEFAULT_EDIT_RESOLUTION if resolution is None
                        else resolution)
    te["vae"] = [VAE_LOADER, 0]

    # 编辑模式必须拿 TextEncode 的 latent 输出（按**第一张**参考图的尺寸生成），
    # 换成 EmptyLatentImage 会因尺寸/通道不符而失败。
    wf[KSAMPLER]["inputs"]["latent_image"] = [TEXT_ENCODE, 2]


def build_upscale(source_image, *, scale=None, seed=None, prefix=None):
    """构建一份 VOSR2 放大图 —— 与 `build()` 那四种模式**无关的另一张图**。

    `source_image` 是 ComfyUI `input/` 里的文件名（插件只读得到它认得的那份）；
    `scale` 是放大倍数，输出尺寸 = 源图尺寸 × scale。

    为什么单独一个函数、而不是往 `build()` 里加个 `mode="upscale"`：
    那个函数的前提是「从基准 API 图打补丁，且必须有提示词」，而这里
    **既没有基准图也没有提示词**（唯一的输入是一张图）。硬塞进去会让
    `build()` 长出一个 `prompt=None` 的特例，而特例正是这个模块当初要消灭的
    东西；两边的参数表也几乎不重叠（左边是 steps/cfg/sampler，这边是 scale）。
    """
    if not isinstance(source_image, str) or not source_image.strip():
        raise ValueError("放大模式必须指定源图（ComfyUI input/ 里的文件名）")
    scale = UPSCALE_DEFAULT_SCALE if scale is None else scale
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise ValueError(f"scale 必须是整数，收到 {scale!r}")
    if not (UPSCALE_MIN_SCALE <= scale <= UPSCALE_MAX_SCALE):
        raise ValueError(f"scale 必须在 {UPSCALE_MIN_SCALE}..{UPSCALE_MAX_SCALE}，"
                         f"收到 {scale}")
    seed = UPSCALE_DEFAULT_SEED if seed is None else seed
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError(f"seed 必须是 >= 0 的整数，收到 {seed!r}")

    # `settings` 用实测那一套的默认档，但**显式写全**：将来插件改默认值，
    # 这份图仍然表达同一个意思（不写就是「跟着插件走」，那就是没钉住）。
    # 三个值都不是随手填的：tile_size=512 / vae_tile_size=1024 是插件 README
    # 在「显存不足」时要求**保持**的那两个；color_alignment=wavelet 是默认档，
    # 实测那批图就是它出的。
    return {
        UP_LOAD: {
            "class_type": "LoadImage",
            "inputs": {"image": source_image, "upload": "image"},
        },
        UP_LOADER: {
            "class_type": "TESpeedVOSR2Loader",
            "_meta": {"title": "TE-Speed VOSR2 Loader"},
            "inputs": {
                "model_bundle": UPSCALE_BUNDLE,
                "precision": "auto",
                "memory_policy": "auto",
                "torch_compile": False,   # 要 triton，且首次调用要编译；没实测过就不开
                "vae_encode_amp": False,
            },
        },
        UP_SETTINGS: {
            "class_type": "TESpeedVOSR2Settings",
            "_meta": {"title": "TE-Speed VOSR2 Settings"},
            "inputs": {
                "quality_profile": "speed",
                "tile_strategy": "auto",
                "tile_size": 512,
                "tile_overlap": 32,
                "vae_tile_size": 1024,
                "vae_tile_overlap": 32,
                "image_batch": 1,
                "frame_batch": 2,
                "dino_batch": 2,
                "temporal_cache": False,
                "cache_threshold": 0.003,
                "cache_refresh": 4,
                "memory_policy": "auto",
                "color_alignment": "wavelet",
            },
        },
        UP_IMAGE: {
            "class_type": "TESpeedVOSR2Image",
            "_meta": {"title": "TE-Speed VOSR2 Image"},
            "inputs": {
                "model": [UP_LOADER, 0],
                "settings": [UP_SETTINGS, 0],
                "images": [UP_LOAD, 0],
                "scale": scale,
                "seed": seed,
            },
        },
        UP_SAVE: {
            "class_type": "SaveImage",
            "inputs": {
                "images": [UP_IMAGE, 0],
                "filename_prefix": prefix or UPSCALE_PREFIX,
            },
        },
    }


# ---------------------------------------------------------------------------
# 收尾：每次提交都得写得出一个新文件
# ---------------------------------------------------------------------------
# 收尾节点**按节点类认，不按 id**（id 是我们自己铸的，见 `is_upscale()` 同一条规矩）。
#
# 这份表自 2026-10-12 起是**跨链路共用**的：Qwen 那两条用 `SaveImage*`，
# MiniMax H3 用 `SaveVideo`，See-through 用 `SeeThrough_SavePSD`。四者的
# `filename_prefix` 语义一样（都是前缀字符串，那个函数只做 `old + "_" + token`），
# 所以一个函数就够，不必每条链路各写一份。
#
# **漏一个的后果与 2026-10-02 用户报的那个一模一样**：那条链路一字不改地再提一遍时
# 一个节点都不跑，磁盘上什么都不多，而 `/history` 回放着**上一次**的产物名 ⇒
# 页面写着「完成」。宁可多列，也不许按链路分家。
SAVE_CLASSES = ("SaveImageAdvanced", "SaveImage", "SaveVideo",
                "SeeThrough_SavePSD")


def unique_save_prefix(wf, token):
    """把收尾节点的 `filename_prefix` 改成 `…_<token>`，返回改完的前缀。

    **为什么非改不可**（2026-10-02 用户报的「原参数重复生图：秒完成 + 没有图」）：
    ComfyUI 的缓存认的是**节点的输入**，不是「这是一次新的提交」。一字不改地再提
    一遍，它**一个节点都不会执行** —— `execution.py` 里命中缓存的节点直接
    `emit_cached_output()` 把上一次的产物名回放给历史，然后返回。于是：

      * 磁盘上什么都没多（收尾节点没跑，就不会写文件）；
      * `history` 里却有产物名，还是**上一次**那个 → 界面上「完成 + 有产物名」；
      * 上一次那张要是已经不在了（P20f 的删除是进回收站），就**连那张也回不来**。

    改前缀正是唯一需要改的那个输入：收尾节点自己重跑一遍（把缓存里那份**重新写
    一遍**，像素与上一次逐位相同），上游一个节点都不用重算 —— 实测 64×64 的图
    84 ms（全命中）→ 122 ms（只跑收尾）；Qwen 那张 1.2 MB 的实测见
    `docs/details.md`。

    `token` 由调用方给（提交路径用 job_id 前 8 位），本函数不认识「作业」这个概念。

    ⚠ **`token` 不只影响缓存，它还是「作业 ↔ 产物」之间唯一的键**：H3 的视频与
    See-through 那几十个层文件都不在 `/history` 里（`SeeThrough_SavePSD` 只返回一个
    字符串，`ui` 是空的），所以 `workbench/artifacts.py` 只能靠**文件名里这段 token**
    去找回它们。两处的取值必须同源（`artifacts.token_of()`），改这里的拼法就等于让
    那两条链路的产物**全部找不到**。
    """
    for node in wf.values():
        if not isinstance(node, dict) or node.get("class_type") not in SAVE_CLASSES:
            continue
        inputs = node.setdefault("inputs", {})
        old = str(inputs.get("filename_prefix") or "").rstrip("_")
        inputs["filename_prefix"] = ("%s_%s" % (old, token)) if old else str(token)
        return inputs["filename_prefix"]
    raise ValueError("这份图里没有收尾节点（%s）：写不出文件，也就谈不上"
                     "「出图必有图」" % " / ".join(SAVE_CLASSES))


# ---------------------------------------------------------------------------
# 反解
# ---------------------------------------------------------------------------
def is_upscale(wf):
    """这张图是不是 VOSR2 放大图（按**节点类**认，不按 id —— id 是我们铸的）。"""
    for node in wf.values():
        if isinstance(node, dict) and node.get("class_type") == "TESpeedVOSR2Image":
            return True
    return False


def describe_upscale(wf):
    """放大型的反解。

    `width`/`height` **如实报 None**：输出尺寸 = 源图尺寸 × scale，而源图尺寸
    不在图里（图只记文件名）。要算得去读那张源图的 IHDR —— 那是调用方的事
    （`workbench/server.py` 会去读，好在页面上显示真实输出尺寸），
    这里不猜、也不拿 1024 当默认值冒充。
    """
    img = (wf.get(UP_IMAGE) or {}).get("inputs") or {}
    src = (wf.get(UP_LOAD) or {}).get("inputs") or {}
    if not isinstance(img, dict):
        img = {}
    return {
        "mode": UPSCALE_MODE,
        # 与 `describe()` 保持**同一个键集**：放大没有提示词，也就没有改写器。
        # 前端拿到「键缺失」和拿到 False 是两种待遇，别让两条链分家。
        "rewrite": False,
        "is_2k": False,           # 同上：读不到真实输出尺寸就不打这个标
        "prompt": "",
        "negative_prompt": "",
        "steps": None,            # one-step 模型，没有步数这个旋钮
        "seed": img.get("seed"),
        "cfg": None,
        "sampler": None,
        "scheduler": None,
        "width": None,
        "height": None,
        "size_source": "源图尺寸 × scale（图里不存源图尺寸）",
        "resolution": None,
        "source_image": src.get("image"),
        # 放大只吃一张，但键集要与 `describe()` 一致（见那里的说明）
        "source_images": [src["image"]] if src.get("image") else [],
        "scale": img.get("scale"),
        "lazy_cache": False,
        "te_speed": False,
        # 同上：VOSR2 走的是另一套权重，没有这个旋钮。
        "turbo": False,
        "nodes": len(wf),
    }


def _edited_images(wf, te):
    """图生图里挂着的参考图，**按 `image_N` 的编号**排好顺序。

    从 `images.image_N` 反查节点，而不是去数 `LoadImage` 节点 —— 前者才是
    「哪些真的连到了文本编码器上」；图里多一个没接上的 LoadImage 不该被算进来。
    `image_N` 缺失时按编号回退到 `LoadImage` 节点本身，这样老图（或者手搭的图）
    照样读得出来。
    """
    out = []
    pairs = []
    for key, value in (te or {}).items():
        if not key.startswith("images.image_"):
            continue
        try:
            index = int(key.rsplit("_", 1)[-1])
        except (TypeError, ValueError):
            continue
        nid = value[0] if isinstance(value, (list, tuple)) and value else None
        pairs.append((index, nid))
    for _, nid in sorted(pairs):
        node = wf.get(str(nid)) if nid is not None else None
        name = ((node or {}).get("inputs") or {}).get("image")
        if name:
            out.append(name)
    if not out:
        # 兜底：没有 autogrow 键（老图/手搭图）时按节点 id 找 LoadImage
        for nid, node in sorted(wf.items(),
                                key=lambda kv: (len(str(kv[0])), str(kv[0]))):
            if (node or {}).get("class_type") == "LoadImage":
                name = (node.get("inputs") or {}).get("image")
                if name:
                    out.append(name)
    return out


def describe(wf):
    """从 API 工作流反解出参数 —— 画廊与表单回显用。"""
    if is_upscale(wf):
        # 按节点类分流。让**所有**调用方都自动认出来，好过让每个调用方
        # 各自记得「放大模式要换一个反解函数」—— 忘一个就是一处静默的错。
        return describe_upscale(wf)
    ks = wf.get(KSAMPLER, {}).get("inputs", {})
    te = wf.get(TEXT_ENCODE, {}).get("inputs", {})
    lat = wf.get(LATENT, {}).get("inputs", {})

    editing = LOAD_IMAGE in wf
    sw = wf.get(SWITCH, {}).get("inputs", {})
    if editing:
        mode = "edit"
        prompt = te.get("prompt", "")
        source_images = _edited_images(wf, te)
        width = height = None
    else:
        mode = "t2i"
        prompt = (sw.get("on_false")
                  or wf.get(PE_PROMPT, {}).get("inputs", {}).get("prompt", ""))
        source_images = []
        # 字面量才回报；链接到 ResolutionSelector 的情况如实说"由节点决定"
        width = lat.get("width") if isinstance(lat.get("width"), int) else None
        height = lat.get("height") if isinstance(lat.get("height"), int) else None

    return {
        "mode": mode,
        # 改写是**额外功能**（2026-10-01 起）：反解出来的位置与 mode 平级，
        # 不再冒充一个模式。图生图那条路把 `SWITCH` 删了 —— 它恒为 False。
        "rewrite": bool(sw.get("switch")),
        "is_2k": bool(width and width >= DEFAULT_2K)
                 or bool(height and height >= DEFAULT_2K),
        "prompt": prompt,
        "negative_prompt": te.get("negative_prompt", ""),
        "steps": ks.get("steps"),
        "seed": ks.get("seed"),
        "cfg": ks.get("cfg"),
        "sampler": ks.get("sampler_name"),
        "scheduler": ks.get("scheduler"),
        "width": width,
        "height": height,
        "size_source": ("ResolutionSelector(节点 %s)" % RESOLUTION
                        if not editing and width is None and height is None
                        else "literal"),
        "resolution": te.get("resolution") if editing else None,
        # 单数与复数**恒等**（前者就是后者的一项）。两处都给，是因为拿这
        # 两个键的地方本来就不同：画廊项/历史回填用单数，多张那份列表用复数。
        "source_image": (source_images[0] if source_images else None),
        "source_images": source_images,
        # 放大模式才有；这里恒为 None，只为让两种图的反解**键集一致** ——
        # 前端拿到缺失的键和拿到 null 是两种待遇，别让它们分家。
        "scale": None,
        "lazy_cache": LAZY_CACHE in wf,
        "te_speed": TE_SPEED in wf,
        # Turbo 与前两个**不在同一层**：它是权重补丁，位置在它们之前，所以
        # 它与 te_speed / lazy_cache **可以同时为真**（不像那两个互斥）。
        "turbo": TURBO_LORA in wf,
        "nodes": len(wf),
    }


def acceleration_line(wf):
    """一行说清这张图接了哪些加速（一个都没接也直说）。

    为什么要有它：TE-Speed 从 2026-10-06 起**默认就接** —— 默认生效的东西必须
    **看得见**，否则「到底接了没接」只能靠翻 JSON 猜。四个 CLI 包装脚本与
    `qwen21_build.py` 自己的命令行共用这一份说法（各写一份就迟早分家）。

    **Turbo 与前两个不是三选一**：它是权重补丁，插在两个加速节点之前，所以它
    与 te_speed / lazy_cache 可以同时出现。这一行就按「挂没挂 LoRA」+「接了哪
    个加速节点」两段拼。
    """
    info = describe(wf)
    if info["te_speed"]:
        who = (f"TE-Speed 已接入（{TE_SPEED_ATTENTION} / {TE_SPEED_STEP_CACHE} / "
               f"阈值 {TE_SPEED_REUSE_THRESHOLD}）")
    elif info["lazy_cache"]:
        who = f"LazyCache 已接入（阈值 {LAZY_CACHE_THRESHOLD}）"
    else:
        who = "两个加速节点都没接"
    if info["turbo"]:
        return (f"Turbo LoRA 已接入（{TURBO_DEFAULT_STEPS} 步 / "
                f"{TURBO_DEFAULT_SCHEDULER} 是这一组实测的默认）；{who}")
    return who


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="构建 Qwen-Image-2.1 / VOSR2 API 工作流")
    ap.add_argument("mode", choices=ALL_MODES)
    # 放大模式**没有提示词** —— 所以这个位置参数可选，缺了由下面按模式判。
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--cfg", type=float)
    ap.add_argument("--sampler")
    ap.add_argument("--scheduler")
    ap.add_argument("--rewrite", action="store_true",
                    help="打开提示词改写器（2026-10-01 起取代 mode=refine）。"
                         "只对文生图有意义：图生图那条链路没有改写器")
    ap.add_argument("--src", action="append",
                    help="参考图 / 源图（须已在 ComfyUI input/ 里）。"
                         "**可给多次**：edit 模式最多 %d 张，第 1 张决定输出尺寸；"
                         "放大模式只取第 1 张" % MAX_REFERENCE_IMAGES)
    ap.add_argument("--resolution", type=int)
    ap.add_argument("--scale", type=int,
                    help=f"放大倍数 {UPSCALE_MIN_SCALE}..{UPSCALE_MAX_SCALE}"
                         f"（默认 {UPSCALE_DEFAULT_SCALE}）")
    ap.add_argument("--base")
    ap.add_argument("--lazy-cache", action="store_true",
                    help="接上 LazyCache 步复用（实测省约一半采样时间，细节略平滑）")
    ap.add_argument("--te-speed", dest="te_speed", action="store_true",
                    default=None,
                    help="接上 TE-Speed —— **默认就接**（实测省约 2/3 采样时间，"
                         "细节保住；需已安装该插件）")
    ap.add_argument("--no-te-speed", dest="te_speed", action="store_false",
                    help="这一张不接 TE-Speed（默认接；与 --lazy-cache 互斥）")
    ap.add_argument("--turbo", dest="turbo", action="store_true", default=None,
                    help="挂上官方 Turbo LoRA，并把步数/调度器补成实测那一组"
                         "（%d 步 / %s）。**默认不挂** —— 证据只有一条提示词一个种子。"
                         "它与 TE-Speed / LazyCache 都不冲突"
                         % (TURBO_DEFAULT_STEPS, TURBO_DEFAULT_SCHEDULER))
    ap.add_argument("--no-turbo", dest="turbo", action="store_false",
                    help="这一张不挂 Turbo LoRA（**本就是默认**，写了只是把话说死）")
    ap.add_argument("--negative", default=None,
                    help="反向词。**CFG<=1 时它不被求值**，见 workbench/config.py")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)

    refs = a.src or []
    # TE-Speed **默认接**（`None` = 命令行没表态）。它与 LazyCache 互斥，而互斥是
    # 构建器当场抛的 —— **不搞"自动挑一个"**（那会让人以为两个都生效了）。所以
    # 这里也当场拒，只是把「怎么改」写清楚：命令行上的报错要能直接照做。
    te_speed = True if a.te_speed is None else a.te_speed
    # Turbo **默认不挂**（`None` = 命令行没表态）。与 TE-Speed 那个默认值相反，
    # 因为它的证据只有一条提示词一个种子 —— 还不够格当默认。
    turbo = False if a.turbo is None else a.turbo
    if a.mode != UPSCALE_MODE and a.lazy_cache and te_speed:
        ap.error("LazyCache 与 TE-Speed 只能二选一（TE-Speed 现在**默认就接**）："
                 "想只接 LazyCache 请加 --no-te-speed")
    if a.mode == UPSCALE_MODE:
        if a.lazy_cache or a.te_speed or turbo:
            ap.error("放大模式走的是 VOSR2，那几个加速开关属于 Qwen 出图，对它无效")
        if a.rewrite:
            ap.error("放大模式走的是 VOSR2，它没有文本编码器，改写器对它无效")
        if a.negative:
            ap.error("放大模式走的是 VOSR2，它没有文本编码器，反向词对它无效")
        if len(refs) > 1:
            ap.error("放大只吃一张源图（VOSR2 那个节点就一个 images 输入），"
                     "收到了 %d 张：%s" % (len(refs), "、".join(refs)))
        wf = build_upscale(refs[0] if refs else None, scale=a.scale, seed=a.seed)
    else:
        if not a.prompt:
            ap.error(f"模式 {a.mode} 需要提示词")
        wf = build(a.mode, prompt=a.prompt, steps=a.steps, width=a.width,
                   height=a.height, seed=a.seed, cfg=a.cfg, sampler=a.sampler,
                   scheduler=a.scheduler, rewrite=a.rewrite,
                   source_images=refs or None,
                   resolution=a.resolution, base=a.base,
                   lazy_cache=a.lazy_cache, te_speed=te_speed, turbo=turbo,
                   negative_prompt=a.negative)

    out = a.out or os.path.join(WORKSPACE, f"qwen21_{a.mode}_api.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)

    info = describe(wf)
    print(f"已写出: {out}")
    print(f"模式  : {info['mode']}" + ("  (2K)" if info["is_2k"] else ""))
    print(f"节点数: {info['nodes']}")
    if info["mode"] == UPSCALE_MODE:
        print(f"源图  : {info['source_image']}")
        print(f"放大  : ×{info['scale']}  seed={info['seed']}"
              f"（输出尺寸 = 源图尺寸 × {info['scale']}，图里不存源图尺寸）")
        return 0
    print(f"steps={info['steps']}  seed={info['seed']}  cfg={info['cfg']}  "
          f"{info['sampler']}/{info['scheduler']}")
    if info["rewrite"]:
        print("改写    : 提示词改写器已打开（pe_t2i，+8.82 GiB / +113 秒）")
    # **无条件打**：「两个都没接」也要说出来 —— 默认生效的东西必须看得见。
    print("加速    :", acceleration_line(wf))
    if info["mode"] == "edit":
        print(f"参考图: {'、'.join(info['source_images'])}"
              f"（{len(info['source_images'])} 张）  resolution={info['resolution']}")
    else:
        w = info["width"] if info["width"] is not None else f"由节点 {RESOLUTION} 决定"
        h = info["height"] if info["height"] is not None else f"由节点 {RESOLUTION} 决定"
        print(f"尺寸  : {w} x {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
