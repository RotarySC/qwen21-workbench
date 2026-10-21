/* 在没有浏览器的情况下验**表单行为**。
 *
 * 为什么需要它：这个 agent 没有浏览器驱动，页面从没在真浏览器里加载过
 * （见 AGENTS.md「页面渲染没被真正看过」）。`test_api.py::FrontendContract`
 * 守的是 **一致性**（id/SSE/路由对得上、`node --check` 过），但它不知道
 * 「切到放大模式以后提示词框到底藏没藏」「collectBody 发出去的 body 长什么样」。
 * 而后者正是前后端之间那份契约 —— 发错了只有到浏览器里才会发现。
 *
 * 做法：`app.js` 只需要 `document.getElementById`，所以用 `vm` 喂一个假 DOM，
 * 再把它导出的两个函数调一遍，把结果打成 JSON 交给 Python 断言。
 * **不是**在模拟浏览器渲染，也不假装是：这里只验表单逻辑。
 *
 * 用法：node form_probe.js <app.js 的绝对路径>
 */
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(process.argv[2], "utf8");

function makeEl(id) {
  const classes = new Set();
  const attrs = {};
  const el = {
    id,
    value: "",
    checked: false,
    disabled: false,
    // `logsOpen()` 读 `$("logview").hidden`，而 `openLogs/closeLogs` 用的正是
    // 这个**属性**（不是 `.hidden` 类）。假 DOM 少了它，碰它的代码会当场炸 ——
    // 那是**假失败**（浏览器里明明没事）。
    hidden: false,
    textContent: "",
    innerHTML: "",
    title: "",
    // `syncPromptLoadButtons()` 读 `dataset.why`，并把放大模式那条禁用理由写到
    // `title` 上、切回来时 `removeAttribute("title")` —— 这两个都是**标准 DOM 面**，
    // 假 DOM 少了它们会当场炸，而那是**假失败**（浏览器里明明没事）。
    dataset: {},
    _classes: classes,
    classList: {
      add: (c) => { classes.add(c); },
      remove: (c) => { classes.delete(c); },
      contains: (c) => classes.has(c),
      toggle: (c, on) => {
        const want = (on === undefined) ? !classes.has(c) : !!on;
        if (want) { classes.add(c); } else { classes.delete(c); }
        return want;
      },
    },
    setAttribute: (key, value) => { attrs[key] = value; },
    getAttribute: (key) => attrs[key],
    removeAttribute: (key) => {
      delete attrs[key];
      if (key === "title") el.title = "";
    },
    // P21c：`placeChainThumb()` 量 `offsetLeft`/`offsetWidth` 并往滑块的
    // `style.transform` / `style.width` 上写。假 DOM 少了这三样会**当场炸**，
    // 而那是**假失败**（浏览器里明明没事）。值一律 0 就够了 ——
    // 探针验的是「有没有的那一格」，不是「滑块滑到了哪」（那个只有真布局说了算）。
    style: {},
    offsetLeft: 0,
    offsetWidth: 0,
    // `renderPicked()` 会去这一栏里找缩略图来挂 onerror。假 DOM 里没有子节点，
    // 但**这个属性必须存在** —— 少了它 `uploadRefs()` 会当场炸，而那是假失败。
    querySelectorAll: () => [],
  };
  return el;
}

// app.js 里 `$("...")` 会碰到的全部 id（含新增的几个）。
// 注意**没有** `source_image`：手打文件名那条路按用户要求已经去掉，选中集
// 搬进了 `state.refs`（下面用 probeApi.state.refs 驱动），页面上不再有那个框。
const IDS = ["mode", "prompt", "steps", "width", "height", "seed", "cfg",
             "sampler", "scheduler", "scale", "lazy_cache",
             "te_speed", "turbo", "size", "size-custom-wrap", "src-wrap", "src-label",
             "upscale-wrap", "prompt-wrap", "library", "gen-row", "cfg-wrap",
             "advanced-wrap", "prompt-len", "negative", "negative-wrap",
             "negative-hint", "negative-len",
             // 提示词改写器：**额外功能**（2026-10-01 起不再是模式 `refine`）。
             // `applyModeUI()` 按模式显隐 `rewrite-wrap` —— 少了它 `$()` 会
             // 返回 null，整个探针当场炸（第一次就是这么炸的）。
             "rewrite", "rewrite-wrap",
             // 出图后自动放大（串联）：同一类东西，同一个坑
             "auto_upscale", "auto_scale", "chain-wrap", "sweep-chain-note",
             // 放大视图里「复制」那一族（2026-10-01）：提示词两段各一块 + 参数一个
             // 按钮。`renderLightbox` 一上来就要写这三样，探针里少一个就当场炸。
             "lb-prompt", "lb-negative", "lb-negative-wrap", "lb-copy-params",
             // 「载入提示词」在放大模式下要禁用，而 `applyModeUI()` 会刷它的状态
             "lb-load-prompt",
             // 上传与选中集那一栏
             "src-file", "upload-msg", "src-info", "ref-picked", "btn-clear-ref",
             // 虚线框下面那句提示。`applyModeUI()` 会按模式改它的文案
             // （放大只用第一张 / 图生图可多选），所以探针里必须存在 ——
             // 少了它 `$()` 返回 null，整个探针会当场炸。
             "ref-drop-note",
             // P20：**只有一颗**「LLM 增强」入口（2026-10-01 改的形状：输入与增强
             // 都在弹出面板里做，所以不再每个框一颗按钮）。`applyModeUI()` 会显隐
             // `prompt-actions` —— 那颗按钮在 `<label>` **外面**（在里面点它会被
             // label 连带激活 textarea），所以它不跟着 `prompt-wrap` 一起藏；
             // `syncEnhanceButtons()` 又会写 `btn-enhance` 的 disabled / title。
             // 少一个就当场炸，而那是**假失败**（浏览器里明明没事）。
             "prompt-actions", "btn-enhance",
             // P20e：面板顶栏下面那一行「这次带不带参考图」（`renderEnhanceRefs()`
             // 写它）。它由 `renderEnhancePanel()` 调 —— 探针现在不跑那个函数，
             // 但 id 清单是「app.js 里 `$()` 会碰到的全部」，少一个就是在给
             // 下一次「当场炸」埋伏笔。
             "en-refs",
             // P21c：链路选择与两条新链路的字段块。
             //   · `chain-tabs` / `chain-thumb` / `chain-<名>` —— `setChain()` 会写
             //     每颗的 aria-checked 与 tabIndex，`placeChainThumb()` 会量
             //     `offsetLeft`/`offsetWidth` 并写滑块的 transform；
             //   · `chain-tag` —— 角标随链路变（页面上写着 QWEN 而跑 H3 是假话）；
             //   · `mode-wrap` —— Qwen 那一档才显示「生成模式」下拉框；
             //   · `steps-wrap` / `size-wrap` —— 拆层要步数**不要**尺寸，
             //     所以那一行不能整块藏（拆层会连步数一起没了）；
             //   · `h3-wrap` / `h3-seconds` / `h3-frames`、`layers-wrap` /
             //     `layers-resolution` / `layers-depth`；
             //   · `sweep-steps-wrap` —— H3 的 steps 轴后端会当场拒，所以藏掉。
             // 少任何一个 `$()` 都返回 null，整个探针当场炸（那是**假失败**）。
             "chain-tabs", "chain-thumb", "chain-tag",
             "chain-qwen", "chain-h3", "chain-layers",
             "mode-wrap", "steps-wrap", "size-wrap", "sweep-steps-wrap",
             "h3-wrap", "h3-seconds", "h3-frames",
             "layers-wrap", "layers-resolution", "layers-depth"];
const els = {};
for (const id of IDS) { els[id] = makeEl(id); }
els.size.value = "1024x1024";

/* 一个只够跑「上传」这一段的假后端。
 *
 * `/api/upload` 回**后端改过的那个名字**（真后端撞上同名会改，见 upload.py），
 * 这里刻意让它与请求里的名字一致 —— 探针验的是「两批会不会累积」，
 * 不是「改名对不对」（改名由 test_upload.py 那层真后端验）。 */
const uploaded = [];
const sandbox = {
  document: {
    getElementById: (id) => (id in els ? els[id] : null),
    addEventListener: () => {},
    querySelectorAll: () => [],
  },
  window: { addEventListener: () => {}, scrollTo: () => {} },
  navigator: {},
  console,
  // 定时器一律不真跑：`uploadRefs()` 结尾会 `scheduleBuild()`，那会去问
  // `/api/build` 并渲染估算 —— 而这一层只验表单逻辑，那些 id 根本不在
  // 假 DOM 里。真跑起来就是一个**假失败**（async 里抛错，node 以 1 退出）。
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  fetch: async (path) => {
    if (path.indexOf("/api/upload") === 0) {
      const name = decodeURIComponent(path.split("filename=")[1] || "");
      uploaded.push(name);
      return { ok: true, status: 200,
               json: async () => ({ filename: name, verified: true, warning: null }) };
    }
    if (path === "/api/inputs") {
      // 上限刻意设成 3：这样「两批累积」与「超上限整批拒绝」都能用少量文件试出来。
      return { ok: true, status: 200,
               json: async () => ({ files: uploaded.slice(), max_references: 3,
                                    verified: true, root_label: "test-input",
                                    how: "", root: "" }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  },
};
vm.createContext(sandbox);
// 顶层 `function` 声明会挂在全局对象上，但把它们显式导出来更稳
// （不依赖 vm 的作用域细节，也不受将来加 "use strict" 影响）。
vm.runInContext(src +
  "\n;globalThis.__probe = { collectBody, applyModeUI, uploadRefs, state, " +
  "normalizeRefs, syncEnhanceButtons, setChain, setChainForMode };",
  sandbox);
const probeApi = sandbox.__probe;

const hidden = (id) => els[id]._classes.has("hidden");

function probe(mode, over, chain) {
  // 每次都先摆成「出图」的样子，再切到目标模式 —— 这样验的是**切换**，
  // 而不是「一开始就藏着」。
  // 走 `setChain()` 而**不是**直接写 `state.chain`：真页面上换链路只有那一条路，
  // 它顺带写角标与滑块。绕过去的话探针读到的角标永远是空的 —— 一个假失败。
  probeApi.setChain("qwen", false);
  els.mode.value = "t2i";
  probeApi.applyModeUI();
  els.prompt.value = "a green apple on a table";
  els.steps.value = "20";
  els.seed.value = "7";
  els.cfg.value = "1.0";
  els.sampler.value = "euler";
  els.scheduler.value = "simple";
  // 选中集：`state.refs` 是唯一真源。`refs` 给数组，`refsText` 给字符串
  // （老记录里那一份是逗号分隔的一串，`normalizeRefs` 两种都要吃）。
  probeApi.state.refs = probeApi.normalizeRefs(["a.png"]);
  els.scale.value = "2";
  els.size.value = "1024x1024";
  els.negative.value = "";
  els.lazy_cache.checked = false;
  els.te_speed.checked = false;
  els.rewrite.checked = false;
  els.auto_upscale.checked = false;
  els.auto_scale.value = "2";
  els["h3-seconds"].value = "5";
  els["layers-resolution"].value = "1024";
  els["layers-depth"].value = "-1";
  // LLM 增强的可用性由「配没配好」决定，所以每一轮都得先摆回「没配」——
  // 靠上一轮的余温会让新增用例把判据搞红，而报错长得像产品坏了。
  probeApi.state.llm = null;
  for (const [k, v] of Object.entries(over || {})) {
    if (k === "refs") { probeApi.state.refs = probeApi.normalizeRefs(v); continue; }
    if (k === "refsText") { probeApi.state.refs = probeApi.normalizeRefs(v); continue; }
    if (k === "llmReady") {
      probeApi.state.llm = v ? { settings: { configured: true } } : null;
      continue;
    }
    els[k].value = String(v);
    els[k].checked = !!v;        // 对 input 无害；对复选框才是重点
  }

  // P21c：`chain` 决定「去哪儿取 mode」——Qwen 那一档取下拉框，另两条链路
  // mode 就是链路本身。不传就是 qwen（老用例的行为一字不变）。
  probeApi.setChain(chain || "qwen", false);
  els.mode.value = mode;
  probeApi.applyModeUI();
  // 增强按钮的可用性**不由 `applyModeUI` 负责**（它的判据不读模式，只看
  // 「框里有没有字」与「LLM 配没配好」），所以这里显式驱动一次 —— 与上面
  // 驱动 `collectBody` 是同一个道理：探针测的是那个函数，不是「谁该调它」。
  probeApi.syncEnhanceButtons();
  const body = probeApi.collectBody();
  return {
    mode,
    chain: probeApi.state.chain,
    chainTag: els["chain-tag"].textContent,
    body,
    hidden: {
      prompt: hidden("prompt-wrap"),
      negative: hidden("negative-wrap"),
      library: hidden("library"),
      gen: hidden("gen-row"),
      cfg: hidden("cfg-wrap"),
      advanced: hidden("advanced-wrap"),
      // 改写器只在文生图下露出来（图生图那条链路把它整段删掉了）
      rewrite: hidden("rewrite-wrap"),
      // 出图后自动放大：放大自己就是那条链，所以放大模式下藏起来
      chain: hidden("chain-wrap"),
      src: hidden("src-wrap"),
      upscale: hidden("upscale-wrap"),
      // P20：增强那颗按钮得跟着提示词一起藏 —— 放大模式下没有提示词输入框，
      // 留着一颗亮着的按钮去增强一个不在页面上的框，就是没有症状的错。
      promptActions: hidden("prompt-actions"),
      // P21c：两组，各自是两个坑。
      //   `mode`  —— 另两条链路不在那个下拉框的选项里，它必须整格藏掉；
      //   `steps` / `size` —— 拆层要步数但**不要**尺寸，所以它们得**分开判**。
      //     整行一起藏会让拆层连步数一起没了（不报错，只是少一格控件）。
      modeSelect: hidden("mode-wrap"),
      steps: hidden("steps-wrap"),
      size: hidden("size-wrap"),
      h3: hidden("h3-wrap"),
      layers: hidden("layers-wrap"),
      sweepSteps: hidden("sweep-steps-wrap"),
      sizeCustom: hidden("size-custom-wrap"),
    },
    // P20：那颗增强入口的可用性与**理由**。现在只有一颗，而且**空着也叫得动** ——
    // 面板里就是输入区，空着进去再写正是这个形状的意义。
    enhance: {
      disabled: !!els["btn-enhance"].disabled,
      title: els["btn-enhance"].title,
    },
    srcLabel: els["src-label"].textContent,
    srcWrapClasses: [...els["src-wrap"]._classes].sort(),
  };
}

const out = {
  t2i: probe("t2i"),
  edit: probe("edit"),
  upscale: probe("upscale"),
  // ---- P21c：两条新链路。它们**不在 `#mode` 的选项里** —— 链路是另一级选择，
  // 所以这里把 `mode` 参数也写成链路名（线上只有一个 `mode` 字段，五个合法值）。
  //
  // `chain` 那个第三参数就是分段控件的选择；不传 = qwen，所以上面那些老用例
  // 的行为一字未变。
  h3: probe("h3", null, "h3"),
  layers: probe("layers", null, "layers"),
  // H3 只用一张首帧：选了两张时发出去的必须只有第一张（图里只有一个 LoadImage）。
  h3_truncates_refs: probe("h3", { refs: ["a.png", "b.png"] }, "h3"),
  // 拆层同理。
  layers_truncates_refs: probe("layers", { refs: ["a.png", "b.png"] }, "layers"),
  // 拆层**要步数、不要尺寸** —— 这两个得分开判（整行一起藏会让拆层连步数一起没了）。
  layers_keeps_steps: probe("layers", { steps: "40" }, "layers"),
  // 三条链路上出图那几个加速开关都必须**如实发 false**（图里根本没有那些节点）。
  h3_clears_accels: probe("h3", { te_speed: true, lazy_cache: true, turbo: true },
                          "h3"),
  layers_clears_accels: probe("layers", { te_speed: true, lazy_cache: true,
                                          turbo: true }, "layers"),
  // 时长与深度分辨率：只在各自那一档发得出数，别的档恒 null。
  h3_seconds_10: probe("h3", { "h3-seconds": "10" }, "h3"),
  layers_depth_512: probe("layers", { "layers-depth": "512",
                                      "layers-resolution": "1280" }, "layers"),
  // 角标随链路变：页面上写着 QWEN-IMAGE-2.1 而跑出来的是 H3 的视频，就是一句假话。
  qwen_tag: probe("t2i"),
  // ★ **可见性矩阵**：把五种组合全跑一遍，Python 那边逐格断言。
  // 只测「H3 藏了模式下拉」是不够的 —— 少一格就是「一个不生效的框留在页面上」，
  // 而那正是这一层要防的那类错。
  matrix: ["t2i", "edit", "upscale", "h3", "layers"].map((m) =>
    probe(m, null, ["h3", "layers"].includes(m) ? m : "qwen")),
  // 三种布局都跑一遍「有效模式」的算路：链路盖过下拉框。
  // 这是「线路上只有一个 mode 字段」那条规矩的**唯一入口**，写错了会让
  // H3 那一档发出 `t2i`（而页面看着一切正常）。
  effective_mode: [
    { chain: "qwen", select: "t2i" },
    { chain: "qwen", select: "upscale" },
    { chain: "h3", select: "t2i" },        // 下拉框停在 t2i，链路必须赢
    { chain: "layers", select: "upscale" },  // 下拉框停在 upscale，链路必须赢
  ].map((c) => {
    probeApi.state.chain = c.chain;
    els.mode.value = c.select;
    return { chain: c.chain, select: c.select,
             body_mode: probeApi.collectBody().mode };
  }),
  // `setChainForMode` 的反向映射 —— 「载入参数」与「最近任务」回填都走它。
  // 老值 `2k` / `refine` 也要落到 qwen 那一档（与 `LEGACY_FORM_MODE` 同一结论）。
  chain_for_mode: ["t2i", "edit", "upscale", "h3", "layers", "2k", "refine",
                   "nonsense"].map((m) => {
    probeApi.state.chain = "qwen";
    return { mode: m, chain: probeApi.setChainForMode(m, false) };
  }),
  // 放大时那几个加速复选框是**藏起来的**。要是用户先勾了再切过来，
  // 它们绝不能被带进请求 —— 否则记录里会写着「这次开了 TE-Speed」，
  // 而那张 VOSR2 图根本没经过 KSampler。那是一条假的参数记录。
  upscale_with_accels: probe("upscale", { te_speed: true, lazy_cache: true,
                                          turbo: true }),
  // 反向词：填了就该**如实进 body**。它生不生效是后端的判断（CFG≤1 时不生效），
  // 前端只负责别自己吞掉它。
  negative_filled: probe("t2i", { negative: "blurry, watermark" }),
  // 放大图里没有文本编码器，所以反向词必须被清成空串 —— 和 prompt 一样，
  // 不能让记录里留下一个"这次用了反向词"而实际根本没经过文本编码器。
  upscale_clears_negative: probe("upscale", { negative: "blurry, watermark" }),
  // 多张参考图：复数键要真的有那么多张，而单数键恒等于第一张
  // （**第一张决定输出尺寸**，见 nodes_qwen.py）。
  multi_refs: probe("edit", { refs: ["a.png", "b.png"] }),
  // 老记录里那份是**逗号分隔的字符串**（`source_image`），照样要能解析：
  // 去重、保序、而且不把带空格的文件名劈开。
  messy_refs: probe("edit", { refsText: " a.png ,b.png, a.png" }),
  spaced_name_refs: probe("edit", { refsText: "my photo.png, b.png" }),
  // 放大**只吃一张**。选了两张时，发出去的必须只有第一张 ——
  // 否则后端会为一张 VOSR2 图收到两张源图，而那张图根本没有第二个输入。
  upscale_truncates_refs: probe("upscale", { refs: ["a.png", "b.png"] }),
  // 提示词改写器：**额外功能**（2026-10-01 起不再是一个模式）。
  // 文生图下勾了就是 `rewrite: true`；图生图 / 放大下它藏起来，而且**必发 false** ——
  // 后端也会拒，但前端先说实话，免得记录里留下一条假参数。
  rewrite_on_t2i: probe("t2i", { rewrite: true }),
  rewrite_on_edit: probe("edit", { rewrite: true }),
  rewrite_on_upscale: probe("upscale", { rewrite: true }),
  // 出图后自动放大（串联）：文生图 / 图生图下能开；放大自己就是那条链，
  // 所以那边藏起来、而且**必发 false**（叠一次只是换个更大的倍数）。
  chain_on_t2i: probe("t2i", { auto_upscale: true, auto_scale: "4" }),
  chain_on_edit: probe("edit", { auto_upscale: true }),
  chain_on_upscale: probe("upscale", { auto_upscale: true }),
  // 一张都没选：单数是 null、复数是空表（**不是** ""/[""]）
  no_refs: probe("edit", { refs: [] }),
  // ---- P20：LLM 增强那一颗入口 ----
  // 没配好（默认）：禁用，而且**说得出理由**（title 指向设置那一块）。
  enhance_unconfigured: probe("t2i"),
  // 配好了：可用。
  enhance_ready: probe("t2i", { llmReady: true }),
  // **框是空的照旧可用** —— 这是新形状最要紧的一处差别：输入在面板里，
  // 空着进去再写正是它的用法（旧形状是「框空就禁用」，那颗按钮属于那个框）。
  enhance_empty_box: probe("t2i", { llmReady: true, prompt: "" }),
  // 放大模式：那一行按钮跟着藏（放大没有提示词输入框，也没有反向词）。
  enhance_on_upscale: probe("upscale", { llmReady: true }),
};

/* 上传：**两批要累积**（用户这次要求改的就是它），超上限要整批拒绝。
 *
 * 这一段是异步的（`uploadRefs` 要 `await fetch`），所以整个收尾放进 IIFE ——
 * stdout 在 await 都落定之后才写，Python 那边读到的仍是完整 JSON。 */
(async () => {
  // 起点**明确写成空的**：以前这里默默依赖「最后一个探针（`no_refs`）把选中集
  // 清空了」，于是往 `out` 里多加一个探针就会把这一段判据搞红，而报错长得
  // 像「上传不累积了」—— 一个假警报。状态由自己摆，别靠上一段的余温。
  probeApi.state.refs = [];
  const upload = async (names) => {
    els["upload-msg"].textContent = "";
    els["upload-msg"].innerHTML = "";
    await probeApi.uploadRefs(names.map((n) => ({ name: n, size: 2048 })));
    // 回话是 innerHTML（里头有 `<span class="ok/bad">`），所以两边都带出来。
    return { refs: probeApi.state.refs.slice(),
             msg: els["upload-msg"].innerHTML || els["upload-msg"].textContent };
  };
  const first = await upload(["one.png", "two.png"]);
  out.upload_first_batch = first;
  // 第二批：**只多两张**，前面那两张必须还在（旧行为是替换，会只剩这两张）。
  const second = await upload(["three.png"]);
  out.upload_second_batch = second;
  out.upload_accumulated = probeApi.state.refs.slice();
  // 已经 3 张（上限也是 3）：再来一张，整批不传，而且**不许留下半截状态**。
  const over = await upload(["four.png"]);
  out.upload_over_limit = over;
  // 空选：什么都不做
  out.upload_empty = await upload([]);
  out.upload_calls = uploaded.slice();
  process.stdout.write(JSON.stringify(out, null, 1));
})();
