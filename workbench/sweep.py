"""seed × steps 网格扫描 —— 只做「把一份表单摊成 N 份」这一件事。

为什么单独一个模块：这是**纯函数**。没有 IO、没有 ComfyUI、没有线程。
网格展开恰恰是最容易出边界错的地方（空列表、超上限、重复组合、单个数字），
隔离出来就能用毫秒级单测钉死，不必每次都起后端烧 GPU。

一条纪律：**扫描的每一格就是一次普通的出图。** 不新造任务类型、不新造收尾路径 ——
它复用 P1 的提交、P3 的耗时、P2 的运行记录。扫描只是「一次提交多个」而已。
所以这里也**不做**并发控制：队列是 ComfyUI 自己的（`POST /prompt` 天然排队），
一次 3×3 就是 9 张排在后面，工作台照旧逐张跟踪。
"""
# 一次扫描最多排多少张。默认跟 spec §6.2 的 `max_jobs` 一致。
# 为什么必须有上限：2048² 一张实测 4~13 分钟，5×5 就是 25 张、
# 最坏情况要跑 5 个小时。上限是**防手滑**，不是防技术。
MAX_JOBS = 12
HARD_MAX_JOBS = 64        # 连请求里想调大也有个天花板

# 与表单、构建器一致的边界（qwen21_build 只管有没有值，范围在这里守）
MAX_STEPS = 200
MAX_SEED = 2 ** 31 - 1

GRID_KEYS = ("seed", "steps")


class GridError(ValueError):
    """网格不合法。继承 ValueError —— server 统一把它变成 400。"""


def _as_int(value, what):
    if isinstance(value, bool) or value is None:
        raise GridError("%s 必须是整数，收到 %r" % (what, value))
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            raise GridError("%s 不能是空字符串" % what)
        try:
            return int(text)
        except ValueError:
            raise GridError("%s 必须是整数，收到 %r" % (what, value))
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise GridError("%s 必须是整数，收到 %r" % (what, value))


def _axis(grid, key):
    """取一条轴上的取值列表。允许写单个数字（`"seed": 123`）当一元列表。"""
    if key not in grid:
        return []
    raw = grid[key]
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = [raw]           # 单个数字也收 —— 别逼用户为了一元写方括号
    out = []
    for i, item in enumerate(items):
        out.append(_as_int(item, "%s[%d]" % (key, i)))
    return out


def _dedupe(values):
    """去重**保留顺序**，并回报丢掉了几个。"""
    out, seen = [], set()
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out, len(values) - len(out)


def parse_grid(grid, max_jobs=None):
    """把 `{seed:[…], steps:[…]}` 摊成有序的组合列表。

    返回 `{"cells": [{seed, steps}, …], "count": n, "dropped_dupes": n}`。

    三条刻意的行为：

    - **先去重、再判上限**。`[7,7,7,7]` 是**1 张**不是 4 张，
      所以它不该被 12 张的上限挡住，报错时说的张数也该是真的那个数。
      重复项不是错误，是浪费 —— 2048² 一张 4~13 分钟，同一个 seed 跑两遍纯属白等。
      去掉，但**报出来**（`dropped_dupes`），不静默改用户给的东西。
    - **笛卡尔积**。`seed:[1,2] × steps:[10,20]` 是 4 格而不是 2 格 ——
      想要「只换 seed」就给一条 steps 轴。
    - **上限只收紧不放开**。`max_jobs` 传得比 `HARD_MAX_JOBS` 大时**夹**到硬上限，
      而不是报错：调用方要的是「尽量多排」，我们给的答复是「最多这么多」。
    """
    if grid is None:
        # `grid` 整个没给（最常见的「忘了写」）等同于给一个空网格，
        # 这样报出来的话是「至少要给一条轴」而不是「必须是对象」——
        # 后者对着一个**不存在**的 grid 说「类型不对」，帮不上忙。
        grid = {}
    if not isinstance(grid, dict):
        raise GridError("grid 必须是对象，形如 {\"seed\":[1,2,3]}")
    limit = MAX_JOBS if max_jobs is None else _as_int(max_jobs, "max_jobs")
    if limit < 1:
        raise GridError("max_jobs 必须 ≥ 1，收到 %r" % limit)
    limit = min(limit, HARD_MAX_JOBS)

    seeds, seed_dupes = _dedupe(_axis(grid, "seed"))
    steps, step_dupes = _dedupe(_axis(grid, "steps"))
    if not seeds and not steps:
        raise GridError("grid 至少要给一条轴：`seed` 或 `steps`，且不能是空列表")

    # 缺哪条轴就用「不指定」当唯一取值 —— 表示「保留基准里的原值」
    seed_axis = seeds or [None]
    steps_axis = steps or [None]

    for s in seeds:
        if s < 0 or s > MAX_SEED:
            raise GridError("seed 要在 0..%d 之间，收到 %d" % (MAX_SEED, s))
    for n in steps:
        if n < 1 or n > MAX_STEPS:
            raise GridError("steps 要在 1..%d 之间，收到 %d" % (MAX_STEPS, n))

    total = len(seed_axis) * len(steps_axis)
    if total > limit:
        raise GridError(
            "这次扫描要排 %d 张（%d seed × %d steps），超过上限 %d 张。"
            "收窄一点，或把 max_jobs 调大（硬上限 %d）。"
            "提醒：2048² 一张实测 4~13 分钟。"
            % (total, len(seed_axis), len(steps_axis), limit, HARD_MAX_JOBS))

    cells = []
    # steps 在外层：同一个 steps 下的几个 seed 排在一起，看着更像「一组对比」
    for n in steps_axis:
        for s in seed_axis:
            cells.append({"seed": s, "steps": n})
    return {"cells": cells, "count": len(cells),
            "dropped_dupes": seed_dupes + step_dupes, "requested": total}


def expand(body, cells):
    """把一格的取值合进原始表单，得到一条可以直接喂 `build_from_body` 的请求体。

    `grid` / `sweep_id` 要剔掉：它们不是构建器的参数，留着会在
    `build_from_body` 里被当成未知字段（无害）但更糟的是会被写进 `params`
    再进 PNG 之外的记录 —— 记录里那两份东西该只描述**这一次出图**。

    `auto_upscale`（出图后自动放大）也在这里剔掉，理由不同：它一开就是
    **每格两个作业**（N 格变 2N），而扫描的语义是「并排看几格参数」——
    链一进来，「扫到一半崩了整次作废」那条规则就说不清了。调用方会把
    「被丢掉了」这件事报给页面，别静默降级。
    """
    base = {k: v for k, v in (body or {}).items()
            if k not in ("grid", "max_jobs", "sweep_id", "auto_upscale")}
    # 放大模式**没有步数**（VOSR2 是 one-step 模型）。要是在这儿放过一条 steps 轴，
    # 构建器会**默默忽略**它 —— 于是 N 格全得到差不多的图，而页面把它们并排摆成
    # 「参数对比」。那正是本工作区最忌讳的那种错：没有症状、看着像结论。
    # 所以在合之前就炸，并说清该改用什么。
    # （`upscale` 这个名字的真源是 `qwen21_build.UPSCALE_MODE`；这里写死字面量
    #   是为了不让 sweep 多背一份对构建器模块的依赖 —— 与 pngmeta 同样的取舍。）
    if base.get("mode") == "upscale" and any(c.get("steps") is not None
                                             for c in cells):
        raise GridError("放大（VOSR2）是 one-step 模型，**没有步数**这个旋钮 ——"
                        "扫步数只会得到几张一模一样的图。要对比就扫 seed："
                        "实测换 seed 会改 73% 的像素。")
    # H3 同一条道理，但原因不同：它的步数**不是没有，而是不可调** ——
    # 4 步是 `lightx2v_turbo_4step` 蒸馏 LoRA 的固定档，页面上根本不给这个旋钮。
    # 放过一条 steps 轴的话，后端会把它丢掉，于是 N 格跑的是**同一份参数**，
    # 而页面把它们并排摆成「参数对比」—— 一次 310 秒 × N，还得不到任何结论。
    if base.get("mode") == "h3" and any(c.get("steps") is not None
                                        for c in cells):
        raise GridError("H3 的步数是 4 步蒸馏 LoRA 的固定档，**页面上没有这个旋钮** ——"
                        "扫步数只会得到几段一模一样的视频（每段约 5 分钟）。"
                        "要对比就扫 seed。")
    out = []
    for cell in cells:
        one = dict(base)
        if cell.get("seed") is not None:
            one["seed"] = cell["seed"]
        if cell.get("steps") is not None:
            one["steps"] = cell["steps"]
        out.append(one)
    return out


def per_step(wall_ms, steps, timing=None):
    """每次采样多久，以及**这个数是从哪来的** —— 后者不能省。

    优先用探针的采样循环读数（`timing["sampling"]["per_step_ms"]`），它只包住
    纯去噪循环，是「每步多久」的正确来源（见 AGENTS.md「采样要拆成两层」）。

    探针没开时退化成 `wall_ms / steps`。但那个数**含模型加载、文本编码与解码**，
    比真实步耗时偏大（冷启动时能偏出一大截），所以必须带着 `source` 一起显示，
    不能与探针读数鱼目混珠 —— 这正是 P3 反复强调的那类「看起来像好事的假数字」。
    """
    out = {"ms": None, "source": None, "wall_ms": wall_ms, "steps": steps}
    if wall_ms is None:
        return out
    sampled = (timing or {}).get("sampling") or {}
    probe_ms = sampled.get("per_step_ms")
    probe_steps = sampled.get("steps")
    # 步数以探针为准校验一次：不一致说明这条 timing 不是这一格的，宁可不给
    if probe_ms and (probe_steps is None or not steps or probe_steps == steps):
        out["ms"] = round(float(probe_ms), 1)
        out["source"] = "probe"
        return out
    if steps:
        out["ms"] = round(float(wall_ms) / float(steps), 1)
        out["source"] = "wall_over_steps"
    return out


def label(source):
    """给页面用的中文说明 —— 把「这个数是怎么来的」摆在台面上。"""
    return {
        "probe": "探针采样循环读数",
        "wall_over_steps": "wall_ms ÷ 步数（含加载与解码，偏大）",
    }.get(source, "未知来源")
