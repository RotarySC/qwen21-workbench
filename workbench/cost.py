"""出图耗时估算 —— 数值全部来自实测，公式是拟合出来的，别当定律。

实测锚点（AGENTS.md「耗时与显存」）：

    1024²（1 MP）  2.3~2.5 s/步
    2048²（4 MP）  ≈25 s/步

两点拟合出幂律 `s/步 = 2.50 × MP^1.66`。**在 1~4 MP 之间插值可信，
出了这个区间就是外推**，所以它叫「经验估算」而不是「预计」。

2026-10-01 起**页面上不再显示这一行**（按用户要求去掉）；估时本身仍在
`/api/build` 的 `estimate` 与任务记录 `estimate_s` 里，进度面板照显。
名字留在这里只是说明这个数的性质。

刻意不猜的部分：冷启动多出的约 12 秒取决于当时显存里还剩什么，
改写器多出的约 113 秒同理 —— 只给固定值，不给「可能更快」的假安慰。
"""
import math

from . import config

# 由两个实测锚点解出：log(25.0/2.50) / log(4) = 1.6609…
PIXEL_EXPONENT = math.log(config.SEC_PER_STEP_4MP / config.SEC_PER_STEP_1MP) / math.log(4.0)


def per_step_seconds(width, height):
    w = width or config.DEFAULT_1MP
    h = height or config.DEFAULT_1MP
    megapixels = (w * h) / float(config.DEFAULT_1MP ** 2)
    return config.SEC_PER_STEP_1MP * (megapixels ** PIXEL_EXPONENT)


def estimate_seconds(mode, steps, width, height, cold=True, rewrite=False):
    """返回 (总秒数, 分解字典)。分解给 UI 显示，免得只给一个数字。

    `rewrite=True`（提示词改写器）要多加载 8.82 GiB、约 +113 秒。2026-10-01 之前
    这件事是**模式** `refine` 的一部分，现在它是独立的开关，所以从 `mode` 里拆出来
    单独传 —— 这样「同一张图的配置」与「哪条链路」就不会再挤在同一个参数里。
    """
    steps = steps or 20
    if mode == "edit":
        # 编辑模式的 latent 由参考图尺寸决定，这里按 resolution 估
        width = height = width or config.DEFAULT_1MP
    per = per_step_seconds(width, height)
    sampling = per * steps
    parts = {
        "sampling_s": round(sampling, 1),
        "overhead_s": config.FIXED_OVERHEAD_S,
        "rewriter_s": config.REWRITER_S if rewrite else 0.0,
        "cold_load_s": config.COLD_LOAD_S if cold else 0.0,
        "per_step_s": round(per, 3),
    }
    total = sum(parts.values())
    return total, parts


def estimate_upscale_seconds(width, height, cold=True):
    """放大（VOSR2）的耗时估算。返回 (总秒数, 分解字典)。

    **参数里的 width/height 是输出尺寸**（源图尺寸 × scale），不是输入 ——
    这个模型的代价只跟输出走，与输入尺寸几乎无关（实测 2×2 矩阵，见
    `config.UPSCALE_BASE_S` 的注释）。把它当成输入尺寸就会差一个 scale² 的量级。

    读不到源图尺寸时传 None：这时**如实说**是按 1024² 输出假设的
    （`parts["assumed"]=True`），不装作知道。
    """
    if width and height:
        out_w, out_h = width, height
        assumed = False
    else:
        out_w = out_h = config.DEFAULT_1MP
        assumed = True
    megapixels = (out_w * out_h) / float(config.DEFAULT_1MP ** 2)
    upscale = config.UPSCALE_BASE_S * (megapixels ** config.UPSCALE_MP_EXPONENT)
    parts = {
        "upscale_s": round(upscale, 1),
        "cold_load_s": config.UPSCALE_COLD_LOAD_S if cold else 0.0,
        "output_mp": round(megapixels, 2),
        "assumed": assumed,
    }
    return sum(parts[k] for k in ("upscale_s", "cold_load_s")), parts


def estimate_h3_seconds(seconds=None, cold=True):
    """H3 图生视频的耗时估算。返回 (总秒数, 分解字典)。

    **单锚点**（4 步 / 5 秒 / 480×864，两次实测都是 310 秒上下），所以这里既不做
    分段，也不按时长外推 —— 分解里带 `assumed=True`，让调用方如实说
    「这一档没实测过，是按那一次推的」。

    为什么宁可不外推：帧数与时长的关系没量过（5 秒 124 帧、10 秒 243 帧），而扩散
    的代价随 token 数走、token 数随帧数走 —— 线性外推会给出一个**偏小**的数，
    而偏小的估时挨着真实耗时，看起来只是工作台算错了。
    """
    seconds = config.H3_DURATION_S if seconds is None else seconds
    assumed = abs(float(seconds) - config.H3_DURATION_S) > 1e-6
    parts = {
        "h3_s": round(config.H3_SECONDS, 1),
        # 那个锚点**已经含**模型加载与解码（是 ComfyUI 自己那句
        # `Prompt executed in` 的读数），所以这里不能再加一份 `cold_load_s`。
        "cold_load_s": 0.0,
        "assumed": assumed,
        "measured_frames": config.H3_FRAMES,
        "measured_seconds": config.H3_DURATION_S,
    }
    return parts["h3_s"], parts


LAYERS_RES_EXPONENT = (math.log(config.LAYERS_ANCHOR_B[1] / config.LAYERS_ANCHOR_A[1])
                       / math.log(float(config.LAYERS_ANCHOR_B[0])
                                  / float(config.LAYERS_ANCHOR_A[0])))


def estimate_layers_seconds(resolution=None, steps=None, resolution_depth=None,
                            cold=True):
    """See-through 拆层的耗时估算。返回 (总秒数, 分解字典)。

    由**两个锚点**解出幂律 `T = 252.40 × (res/1024)^2.0989`
    （1024 → 252.40 秒、1280 → 403.20 秒）。指数 2.0989 略高于面积的 2 ——
    两个锚点因此都被精确复现，而不是拿面积律去近似其中一个。

    `resolution_depth` 只用来判「要不要标记为外推」：跟随（`-1`）时深度那一段
    跑在 `resolution` 上，而两个锚点都是这个配置；给了更小的值只会**更快**，
    所以这里**不**按它缩时间 —— 缩了就把锚点本身也一起改掉了。
    """
    res = resolution or config.LAYERS_BASE_RES
    steps = steps or config.LAYERS_BASE_STEPS
    scale = (float(res) / float(config.LAYERS_BASE_RES)) ** LAYERS_RES_EXPONENT
    step_factor = float(steps) / float(config.LAYERS_BASE_STEPS)
    total = config.LAYERS_ANCHOR_A[1] * scale * step_factor
    # 两个锚点都在 1024..1280 之间、都是 30 步。出这个范围就是外推 ——
    # 尤其是**步数那一维**：它一次都没被测过。
    assumed = (res > config.LAYERS_ANCHOR_B[0]
               or res < config.LAYERS_BASE_RES
               or steps != config.LAYERS_BASE_STEPS)
    parts = {
        "layers_s": round(total, 1),
        "res_scale": round(scale, 3),
        "step_factor": round(step_factor, 3),
        # 锚点本身含模型加载（nf4 权重 + Marigold），所以不再加一份。
        "cold_load_s": 0.0,
        "assumed": assumed,
    }
    return parts["layers_s"], parts


def humanize(seconds):
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return "%d 秒" % seconds
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return "%d 分 %02d 秒" % (minutes, rest)
    hours, minutes = divmod(minutes, 60)
    return "%d 小时 %02d 分" % (hours, minutes)


# --- 成本预警 ------------------------------------------------------------
# 两个理由是**分开**的，因为它们防的不是同一件事：
#   · 时间 —— 一次 11 分钟的等待，误点一次就没了
#   · 显存 —— 改写器要多占 8.82 GiB，22 GB 卡上跑完 2K 再叠它会 OOM
# 所以门槛不是「一个秒数」，而是「秒数 或 某些模式」。
CONFIRM_AFTER_S = 120.0
# 「改写器要不要拦一道」的判据是**开关**而不是模式（2026-10-01 起）：
# 它从一开始就与 `mode == "refine"` 等价，只是那时没有独立的字段可用。
# 「真的在跑大图」的判据 —— 与 `qwen21_build.describe()` 的 `is_2k` 保持一致（≥2048）
BIG_SIDE = 2048
# 放大模式的「大图」门槛要高得多：实测 4096² 输出只要 41 秒、峰值显存约 7.5 GB，
# 这个量级不值得拦。8192² 才是 4 倍面积（≈67 MP）——那里显存与出图体积都该提醒。
UPSCALE_BIG_SIDE = 8192


def confirm_policy(mode, seconds, width=None, height=None, rewrite=False):
    """要不要用户再点一次才算数。返回 None = 不需要。

    刻意放在这里而不是前端：这样 curl 直接打 `/api/run` 也挡得住，
    而且门槛本身（哪个开关、多少秒）是能用单测钉住的。
    """
    reasons = []
    if rewrite:
        reasons.append("改写器要多加载 8.82 GiB（约 +%d 秒）"
                       % int(config.REWRITER_S))
    if seconds is not None and seconds >= CONFIRM_AFTER_S:
        reasons.append("预计 %s，超过 %d 秒"
                       % (humanize(seconds), int(CONFIRM_AFTER_S)))
    if mode == "upscale":
        # 放大的尺寸判据与出图**不是一回事**：这里 width/height 是输出尺寸，
        # 而且 2048² 的输出只是常规操作（41 秒），拿 BIG_SIDE 去拦会误拦一片。
        if (width and width >= UPSCALE_BIG_SIDE) or (height and height >= UPSCALE_BIG_SIDE):
            reasons.append("输出 %s×%s 是 4 倍于实测过的最大档"
                           "（4096² 输出实测 41 秒、峰值显存约 7.5 GB）——"
                           "再往上显存与出图体积都会明显吃紧"
                           % (width, height))
    # 2048² 的步耗时对**显存状态**极敏感，与秒数无关，所以单独说一句。
    # 判据用**实际尺寸**而不是「有没有那个 2K 模式」—— 那个模式 2026-10-01 已经
    # 撤掉了（分辨率在「基础参数 → 尺寸」里调），而判据当时就已经落在尺寸上
    # （第一版只看 mode，把 `2k` + 1024² 误拦了，单测当场抓住）。
    # 放大模式走的是 VOSR2，没有「每步耗时」，所以这条对它不适用。
    elif (width and width >= BIG_SIDE) or (height and height >= BIG_SIDE):
        reasons.append("2048² 的步耗时受显存状态影响极大：干净状态实测 26.6 s/步，"
                       "而紧接着一次改写器（refine）之后实测 77.4 s/步 —— 同一张图"
                       "从 4.4 分钟变 13 分钟。跑之前先腾显存（`/free`）")
    if not reasons:
        return None
    return {
        "required": True,
        "reasons": reasons,
        "text": "；".join(reasons) + "。再点一次「确认提交」才会开始。",
    }


def merge_policies(*policies):
    """把几次判定合成一条 —— 出图 + 自动放大那条链上两道门槛都要保留。

    合并的是**理由**（各自独立成立），不是阈值：漏掉任何一条都等于给那条路
    开了个后门（比如「出图不贵、但自动 ×4 放大到 8192²」）。
    """
    kept = [p for p in policies if p]
    if not kept:
        return None
    reasons = []
    for p in kept:
        for reason in p.get("reasons") or []:
            if reason not in reasons:      # 同一句话不重复说两遍
                reasons.append(reason)
    return {
        "required": True,
        "reasons": reasons,
        "text": "；".join(reasons) + "。再点一次「确认提交」才会开始。",
    }


def cost_model():
    """给前端的常数 —— 只此一处真源，免得前后端两套数字漂移。"""
    return {
        "sec_per_step_1mp": config.SEC_PER_STEP_1MP,
        "sec_per_step_4mp": config.SEC_PER_STEP_4MP,
        "fixed_overhead_s": config.FIXED_OVERHEAD_S,
        "cold_load_s": config.COLD_LOAD_S,
        "rewriter_s": config.REWRITER_S,
        "supported_range_mp": [1.0, 4.0],
        # 放大那一套是**另一条公式**（one-step，没有步数），别和上面混用
        "upscale_base_s": config.UPSCALE_BASE_S,
        "upscale_mp_exponent": config.UPSCALE_MP_EXPONENT,
        "upscale_cold_load_s": config.UPSCALE_COLD_LOAD_S,
        "upscale_anchors": [
            {"case": "1024² →×2 = 2048² 输出", "seconds": 14,
             "note": "实测 5 次 13.24~14.71 s（插件自报），墙钟 14.2 s"},
            {"case": "1024² →×4 = 4096² 输出", "seconds": 39,
             "note": "实测 38.90 s，墙钟 41.4 s，峰值显存约 7.5 GB"},
            {"case": "512² →×4 = 2048² 输出", "seconds": 14,
             "note": "实测 13.65 / 14.71 s —— **与 1024² 的输入同价**，"
                     "所以这个成本模型只看输出尺寸"},
        ],
        # --- MiniMax H3（单锚点，别按时长外推）----------------------------
        "h3_seconds": config.H3_SECONDS,
        "h3_anchor": {
            "case": "480×864 / 124 帧 / 4 步 / LoRA 0.75",
            "seconds": config.H3_SECONDS,
            "note": "两次实测 310.31 s 与 316.9 s（都含加载与解码）。"
                    "**只有一个锚点** —— 帧数与时长的关系没量过，所以别的时长"
                    "不给数。峰值显存 21.24 / 22 GiB（96.5%），跑之前先腾显存。",
        },
        # --- See-through（两锚点解一个幂律指数，随分辨率）------------------
        "layers_anchor_s": config.LAYERS_ANCHOR_A[1],
        "layers_anchor_res": config.LAYERS_ANCHOR_A[0],
        "layers_res_exponent": round(LAYERS_RES_EXPONENT, 4),
        "layers_base_steps": config.LAYERS_BASE_STEPS,
        "layers_anchors": [
            {"case": "1024 / 30 步 / 深度跟随", "seconds": 252.4,
             "note": "实测 252.40 s（fp16，nf4；23 层）"},
            {"case": "1280 / 30 步 / 深度跟随", "seconds": 403.2,
             "note": "实测 403.20 s（fp16，nf4；24 层，多一个 tail）"},
        ],
        "anchors": [
            {"case": "1024² / 20 步", "seconds": 65, "note": "实测温启动 53.4 s"},
            {"case": "1024² / 40 步", "seconds": 115, "note": "实测冷 110.2 / 温 98.0"},
            {"case": "1024² / 25 步 + 改写器", "seconds": 190,
             "note": "另需 ~8.8 GB 显存"},
            {"case": "2048² / 25 步", "seconds": 665,
             "note": "实测 11 分 05 秒，峰值显存 ~18.9 GB"},
            {"case": "2048² / 10 步（紧接在 refine 之后）", "seconds": 783,
             "note": "实测 77.4 s/步（探针采样循环读数），而干净状态是 26.6 s/步。"
                     "**同一个模式的步耗时能差 2.9 倍**，取决于跑之前显存里还剩什么。"
                     "本表给的是干净状态的预期，别当成保证。"},
        ],
    }
