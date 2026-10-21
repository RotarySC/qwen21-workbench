# Qwen-Image-2.1 出图工作台

一个本地浏览器出图工作台，把 ComfyUI 上的五条出图链路包成一套统一界面：填参数、提交、
跟踪、看图、归档。

- **后端**：Starlette + uvicorn，单进程
- **前端**：零构建，单文件 `app.js`，没有打包步骤
- **队列**：用 ComfyUI 自己的 `POST /prompt`。工作台不维护队列，只负责构建工作流、
  跟踪任务、呈现产物

工作台与 ComfyUI **同机部署**，默认连 `127.0.0.1:8188`。前端不接 TLS 与 basic auth，
所以后端必须是 `http://` / `ws://` 直接可达的地址。

## 出图链路

五条链路彼此平级 —— 不是同一个下拉框里的五个选项 —— 各自有独立的参数与产物，
共用同一套任务账与画廊。

| 链路 | 输入 | 产物 | 依赖 |
|---|---|---|---|
| 文生图 | 提示词 | PNG | Qwen 四个权重 |
| 图生图 / 编辑 | 提示词 + 参考图（可多张） | PNG | 同上 |
| 放大超分 | 源图 + 倍数 | PNG | VOSR2 权重 + 插件 |
| 视频（MiniMax H3） | 首帧 + 提示词 + 时长 | MP4 | H3 权重 + 插件 |
| 拆层（See-through） | 源图 + 分辨率 | 每层一张 RGBA PNG + `_layers.json` | 拆层权重 + 插件（PSD 在浏览器端拼） |

文生图链路另有三个可选旋钮：提示词改写器、Turbo LoRA、缓存加速。

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11。出图与放大超分用的加速插件是 Windows 原生 `.pyd`，没有 Linux 构建 |
| GPU | NVIDIA。22 GB 显存可跑全部链路：Qwen 1024² 峰值约 18.9 GiB，H3 视频峰值 21.2 GiB |
| ComfyUI | ≥ 0.39 —— H3 依赖的几个节点是 0.39 才有的 |
| Python | 3.14（工作台自己的 venv，与 ComfyUI 的 venv 相互独立） |
| 磁盘 | 模型合计约 90 GiB，另留缓存空间 |

## 安装

### 1. ComfyUI、插件与模型

按 [docs/DEPLOY.md](docs/DEPLOY.md) 走。那份指南覆盖四个插件节点、五组权重的下载与落点、
拆层必须的 fp16 副本与 LaMa 检查点、环境变量，每一步都给了判据。

### 2. 工作台

解释器必须建在**仓库目录之外**：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" -m venv "$env:LOCALAPPDATA\qwen21-venv"
& "$env:LOCALAPPDATA\qwen21-venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
```

`requirements-lock.txt` 是实测跑通的那一组完整版本，装它。`requirements.txt` 是同一批
依赖的分组说明，用来理解「为什么是这些包」。

### 3. 配置

至少设两个环境变量：

```powershell
$env:QWEN21_COMFY_ROOT = "<ComfyUI 安装目录>"
$env:QWEN21_OUTPUT_DIR = "<产物目录>"
```

`QWEN21_COMFY_ROOT` 不设时按 Comfy Desktop 的默认实例路径找；`QWEN21_OUTPUT_DIR`
不设时画廊是空的 —— ComfyUI 写的是安装目录下的 `output/`。完整配置项见
[docs/DEPLOY.md](docs/DEPLOY.md) 的「环境变量」。

### 4. 启动

```powershell
# 在仓库根目录下执行（`-m` 从当前目录找包）
& "$env:LOCALAPPDATA\qwen21-venv\Scripts\python.exe" -m workbench.server --port 8189
# → http://127.0.0.1:8189
```

Windows 上也可以直接双击 `run.bat`（可选带一个端口参数）。它按
`QWEN21_PYTHON` → `%LOCALAPPDATA%\qwen21-venv` → `<工作区>\.venv` 的顺序找解释器。

## 使用

页面右上角那颗开关用于起停后端，只关自己起的那个。后端没起时，日志台会给出那条
可直接复制的启动命令。

页面打不开、或显示「后端未运行」时，先看**日志台 → 环境体检**：它会报出解释器、
状态目录、后端三态、Qwen 的四个模型文件在不在、探针，以及启动命令。

## 目录结构

```
workbench/            后端
  server.py             路由、参数校验、提交前体检
  comfy.py              ←→ ComfyUI HTTP
  watcher.py            WebSocket + /history 轮询 → 状态与 SSE
  jobs.py               任务账（state/jobs/*.json + runs.jsonl）
  gallery.py            画廊、归档、取图
  launch.py             起停后端
  llm.py                提示词增强
  thumbs.py             缩略图缓存
  artifacts.py          视频与层集的产物识别
  static/               前端：app.js、index.html、style.css、psd.js（层集拼 PSD）
qwen21_build.py       文生图 / 图生图 / 放大 三条链路的构建器
h3_build.py            MiniMax H3 视频的构建器
layers_build.py        See-through 拆层的构建器
project_paths.py       基准工作流图的落点规则
workflows/             基准工作流图（每条链路长什么样的定义）
tests/                 单测 + 真 headless Chromium 的 page_shot.py + 假 DOM 探针
```

`workflows/` 里那三张图定义了每条链路的节点、接线与默认参数，构建器只往里填参数。
落点规则只有一处（`project_paths.py`）：环境变量 → 仓库自带的 `workflows/` → 工作区。
工作区那一档是为「图放别处、仓库只放代码」的部署留的回落。

运行时的东西不在仓库里：任务账与缩略图缓存在 `state/`（默认在仓库下，可用
`QWEN21_STATE` 改），产物在 `QWEN21_OUTPUT_DIR`。

## 测试

```powershell
# 在仓库根目录下执行

# 全量单测：不需要后端，也不需要 GPU
& "$env:LOCALAPPDATA\qwen21-venv\Scripts\python.exe" -m unittest discover -s tests -t tests

# 真浏览器：渲染 + 驱动 + 截图 + 量测
# 前提：headless Chromium 在盘上，且工作台已经在 127.0.0.1:8189 跑着（见脚本头部注释）
& "$env:LOCALAPPDATA\qwen21-venv\Scripts\python.exe" tests/page_shot.py --out <截图目录>
```

判据分四层：契约与一致性（unittest）、长相（真 headless Chromium + CDP）、
假 DOM 探针（表单逻辑与视图切换）、活体冒烟（`tests/smoke_*.py`，要真后端与 GPU）。
两层分工不能互相替代：截图管长相，契约判据管一致性，而截图必须用眼睛看。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | 从零部署：ComfyUI、插件、模型、环境变量、验收步骤、容易静默失败的清单 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层与数据流、各模块职责、状态落点、前端的几个刻意选择 |

## 已知限制

- 只在 **Windows + 单机 ComfyUI** 上验证过。前端不接 TLS 与 basic auth，后端必须是
  同机（或隧道）可达的 `http://` / `ws://` 地址。
- **解释器必须建在仓库目录之外**。若仓库所在目录带 Low 完整性标签，在其中创建的
  解释器会继承该标签，于是写不了 `%TEMP%` 与 ComfyUI 安装目录，一键启动只能退到
  受限模式：模型能读、出图能出，但模型下载与节点安装用不了。拦人的是强制标签，
  不是权限位。
- 缓存位置（`HF_HOME` / `TORCH_HOME`）刻意钉在仓库之外的一处，而不是用户主目录下。
  同一台机器上换账户启动同一个后端时，用户主目录指向的不是同一个位置，表现为
  「模型明明下过，它却去联网重下」。
- 部分链路依赖外部插件，缺失时不是报错而是**静默退化**（例如拆层的 LaMa 检查点缺了，
  只是不拆前后发）。这些逐条写在 [docs/DEPLOY.md](docs/DEPLOY.md) 里。
- 部署完请跑一遍 [docs/DEPLOY.md](docs/DEPLOY.md) 的「验收」一节，不要只看服务起没起来。

## 第三方组件

本仓库不打包任何插件或模型权重 —— 它们按 [docs/DEPLOY.md](docs/DEPLOY.md) 的指引从
上游单独下载，许可归各自上游。两点需要留意：

- `custom_nodes/` 下两个 TE-Speed 插件是**闭源 Windows 二进制**（`.pyd`），上游仓库里
  没有 LICENSE 文件，使用前请自行确认许可。它们也是本项目「Windows only」的原因。
- `workbench/static/ag-psd.bundle.js` 是上游 [ag-psd](https://github.com/Agamnentzar/ag-psd)
  的打包产物，用于在浏览器端拼 PSD；许可归上游。
