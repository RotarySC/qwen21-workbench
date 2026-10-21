/**
 * 层集 → PSD（浏览器里生成，不占后端）。
 *
 * ## 与插件自带那份（`custom_nodes/ComfyUI-See-through/web/seethrough_psd.js`）的区别
 *
 * 那份去读 `seethrough_psd_info.log` —— 一个**最后写入者胜**的指针，只服务插件自己
 * 那颗「Download PSD」按钮。拿它当历史回看，看到的永远是**最近一次**的层，而不是
 * 你点开那个作业的层；两个作业长得一模一样，看不出来（这正是 `workbench/artifacts.py`
 * 存在的理由）。这里吃的是 `/api/job` 现算的 `artifact`，**作业自己的那一份**。
 *
 * 取图口也不同：插件走 ComfyUI 的 `/view`，我们走工作台自己的
 * `/api/gallery/image?root=<下标>&name=<文件名>` —— 层文件不在画廊的列表里
 * （那是刻意的，见 `gallery.py`），但取图那条路照样认它们。
 *
 * `ag-psd.bundle.js` 是**逐字节照搬**插件那一份（297,416 B，
 * sha256 bdf200282ea3e7d582a1a6746df82561231c9b85c75eb31b8f2965aad83d7794）——
 * 自己再打包一份出来，两边迟早对不上，而那种分家的症状是「某个层读不出来」。
 *
 * 依赖：只依赖 `window.AgPsd`（由上面那个 bundle 提供）与 DOM 画布。
 */
(function () {
  "use strict";

  var BUNDLE_NAME = "ag-psd.bundle.js";

  // 本文件自己的目录 —— `currentScript` 只在脚本**初次执行**时有效，所以现在取。
  var OWN_DIR = (function () {
    var src = document.currentScript && document.currentScript.src;
    if (!src) return null;
    return src.slice(0, src.lastIndexOf("/") + 1);
  })();

  var _bundlePromise = null;

  /** 惰性挂上 ag-psd。**只在真的要出 PSD 时才加载** —— 297 KB 不该压在首屏。 */
  function ensureBundle() {
    if (window.AgPsd) return Promise.resolve(window.AgPsd);
    if (_bundlePromise) return _bundlePromise;
    _bundlePromise = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = (OWN_DIR || "/static/") + BUNDLE_NAME;
      s.onload = function () {
        if (!window.AgPsd) {
          reject(new Error("ag-psd 加载了但没有挂上 window.AgPsd"));
          return;
        }
        resolve(window.AgPsd);
      };
      s.onerror = function () {
        _bundlePromise = null;    // 失败了要能重试，否则一次网络抖动就永久坏掉
        reject(new Error("ag-psd 没加载上（" + s.src + "）"));
      };
      document.head.appendChild(s);
    });
    return _bundlePromise;
  }

  function loadImage(url) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      // 刻意**不设** crossOrigin：页面与图都在工作台这一个源上，同源不走 CORS。
      // 设了反而多一个「服务端没发 CORS 头就加载失败」的失败模式。
      img.onload = function () { resolve(img); };
      img.onerror = function () {
        reject(new Error("这张层读不出来：" + url));
      };
      img.src = url;
    });
  }

  function layerUrl(root, filename) {
    return "/api/gallery/image?root=" + encodeURIComponent(String(root)) +
           "&name=" + encodeURIComponent(filename);
  }

  /**
   * 这份层集能不能出 PSD。返回 `null` 表示能，否则返回一句给人看的话。
   *
   * **两种「不行」都不许装作没事**：清单读不出来（`error`），以及有的层不在磁盘上
   * （`missing`）。后者尤其要命 —— 出出来的 PSD 会**少几层而看不出来**。
   */
  function refusal(layerSet, kind) {
    if (!layerSet) return "这个作业没有层集产物";
    if (layerSet.kind !== "layers") return "这个作业的产物不是层集";
    if (layerSet.error) return layerSet.error;
    var layers = layerSet.layers || [];
    if (!layers.length) return "这份层清单里一层都没有";
    if (!layerSet.width || !layerSet.height) return "层清单里没有画布尺寸";
    if ((layerSet.missing || []).length) {
      var miss = layerSet.missing;
      return "有 " + miss.length + " 张层不在磁盘上（" + miss.slice(0, 3).join("、") +
             (miss.length > 3 ? " 等" : "") + "）—— 出出来的 PSD 会少层，先还原或重跑";
    }
    if (kind === "depth") {
      var noDepth = layers.filter(function (l) { return !l.depth_filename; });
      if (noDepth.length) {
        return "有 " + noDepth.length + " 层没有深度图 —— 这次拆层没出深度" +
               "（`depth_follows` 关着），深度 PSD 出不了";
      }
    }
    return null;
  }

  /**
   * 组装 PSD。返回 `{blob, filename, layers}`；不能出就抛（消息已能直接上页面）。
   *
   * `kind` 是 `"rgba"`（默认）或 `"depth"`。
   */
  async function build(layerSet, kind) {
    kind = kind || "rgba";
    var why = refusal(layerSet, kind);
    if (why) throw new Error(why);

    var AgPsd = await ensureBundle();
    var isDepth = kind === "depth";
    var width = layerSet.width;
    var height = layerSet.height;
    var root = layerSet.root;

    var composite = document.createElement("canvas");
    composite.width = width;
    composite.height = height;
    var cctx = composite.getContext("2d");

    var children = [];
    for (var i = 0; i < layerSet.layers.length; i++) {
      var layer = layerSet.layers[i];
      var filename = isDepth ? layer.depth_filename : layer.filename;
      if (!filename) continue;

      var img = await loadImage(layerUrl(root, filename));
      var lw = layer.right - layer.left;
      var lh = layer.bottom - layer.top;
      if (lw <= 0 || lh <= 0) continue;

      // 层画布按它自己的天然尺寸，再按 (left, top) 贴进合成图 ——
      // 用整幅尺寸会让 PSD 里每层都一样大，改起来就白改了。
      var lc = document.createElement("canvas");
      lc.width = lw;
      lc.height = lh;
      lc.getContext("2d").drawImage(img, 0, 0, lw, lh);
      cctx.drawImage(img, layer.left, layer.top, lw, lh);

      children.push({
        name: layer.name || ("layer_" + i),
        canvas: lc,
        left: layer.left, top: layer.top,
        right: layer.right, bottom: layer.bottom,
        blendMode: "normal",
        opacity: 1
      });
    }
    if (!children.length) throw new Error("一层都没读进来 —— 取图口没给图");

    var buffer = AgPsd.writePsd({
      width: width, height: height, canvas: composite, children: children
    });
    return {
      blob: new Blob([buffer], { type: "application/octet-stream" }),
      filename: psdFilename(layerSet, kind),
      layers: children.length
    };
  }

  /**
   * 下载文件名：`<prefix>_<时间戳>[_depth].psd`。
   *
   * 前缀取清单 JSON 自己的 `prefix` 字段、时间戳从**清单文件名**里抠 —— 两处都
   * 不另算一个。另算就会与磁盘上那些层文件对不上号，而「名字对不上」是看不出来的。
   *
   * 真实形状（`layers_build` 的 docstring + 出图主机上那三份实测产物）：
   *
   *     <prefix>_<YYYYmmdd>_<HHMMSS>_<uuid8>_layers.json
   *
   * 其中 `<prefix>` 可能是 `seethrough`，也可能是 `seethrough_<job_id[:8]>` ——
   * 走工作台时会多插一段（`unique_save_prefix`），所以**不能**假定前缀是哪一个。
   *
   * ⚠️ 拿不到前缀/时间戳时退回清单名，**绝不许退化成 `.psd`**：那是隐藏文件，
   * 而且两次下载会静默覆盖同一个名字。
   */
  function psdFilename(layerSet, kind) {
    var suffix = kind === "depth" ? "_depth" : "";
    var info = String((layerSet && layerSet.info) || "");
    var prefix = (layerSet && layerSet.prefix) || "";
    var m = /_(\d{8}_\d{6})_[0-9a-f]{8}_layers\.json$/.exec(info);
    if (prefix && m) return prefix + "_" + m[1] + suffix + ".psd";
    var stem = info.replace(/\.json$/, "") || "layers";
    return stem + suffix + ".psd";
  }

  /** 出 PSD 并触发下载。返回 `{filename, layers}`，好在页面上报一句。 */
  async function download(layerSet, kind) {
    var built = await build(layerSet, kind);
    var url = URL.createObjectURL(built.blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = built.filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // 回收要等浏览器真的把下载接过去；立刻 revoke 在部分浏览器上会截断文件。
    setTimeout(function () { URL.revokeObjectURL(url); }, 30000);
    return { filename: built.filename, layers: built.layers };
  }

  window.WbPsd = {
    ensureBundle: ensureBundle,
    build: build,
    download: download,
    refusal: refusal,
    psdFilename: psdFilename,
    layerUrl: layerUrl
  };
})();
