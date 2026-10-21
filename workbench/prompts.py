"""提示词库 —— 一个 JSON 文件，够用了。

为什么不上数据库：单用户、本机、量级是几十条。spec §11 明确不做多用户与鉴权，
加一个数据库只会多一个会坏的东西。原子写（临时文件 + `os.replace`）已经保证
读的人不会读到半截。

提示词库的价值在于**把「一次调好的提示词」变成可复用的资产** ——
本工作区里提示词的长度常常是几百字，丢了就得重写。
"""
import os
import threading
import uuid

from . import config
from .util import atomic_write_json, now_ms, read_json

MAX_TEXT = 20000
MAX_TITLE = 120
MAX_TAGS = 12
MAX_ROWS = 2000


def _clean_tags(tags):
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [t for t in tags.replace("，", ",").split(",")]
    if not isinstance(tags, (list, tuple)):
        raise ValueError("tags 必须是数组或逗号分隔的字符串")
    seen, out = set(), []
    for tag in tags:
        tag = str(tag).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:40])
    return out[:MAX_TAGS]


def _default_title(text):
    first = (text or "").strip().splitlines()
    head = first[0].strip() if first else ""
    return (head[:MAX_TITLE] or "未命名")


class PromptLibrary:
    def __init__(self, path=None, max_rows=None):
        self.path = path or config.PROMPTS_FILE
        # 做成实例属性而不是只读模块常量：它是**策略**，不同部署可以不同，
        # 测试也要能把它调小来验证裁剪逻辑。
        self.max_rows = max_rows or MAX_ROWS
        self._lock = threading.RLock()
        self._rows = []
        self.load()

    # --- 磁盘 -----------------------------------------------------------
    def load(self):
        data = read_json(self.path, default=[])
        if isinstance(data, dict):            # 容忍 {prompts: [...]} 形状
            data = data.get("prompts")
        rows = []
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("id") and row.get("text"):
                    rows.append(row)
        with self._lock:
            self._rows = rows
            return len(rows)

    def _save(self):
        atomic_write_json(self.path, self._rows)

    # --- 查询 -----------------------------------------------------------
    def list(self, query=None, tag=None):
        with self._lock:
            rows = [dict(r) for r in self._rows]
        if tag:
            rows = [r for r in rows if tag in (r.get("tags") or [])]
        if query:
            needle = query.strip().lower()
            if needle:
                rows = [r for r in rows
                        if needle in (r.get("title") or "").lower()
                        or needle in (r.get("text") or "").lower()
                        or any(needle in t.lower() for t in (r.get("tags") or []))]
        # 常用的排前面，其次按最近更新
        rows.sort(key=lambda r: (-(r.get("uses") or 0), -(r.get("updated_at") or 0)))
        return rows

    def get(self, pid):
        with self._lock:
            for row in self._rows:
                if row.get("id") == pid:
                    return dict(row)
        return None

    def tags(self):
        seen = {}
        for row in self.list():
            for tag in row.get("tags") or []:
                seen[tag] = seen.get(tag, 0) + 1
        return sorted(({"tag": t, "count": c} for t, c in seen.items()),
                      key=lambda x: (-x["count"], x["tag"]))

    def count(self):
        with self._lock:
            return len(self._rows)

    # --- 增删改 ---------------------------------------------------------
    def create(self, text, title=None, tags=None):
        text = (text or "").strip()
        if not text:
            raise ValueError("提示词不能为空")
        if len(text) > MAX_TEXT:
            raise ValueError("提示词太长（上限 %d 字）" % MAX_TEXT)
        ts = now_ms()
        row = {
            "id": uuid.uuid4().hex[:12],
            "title": (title or "").strip()[:MAX_TITLE] or _default_title(text),
            "text": text,
            "tags": _clean_tags(tags),
            "uses": 0,
            "created_at": ts,
            "updated_at": ts,
        }
        with self._lock:
            self._rows.append(row)
            self._prune()
            self._save()
        return dict(row)

    def update(self, pid, text=None, title=None, tags=None):
        with self._lock:
            for row in self._rows:
                if row.get("id") != pid:
                    continue
                if text is not None:
                    text = str(text).strip()
                    if not text:
                        raise ValueError("提示词不能为空")
                    if len(text) > MAX_TEXT:
                        raise ValueError("提示词太长（上限 %d 字）" % MAX_TEXT)
                    row["text"] = text
                if title is not None:
                    row["title"] = str(title).strip()[:MAX_TITLE] \
                        or _default_title(row.get("text"))
                if tags is not None:
                    row["tags"] = _clean_tags(tags)
                row["updated_at"] = now_ms()
                self._save()
                return dict(row)
        return None

    def delete(self, pid):
        with self._lock:
            before = len(self._rows)
            self._rows = [r for r in self._rows if r.get("id") != pid]
            if len(self._rows) == before:
                return False
            self._save()
            return True

    def touch(self, pid):
        """记一次使用 —— 让常用的浮到前面。"""
        with self._lock:
            for row in self._rows:
                if row.get("id") == pid:
                    row["uses"] = (row.get("uses") or 0) + 1
                    row["updated_at"] = now_ms()
                    self._save()
                    return dict(row)
        return None

    def _prune(self):
        """只裁「用得最少且最旧」的，绝不动常用的那些。"""
        if len(self._rows) <= self.max_rows:
            return
        self._rows.sort(key=lambda r: (-(r.get("uses") or 0),
                                       -(r.get("updated_at") or 0)))
        del self._rows[self.max_rows:]

    # --- 导入导出（便于在机器之间搬）-------------------------------------
    def export(self):
        return {"version": 1, "exported_at": now_ms(), "prompts": self.list()}

    def import_rows(self, payload, overwrite=False):
        """按 `id` 去重合并。返回 (新增, 跳过)。"""
        rows = payload.get("prompts") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("导入内容必须是提示词数组")
        with self._lock:
            have = {r.get("id") for r in self._rows}
            added = skipped = 0
            for row in rows:
                if not isinstance(row, dict) or not (row.get("text") or "").strip():
                    skipped += 1
                    continue
                if row.get("id") in have and not overwrite:
                    skipped += 1
                    continue
                if row.get("id") in have:
                    self._rows = [r for r in self._rows if r.get("id") != row["id"]]
                ts = now_ms()
                self._rows.append({
                    "id": row.get("id") or uuid.uuid4().hex[:12],
                    "title": (row.get("title") or "").strip()[:MAX_TITLE]
                             or _default_title(row.get("text")),
                    "text": str(row["text"])[:MAX_TEXT],
                    "tags": _clean_tags(row.get("tags")),
                    "uses": int(row.get("uses") or 0),
                    "created_at": int(row.get("created_at") or ts),
                    "updated_at": ts,
                })
                added += 1
            self._prune()
            self._save()
        return added, skipped
