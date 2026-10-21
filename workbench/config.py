"""工作台的路径、端口与常量 —— 只此一处。

成本模型里的每个数字都是**实测值**，不是估的；出处见 AGENTS.md「耗时与显存」。
"""
import glob
import os
import re

# --- 目录 ---------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))     # …/qwen21-workbench/workbench
REPO = os.path.dirname(HERE)                          # …/qwen21-workbench
# 基准图与产物所在的「运维现场」= 仓库的上一层（就是 AGENTS.md 那个工作区）
WORKSPACE = os.environ.get("QWEN21_WORKSPACE") or os.path.dirname(REPO)

STATE_DIR = os.environ.get("QWEN21_STATE") or os.path.join(REPO, "state")
JOBS_DIR = os.path.join(STATE_DIR, "jobs")
RUNS_FILE = os.path.join(STATE_DIR, "runs.jsonl")
PROMPTS_FILE = os.path.join(STATE_DIR, "prompts.json")
CLIENT_ID_FILE = os.path.join(STATE_DIR, "client_id.txt")
BACKEND_LOG = os.path.join(STATE_DIR, "backend.log")
STATIC_DIR = os.path.join(HERE, "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")

# --- 工作台自身 ---------------------------------------------------------
HOST = os.environ.get("QWEN21_HOST", "127.0.0.1")
PORT = int(os.environ.get("QWEN21_PORT", "8189"))

# --- ComfyUI ------------------------------------------------------------
COMFY_HOST = os.environ.get("QWEN21_COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("QWEN21_COMFY_PORT", "8188"))
COMFY_BASE = "http://%s:%d" % (COMFY_HOST, COMFY_PORT)
COMFY_WS = "ws://%s:%d/ws" % (COMFY_HOST, COMFY_PORT)

HTTP_TIMEOUT = 5.0        # 本地调用，要快。出图的等待不在这里，靠轮询
PROBE_TIMEOUT = 2.0       # 判据 6 的探测：**读**的上限（后端在答话但很慢 → 忙）
# **连接**的上限，必须比 PROBE_TIMEOUT 宽。实测本机连一个**没有人监听**的回环端口，
# `WSAECONNREFUSED` 要 ~2.0~2.4 秒才回来（raw socket、.NET、httpx 三路一致）。
# 用 2 秒去探就会**卡在拒绝到达之前**超时，于是「连接被拒」被误判成「忙」——
# 后果是 probe() 永远不返回 False：页面永远说不出「后端未运行」（判据 6 失效），
# 而「端口被占就拒绝启动」的规则会让一键启动**永远拒绝**。
# 6 秒给了 2.5 倍余量；后端在跑时这条根本用不到（实测 50 毫秒就答完）。
PROBE_CONNECT_TIMEOUT = 6.0
VIEW_TIMEOUT = 30.0       # /view 拉图可以慢
# /upload/image 传参考图。上限 32 MiB 在回环上是一瞬，但后端**正忙着出图**时
# 它自己的 HTTP 也会被占住（实测 2K 期间 /queue 五秒不答），所以给得比 HTTP_TIMEOUT 宽。
UPLOAD_TIMEOUT = 60.0
POLL_INTERVAL = 1.5       # 认领/收尾用的 /history 轮询间隔（秒）

# 判定「任务凭空消失」的宽限期：提交后到进 /queue 之间有个窗口，别误判
LOST_GRACE_MS = 15000

# 取消后等多久去 /history 拿真实终态。等到了就是 execution_interrupted（准），
# 等不到就直接判取消（用户意图优先）。
CANCEL_WAIT_S = 5.0

# 撤销**一批**任务（一次扫描）时，整批共用的等待预算。
# 为什么要有这个：单张等 5 秒是合理的，但 12 格的扫描如果每格都等，
# 一次「撤销」请求就要挂 60 秒 —— 页面看着像死了一样。
# 预算用完就按用户意图直接收尾，并把「没等到真实终态」的那几个 id 报出来。
CANCEL_BATCH_BUDGET_S = 5.0

# 一键启动后端（P6）时等它就绪的上限。spec §6.6 定的是 180 秒：
# 冷启动要读 16 GiB 权重的元数据、加载 custom nodes，实测十几秒；
# 180 秒是给「机器同时在干别的」留的余量。
BACKEND_READY_TIMEOUT_S = float(os.environ.get("QWEN21_READY_TIMEOUT", "180"))

# --- 铁律 1 的启动命令 --------------------------------------------------
COMFY_ROOT = os.environ.get("QWEN21_COMFY_ROOT") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Comfy-Desktop", "ComfyUI-Installs", "ComfyUI", "ComfyUI")
COMFY_PYTHON = os.path.join(COMFY_ROOT, ".venv", "Scripts", "python.exe")
INSTANCE_YAML_GLOB = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "Comfy Desktop", "instance-model-paths", "*.yaml")

# --- 出图落点：写进**工作区**，别再往 %LOCALAPPDATA% 的安装目录里堆 ----------
# `--output-directory` 是**唯一**能改落盘的开关：`%APPDATA%\Comfy Desktop\settings.json`
# 里的 `outputDir` 属于 Comfy Desktop 自己（手动启动根本不读它），而保存节点
# （`461 SaveImageAdvanced`）的 `filename_prefix` 只能给输出目录**里面的**子目录，
# 给不了绝对路径。
#
# 所以它必须写进**命令的唯一真源**：页面给用户复制的那条命令、以及「一键启动」按钮，
# 都从 `start_command()` 取 argv。只改 AGENTS.md 的文本会让两边悄悄分家 ——
# 文档说写工作区，页面却递给用户一条写安装目录的命令。
OUTPUT_DIR = os.environ.get("QWEN21_OUTPUT_DIR") or os.path.join(WORKSPACE, "output")

# 画廊的**归档目录**（P20h）。画廊里那颗「删除」不再把图扔进回收站，而是**挪到这里**：
# 归档是我们自己的一层 —— 页面里看得见名字、点一下就还原；回收站退回它该待的位置
# （最后一道兜底，见 trash.py 顶部那段）。要在页面上彻底删掉，就到「已归档」那一档里点。
#
# 为什么**不能**叫 `archive/`：工作区根那个名字已经归「压缩前的文档」（而且标着只读），
# 两类东西挤进同一个名字之后，谁都会以为里面装的是同一回事。
#
# 与 OUTPUT_DIR 同一个算法（工作区根 + 目录名），而且只有这一处：画廊扫它、端点往它
# 里面搬、页面说「已归档」时指的必须是同一个目录 —— 散在几处拼，改一处就悄悄分家。
ARCHIVE_DIR = os.environ.get("QWEN21_ARCHIVE_DIR") or os.path.join(
    WORKSPACE, "output-archived")

# --- 一键启动：受限环境里的落点（见 AGENTS.md 铁律 2）-----------------------
# 实测（2026-10-01）：**工作台自己**跑在一个只能写工作区的环境里 —— 它的解释器在
# 工作区里，实测以 Low 完整性（S-1-16-4096）启动，派生的子进程继承它。同一台机器、
# 同一个账号，工作区**外面**的解释器写哪儿都行。于是它派生的 ComfyUI 写不了 %TEMP%
# （torch 要在那儿建编译缓存）、也写不了自己的安装目录（`user/` 与 `input/` 都在那儿）,
# 死在 `import torch` —— 而那段堆栈里最像原因的那一行**不是**原因。
#
# 所以受限时把这四处搬进工作区。这是**实测能起来**的那一套：24 秒就绪、四个模型
# 可见、rgthree 与 TE-Speed 节点都在、参考图上传 HTTP 200。
BACKEND_CACHE = os.environ.get("QWEN21_BACKEND_CACHE") or os.path.join(
    WORKSPACE, ".cache", "comfy-backend")


def confined_layout():
    """受限模式下 ComfyUI 的落点。

    全是工作区里的**绝对路径** —— 相对路径会被 ComfyUI 按自己的 cwd（安装目录）
    解析，而那里正好是写不进去的地方。
    """
    return {
        # Python 的 tempfile（torch 的编译缓存从这里取）
        "tmp": os.path.join(BACKEND_CACHE, "tmp"),
        # 单独给 torch 一个名字，缓存看得见、也好清
        "torch": os.path.join(BACKEND_CACHE, "torch"),
        # ComfyUI 自己的 temp（中间结果、预览）
        "comfy_temp": os.path.join(BACKEND_CACHE, "temp"),
        # 用户目录：设置、数据库、Manager 的配置与缓存、日志
        "user": os.path.join(BACKEND_CACHE, "user"),
        # 输入目录：参考图落在这里（页面上传走 ComfyUI 的 /upload/image）
        "input": os.path.join(BACKEND_CACHE, "input"),
    }


def find_instance_yaml():
    """Comfy Desktop 生成的模型路径配置。

    `inst-<id>` 每次重建都变，**不能写死**（AGENTS.md 铁律 1 的坑）。
    """
    try:
        hits = glob.glob(INSTANCE_YAML_GLOB)
    except OSError:
        return None
    return max(hits, key=os.path.getmtime) if hits else None


#: 缓存根。**必须由我们钉死位置**：`HF_HOME` / `TORCH_HOME` 默认都落在 `~` 下面，
#: 而 `~` / `%APPDATA%` / `%LOCALAPPDATA%` 全是**账户相对**的 —— 同一个后端，
#: 以 SYSTEM 起与以用户起看到的不是同一个目录（实测 2026-10-12）。
#:
#: 默认落在 `dirname(COMFY_ROOT)` 下面，也就是「这台机器这个实例」自己的地盘：
#: 出图主机上是 `Works\cache\`，与账户无关，两个账户都看得见。
CACHE_ROOT = os.environ.get("QWEN21_CACHE_DIR") or os.path.join(
    os.path.dirname(COMFY_ROOT), "cache")


#: 起后端时**必须**带上的环境变量，三处共用这一份：
#:   · `launch.child_env()` —— 从工作台那颗开关起的那个进程
#:   · `start_command()["display"]` —— 页面给用户复制的那条命令
#:   · 安装目录里那个 `start-comfyui.bat` —— **开机时那个计划任务**跑的就是它
#:
#: 前两处同源（都在这个文件里），第三处**不在本仓库**，所以只能**对照**：
#: `boot_script()` 读它、`/api/backend` 报出来、页面在「环境体检」里画一行。
#:
#: 少了变量的后果**没有症状**：页面说「后端在跑」，然后 See-through 那条链路
#: 退避 5 次之后整次跑死，报一个看不懂的错（插件里那个写死的
#: `frankjoshua/juggernautXL_version6Rundiffusion` 去够 `huggingface.co`，
#: 而那台机器连不上那里，实测 15 秒超时黑洞）。
#:
#: `HF_ENDPOINT` 是 `huggingface_hub` 认的变量，会把默认端点**整体换掉** ——
#: 不改插件源码、不做缓存手术、用的还是上游同一份文件。
#:
#: `HF_HOME` / `TORCH_HOME` 钉的是**位置**，管的是另一件事：默认值在 `~` 下，
#: 于是换个账户起同一个后端，HF 缓存与 torch 缓存**双双换了一套**。实测后果：
#: LaMa 检查点（插件里唯一一个写死完整 URL 的外部依赖，`HF_ENDPOINT` 救不了它）
#: 在 SYSTEM 眼里不存在 ⇒ 前后发**静默地**不拆，而体检还报「检查点不在」。
BACKEND_ENV = {
    "HF_ENDPOINT": "https://hf-mirror.com",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HOME": os.path.join(CACHE_ROOT, "hf"),
    "TORCH_HOME": os.path.join(CACHE_ROOT, "torch"),
}


def start_command():
    """「后端没起」时给页面复制的命令。与 AGENTS.md 铁律 1 逐字一致。

    刻意不用 MCP 的 `launch_comfyui`：它的 `--launch-extras` 按空格切分
    （`comfy-cli: command/launch.py:438`），而 yaml 路径含空格，必然断。

    **`display` 是给用户粘进 `cmd` 的，所以是一段 `.bat` 的正文，不是一行链。**
    实测 `cmd /c set "VAR=…" && cd /d … && python …` 那种一行链撑不住（后端起不来），
    所以环境变量**单独占一行**。`argv` 里没有它们的位置 —— 那是 `Popen` 的入参，
    环境走 `env=`（见 `launch.child_env()`）。
    """
    yaml = find_instance_yaml()
    shown = yaml or r"<APPDATA>\Comfy Desktop\instance-model-paths\inst-*.yaml"
    # 环境变量那几行从 `BACKEND_ENV` 现拼 —— 加一个变量不用记得改第二处。
    env_lines = "".join('set "%s=%s"\n' % (key, value)
                        for key, value in sorted(BACKEND_ENV.items()))
    return {
        "cwd": COMFY_ROOT,
        "argv": [COMFY_PYTHON, "-s", "main.py", "--enable-manager",
                 "--extra-model-paths-config", shown,
                 "--output-directory", OUTPUT_DIR,
                 "--port", str(COMFY_PORT)],
        "env": dict(BACKEND_ENV),
        "yaml_found": bool(yaml),
        "display": env_lines +
                   'cd /d "%s"\n"%s" -s main.py --enable-manager '
                   '--extra-model-paths-config "%s" '
                   '--output-directory "%s" --port %d'
                   % (COMFY_ROOT, COMFY_PYTHON, shown, OUTPUT_DIR, COMFY_PORT),
    }


#: 生产后端**不是**上面那条可复制命令起的：开机时「ComfyUI-Backend」计划任务
#: （`/RU SYSTEM`）跑的是安装目录里这个 `.bat`。它不在本仓库里，**没有版本保护
#: 兜底** —— 改了它不会有人告诉你，只能靠对照（`boot_script()`）。
BOOT_SCRIPT_NAME = "start-comfyui.bat"


def boot_script():
    """开机脚本与 `BACKEND_ENV` 的对照。**只读那个文件，绝不改它。**

    判据只有一条：`BACKEND_ENV` 里**每一个**变量都在里面、而且值一样。
    少了或不一样 ⇒ 开机起的后端与工作台起的后端跑在**两套环境**里，而页面
    会说「后端就绪」—— 那种坏法**没有症状**，所以必须有一条会说出来的判据。

    2026-10-12 实测就是这一条：出图主机重启之后，后端由开机任务以 SYSTEM 拉起，
    那条 `.bat` 里没有 `HF_ENDPOINT` ⇒ 拆层在 `huggingface.co` 上退避 5 次、
    GPU 0%、整次跑死（日志里 `huggingface.co` 命中 12 次、`hf-mirror.com` 0 次）。

    返回 `path` / `present` / `missing`（键名）/ `wrong`（`[{key,want,got}]`）/ `ok`。
    **`present=False` 不是错误**：那是「这台机器没有这条开机路径」，没有可对照的
    东西，页面那一行也不画。

    只认**行首就是 `set`** 的那一行（`REM set "…"` / `:: set "…"` 都不算）——
    注释掉一行等于没设，而「注释掉」正是最容易骗过肉眼的那种。
    """
    path = os.path.join(COMFY_ROOT, BOOT_SCRIPT_NAME)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return {"path": path, "present": False,
                "missing": sorted(BACKEND_ENV), "wrong": [], "ok": False}

    found = {}
    for line in text.splitlines():
        match = re.match(r'\s*set\s+"?([A-Za-z_][A-Za-z0-9_]*)=(.*?)"?\s*$', line)
        if match:
            found[match.group(1).upper()] = match.group(2)

    missing, wrong = [], []
    for key, value in sorted(BACKEND_ENV.items()):
        got = found.get(key.upper())
        if got is None:
            missing.append(key)
        elif got != value:
            wrong.append({"key": key, "want": value, "got": got})
    return {"path": path, "present": True, "missing": missing,
            "wrong": wrong, "ok": not missing and not wrong}


# --- 四个模型文件：铁律 1 的直接体检 ------------------------------------
# 缺任何一个都说明 `--extra-model-paths-config` 没生效（或 yaml 指到了别处）。
# 能在白跑一次出图之前发现 —— 这是这一期最省时间的一个检查。
REQUIRED_MODELS = (
    ("dit", "UNETLoader", "unet_name",
     "qwen_image_2.1_int8_convrot.safetensors"),
    ("text_encoder", "CLIPLoader", "clip_name",
     "qwen3vl_8b_int8_convrot.safetensors"),
    ("rewriter", "CLIPLoader", "clip_name",
     "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"),
    ("vae", "VAELoader", "vae_name",
     "qwen_image_2.1_vae_bf16.safetensors"),
)

# --- 可选插件的节点类 ----------------------------------------------------
# 与模型不同，**缺了不影响基准功能** —— 所以它只在该功能被勾上时才体检，
# 而且缺了是 error 级（提交必然失败），不是 warn。
REQUIRED_NODES = (
    ("te_speed", "TESpeedQwenImage21", "TE-Speed-QwenImage21"),
    ("upscale", "TESpeedVOSR2Image", "TE-Speed-VOSR2"),
)

# --- 放大模式的权重：靠**插件自己的候选表**判在不在 ------------------------
# `TESpeedVOSR2Loader.model_bundle` 的选项来自插件对 `models/vosr2/` 的扫描，
# 所以「候选表里有没有 VOSR2」就是「权重下没下」的判据 —— 与四个 Qwen 模型
# 用的是同一套办法（`combo_options`），不必自己去猜路径。
# 期望值不写在这里，取 `qwen21_build.UPSCALE_BUNDLE` —— 免得同一个字面量
# 在两个模块里各存一份、哪天改一个忘一个。
# **注意**：vosr2 的权重不在共享库（`extra_model_paths` 里没有 vosr2 这个键），
# 它落在**安装目录**的 `models/vosr2/VOSR2/`，所以别拿 REQUIRED_MODELS 去探。
UPSCALE_BUNDLE_NODE = ("TESpeedVOSR2Loader", "model_bundle")

# --- Turbo LoRA 的权重：与放大那条**同一个办法**，但它是**可选**的 --------------
# `LoraLoaderModelOnly.lora_name` 的候选表来自 ComfyUI 对 `models/loras/` 的扫描，
# 所以「候选表里有没有那个文件」就是「权重下没下」的判据 —— 不自己去猜路径。
# **为什么不写进 `REQUIRED_MODELS`**：那四个是基准功能的一部分，缺了整条链路都
# 出不了图；这一个缺了只影响 turbo 那条路（默认还关着），写进去会让**没下 LoRA
# 的人每次看体检都是琥珀色**。所以它由 `server._preflight_extras` 在勾上时
# 临时并进同一份清单 —— 形状与错误文案都复用那一套。
# 期望值不写在这里，取 `qwen21_build.TURBO_LORA_FILE`（同上一条的理由）。
TURBO_LORA_NODE = ("LoraLoaderModelOnly", "lora_name")

# --- 探针（P3）----------------------------------------------------------
PROBE_REPORT = os.environ.get("QWEN21_PROBE_REPORT") or os.path.join(
    WORKSPACE, "node_probe_report.json")
# 与 node_probe.py 的算法**保持一致**：状态文件放在**报告旁边**。
# 放进工作台的 state/ 是不行的 —— 探针跑在 ComfyUI 进程里，它不知道工作台的
# state 目录在哪；两边各写一份就永远读不到。
PROBE_STATE = os.environ.get("QWEN21_PROBE_STATE") or os.path.join(
    os.path.dirname(os.path.abspath(PROBE_REPORT)), "probe_state.json")

# --- 成本模型（全部来自实测）-------------------------------------------
SEC_PER_STEP_1MP = 2.50   # 1024²：实测 2.3~2.5 s/it
SEC_PER_STEP_4MP = 25.0   # 2048²：实测 ≈25 s/it（≈11 倍，token 数是 4 倍）
FIXED_OVERHEAD_S = 3.0    # 文本编码 + VAE 解码 + 存盘
COLD_LOAD_S = 12.0        # 模型不在显存时额外
REWRITER_S = 113.0        # switch=true：多加载 8.82 GiB 并改写提示词
DEFAULT_1MP = 1024

# --- 放大（VOSR2）的成本模型 —— 与上面的「s/步」**不是一回事** ------------
# VOSR2 是 one-step 模型，**没有步数这个旋钮**；它的代价**只随输出尺寸变**，
# 与输入尺寸几乎无关（实测 2×2 矩阵：512²→2048² 与 1024²→2048² 看不出差别、
# 2048²→4096² 与 1024²→4096² 一致；逐阶段数字见 qwen21_build 里那段注释）。
#
# 由两个锚点解出 `T = 4.71 × 输出MP^0.742`：
#   输出 2048²（4.194 MP）→ 实测 13.24 / 13.26 / 13.28 / 13.65 / 14.71 s
#   输出 4096²（16.777 MP）→ 实测 37.29 / 38.90 s
# 两点都落在 2% 以内 —— 但**只有两个输出尺寸的锚点**，所以 4096² 以上是外推。
# 名字仍叫「经验估算」；2026-10-01 起页面上不再显示（只剩 /api/build 与任务记录）。
UPSCALE_BASE_S = 4.71
UPSCALE_MP_EXPONENT = 0.742
UPSCALE_COLD_LOAD_S = 7.0   # TESpeedVOSR2Loader 实测 6963 ms（只有冷启动付）

# --- MiniMax H3 的成本模型 —— 又一个**单锚点** ---------------------------
# 2026-10-09/10 两次实测，同一份图（4 步 / 5 秒 / 480×864 / 9:16）：
#   最小路径（19 节点）310.31 秒 · 补齐辅助节点后（20 节点）316.9 秒
# 两个读数都**含模型加载与解码**（ComfyUI 自己那句 `Prompt executed in`）。
#
# **只有一个锚点**，所以既不拆「加载 / 采样 / 解码」，也不外推别的时长 ——
# 代价模型拿它当常数用，并在分解里带 `assumed` 让调用方如实说。10 秒那一档
# （243 帧 ≈ 1.96 倍帧数）**从没跑过**，帧数与时长的关系没量过，别编。
H3_SECONDS = 310.3
H3_STEPS = 4                # 蒸馏 LoRA 的固定档；它不是一个可调项，所以不进模型
H3_FRAMES = 124
H3_DURATION_S = 5.167

# --- See-through 拆层的成本模型 —— 两个锚点，随**面积**走 ----------------
# 2026-10-11 fp16 实测（30 步 / `resolution_depth=-1` / nf4）：
#   1024 → 252.40 秒（23 层）
#   1280 → 403.20 秒（24 层，多一个 tail）
# 面积比 (1280/1024)² = 1.5625，耗时比 403.20/252.40 = 1.597 —— 差 2.2%。
#
# 所以**不**直接套面积平方律（那会给出 394.4 秒，连自己的 1280 锚点都对不上），
# 而是像放大那一套（`UPSCALE_MP_EXPONENT`）一样从两点解一个幂律指数，在
# cost.py 里算出来。两个锚点因此都被**精确复现**，在这之间是插值。
LAYERS_ANCHOR_A = (1024, 252.40)
LAYERS_ANCHOR_B = (1280, 403.20)
LAYERS_BASE_RES = 1024
LAYERS_BASE_STEPS = 30

# **步数那一维没有锚点**（两次都是 30 步），所以 `steps != 30` 时那个线性因子是
# **外推**，不是实测 —— 调用方要在分解里带 `assumed`，页面上也要说出来。

# --- 反向词：CFG=1.0 时它**一个字都不生效** ------------------------------
# 这张图本来就接着 negative 通路（`TextEncodeQwenImage21` 的输出 slot 1 →
# `KSampler.negative`），**接线是通的**。问题在采样器：
# `comfy/samplers.py:610` 在 cfg≈1 且没人打开 `disable_cfg1_optimization` 时
# 直接 `uncond_ = None` —— 负向分支**根本不被求值**。这条图里没有那个开关，
# 而且**打开也没用**：cfg=1 时 `uncond + (cond-uncond)*1.0 ≡ cond`，
# 数学上自己抵消了。所以「让它生效」只能靠把 CFG 抬上去。
#
# 2026-09-30 实测（1024² / 12 步 / seed 20260930；仪器 `logs/neg_prompt_probe.py`，
# 报告 `logs/neg_prompt_probe_report*.json`）。同一提示词、同一 seed，只换
# 「反向词空 vs 反向词 144 字」：
#     cfg=1.0 → 逐字节相同，**0 / 4 194 304 字节**变化
#     cfg=2.0 → 76.7% 字节变化（mean_abs 15.25）
#     cfg=2.5 → 76.9% 字节变化（mean_abs 16.67）
#     对照：cfg 1.0 → 2.5（反向词都空）→ 74.3% 变化，证明 cfg 本身是活的
# 耗时是**第二条互不相干**的证据：cfg=1.0 是 29.6 / 30.4 s，cfg=2.0 是
# 56.1 / 56.7 s，cfg=2.5 是 57.7 / 57.7 s —— 几乎正好翻倍，因为那遍负向
# 求值现在真的跑了。两个仪器同一个结论。
#
# 所以反向词**不是一个可以留着不生效的旋钮**：填了却在 CFG≤1，它就是一个
# 没有症状的错误 —— 页面接受、记录留存、画廊回显，而像素一个没变。
# 门槛由**后端**判（`server.py::_warnings_for`），理由与成本预警完全一样：
# curl 直接打 /api/run 也挡得住，而且能用单测钉住，不靠「记得改前端」。
NEGATIVE_PROMPT_CFG_CEILING = 1.0
NEGATIVE_PROMPT_CFG_SUGGEST = 2.0
# 抬到 SUGGEST 的代价，页面上必须**连代价一起说**，否则那个按钮看着像白送的。
# 耗时是实测；画质那一句是**目视**判断（2.0 比 2.5 好得多：茎与木纹都还在，
# 2.5 那张基本糊平了；但皮肤仍比 CFG=1.0 平滑一些），别当成量出来的数。
NEGATIVE_PROMPT_SECONDS_AT_CEILING = (29.6, 30.4)
NEGATIVE_PROMPT_SECONDS_AT_SUGGEST = (56.1, 56.7)
