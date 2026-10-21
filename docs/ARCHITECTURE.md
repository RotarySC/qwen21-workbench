# 架构与数据流

## 三层，一条边界

```
浏览器 ──SSE + HTTP──▶ 工作台 (:8189) ──HTTP + WS──▶ ComfyUI (:8188)
```

**队列不归工作台管** —— 用 ComfyUI 自己的。`POST /prompt` 天然排队，所以工作台里没有
worker，只有「跟踪 + 落盘 + 广播」。这一条决定了很多细节：工作台重启之后还能靠
`/queue` 与 `/history` 把在跑的任务认回来。

## 后端的几块

| 模块 | 职责 | 值得先知道的一点 |
|---|---|---|
| `server.py` | 路由、参数校验、提交前体检 | 体检文案与结构化字段出自**同一个函数**，页面只画 |
| `comfy.py` | ←→ ComfyUI HTTP | 三态探活：**连接被拒**才算没起；「没答好」也算在跑 |
| `watcher.py` | WS 读线程 + `/history` 轮询 | 收尾**只有一条路径**（见下） |
| `jobs.py` | 任务账（`state/jobs/*.json` + `runs.jsonl`） | 产物状态**现算在响应里**，绝不写进账 |
| `gallery.py` | 画廊、归档、取图 | 取图与删除**共用**同一份路径校验 |
| `launch.py` | 起停后端（一键启动 / 顶栏开关） | 启动 argv 的唯一真源是 `config.start_command()` |
| `llm.py` | 提示词增强 | 提交前的一次 `chat/completions`，不是提交时静默生效的开关 |
| `thumbs.py` | 缩略图缓存 | 缓存键含 mtime；原子落盘；解不开就发原图 |
| `artifacts.py` | 视频 / 层集的产物识别 | 按「任务号前 8 位」这个 token 认，不靠文件名开头 |

### 为什么收尾只有一条路径

`client_id` 归**最后提交者** —— 谁最后提交，谁就收到所有任务的事件。所以工作台不能
把「收尾」挂在 WebSocket 上：多客户端（或工作台重启）时事件会送到别人那里。
于是 `watcher.py::_finish_from_history` 是唯一收尾点，WS 只是**提前触发**它，
1.5 秒的轮询兜底。自动放大那条链也挂在这条路径上（收尾时喊一声注入的回调）。

### 状态落在哪

```
state/
  jobs/<job_id>.json   一条任务（参数、状态、产物名、耗时）
  runs.jsonl           只追加的流水
  prompts.json         提示词库
  thumbs/              缩略图缓存（收尾按预算清理，刚写的那份绝不删）
  backend.log          一键启动时把后端 stdout 抄一份
```

产物**不在这里** —— 在 ComfyUI 的 `--output-directory` 指的目录。画廊扫的是一组
候选根（当前进程启动参数 → 安装目录 → 推算兜底 → 桌面版配置 → 工作区），
逐张标注来源并按路径去重。

## 五条链路各自一个构建器

| 构建器 | 链路 | 基准图 |
|---|---|---|
| `qwen21_build.py` | 文生图 · 图生图 · 放大 | `workflows/qwen21_api_base.json` |
| `h3_build.py` | MiniMax H3 视频 | `workflows/h3_api_base.json` |
| `layers_build.py` | See-through 拆层 | `workflows/seethrough_api_base.json` |

三者是**刻意平级**的（互不 import）：它们各自知道自己的节点 id、默认值与产物命名，
共用的只有「基准图在哪」这一条规则（`project_paths.py`）。

**为什么「永远提交修好断边的图」**：`comfy-cli` 把 UI 格式转成 API 格式时会丢掉
「子图输入节点 → 纯 link 型输入」的边（本项目的模板里丢的正是承载提示词的
`ComfySwitchNode.on_false`）。后果是提示词从未进文本编码器 —— **输出只由 seed 决定**，
而 `validate_workflow` 照样报 `valid: true`。所以基准图是**手工修好**的那一份，
构建器只往里填参数。

## 前端

`workbench/static/app.js` 是**单文件、零构建**的。几个刻意的选择：

- **静态文件 `no-cache`，HTML `no-store`** —— 否则会长出「新 HTML + 旧 app.js」
  那种两半不同版的组合。
- 页面上有一小段**内联守卫**（排在 `app.js` 之前）：脚本没跑起来时自己画一条横幅说
  出来，而不是让用户对着一个「连接中…」永远不变的页面。
- **可见文字里不出现数字**（估时、耗时预估那类）：它们对「这台机器此刻有多快」不敏感，
  一个偏大的数挨着真实耗时，看起来只是工作台算错了。
- 隐藏一个整页视图**只用 `hidden` 属性**，不要又挂 `.hidden` 类（两条 CSS 都吃，
  而 `el.hidden = false` 只摘属性 ⇒ 那个视图永远不可见，DOM 里内容一行不少）。

## 判据怎么分层

| 层 | 工具 | 管什么 |
|---|---|---|
| 契约 / 一致性 | `tests/test_api.py` 等（unittest，~1300 项） | 路由、校验、文案、前端契约 |
| 长相 | `tests/page_shot.py`（真 headless Chromium + CDP） | 渲染 + 驱动 + 截图 + 量测 |
| 假 DOM | `tests/form_probe.js` / `views_probe.js` | 表单逻辑与视图切换（比真浏览器快） |
| 活体 | `tests/smoke_*.py` | 真后端、真出图（要 GPU） |

截图**必须用眼睛看**；契约判据**不能**被截图替掉 —— 两者分工是「长相 vs 一致性」。
