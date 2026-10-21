/* Qwen-Image-2.1 工作台前端 —— 零构建，无依赖。
 *
 * 两条设计红线：
 *  1. 提交后**立刻**本地置为 queued 再渲染，不等 SSE。判据 1 要求点下去
 *     一秒内就有反馈；等第一个事件回来会有一个肉眼可见的空档。
 *  2. SSE 只做增量。真正的权威状态是服务端的 `snapshot`（连接时必发）
 *     和 `/api/jobs`。所以刷新页面永远不会丢任务 —— 刷新等于重连，
 *     重连等于「重放 + 快照」。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const TERMINAL = new Set(["done", "error", "cancelled"]);

const state = {
  resultTab: "live",      // 视图状态独立于任务：SSE 更新不切走用户正在浏览的画廊。
  // P21c：当前选中的**链路**（`qwen` | `h3` | `layers`）。
  //
  // 页面上是两级的（先选链路，再在 Qwen 那一档里选形状），但**线路上只有一个
  // `mode` 字段** —— 这个值只决定「去哪儿取 mode」：Qwen 那一档取 `#mode`
  // 下拉框，另两档 mode 就是它自己。刻意不加一个平行的 `chain` 请求字段，
  // 那会让「哪条链路」有两份真源。唯一的反向映射是 `setChainForMode()`。
  chain: "qwen",
  jobs: new Map(),
  backend: null,
  currentId: null,
  queueRemaining: null,
  external: null,
  build: null,
  // P2
  gallery: null,          // /api/gallery 的整份回执（含 counts 与 roots）
  galleryMode: "",        // "" | t2i | refine | edit | 2k | __foreign
  galleryQuery: "",
  // P20f：分页与多选
  galleryPage: 0,         // 0 起的页码（请求时换算成 offset）
  galleryCols: null,      // 上一次**实测量**到的列数（量不到就是 null，见 galleryCols()）
  galleryPick: new Set(), // 本页勾中的 id（**活不过一次刷新**，见 fetchGallery）
  galleryCanDelete: null, // 后端说这台机器现在能不能删（`can_delete`）
  galleryDeleteHint: "",  // 不能删时那句话（`delete_hint`）
  galleryFits: 0,         // 布局校正重取过几次（封顶 3，见 scheduleGalleryFit）
  // P20h：**这是哪一档**。「已归档」不是又一颗筛选 chip —— 筛选是在扫出来的那一堆
  // 里挑，而这一档换的是**扫哪个目录**（服务端换一个根），所以它得是状态，不是参数。
  galleryArchived: false,
  library: null,          // /api/prompts 的整份回执
  libraryQuery: "",
  // P20j：最近任务的「产物还在不在」与那颗「清理」
  jobsStale: 0,           // 后端现算：有多少条记录已经**永远回不来**（按钮上的数字）
  jobsCleanNote: "",      // 上一次清理的回执（一句话；刷新就没了，所以同时进日志台）
  // P3
  timings: new Map(),     // job_id -> /api/jobs/{id}/timings 的回执
  timingsPending: new Set(),
  // P4
  inputs: null,           // /api/inputs 的整份回执（后端真正能读到的参考图）
  refs: [],               // 已选参考图/源图的名字（**唯一真源**，见 renderPicked）
  confirmArmed: false,    // 成本预警：贵的那档要点两下
  // P5
  sweeps: new Map(),      // sweep_id -> /api/sweep/{id} 的回执
  sweepId: null,          // 当前正在看的那一次扫描
  // P10
  lightboxId: null,       // 放大视图正在看的那张（画廊项的 id，不是数组下标）
  lightbox1to1: false,    // 原始像素 / 适应窗口
  lightboxMoved: 0,       // 本次按住拖了多远（用来把「拖动」与「点一下」分开）
  // P20：日志台
  logs: {
    cursor: 0,            // `/api/logs` 的游标（服务端环的 seq，不是事件 id）
    epoch: null,          // 服务进程标识：变了就是重启过，手里的旧行全部作废
    rows: [],             // 已收到的时间线（事件 + 本进程的日志记录）
    workbench: [],        // 「工作台日志」那一栏的尾部
    backend: null,        // 后端日志那一块的回执（含全部候选来源）
    rendered: 0,          // 已经画进 DOM 的行数（只追加、不整块重画）
    filteredAt: null,     // 上次画的时候过滤器是什么（变了才重画）
    follow: true,
    at: null,
    timer: null,
  },
  // P20：LLM 增强的设置。**里面没有密钥明文** —— 只有 has_key 与掩码。
  llm: null,
};

/* ---------- 小工具 ---------- */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function intOrNull(v) {
  if (v === "" || v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}
function floatOrNull(v) {
  if (v === "" || v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function fmtS(sec) {
  sec = Math.round(Number(sec) || 0);
  if (sec < 60) return sec + " 秒";
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + " 分 " + String(s).padStart(2, "0") + " 秒";
  return Math.floor(m / 60) + " 小时 " + String(m % 60).padStart(2, "0") + " 分";
}
function fmtElapsed(ms) {
  return fmtS(Math.max(0, ms) / 1000);
}

async function api(path, opts) {
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new Error("连不上工作台自己（" + e.message + "）");
  }
  let data = null;
  try { data = await r.json(); } catch (e) { /* 可能是空体 */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || ("HTTP " + r.status));
    err.payload = data;
    throw err;
  }
  return data;
}
const postJSON = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const getJSON = (path) => api(path, {});

/* ---------- 链路与形状 ----------
 *
 * 页面上是**两级**：先选链路（三颗平级的分段按钮），再在 Qwen 那一档里选形状。
 * 线路上是**一级**：只有一个 `mode` 字段，五个合法值。
 *
 * 为什么不做成「一个下拉里五项」：那个下拉框（`#mode`）装的是**同一条链路的三种
 * 形状**（文生图 / 图生图 / 放大），而 H3 与 See-through 是另外两条链路 ——
 * 模型不同、图不同、产物也不同。混在一起会让「模式」继续当杂物箱，
 * 而那正是 2026-10-01 撤掉 `2k` / `refine` 时要治的病。
 */
const CHAINS = ["qwen", "h3", "layers"];

/* 链路 → 角标上那行字。`panel-tag` 现在**随链路变**：页面上写着
 * QWEN-IMAGE-2.1 而跑出来的是 H3 的视频，就是一句假话。 */
const CHAIN_TAG = {
  qwen: "QWEN-IMAGE-2.1",
  h3: "MINIMAX-H3",
  layers: "SEE-THROUGH",
};

/* 链路 → 提交按钮上的字。**只有 `renderRunButton()` 一个写者** ——
 * `flashButton()` 从不作用在 `#btn-run` 上（已核），所以改这里的文案不会踩
 * 「它把第一次调用时那版文字当成原样」那个尖角。 */
const CHAIN_RUN_LABEL = { qwen: "出图", h3: "出视频", layers: "开始拆层" };

/* 哪个 `#mode` 值属于哪条链路 —— **反向映射只此一处**（「载入参数」与
 * 「最近任务」回填都走它）。Qwen 那三档都落在 `qwen`。 */
function setChainForMode(mode, apply = true) {
  const chain = CHAINS.includes(mode) ? mode : "qwen";
  setChain(chain, apply);
  return chain;
}

function setChain(chain, apply = true) {
  if (!CHAINS.includes(chain)) return;
  state.chain = chain;
  for (const name of CHAINS) {
    const button = $("chain-" + name);
    if (!button) continue;
    const on = name === chain;
    button.setAttribute("aria-checked", String(on));
    button.tabIndex = on ? 0 : -1;
  }
  $("chain-tag").textContent = CHAIN_TAG[chain] || "";
  if (apply) applyModeUI();
  placeChainThumb();
}

/* 滑块：与 `placeThumb` 同一套（量 offsetLeft/offsetWidth，不用
 * getBoundingClientRect —— 后者含 transform，而滑块自己就是被 transform 移动的，
 * 用它会让下一次测量读到上一次的位置）。 */
function placeChainThumb() {
  const button = $("chain-" + state.chain);
  const thumb = $("chain-thumb");
  if (!button || !thumb) return;
  thumb.style.transform = "translateX(" + button.offsetLeft + "px)";
  thumb.style.width = button.offsetWidth + "px";
}

function initChainTabs() {
  for (const name of CHAINS) {
    const button = $("chain-" + name);
    if (!button) continue;
    button.onclick = () => selectChain(name);
    button.addEventListener("keydown", (event) => {
      const index = CHAINS.indexOf(name);
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % CHAINS.length;
      else if (event.key === "ArrowLeft") next = (index + CHAINS.length - 1) % CHAINS.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = CHAINS.length - 1;
      else return;
      event.preventDefault();
      selectChain(CHAINS[next]);
      $("chain-" + CHAINS[next]).focus();
    });
  }
  setChain(state.chain);
  window.addEventListener("resize", () => placeChainThumb());
}

/* 点一颗链路按钮：换档 + 走同一条「表单变了」的路。
 * `formChanged` 要到文件后段才定义 —— 它是个**函数声明**，会提升，所以在
 * 点击（用户交互）那一刻它早就在了。 */
function selectChain(name) {
  if (name === state.chain) return;
  setChain(name);
  formChanged();
}

/* 成果视图（只切换显示，保留输入、筛选与滚动位置） */
const RESULT_TABS = ["live", "gallery", "jobs"];

/* 滑块位置：量出当前标签在 .result-tabs 里的位置与宽度平移过去。
 *
 * 用 offsetLeft/offsetWidth（布局值）而**不是** getBoundingClientRect：
 * 后者含 transform，而滑块自己就是被 transform 移动的 —— 用它会让下一次
 * 测量读到上一次的位置，误差还会累积。
 * 标签宽度随断点变（`@media (max-width:639px)` 里 `.result-tab{flex:1}`），
 * 所以 resize 时必须重量一次，否则滑块会停在旧位置上。
 */
function placeThumb(tab) {
  const button = $("tab-" + tab);
  $("result-thumb").style.transform = "translateX(" + button.offsetLeft + "px)";
  $("result-thumb").style.width = button.offsetWidth + "px";
}

function setResultTab(tab, focus = false) {
  if (!RESULT_TABS.includes(tab)) return;
  const from = RESULT_TABS.indexOf(state.resultTab);
  const to = RESULT_TABS.indexOf(tab);
  state.resultTab = tab;
  for (const name of RESULT_TABS) {
    const selected = name === tab;
    const button = $("tab-" + name);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    const panel = $("view-" + name);
    panel.hidden = !selected;
    // `hidden` 是 display:none，transition 抓不到"刚显示"那一瞬，所以用
    // animation（每次显示都会重放）。先删掉再强制读一次 offsetWidth，
    // 否则切到**同一个方向**时属性没变、动画不会重放。
    // 首次进入不放动画：`from === to` 时什么都不做，免得页面一打开就滑一下。
    if (selected && from !== to) {
      delete panel.dataset.slide;
      void panel.offsetWidth;
      panel.dataset.slide = to < from ? "left" : "right";
    } else {
      delete panel.dataset.slide;
    }
  }
  placeThumb(tab);
  // 画廊的页大小取自**实际列数**，而面板藏着的那些格子里每张的 offsetTop 都是 0
  // —— 所以一显示出来就要重量一次（第一次量不到就沿用兜底值，这里把它纠正过来）。
  if (tab === "gallery") remeasureGallery();
  if (focus) $("tab-" + tab).focus();
}

function initResultTabs() {
  for (const name of RESULT_TABS) {
    const button = $("tab-" + name);
    button.onclick = () => setResultTab(name);
    button.addEventListener("keydown", (event) => {
      const index = RESULT_TABS.indexOf(name);
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % RESULT_TABS.length;
      else if (event.key === "ArrowLeft") next = (index + RESULT_TABS.length - 1) % RESULT_TABS.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = RESULT_TABS.length - 1;
      else return;
      event.preventDefault();
      setResultTab(RESULT_TABS[next], true);
    });
  }
  setResultTab(state.resultTab);
  // 标签宽度与字体都会随窗口变，滑块得跟着走。
  window.addEventListener("resize", () => placeThumb(state.resultTab));
}

/* ---------- 表单 ---------- */
/* 模式标签（2026-10-01 重构后只剩三个形状）。
   `refine` / `2k` 留在表里是**给老记录用的兜底**：任务记录与画廊扫的是历史，
   那些值还躺在磁盘上，删掉它们等于让老记录显示成一串裸标识符。新记录不会再产生
   这两个值（后端也会拒），所以带上「旧」字，一眼看得出它不是现在的说法。 */
const MODE_LABEL = { t2i: "T2I", edit: "Edit", upscale: "放大",
                     h3: "H3 视频", layers: "拆层",
                     refine: "Refine（旧）", "2k": "2K（旧）" };

/* 老记录里的模式值要被**翻译**过来再填进表单。
   不翻译的症状很轻但很坏：`$("mode").value = "2k"` 在一个没有这个选项的
   select 上会**静默变成空串** —— 页面看着只是「模式没选」，而表单已经在
   一个未定义状态里了。 */
const LEGACY_FORM_MODE = { "2k": "t2i", refine: "t2i" };

function collectBody() {
  // ★ **有效模式**：Qwen 那一档取 `#mode` 下拉框，另两条链路 `mode` 就是链路本身。
  // 线路上只有一个 `mode` 字段（五个合法值），页面的两级只体现在这一行里。
  const mode = state.chain === "qwen" ? $("mode").value : state.chain;
  // 放大（VOSR2）与两条新链路都是**另一个模型、另一张图**：出图专用那些字段
  // 一律发 null/false，而不是把出图那套原样带上 —— 后端会忽略它们，但记录里
  // 留下一堆假的参数，日后回看会以为它们当时起过作用。
  const upscale = mode === "upscale";
  const h3 = mode === "h3";
  const layers = mode === "layers";
  // 「出图那两条」（t2i / edit）—— 只有它们用尺寸、CFG、采样器、三个加速开关。
  const qwenDraw = mode === "t2i" || mode === "edit";
  // 提示词：Qwen 那两条要，H3 要（图里那个 `MiniMaxH3ImageToVideo` 吃 prompt），
  // 放大与拆层没有文本输入。
  const wantsPrompt = qwenDraw || h3;
  // 步数：只有拆层复用出图那一格（它有自己的 `num_inference_steps`）。
  const wantsSteps = qwenDraw || layers;

  let width = null, height = null;
  if (qwenDraw) {
    const size = $("size").value;
    if (size === "custom") {
      width = intOrNull($("width").value);
      height = intOrNull($("height").value);
    } else {
      const parts = size.split("x");
      width = Number(parts[0]);
      height = Number(parts[1]);
    }
    // 2K 那个模式已经撤掉：想要 2048² 就在「尺寸」里选它（那一档还在）。
  }
  // 参考图：一张与多张是**同一件事**的两种写法，所以两个键都发，而且恒等
  // （`source_image` 就是 `source_images[0]`）。后端也会核这一点，不一致就
  // 当场 400 —— 同一件事有两份真源，迟早会分家。
  // 放大 / H3 / 拆层**都只吃一张**（那三条图里各只有一个 `LoadImage`）：
  // 这里就只发第一张，页面上那句「只用第一张」正是说的这件事。
  const refs = state.refs.slice();
  const single = upscale || h3 || layers;
  const picked = single ? refs.slice(0, 1) : refs;
  // 键集**恒定**（用不上的那几个是 null）—— 任务记录、画廊项、表单回显三处
  // 共用这一份形状，「键缺失」和「键是 null」在前端是两种待遇。
  return {
    mode: mode,
    prompt: wantsPrompt ? $("prompt").value : "",
    negative_prompt: qwenDraw ? $("negative").value : "",
    steps: wantsSteps ? intOrNull($("steps").value) : null,
    width: width,
    height: height,
    seed: intOrNull($("seed").value),
    cfg: qwenDraw ? floatOrNull($("cfg").value) : null,
    sampler: qwenDraw ? $("sampler").value : null,
    scheduler: qwenDraw ? $("scheduler").value : null,
    source_image: picked[0] || null,
    source_images: picked,
    // 分辨率：图生图画参考图缩放到的边长，拆层是它自己的拆层分辨率。
    resolution: mode === "edit" ? 1024
      : (layers ? intOrNull($("layers-resolution").value) : null),
    scale: upscale ? intOrNull($("scale").value) : null,
    // H3 的时长（秒）。**那个数是近似的** —— 基准图里有一条公式把它吸附到
    // 17 帧网格上，真实帧数由后端反解回来（`describe.frames`），页面照实显示。
    seconds: h3 ? intOrNull($("h3-seconds").value) : null,
    // 拆层的深度分辨率（`-1` = 跟随上面的分辨率）。
    resolution_depth: layers ? intOrNull($("layers-depth").value) : null,
    lazy_cache: qwenDraw ? $("lazy_cache").checked : false,
    te_speed: qwenDraw ? $("te_speed").checked : false,
    // Turbo：**与上面两个不互斥** —— 它是权重补丁，插在那两个加速节点之前，
    // 所以这里没有「勾一个弹掉另一个」那套（那条只属于 lazy_cache / te_speed）。
    turbo: qwenDraw ? $("turbo").checked : false,
    // 提示词改写器：**额外功能**，不再是模式（2026-10-01 起）。
    // 只有文生图有它 —— 图生图那条链路把改写器节点整段删掉了，放大与拆层
    // 根本没有文本编码器，H3 的图里也没有那个节点。
    rewrite: (mode === "t2i") && $("rewrite").checked,
    // 出图后自动放大（串联）：放大自己就是那条链，H3 出的是视频、拆层出的是
    // 几十张层图，三者在后端都会被 400 挡掉（见 `_auto_upscale`）。这里先发
    // false，说实话，免得记录里留一个假参数。
    auto_upscale: {
      enabled: qwenDraw && $("auto_upscale").checked,
      scale: intOrNull($("auto_scale").value),
    },
  };
}

function applyModeUI() {
  // 与 `collectBody` 同一条算法：**有效模式**由链路决定（线路上只有一个 `mode`）。
  const qwen = state.chain === "qwen";
  const mode = qwen ? $("mode").value : state.chain;
  const upscale = mode === "upscale";
  const h3 = mode === "h3";
  const layers = mode === "layers";
  const qwenDraw = mode === "t2i" || mode === "edit";
  const wantsPrompt = qwenDraw || h3;
  const wantsSteps = qwenDraw || layers;
  const single = upscale || h3 || layers;
  // 2K 不再是一个模式（分辨率就在「尺寸」里调），所以这里没有任何 2k 特判。

  // 参考图 / 源图：图生图、放大、H3（首帧）、拆层都要它，四条共用同一个字段
  // （同一个上传通道）。
  const needsSource = (mode === "edit" || upscale || h3 || layers);
  $("src-wrap").classList.toggle("hidden", !needsSource);
  // 标签就几个字：下面那个虚线框自己会说「点击选择，或把图片拖到这里」，
  // 标签再复述一遍只是噪音。
  $("src-label").textContent = upscale ? "源图"
    : (h3 ? "首帧图" : (layers ? "源图" : "参考图"));
  // 放大**只吃一张**（VOSR2 那个节点就一个 images 输入），而图生图能挂多张。
  // 两处都说清楚，否则用户会以为拖两张进去放大就会「两张合成一张」。
  // 「多次选择会累积」也得说 —— 上传**不再**替换上一次的那批（按用户要求改的），
  // 而一个不说的累积行为会让人以为第一次选的那张丢了。
  $("ref-drop-note").textContent = single
    ? "点击选择，或把图片拖到这里（只用第一张）"
    : "点击选择（可多选），或把图拖到这里 —— 文件夹里的、右边画廊里的都行；多次选择会累积";

  // ★ 可见性**矩阵**：逐条列出来，而不是靠那么一两个布尔反推。
  // 反推的坏法很轻但很坏 —— 某个控件留在页面上、看着能调、实际不生效，
  // 那正是本工作区最忌讳的「没有症状的错」。
  //
  // | 字段块            | Qwen t2i/edit | Qwen 放大 | H3 | 拆层 |
  // |-------------------|---------------|-----------|----|------|
  // | 模式下拉          | ✅            | ✅        | —  | —    |
  // | 参考图 / 源图     | 仅 edit       | ✅        | ✅ | ✅   |
  // | 提示词 / 增强     | ✅            | —         | ✅ | —    |
  // | 反向词 / 提示词库 | ✅            | —         | —  | —    |
  // | 步数              | ✅            | —         | —  | ✅   |
  // | 尺寸 / CFG        | ✅            | —         | —  | —    |
  // | 高级设置          | ✅            | —         | —  | —    |
  // | 放大倍数          | 仅 upscale    | ✅        | —  | —    |
  // | H3 时长           | —             | —         | ✅ | —    |
  // | 拆层分辨率        | —             | —         | —  | ✅   |
  // | 扫描 steps 轴     | ✅            | —         | —  | ✅   |
  $("mode-wrap").classList.toggle("hidden", !qwen);
  $("prompt-wrap").classList.toggle("hidden", !wantsPrompt);
  // 增强那颗按钮在 `<label>` **外面**（在里面点它会被 label 连带激活 textarea），
  // 所以它不跟着 prompt-wrap 一起藏 —— 得单独说一句。不说的话，拆层模式下
  // 会留着一颗按钮去增强一个**不在页面上**的输入框。
  $("prompt-actions").classList.toggle("hidden", !wantsPrompt);
  $("negative-wrap").classList.toggle("hidden", !qwenDraw);
  $("library").classList.toggle("hidden", !qwenDraw);
  // 步数与尺寸**分开判**：拆层要步数但不要尺寸（它没有「输出尺寸」这个概念），
  // 而 H3 两样都不要。整行一起藏会让拆层连步数一起没了 ——
  // 那是个不报错、只是少一格控件的坏法。
  $("gen-row").classList.toggle("hidden", !wantsSteps);
  $("steps-wrap").classList.toggle("hidden", !wantsSteps);
  $("size-wrap").classList.toggle("hidden", !qwenDraw);
  $("cfg-wrap").classList.toggle("hidden", !qwenDraw);
  $("advanced-wrap").classList.toggle("hidden", !qwenDraw);
  // 改写器只在文生图下有意义（图生图那条链路把它整段删掉了）。**藏起来**而不是
  // 留一个勾了没反应的开关 —— 后者正是本工作区最忌讳的「没有症状的错」。
  $("rewrite-wrap").classList.toggle("hidden", mode !== "t2i");
  // 「出图后自动放大」：放大**自己**就是那条链；H3 出的是视频、拆层出的是几十张
  // 层图 —— 三者后端都会拒（见 `_auto_upscale`），所以这里就藏起来。
  $("chain-wrap").classList.toggle("hidden", !qwenDraw);
  renderSweepChainNote();
  // 「载入提示词」那颗按钮的可用性也跟着模式走（拆层下没有提示词输入框）
  syncPromptLoadButtons();
  $("upscale-wrap").classList.toggle("hidden", !upscale);
  $("h3-wrap").classList.toggle("hidden", !h3);
  $("layers-wrap").classList.toggle("hidden", !layers);
  // H3 的步数是蒸馏 LoRA 的固定档，后端会**当场拒掉**一条 steps 轴
  // （见 `sweep.expand`）—— 那就别把那一格摆出来，让人填一个必然失败的东西。
  $("sweep-steps-wrap").classList.toggle("hidden", h3);
  $("size-custom-wrap").classList.toggle(
    "hidden", !qwenDraw || $("size").value !== "custom");
}

/* 扫描里**不接**「出图后自动放大」那条链（后端会剥掉它，见 `sweep.expand`）。
   所以勾着它去按扫描之前就得说一声 —— 否则用户拿到 N 张没放大的图，只会以为
   扫描坏了。判据与后端同一份：开着链、而且当前不是放大模式。 */
function renderSweepChainNote() {
  // 判据与 `collectBody()` 里 `auto_upscale.enabled` **逐字同源**
  // （`qwenDraw && checked`）—— 两处各写一份的话，页面会在链其实没开的时候说
  // 「不生效」，或者反过来：开了却不说。而且这里要用**有效模式**，
  // 不是 `$("mode").value`：在 H3 / 拆层那一档上那个下拉框的值与真正跑的东西无关。
  const mode = state.chain === "qwen" ? $("mode").value : state.chain;
  const qwenDraw = mode === "t2i" || mode === "edit";
  const on = qwenDraw && $("auto_upscale").checked;
  $("sweep-chain-note").textContent = on
    ? "「出图后自动放大」在扫描里不生效。"
    : "";
}

let buildTimer = null;
function scheduleBuild() {
  disarmConfirm();
  clearTimeout(buildTimer);
  buildTimer = setTimeout(refreshBuild, 280);
}

async function refreshBuild() {
  const body = collectBody();
  // 必填项按**链路**分：
  //   t2i / edit / H3 → 提示词（放大与拆层没有提示词这个输入）
  //   upscale / layers → 源图
  //   H3              → 提示词**和**首帧
  // 2026-10-01：没填时**不再往页面上写一句话**（那句本来就是估时行的一部分，
  // 按用户要求一起去掉了）。缺什么由点「出图」时 `run()` 当场说 —— 一句话只在
  // 真用得上的时候出现，比常驻一句更省版面。
  let missing = false;
  if (body.mode === "upscale" || body.mode === "layers") {
    missing = !body.source_image;
  } else if (body.mode === "h3") {
    missing = !(body.prompt && body.prompt.trim()) || !body.source_image;
  } else {
    missing = !(body.prompt && body.prompt.trim());
  }
  if (missing) {
    state.build = null;
    $("warnings").innerHTML = "";
    // 后端**这一轮根本没判**反向词（请求没发出去），所以不能留着上一次的结论。
    // 不然后端就没机会纠正它：清了提示词、再把 CFG 调回 1.0，标题上会一直顶着
    // 「生效中」而 warnings 是空的 —— 两个地方互相打脸，用户没法判断哪句是真的。
    // 这是**真的渲染页面**之后才发现的（tests/page_shot.py），假 DOM 里看不见。
    renderNegative(null);
    renderH3Frames(null);
    renderRunButton();
    return;
  }
  try {
    const data = await postJSON("/api/build", body);
    state.build = data;
    renderBuildWarnings(data);
  } catch (e) {
    // 后端没答上（参数非法 / 后端没起）时那句话给谁看？**给 warnings 那一栏** ——
    // 它本来就是「提交前后端说的话」唯一的地方，估时行去掉之后更没有第二个落点。
    // 用 `warn-line error` 这个长相：与体检项同一条左边线颜色，不用学新样式。
    state.build = null;
    $("warnings").innerHTML =
      '<div class="warn-line error">' + esc(e.message) + "</div>";
    renderNegative(null);           // 同上：没有后端结论就不许声称任何状态
    renderH3Frames(null);           // 同理：这一轮后端根本没反解，别留上一档的帧数
    renderRunButton();
  }
}

/* 提交前那一栏**警告**（旧名字 `renderEstimate` —— 那时它还负责把估时行画出来）。
 *
 * 2026-10-01 按用户要求：估时行与那些**说明性**提示都不再上页面。判据落在
 * `level` 上，不逐条列白名单 —— `info` 的含义正是「说清一件事，但不改变结果」
 * （TE-Speed / 缓存加速省多少、输出多大、参考图已在 input/ 里），
 * `warn` / `error` 是「会拦住你」或「你填的东西不生效」，一件都不能省：
 * 「提交必然失败」「反向词不会生效」恰是本工作区最不能静默的两类错。
 *   —— 白名单会长在文案上：后端改一句话，页面就少一条警告，而且**没有症状**。
 * 估时本身仍在 `/api/build` 的 `estimate` 里，任务记录也照旧记 `estimate_s`
 * （进度面板与任务列表照显），所以这一栏消失不等于那个数不存在。 */
function renderBuildWarnings(data) {
  const ws = (data.warnings || []).filter((w) => w.level !== "info");
  $("warnings").innerHTML = ws.map((w) =>
    '<div class="warn-line ' + esc(w.level) + '">' + esc(w.text) + "</div>").join("");
  renderNegative(data);
  renderH3Frames(data);
  renderRunButton();
}

/* H3 时长旁边那一句「124 帧 / 5.17 秒」。
 *
 * **数是从后端反解回来的**（`describe.frames` / `describe.duration_s`），不是
 * 页面上算的：基准图里有一个 `ComfyMathExpression` 把秒数吸附到 17 帧网格上，
 * 在页面上再抄一份公式，迟早就与图里那份分家 —— 而分家之后用户看到的是一句
 * 与产物对不上的话，且**没有症状**。
 *
 * 拆层 / 放大 / 出图那一档没有这个数，所以清空（不能留着上一档的读数）。 */
function renderH3Frames(data) {
  const el = $("h3-frames");
  if (!el) return;
  const d = (data && data.describe) || {};
  if (state.chain !== "h3" || !d.frames) {
    el.textContent = "";
    return;
  }
  const secs = (typeof d.duration_s === "number") ? d.duration_s.toFixed(2) : null;
  el.textContent = secs ? "→ " + d.frames + " 帧 / " + secs + " 秒" : "";
}

/* 反向词的状态由**后端**算（`server.py::negative_prompt_state`），这里只显示：
 * 折叠标题上的状态字（折着也看得见，否则它就是个藏起来的失效开关）+ 那个
 * 「让它生效」的按钮。长解释在上面的 warnings 里，此处**不复述**。
 *
 * 为什么不在前端判 CFG：判据一旦有两份就一定会分家 —— 页面顶着「生效中」而
 * 下面的警告说「不会生效」，用户没法解释哪句是真的。
 * 那个按钮上的数值也取自后端（st.suggest），免得哪天后端改了、按钮还写着旧数。 */
function renderNegative(data) {
  const st = data && data.negative_prompt;
  const button = $("btn-raise-cfg");
  if (!st) {
    $("negative-hint").textContent = "";
    button.classList.add("hidden");
    return;
  }
  $("negative-hint").textContent = !st.filled ? ""
    : (st.live ? "生效中" : "填了，但不生效");
  button.classList.toggle("hidden", !st.inert);
  // `toFixed(1)`：不写的话 JS 会把 2.0 渲染成「2」，而同一屏上的警告文字写的是
  // 「2.0」—— 两个数字看着不一样，人就会怀疑其中一个。数值本身仍取自后端。
  if (st.inert) {
    button.textContent = "让反向词生效（CFG → " + st.suggest.toFixed(1) + "）";
  }
}

/* ---------- 成本预警（判据：贵的那档要点两下） ----------
   要不要确认由**后端**决定（cost.confirm_policy），前端只负责显示与上膛 ——
   这样 curl 直接打 /api/run 也挡得住，门槛本身还能用单测钉住。 */
function renderRunButton() {
  const c = state.build && state.build.estimate && state.build.estimate.confirm;
  const need = !!(c && c.required);
  const btn = $("btn-run");
  btn.classList.toggle("armed", need && state.confirmArmed);
  // 按钮上的字**按链路变**（出图 / 出视频 / 开始拆层）—— 三条链路的产物完全不同，
  // 一个「出图」压在一次 310 秒的视频生成上是不诚实的。
  // `#run-label` 是这颗按钮里唯一的文字节点，所以整个 `textContent` 由这一处写。
  // `flashButton()` 从不作用在 `#btn-run` 上（已核），所以不踩那个尖角。
  const base = CHAIN_RUN_LABEL[state.chain] || "出图";
  $("run-label").textContent = !need ? base
    : (state.confirmArmed ? "确认提交" : base + " · 先确认");
  $("confirm-hint").innerHTML = need
    ? '<span class="' + (state.confirmArmed ? "bad" : "dim") + '">' +
      (state.confirmArmed ? "再点一次开始 —— " : "") + esc(c.text) + "</span>"
    : "";
}
function disarmConfirm() {
  // 任何参数改动都重新上膛：否则「改完再点一次」会直接提交，等于没有预警
  if (state.confirmArmed) { state.confirmArmed = false; renderRunButton(); }
}

/* ---------- 参数扫描（P5）----------
   判据 5：N 个 seed 并排出图，各带 wall_ms 与 s/步。

   前端刻意**不自己算** s/步 —— 那个数有两种来源（探针的采样循环读数，
   或者 wall_ms ÷ 步数），后一种含加载与解码、明显偏大。后端会把来源一起给，
   页面照实显示；自己再算一遍就等于把这个区分抹掉了。 */
function parseList(text) {
  return String(text || "")
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter((s) => s !== "")
    .map((s) => Number(s))
    .filter((n) => Number.isFinite(n));
}

async function runSweep() {
  const body = collectBody();
  if (!body.prompt || !body.prompt.trim()) {
    $("sweep-msg").innerHTML = '<span class="bad">提示词不能为空</span>';
    return;
  }
  const seeds = parseList($("sweep-seeds").value);
  const steps = parseList($("sweep-steps").value);
  if (!seeds.length && !steps.length) {
    $("sweep-msg").innerHTML =
      '<span class="bad">至少给一条轴：seed 或 steps</span>';
    return;
  }
  const grid = {};
  if (seeds.length) grid.seed = seeds;
  if (steps.length) grid.steps = steps;
  // 成本预警在扫描里同样生效（后端会拦）；这里先把上膛状态带上，
  // 免得「一次排 3 个 2K」每次都要被 409 挡一次
  if (state.confirmArmed) body.confirm = true;

  $("btn-sweep").disabled = true;
  $("sweep-msg").textContent = "提交中…";
  try {
    const data = await postJSON("/api/sweep", Object.assign({}, body,
                                                             { grid: grid }));
    state.sweepId = data.sweep_id;
    $("sweep-msg").innerHTML = "已排队 <b>" + data.count + "</b> 张" +
      (data.dropped_dupes ? "（去掉了 " + data.dropped_dupes + " 个重复）" : "") +
      " <span class=\"dim\">· 每格就是一次普通出图，队列由 ComfyUI 自己排</span>";
    disarmConfirm();
    await fetchSweep(data.sweep_id);
  } catch (e) {
    // 409 = 成本预警拦的。把 confirm 带回去上膛，再点一次就能过
    const c = e.payload && e.payload.confirm;
    if (e.payload && e.payload.confirm) {
      state.confirmArmed = true;
      $("btn-sweep").textContent = "扫描 · 确认";
      $("sweep-msg").innerHTML = '<span class="bad">' + esc(c.text) + "</span>";
    } else {
      $("sweep-msg").innerHTML = '<span class="bad">' + esc(e.message) + "</span>";
    }
  } finally {
    $("btn-sweep").disabled = false;
  }
}

async function fetchSweep(id) {
  if (!id) return;
  try {
    const data = await getJSON("/api/sweep/" + encodeURIComponent(id));
    state.sweeps.set(id, data);
    state.sweepId = id;
    renderSweep();
  } catch (e) {
    $("sweep-grid").innerHTML = '<p class="bad">读不了这次扫描：' +
      esc(e.message) + "</p>";
  }
}

function sweepCellHtml(cell) {
  const cellParam = cell.cell || {};
  const bits = [];
  if (cellParam.seed != null) bits.push("<b>seed</b> " + esc(cellParam.seed));
  if (cellParam.steps != null) bits.push("<b>steps</b> " + esc(cellParam.steps));
  if (!bits.length) bits.push("<span class=\"dim\">未指定 seed/steps</span>");

  const ps = cell.per_step || {};
  // s/步 必须连来源一起显示 —— 两种算法的数不是同一个量
  const perStep = ps.ms == null
    ? '<span class="dim">s/步 —</span>'
    : "<b>" + (ps.ms / 1000).toFixed(2) + " s/步</b> " +
      '<span class="dim" title="' + esc(sweepSourceText(ps.source)) + '">' +
      esc(ps.source === "probe" ? "探针" : "≈ 偏大") + "</span>";

  const wall = cell.wall_ms == null
    ? '<span class="dim">wall —</span>'
    : "<b>" + (cell.wall_ms / 1000).toFixed(1) + " s</b>";
  const cov = cell.timing && cell.timing.coverage_pct != null
    ? '<span class="dim">覆盖 ' + esc(cell.timing.coverage_pct) + "%</span>" : "";

  const o = (cell.outputs || [])[0];
  let fig;
  if (o) {
    const url = "/api/image?filename=" + encodeURIComponent(o.filename) +
                "&subfolder=" + encodeURIComponent(o.subfolder || "") +
                "&type=" + encodeURIComponent(o.type || "output");
    fig = '<img loading="lazy" alt="' + esc(o.filename) + '" src="' + url + '">';
  } else {
    fig = '<span class="sweep-blank">' +
      esc(statusLabel(cell.status)) +
      (cell.status === "error" ? "<br><span class=\"dim\">" +
        esc((cell.error || {}).message || "") + "</span>" : "") + "</span>";
  }
  return '<button type="button" class="sweep-cell" data-job="' + esc(cell.job_id) + '">' +
    fig +
    '<span class="sweep-caption">' + bits.join(" · ") + "</span>" +
    '<span class="sweep-stats">' + wall + " " + perStep + " " + cov + "</span>" +
    "</button>";
}

function sweepSourceText(source) {
  return source === "probe"
    ? "探针的采样循环读数（只含纯去噪循环）"
    : "wall_ms ÷ 步数 —— 含模型加载与解码，比真实步耗时偏大";
}

function refreshSweepIfRunning() {
  const d = state.sweepId ? state.sweeps.get(state.sweepId) : null;
  if (d && d.finished < d.count) fetchSweep(state.sweepId);
}

function renderSweep() {  const data = state.sweepId ? state.sweeps.get(state.sweepId) : null;
  const box = $("sweep-grid");
  if (!data) {
    box.innerHTML = '<p class="dim">还没有扫描。</p>';
    return;
  }
  const cells = (data.cells || []).slice().sort(
    (a, b) => (a.cell_index == null ? 1e9 : a.cell_index) -
              (b.cell_index == null ? 1e9 : b.cell_index));
  const head = '<p class="dim">' + data.finished + " / " + data.count +
    " 跑完 · 总 " + (data.wall_ms_total == null ? "—"
      : (data.wall_ms_total / 1000).toFixed(0) + " s") +
    " · " + esc(data.per_step_note || "") + "</p>";
  box.innerHTML = head + '<div class="sweep-row">' +
    cells.map(sweepCellHtml).join("") + "</div>";
  // 点一格 = 把它填回表单（复用 P2 那套，参数已经在记录里了）
  for (const el of box.querySelectorAll(".sweep-cell")) {
    el.onclick = () => selectJob(el.dataset.job);
  }
}


/* ---------- 任务状态更新 ---------- */
function upsert(id, patch) {
  const cur = state.jobs.get(id) || {
    job_id: id, outputs: [], prompt_text: "", params: {}, node_classes: {},
  };
  Object.assign(cur, patch || {});
  if (!Array.isArray(cur.outputs)) cur.outputs = [];
  state.jobs.set(id, cur);
  return cur;
}
function mergeOutputs(job, incoming) {
  const seen = new Set((job.outputs || []).map((o) => o.filename + "|" + o.subfolder));
  for (const o of incoming || []) {
    const k = o.filename + "|" + (o.subfolder || "");
    if (!seen.has(k)) { seen.add(k); job.outputs.push(o); }
  }
}
function sortedJobs() {
  return [...state.jobs.values()].sort(
    (a, b) => (b.created_at || 0) - (a.created_at || 0));
}
function anyRunning() {
  for (const j of state.jobs.values()) {
    if (j.status === "queued" || j.status === "running") return true;
  }
  return false;
}

/* ---------- 渲染 ---------- */
function render() {
  renderBackendSwitch();
  renderLive();
  renderJobs();
}

/* ---------- 后端开关（P20i）----------
   一颗**没有文字**的滑动开关，摆在右上角。原来那块告警板整个撤掉了 ——
   后端没起是常态，不该在常态上长一块红板子。

   **开** = 按铁律 1 的命令拉起后端；**关** = 停掉工作台自己起的那个
   （不是自己起的，后端会拒绝，并把原因写进日志台）。

   六态，全是**算出来**的，不是记出来的：「后端在不在」永远看后端本身
   （`be.running`）；「那个进程是不是我的」看 `launch.process_alive` ——
   进程句柄只在本进程里有意义，所以工作台重启过之后它会如实说「不是我起的」。

   | state | 什么时候 | 颜色/位置 |
   |---|---|---|
   | checking | 第一次探活还没回来 | 灰、左、不可点 |
   | off | 没在跑、也没刚失败 | 灰、左 |
   | starting | 我们正在起它 | 蓝、左、脉动、不可点 |
   | on | 在跑、是我们的、模型齐 | 绿、右 |
   | degraded | 在跑但缺模型文件 | 琥珀、右（别急着出图） |
   | external | 在跑但不是这次起的 | 绿虚线、右（点了会被拒） |
   | error | 上次启动失败了 | 红、左（点一下就是重试） |

   `title` 是状态的**详细版**（悬停提示），也是拒绝原因的唯一落点 ——
   真出事时那句话同时会进日志台的时间线，这里只是把它摆在手边。 */
const SWITCH_LABEL = {
  checking: "正在检查", off: "没在跑", starting: "正在启动",
  on: "在跑", degraded: "在跑，但模型文件不全",
  external: "在跑（不是工作台起的）", error: "上次没起来",
};

function switchView(be) {
  const launch = (be && be.launch) || {};
  const missing = (be && be.missing_models) || [];
  let state = "checking";
  if (be) {
    if (launch.phase === "starting") state = "starting";
    else if (be.running) state = (missing.length ? "degraded"
                                : (launch.process_alive ? "on" : "external"));
    else if (launch.phase === "failed" || launch.phase === "exited") state = "error";
    else state = "off";
  }
  // 「受限模式」原来只写在药丸那行小字上，那行已经没有了 —— 挪进提示里，
  // 并在日志台的「环境体检」里留一行（不然这台机器的落点被搬过，页面上再没有第二处说得出来）。
  const lay = layoutOf(launch);
  const bits = [];
  if (state === "external") {
    bits.push("这个后端不是工作台这次起的（工作台重启过，或它是你在别处起的）"
              + "—— 认不出的进程工作台不杀，点它会被拒绝。");
  }
  if (state === "degraded") {
    bits.push("缺 " + missing.join("、") + " —— extra_model_paths 没生效？"
              + "别急着出图，会白跑。");
  }
  if (state === "starting" && launch.elapsed_ms != null) {
    bits.push("已经等了 " + Math.round(launch.elapsed_ms / 1000) + " 秒 / 上限 "
              + launch.ready_timeout_s + " 秒；起没起来的判断看后端本身。");
  }
  if (state === "error") {
    bits.push(launch.error || launch.error_short || "上次启动没起来。");
  }
  if (launch.phase === "stopped") bits.push("上一次是工作台停掉的。");
  if (lay.mode === "confined") bits.push("受限模式：" + (lay.note || "落点在工区里"));
  if (be && be.hint) bits.push(be.hint);
  bits.push(state === "on" || state === "degraded" || state === "external"
    ? "点一下 = 停掉它（正在出图时会拒绝，原因在日志台）。"
    : "点一下 = 按铁律 1 的命令拉起后端（进度与失败原因都在日志台）。");
  return { state, title: bits.filter(Boolean).join("\n") };
}

function renderBackendSwitch() {
  const btn = $("be-switch");
  if (!btn) return;
  const view = switchView(state.backend);
  btn.dataset.state = view.state;
  btn.title = view.title;
  btn.setAttribute("aria-checked",
    (view.state === "on" || view.state === "degraded"
     || view.state === "external") ? "true" : "false");
  // 名字不上屏，但读屏得说到「它现在是什么」—— 只报「后端开关」等于报了一个开关
  // 却不说它开没开。
  btn.setAttribute("aria-label", "后端开关 · " + SWITCH_LABEL[view.state]);
  // 进行中的两态不可点：Launcher 那边也拦（「已经在启动中了」），但那是一句 409，
  // 而一颗点得动却没反应的开关看起来就是坏的。
  btn.disabled = view.state === "starting" || view.state === "checking";
}

/* 收尾节点（写盘的那个）：**按类名认，不按 id**。
   提交前会给它的前缀塞一个本次作业的短标记，为的就是「同一份参数再提一遍也写得下
   新文件」（见 `qwen21_build.unique_save_prefix`）—— 所以判断「这次到底重算了没有」
   时，要把它单独摘出去：它每次都跑。 */
const SAVE_CLASSES = new Set(["SaveImage", "SaveImageAdvanced"]);

/* 这次是「真重算」「只有一部分命中」「上游全在缓存里」？
 *
 * 判据只看一件事：**除收尾之外，还有没有节点真的跑过**。全都在缓存里就意味着
 * 采样一步都没走、只是把缓存里那张重新写了出来（秒级）—— 那件事必须说出来，
 * 否则「秒完成」看起来就像出图坏了（2026-10-02 用户报的就是这个）。
 * 两种输入（任务记录 / 耗时面板的行）在这里合成同一种形状，判据只有这一份。
 */
function cacheVerdict(items) {
  const cached = items.filter((i) => i.cached);
  if (!cached.length) return { count: 0, kind: "none" };
  const upstream = items.filter((i) => !SAVE_CLASSES.has(i.cls));
  const all = upstream.length > 0 && upstream.every((i) => i.cached);
  return { count: cached.length, kind: all ? "all" : "warm" };
}

function cacheVerdictOfJob(job) {
  const classes = job.node_classes || {};
  const hit = new Set(job.cached_nodes || []);
  return cacheVerdict(Object.keys(classes).map((id) => (
    { id, cls: classes[id], cached: hit.has(id) })));
}

/* 产物图取不到了 —— 三件毫不相干的事长得一模一样，而**说错是哪一件**会把用户
 * 指去修错的地方（去回收站找一张好端端的图，或者白重出一遍）：
 *
 *   · 产物真的没了（删掉 / 搬走）—— 参数还在记录里，重出一次就有；
 *   · **后端没在跑** —— 这张图是经 `/api/image` → ComfyUI `/view` 取的，后端不在
 *     就取不到，而文件好端端地躺在 `output/` 里（2026-10-03 用户那张截图里
 *     「文件丢失」有一半就是这个）；
 *   · 后端在跑，但它的输出目录里没有这一张。
 *
 * 判据只能来自**后端现算的那一份**（`job.gone`，后端摸得到磁盘）与「后端在不在」
 * （`state.backend`）：页面手里没有磁盘，`img.onerror` 只知道「这张图没画出来」。
 */
function markShotGone(img, job) {
  img.classList.add("broken");
  const cap = img.parentElement && img.parentElement.querySelector("figcaption");
  if (!cap) return;
  const be = state.backend || {};
  let why;
  if (job && job.gone) {
    why = '<b class="bad">产物已经不在了</b>' +
      '<span class="dim">（既不在 output/ 里、也不在归档目录里；' +
      "参数还在任务记录里，重出一次就有）</span>";
  } else if (!be.running) {
    why = '<b class="bad">取不到图：后端没在跑</b>' +
      '<span class="dim">（图是经后端 /view 取的，文件多半还在 output/ 里）</span>';
  } else {
    why = '<b class="bad">取不到图</b>' +
      '<span class="dim">（后端在跑，但它的输出目录里没有这一张）</span>';
  }
  cap.innerHTML = esc(img.alt || "这张图") + " " + why;
}

/* 还原一张（大卡片上那颗按钮）。
 *
 * 走的是**批量那条同一个端点**，只是件数是一 —— 另开一条「单张还原」的路，就会长出
 * 第二份撞名与失败的口径。失败**就地说明**（那颗按钮自己变成理由），不弹、也不写进
 * 画廊那一栏：用户此刻看的是上面这块大卡片，消息跑到别处等于没说。
 */
async function restoreOne(btn) {
  // 名字从**这颗按钮自己身上**取（`data-name`，画它的时候就写进去了）—— 不许去
  // 爬那几段文字：文案改一个字，取出来的就是半截文件名，而它会安静地去还原一张
  // 根本不存在的东西（表现是「点了没反应」）。
  const name = btn.dataset.name || "";
  btn.disabled = true;
  btn.textContent = "正在还原…";
  try {
    const r = await postJSON("/api/gallery/restore", { items: [{ name: name }] });
    const failed = (r.failed || [])[0];
    if (failed) {
      btn.textContent = "没成：" + failed.why;
      btn.disabled = false;
      return;
    }
    fetchJobs();               // 卡片据此重画成正常那张（图回来了）
  } catch (e) {
    btn.textContent = "没成：" + e.message;
    btn.disabled = false;
  }
}

function renderLive() {
  const box = $("live");
  const job = state.currentId ? state.jobs.get(state.currentId) : null;
  if (!job) {
    box.innerHTML = '<div class="empty-state">' +
      '<div class="empty-visual"><svg viewBox="0 0 48 48" fill="none" aria-hidden="true">' +
        '<rect x="7" y="7" width="34" height="34" rx="4" stroke="currentColor" stroke-width="1.5"/>' +
        '<circle cx="17" cy="18" r="3" stroke="currentColor" stroke-width="1.5"/>' +
        '<path d="m8 34 10-9 7 6 7-11 9 14" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>' +
      '</svg></div><h3>等待第一张作品</h3><p>在左侧描述你的画面，点击「出图」开始创作。</p>' +
      '<span class="eyebrow">YOUR NEXT IMAGE STARTS HERE</span></div>';
    return;
  }
  const p = job.progress;
  const status = job.status || "queued";
  const running = status === "queued" || status === "running";
  const cls = job.class_type || (job.node_classes || {})[job.node_id] || "";
  const when = job.started_at || job.created_at || Date.now();

  let bar;
  if (status === "done" || status === "error" || status === "cancelled") {
    const pct = status === "done" ? 100 : 0;
    bar = '<div class="bar" role="progressbar" aria-label="任务进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '"><i style="width:' + pct + '%"></i></div>';
  } else if (p && p.max) {
    const pct = Math.min(100, Math.round((p.value / p.max) * 100));
    bar = '<div class="bar" role="progressbar" aria-label="采样进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '"><i style="width:' + pct + '%"></i></div>';
  } else {
    bar = '<div class="bar idle" role="progressbar" aria-label="等待采样进度"><i></i></div>';
  }

  let stepLine = "";
  if (running) {
    if (p && p.max) {
      stepLine = "采样 " + p.value + " / " + p.max + " 步";
    } else if (cls) {
      stepLine = "正在执行 " + cls + "（节点 " + esc(job.node_id || "?") + "）";
    } else {
      stepLine = "排队中…";
    }
  }

  const meta = [];
  meta.push("<span>模式 <b>" + esc(MODE_LABEL[job.mode] || job.mode || "?") + "</b></span>");
  const params = job.params || {};
  if (params.steps) meta.push("<span>步数 <b>" + params.steps + "</b></span>");
  if (params.width && params.height) meta.push("<span>尺寸 <b>" + params.width + "×" + params.height + "</b></span>");
  if (params.seed != null) meta.push("<span>种子 <b>" + params.seed + "</b></span>");
  // 两个额外功能都不是模式，所以各自一格：「这张是改写的」「这张出图后还要接放大」
  if (params.rewrite) meta.push("<span>改写 <b>开</b></span>");
  if ((params.auto_upscale || {}).enabled) {
    meta.push("<span>出图后放大 <b>×" + esc(params.auto_upscale.scale) + "</b></span>");
  }
  // 链的脚印：子作业说「我放大自谁」，父作业说「我接出去的放大怎么样了」
  if (job.parent_job_id) meta.push('<span class="dim">放大自上一个作业</span>');
  if (job.chain) {
    meta.push(job.chain.status === "submitted"
      ? '<span class="dim">已接放大 ×' + esc(job.chain.scale) + "</span>"
      : '<span class="bad">放大没接上</span>');
  }
  if (running) meta.push('<span>已用 <b data-elapsed="' + when + '">…</b></span>');
  if (job.wall_ms) meta.push("<span>总耗时 <b>" + fmtElapsed(job.wall_ms) + "</b></span>");
  // **页面上不报估算**（2026-10-01 按用户要求）：那个数是有依据的，但它对
  // 「这台机器现在这一刻有多快」不敏感（开着加速、显存里还留着上一个模型，
  // 都能差两三倍），而一个偏大的数就摆在真实耗时旁边，看起来只是工作台算错了。
  // 数本身没删：任务记录的 `estimate_s` 还在，成本预警（贵的那档要点两下）
  // 正是从它来的 —— 与提交前那一栏撤掉估时行是同一条规矩。
  // 缓存命中分两种，说法必须分开：「只有一部分命中」是原来的温启动；**上游全命中**
  // 意味着采样一步都没走、只是把缓存里那张重新写了一遍 —— 那件事得说出来，
  // 否则「秒完成」看起来就像出图坏了（2026-10-02 用户报的就是这个）。
  const cv = cacheVerdictOfJob(job);
  if (cv.kind === "all") {
    meta.push('<span class="warm">没有重新采样：上游全在缓存里，这次只是把上次那张重新写了一遍</span>');
  } else if (cv.kind === "warm") {
    meta.push('<span class="dim">温启动（' + cv.count + " 个节点走缓存）</span>");
  }

  let err = "";
  if (job.error) {
    const e = job.error;
    err = '<div class="errbox" role="alert"><strong>' + esc(e.message || "出错了") + "</strong>";
    if (e.node_type) err += "<br>节点类型：" + esc(e.node_type);
    if (e.node_errors && Object.keys(e.node_errors).length) {
      err += "<br>" + esc(JSON.stringify(e.node_errors, null, 2));
    }
    err += "</div>";
  }
  // 链失败：**这张图本身是成功的**，所以它不该混进上面那个「出错了」里 ——
  // 而且必须给出下一步（单独跑一次放大），否则用户只能看着一张没放大的图发呆。
  if (job.chain && job.chain.status === "failed") {
    err += '<div class="errbox" role="alert"><strong>出图后自动放大没接上</strong><br>' +
      esc(job.chain.error || "原因不明") +
      "<br>这张图本身是成功的；要放大它，把它当源图单独跑一次「放大」就行。</div>";
  }

  let fig = "";
  const outs = job.outputs || [];
  // ★ 有 `artifact` 的作业（H3 的视频 / 拆层的层集）**不画这张大图**。
  //
  // H3 那条是活体验收抓到的真凭据：`SaveVideo` 把 mp4 放在 **`images`** 键下
  //（另带一个 `animated: [true]`），于是 `comfy.outputs_from_history()` 照原样
  // 把它当「一张图」收进了 `job.outputs`。这里就会拼出
  // `/api/image?filename=….mp4&subfolder=video` 塞进 `<img>` —— 浏览器解不了 mp4，
  // 触发 `onerror` → `markShotGone()` → 页面上写着「多半是被删了」，
  // 而**那段视频就在下面那块 `<video>` 里好好地放着**。
  // 拆层那条则是 `outputs` 本来就空（`SeeThrough_SavePSD.save()` 返回裸字符串）。
  //
  // 判据用 `job.artifact` 而不是「文件名后缀像不像视频」：那张后缀表在
  // `artifacts.VIDEO_EXT` 里只有一份，页面再抄一份就是第二个真源。
  if (outs.length && !job.artifact) {
    const o = outs[outs.length - 1];
    const url = "/api/image?filename=" + encodeURIComponent(o.filename) +
                "&subfolder=" + encodeURIComponent(o.subfolder || "") +
                "&type=" + encodeURIComponent(o.type || "output");
    if (job.archived) {
      // 产物已经归档：**不再画一张取不到的图**。画了就会走 `markShotGone` 那条文案，
      // 而它说的是「多半是被删了」—— 把「我们搬走了」说成「没了」是错的（而且还让
      // 用户去回收站找）。这里是同一套判据的另一面：**说得出它去哪了**。
      fig = '<div class="errbox" role="status"><strong>这张已经归档</strong>' +
        '<div class="dim">' + esc(o.filename) +
        " —— 它没有被删掉，就在归档目录里。</div>" +
        '<button class="ghost" id="btn-unarchive" type="button" data-name="' +
        esc(o.filename) + '">还原这张</button></div>';
    } else {
      fig = '<figure><img src="' + url + '" alt="' + esc(o.filename) + '" loading="lazy">' +
            '<figcaption class="dim">' + esc(o.filename) + "</figcaption></figure>";
    }
  }

  box.innerHTML =
    '<div class="job-head">' +
      '<span class="pill ' + esc(status) + '">' + esc(statusLabel(status)) + "</span>" +
      '<span class="dim">' + esc(job.job_id || "") + "</span>" +
      (running ? '<button class="ghost" id="btn-cancel">取消</button>' : "") +
    "</div>" +
    bar +
    (stepLine ? '<div class="dim" style="margin-bottom:8px">' + stepLine + "</div>" : "") +
    '<div class="meta">' + meta.join("") + "</div>" +
    err + artifactStage(job) + fig + chainStage(job) + timingsHtml(job.job_id) +
    '<div class="dim result-prompt">' + esc(job.prompt_text || "") + "</div>";

  const cancel = $("btn-cancel");
  if (cancel) cancel.onclick = () => cancelJob(job.job_id);
  // 链上那一段是**另一个作业**，所以它自己的取消按钮要指向子作业 —— 父作业
  // 那时已经完成了，顶栏那颗「取消」根本不在。
  const chainCancel = $("btn-chain-cancel");
  if (chainCancel) chainCancel.onclick = () => cancelJob(job.chain.job_id);
  const chainOpen = box.querySelector("#stage-chain .stage-open");
  if (chainOpen) chainOpen.onclick = () => selectJob(chainOpen.dataset.job);
  const unarchive = box.querySelector("#btn-unarchive");
  if (unarchive) unarchive.onclick = () => restoreOne(unarchive);
  // 两颗 PSD 按钮只在**层集产物画出来了**的时候存在，所以按存在与否绑
  //（`artifactStage()` 在不能出 PSD 时会一颗都不画）。
  const psdRgba = $("btn-psd-rgba");
  if (psdRgba) psdRgba.onclick = () => downloadPsd(job, "rgba");
  const psdDepth = $("btn-psd-depth");
  if (psdDepth) psdDepth.onclick = () => downloadPsd(job, "depth");
  // 终态才去取耗时 —— 还在跑的时候报告根本没写完
  const shot = box.querySelector("figure img");
  if (shot) shot.onerror = () => markShotGone(shot, job);
  if (TERMINAL.has(status)) ensureTimings(job.job_id);
  tickElapsed();
}

function statusLabel(s) {
  return { queued: "排队中", running: "运行中", done: "完成",
           error: "失败", cancelled: "已取消" }[s] || s;
}

/* 「出图后放大」那一段（链）的进度。
 *
 * 为什么要单独画一块：链上的放大是**另一个作业**，父作业那时候已经 done 了 ——
 * 面板只画 `state.currentId` 的话，画面上就是「完成 + 100%」，而 GPU 其实还在忙
 * 二三十秒，用户完全看不出来（2026-10-01 用户报的就是这一条）。
 *
 * 为什么那条进度条是**按时间**画的：VOSR2 插件**不报进度**（翻过它的源码，
 * 整个包里没有一处 `PROGRESS_BAR`），能报的只有「正在执行哪个节点」和经过的
 * 时间。所以分母只能用估算，而且**到 95% 就封顶** —— 它本来就不是真实百分比，
 * 涨到头停住才对；说明也写在块里，别让一条按时间画的条子冒充进度。
 *
 * 子作业在 `state.jobs` 里（它自己的 SSE 事件会 upsert 进来，页面刷过也会从
 * `/api/jobs` 拿全），所以这里**不用再发请求**。
 */
function chainStage(job) {
  const c = job.chain || {};
  if (c.status !== "submitted" || !c.job_id) return "";
  const kid = state.jobs.get(c.job_id) || { job_id: c.job_id, status: "queued" };
  const status = kid.status || "queued";
  const live = !TERMINAL.has(status);
  const when = kid.started_at || kid.created_at || Date.now();
  const est = Number(kid.estimate_s) || 0;
  let pct = 0;
  if (status === "done") pct = 100;
  else if (live && est > 0) {
    pct = Math.max(2, Math.min(95, Math.round((Date.now() - when) / (est * 1000) * 100)));
  }
  const cls = kid.class_type || (kid.node_classes || {})[kid.node_id] || "";
  const bits = ['<span class="pill ' + esc(status) + '">' + esc(statusLabel(status)) + "</span>"];
  if (live) {
    bits.push('<span>已用 <b data-elapsed="' + when + '">…</b></span>');
    bits.push('<button class="ghost" id="btn-chain-cancel" type="button">取消放大</button>');
  } else if (kid.wall_ms) {
    bits.push("<span>耗时 <b>" + fmtElapsed(kid.wall_ms) + "</b></span>");
  }
  // 条子自己带着「起点 + 分母」，因为它是**按时间**长的：只在 SSE 有消息时重画
  // 的话，两次节点事件之间它会一动不动（`tickElapsed` 每 250ms 会来刷它）。
  const bar = '<div class="bar" role="progressbar" aria-label="出图后放大的进度" ' +
    'aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '">' +
    '<i data-elapsed-bar="' + when + '" data-elapsed-max="' + esc(est) + '"' +
    ' style="width:' + pct + '%"></i></div>';
  let note = "";
  if (live) {
    note = '<div class="dim stage-note">' +
      (cls ? "正在执行 " + esc(cls) + "（节点 " + esc(kid.node_id || "?") + "）· " : "") +
      "这一段报不出百分比（VOSR2 插件不报进度），条子按经过时间画，不是真实进度。</div>";
  } else if (status === "done") {
    note = '<div class="dim stage-note">放大完成。' +
      '<button class="ghost stage-open" type="button" data-job="' +
      esc(kid.job_id) + '">看放大后的图</button></div>';
  } else if (status === "error") {
    note = '<div class="dim stage-note">放大失败了：' +
      esc((kid.error || {}).message || "原因不明") +
      "。出图那张本身是好的，就在上面。</div>";
  }
  return '<div class="stage" id="stage-chain">' +
    '<div class="stage-head"><strong>出图后放大 ×' + esc(c.scale) + "</strong>" +
    bits.join("") + "</div>" + bar + note + "</div>";
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  if (n >= 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}

/* 两条新链路的产物那一块（H3 的视频 / See-through 的层集）。
 *
 * **为什么不能靠 `job.outputs`**（这三条是后端 `artifacts.py` 存在的理由）：
 *   ① `SeeThrough_SavePSD.save()` 返回的是个**裸字符串**，所以 `/history` 里
 *      一个产物名都没有 —— 一次成功的拆层会显示成「完成、但没有产物」；
 *   ② H3 的视频落在 `output/video/` 子目录，`/api/image` 那条路只在顶层找；
 *   ③ 插件自己那份 `seethrough_psd_info.log` 是**最后写入者胜**的指针，拿它
 *      回看永远是最近一次的层，而不是你点开这个作业的层。
 *
 * 所以判据全在后端算好、走 `job.artifact` —— 页面**不猜磁盘**。
 */
function artifactStage(job) {
  const art = job.artifact;
  if (!art) return "";

  if (art.kind === "video") {
    const src = "/api/media?filename=" + encodeURIComponent(art.filename) +
                "&subfolder=" + encodeURIComponent(art.subfolder || "");
    const bits = [esc(art.filename)];
    if (art.bytes) bits.push(fmtBytes(art.bytes));
    // 子目录那一层要说出来：视频**不在** output/ 顶层，用户按文件名去翻会找不到。
    if (art.subfolder) bits.push("在 " + esc(art.subfolder) + "/ 下");
    return '<div class="stage" id="stage-artifact">' +
      '<div class="stage-head"><strong>视频产物</strong>' +
      '<span class="dim">' + bits.join(" · ") + "</span></div>" +
      '<video class="artifact-video" controls preload="metadata" playsinline ' +
      'src="' + esc(src) + '"></video>' +
      '<div class="dim stage-note">' +
      '<a href="' + esc(src) + '" download="' + esc(art.filename) + '">下载这段视频</a>' +
      "</div></div>";
  }

  if (art.kind !== "layers") return "";

  const why = window.WbPsd ? window.WbPsd.refusal(art, "rgba") : "PSD 组件没加载上";
  const layers = art.layers || [];
  const head = ['<strong>分层产物</strong>',
    '<span class="dim">' + esc(art.info || "") + "</span>"];
  head.push('<span class="dim">' + (art.width || "?") + "×" + (art.height || "?") +
            " · " + layers.length + " 层</span>");

  // 两颗按钮**只在真能出 PSD 时才画**：画一颗点了报错的按钮，用户会以为是网络问题。
  let buttons = "";
  if (!why) {
    buttons = '<button class="ghost" id="btn-psd-rgba" type="button">下载 PSD</button>' +
              '<button class="ghost" id="btn-psd-depth" type="button">下载深度 PSD</button>';
  }

  const cells = layers.map(function (l) {
    const missing = (art.missing || []).indexOf(l.filename) >= 0;
    const img = missing
      ? '<div class="layer-missing">不在磁盘上</div>'
      : '<img loading="lazy" alt="' + esc(l.name || "") + '" src="' +
        // 格子画**缩略图**（长边 512 的 JPEG）；出 PSD 那条路走原图
        //（`psd.js` 的 `layerUrl`）—— 与画廊同一条规矩。
        // 第三个数传 `false`：产物在**当前**输出目录里，与画廊翻到哪一档无关。
        esc(galleryUrl({ root: art.root, filename: l.filename }, true, false)) + '">';
    const w = (l.right || 0) - (l.left || 0);
    const h = (l.bottom || 0) - (l.top || 0);
    return '<figure class="layer-cell' + (missing ? " is-missing" : "") + '">' +
      img + '<figcaption><span class="layer-name">' + esc(l.name || "?") +
      '</span><span class="dim">' + w + "×" + h + " @" + (l.left || 0) + "," +
      (l.top || 0) + "</span></figcaption></figure>";
  }).join("");

  // 状态行**单独一个元素**：按钮的文字不许被程序改（那是全页的规矩，
  // 因为 `flashButton` 会把你改过的那一版当成「原样」，之后再也变不回来）。
  const note = why
    ? '<div class="stage-note errline">' + esc(why) + "</div>"
    : '<div class="dim stage-note" id="psd-status">' +
      "PSD 在浏览器里生成（层按各自的位置与原尺寸贴回去，" +
      "不是整幅缩放）。</div>";

  return '<div class="stage" id="stage-artifact">' +
    '<div class="stage-head">' + head.join("") + buttons + "</div>" +
    '<div class="layer-grid">' + cells + "</div>" + note + "</div>";
}

/* 点「下载 PSD」。失败**留在那一行里**（不改按钮文字），与 LLM 面板同一条规矩。 */
async function downloadPsd(job, kind) {
  const line = $("psd-status");
  const art = (job || {}).artifact;
  const btn = kind === "depth" ? $("btn-psd-depth") : $("btn-psd-rgba");
  if (!line || !art) return;
  if (btn) btn.disabled = true;
  line.className = "dim stage-note";
  line.textContent = "正在生成 " + (kind === "depth" ? "深度 " : "") + "PSD…";
  try {
    const got = await window.WbPsd.download(art, kind);
    line.className = "dim stage-note";
    line.textContent = "已生成 " + got.filename + "（" + got.layers + " 层）";
  } catch (e) {
    line.className = "stage-note errline";
    line.textContent = "没成：" + (e && e.message ? e.message : e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderJobs() {
  const focusedJob = document.activeElement?.closest(".job-row")?.dataset.job;
  // **产物已经取不到的**那一行不画（P20h 起就有这条规矩，P20j 把判据换对了）。
  //
  // 判据由后端给（`job.gone` = 有产物、但一张都取不到了），页面**不去猜磁盘**：
  // 猜错的表现就是 2026-10-03 用户报的那一次 —— 从归档页彻底删掉一张之后，那一行
  // **重新冒出来**还带着一张取不到的图。旧判据是「文件在不在归档目录里」，于是
  // 「归档过、又被彻底删除」落进缝里（两处都不在 ⇒ 它算「没归档」）。
  // 现在三种情况（归档 / 彻底删除 / 手工搬走）合成同一个答案 —— 对「最近任务」
  // 来说它们没有区别：都是「这一行没有图可以给你看」。
  //
  // 记录本身还在账里（`state/jobs/*.json`），只有那颗「清理」会真的把它去掉，
  // 而它的射程更窄：**产物永远回不来的**（归档里的还能还原，绝不在射程内）。
  // 排队/在跑/失败/取消**永远画**（还没有产物，`gone` 恒为 false）。
  const rows = sortedJobs().filter((j) => !j.gone);
  $("jobs-count").textContent = rows.length ? "（" + rows.length + "）" : "";
  // 那颗「清理」的画法要跟着一起刷 —— 而且**在下面那个提前 return 之前**：
  // 全都取不到图的时候（用户刚清完归档又删干净），列表是空的，而按钮正是
  // 那一刻最该说得清「还剩几条可清」的东西。
  renderJobsClean();
  if (!rows.length) {
    $("jobs").innerHTML = '<p class="dim">' + (state.jobsStale
      ? "没有可显示的任务：账里这些记录的产物都已经不在磁盘上了。"
      : "还没有任务记录。") + "</p>";
    return;
  }
  $("jobs").innerHTML = rows.map((j) => {
    const o = (j.outputs || [])[0];
    const thumb = o
      ? '<img class="job-thumb" loading="lazy" alt="" src="/api/image?filename=' +
        encodeURIComponent(o.filename) + "&type=" + encodeURIComponent(o.type || "output") + '">'
      : '<span class="job-thumb blank" aria-hidden="true">·</span>';
    const bits = [
      '<span class="pill ' + esc(j.status) + '">' + esc(statusLabel(j.status)) + "</span>",
      esc(MODE_LABEL[j.mode] || j.mode || "?"),
    ];
    const p = j.params || {};
    if (p.steps) bits.push(p.steps + " 步");
    if (p.seed != null) bits.push("seed " + p.seed);
    if (p.rewrite) bits.push("改写");
    if ((p.auto_upscale || {}).enabled) bits.push("链 ×" + p.auto_upscale.scale);
    if (j.parent_job_id) bits.push("放大自上一个作业");
    if (j.chain) {
      bits.push(j.chain.status === "submitted"
        ? "已接放大 ×" + j.chain.scale : "放大没接上");
    }
    if (p.lazy_cache) bits.push("缓存加速");
    if (p.te_speed) bits.push("TE-Speed");
    // Turbo 与前两个**可以同时出现**（不互斥），所以它是**另起一个**徽章，
    // 不写成 else-if。
    if (p.turbo) bits.push("Turbo");
    // 排队中不再报「≈N 秒」（页面上不报估算，见 `renderLive` 里那段注释）
    const dur = j.wall_ms ? fmtElapsed(j.wall_ms) : "";
    if (dur) bits.push(dur);
    return '<button type="button" class="job-row' + (j.job_id === state.currentId ? " sel" : "") +
      '" data-job="' + esc(j.job_id) + '" title="' + esc(j.prompt_text || j.job_id || "查看任务") + '">' + thumb +
      '<span class="job-main"><span class="job-text">' +
      esc(j.prompt_text || "（无提示词）") + "</span>" +
      '<span class="job-sub">' + bits.join(" · ") + "</span></span>" +
      '<span class="dim job-id">' + esc((j.job_id || "").slice(0, 8)) + "</span></button>";
  }).join("");
  for (const el of $("jobs").querySelectorAll(".job-row")) {
    el.onclick = () => selectJob(el.dataset.job);
    if (el.dataset.job === focusedJob) el.focus({ preventScroll: true });
  }
  // 缩略图取不到（那张图已经被删/搬走）就画成斜纹块，与参考图那一栏同一个形状 ——
  // 一行 48px 的地方写不下一句话，点开上面那块会说清楚。
  for (const img of $("jobs").querySelectorAll(".job-thumb")) {
    img.onerror = () => img.classList.add("broken");
  }
}

/* 那颗「清理」：把**产物已经找不回来**的记录从任务账里去掉（P20j）。
 *
 * 四条不能忘：
 *  1. **射程由后端定**（`/api/jobs` 的 `stale` 就是那个数字）：产物既不在任何输出
 *     目录里、也不在归档目录里。归档里的绝不清 —— 图还原回 output/ 之后，那一行
 *     必须带着耗时与参数回来（P20h 第④条）。
 *  2. **只删记录，产物文件一张不动** —— 这句话既要在按钮的 tip 里，也要在回执里：
 *     紧挨着「清理」这个词的通常是「删东西」，而被删掉的记录进不了回收站。
 *  3. 没得清时**禁用并说出理由**，不画一颗点下去什么都不会发生的按钮。
 *  4. 清完之后**按回执对账**（`removed` 里那些 id 从本地那张表里去掉），而不是
 *     「再拉一次列表就算数」—— `upsert` 只加不删，被清掉的行会赖在页面上不走。
 */
function renderJobsClean() {
  const btn = $("btn-clean-jobs");
  if (!btn) return;
  const n = state.jobsStale | 0;
  btn.textContent = n ? "清理 " + n + " 条" : "清理";
  btn.disabled = n === 0;
  btn.title = n
    ? ("把这 " + n + " 条记录从任务账里去掉：它们的产物已经不在磁盘上、"
       + "也不在归档目录里，找不回来了。只删记录 —— 产物文件一张不动；"
       + "归档里的记录不在射程内（那些还能还原）。")
    : "没有可清理的记录：产物还在磁盘上的、以及归档里还能还原的，都留着。";
  const note = $("clean-note");
  if (note) note.textContent = state.jobsCleanNote;
}

async function cleanJobs() {
  const btn = $("btn-clean-jobs");
  if (btn) btn.disabled = true;                  // 一次点击只发一次请求
  state.jobsCleanNote = "正在清理…";
  renderJobsClean();
  try {
    const r = await postJSON("/api/jobs/clean", {});
    for (const id of r.removed || []) state.jobs.delete(id);
    if (state.currentId && !state.jobs.has(state.currentId)) state.currentId = null;
    state.jobsCleanNote = "清掉 " + (r.count || 0) + " 条记录（只删记录，产物文件一张没动）";
  } catch (e) {
    state.jobsCleanNote = "没清成：" + e.message;
  }
  await fetchJobs();
}

function tickElapsed() {
  const now = Date.now();
  for (const el of document.querySelectorAll("[data-elapsed]")) {
    el.textContent = fmtElapsed(now - Number(el.dataset.elapsed));
  }
  // 「出图后放大」那一段的条子是**按时间**长的（VOSR2 不报进度，见 `chainStage`），
  // 所以它得跟着这个 ticker 走 —— 只在 SSE 有消息时重画的话，两次节点事件之间
  // 它会一动不动。起点与分母都写在那颗 `<i>` 自己身上。
  for (const el of document.querySelectorAll("[data-elapsed-bar]")) {
    const max = Number(el.dataset.elapsedMax) * 1000;
    if (!(max > 0)) continue;
    const pct = Math.max(2, Math.min(95,
      ((now - Number(el.dataset.elapsedBar)) / max) * 100));
    el.style.width = pct.toFixed(1) + "%";
    const bar = el.parentElement;
    if (bar && bar.hasAttribute("aria-valuenow")) {
      bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    }
  }
}

/* 把一组参数填回表单。**两条入口，两种语义**（共用这一条路径是因为两边字段名
   不同：任务在 `params` 里，画廊项是平铺的，所以调用方先归一化）：

   · `selectJob()`（点「最近任务」）—— 回看一条任务**当时是什么**，连提示词、
     反向词、模式、参考图一起填回来（`promptText` 是字符串）；
   · `loadFromGallery()`（画廊「载入参数」）—— 只拿**数值参数**，其余一个不碰
     （只传 7 个键，`promptText` 传 `null`）。

   所以这里一律「**缺键 = 不动**」（每一条都是 `if (x != null)`），差别留给调用方。
   唯一的例外是尺寸那条 —— 它要读表单**当前**的模式，见下面那一段。 */
function applyParams(p, promptText) {
  p = p || {};
  // 老记录里的 `2k` / `refine` 先翻译成现在的说法，**并且只在下拉框真有这个
  // 选项时才写**（否则 select 会静默变空串 —— 见 `LEGACY_FORM_MODE` 那段）。
  if (p.mode) {
    // 老记录里的 `2k` / `refine` 先翻译成现在的说法，**并且只在下拉框真有这个
    // 选项时才写**（否则 select 会静默变空串 —— 见 `LEGACY_FORM_MODE` 那段）。
    const translated = LEGACY_FORM_MODE[p.mode] || p.mode;
    if (translated === "qwen" || translated === "h3" || translated === "layers") {
      // `h3` / `layers` 是**链路本身**：它们不在 `#mode` 的选项里，直接往下拉框
      // 里写会静默变成空串 —— 与 `LEGACY_FORM_MODE` 那段是同一个坑，只是换了
      // 一层。先切链路，下拉框**不动**（留着上一次的形状，切回 Qwen 时还在）。
      setChainForMode(translated, false);
    } else if ([...$("mode").options].some((o) => o.value === translated)) {
      // Qwen 那三档：先切回 qwen 那一档再写形状 —— 顺序不能反，下拉框的值
      // 只在 qwen 档下有意义。
      setChainForMode("qwen", false);
      $("mode").value = translated;
    }
  }
  if (promptText != null) $("prompt").value = promptText;
  // 反向词也要跟着回来（**任务记录那条路**）—— 回看一条任务时，表单上有个空的
  // 反向词会让人以为这张图本来就带着那段词。画廊那条路**刻意不传**它：见
  // `loadFromGallery`。
  if (p.negative_prompt != null) $("negative").value = p.negative_prompt;
  if (p.steps != null) $("steps").value = p.steps;
  if (p.seed != null) $("seed").value = p.seed;
  if (p.cfg != null) $("cfg").value = p.cfg;
  if (p.sampler) $("sampler").value = p.sampler;
  if (p.scheduler) $("scheduler").value = p.scheduler;
  // 源图 / 参考图 —— 同样是**任务记录那条路**要的：放大模式少了源图就提交不了，
  // 「载入了参数却没载入源图」看着像载入失败了。画廊那条路**不传**它（模式都不传，
  // 而这张控件只对图生图 / 放大有意义：填回去等于往一个当前模式收着的框里写，
  // 等用户切模式才突然冒出来）。
  // 多张时**以列表那份为准**（它才是真相，单数那个只是它的第一项）；
  // 只有单数（老记录）就退回去用它。
  const refs = Array.isArray(p.source_images) && p.source_images.length
    ? p.source_images
    : (p.source_image != null ? [p.source_image] : null);
  if (refs) state.refs = normalizeRefs(refs);
  renderPicked();
  // 放大倍数：下拉里只有几个常用档，历史里可能是别的值（比如 ×5）。
  // 那就现加一个 option，而不是把 select 置空 —— 置空会让表单静默变成 ×2。
  if (p.scale != null) {
    const sel = $("scale");
    if (![...sel.options].some((o) => Number(o.value) === Number(p.scale))) {
      sel.add(new Option("×" + p.scale, String(p.scale)));
    }
    sel.value = String(p.scale);
  }
  // 改写开关（**额外功能**，2026-10-01 起）：新记录直接带 `rewrite`；老记录带的是
  // `mode="refine"`，按它反推。画廊那条路两个都不传 —— 契约是「只拿数值」（P15）。
  if (p.rewrite != null) $("rewrite").checked = !!p.rewrite;
  else if (p.mode) $("rewrite").checked = (p.mode === "refine");
  if (p.lazy_cache != null) $("lazy_cache").checked = !!p.lazy_cache;
  if (p.te_speed != null) $("te_speed").checked = !!p.te_speed;
  // Turbo **不参与下面那条「二选一」**：它是权重补丁，与那两个加速节点都合得来。
  // 老记录没有这个键（`p.turbo == null`）时按「不动」处理 —— 与「缺键 = 不动」
  // 是同一条规则，别拿默认的 false 去覆盖用户当前勾着的状态。
  if (p.turbo != null) $("turbo").checked = !!p.turbo;
  // 两个加速开关插在同一条 model 连线上。构建器本来就互斥，所以历史图不会
  // 出现"两个都开"；真碰上了也不能让表单停在一个提交必然 400 的状态 ——
  // 以 TE-Speed 为准，与后端报错里的推荐一致。
  if ($("te_speed").checked && $("lazy_cache").checked) $("lazy_cache").checked = false;
  // 尺寸只对出图有意义；放大与图生图的尺寸由源图决定，硬填会把表单带到
  // 一个自相矛盾的状态（比如放大模式下「尺寸 2048×2048」亮着）。
  // 判据读**表单当前**的模式，不是传进来的 `p.mode` —— 画廊那条路已经不传模式了
  // （载入参数只拿数值），拿 `p.mode` 判会**恒真**，于是往图生图 / 放大模式里写
  // 尺寸，而那两栏此刻是收着的：写进去看不见，等用户切模式才突然出现。
  const currentMode = $("mode").value;
  if (p.width && p.height && currentMode !== "edit" && currentMode !== "upscale") {
    const v = p.width + "x" + p.height;
    const opt = [...$("size").options].find((o) => o.value === v);
    if (opt) { $("size").value = v; }
    else { $("size").value = "custom"; $("width").value = p.width; $("height").value = p.height; }
  }
  applyModeUI();
  $("prompt-len").textContent = $("prompt").value.length;
  $("negative-len").textContent = $("negative").value.length;
  refreshBuild();
}

function selectJob(id) {
  state.currentId = id;
  const job = state.jobs.get(id);
  applyParams((job && job.params) || {}, (job && job.prompt_text) || "");
  setResultTab("live", true);
  render();
}

/* ---------- 动作 ---------- */
function setRunMsg(text, isErr) {
  $("run-msg").innerHTML = text ? (isErr ? '<span class="bad">' + esc(text) + "</span>" : esc(text)) : "";
}

function newSeed() {
  $("seed").value = Math.floor(Math.random() * 2147483647);
}

async function run() {
  const body = collectBody();
  // 必填项按模式分 —— 放大要源图，出图要提示词。两边都要在**这里**挡一道，
  // 否则用户点下去只会收到后端一句 400。
  if (body.mode === "upscale") {
    if (!body.source_image) { setRunMsg("放大模式要先选一张源图", true); return; }
  } else if (!body.prompt || !body.prompt.trim()) {
    setRunMsg("提示词不能为空", true); return;
  }
  // 第一次点只是上膛，第二次才提交（后端也会拦，见 cost.confirm_policy）
  const c = state.build && state.build.estimate && state.build.estimate.confirm;
  if (c && c.required && !state.confirmArmed) {
    state.confirmArmed = true;
    renderRunButton();
    setRunMsg(c.text, true);
    return;
  }
  body.confirm = true;
  $("btn-run").disabled = true;
  setRunMsg("提交中…");
  try {
    const data = await postJSON("/api/run", body);
    // 本地先落地，别等 SSE —— 判据 1 的「一秒内看到步进」
    upsert(data.job_id, {
      job_id: data.job_id, mode: body.mode, status: "queued",
      created_at: Date.now(), params: body, prompt_text: body.prompt,
      outputs: [], estimate_s: data.estimate_s, node_classes: data.node_classes || {},
    });
    state.currentId = data.job_id;
    setResultTab("live", true);
    render();
    setRunMsg("已提交" + (data.queue_number != null ? "，队列号 " + data.queue_number : ""));
  } catch (e) {
    setRunMsg(e.message, true);
    if (e.payload && e.payload.job_id) {
      upsert(e.payload.job_id, { job_id: e.payload.job_id, status: "error", mode: body.mode,
        prompt_text: body.prompt, params: body, outputs: [],
        error: { message: e.payload.error, node_errors: e.payload.node_errors } });
      state.currentId = e.payload.job_id;
      render();
    }
  } finally {
    $("btn-run").disabled = false;
  }
}

async function cancelJob(id) {
  try {
    await postJSON("/api/jobs/" + encodeURIComponent(id) + "/cancel", {});
  } catch (e) { setRunMsg("取消失败：" + e.message, true); }
}

/* ---------- 后端开关：点一下它做什么（P20i）----------
   页面**不猜**就绪与否 —— 那个判断永远看后端的 `/system_stats`（后端算的）。
   这里只负责把「我点的那次」发出去，再把结果摆回那颗开关上。

   为什么错误不再上页面：这是用户定的形状（「只进日志台，开关变红，悬停提示」）。
   所以三件事必须一起成立，缺一件那句话就等于没说：
     ① 后端把拒绝/失败**写进日志**（`server.py::post_backend_stop` 与 `launch.py`），
        日志台的时间线里看得到原文 —— 那是**留存**的那一份；
     ② 开关变红（`data-state=error`）或者保持原样（外部后端被拒），
     ③ `title` 立刻带上那句原因 —— 那是**当场**的那一份。 */
function layoutOf(launch) {
  return (launch && launch.layout) || {};
}

async function toggleBackend() {
  const btn = $("be-switch");
  const view = switchView(state.backend);
  if (view.state === "starting" || view.state === "checking") return;
  const stopping = view.state === "on" || view.state === "degraded"
    || view.state === "external";
  if (stopping) return stopBackend(btn);
  return startBackend(btn);
}

async function startBackend(btn) {
  btn.disabled = true;
  btn.dataset.state = "starting";       // 立刻给反应；随后由 /api/backend 校准
  btn.title = "正在按铁律 1 的命令拉起后端…（进度与失败原因都在日志台）";
  try {
    await postJSON("/api/backend/start", {});
  } catch (e) {
    // 409 = 不该起（端口上有东西 / 缺 yaml / 已在启动中）。原因进 title，
    // 后端同时把它写进了工作台日志（日志台的时间线里有原文）。
    btn.dataset.state = "error";
    btn.title = String((e && e.message) || e);
  }
  await fetchBackend();
}

async function stopBackend(btn) {
  btn.disabled = true;
  try {
    await postJSON("/api/backend/stop", {});
  } catch (e) {
    // 两条拒绝：有作业在跑 / 不是工作台起的（还有「停不掉」）。
    // 「外部」那一条**不能把开关画成红的** —— 后端好着呢，是这次动作不算数。
    btn.title = String((e && e.message) || e);
    if (btn.dataset.state !== "external") btn.dataset.state = "error";
  }
  await fetchBackend();
}

/* 启动中那一秒一次的补读：走 `/api/backend/launch`（只读本进程的状态 + 日志尾），
   不走 `/api/backend` —— 后者要向后端要 system_stats 与模型体检，而后端此刻
   还没就绪，那是每秒钟一次 2 秒级的连接拒绝。 */
async function tickLaunch() {
  try {
    const launch = await getJSON("/api/backend/launch");
    if (!state.backend) return;
    state.backend.launch = launch;
    renderBackendSwitch();
  } catch (e) { /* 读不到状态不影响页面其余部分 */ }
}
/* ---------- 参考图：上传 + 后端能读到的清单 ---------- */
function renderInputs() {
  const d = state.inputs;
  if (!d) { $("src-info").textContent = "读不到 /api/inputs"; return; }
  const files = d.files;
  const bits = [];
  bits.push(d.verified ? "落点已核对" : "落点未核对");
  bits.push(d.root_label || "找不到 input 目录");
  // 以前这里写的是「N 张可选」—— 那时下面有个 datalist 让你按名字挑。那条路
  // 已经去掉（页面上能选的只有**上传**），所以改成陈述事实：后端 input/ 里
  // 现在有什么。这一行仍然有用，它是「上传到底落到哪」的唯一现场证据。
  bits.push(files ? "input/ 里现有 " + files.length + " 个文件" : "读不到文件列表");
  $("src-info").textContent = bits.join(" · ");
  $("src-info").title = (d.how || "") + (d.root ? "\n" + d.root : "");
  // 清单变了就重画一遍选中集：那些缩略图里「后端清单里有没有它」的标注
  // （`.missing`）就是拿这份清单算的，不重画就会停在上一轮的说法上 ——
  // 而「页面说没有、其实已经有了」正好是这一栏要消灭的症状。
  renderPicked();
}

async function fetchInputs() {
  try {
    state.inputs = await getJSON("/api/inputs");
    renderInputs();
  } catch (e) {
    $("src-info").innerHTML = '<span class="bad">读不了参考图清单：' + esc(e.message) + "</span>";
  }
}

function inputPreviewUrl(name) {
  // 走**后端**取图，而不是拿本地文件做预览：能不能取到本身就是个信号 ——
  // 取不到说明后端读不到它，而那正是这一栏要回答的问题。
  // 于是「刚上传的本地文件」与「从 input/ 里挑的已有文件」共用同一条路径，
  // 也就不需要 objectURL 那套创建/回收。
  return "/api/image?filename=" + encodeURIComponent(name) +
         "&subfolder=&type=input";
}

/* 拖进来的东西该按**哪张**算 —— 两条完全不同的来路，判据也不一样。
 *
 * 1. 从资源管理器 / 桌面拖进来的**文件**：`dt.files` 里的名字是真的，直接用。
 * 2. 从**本页**拖一张图（画廊格子、放大视图、已经选中的方块）：实测（真 CDP
 *    拖放，`logs/gallery_drag_probe.py` 里那份 `page_saw`）`dt.files` 里那个
 *    File 的名字是浏览器**自己合成**的 —— 对 `/api/gallery/image?root=0&name=X.png`
 *    这种地址，它按「URL 末段 + MIME 后缀」给出 **`image.png`**，与 X 毫无关系
 *    （格子上的缩略图那个口同理，合成出来的会是 `thumb.jpg`）。
 *
 *    这一条曾经是坏的，而且是**没有症状**的那种坏：每拖一张都叫 `image.png`，
 *    于是①后一张把前一张覆盖掉（`/api/upload` 默认 overwrite=1），②选中集按
 *    名字去重，第二张永远进不来。实测证据：用户拖过三张之后，后端 `input/` 里
 *    只有一个 `image.png`，而它的大小 1628581 正好等于**第二张**画廊图。
 *
 *    修法：名字与字节**都从 `text/uri-list` 取** —— 那里面 `name=` 才是这张图
 *    真正的文件名，字节则重新按 URL 取一次（不吃浏览器给的那一份）。名字错了
 *    后面全错；字节是从哪来的，也要能说清。
 */
function refFromUri(uri) {
  if (!uri) return null;
  // `text/uri-list` 允许多行（一行一个 URI）与 `#` 开头的注释行。
  const first = String(uri).split(/\r?\n/).map((s) => s.trim())
    .find((s) => s && s[0] !== "#");
  if (!first) return null;
  let url;
  try { url = new URL(first, location.href); } catch (e) { return null; }
  // **只认我们自己的两个取图口**：别的地址（外站、`file://`、随便一段文字）
  // 一律不猜 —— 猜来的名字会把图传成另一个名字，而那种错没有症状。
  if (url.origin !== location.origin) return null;
  const key = (url.pathname === "/api/gallery/image"
               || url.pathname === "/api/gallery/thumb") ? "name"
    : (url.pathname === "/api/image" ? "filename" : null);
  if (!key) return null;
  const raw = url.searchParams.get(key);
  if (!raw) return null;
  // 格子上画的现在是**缩略图**（P20l），而拖走的必须是**原图**：拿一张 512px 的
  // JPEG 当参考图是**没有症状**的降级 —— 传得上去、出得来图，只是少了一半像素，
  // 而用户以为自己拖的就是那一张。所以把地址**改写回取原图那个口**。
  if (url.pathname === "/api/gallery/thumb") url.pathname = "/api/gallery/image";
  const base = raw.split(/[\\/]/).pop().trim();   // 只要 basename；后端还会再挡一道
  return base ? { name: base, url: url.toString() } : null;
}

/** 松手之后到底传什么。异步：从本页拖来的那张要**重新取一次字节**。 */
async function takeDropped(dt) {
  // `getData` 只在 drop 事件**同步**那一段内有效，所以这两句必须在任何 await 之前。
  const files = Array.from((dt && dt.files) || []);
  const ref = refFromUri(dt && (dt.getData("text/uri-list") ||
                                dt.getData("text/html")));
  if (ref) {
    try {
      const url = new URL(ref.url);
      // 刚生成的图也要拿到**最新那份**：画廊那条路带 ETag，理论上会重新校验，
      // 但在这里赌缓存等于把「传上去的是哪张」交给一个我们看不见的层。
      url.searchParams.set("_", String(Date.now()));
      const r = await fetch(url.toString());
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      if (!blob.size) throw new Error("取回来是空的");
      await uploadRefs([new File([blob], ref.name,
                                 { type: blob.type || "image/png" })]);
      return;
    } catch (e) {
      $("upload-msg").innerHTML = '<span class="bad">这张图没能取到：' +
        esc(ref.name) + "（" + esc(e.message) + "）</span>";
      return;
    }
  }
  if (files.length) { await uploadRefs(files); return; }
  $("upload-msg").innerHTML = '<span class="bad">拖进来的东西里没有图片 —— ' +
    "从右边的画廊拖一张，或从文件夹里拖一个图片文件。</span>";
}

/* 选中集的**唯一真源**是 `state.refs`（下面那排方块只是它的视图）。
 *
 * 以前这个真源是页面上那个文本框的 value（「或直接填 input/ 里已有的文件名」），
 * 按用户要求那条路已经去掉：手打名字给的是**假路径**（名字对不上、图不在后端），
 * 而且它与上传抢同一份状态。现在只有一条写入路径 —— **上传回执** ——
 * 所以状态放在 JS 里，页面上没有它的影子。
 *
 * 为什么不反过来（拿 DOM 当真源）：上传、从历史回填（applyParams）两条路都要
 * 写它，只维护一个状态就不会出现「框里写着 a.png 而下面显示 b.png」这种
 * 自相矛盾 —— 而那种矛盾用户没法解释。
 *
 * 分隔符**只认逗号**（半角/全角/换行），不认空格：文件名里可以有空格
 * （`my photo.png`），按空格切会把它劈成两张不存在的图。数组输入没有这个问题，
 * 但老记录里的 `source_image` 是一串字符串，所以两种都要吃。
 */
function normalizeRefs(value) {
  const parts = Array.isArray(value)
    ? value
    : String(value == null ? "" : value).split(/[,，\n]+/);
  const seen = new Set();
  const out = [];
  for (const raw of parts) {
    const name = String(raw == null ? "" : raw).trim();
    if (!name || seen.has(name)) continue;   // 去重：同一张图挂两遍没有意义
    seen.add(name);
    out.push(name);
  }
  return out;
}

function setRefs(value) {
  state.refs = normalizeRefs(value);
  renderPicked();
}

/* 最多几张。**上限来自后端**（`/api/inputs` 里的 `max_references`，它又来自
 * `TextEncodeQwenImage21` 的 autogrow 候选名 image_1..image_16）——
 * 前端自己写死一个数就会与后端分家，而分家之后只有提交时才报错。 */
function maxRefs() {
  const n = state.inputs && state.inputs.max_references;
  return Number.isFinite(n) && n > 0 ? n : 16;
}

/* 把「当前选中的参考图」画出来 —— **大图方块**，排在虚线框**下面**。
 * 名字为空就整块收起来（虚线框本身永远在，它是稳定的入口）。
 *
 * 方块里只有图，**没有文件名**（按用户要求去掉）：文件名长短不一，一张卡片
 * 就被撑成一根长条，方阵排得七零八落。名字移到 `title` 与 `aria-label` ——
 * 悬停看得到、读屏念得出来，而且不占版面。
 *
 * 两个角标：右上 `×`（移除），左上**序号**（两张以上才出现）。
 * 序号不是装饰 —— **第一张决定输出尺寸**（见 nodes_qwen.py），多张时它是唯一
 * 的顺序线索，去掉名字之后更是只剩它了。
 *
 * `×` **平时不显示**（2026-10-01 按用户要求）：悬停到这一块上、或 Tab 到它才
 * 浮出来 —— 判据整个在 CSS 里（`style.css` 那条 `@media (hover: hover)`），
 * 这里一行 JS 都不加：隐藏的条件（有没有悬停）与显示的条件（键盘聚焦）本来
 * 就是同一条规则的两半，分成两处判，迟早就分家。 */
function renderPicked() {
  const box = $("ref-picked");
  const refs = state.refs;
  const known = state.inputs && state.inputs.files;
  if (!refs.length) {
    box.innerHTML = "";
    box.classList.add("hidden");
    $("btn-clear-ref").classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  $("btn-clear-ref").classList.remove("hidden");
  box.innerHTML = refs.map((name, i) => {
    // 后端清单里没有它 = 提交必然失败。**当场标出来**，别等提交时收一句 400。
    const missing = Array.isArray(known) && !known.includes(name);
    const why = missing ? "后端 input/ 里没有这个文件 —— 提交必然失败" : name;
    return '<span class="ref-chip' + (missing ? " missing" : "") +
      '" role="listitem" data-name="' + esc(name) + '" title="' + esc(why) + '">' +
      '<img class="ref-thumb" alt="" loading="lazy" src="' +
        esc(inputPreviewUrl(name)) + '">' +
      (refs.length > 1
        ? '<span class="ref-order" aria-hidden="true">' + (i + 1) + "</span>" : "") +
      '<button type="button" class="ref-remove" data-idx="' + i +
        '" aria-label="移除参考图 ' + esc(name) + '" title="移除">×</button>' +
      "</span>";
  }).join("");
  for (const img of box.querySelectorAll(".ref-thumb")) {
    // 取不到就画成占位块。**这本身是个信号**：名字在清单里、图却取不到，
    // 说明清单和磁盘不是一回事 —— 而那是这一栏最该暴露的东西。
    img.onerror = () => img.classList.add("broken");
  }
  for (const btn of box.querySelectorAll(".ref-remove")) {
    btn.onclick = () => {
      const rest = state.refs.slice();
      rest.splice(Number(btn.dataset.idx), 1);
      setRefs(rest);
      scheduleBuild();
    };
  }
}

/* 把一批文件传上去，然后**用后端回执里的名字追加进**选中集。
 *
 * 追加，不是替换（按用户要求改的）。原来的理由（「想换一张的人会静默拿到两张」）
 * 在**只有一个入口**时站得住，而现在参考图是**攒起来**用的：图生图常要把三五张
 * 素材凑齐，每选一次就把前面的清掉等于逼着用户一次全选对。
 *
 * 换一张仍然是明摆着的：方块右上角那个 `×`。
 *
 * 上限按**并集**判，不按这一批判 —— 否则「已有 15 张、再选 3 张」会先写进去
 * 再报错，留下一个半截的状态。装不下就整批不传，并说清楚还能放几张。 */
async function uploadRefs(files) {
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length) { $("upload-msg").textContent = "先选一个文件"; return; }
  const room = maxRefs();
  const have = state.refs.length;
  if (have + list.length > room) {
    $("upload-msg").innerHTML = '<span class="bad">最多 ' + room +
      " 张参考图（后端那个文本编码器的 autogrow 就到这里）：已经选了 " + have +
      " 张，这次再选 " + list.length + " 张会超 —— 这一批没上传。</span>";
    return;
  }
  const saved = [];
  const failed = [];
  for (const f of list) {
    $("upload-msg").textContent = "上传中…（" + f.name + "，" +
      Math.round(f.size / 1024) + " KB）";
    try {
      // 原始字节直接当 body：服务端收 request.body()，不需要 multipart
      const r = await fetch("/api/upload?filename=" + encodeURIComponent(f.name),
                            { method: "POST", body: f });
      let data = null;
      try { data = await r.json(); } catch (e) { /* 空体 */ }
      if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
      // **以回执里的文件名为准**：后端那条路撞上同名时可能改名
      // （`a (1).png`），拿本地的 f.name 填进表单就会指向一个不存在的文件。
      saved.push(data.filename);
      if (!data.verified) failed.push(data.filename + "（" + (data.warning || "没核对过") + "）");
    } catch (e) {
      failed.push(f.name + "（" + e.message + "）");
    }
  }
  if (saved.length) {
    // 并进已有的那一份。`setRefs` 顺带去重 —— 同名重传会**替换**后端那份文件
    // （`/api/upload` 默认 overwrite=1），所以名字撞上就等于同一张，不该出现两块。
    // **这条推理的前提是「名字真的是那张图的名字」** —— 从画廊拖进来的那一路曾经
    // 每张都叫 `image.png`，于是「撞上」变成了「所有图都撞上」（见 `refFromUri`）。
    // 名字错了，这条推理整条失效。
    setRefs(state.refs.concat(saved));
    await fetchInputs();
  }
  $("src-file").value = "";
  const bits = [];
  if (saved.length) bits.push('<span class="ok">已上传 ' + saved.length + " 张（现有 " +
    state.refs.length + " 张）：" + esc(saved.join("、")) + "</span>");
  if (failed.length) bits.push('<span class="bad">没成功：' + esc(failed.join("；")) + "</span>");
  $("upload-msg").innerHTML = bits.join(" · ") || "什么都没传";
  scheduleBuild();
}

/* 虚线框：点击开选择器、Enter/Space 同效、拖进来松手即传。
 *
 * dragenter/dragleave 用**计数器**：鼠标划过框里的图标与文字时，子元素会各发
 * 一次 leave，直接 toggle 会让高亮闪个不停。 */
function initDropzone() {
  const drop = $("ref-drop");
  let depth = 0;
  const pick = () => $("src-file").click();

  drop.onclick = pick;
  drop.addEventListener("keydown", (event) => {
    // 它的角色是按钮，键盘就得跟鼠标一样能开 —— 只挂 onclick 的话，
    // 键盘用户会卡在一个 tabindex="0" 却按不动的框上。
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pick();
    }
  });
  drop.addEventListener("dragenter", (event) => {
    event.preventDefault();
    depth += 1;
    drop.classList.add("dragover");
  });
  drop.addEventListener("dragover", (event) => event.preventDefault());
  drop.addEventListener("dragleave", () => {
    depth -= 1;
    if (depth <= 0) { depth = 0; drop.classList.remove("dragover"); }
  });
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    depth = 0;
    drop.classList.remove("dragover");
    // 判据在 `takeDropped` 里：拖进来的可能是**文件**（名字是真的），也可能是
    // **本页的一张图**（浏览器给的名字是合成的 `image.png`，见那个函数的长注释）。
    void takeDropped(event.dataTransfer);
  });

  // **拖歪了必须拦住。** 拖到框外松手，浏览器默认会**导航到那张图片** ——
  // 整页表单连同正在写的内容一起没了，而且没有任何提示。
  // 这里只 preventDefault、**不接管**：「拖歪」就该等于什么都没发生。
  for (const name of ["dragover", "drop"]) {
    document.addEventListener(name, (event) => event.preventDefault());
  }

  $("src-file").addEventListener("change", () => uploadRefs($("src-file").files));
  $("btn-clear-ref").onclick = () => {
    setRefs([]);
    $("upload-msg").textContent = "";
    scheduleBuild();
  };
}

async function fetchBackend() {
  try {
    const be = await api("/api/backend");
    state.backend = be;
  } catch (e) {
    state.backend = null;
  }
  renderBackendSwitch();
  // 日志台的「环境体检」用的是**这一份**数据（不另拉一条、也不另算一套），
  // 所以这里顺手刷一下 —— 那条 15 秒的定时器因此对两个视图都有效。
  if (logsOpen()) renderLogEnv();
}

async function fetchJobs() {
  try {
    const data = await api("/api/jobs?limit=60");
    // 「可清理几条」是**整本账**上的数（后端现算，磁盘是唯一真源），不是这一页上的。
    state.jobsStale = Number(data.stale) || 0;
    for (const j of data.jobs || []) upsert(j.job_id, j);
    if (!state.currentId) {
      const active = (data.jobs || []).find((j) => j.status === "queued" || j.status === "running");
      if (active) state.currentId = active.job_id;
    }
    render();
  } catch (e) { /* SSE 快照会补上 */ }
}

/* ---------- SSE ---------- */
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => { $("sse-state").textContent = "实时连接"; };
  es.onerror = () => { $("sse-state").textContent = "连接断开，重连中…"; };

  es.addEventListener("snapshot", (e) => {
    const d = JSON.parse(e.data);
    for (const j of d.jobs || []) upsert(j.job_id, j);
    if (!state.currentId && (d.active || []).length) state.currentId = d.active[0];
    render();
  });

  const on = (type, fn) => es.addEventListener(type, (e) => {
    fn(JSON.parse(e.data)); render();
  });

  on("job_queued", (d) => upsert(d.job_id, d.job || { status: "queued" }));
  on("job_started", (d) => upsert(d.job_id, {
    status: "running", started_at: Date.now(), mode: d.mode }));
  on("node", (d) => upsert(d.job_id, {
    status: "running", node_id: d.node_id, class_type: d.class_type }));
  on("progress", (d) => upsert(d.job_id, {
    status: "running", node_id: d.node_id,
    progress: { value: d.value, max: d.max } }));
  on("cached", (d) => upsert(d.job_id, { cached_nodes: d.nodes || [] }));
  on("output", (d) => {
    const job = upsert(d.job_id, {});
    mergeOutputs(job, d.outputs);
  });
  on("job_done", (d) => {
    const job = upsert(d.job_id, {
      status: "done", wall_ms: d.wall_ms, ended_at: Date.now(), progress: null });
    mergeOutputs(job, d.outputs);
    // 新图落盘了 —— 让画廊跟上，不用手点「重扫」
    clearTimeout(state.galleryTimer);
    state.galleryTimer = setTimeout(fetchGallery, 800);
    // 如果这张是扫描里的某一格，并排图要立刻补上它（别等 3 秒的轮询）
    refreshSweepIfRunning();
  });
  on("job_error", (d) => upsert(d.job_id, {
    status: "error", error: { message: d.message || "执行出错", node_type: d.node_type },
    progress: null }));
  // 出图后自动放大那条链的脚印：提交成功、或者**失败**，都落在父作业上。
  // 失败尤其要推 —— 那时候父作业已经是 done 了，不给它一个落点，
  // 「怎么没放大」就只能靠用户自己猜。
  on("job_chain", (d) => upsert(d.job_id, { chain: d.chain || null }));
  // 扫描是**提交者**发的事件，但对另一个开着的标签页就是唯一的通知 ——
  // 只靠 HTTP 响应的话，第二个标签页要等到轮询才发现有这次扫描。
  on("sweep_queued", (d) => {
    state.sweepId = d.sweep_id;
    fetchSweep(d.sweep_id);
  });
  on("sweep_failed", (d) => {
    fetchSweep(d.sweep_id);
    $("sweep-msg").innerHTML = '<span class="bad">扫描作废（第 ' +
      esc(d.at + 1) + "/" + esc(d.total) + " 格）：" + esc(d.message) + "</span>";
  });
  // --- 后端开关（P6 起，P20i 换成那颗开关）---
  // 四条事件只做一件事：**重新读一次状态**。启动的正文（那段 stdout）归日志台 ——
  // 它读的是同一个日志文件（launcher 抄的那份），页面这边不再留第二份副本。
  on("backend_launching", () => { tickLaunch(); });
  on("backend_ready", () => { fetchBackend(); });
  on("backend_failed", () => { fetchBackend(); });
  // 「停掉」既改开关（`fetchBackend` 直接读 `/api/backend`），也**同时**让别的
  // 标签页跟上 —— 这颗开关开着的每个页面都得知道后端没了。
  on("backend_stopped", () => { fetchBackend(); });
  on("job_cancelled", (d) => upsert(d.job_id, { status: "cancelled", progress: null }));
  on("status", (d) => { state.queueRemaining = d.queue_remaining; renderQueue(); });
  on("external_job", () => { state.external = Date.now(); renderQueue(); });
  on("backend", (d) => {
    if (state.backend) state.backend.running = d.running;
    fetchBackend();
  });
  return es;
}

function renderQueue() {
  const bits = [];
  if (state.queueRemaining != null) bits.push("队列 " + state.queueRemaining);
  if (state.external && Date.now() - state.external < 180000) {
    bits.push("后端在跑工作台之外提交的任务");
  }
  $("queue-info").textContent = bits.join(" · ");
}

/* ---------- 画廊（P2）---------- */
/* 画廊直接扫磁盘，所以**后端没起也看得见历史**（模式/seed/步数/尺寸/提示词都
   在 PNG 自己的 tEXt 里）。图来自多个输出目录，逐张标注来源。 */
const GALLERY_CHIPS = [
  ["", "全部", (c) => c.all],
  ["t2i", "T2I", (c) => c.t2i],
  ["edit", "Edit", (c) => c.edit],
  ["2k", "2K", (c) => c.is_2k],
  // 「改写」与模式**正交**：一张图可以既是 T2I 又是改写过的。所以它走自己的
  // 查询参数（`rewrite=1`），不是 `mode` 的一个取值 —— 2026-10-01 之前那个
  // `refine` chip 是模式，重构之后它不再是一个模式了。
  ["rewrite", "改写", (c) => c.rewrite],
  ["__foreign", "非本链路", (c) => c.unrecognized],
];

/* 取图有**两个口，参数一模一样**，差别只在发的是哪一份字节：

 *   · `/api/gallery/image` —— 原图（放大视图、1:1 原始像素、拖去做参考图都用它）；
 *   · `/api/gallery/thumb` —— 格子上那张小图（长边 512 的 JPEG，服务端缓存）。
 *
 * 为什么要有第二个口：一页 18 格的原图实测合计 **65.4 MB**（最大一张 4096² /
 * 18.5 MB），浏览器每张都整份下、整份解码 —— 用户报的「有高分辨率的作品时页面
 * 卡顿」就是它。同一页缩略图合计 0.71 MB。
 *
 * **别把这两个口混着用**：拿缩略图当原图是**没有症状**的降级（传得上去、出得来
 * 图，只是少了一半像素）。所以 `refFromUri()` 会把拖来的缩略图地址**改写回原图**。
 */
function galleryUrl(it, thumb, archived) {
  // 归档档里的图**不在** `candidate_roots()` 里（正常档正是靠这一点看不见它们），
  // 所以取图那条路也得跟着换根 —— 否则归档档的每一格都 404，而更坏的一种是
  // **取到 output/ 里同名的另一张**（同名在这里是真实会发生的：ComfyUI 的下一个
  // 序号是扫现存文件名算的，归档走一张之后 CLI 会再写一张同名的）。
  //
  // 第三个数是**归档档的显式覆盖**，默认跟 `state.galleryArchived`（那是「画廊视图
  // 正翻到哪一档」）。放大视图里画的是**刚跑出来的产物**，它在当前的输出目录里，
  // 与画廊翻到哪一档毫无关系 —— 共用那个状态，会让「翻过归档档后再点开一个作业」
  // 去归档目录里找这张图，而它就在眼前那个目录里。
  //
  // 做成**参数**而不是另写一个函数：那个三元的拼法只许有一处。各写一份的话
  // `&archived=1` 迟早只加在一处，而症状是「归档档里格子全 404、放大视图却是好的」
  //（`FrontendContract` 里有判据钉着这条拼法只出现一次）。
  const inArchive = archived === undefined ? state.galleryArchived : archived;
  return "/api/gallery/" + (thumb ? "thumb" : "image") +
         "?root=" + encodeURIComponent(it.root) +
         "&name=" + encodeURIComponent(it.filename) +
         (inArchive ? "&archived=1" : "");
}

function galleryImageUrl(it) { return galleryUrl(it, false); }

function galleryThumbUrl(it) { return galleryUrl(it, true); }

/* ---------------------------------------------------------------------------
 * 分页与多选（P20f）
 *
 * 页大小 = **实测量的列数 × 3 行**，不是写死一个数：`.gallery` 是
 * `repeat(auto-fill, minmax(min(100%, 180px), 1fr))` —— 窗口一窄它就掉到
 * 一列（窄屏断点里更是写死一列），写死 18 张在窄屏上就是 18 行，
 * 而「每页 3 行」是用户点名要的东西。
 *
 * 量法是**数同排的格子**：渲染完以后，与第一格 `offsetTop` 相同的连续格子数
 * 就是列数。比去解析 `grid-template-columns` 可靠（那里可能回 "none"）。
 *
 * 面板藏着的时候（`display:none`）所有格子的 `offsetTop` 都是 0，量出来会是
 * 「格子总数」—— 一个**看起来很像成功**的假读数。所以先看 `offsetParent`：
 * 拿不到就返回 null，调用方沿用上一次的值（或那个保守的兜底）。
 * ------------------------------------------------------------------------- */
const GALLERY_ROWS = 3;
const GALLERY_COLS_FALLBACK = 6;

function galleryCols() {
  const el = $("gallery");
  if (!el || !el.offsetParent) return null;      // 藏着 / 还没排版：量到的是假的
  const tiles = el.querySelectorAll(".tile");
  if (!tiles.length) return null;
  const top = tiles[0].offsetTop;
  let n = 0;
  for (const tile of tiles) {
    if (tile.offsetTop !== top) break;
    n++;
  }
  // 这一排就是**全部**格子 —— 那它可能根本没排满（2 张图摆在 5 列里，量出来是
  // 2），列数量不出来。宁可沿用上一次的值：拿「这一页剩下的张数」当列数去算，
  // 页会越缩越小（最后一段只有 1 张图时，页大小会变成 3）。
  if (n === tiles.length) return null;
  return n || null;
}

function galleryPageSize() {
  return (state.galleryCols || GALLERY_COLS_FALLBACK) * GALLERY_ROWS;
}

/* 布局给出的列数变了（切到画廊 / 窗口 resize）→ 重新量、必要时重取。
 * 只在**真的变了**的时候才发请求：resize 会连着触发很多次。 */
function remeasureGallery() {
  const cols = galleryCols();
  if (!cols || cols === state.galleryCols) return;
  state.galleryCols = cols;
  // 页大小一变，页码对应的那一段内容就整个挪位了 —— 回到第 1 页，
  // 而不是停在一个含义已经变掉的页码上。
  state.galleryPage = 0;
  fetchGallery();
}

async function fetchGallery() {
  const limit = galleryPageSize();
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(state.galleryPage * limit),
  });
  const chip = state.galleryMode;
  if (chip === "__foreign") params.set("recognized", "0");
  else if (chip === "2k") params.set("is_2k", "1");
  else if (chip === "rewrite") params.set("rewrite", "1");
  else if (chip) params.set("mode", chip);
  if (state.galleryQuery) params.set("q", state.galleryQuery);
  // 「已归档」换的是**扫哪个目录**（服务端换一个根），不是在这一堆里挑 —— 所以它
  // 与 chip 不是一回事，也不能被 chip 表示。
  if (state.galleryArchived) params.set("archived", "1");
  try {
    const data = await api("/api/gallery?" + params.toString());
    // 页码越界了（刚删空 / 页数变少）：退到最后一页重取，别显示一个空列表 ——
    // 空列表与「这里本来就没图」长得一模一样。
    const pages = Math.max(1, Math.ceil((data.total || 0) / limit));
    if (state.galleryPage > pages - 1) {
      state.galleryPage = pages - 1;
      return fetchGallery();
    }
    state.gallery = data;
    // 「能不能删」由后端说（它要去看 send2trash 在不在）—— 页面不猜。判据落在这
    // 里而不是点击后：一颗点了才知道坏掉的按钮，用户只会以为是自己弄错了。
    state.galleryCanDelete = data.can_delete !== false;
    state.galleryDeleteHint = data.delete_hint || "";
    // **哪一档也由后端说**（回执里那个 `archived`）：页面跟着回执走，就不存在
    // 「按钮以为在归档档、数据却是正常档」这种分家。
    state.galleryArchived = data.archived === true;
    // **选中集活不过一次刷新 / 翻页 / 换筛选 / 重扫**，这是刻意的：留下来就会
    // 出现「你看不见、但已经被勾中」的图，而下一步是删除。宁可让用户多勾一次。
    state.galleryPick.clear();
    renderGallery(data);
    scheduleGalleryFit();
  } catch (e) {
    $("gallery").innerHTML = '<p class="bad">读不到画廊：' + esc(e.message) + "</p>";
  }
}

/* 工具条上那几颗按钮此刻长什么样。
 *
 * 两档的语义**完全不同**，所以必须分支，而不是同一句话换个词：
 *   · 正常档：一颗「归档」= 把图搬进归档目录；
 *   · 归档档：两颗 —— 「还原」与「彻底删除」（后者才轮到回收站，才需要 send2trash）。
 *
 * 有一条**容易写错的**：`can_delete === false`（没装 send2trash）不该让「归档」也灰掉。
 * 归档只是一次改名，跟回收站那条路一点关系没有 —— 一视同仁地套上去，会让这台机器上
 * 唯一一个「删了还能回来」的入口平白消失，而它本来是好用的。
 */
function syncPickUI() {
  const n = state.galleryPick.size;
  const archived = state.galleryArchived;
  const del = $("btn-delete");
  del.textContent = (archived ? "彻底删除选中的 " : "归档选中的 ") + n + " 张";
  del.disabled = n === 0 || (archived && state.galleryCanDelete === false);
  // 禁用要说出理由 —— 一颗灰着的按钮不解释自己，用户只会以为坏了。
  del.title = archived
    ? (state.galleryCanDelete === false
        ? (state.galleryDeleteHint || "这台机器上彻底删除不可用")
        : (n ? "从归档里彻底删掉（进回收站，仍可还原）" : "先勾选要彻底删除的图"))
    : (n ? "搬进归档目录（可在「已归档」那一档里还原）" : "先勾选要归档的图");
  const restore = $("btn-restore");
  restore.hidden = !archived;
  restore.textContent = "还原选中的 " + n + " 张";
  restore.disabled = n === 0;
  restore.title = n ? "搬回 output/（撞名的那几张会单独报出来）" : "先勾选要还原的图";
  for (const tile of $("gallery").querySelectorAll(".tile")) {
    const on = state.galleryPick.has(tile.dataset.id);
    tile.classList.toggle("sel", on);
    // 勾选态要跟着走：**全选是就地改状态、不重建 DOM 的**，只刷那颗格子的
    // 边框而不管框里那个勾，用户看到的会是「边框亮着、勾没打上」。
    const box = tile.querySelector("input[data-pick]");
    if (box) box.checked = on;
  }
  const tab = $("btn-archived");
  if (tab) {
    tab.setAttribute("aria-pressed", archived ? "true" : "false");
    tab.classList.toggle("on", archived);
  }
  $("btn-pick-all").disabled = !$("gallery").querySelectorAll(".tile").length;
  $("btn-pick-none").disabled = n === 0;
}

function setPicked(id, on) {
  if (on) state.galleryPick.add(id);
  else state.galleryPick.delete(id);
  syncPickUI();
}

function pickAllOnPage() {
  for (const tile of $("gallery").querySelectorAll(".tile")) {
    state.galleryPick.add(tile.dataset.id);
  }
  syncPickUI();
}

function galleryMsg(text, bad) {
  const el = $("gallery-msg");
  el.textContent = text || "";
  el.classList.toggle("bad", !!bad);
}

/* 勾中的那几张做一件事。三种动作（归档 / 还原 / 彻底删除）**共用这一条路径** ——
   差别只有端点和那句回话。各写一份的话，三种动作的报账口径迟早分家，而它们**全都
   会动磁盘上的真文件**。

   三件事与后端约定死了（与 P20f 那条删除逐条相同）：
   · 请求体从**回执里的字段**取（不是去解析 `state.gallery.items` 的 id 字符串 ——
     那是内部约定，不是接口）。归档要给 `root`；还原与彻底删除**不给**：归档里的
     东西只有一个来源，多一个能被伪造成数字的参数就是多一条路；
   · 回执里的 `archived` / `restored` / `purged` 才算成了，`failed` 每条都带理由；
   · 做完**重取当前页**（磁盘才是真源），页空了自己会退回上一页。

   回话只说一件事：几张成了。**不列文件名** —— 那是用户刚在屏幕上勾过的东西，再念
   一遍只是噪音；而没成的那几张必须点名，因为那是他需要处理的。
*/
const PICK_ACTIONS = {
  archive: { url: "/api/gallery/archive", busy: "正在把 N 张移进归档目录…" },
  restore: { url: "/api/gallery/restore", busy: "正在把 N 张搬回 output/…" },
  purge: { url: "/api/gallery/purge", busy: "正在彻底删除 N 张…" },
};

function pickReplyText(kind, r) {
  const mb = (r.bytes || 0) / 1048576;
  if (kind === "archive") {
    return r.count + " 张 → 已归档（可还原" +
           (r.count ? "，" + mb.toFixed(1) + " MB" : "") + "）";
  }
  if (kind === "restore") {
    return r.count + " 张 → 已还原回 output/" +
           (r.count ? "（" + mb.toFixed(1) + " MB）" : "");
  }
  return r.count + " 张 → 回收站（仍可还原" +
         (r.count ? "，" + mb.toFixed(1) + " MB" : "") + "）";
}

async function pickedAction(kind) {
  const spec = PICK_ACTIONS[kind];
  const items = [];
  for (const it of (state.gallery.items || [])) {
    if (!state.galleryPick.has(it.id)) continue;
    items.push(kind === "archive"
      ? { root: it.root, name: it.filename }
      : { name: it.filename });
  }
  if (!items.length) return;
  const btn = kind === "restore" ? $("btn-restore") : $("btn-delete");
  btn.disabled = true;
  galleryMsg(spec.busy.replace("N", String(items.length)));
  try {
    const r = await postJSON(spec.url, { items: items });
    const failed = r.failed || [];
    let text = pickReplyText(kind, r);
    if (failed.length) {
      text += "；" + failed.length + " 张没成：" +
        failed.slice(0, 5).map((f) => (f.name || "?") + "（" + f.why + "）").join("、") +
        (failed.length > 5 ? "…" : "");
    }
    await fetchGallery();
    // 任务列表也跟着刷：归档与还原都会改变「最近任务」里那一行在不在（P20h）。
    // 不刷的话，用户会看着一条明明已经归档的记录继续躺在那里 —— 而它这时点开
    // 是取不到图的（那张图已经不在 output/ 里了）。
    fetchJobs();
    galleryMsg(text, failed.length > 0);
  } catch (e) {
    syncPickUI();               // 让按钮回到它**该**在的样子，不由这里说了算
    galleryMsg("没做成：" + e.message, true);
  }
}

function renderGallery(data) {
  const focusedImage = document.activeElement?.closest(".tile")?.dataset.id;
  const focusedChip = document.activeElement?.closest(".chip")?.dataset.mode;
  const counts = data.counts || {};
  // 归档档**第一眼就得看出是另一档**：同一排格子、同一套按钮位置，只是那几个词换了
  // —— 不说的话，用户会以为「刚归档掉的那些图怎么又回来了」。
  $("gallery-count").textContent = data.scanned
    ? (data.archived
        ? "（" + data.scanned + " 张已归档）"
        : "（" + data.scanned + " 张，其中 " + (counts.recognized || 0) +
          " 张是本链路）")
    : "";
  renderChips(counts);
  if (focusedChip != null) {
    for (const el of $("gallery-chips").querySelectorAll(".chip")) {
      if (el.dataset.mode === focusedChip) el.focus({ preventScroll: true });
    }
  }

  const roots = (data.roots || []).filter((r) => r.exists);
  $("gallery-roots").textContent = roots.length
    ? "扫描 " + roots.map((r) => r.label + "（" + r.path + "）").join("；")
    : "没有找到任何存在的输出目录 —— 出过图吗？";

  const items = data.items || [];
  if (!items.length) {
    $("gallery").innerHTML = '<p class="dim">没有匹配的图。</p>';
    // 空页也得把工具条与分页条刷一遍：否则「删除选中的 3 张」会留在一个已经
    // 没有格子的页面上，而那 3 张其实已经不在眼前了。
    renderPager(data);
    syncPickUI();
    return;
  }
  $("gallery").innerHTML = items.map((it) => {
    const badges = [];
    if (it.mode) badges.push('<span class="pill ' + esc(it.mode) + '">' +
                             esc(MODE_LABEL[it.mode] || it.mode) + "</span>");
    if (it.is_2k) badges.push('<span class="pill">2K</span>');
    // 「改写」是**额外功能**的标记（2026-10-01 起），与模式那两颗正交 ——
    // 老 refine 产物靠 PNG 里的 `switch.switch` 反推出来，所以历史图照样有。
    if (it.rewrite) badges.push('<span class="pill">改写</span>');
    if (!it.recognized) badges.push('<span class="pill warnpill">非本链路</span>');
    if (it.has_timings && it.run) badges.push('<span class="pill done">' +
      esc(fmtElapsed(it.run.wall_ms)) + "</span>");
    else if (it.run_match === "conflict") badges.push(
      '<span class="pill warnpill" title="文件名与运行记录撞了，不敢显示耗时">耗时存疑</span>');

    // 参数行 / 提示词 / 来源目录**不再上格子**（2026-10-01 按用户要求）：三行字
    // 挤在 180px 宽的格子里谁也读不完，而它们在放大视图里一字不少。
    // 「元数据读不出来」原来也在这三行里，它是**唯一在别处没有**的一条（那种图
    // 只能画成 `?` 占位块），所以升格成一颗 pill —— 摘掉它，用户看到的就是一个
    // 没有解释的问号。
    if (it.unreadable) badges.push('<span class="pill warnpill">元数据读不出来</span>');

    const img = it.unreadable
      ? '<span class="tile-blank">?</span>'
      // 格子画的是**缩略图**（`galleryThumbUrl`）。`width`/`height` 不用写：
      // CSS 里 `.tile img` 是 `width:100%` + `aspect-ratio:1`，尺寸与占位早就定了，
      // 写进去只是同一个大小的第二份说法。
      : '<img loading="lazy" decoding="async" alt="' + esc(it.filename) +
        '" src="' + galleryThumbUrl(it) + '">';
    /* 一张图 = 三个**各自独立**的动作，所以格子不再是「一整块按钮」。
     *
     * 上一版整块格子是一个 <button>，点哪儿都是「载入参数」。图多了之后
     * 最常用的两个动作反而没有入口：想看清楚这张图（点一下图）和想把提示词
     * 拿走（点一句话）都做不到 —— 而这两件事都与「把参数填回表单」无关，
     * 一个把表单覆盖掉的动作不该占着整块。 */
    /* 第四件事是「选中它」（P20f）。勾选框**不在** `.tile-shot` 里面 ——
     * `<button>` 里塞 `<input>` 是非法结构，而且点勾选框会顺手触发它的父按钮
     * （于是勾一张图就弹出放大视图）。做成子元素、`data-pick` 单独派发，
     * 两条路互不打扰。 */
    return '<article class="tile' + (state.galleryPick.has(it.id) ? " sel" : "") +
        '" data-id="' + esc(it.id) + '">' +
      '<label class="tile-pick" title="选中这张"><input type="checkbox" data-pick="' +
        esc(it.id) + '"' + (state.galleryPick.has(it.id) ? " checked" : "") +
        ' aria-label="选中 ' + esc(it.filename) + '"></label>' +
      '<button type="button" class="tile-shot" data-act="zoom" aria-label="放大查看 ' +
        esc(it.filename) + '" title="' +
        esc(it.filename + "（" + (it.root_label || "") + "）· 点一下放大") + '">' + img +
        '<span class="tile-zoom" aria-hidden="true">放大</span></button>' +
      '<div class="tile-caption"><span class="tile-badges">' + badges.join("") +
      "</span></div>" +
      '<div class="tile-actions">' +
        // 「载入」这一族（2026-10-01 按用户要求）：点一下把这张图的东西**自动填回
        // 它自己的位置** —— 提示词两段各回各的文本框，数值回数值栏。格子只做这件事；
        // 复制那一族留在放大视图（那里有地方，也是「先看清楚再拿走」那一个）。
        '<button type="button" class="ghost" data-act="loadprompt"' +
          (it.prompt ? ""
                     : ' disabled data-why="empty"'
                       + ' title="这张图的元数据里没有提示词"') +
          ">载入提示词</button>" +
        '<button type="button" class="ghost" data-act="load"' +
          (it.recognized ? "" : ' title="非本链路的图，参数对不上本表单"') +
          ">载入参数</button>" +
      "</div></article>";
  }).join("");

  for (const el of $("gallery").querySelectorAll(".tile")) {
    const id = el.dataset.id;
    const found = () => (state.gallery.items || []).find((x) => x.id === id);
    for (const btn of el.querySelectorAll("[data-act]")) {
      const act = btn.dataset.act;
      btn.onclick = () => {
        const item = found();
        if (!item) { fetchGallery(); return; }   // 图被别人删了：清单已经过期
        if (act === "zoom") openLightbox(item.id);
        else if (act === "loadprompt") loadPromptsFromGallery(item);
        else loadFromGallery(item);
      };
    }
    // 勾选框走**自己的**一条线（`data-pick`，不在 `[data-act]` 那张派发表里）：
    // 那颗表的兜底分支是「载入参数」，勾一下就会把表单覆盖掉。
    const box = el.querySelector("[data-pick]");
    if (box) {
      box.onchange = (event) => {
        setPicked(id, event.target.checked);
        galleryMsg("");           // 上一次删除的报账留在屏幕上会看着像这次的结果
      };
    }
    if (id === focusedImage) {
      const shot = el.querySelector(".tile-shot");
      if (shot) shot.focus({ preventScroll: true });
    }
  }
  syncPickUI();
  renderPager(data);
  // 「载入提示词」要不要禁用，取决于**表单此刻的模式**（放大模式没有提示词输入框），
  // 而格子是重新渲染出来的 —— 不在这里刷一遍，刚切完模式的那几颗会停在错的状态。
  syncPromptLoadButtons();
}

/* 渲染完再量一次列数，量出来与刚才用的页大小不一样就重取一页。
 *
 * 为什么**不写在 `renderGallery` 里**：那个函数也会被别处直接调用（判据往
 * `state.gallery.items` 里塞两条再渲染一把），在那里发请求会把刚塞进去的东西
 * 顺手冲掉 —— 2026-10-01 就是这么把第 18 步整段弄红的（注入的两格被一次
 * 重取换成了真图，而判据读到的提示词属于另一张图）。
 * 布局校正只属于「刚从服务端取回来」这条路。
 *
 * `setTimeout(0)` 是为了让这一帧的排版先落地。连改三次还量不准就不再改了 ——
 * 列数来回摆动时，无限重取会把 106 张的扫描反复叫起来（那才叫真的卡）。
 */
function scheduleGalleryFit() {
  setTimeout(() => {
    const cols = galleryCols();
    if (!cols || cols === state.galleryCols) return;
    if (state.galleryFits >= 3) return;
    state.galleryCols = cols;
    state.galleryFits += 1;
    if (cols * GALLERY_ROWS !== (state.gallery || {}).limit) {
      state.galleryPage = 0;
      fetchGallery();
    }
  }, 0);
}

function renderPager(data) {
  const limit = data.limit || galleryPageSize();
  const total = data.total || 0;
  const pages = Math.max(1, Math.ceil(total / limit));
  const page = Math.min(state.galleryPage, pages - 1);
  // 只有一页时整条收起来：一个「第 1/1 页」的条子不说明任何事，还占一行。
  $("gallery-pager").hidden = pages <= 1;
  $("gallery-page-info").textContent =
    "第 " + (page + 1) + " / " + pages + " 页 · 本页 " + (data.items || []).length +
    " 张 · 共 " + total + " 张";
  $("btn-page-prev").disabled = page <= 0;
  $("btn-page-next").disabled = page >= pages - 1;
}

function goToPage(delta) {
  state.galleryPage = Math.max(0, state.galleryPage + delta);
  galleryMsg("");
  fetchGallery();
}

function renderChips(counts) {
  $("gallery-chips").innerHTML = GALLERY_CHIPS.map(([value, label, count]) => {
    const n = count ? count(counts) : 0;
    return '<button class="chip' + (state.galleryMode === value ? " on" : "") +
      '" data-mode="' + esc(value) + '">' + esc(label) +
      (n ? ' <span class="dim">' + n + "</span>" : "") + "</button>";
  }).join("");
  for (const el of $("gallery-chips").querySelectorAll(".chip")) {
    el.onclick = () => {
      state.galleryMode = el.dataset.mode;
      state.galleryPage = 0;    // 换了筛选，旧页码没有意义（见 btn-pick-all 那段）
      fetchGallery();
    };
  }
}

/* 判据 3 的「点一下能把参数填回表单」。
 *
 * 2026-10-01 按用户要求**收窄成「只拿数值参数」**：提示词、反向词、模式（含
 * `2K` 那个折叠）、参考图 / 源图、加速开关、放大倍数**一律不传**，表单停在
 * 用户自己摆好的状态上。
 *
 * 为什么：这个按钮以前是把表单**整个覆盖**掉的动作，而它最常被用的地方是
 * 「看见一张喜欢的图，想拿它的参数配**我自己的**提示词」—— 提示词、模式、
 * 参考图被悄悄换掉时，用户往往到出图之后才发现（本工作区最忌讳的那种
 * **没有症状**的错）。分工也因此变清楚了：旁边的「复制提示词」负责拿词，
 * 这个负责拿数；两边都在格子和放大视图里各有一个入口。
 *
 * 怎么做到「不传」：`applyParams` 里每一条都是 `if (x != null)`，**缺键 = 不动**。
 * 唯一的例外是尺寸 —— 它那条守卫要读表单当前的模式（见 `applyParams`）。
 */
/* 「载入提示词」：与「载入参数」一个意思 —— 点一下，这张图里的东西**各回各的位置**。
 * 数值回数值栏（`loadFromGallery`），提示词两段回两个文本框（这个函数）。
 *
 * 两条口径（2026-10-01 按用户要求定的）：
 *   · **有才写**：这张图没有反向词就**不动**那个框 —— 与「载入参数」的「缺键 = 不动」
 *     是同一条规则（覆盖掉用户手里正改的东西得有理由，而「这张图没有」不是理由）；
 *   · 放大模式下整颗按钮**禁用**（那是唯一没有提示词输入框的模式）：写进一个藏着的框
 *     等于没有症状的错 —— 切模式才突然冒出来。
 *
 * 它**不碰**模式、参数与参考图：那些是另外两颗按钮的事。
 */
function loadPromptsFromGallery(item) {
  if ($("mode").value === "upscale") {
    setRunMsg("放大模式没有提示词输入框，切回文生图 / 图生图再载入", true);
    return;
  }
  if (!item.prompt) {
    setRunMsg("这张图的元数据里没有提示词", true);
    return;
  }
  // `null` = 不动那个框（`applyParams` 里就是按「非 null 才写」判的）
  applyParams({negative_prompt: item.negative_prompt || null}, item.prompt);
  setRunMsg("已从 " + item.filename + " 载入" +
            (item.negative_prompt ? "提示词与反向词" : "提示词") +
            "；模式、参数、参考图都没动");
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  $("control-scroll").scrollTo({ top: 0, behavior });
  if (window.matchMedia("(max-width: 1023px)").matches) window.scrollTo({ top: 0, behavior });
}

/* 「载入提示词」在放大模式下禁用（2026-10-01 按用户要求）。判据是**表单此刻的模式**，
 * 所以两处都要刷：切模式（`applyModeUI`）与重画（`renderGallery` / `renderLightbox`）。
 * `data-why="empty"` 是另一条禁用理由（这张图没有提示词），它比模式那条更具体，
 * 而且与模式无关 —— 遇上它就跳过，别把理由换了。 */
function syncPromptLoadButtons() {
  const off = $("mode").value === "upscale";
  const why = "放大模式没有提示词输入框，切回文生图 / 图生图再载入";
  for (const el of [...document.querySelectorAll('[data-act="loadprompt"]'),
                    $("lb-load-prompt")]) {
    if (!el || el.dataset.why === "empty") continue;
    el.disabled = off;
    if (off) el.title = why; else el.removeAttribute("title");
  }
}

function loadFromGallery(item) {
  if (!item.recognized) {
    setRunMsg("这张不是 Qwen-Image-2.1 链路的图，参数对不上本表单", true);
    return;
  }
  applyParams({
    steps: item.steps, seed: item.seed, cfg: item.cfg,
    sampler: item.sampler, scheduler: item.scheduler,
    width: item.width, height: item.height,
  }, null);
  // 报出**这次真的拿到了什么**，而不是把键名背一遍：一张放大图（VOSR2）只有
  // seed，空格子和采样器都是 null；照背键名会让人以为步数也被换了。
  const got = [];
  if (item.steps != null) got.push("步数");
  if (item.seed != null) got.push("seed");
  if (item.cfg != null) got.push("CFG");
  if (item.sampler) got.push("采样器");
  if (item.scheduler) got.push("调度器");
  if (item.width && item.height) got.push("尺寸");
  setRunMsg("已从 " + item.filename + " 载入参数（" +
            (got.length ? got.join(" / ") : "没有可载入的数值") +
            "）；提示词、反向词、模式、参考图、加速开关都没动");
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  $("control-scroll").scrollTo({ top: 0, behavior });
  if (window.matchMedia("(max-width: 1023px)").matches) window.scrollTo({ top: 0, behavior });
}

/* ---------- 放大视图（画廊的「看清」与「拿走」）----------
 *
 * 为什么是一个 <dialog> 而不是自己搭的遮罩：Esc 关闭、焦点锁在里面、背后 inert
 * —— 三样浏览器都给。自己搭要各写一遍，而漏掉哪一样都**没有症状**（用户只
 * 觉得「关不掉」「tab 跑到背后去了」）。`showModal()` 还会在关闭时把焦点还给
 * 点开它的那个按钮（本模块不用自己记）。
 *
 * 里面不只有图，**还有完整提示词**：这是页面上唯一摆着全文的地方（格子上一个字
 * 都没有 —— 2026-10-01 减字之后连截断的那一行也撤了），照着一句看着完整的话去
 * 复制、粘出来却是断的 —— 那种错只有把全文摆在眼前才不会发生。「复制提示词」
 * 因此在两个地方各有一个入口，而放大视图这个是**看得见再拿**的那个。
 *
 * 这行 meta 也是**参考图 / 源图唯一的落点**：格子只剩一行药丸，「载入参数」又
 * 已经不碰它们了（只拿数值），不说在这儿就等于页面上再也看不到「这张图用了哪张图」。
 *
 * 两级缩放：默认「适应窗口」（整张图都在眼里），点一下图切到「原始像素」
 * （1:1，看的是这张图真正的细节），1:1 时按住左键可以拖动。
 */
function lightboxItems() {
  return (state.gallery && state.gallery.items) || [];
}

function lightboxItem() {
  return lightboxItems().find((x) => x.id === state.lightboxId) || null;
}

function openLightbox(id) {
  if (!lightboxItems().some((x) => x.id === id)) return;
  state.lightboxId = id;
  state.lightbox1to1 = false;
  const dlg = $("lightbox");
  if (!dlg.open) dlg.showModal();     // 开着就只换内容，不重新开一遍（会闪）
  renderLightbox();
}

function closeLightbox() {
  const dlg = $("lightbox");
  if (dlg.open) dlg.close();
}

/* 换一张。顺序就是画廊里的顺序（同一个数组），尽头就停在尽头 ——
 * 转圈（最后一张的下一张回到第一张）会让人失去方向感。 */
function stepLightbox(delta) {
  const items = lightboxItems();
  const i = items.findIndex((x) => x.id === state.lightboxId);
  if (i < 0) return;
  const next = items[i + delta];
  if (!next) return;
  state.lightboxId = next.id;
  state.lightbox1to1 = false;
  renderLightbox();
  $("lb-stage").scrollTo({ top: 0, left: 0 });
}

function renderLightbox() {
  const it = lightboxItem();
  if (!it) { closeLightbox(); return; }
  const items = lightboxItems();
  const i = items.findIndex((x) => x.id === it.id);

  $("lb-name").textContent = it.filename;
  const meta = [];
  if (it.mode) meta.push(MODE_LABEL[it.mode] || it.mode);
  if (it.is_2k) meta.push("2K");
  if (it.rewrite) meta.push("改写");
  if (!it.recognized) meta.push("非本链路");
  if (it.width && it.height) meta.push(it.width + "×" + it.height);
  if (it.steps != null) meta.push(it.steps + " 步");
  if (it.seed != null) meta.push("seed " + it.seed);
  // 参考图 / 源图：**页面上唯一能看到它的地方**（格子只剩一行药丸，而
  // 「载入参数」已经只拿数值了）。多张时按顺序列出来并带上张数 ——
  // 「第一张决定输出尺寸」，顺序本身就是信息。
  const srcs = (it.source_images || []).filter(Boolean);
  if (srcs.length) {
    meta.push("源图 " + srcs.join("、") +
              (srcs.length > 1 ? "（" + srcs.length + " 张）" : ""));
  }
  if (it.has_timings && it.run) meta.push("耗时 " + fmtElapsed(it.run.wall_ms));
  meta.push((it.root_label || it.root || "未知来源") + " · 第 " + (i + 1) + "/" +
            items.length + " 张");
  $("lb-meta").textContent = meta.join(" · ");

  const img = $("lb-img");
  img.src = galleryImageUrl(it);
  img.alt = it.filename;
  // 提示词两段**各占一块**：`#lb-prompt` 只装正向那段，`#lb-negative` 只装反向。
  // 标签（「提示词」/「反向词」）是兄弟节点，**不进**这两个元素 —— 自动复制失败
  // 的兜底会 `selectNodeContents` 整块选中，标签混进去就会被一起复制走。
  $("lb-prompt").textContent = it.prompt || "（元数据里没有提示词）";
  $("lb-negative").textContent = it.negative_prompt || "";
  $("lb-negative-wrap").classList.toggle("hidden", !it.negative_prompt);
  $("lb-copy").disabled = !it.prompt;
  $("lb-copy-negative").classList.toggle("hidden", !it.negative_prompt);
  $("lb-copy-params").disabled = !paramsLine(it);
  // 「载入提示词」：这张图没有提示词就禁用（那条理由更具体，见 `syncPromptLoadButtons`），
  // 否则交给「表单此刻的模式」判。
  const lbLoad = $("lb-load-prompt");
  lbLoad.dataset.why = it.prompt ? "" : "empty";
  lbLoad.disabled = !it.prompt;
  if (it.prompt) lbLoad.removeAttribute("title");
  else lbLoad.title = "这张图的元数据里没有提示词";
  syncPromptLoadButtons();
  // 非本链路的图**不禁用「载入参数」**：禁用一个按钮说不出理由。让它点下去，
  // 由 loadFromGallery 给出那句解释（这里先把放大视图关掉，那句话才看得见）。
  $("lb-prev").disabled = i <= 0;
  $("lb-next").disabled = i >= items.length - 1;
  setLightboxZoom(false);
}

function setLightboxZoom(on) {
  state.lightbox1to1 = !!on;
  // 换挡就把「刚才拖了多远」清零：不然 1:1 里拖过之后切回适应窗口，
  // 下一次点图会被当成「拖动的尾巴」吞掉一次（点一下没反应）。
  state.lightboxMoved = 0;
  $("lb-stage").classList.toggle("zoomed", state.lightbox1to1);
  $("lb-zoom").textContent = state.lightbox1to1 ? "适应窗口" : "原始像素";
}

/* 把放大视图里那段提示词**选中**。自动复制失败时的兜底：全文在这儿、已经
 * 选好，用户按 Ctrl+C 就行 —— 比一句「复制失败」有用得多。 */
function selectLightboxPrompt() {
  const node = $("lb-prompt");
  try {
    const range = document.createRange();
    range.selectNodeContents(node);
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  } catch (e) { /* 选不中也不该炸掉整个动作 */ }
}

/* 复制一段文字，返回**真的成功没有**。
 *
 * 两级：`navigator.clipboard`（工作台跑在 127.0.0.1，是本机安全上下文，
 * 这条路通）失败退到临时 textarea + `execCommand`。两级都失败就返回 false ——
 * **绝不静默当成功**：静默失败的后果是用户以为复制到了，粘出来是上一次的内容。
 *
 * textarea 那条路会把焦点挪走，所以前后各记一次焦点，用完还回去 ——
 * 否则键盘用户复制一次就丢了自己的位置。 */
async function copyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 权限/焦点问题，退到下一条 */ }
  const back = document.activeElement;
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return !!ok;
  } catch (e) {
    return false;
  } finally {
    if (back && back.focus) back.focus();
  }
}

/* 按钮上的回话：改字 + 1.4 秒后自己变回来。
 * 用一个**看得见**的回话而不是一句 toast —— 点的是哪个按钮，回答就出现在哪个
 * 按钮上，中间不需要用户去找。 */
function flashButton(btn, text, isErr) {
  if (!btn) return;
  if (btn.dataset.label == null) btn.dataset.label = btn.textContent;
  clearTimeout(Number(btn.dataset.timer || 0));
  btn.textContent = text;
  btn.classList.toggle("flasherr", !!isErr);
  btn.dataset.timer = String(setTimeout(() => {
    btn.textContent = btn.dataset.label;
    btn.classList.remove("flasherr");
  }, 1400));
}

/* 「复制提示词」的完整设计在 docs/superpowers/specs/2026-10-01-…
 * 一句话：复制的是**这张图自己的**提示词（PNG 元数据里那份，也就是当初真的
 * 喂给采样器的那份），不是表单里现在写着的那份 —— 表单随时可能已经变了。 */
async function copyPrompt(item, btn) {
  const text = item.prompt || "";
  if (!text) { flashButton(btn, "这张没有提示词", true); return; }
  if (await copyText(text)) { flashButton(btn, "已复制"); return; }
  // 兜底：把全文摆出来并选中，让用户自己按 Ctrl+C。**同时说出来**，
  // 否则用户会盯着一个没反应的按钮。
  openLightbox(item.id);
  selectLightboxPrompt();
  $("lb-status").textContent = "自动复制失败：全文已选中，按 Ctrl+C 复制";
  flashButton(btn, "请手动复制", true);
}

/* 「复制」这一族的口径（2026-10-01 按用户要求，**改动只有这一条**）：
 *
 * 一张图上有**三份互不相同**的东西 —— 提示词、反向词、数值参数。以前「复制提示词」
 * 只有一份，反向词一出现，那一段就与提示词混在一起，粘出去分不出哪段是哪段
 * （用户的原话：「单纯的复制无法区分两者」）。现在的形状与「载入参数」一致：
 *
 *   复制提示词 · 复制反向词 · 复制参数   （+ 载入参数 = 把数值填回表单）
 *
 * **每个动作只取自己那一份，谁也不合并** —— 合并了就再也拆不开，而用户要的是
 * 「有时只要提示词，有时只要参数」。没有反向词的图上，那个按钮不出现。
 */

/* 参数那一行：与 `loadFromGallery` 取的是**同一批键**（steps / seed / cfg /
 * sampler / scheduler / 尺寸）—— 「载入参数」把这份数值填回表单，这里把它变成
 * 一行文本带走。两处的键集必须一致，`test_api.py` 有一条判据盯着这个镜像。
 *
 * 另外两个字段只在**别的那条链**上有：放大图没有步数也没有尺寸，它的参数就是
 * `seed` + `scale` + 源图。少了这两个，「复制参数」在放大图上等于什么都没复制。
 * （它们**不进**表单：模式不同，写进去是错的 —— 那是「载入参数」不收它们的理由，
 * 与「带走一份记录」无关。） */
function paramsLine(item) {
  const bits = [];
  if (item.steps != null) bits.push("steps=" + item.steps);
  if (item.seed != null) bits.push("seed=" + item.seed);
  if (item.cfg != null) bits.push("cfg=" + item.cfg);
  if (item.sampler) bits.push("sampler=" + item.sampler);
  if (item.scheduler) bits.push("scheduler=" + item.scheduler);
  if (item.width && item.height) bits.push(item.width + "×" + item.height);
  if (item.scale != null) bits.push("scale=" + item.scale);
  if (item.source_image) bits.push("source=" + item.source_image);
  return bits.join("  ");
}

async function copyParams(item, btn) {
  const text = paramsLine(item);
  if (!text) { flashButton(btn, "这张没有参数", true); return; }
  if (await copyText(text)) { flashButton(btn, "已复制"); return; }
  flashButton(btn, "复制失败", true);
}

async function copyNegative(item, btn) {
  if (await copyText(item.negative_prompt || "")) {
    flashButton(btn, "已复制");
    $("lb-status").textContent = "反向词已复制";
  } else {
    flashButton(btn, "复制失败", true);
  }
}

function initLightbox() {
  const dlg = $("lightbox");
  const stage = $("lb-stage");
  $("lb-close").onclick = closeLightbox;
  $("lb-zoom").onclick = () => setLightboxZoom(!state.lightbox1to1);
  $("lb-prev").onclick = () => stepLightbox(-1);
  $("lb-next").onclick = () => stepLightbox(1);
  $("lb-copy").onclick = () => copyPrompt(lightboxItem() || {}, $("lb-copy"));
  $("lb-copy-negative").onclick = () => copyNegative(lightboxItem() || {},
                                                     $("lb-copy-negative"));
  $("lb-copy-params").onclick = () => copyParams(lightboxItem() || {},
                                                 $("lb-copy-params"));
  $("lb-load-prompt").onclick = () => {
    const it = lightboxItem();
    if (!it) return;
    closeLightbox();          // 先关：那句「已从 … 载入」在放大视图背后看不见
    loadPromptsFromGallery(it);
  };
  $("lb-load").onclick = () => {
    const it = lightboxItem();
    if (!it) return;
    closeLightbox();          // 先关：那句「已载入」的提示在放大视图背后
    loadFromGallery(it);
  };
  // Esc 由 <dialog> 自己处理（表单里的其它元素不拦它）。关掉之后状态要清干净，
  // 否则下一次打开会先闪一下上一张。
  dlg.addEventListener("close", () => {
    state.lightboxId = null;
    state.lightbox1to1 = false;
    $("lb-img").removeAttribute("src");
    $("lb-status").textContent = "";
  });
  // 点在遮罩上（事件目标是 dialog 自己）就关。点在内容上不关。
  dlg.onclick = (event) => { if (event.target === dlg) closeLightbox(); };
  dlg.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") { event.preventDefault(); stepLightbox(-1); }
    else if (event.key === "ArrowRight") { event.preventDefault(); stepLightbox(1); }
  });

  /* 1:1 的图比窗口大，得能拖着看。
   *
   * 拖动**不能**被当成「点一下」（点一下是切回适应窗口），所以两条路分开判：
   * 按下记位置，挪动超过 4px 才算拖；`click` 里只看这个距离。
   *
   * 指针捕获**要等真的拖起来再设**（`moved > 4` 那一步），不能在按下时就设：
   * 设了之后 `click` 会派发给捕获元素（stage）而不是那张图，于是「1:1 时
   * 点一下切回适应窗口」就静默失效了 —— 而这正是那种只有用起来才知道的错。 */
  let pan = null;
  const captured = (event) => !!(stage.hasPointerCapture && event.pointerId != null &&
                                 stage.hasPointerCapture(event.pointerId));
  stage.addEventListener("pointerdown", (event) => {
    if (!state.lightbox1to1) return;
    pan = { x: event.clientX, y: event.clientY,
            left: stage.scrollLeft, top: stage.scrollTop };
    state.lightboxMoved = 0;
  });
  stage.addEventListener("pointermove", (event) => {
    if (!pan) return;
    const dx = event.clientX - pan.x;
    const dy = event.clientY - pan.y;
    state.lightboxMoved = Math.max(state.lightboxMoved,
                                   Math.abs(dx) + Math.abs(dy));
    if (state.lightboxMoved > 4) {
      if (!captured(event)) {
        try { stage.setPointerCapture(event.pointerId); } catch (e) { /* 无所谓 */ }
      }
      stage.classList.add("panning");
    }
    stage.scrollLeft = pan.left - dx;
    stage.scrollTop = pan.top - dy;
  });
  const endPan = (event) => {
    pan = null;
    stage.classList.remove("panning");
    if (event && captured(event)) {
      try { stage.releasePointerCapture(event.pointerId); } catch (e) { /* 无所谓 */ }
    }
  };
  stage.addEventListener("pointerup", endPan);
  stage.addEventListener("pointercancel", endPan);
  // 点一下就换挡：适应窗口 ⇄ 原始像素。刚拖完的那一下不算。
  $("lb-img").onclick = () => {
    if (state.lightboxMoved > 4) { state.lightboxMoved = 0; return; }
    setLightboxZoom(!state.lightbox1to1);
  };
}

/* ---------- 提示词库（P2）---------- */
async function fetchLibrary() {
  const params = new URLSearchParams();
  if (state.libraryQuery) params.set("q", state.libraryQuery);
  try {
    const data = await api("/api/prompts?" + params.toString());
    state.library = data;
    renderLibrary(data);
  } catch (e) {
    $("lib-list").innerHTML = '<p class="bad">读不到提示词库：' + esc(e.message) + "</p>";
  }
}

function renderLibrary(data) {
  const rows = data.prompts || [];
  $("lib-count").textContent = rows.length ? "（" + rows.length + "）" : "";
  if (!rows.length) {
    $("lib-list").innerHTML = '<p class="dim">' +
      (state.libraryQuery ? "没有匹配的提示词。" : "还没有保存过提示词。") + "</p>";
    return;
  }
  $("lib-list").innerHTML = rows.map((r) =>
    '<div class="lib-row" data-id="' + esc(r.id) + '">' +
      '<button type="button" class="lib-main" data-act="use" title="点一下填进表单">' +
        '<span class="lib-title">' + esc(r.title) + "</span>" +
        '<span class="lib-sub dim">' + esc((r.tags || []).join(" · ") || "无标签") +
          (r.uses ? " · 用过 " + r.uses + " 次" : "") + "</span>" +
      "</button>" +
      '<button type="button" class="ghost" data-act="del" aria-label="删除提示词：' + esc(r.title) + '"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button>' +
    "</div>").join("");
  for (const el of $("lib-list").querySelectorAll(".lib-row")) {
    const id = el.dataset.id;
    el.querySelector('[data-act="use"]').onclick = () => usePrompt(id);
    el.querySelector('[data-act="del"]').onclick = () => deletePrompt(id, el);
  }
}

function usePrompt(id) {
  const row = ((state.library || {}).prompts || []).find((r) => r.id === id);
  if (!row) return;
  $("prompt").value = row.text;
  $("prompt-len").textContent = row.text.length;
  refreshBuild();
  postJSON("/api/prompts/" + encodeURIComponent(id) + "/use", {}).catch(() => {});
  setRunMsg("已载入提示词：" + row.title);
}

async function savePrompt() {
  const text = $("prompt").value.trim();
  if (!text) { setRunMsg("提示词是空的，没什么可存", true); return; }
  let title = "";
  if (text.length > 60) {
    // 长的提示词让用户起个名字；空标题由服务端按首行兜底。
    title = window.prompt("给这条提示词起个名字：",
                          text.split("\n")[0].trim().slice(0, 60));
    if (title === null) return;            // 取消
  }
  try {
    await postJSON("/api/prompts", { text: text, title: title });
    setRunMsg("已存进提示词库");
    await fetchLibrary();
  } catch (e) { setRunMsg("保存失败：" + e.message, true); }
}

async function deletePrompt(id, rowEl) {
  const row = ((state.library || {}).prompts || []).find((r) => r.id === id);
  if (!window.confirm("删除「" + ((row && row.title) || id) + "」？")) return;
  try {
    await api("/api/prompts/" + encodeURIComponent(id), { method: "DELETE" });
    await fetchLibrary();
  } catch (e) { setRunMsg("删除失败：" + e.message, true); }
}

/* ---------- 逐节点耗时（P3，判据 4）---------- */
function fmtMs(ms) {
  if (ms == null) return "";
  if (ms >= 60000) return (ms / 60000).toFixed(2) + " 分";
  if (ms >= 1000) return (ms / 1000).toFixed(1) + " s";
  return Math.round(ms) + " ms";
}

function ensureTimings(jobId) {
  if (!jobId || state.timings.has(jobId) || state.timingsPending.has(jobId)) return;
  state.timingsPending.add(jobId);
  api("/api/jobs/" + encodeURIComponent(jobId) + "/timings")
    .then((data) => state.timings.set(jobId, data))
    .catch((e) => state.timings.set(jobId, {
      available: false, reason: "读耗时失败：" + e.message }))
    .finally(() => {
      state.timingsPending.delete(jobId);
      if (state.currentId === jobId) renderLive();
    });
}

function timingsHtml(jobId) {
  const t = state.timings.get(jobId);
  if (!t && state.timingsPending.has(jobId)) {
    return '<div class="timings"><h3>耗时分解</h3><p class="dim">读取中…</p></div>';
  }
  if (!t) return "";
  if (!t.available) {
    // 探针没开是**正常状态**，不是错误 —— 出图完全正常，只是没有耗时数据
    return '<div class="timings"><h3>耗时分解</h3><p class="dim">本次无耗时数据：'
      + esc(t.reason || "未启用探针") + "</p></div>";
  }

  const rows = t.rows || [];
  const widest = rows.length ? Math.max(...rows.map((r) => r.compute_ms)) : 1;
  const s = t.sampling || {};
  const cov = t.coverage_pct;

  const bars = rows.map((r) => {
    // 条长按「最慢的节点」归一，不是按 wall —— 否则除了 KSampler 全是
    // 一条看不见的线，读不出相对关系。
    const w = Math.max(0.4, 100 * r.compute_ms / (widest || 1));
    return '<div class="trow' + (r.is_sampler ? " sampler" : "") + '">' +
      '<span class="tnode mono">' + esc(r.node_id) + "</span>" +
      '<span class="tclass" title="' + esc(r.class_type) + '">' + esc(r.class_type) +
        (r.cached ? ' <span class="pill">缓存</span>' : "") + "</span>" +
      '<span class="tbar"><i style="width:' + w.toFixed(2) + '%"></i></span>' +
      '<span class="tms mono">' + fmtMs(r.compute_ms) + "</span>" +
      '<span class="tpct dim">' +
        (r.pct_of_wall != null ? r.pct_of_wall.toFixed(1) + "%" : "") + "</span>" +
      '<span class="tvram dim">' +
        (r.vram_used_mb != null ? Math.round(r.vram_used_mb) + " MB" : "") +
      "</span></div>";
  }).join("");

  // 采样拆分：节点耗时 = 纯去噪循环 + 节点内其余开销。差那 25 ms 也要说出来。
  const split = (s.node_ms != null)
    ? "KSampler 节点 " + fmtMs(s.node_ms) + " = 纯采样循环 " + fmtMs(s.loop_ms) +
      (s.steps ? "（" + s.steps + " 步，每步 " + s.per_step_ms + " ms）" : "") +
      " + 节点内其他 " + fmtMs(s.node_overhead_ms)
    : (s.loop_ms ? "纯采样循环 " + fmtMs(s.loop_ms) : "");

  return '<div class="timings"><h3>耗时分解</h3>' +
    '<div class="cov">' +
      '<span class="dim">覆盖率</span>' +
      '<span class="cov-bar"><i style="width:' +
        Math.max(0, Math.min(100, cov || 0)).toFixed(1) + '%"></i></span>' +
      '<span class="mono">' + (cov != null ? cov.toFixed(1) + "%" : "?") + "</span>" +
      '<span class="dim">节点合计 ' + fmtMs(t.total_ms) + " / 总 " +
        fmtMs(t.wall_ms) + "（未计入 " + fmtMs(t.unaccounted_ms) + "）</span>" +
    "</div>" +
    (split ? '<p class="split dim">' + esc(split) + "</p>" : "") +
    (t.warm_start
      ? '<p class="split dim">' +
        (cacheVerdict(rows.map((r) => (
          { id: r.node_id, cls: r.class_type, cached: !!r.cached }))).kind === "all"
          ? "没有重新采样：上游全在缓存里，这张图是把缓存里那份重新写出来的 —— "
          : "温启动：") +
        t.cached_count + " 个节点走缓存（" +
        esc((t.cached_nodes || []).slice(0, 8).join(" / ")) +
        ((t.cached_nodes || []).length > 8 ? " …" : "") + "）</p>"
      : "") +
    '<div class="trows">' + bars + "</div>" +
    "</div>";
}

/* ---------- 日志台（P20）----------
 *
 * 与 SSE **无关**，这是刻意的：SSE 断了的时候正是最需要看日志的时候，
 * 挂在事件流上的日志视图会在最该工作的那一刻跟着一起瞎。所以它自己轮询
 * `/api/logs`，而且**只在视图开着的时候**轮询（关掉就停，不空转）。
 *
 * 环境体检那一块走现成的 `/api/backend`（那里已经有一整套推导与三态探活），
 * 所以日志台不另算一份 —— 两份推导迟早会分家。
 */
const LOG_POLL_MS = 1000;
const LOG_MAX_ROWS = 800;    // 时间线在页面里最多留这么多行
const LOG_MAX_LOGS = 400;    // 工作台日志那一栏的尾部长度

function logsOpen() { return !$("logview").hidden; }

function clockText(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

/* 「多久以前」。**这个数必须显出来**：实测本机后端在跑但空闲了一个半小时，
   它那份日志最后写入停在 1.5 小时前 —— 不说的话，一屏陈旧日志看起来就像
   「后端死了」。 */
function agoText(seconds) {
  if (seconds == null) return "";
  if (seconds < 90) return seconds + " 秒前";
  if (seconds < 5400) return Math.round(seconds / 60) + " 分钟前";
  return (seconds / 3600).toFixed(1) + " 小时前";
}

function openLogs() {
  $("workspace").hidden = true;
  $("logview").hidden = false;
  $("btn-logs").setAttribute("aria-expanded", "true");
  state.logs.cursor = 0;
  state.logs.epoch = null;
  state.logs.rows = [];
  state.logs.workbench = [];
  state.logs.rendered = 0;
  state.logs.filteredAt = null;
  $("logs-meta").textContent = "正在读…";
  fetchLogs(true);
  fetchBackend();          // 环境体检复用那一条，不另算一套
  startLogPolling();
  // 焦点跟着走，否则 Tab 还会在工作区那一侧转（视图换了，键盘用户却不知道）。
  // **推到下一帧**：刚刚 un-hide 的元素这一帧还不一定可聚焦（浏览器要重算样式），
  // 直接调会**静默不生效**。`requestAnimationFrame` 不在时退回直接调
  // （两个假 DOM 里就没有它）。
  const focusBack = () => $("btn-logs-back").focus();
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(focusBack);
  } else {
    focusBack();
  }
}

function closeLogs() {
  stopLogPolling();
  $("logview").hidden = true;
  $("workspace").hidden = false;
  $("btn-logs").setAttribute("aria-expanded", "false");
  $("btn-logs").focus();
}

function startLogPolling() {
  stopLogPolling();
  state.logs.timer = setInterval(() => fetchLogs(false), LOG_POLL_MS);
}

function stopLogPolling() {
  if (state.logs.timer) {
    clearInterval(state.logs.timer);
    state.logs.timer = null;
  }
}

async function fetchLogs(first) {
  const params = (first || !state.logs.cursor) ? "" : ("?since=" + state.logs.cursor);
  let data;
  try {
    data = await getJSON("/api/logs" + params);
  } catch (e) {
    // 日志台自己都拉不到数据，**要说出来** —— 空白的一屏看起来像「什么都没发生」，
    // 而那正是最坏的一种误导（出事的时候日志页恰好是空的）。
    $("logs-meta").textContent = "拉不到日志：" + e.message;
    return;
  }
  // 服务重启过（epoch 变了）：环里的 seq 从 1 重来，手里的旧行全部作废。
  // 服务端也会报 reset（游标比它的 last_seq 还大），两条路都清一次。
  if (data.reset || (state.logs.epoch !== null && data.epoch !== state.logs.epoch)) {
    state.logs.rows = [];
    state.logs.workbench = [];
    state.logs.rendered = 0;
  }
  state.logs.epoch = data.epoch;
  const fresh = data.timeline || [];
  if (data.workbench) {
    // 首次（或重置后）服务端给的是**尾部快照**，此时 timeline 里也含这些行，
    // 再叠一遍就每行出现两次。
    state.logs.workbench = data.workbench.slice();
  } else {
    for (const row of fresh) {
      if (row.kind === "log") state.logs.workbench.push(row);
    }
    if (state.logs.workbench.length > LOG_MAX_LOGS) {
      state.logs.workbench = state.logs.workbench.slice(-LOG_MAX_LOGS);
    }
  }
  if (fresh.length) {
    state.logs.rows = state.logs.rows.concat(fresh);
    if (state.logs.rows.length > LOG_MAX_ROWS) {
      state.logs.rows = state.logs.rows.slice(-LOG_MAX_ROWS);
      state.logs.rendered = 0;      // 砍掉过头部 → 已画的下标全乱了，重画
      state.logs.filteredAt = null;
    }
  }
  state.logs.cursor = data.cursor;
  state.logs.backend = data.backend;
  state.logs.at = data.now;
  renderLogs(false);
}

function feedRowHtml(row) {
  const detail = row.detail
    ? '<div class="fdetail">' + esc(row.detail) + "</div>" : "";
  return '<div class="feed-row ' + esc(row.level) + '">'
    + '<span class="ft">' + clockText(row.ts) + "</span>"
    + '<span class="fl">' + esc(row.level) + "</span>"
    + '<span class="fm"><span class="fs">' + esc(row.source) + "</span> "
    + esc(row.text) + "</span>"
    + detail + "</div>";
}

/* 只**追加**、不整块重画。每秒重画一次会把用户正选着的那段文字抹掉 ——
 * 而「选中一行去搜」正是看日志时的常见动作，抹掉还是**没有症状**的
 * （滚动位置看着还在，选中的东西没了）。 */
function renderFeed(force) {
  const box = $("log-feed");
  const only = $("logs-errors-only").checked;
  if (force || only !== state.logs.filteredAt) {
    box.innerHTML = "";
    state.logs.rendered = 0;
    state.logs.filteredAt = only;
  }
  let rows = state.logs.rows;
  if (only) rows = rows.filter((r) => r.level !== "info");
  const fresh = rows.slice(state.logs.rendered);
  if (fresh.length) {
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
    box.insertAdjacentHTML("beforeend", fresh.map(feedRowHtml).join(""));
    state.logs.rendered = rows.length;
    if (state.logs.follow && near) box.scrollTop = box.scrollHeight;
  }
  if (!rows.length) {
    box.innerHTML = '<p class="dim">'
      + (only ? "没有警告或错误。" : "还没有任何记录。") + "</p>";
    state.logs.rendered = 0;
  }
  $("logs-count").textContent = only
    ? (rows.length + " 条（只看警告/错误）") : (rows.length + " 条");
}

function renderLogPane(id, rows, empty) {
  const box = $(id);
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
  box.textContent = rows && rows.length ? rows.join("\n") : empty;
  if (state.logs.follow && near) box.scrollTop = box.scrollHeight;
}

function renderLogs(force) {
  state.logs.follow = $("logs-follow").checked;
  renderFeed(force);
  // 工作台日志那一栏：kind=log 的行，带时间戳与级别，**这就是原文**
  const wb = state.logs.workbench || [];
  renderLogPane("log-workbench", wb.map((r) => (
    clockText(r.ts) + " " + r.level.toUpperCase().padEnd(5) + " "
    + r.source + "  " + r.text + (r.detail ? "\n" + r.detail : ""))),
    "还没有工作台的日志记录。");
  const be = state.logs.backend;
  if (!be) {
    renderLogPane("log-backend", [], "正在读…");
  } else if (!be.path) {
    renderLogPane("log-backend", [], be.note || "没有后端日志。");
    $("log-backend-label").textContent = "";
  } else {
    renderLogPane("log-backend", be.lines, "（这份日志是空的）");
    const stale = be.stale_s != null && be.stale_s > 120
      ? "，最后写入 " + agoText(be.stale_s) + "（后端空闲时不写日志）" : "";
    $("log-backend-label").textContent = be.label + " · "
      + be.lines.length + " 行" + stale;
  }
  renderLogSources(be);
  renderLogEnv();
  const s = state.logs;
  $("logs-meta").textContent = s.at
    ? ("读取于 " + clockText(s.at) + " · 每 " + (LOG_POLL_MS / 1000) + " 秒刷新")
    : "";
}

function renderLogSources(be) {
  const box = $("log-backend-sources");
  if (!be || !be.candidates || !be.candidates.length) {
    box.textContent = "没有候选日志文件。";
    return;
  }
  const lines = be.candidates.map((c) => (
    (c.path === (be.path || "") ? "▶ " : "  ") + c.label + " · "
    + Math.round(c.size / 1024) + " KB · 最后写入 " + agoText(c.stale_s)));
  box.textContent = "按「最近写入」挑的是第一份；以下是全部候选（陈旧的也在，"
    + "免得看着像没有）：\n" + lines.join("\n");
}

/* 环境体检：数据来自 `/api/backend`（15 秒刷一次，与顶栏那颗灯同一个源）。 */
function renderLogEnv() {
  const be = state.backend;
  const box = $("log-env");
  if (!be) {
    box.innerHTML = '<dt>状态</dt><dd>正在读…</dd>';
    return;
  }
  const row = (k, v, cls) => '<dt>' + esc(k) + '</dt><dd'
    + (cls ? ' class="' + cls + '"' : "") + ">" + esc(v) + "</dd>";
  const out = [];
  out.push(row("工作台", location.host));
  // 解释器与状态目录在这里，**不在时间线那条日志里**：两个绝对路径会把那一栏
  // 撑成五行折行，而它们本来就是「环境」而不是「某时刻发生的一件事」。
  const rt = be.runtime || {};
  if (rt.python) out.push(row("解释器", rt.python));
  if (rt.state_dir) out.push(row("状态目录", rt.state_dir));
  const live = { ok: "运行中", unclear: "在跑，但探活没答完", down: "未运行" }[be.liveness] || "未知";
  out.push(row("后端", be.url + " · " + live
    + (be.jobs ? " · 队列 " + be.jobs.active : ""),
    be.liveness === "ok" ? "good" : "bad"));
  out.push(row("ComfyUI", be.comfyui_version || "（未答）"));
  if (be.device) {
    const gb = (n) => (n / 1073741824).toFixed(1) + " GiB";
    out.push(row("显卡", be.device.name + " · 空闲 " + gb(be.device.vram_free || 0)
      + " / " + gb(be.device.vram_total || 0)));
  }
  const models = be.models || {};
  const names = { dit: "DiT", text_encoder: "文本编码器", rewriter: "改写器", vae: "VAE" };
  const parts = Object.keys(models).map((k) => {
    const m = models[k] || {};
    const mark = m.present === true ? "✓" : (m.present === false ? "✗" : "?");
    return (names[k] || k) + " " + mark;
  });
  out.push(row("模型", parts.join("  "), be.missing_models && be.missing_models.length ? "bad" : ""));
  if (be.probe) {
    const p = be.probe;
    const r = p.report || {};
    const count = r.count != null ? r.count
      : (r.entries != null ? r.entries : (r.total != null ? r.total : null));
    out.push(row("探针", (p.known ? "有状态文件" : "没有状态文件")
      + (p.alive === true ? " · 进程活着"
        : (p.alive_unknown ? " · 判不了活" : " · 进程已死"))
      + (p.pid ? " · pid " + p.pid : "")
      + (count != null ? " · 报告 " + count + " 次" : "")),
      p.alive === false ? "warn" : "");
  }
  if (be.jobs) out.push(row("作业", "共 " + be.jobs.total + " · 在跑 " + be.jobs.active));
  out.push(row("提示词库", (be.prompts || 0) + " 条"));
  const roots = be.output_roots || [];
  if (roots.length) {
    out.push(row("输出目录", roots.length + " 个来源：" + roots
      .map((r) => (r.label || r.path || "?")).join(" / ")));
  }
  if (be.launch) {
    out.push(row("一键启动", be.launch.phase + (be.launch.launch_id ? " · " + be.launch.launch_id : "")
      + (be.launch.process_alive ? " · 进程活着" : "")),
      be.launch.phase === "failed" ? "bad" : "");
  }
  // 「启动命令」搬到这儿来了（P20i）：它原来长在页面顶那块告警板上，而那块板
  // 已经撤掉。命令本身一个字没改 —— 它就是 `config.start_command()["display"]`，
  // 也就是铁律 1 那条（唯一真源），页面只是多了个复制按钮。
  const cmd = (be.start_command || {}).display || "";
  if (cmd) {
    out.push('<dt>启动命令</dt><dd class="cmd-row"><code id="log-cmd">' + esc(cmd)
      + '</code><button class="ghost" id="btn-copy-cmd" type="button">复制</button></dd>');
  }
  // 开机脚本（P22）：生产后端是**开机任务**拉起的，跑的**不是**上面那条命令 ——
  // 它由安装目录里那个 .bat 说了算，而那个文件不在仓库里、改它没有任何保护。
  // 少了变量时那条路会静默地跑在另一套环境里（实测：拆层去够 huggingface.co，
  // 退避 5 次后整次跑死，而这一页写着「后端就绪」）。文案由后端给，页面只画。
  const boot = be.boot_script || {};
  if (boot.present) {
    out.push(row("开机脚本", boot.summary || "", boot.ok ? "" : "warn"));
  }
  // 受限模式原来只写在顶栏那行小字上（那行已经没有了）。落点在工区里是**这台机器
  // 的处境**，不是一个时刻发生的事，所以它属于「环境体检」而不是时间线。
  const lay = layoutOf(be.launch);
  if (lay.mode === "confined") {
    out.push(row("落点", "受限模式 · " + (lay.note || "临时/用户/输入目录都在工区里"),
                 "warn"));
  }
  if (be.hint) out.push(row("一句话", be.hint));
  box.innerHTML = out.join("");
  // 复制按钮是**每次重画时新建的**，所以在这里绑最稳（外面绑会绑到上一颗上）。
  const copy = $("btn-copy-cmd");
  if (copy) copy.onclick = copyStartCommand;
}

/* 复制那条启动命令。它只在「日志 → 环境体检」里有一个落点，所以复制失败时
   必须给出**第二条路**（选中它、自己按 Ctrl+C），否则按钮闪一下「复制失败」
   就什么都没了 —— 而这条命令正是后端起不来时唯一能自己动手的东西。 */
async function copyStartCommand() {
  const btn = $("btn-copy-cmd");
  const code = $("log-cmd");
  const text = code ? code.textContent : "";
  if (!text) return;
  if (await copyText(text)) { flashButton(btn, "已复制"); return; }
  const r = document.createRange();
  r.selectNodeContents(code);
  const sel = getSelection();
  sel.removeAllRanges(); sel.addRange(r);
  flashButton(btn, "已选中，按 Ctrl+C", true);
}

async function copyAllLogs() {
  const btn = $("btn-logs-copy");
  const be = state.logs.backend || {};
  const feed = (state.logs.rows || []).map((r) => (
    clockText(r.ts) + " " + r.level.toUpperCase() + " " + r.source + "  " + r.text
    + (r.detail ? "\n" + r.detail : ""))).join("\n");
  const text = [
    "== 时间线 ==", feed || "（空）",
    "", "== 后端日志（" + (be.path || "没有") + "）==",
    (be.lines || []).join("\n") || "（空）",
    "", "== 工作台日志 ==",
    (state.logs.workbench || []).map((r) => clockText(r.ts) + " " + r.level
      + " " + r.source + "  " + r.text).join("\n") || "（空）",
    // 启动命令也抄一份（P20i）：它只长在「环境体检」里，而这一份是拿去贴给别人
    // 或者在别处重建现场的 —— 少了它，那条命令就得回头再找一次。
    "", "== 启动命令 ==",
    ((state.backend || {}).start_command || {}).display || "（读不到）",
  ].join("\n");
  const ok = await copyText(text);
  flashButton(btn, ok ? "已复制" : "复制失败", !ok);
}

function initLogsView() {
  $("btn-logs").onclick = openLogs;
  $("btn-logs-back").onclick = closeLogs;
  $("btn-logs-refresh").onclick = () => {
    state.logs.rows = [];
    state.logs.workbench = [];
    state.logs.rendered = 0;
    state.logs.filteredAt = null;
    state.logs.cursor = 0;
    fetchLogs(true);
    fetchBackend();
  };
  $("btn-logs-copy").onclick = copyAllLogs;
  $("logs-errors-only").onchange = () => renderLogs(true);
  $("logs-follow").onchange = () => { state.logs.follow = $("logs-follow").checked; };
  // Esc 回工作台：与放大视图同一个约定（那里 Esc 关窗，这里 Esc 退视图）。
  // 但只在**没有别的对话框开着**的时候 —— 否则在对照窗口里按 Esc 会把底下
  // 整个日志台也退掉，一次 Esc 干了两次事。
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !logsOpen()) return;
    if (document.querySelector("dialog[open]")) return;
    closeLogs();
  });
}

/* ---------- LLM 提示词增强（P20）----------
 *
 * 与「提示词改写器」是两个东西，**互不替代也不互斥**：改写器是图里那个
 * 8.82 GiB 的模型（提交时在图中生效，一百多秒）；这个是提交前的一次
 * 文本进文本出（秒级）。因为**要先出对照再采用**，它只能是个按钮，
 * 不能是提交时静默生效的复选框 —— 那样你就看不到它改了什么。
 */
function llmReady() {
  return !!(state.llm && state.llm.settings && state.llm.settings.configured);
}

async function fetchLLM() {
  try {
    state.llm = await getJSON("/api/llm");
  } catch (e) {
    $("llm-msg").textContent = "读不到设置：" + e.message;
    syncEnhanceButtons();
    return;
  }
  renderLLMSettings();
}

function renderLLMSettings() {
  const d = state.llm;
  if (!d) return;
  const s = d.settings;
  $("llm-base").value = s.base_url || "";
  $("llm-model").value = s.model || "";
  // 密钥**永远不回显**：输入框重置成空，只用 placeholder 说明「已配置」。
  // 留空保存 = 保持原样（后端那条规则），所以空着不会把密钥抹掉。
  $("llm-key").value = "";
  $("llm-key").placeholder = s.has_key
    ? ("已配置（" + s.key_masked + "）留空 = 不改") : "还没有填";
  $("llm-timeout").value = String(Math.round(s.timeout_s || 60));
  $("llm-system").value = s.system || "";
  // 内置规则**不在页面上抄第二份**（`llm.py::SYSTEM_BASE` 是唯一真源），
  // 页面上只有一行功能描述。这里也不再往 DOM 里塞规则全文。
  $("llm-state").textContent = s.configured
    ? "已配置" : ("未配置" + (s.missing && s.missing.length
      ? "（缺 " + s.missing.join(" / ") + "）" : ""));
  syncEnhanceButtons();
}

async function saveLLMSettings() {
  const btn = $("btn-llm-save");
  const body = {
    base_url: $("llm-base").value.trim(),
    model: $("llm-model").value.trim(),
    timeout_s: Number($("llm-timeout").value) || 60,
    system: $("llm-system").value,
  };
  // 密钥那一栏只有**真的填了**才送上去；空着表示「不动它」。
  const key = $("llm-key").value.trim();
  if (key) body.api_key = key;
  btn.disabled = true;
  try {
    state.llm = await postJSON("/api/llm", body);
    renderLLMSettings();
    $("llm-msg").textContent = "已保存";
  } catch (e) {
    $("llm-msg").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

/* 增强面板。**输入与处理都在这一层里做**（2026-10-01 按用户要求改的形状）。
 *
 * 为什么改：上一版是「每个框一颗按钮 → 点一下就跑 → 弹对照」。加一个可增强的
 * 东西就要加一颗按钮、加一套 id、加一份可用性判断 —— 扩展不动。现在只有**一颗**
 * 入口，顶栏两个下拉框都按**后端那两张表**建：
 *
 *   `llm.TARGETS_INFO`   → 「要处理哪一段」（提示词 / 反向词）
 *   `llm.ACTIONS_INFO`   → 「做什么」（增强 / 译成英文 / 译成中文）
 *
 * 加一个目标或加一个动作都 = 后端加一行。**页面里不认识任何一个动作 id**：
 * 右栏标题、「采用<noun>」、空稿占位、「正在<label>…」、「<label>失败」全从表里
 * 那两行文字拼出来（有一条判据钉住 app.js 里不出现「译成英文」）。
 *
 * 与上一版最要紧的一处差别：**面板里那颗「执行」按钮的文字是常量**。
 * 上一版是按钮自己显示「增强中…」、`finally` 里再「恢复」，而那个「原样」是从
 * DOM 里读的（`const back = btn.textContent`）—— 失败一次之后，`flashButton`
 * 已经把「增强中…」当成原样记进了 `dataset.label`，于是从那以后**每一步都把它
 * 「恢复」成「增强中…」，按钮永远卡住**，而它既不忙也没坏。
 * 现在进度与报错都去状态行，按钮文字是常量、也不随所选动作变 —— 具体做什么由
 * 紧挨着的那个下拉框说，**宁可标签泛一点，也不要让按钮文字再和运行时状态挂钩**。
 */
let enhanceSession = null;   // { target, action, drafts: { targetId: {input, result, meta} } }
// 面板重开时记住上一次选的「做什么」（只在这次页面会话里，不落盘）。翻译往往
// 要连着做几次，每次都弹回「增强」是白白多一次点击。
let enhanceLastAction = "enhance";

function enhanceTargets() {
  // 后端给的那张表；没拿到（还没 fetch 完 / 接口坏了）时退回两个已知目标，
  // 免得整个面板因为一次失败就打不开。
  if (state.llm && Array.isArray(state.llm.targets) && state.llm.targets.length) {
    return state.llm.targets;
  }
  return [{ id: "prompt", label: "提示词", field: "prompt" },
          { id: "negative", label: "反向词", field: "negative" }];
}

function enhanceTargetInfo(id) {
  const list = enhanceTargets();
  return list.find((t) => t.id === id) || list[0];
}

/* 这次要带给 LLM 看的参考图 —— **图上会用到的，才给它看**。
 *
 * 图生图与放大会把参考图喂进模型（一个当 reference_latents，一个当源图），
 * 文生图**不会**：那时把图递给 LLM，它就会照着那张图写提示词，而这张图在出图
 * 时根本不存在 —— 那是「空转」的近亲，而且同样没有症状（出图能出、图也对，
 * 只是内容跟用户真正想要的那张没关系）。
 *
 * 判据读**表单此刻的模式**（与「载入参数」那条坑同一条规矩：读别处的模式会
 * 恒真/恒假），所以 `applyModeUI` 里也要刷一次面板（见那里的一段注释）。 */
function llmRefs() {
  const mode = $("mode").value;
  return mode === "t2i" ? [] : state.refs.slice();
}

/* 那一行说清「这次带不带图、带哪几张」。**说在明处**是这个功能的形状的一部分：
 * 模型看到的是哪几张图，用户必须能一眼对上 —— 否则「它怎么写出这种东西」就没法
 * 解释了。带不上时也要说清是为什么（两种原因完全不同）。 */
function renderEnhanceRefs() {
  const refs = llmRefs();
  const el = $("en-refs");
  if (refs.length) {
    el.textContent = "随附 " + refs.length + " 张参考图：" + refs.join("、");
  } else if (state.refs.length) {
    el.textContent = "这次不带图：文生图不会用到参考图";
  } else {
    el.textContent = "这次不带图（还没有参考图）";
  }
}

/* 动作表。**兜底只给一项**（就那一个默认动作），刻意**不抄一份完整表** ——
 * 抄了就等于页面里有了第二真源，后端加一个动作时两边会分家，而那是加动作的人
 * 最容易漏的一处。真源在 `llm.ACTIONS_INFO`。 */
function enhanceActions() {
  if (state.llm && Array.isArray(state.llm.actions) && state.llm.actions.length) {
    return state.llm.actions;
  }
  return [{ id: "enhance", label: "增强", noun: "增强稿" }];
}

function enhanceActionInfo(id) {
  const list = enhanceActions();
  return list.find((a) => a.id === id) || list[0];
}

/* 面板里那一份草稿。**切换目标不丢东西**：每个目标各留一份（输入 + 增强稿），
   来回切的时候各自还在 —— 上一版是「一个框一颗按钮」，这个问题不存在；
   现在合成一个面板，不这么做就会「切过去再切回来，写的东西没了」。 */
function enhanceDraft(target, seedFromForm) {
  const session = enhanceSession;
  const info = enhanceTargetInfo(target);
  if (seedFromForm || !session.drafts[target]) {
    const box = info.field ? $(info.field) : null;
    session.drafts[target] = {
      input: box ? box.value : "",
      result: "",
      meta: "",
    };
  }
  return session.drafts[target];
}

function openEnhancePanel(target) {
  // 按钮在没配好时是禁用的，这里是第二道（键盘 / 程序调用也挡得住）
  if (!llmReady()) return;
  enhanceSession = { target: target || "prompt", action: enhanceLastAction, drafts: {} };
  fillEnhanceTargets();
  fillEnhanceActions();
  $("en-target").value = enhanceSession.target;
  const sel = $("en-action");
  sel.value = enhanceSession.action;
  // **以下拉框里真的选中了谁为准**：后端那张表可能已经没有这个动作了（或者
  // localStorage 一样地记着一个旧 id），那时 `value =` 不会生效、下拉框会显示
  // 第一项，而请求里发着那个已消失的 id —— 面板上写着「增强」、发出去的是别的
  // 东西，又是一条没有症状的错。
  enhanceSession.action = sel.value || enhanceActions()[0].id;
  enhanceDraft(enhanceSession.target, true);
  renderEnhancePanel();
  $("enhance").showModal();
  $("en-input").focus();
}

/* 两张下拉框的建法**一模一样**：只在选项变了的时候重建 —— 每次都重建会把用户
 * 选中的那一项弹回第一项，而下拉框就在顶栏，弹回去看起来像「点了没反应」。 */
function fillSelect(sel, rows) {
  const have = [...sel.options].map((o) => o.value);
  if (have.length === rows.length && have.every((v, i) => v === rows[i].id)) return;
  sel.innerHTML = rows.map((r) => '<option value="' + esc(r.id) + '">'
    + esc(r.label) + "</option>").join("");
}

function fillEnhanceTargets() {
  fillSelect($("en-target"), enhanceTargets());
}

function fillEnhanceActions() {
  fillSelect($("en-action"), enhanceActions());
}

function switchEnhanceTarget(id) {
  if (!enhanceSession) return;
  enhanceSession.target = id;
  // 这个目标第一次进来 → 从表单里取初值；已经来过 → 用它自己那份草稿
  enhanceDraft(id, !enhanceSession.drafts[id]);
  renderEnhancePanel();
  $("en-input").focus();
}

function switchEnhanceAction(id) {
  if (!enhanceSession) return;
  enhanceSession.action = id;
  enhanceLastAction = id;
  // 动作一变，右栏那份稿子就**对不上了**（它是在另一个动作下做出来的）。
  // 与「改了输入就作废」是同一条规矩：留着一份对不上的稿子，而「采用」一按
  // 就把它写回表单 —— 那不是防错，是防**没有症状的错**。
  // 输入本身留着（那是你写的字，跟动作无关），作废的只是结果。
  Object.keys(enhanceSession.drafts).forEach((key) => {
    enhanceSession.drafts[key].result = "";
    enhanceSession.drafts[key].meta = "";
  });
  renderEnhancePanel();
  $("en-input").focus();
}

function renderEnhancePanel() {
  const session = enhanceSession;
  if (!session) return;
  const info = enhanceTargetInfo(session.target);
  const act = enhanceActionInfo(session.action);
  const draft = session.drafts[session.target] || { input: "", result: "", meta: "" };
  // 左边那一栏的 textarea **不要**在每次重画时重写：用户正在里面打字，
  // 而打字本身会触发重画（字数）。只在值真的不同时写。
  const input = $("en-input");
  if (input.value !== draft.input) input.value = draft.input;
  $("en-from-count").textContent = draft.input.length + " 字";
  $("en-to").textContent = draft.result || "（还没有" + act.noun + "）";
  $("en-to").classList.toggle("empty", !draft.result);
  $("en-to-count").textContent = draft.result ? draft.result.length + " 字" : "";
  $("en-meta").textContent = draft.meta || "";
  $("en-title").textContent = "LLM 增强 · " + info.label;
  // 右栏标题、「采用」那两个字都跟着动作走 —— 但**都从后端那张表来**，
  // 页面里不认识任何一个动作 id。
  $("en-to-title").textContent = act.noun;
  $("en-adopt").textContent = "采用" + act.noun;
  // 「采用」只有一个含义：**采用这一稿**。没有稿就禁用并说明 —— 不搞「有稿采用
  // 稿、没稿采用左边」那种隐式分支（那会在你改过左边之后悄悄丢掉你的改动）。
  $("en-adopt").disabled = !draft.result;
  $("en-adopt").title = draft.result ? "" : "先点「执行」生成一" + act.noun;
  // 「← 移到左边」同样只在有稿时可用：接力用的两条腿都在右栏，一起灰才对。
  $("en-send").disabled = !draft.result;
  $("en-send").title = draft.result
    ? "把这一稿放到左边，接着做下一道（右边会清空 —— 免得两份对不上）"
    : "先点「执行」生成一" + act.noun;
  $("en-status").textContent = "「执行」用左边此刻的内容做「" + act.label
    + "」；采用后写回「" + info.label + "」框，可以 Ctrl+Z 撤销。";
  $("en-status").classList.remove("bad");
  renderEnhanceRefs();
}

function syncEnhanceButtons() {
  // 现在只有一颗入口了。**空着也叫得动** —— 面板里就是输入区，
  // 空着进去再写正是这个形状的意义（上一版是「框是空的就禁用」）。
  const btn = $("btn-enhance");
  btn.disabled = !llmReady();
  btn.title = llmReady()
    ? "增强或翻译提示词 / 反向词：点开面板选做哪一道"
    : "先在「高级设置 → LLM 增强设置」里填好接口地址、模型名与密钥";
}

/* 面板里的「执行」。用的是**左边 textarea 此刻的内容** —— 不是打开面板时那份。 */
async function runEnhance() {
  const session = enhanceSession;
  if (!session) return;
  const target = session.target;
  const action = session.action;
  const act = enhanceActionInfo(action);
  const text = $("en-input").value.trim();
  if (!text) {
    $("en-status").textContent = "左边是空的，先写点要做「" + act.label + "」的内容。";
    $("en-status").classList.add("bad");
    return;
  }
  const draft = enhanceDraft(target, false);
  draft.input = $("en-input").value;
  // 换了输入就把旧稿标成过期（**别让一份对不上现在这段输入的稿子留在那儿**）
  draft.result = "";
  draft.meta = "";
  const btn = $("en-run");
  btn.disabled = true;
  $("en-status").classList.remove("bad");
  renderEnhancePanel();
  // 参考图**在这一刻**取（面板开着的时候表单动不了 —— 它是个模态 `<dialog>`）。
  // 发出去的份数以**回执**为准（`out.images`）：读盘那一步失败时服务端会整次
  // 400，页面自己数的那个数在那时是错的。
  const refs = llmRefs();
  $("en-status").textContent = "正在" + act.label + "…（" + text.length + " 字"
    + (refs.length ? " + " + refs.length + " 张参考图" : "") + "发出去）";
  try {
    const out = await postJSON("/api/llm/enhance", { text, target, action, refs });
    // 请求飞在路上时用户可能换了动作、换了目标、或者干脆把面板关了 —— 那这一份
    // 就**不再对应**面板上此刻显示的东西，写进去只会让人「采用」到错的那一份。
    if (enhanceSession !== session || session.action !== action
        || session.target !== target) return;
    draft.result = out.text;
    draft.meta = (out.model || "") + " · "
      + (out.elapsed_ms / 1000).toFixed(1) + " 秒"
      + (out.images ? " · " + out.images + " 张图" : "")
      + (out.usage && out.usage.total_tokens
        ? " · " + out.usage.total_tokens + " tokens" : "");
    renderEnhancePanel();
  } catch (e) {
    if (enhanceSession !== session) return;
    // **失败要说在面板里**，而且留在那儿 —— 上一版把错误写进按钮旁边一个
    // 小字 span，用户根本没看见，只看见按钮卡住了。
    $("en-status").textContent = act.label + "失败：" + e.message;
    $("en-status").classList.add("bad");
    $("en-to").textContent = "（这次没有拿到" + act.noun + "）";
    $("en-to").classList.add("empty");
    $("en-to-count").textContent = "";
    $("en-adopt").disabled = true;
    $("en-send").disabled = true;
  } finally {
    // 只恢复「可点」；**按钮文字从头到尾没被改过**
    btn.disabled = false;
  }
}

function adoptEnhance() {
  const session = enhanceSession;
  if (!session) return;
  const info = enhanceTargetInfo(session.target);
  const draft = session.drafts[session.target];
  if (!draft || !draft.result) return;
  const box = info.field ? $(info.field) : null;
  if (box) {
    box.value = draft.result;
    // 长度计数与预检都要跟着走 —— 少了这一步，字数显示与提交内容会对不上。
    const counter = info.field === "prompt" ? "prompt-len" : "negative-len";
    if ($(counter)) $(counter).textContent = box.value.length;
    // 这一栏的值是**程序写进去**的，不派发 input 的话不会有别的处理跑起来
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }
  scheduleBuild();
  enhanceSession = null;
  $("enhance").close();
  flashButton($("btn-enhance"), "已采用");
}

function closeEnhance() {
  $("enhance").close();     // 什么都不动 —— Esc / × / 放弃 都走这里
  enhanceSession = null;
}

/* 接力：把右栏这一稿搬到左边，好接着做下一道（典型用法是「先增强成中文，
 * 再译成英文」）。
 *
 * 三条不能忘：
 *  1. **只动左边那个输入框**，不写回表单 —— 写回表单是「采用」的事，两者混在
 *     一起的话「我不想改表单，只是想接着做一道」就没有出路了。
 *  2. 搬完立刻**作废右栏**（与「改了输入就作废」同一条规矩）：右边那份稿子现在
 *     对应的是左边这段字，而左边已经被换掉了。留着的后果是「采用」把它写回去，
 *     而它对应的不是你眼前这段输入。
 *  3. **不派发 input 事件**：`en-input` 上那个监听器做的就是「改了输入就作废」，
 *     这里已经作废过了；派发一次只是让同一件事跑两遍。 */
function sendEnhanceToInput() {
  const session = enhanceSession;
  if (!session) return;
  const draft = session.drafts[session.target];
  if (!draft || !draft.result) return;
  draft.input = draft.result;
  draft.result = "";
  draft.meta = "";
  renderEnhancePanel();
  $("en-status").textContent = "已放到左边。选好「做什么」再点「执行」。";
  $("en-input").focus();
}

function initEnhance() {
  $("btn-enhance").onclick = () => openEnhancePanel("prompt");
  $("btn-llm-save").onclick = saveLLMSettings;
  $("en-run").onclick = runEnhance;
  $("en-adopt").onclick = adoptEnhance;
  $("en-send").onclick = sendEnhanceToInput;
  $("en-discard").onclick = closeEnhance;
  $("en-close").onclick = closeEnhance;
  $("en-target").onchange = () => switchEnhanceTarget($("en-target").value);
  $("en-action").onchange = () => switchEnhanceAction($("en-action").value);
  // 左边打字时只刷字数与「采用」的可用性 —— **不重写 textarea 自己**
  $("en-input").addEventListener("input", () => {
    if (!enhanceSession) return;
    const draft = enhanceDraft(enhanceSession.target, false);
    draft.input = $("en-input").value;
    $("en-from-count").textContent = draft.input.length + " 字";
    // 输入一动，右边那份稿子就**对不上了** —— 立刻作废，别让一份对不上的稿子
    // 留在那儿：那会让人以为它就是你眼前这段输入的增强结果，而「采用」一按
    // 就把它写回表单。这不是防错，是防「没有症状的错」。
    if (draft.result) {
      draft.result = "";
      draft.meta = "";
      renderEnhancePanel();
    }
  });
  // Esc 与 <dialog> 的默认关闭都走这里：**默认必须是「放弃」**。
  // 让 Esc 等于「采用」的话，一个想关掉窗口的动作会改掉你的提示词。
  $("enhance").addEventListener("close", () => { enhanceSession = null; });
}

/* ---------- 启动 ---------- */
/* 表单「换了要跑什么」这件事**只有这一处** —— `#mode` 下拉框与三颗链路按钮
 * 都调它。两处各写一份的坏法：点了链路之后不重算预检（页面还显示上一档的
 * 警告与确认）、或者不拉 input 清单（源图那栏说「正在读…」却永远读不完）。 */
function formChanged() {
  applyModeUI();
  // 切进图生图 / 放大 / H3 / 拆层时现拉一次源图清单 —— 后端可能刚起、
  // 也可能刚被别处改过。判据用**有效模式**（链路可能盖过下拉框）。
  const mode = state.chain === "qwen" ? $("mode").value : state.chain;
  if (["edit", "upscale", "h3", "layers"].includes(mode)) fetchInputs();
  scheduleBuild();
}

function init() {
  initResultTabs();
  initChainTabs();
  newSeed();
  applyModeUI();
  $("prompt-len").textContent = "0";
  render();

  const onChange = () => formChanged();
  $("mode").addEventListener("change", onChange);
  $("size").addEventListener("change", onChange);
  for (const id of ["prompt", "negative", "steps", "width", "height", "seed", "cfg",
                    "sampler", "scheduler", "scale", "lazy_cache",
                    "te_speed", "turbo", "rewrite", "auto_upscale", "auto_scale"]) {
    $(id).addEventListener("input", () => {
      if (id === "prompt") $("prompt-len").textContent = $("prompt").value.length;
      if (id === "negative") $("negative-len").textContent = $("negative").value.length;
      // 两个增强按钮的可用性跟着**框里的内容**走：空的就没有可增强的东西。
      // 不刷的话，删光了字那颗按钮还亮着，点下去才收到一句错误。
      if (id === "prompt" || id === "negative") syncEnhanceButtons();
      // 两个加速开关插在同一条 model 连线上，同时开等于互相抢线。与其让用户
      // 提交后收到一句 400，不如勾一个就把另一个弹掉（后端也仍然拦着）。
      if (id === "te_speed" && $("te_speed").checked) $("lazy_cache").checked = false;
      if (id === "lazy_cache" && $("lazy_cache").checked) $("te_speed").checked = false;
      // 链在扫描里不生效 —— 勾/取消要当场改那句话
      if (id === "auto_upscale") renderSweepChainNote();
      scheduleBuild();
    });
  }
  $("btn-dice").onclick = () => { newSeed(); scheduleBuild(); };
  $("btn-run").onclick = run;
  // 「让它生效」那个按钮上的数值由 `renderNegative` 从后端取（st.suggest），
  // 这里只负责把值应用回表单再重算 —— 硬编码 2.0 会和后端分家。
  $("btn-raise-cfg").onclick = () => {
    const st = state.build && state.build.negative_prompt;
    $("cfg").value = (st && st.suggest) || 2.0;
    scheduleBuild();
  };
  initDropzone();
  initLightbox();
  initLogsView();
  initEnhance();
  $("btn-sweep").onclick = runSweep;
  $("be-switch").onclick = toggleBackend;
  $("btn-refresh").onclick = () => { fetchBackend(); fetchJobs(); fetchGallery(); fetchInputs(); };
  $("btn-clean-jobs").onclick = cleanJobs;
  $("btn-save-prompt").onclick = savePrompt;
  $("btn-gallery-refresh").onclick = fetchGallery;

  // 画廊的多选与分页（P20f）。翻页/搜索都从第 0 页起 —— 页码是**上一批筛选结果**
  // 里的位置，换了筛选还留着它，落点就没有意义了。
  $("btn-pick-all").onclick = pickAllOnPage;
  $("btn-pick-none").onclick = () => { state.galleryPick.clear(); syncPickUI(); };
  // 那一档换的是**扫哪个目录**，所以切换后回到第 1 页：页码是上一批结果里的位置，
  // 换了目录还留着它，落点就没有意义了（与换 chip、换搜索词同一条规矩）。
  $("btn-archived").onclick = () => {
    state.galleryArchived = !state.galleryArchived;
    state.galleryPage = 0;
    fetchGallery();
  };
  // 同一颗按钮，两档两件事：正常档归档、归档档彻底删除。**分支写在这里**，而不是
  // 让它自己去读状态 —— 读状态的那些写法，在「回执还没回来」的那一瞬最容易分家。
  $("btn-delete").onclick = () => pickedAction(
    state.galleryArchived ? "purge" : "archive");
  $("btn-restore").onclick = () => pickedAction("restore");
  $("btn-page-prev").onclick = () => goToPage(-1);
  $("btn-page-next").onclick = () => goToPage(1);
  // 列数变了，每页几张就跟着变 —— 不重量就会一直用旧页大小，而那条
  // 「每页 3 行」的承诺会悄悄失效（窗口一变宽，页就只剩两行多）。
  let galleryResize = null;
  window.addEventListener("resize", () => {
    if (state.resultTab !== "gallery") return;
    clearTimeout(galleryResize);
    galleryResize = setTimeout(remeasureGallery, 250);
  });

  let galleryTimer = null;
  $("gallery-search").addEventListener("input", () => {
    state.galleryQuery = $("gallery-search").value;
    state.galleryPage = 0;
    clearTimeout(galleryTimer);
    galleryTimer = setTimeout(fetchGallery, 300);
  });
  let libTimer = null;
  $("lib-search").addEventListener("input", () => {
    state.libraryQuery = $("lib-search").value;
    clearTimeout(libTimer);
    libTimer = setTimeout(fetchLibrary, 300);
  });

  setInterval(tickElapsed, 250);
  setInterval(fetchBackend, 15000);
  setInterval(renderQueue, 5000);
  // 扫描的格子是**普通任务**，所以「跑完没跑完」看的是同一条任务通道。
  // 有在跑的格子就定期刷新，没在跑就完全不动 —— 免得空转着打后端。
  setInterval(() => {
    const d = state.sweepId ? state.sweeps.get(state.sweepId) : null;
    if (d && d.finished < d.count) fetchSweep(state.sweepId);
  }, 3000);
  // 启动中是唯一需要高频盯着的状态（页面可能刚打开、或从别的标签页发起的）。
  // 别的阶段完全不轮询 —— SSE 会推。走 `tickLaunch`（只读本进程的状态），
  // **不走 `/api/backend`**：后者要向后端要统计，而它此刻还没就绪。
  setInterval(() => {
    const launch = (state.backend || {}).launch;
    if (launch && launch.phase === "starting") tickLaunch();
  }, 1000);

  fetchBackend();
  fetchJobs();
  refreshBuild();
  fetchGallery();
  fetchLibrary();
  fetchInputs();
  fetchLLM();
  connect();
  // 「我跑完了」的唯一标记（P20k）。`index.html` 顶上那段守卫 2.5 秒后看它：
  // 没置就把「脚本没跑起来」那条横幅画出来（横幅是那一页唯一的自己解释自己）。
  window.__wb_booted = true;
}

document.addEventListener("DOMContentLoaded", init);
