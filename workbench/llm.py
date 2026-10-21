"""LLM 提示词增强 —— 一次 OpenAI 兼容的 `/chat/completions` 调用。

**为什么是「OpenAI 兼容」而不是写死某一家**：DeepSeek / 硅基流动 / 月之暗面 /
本地 Ollama 暴露的都是同一个形状，于是「换一家」只是改 base_url 与模型名；
写死一家的话，换个厂商要改代码 —— 而这类事本来就不该由代码决定。

**这条链路不碰 ComfyUI、不碰工作流**：输入一段文本、输出一段文本，
出图那一步完全不知道它发生过。

**一次调用做三件事**：增强 / 译成英文 / 译成中文（动作表见 `ACTIONS_INFO`）。
「译」那一支与「增强」的要求**正好相反** —— 增强要它发挥，翻译要它**别发挥**，
所以两者的规则与温度都分开（`SYSTEM_BASE` / `TRANSLATE_RULES` / `ACTION_TEMPERATURE`）。
把「翻译」写成增强的一条附加说明是不行的：那样中文进、英文出，但补了一堆用户
没写过的元素，而「只看懂原文写了什么」这个用法就没了。

凭据落在 `state/llm.json`（`.gitignore` 把整个 `state/` 都忽略了，实测
`git check-ignore` 命中）。**密钥永不回显**：`public()` 只给 `has_key` 与掩码。

四个坑，前三个本机踩过：

  - **`trust_env=False`**：本机 `NO_PROXY` 里那个裸的 `[::1]` 会被 httpx 当成
    URL 去解析，于是 `httpx.Client()` **在构造时就炸** —— 那不是请求失败，是
    构造失败，报错还长得不像网络问题。回环与出网调用一律这么写。
  - **连接预算要单独放宽**：本机连一个没人监听的端口，拒绝要 2.0~2.4 秒才回来
    （见 `config.PROBE_CONNECT_TIMEOUT`）。用 2 秒去连会把「连不上」误报成「超时」。
  - **反向词要单独一句规则**：正向那套「八步结构」用在反向词上会得到一段散文，
    而反向词要的是逗号分隔的元素表 —— 同一个模型、同一个接口，差的就是这句。
  - **失败必须说得出是哪一种**：401（密钥不对）与 404（模型名不对）处理方式
    完全不同，都报「增强失败」等于让用户自己去猜。
"""
import json
import os

import httpx

from . import config
from .util import atomic_write_json, now_ms, read_json

MAX_TEXT = 20000
DEFAULT_TIMEOUT_S = 60.0
# 单独给连接一档：与 `config.PROBE_CONNECT_TIMEOUT` 同一个理由（本机拒绝要
# 2.0~2.4 秒才回来）。读取那档才是「模型干活」的时间，由用户配。
CONNECT_TIMEOUT_S = 6.0
ERROR_BODY_CHARS = 200

TARGET_PROMPT = "prompt"
TARGET_NEGATIVE = "negative"

# **目标表的唯一真源。** 页面从 `/api/llm` 拿它去建「要增强哪一段」那个下拉框，
# 所以「加一个可增强的东西」= 在这里加一行（外加它的 `SYSTEM_BASE`），
# 不必去页面里加第二颗按钮 —— 那正是原来那种形状扩展不动的地方。
#   id    —— 走接口的那个值
#   label —— 页面上显示的名字
#   field —— 采用时写回**表单里哪一个元素**（页面上那两个输入框的 id）
TARGETS_INFO = (
    {"id": TARGET_PROMPT, "label": "提示词", "field": "prompt"},
    {"id": TARGET_NEGATIVE, "label": "反向词", "field": "negative"},
)
TARGETS = tuple(t["id"] for t in TARGETS_INFO)

ACTION_ENHANCE = "enhance"
ACTION_TO_EN = "to_en"
ACTION_TO_ZH = "to_zh"

# **动作表的唯一真源**（与 `TARGETS_INFO` 同一套规矩）：页面从 `/api/llm` 拿它去建
# 顶栏那个「做什么」下拉框，所以「加一个动作」= 这里加一行 + 在规则与温度里各配
# 一条，**页面一个字都不用改**（有一条判据钉住 app.js 里不认识「译成英文」）。
#   id    —— 走接口的那个值
#   label —— 页面上显示的名字；「正在<label>…」「<label>失败」这些句子也由它拼
#   noun  —— 这一支的产物叫什么：右栏标题、「采用<noun>」、空稿占位都从它来。
#            少了它页面就得按动作 id 分支，那正是把表抄成第二份。
ACTIONS_INFO = (
    {"id": ACTION_ENHANCE, "label": "增强", "noun": "增强稿"},
    {"id": ACTION_TO_EN, "label": "译成英文", "noun": "译稿"},
    {"id": ACTION_TO_ZH, "label": "译成中文", "noun": "译稿"},
)
ACTIONS = tuple(a["id"] for a in ACTIONS_INFO)

# 内置规则：**分目标**。用户在设置里写的那句是**追加**在这后面，而不是替换
# —— 替换会让「逗号分隔的元素表」这条形状约束一起没了，而那正是反向词利用率
# 高低的关键。
SYSTEM_BASE = {
    TARGET_PROMPT: (
        "你是图像生成用的提示词润色助手。把用户给的提示词改写成更具体、"
        "更可执行的提示词：主体、材质、光线、构图、镜头、画风各说清楚；"
        "保留原意与已经出现的具体元素，不要引入用户没提过的主体。"
        "直接输出提示词本身，不要解释、不要引号、不要 Markdown。"
    ),
    TARGET_NEGATIVE: (
        "你是图像生成用反向词（negative prompt）的润色助手。把用户给的内容"
        "整理成逗号分隔的英文元素表：只保留「不希望出现」的具体元素，去重、"
        "按重要性排序；不要写成句子或段落。"
        "直接输出那一行元素表，不要解释、不要引号、不要 Markdown。"
    ),
}

# 翻译那两条**不看目标**：一条规则靠「保持原文的形式」把那点差别吃掉（原本是
# 逗号分隔的元素表就还是元素表）。所以再加一个目标不必再配一份翻译规则 ——
# 「增强」那一支仍按目标分开（见 `SYSTEM_BASE`）。
#
# 措辞是「不增不减 / 不改写 / 不润色」而**不是**「译得更好」：用户点「译成英文」
# 的两种场景（自己写中文、想拿英文提示词；拿到一串英文、想看懂写了什么）都要的是
# **忠实**。想润色就先「增强」再「翻译」—— 面板里那个「← 移到左边」就是给这个
# 接力用的。
TRANSLATE_RULES = {
    ACTION_TO_EN: (
        "你是翻译。把用户给的文本**忠实地**译成英文：不增不减、不改写、不润色，"
        "不补任何原文里没有的元素；保持原文的形式（原本是逗号分隔的元素表就还是"
        "元素表，原本是句子就还是句子）、语气与专有名词。"
        "直接输出译文本身，不要解释、不要引号、不要 Markdown。"
    ),
    ACTION_TO_ZH: (
        "你是翻译。把用户给的文本**忠实地**译成中文：不增不减、不改写、不润色，"
        "不补任何原文里没有的元素；保持原文的形式（原本是逗号分隔的元素表就还是"
        "元素表，原本是句子就还是句子）、语气与专有名词。"
        "直接输出译文本身，不要解释、不要引号、不要 Markdown。"
    ),
}

# 温度**分动作**：翻译是有唯一正确答案的活，温度高就是在让它自由发挥 —— 而
# 「忠实」正是那一支的全部要求；增强要的才是发挥，照旧 0.7（原值，没动）。
ACTION_TEMPERATURE = {ACTION_ENHANCE: 0.7, ACTION_TO_EN: 0.2, ACTION_TO_ZH: 0.2}

# 一次最多带几张参考图。**与页面那个上限同源**：图生图的 `TextEncodeQwenImage21`
# autogrow 就只到 image_1..image_16，能选进来的最多 16 张。
MAX_IMAGES = 16

# 有图时**追加**在规则后面的一段（没图时一个字都不加：那条「老调用方发出去的
# 东西逐字节不变」的判据钉着的正是这件事）。
#
# 为什么非要有这一段：不给它说清这几张图是什么，模型会把参考图当成「用户上传的
# 附件」随口一提，或者干脆只顾着描述图、忘了用户写的那段字。而「不要写序号」
# 这一条来自本链路自己的事实 —— 提示词里没有「第几张」这回事（指令要按内容
# 描述，见 AGENTS.md 的「多参考图」那条），所以别让它把序号写进提示词。
IMAGE_RULE = (
    "随附 %d 张参考图，它们就是生成时会用到的那几张（按顺序给你，第一张决定输出"
    "尺寸）。**按内容描述它们**（主体、颜色、构图、材质、风格），要指某一处时说"
    "「画面左侧的那个…」，**不要写「第一张」「图 2」这类序号** —— 提示词里没有"
    "序号这回事。用户写的那段字仍然是你要处理的对象，图是给你看的事实依据。"
)


_FIELDS = ("base_url", "model", "api_key", "system", "timeout_s")


def _label_of(table, key, fallback):
    for row in table:
        if row["id"] == key:
            return row["label"]
    return fallback


def target_label(target):
    return _label_of(TARGETS_INFO, target or TARGET_PROMPT, TARGET_PROMPT)


def action_label(action):
    return _label_of(ACTIONS_INFO, action or ACTION_ENHANCE, ACTION_ENHANCE)


def rule_for(target, action):
    """这一对 (目标, 动作) 用哪条内置规则。

    抽出来是为了让「翻译不看目标」这条决定**只有一处**，而且能被判据直接读到。
    """
    if action == ACTION_ENHANCE:
        return SYSTEM_BASE[target]
    return TRANSLATE_RULES[action]


class LLMError(Exception):
    """带 `kind` 的失败。`kind` 是给判据用的，文案是给人看的。"""

    def __init__(self, message, kind="unknown", detail=None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def mask_key(key):
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return "•" * len(key)
    return "%s…%s" % (key[:3], key[-4:])


def scrub(text, key):
    """把密钥从**任何要出门的文本**里抹掉。

    为什么要这一步：服务端的报错正文经常把收到的密钥回显一段
    （`{"error": {"message": "Invalid API key: sk-abc123..."}}`），而这条消息
    会进页面、还会进日志台的时间线 —— 那等于把一个密钥抄到了两个更松的地方，
    而且**没有症状**（页面上看起来只是一条很正常的 401）。
    整串与末尾 8 位都换掉：有的服务端只回显尾部。
    """
    out = "" if text is None else str(text)
    key = (key or "").strip()
    if not key:
        return out
    masked = mask_key(key)
    out = out.replace(key, masked)
    if len(key) > 8:
        out = out.replace(key[-8:], masked)
    return out


def endpoint_url(base_url):
    """把 base_url 拼成 chat 端点。

    **已经以 `/chat/completions` 结尾就照用** —— 很多人是从文档里整条 URL
    复制的，再拼一次会得到一个 404，而那个 404 看起来像「模型名不对」。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def build_messages(text, target=None, action=None, system="", images=None):
    """组 `messages`。抽出来是为了**能单测**（发出去的是什么，不必真连网）。

    `action=None` 就是「增强」，`images=None`（或缺省）就是纯文本 ——
    **两条老路径发出去的东西都逐字节不变**（有判据钉着）。

    `images` 是 `vision.prepare()` 的产物（`{name, mime, b64, …}`）：有图时
    用户那条消息变成 OpenAI 的 content 数组 —— 一段 text 加若干 image_url 的
    data URI。**这里不认识 PIL、也不认识磁盘**（读盘与缩图是 `server.py`
    那条路的事），所以这个函数照旧是纯的、可以离线断言。
    """
    target = target or TARGET_PROMPT
    action = action or ACTION_ENHANCE
    if target not in TARGETS:
        raise ValueError("target 只能是 %s" % " 或 ".join(TARGETS))
    if action not in ACTIONS:
        raise ValueError("action 只能是 %s" % " 或 ".join(ACTIONS))
    rule = rule_for(target, action)
    extra = (system or "").strip()
    if extra:
        rule = "%s\n\n补充要求：%s" % (rule, extra)
    body = (text or "").strip()
    shots = list(images or [])
    if not shots:
        return [{"role": "system", "content": rule},
                {"role": "user", "content": body}]
    rule = "%s\n\n%s" % (rule, IMAGE_RULE % len(shots))
    content = [{"type": "text", "text": body}]
    for im in shots:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:%s;base64,%s"
                                             % (im["mime"], im["b64"])}})
    return [{"role": "system", "content": rule},
            {"role": "user", "content": content}]


def parse_completion(payload):
    """从返回体里取那段文本；取不到就说清是「形状不对」还是「内容是空的」。"""
    if not isinstance(payload, dict):
        raise LLMError("返回体不是 JSON 对象", kind="shape",
                       detail=str(payload)[:ERROR_BODY_CHARS])
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            raise LLMError("服务端报错：%s" % err["message"], kind="api")
        raise LLMError("返回体里没有 choices", kind="shape",
                       detail=json.dumps(payload, ensure_ascii=False)[:ERROR_BODY_CHARS])
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if content is None:
        # 有些实现把正文放在 text 里（补全形状），一并认下
        content = first.get("text")
    text = (content or "").strip()
    if not text:
        raise LLMError("模型返回了空内容", kind="empty",
                       detail=json.dumps(first, ensure_ascii=False)[:ERROR_BODY_CHARS])
    return text, payload.get("model"), payload.get("usage")


def classify_http_error(status, body_text, base_url):
    """HTTP 状态码 → 一句**能照着修**的话。"""
    snippet = (body_text or "").strip().replace("\n", " ")[:ERROR_BODY_CHARS]
    if status in (401, 403):
        return LLMError("密钥不对或无权限（HTTP %d）：%s"
                        % (status, snippet or "服务端没给原因"),
                        kind="auth", detail=snippet)
    if status == 404:
        return LLMError("端点上没有这个模型（HTTP 404）：%s。"
                        "检查模型名，以及 base_url 有没有少 `/v1`。"
                        % (snippet or "服务端没给原因"),
                        kind="model", detail=snippet)
    if status == 429:
        return LLMError("被限流了（HTTP 429）：%s" % (snippet or "稍后再试"),
                        kind="rate", detail=snippet)
    return LLMError("服务端返回 HTTP %d：%s" % (status, snippet or "没有正文"),
                    kind="api", detail=snippet)


class LLMSettings:
    """`state/llm.json` 的读写。**只有这里写盘** —— 增强那条路一个文件都不碰。"""

    def __init__(self, path=None):
        self.path = path or os.path.join(config.STATE_DIR, "llm.json")
        self.load()

    def load(self):
        data = read_json(self.path, default={})
        if not isinstance(data, dict):
            data = {}
        self._data = {k: data.get(k) for k in _FIELDS}
        self._data["base_url"] = str(self._data.get("base_url") or "").strip()
        self._data["model"] = str(self._data.get("model") or "").strip()
        self._data["api_key"] = str(self._data.get("api_key") or "").strip()
        self._data["system"] = str(self._data.get("system") or "").strip()
        try:
            self._data["timeout_s"] = float(self._data.get("timeout_s")
                                            or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            self._data["timeout_s"] = DEFAULT_TIMEOUT_S
        return dict(self._data)

    def raw(self):
        return dict(self._data)

    def public(self):
        """给页面看的。**没有 api_key** —— 连掩码都是另算的。"""
        data = dict(self._data)
        key = data.pop("api_key", "")
        data["has_key"] = bool(key)
        data["key_masked"] = mask_key(key)
        data["endpoint"] = endpoint_url(data.get("base_url"))
        data["configured"] = bool(data.get("base_url") and data.get("model") and key)
        data["missing"] = [name for name, ok in (
            ("base_url", bool(data.get("base_url"))),
            ("model", bool(data.get("model"))),
            ("api_key", bool(key)),
        ) if not ok]
        return data

    def save(self, patch):
        """`api_key` **留空 = 保持原样**，不是清空。

        为什么：掩码回显之后，用户改模型名再保存，那条空着的密钥字段会被
        当成「清掉」—— 于是他下一次增强才失败，而当时他什么都没删。
        真要清就传 `{"api_key": null}`（显式 null 与空串是两件事）。
        """
        if not isinstance(patch, dict):
            raise ValueError("设置必须是对象")
        data = dict(self._data)
        for name in ("base_url", "model", "system"):
            if name in patch and patch[name] is not None:
                data[name] = str(patch[name]).strip()
        if "api_key" in patch:
            value = patch["api_key"]
            if value is None:
                data["api_key"] = ""            # 显式清空
            elif str(value).strip():
                data["api_key"] = str(value).strip()
            # 空串 = 保持原样（见 docstring）
        if "timeout_s" in patch and patch["timeout_s"] is not None:
            try:
                data["timeout_s"] = max(5.0, min(float(patch["timeout_s"]), 600.0))
            except (TypeError, ValueError):
                raise ValueError("超时必须是数字（秒）")
        self._data = data
        atomic_write_json(self.path, {k: data.get(k) for k in _FIELDS})
        return self.public()


async def enhance(text, target=None, action=None, settings=None, client=None,
                  images=None):
    """把一段文本交给 LLM 处理一次。返回
    `{text, model, elapsed_ms, target, action, images, usage}`。

    「一次调用做三件事」：`action` 是增强还是翻译（见 `ACTIONS_INFO`）。函数名与
    路由名里的 enhance 现在读作「交给 LLM 处理一次」—— **不是**「只做增强」。

    `images` 是 `vision.prepare()` 的产出，可空（空 = 老行为：纯文本）。
    **不写任何文件**（只有 `LLMSettings.save()` 写盘）。失败一律抛 `LLMError`。
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("这一段是空的，没有可处理的内容", kind="input")
    if len(text) > MAX_TEXT:
        raise LLMError("内容太长（上限 %d 字）" % MAX_TEXT, kind="input")
    shots = list(images or [])
    if len(shots) > MAX_IMAGES:
        raise LLMError("一次最多带 %d 张参考图（这次 %d 张）"
                       % (MAX_IMAGES, len(shots)), kind="input")
    for im in shots:
        if not isinstance(im, dict) or not im.get("mime") or not im.get("b64"):
            # 「有一张没准备好」在这里挡掉：不挡的话它会以 KeyError 的形式在拼
            # data URI 时炸出来，而那个堆栈长得不像「图片坏了」，像「接口返回的
            # 形状不对」—— 会把人带到错误的方向去。
            raise LLMError("有一张参考图没准备好（缺 mime 或 base64）", kind="input")
    target = target or TARGET_PROMPT
    action = action or ACTION_ENHANCE
    if target not in TARGETS or action not in ACTIONS:
        # 路由那层已经挡过一次；这里是给直接调用的人的第二道（别让它带着一个
        # 不认识的动作走完一次网络往返再报一个看不懂的错）。
        raise LLMError("不认识的目标或动作：%s / %s" % (target, action), kind="input")
    data = settings.raw() if settings else {}
    url = endpoint_url(data.get("base_url"))
    if not url or not data.get("model"):
        raise LLMError("还没配好：缺 %s" % ("、".join(
            n for n in ("base_url", "model", "api_key")
            if not data.get(n))), kind="config")
    if not data.get("api_key"):
        raise LLMError("还没配密钥", kind="config")
    timeout_s = float(data.get("timeout_s") or DEFAULT_TIMEOUT_S)
    body = {
        "model": data["model"],
        "messages": build_messages(text, target, action, data.get("system"), shots),
        "temperature": ACTION_TEMPERATURE[action],
        "stream": False,
    }
    headers = {"Authorization": "Bearer %s" % data["api_key"],
               "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=timeout_s,
                            write=10.0, pool=CONNECT_TIMEOUT_S)
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
    started = now_ms()
    try:
        try:
            resp = await client.post(url, json=body, headers=headers,
                                     timeout=timeout)
        except httpx.TimeoutException as exc:
            raise LLMError("LLM 在 %.0f 秒内没回话（%s）" % (timeout_s, url),
                           kind="timeout", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMError("连不上 %s：%s" % (url, exc), kind="network",
                           detail=str(exc)) from exc
        if resp.status_code >= 400:
            raise classify_http_error(resp.status_code, resp.text, url)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise LLMError("返回的不是 JSON（HTTP %d）：%s"
                           % (resp.status_code,
                              resp.text.strip()[:ERROR_BODY_CHARS]),
                           kind="shape") from exc
        out, model_name, usage = parse_completion(payload)
        return {"text": out, "model": model_name or data["model"],
                "elapsed_ms": now_ms() - started,
                "target": target, "action": action,
                # 实际带了几张图 —— 页面拿它写「· 2 张图」，日志台拿它留痕。
                # 「带了几张」必须有一个**回执**：只靠页面自己数，读盘失败时
                # 页面会以为带上了（而服务端那一步是失败的），那就又是一种
                # 没有症状的错。
                "images": len(shots),
                "usage": usage if isinstance(usage, dict) else None}
    except LLMError as exc:
        # **出门前抹一遍密钥**：失败文案会进页面、也会进日志台，而服务端的
        # 报错正文常常把密钥回显一段。见 `scrub` 的 docstring。
        raise LLMError(scrub(str(exc), data.get("api_key")), kind=exc.kind,
                       detail=scrub(exc.detail, data.get("api_key"))) from None
    finally:
        if own:
            await client.aclose()
