#!/usr/bin/env node
/* P21c 的临时判据：新加的 id 在 index.html 里**各出现恰好一次**。
 *
 * 为什么值得单独跑一次：`$("id")` 撞上重复 id 时 `getElementById` 只给你第一个，
 * 而页面上两个元素长得一样 —— 于是「改了没反应」的那一个看起来是产品坏了。
 * 少一个则当场 null，`applyModeUI` 一上来就炸。
 */
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(
  path.join(__dirname, "..", "workbench", "static", "index.html"), "utf8");

const IDS = [
  "chain-tabs", "chain-thumb", "chain-tag",
  "chain-qwen", "chain-h3", "chain-layers",
  "mode-wrap", "steps-wrap", "size-wrap", "sweep-steps-wrap",
  "h3-wrap", "h3-seconds", "h3-frames",
  "layers-wrap", "layers-resolution", "layers-depth",
];
let bad = 0;
for (const id of IDS) {
  const n = (html.match(new RegExp('id="' + id + '"', "g")) || []).length;
  if (n !== 1) { console.log("BAD  " + id + " x" + n); bad++; }
}
// 老 id 也得还在（改名改漏了会静默少一格控件）
for (const id of ["mode", "steps", "size", "upscale-wrap", "gen-row",
                  "prompt-wrap", "cfg-wrap", "advanced-wrap"]) {
  const n = (html.match(new RegExp('id="' + id + '"', "g")) || []).length;
  if (n !== 1) { console.log("BAD  " + id + " x" + n); bad++; }
}
console.log(bad ? "FAILED " + bad : "OK " + (IDS.length + 8) + " ids unique");
process.exit(bad ? 1 : 0);
