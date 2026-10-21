/* Execute the real view handlers against a small DOM double. Rendering is
 * checked separately in a browser; this probe guards view and form state. */
"use strict";
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[3], "utf8");
const els = {};
let focused = null;
let requests = [];
let runFailure = false;
function makeEl(id) {
  const classes = new Set();
  const attrs = {};
  const listeners = {};
  return {
    id, value: "", checked: false, hidden: false, tabIndex: 0,
    textContent: "", innerHTML: "", scrollTop: 0, disabled: false,
    options: [], _listeners: listeners,
    /* 真元素上这些属性都有；假 DOM 少了它们，凡是碰布局的代码都会**当场炸**，
     * 而那是**假失败** —— 浏览器里明明没事。所以照抄真元素的最小面。
     * offsetLeft/offsetWidth 由下面按 TAB_POS 喂进去：这里量的是
     * 「JS 有没有把正确的数值写进 CSS」，**布局本身归浏览器截图管**。 */
    style: {}, dataset: {}, offsetLeft: 0, offsetWidth: 0,
    setAttribute: (key, value) => { attrs[key] = value; },
    getAttribute: (key) => attrs[key],
    // `syncPromptLoadButtons()` 在放大模式下会把禁用理由从 `title` 上撤掉 ——
    // 少了这个面，凡是「按状态加减属性」的代码都会当场炸（假失败）。
    removeAttribute: (key) => { delete attrs[key]; },
    addEventListener: (key, fn) => { listeners[key] = fn; },
    querySelectorAll: () => [],
    // `renderLive()` 把面板渲完之后要回头挂链上那颗「看放大后的图」的 onclick，
    // 找它用的就是 `querySelector` —— 真元素上当然有，假 DOM 少了它整支会**当场炸**
    // （那是假失败：浏览器里明明没事）。找不到就是 `null`，与真元素一致。
    querySelector: () => null,
    focus: () => { focused = id; },
    scrollTo: ({ top }) => { els[id].scrollTop = top; },
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => {
        const value = on === undefined ? !classes.has(name) : on;
        if (value) classes.add(name); else classes.delete(name);
        return value;
      },
    },
  };
}
for (const match of html.matchAll(/id="([^"]+)"/g)) els[match[1]] = makeEl(match[1]);
class FakeEvents {
  constructor() { this.listeners = {}; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  emit(name, data) { this.listeners[name]({ data: JSON.stringify(data) }); }
}
const sandbox = {
  document: {
    getElementById: (id) => els[id] || null,
    querySelectorAll: () => [], addEventListener: () => {},
  },
  window: { matchMedia: () => ({ matches: false }), scrollTo: () => {},
            addEventListener: () => {} },
  navigator: {}, console, EventSource: FakeEvents,
  // `refFromUri`（拖放那条路）要用 `URL` 与 `location`：`URL` 不是 JS 内建，
  // 新开的 vm 上下文里没有它，不补就会以 ReferenceError 当场炸 —— 而那种失败
  // 看起来像「产品坏了」，其实只是假 DOM 少了一样东西。
  // `origin` 也必须给：`refFromUri` 拿它挡外站，只给 `href` 的话每个地址都会被
  // 判成「别的站」而返回 null，而九个用例一起变成 null 看起来像产品坏了。
  URL, location: (() => { const b = new URL("http://127.0.0.1:8189/");
                          return { href: b.href, origin: b.origin }; })(),
  setTimeout: () => 0, clearTimeout: () => {},
  setInterval: () => 0, clearInterval: () => {},
  fetch: async (path, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    requests.push({ path, body });
    let data = { estimate: { human: "40 秒", parts: { sampling_s: 35, overhead_s: 5, per_step_s: 1.75 } }, warnings: [] };
    let ok = true;
    if (path === "/api/run") {
      ok = !runFailure;
      data = ok ? { job_id: "submitted-job", estimate_s: 40 } : { error: "测试提交失败" };
    } else if (path.includes("/timings")) data = { available: false };
    return { ok, status: ok ? 200 : 400, json: async () => data };
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") +
  "\n;globalThis.probe = { state, initResultTabs, setResultTab, run, selectJob, loadFromGallery, connect, renderBuildWarnings, refFromUri };", sandbox);
const api = sandbox.probe;
function form() {
  for (const [id, value] of Object.entries({ mode: "t2i", prompt: "neon skyline", steps: "20", size: "1024x1024", seed: "7", cfg: "1.0", sampler: "euler", scheduler: "simple", scale: "2" })) els[id].value = value;
  els.size.options = [{ value: "1024x1024" }];
  els.scale.options = [{ value: "2" }];
}
function view() {
  return {
    active: api.state.resultTab,
    selected: ["live", "gallery", "jobs"].filter((name) => els["tab-" + name].getAttribute("aria-selected") === "true"),
    visible: ["live", "gallery", "jobs"].filter((name) => !els["view-" + name].hidden),
    tabStops: ["live", "gallery", "jobs"].filter((name) => els["tab-" + name].tabIndex === 0),
    // 滑块是否被写到了**当前**标签的位置与宽度上（JS → CSS 那一步）
    thumb: els["result-thumb"].style.transform + " / " + els["result-thumb"].style.width,
    // 切换动画的方向（data-slide），没动画时为 null
    slide: ["live", "gallery", "jobs"].map((name) => els["view-" + name].dataset.slide || null),
  };
}
function key(name, value) {
  let prevented = false;
  els["tab-" + name]._listeners.keydown({ key: value, preventDefault: () => { prevented = true; } });
  return { ...view(), focused, prevented };
}
(async () => {
  const out = {};
  form();
  /* 假 DOM 没有布局引擎，标签的位置与宽度得**喂**进去（见 makeEl 的注释）。
   * 三个宽度刻意各不相同 —— 一样宽就验不出"写的是当前那个标签"。 */
  for (const [name, left, width] of [["live", 4, 76], ["gallery", 84, 60], ["jobs", 148, 88]]) {
    els["tab-" + name].offsetLeft = left;
    els["tab-" + name].offsetWidth = width;
  }
  api.initResultTabs();
  out.initial = view();
  els["tab-gallery"].onclick();
  out.click = view();
  els["gallery-search"].value = "blue";
  els["lib-search"].value = "saved";
  els["view-gallery"].scrollTop = 180;
  api.state.galleryQuery = "blue";
  api.setResultTab("jobs"); api.setResultTab("gallery");
  out.retained = { prompt: els.prompt.value, search: els["gallery-search"].value, librarySearch: els["lib-search"].value, scrollTop: els["view-gallery"].scrollTop, query: api.state.galleryQuery };
  out.keyboard = [key("gallery", "ArrowLeft"), key("live", "ArrowLeft"), key("jobs", "Home"), key("live", "End"), key("jobs", "ArrowRight"), key("live", "Tab")];
  api.setResultTab("gallery");
  const events = api.connect();
  const job = { job_id: "history-job", mode: "t2i", status: "running", params: { mode: "t2i", prompt: "history prompt", seed: 42 }, prompt_text: "history prompt", outputs: [] };
  events.emit("snapshot", { jobs: [job], active: [job.job_id] });
  events.emit("progress", { job_id: job.job_id, value: 4, max: 20 });
  events.emit("job_done", { job_id: job.job_id, wall_ms: 40000, outputs: [] });
  out.events = { ...view(), status: api.state.jobs.get(job.job_id).status, currentId: api.state.currentId };
  api.selectJob(job.job_id);
  out.selectedJob = { ...view(), prompt: els.prompt.value, seed: els.seed.value, focused };
  api.setResultTab("gallery");
  api.loadFromGallery({ recognized: true, mode: "t2i", filename: "saved.png", prompt: "saved prompt", seed: 99, width: 1024, height: 1024 });
  out.galleryLoad = { ...view(), prompt: els.prompt.value, seed: els.seed.value, search: els["gallery-search"].value };
  api.loadFromGallery({ recognized: false, filename: "other.png" });
  out.foreign = { ...view(), prompt: els.prompt.value, message: els["run-msg"].innerHTML };
  requests = [];
  els.prompt.value = "";
  await api.run();
  out.validation = { ...view(), submitted: requests.some((r) => r.path === "/api/run") };
  form();
  runFailure = true;
  await api.run();
  out.failedRun = { ...view(), disabled: els["btn-run"].disabled };
  runFailure = false;
  api.state.build = { estimate: { confirm: { required: true, text: "成本较高" } } };
  requests = [];
  await api.run();
  out.confirmation = { ...view(), submitted: requests.some((r) => r.path === "/api/run"), armed: api.state.confirmArmed };
  await api.run();
  out.submitted = { ...view(), request: requests.find((r) => r.path === "/api/run").body, currentId: api.state.currentId, disabled: els["btn-run"].disabled, focused };
  /* 提交前那一栏只画 warn/error（2026-10-01 按用户要求：估时行与说明性的 info
   * 都不上页面）。单独喂一份三种 level 都有的 payload，直接量那一栏的 HTML ——
   * 这是**渲染这一步**的判据，与后端给不给 info 是两件事：`/api/build` 的契约
   * 一个字没改（那边的 reading 仍在）。标记用 ASCII，断言里不会与别处的门道混。 */
  api.renderBuildWarnings({ warnings: [
    { level: "info", text: "INFO_MARKER_TE_SPEED" },
    { level: "warn", text: "WARN_MARKER_INERT" },
    { level: "error", text: "ERROR_MARKER_MODELS" }], negative_prompt: null });
  out.warningsRendered = els.warnings.innerHTML;
  /* 从拖放 URI 里认「这是哪张图」—— 只认自己那两个取图口，别的都不猜。
   * 这是「从画廊拖图」那个 bug 的**解析那一半**（另一半是真浏览器里比 sha256，
   * 在 `page_shot`）。9 个用例里 4 个必须是 null：猜错的名字会把图传成另一个
   * 名字，而那种错没有症状。 */
  out.refFromUri = [
    "http://127.0.0.1:8189/api/gallery/image?root=0&name=Qwen_a.png",
    "/api/gallery/image?root=1&name=sub%2FQwen_b.png",
    "/api/image?filename=apple_1024.png&subfolder=&type=input",
    "https://example.com/api/gallery/image?name=evil.png",
    "file:///C:/Users/x/pic.png",
    "/api/gallery/image?root=0",
    "# comment\n/api/gallery/image?root=0&name=after_comment.png",
    "/api/upload?filename=x.png",
    "",
  ].map((u) => { const r = api.refFromUri(u); return r && r.name; });
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exitCode = 1; });
