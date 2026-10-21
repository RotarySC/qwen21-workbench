#!/usr/bin/env node
/* P21d 的判据：`static/psd.js` 里那几个**纯函数**。
 *
 * 为什么单写一份：`psd.js` 里能出错的其实只有三处 —— 文件名怎么拼、什么情况下
 * **拒绝**出 PSD、以及取图 URL 怎么转义。真正的画布与 `AgPsd` 那一段只能靠真
 * 浏览器（P21e 的 `page_shot.py`），但上面三条在没浏览器时就能钉死，而且
 * **每一条错了都没有症状**：
 *
 *   · 文件名拼错 → 下载下来的 PSD 叫别的名字（用户看不出对不对）
 *   · 该拒绝时不拒绝 → 出一份**少几层**的 PSD，而它长得像一份好的
 *   · URL 不转义 → 层名带空格/中文时那张图读不出来，插件那边是 `alert` 一句
 *
 * 用法：node psd_probe.js <psd.js 的绝对路径>
 */
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(process.argv[2], "utf8");

// ── 最小假 DOM ──────────────────────────────────────────────────────────────
// 只补 `psd.js` 真的要碰的那几个面。少了会当场炸 —— 那是**假失败**，
// 补这里，别改 `psd.js` 去迁就它（与 `form_probe.js` 同一条规矩）。
const window_ = {};
const sandbox = {
  window: window_,
  console,
  setTimeout,
  document: {
    // `psd.js` 在**初次执行**时读它，用来定位同目录下的 ag-psd 包
    currentScript: { src: "https://example.test/static/psd.js" },
    createElement() {
      return { src: "", onload: null, onerror: null };
    },
    head: { appendChild() {} },
    body: { appendChild() {}, removeChild() {} },
  },
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const P = window_.WbPsd;
let bad = 0;

function ok(name, cond, extra) {
  if (cond) return;
  bad++;
  console.log("BAD  " + name + (extra === undefined ? "" : "  <- " + extra));
}

ok("WbPsd exported", !!P);
if (!P) { console.log("FAILED 1"); process.exit(1); }

for (const k of ["build", "download", "refusal", "psdFilename", "layerUrl", "ensureBundle"]) {
  ok("exposes " + k, typeof P[k] === "function");
}

// ── psdFilename ────────────────────────────────────────────────────────────
// 前缀取清单 JSON 自己的 `prefix`、时间戳从清单文件名里抠 —— 两处都不另算。
// 真实形状见出图主机上那三份产物（`st1280_20261011_233208_571ab050_layers.json`）。

// 走工作台时前缀里**会多一段 job token**（`unique_save_prefix` 插的），
// 而它同时出现在每一个层文件名里 —— 所以 PSD 名跟着带上是**对的**，
// 不是噪音：两次运行的层文件就是这么区分的。
ok("filename follows the workbench prefix",
   P.psdFilename({
     info: "seethrough_a1b2c3d4_20261012_101530_ab12cd34_layers.json",
     prefix: "seethrough_a1b2c3d4",
   }, "rgba") === "seethrough_a1b2c3d4_20261012_101530.psd",
   P.psdFilename({
     info: "seethrough_a1b2c3d4_20261012_101530_ab12cd34_layers.json",
     prefix: "seethrough_a1b2c3d4",
   }, "rgba"));

// 直接走后端（不经工作台）的那三份：前缀就是裸的 `st1280`
ok("filename follows the bare prefix",
   P.psdFilename({ info: "st1280_20261011_233208_571ab050_layers.json", prefix: "st1280" }, "rgba")
     === "st1280_20261011_233208.psd",
   P.psdFilename({ info: "st1280_20261011_233208_571ab050_layers.json", prefix: "st1280" }, "rgba"));

ok("depth filename gets the suffix",
   P.psdFilename({ info: "st1280_20261011_233208_571ab050_layers.json", prefix: "st1280" }, "depth")
     === "st1280_20261011_233208_depth.psd",
   P.psdFilename({ info: "st1280_20261011_233208_571ab050_layers.json", prefix: "st1280" }, "depth"));

// 清单里没有 `prefix`（老产物 / 手工搬来的）时退回清单名，不许抛
const odd = P.psdFilename({ info: "weird-name_layers.json" }, "rgba");
ok("filename falls back without a prefix", odd === "weird-name_layers.psd", odd);
ok("filename falls back without throwing", typeof odd === "string");

// ★ 退化名：什么都没有时不能给出 `.psd` —— 那是隐藏文件，而且两次下载会
// 静默盖掉同一个名字，而用户只会看到「下载了」。
const degenerate = P.psdFilename({}, "rgba");
ok("filename never degenerates to a bare dot", degenerate !== ".psd", degenerate);
ok("filename stays a usable name", /^[^./].*\.psd$/.test(degenerate), degenerate);
ok("a prefix without a timestamp still falls back safely",
   /^[^./].*\.psd$/.test(P.psdFilename({ info: "x_layers.json", prefix: "p" }, "rgba")),
   P.psdFilename({ info: "x_layers.json", prefix: "p" }, "rgba"));

// ── refusal ────────────────────────────────────────────────────────────────
const good = {
  kind: "layers", root: 0, width: 8, height: 8, info: "a_20260101_000000_ff_layers.json",
  error: null, missing: [],
  layers: [{ name: "tail", filename: "a.png", left: 0, top: 0, right: 2, bottom: 2 }],
};
ok("a good set is not refused", P.refusal(good, "rgba") === null, P.refusal(good, "rgba"));
ok("null is refused", typeof P.refusal(null, "rgba") === "string");
ok("a video artifact is refused",
   typeof P.refusal({ kind: "video" }, "rgba") === "string");
ok("carries the reader error through",
   P.refusal(Object.assign({}, good, { error: "E-XYZ" }), "rgba") === "E-XYZ");
ok("an empty layer list is refused",
   typeof P.refusal(Object.assign({}, good, { layers: [] }), "rgba") === "string");
ok("a missing canvas size is refused",
   typeof P.refusal(Object.assign({}, good, { width: null }), "rgba") === "string");

// ★ 最要命的一条：层不在磁盘上时必须**拒绝**，不许出一份少几层的 PSD。
// 少层的 PSD 与完整的 PSD 长得一模一样，用户不会发现。
const short = Object.assign({}, good, { missing: ["a.png", "b.png"] });
const why = P.refusal(short, "rgba");
ok("missing layers are refused", typeof why === "string", why);
ok("the refusal names the count", /\b2\b/.test(why || ""), why);

// 深度 PSD 要每一层都有深度图；缺一层就不能出
const noDepth = Object.assign({}, good, {
  layers: [good.layers[0], { name: "x", filename: "b.png", left: 0, top: 0, right: 2, bottom: 2 }],
});
ok("depth refuses when a layer has no depth map",
   typeof P.refusal(noDepth, "depth") === "string");
ok("depth is fine when every layer has one",
   P.refusal(Object.assign({}, good, {
     layers: [Object.assign({}, good.layers[0], { depth_filename: "a_depth.png" })],
   }), "depth") === null);
ok("rgba does not require depth maps", P.refusal(noDepth, "rgba") === null);

// ── layerUrl ───────────────────────────────────────────────────────────────
// 层名里有空格（`front hair`）、有中文、有 `#` —— 不转义就取不到那张图。
const u = P.layerUrl(2, "a b#c/d.png");
ok("url carries the root", u.indexOf("root=2") >= 0, u);
ok("url escapes spaces", u.indexOf("a%20b") >= 0, u);
ok("url escapes the hash", u.indexOf("%23") >= 0, u);
ok("url never leaves a bare space or hash",
   !/[ #]/.test(u), u);
ok("url walks /api/gallery/image",
   u.indexOf("/api/gallery/image") === 0, u);

console.log(bad ? "FAILED " + bad : "OK psd.js pure functions");
process.exit(bad ? 1 : 0);
