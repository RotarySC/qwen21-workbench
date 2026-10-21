"""零依赖小工具：原子写、JSON 读写、时间。

原子写用「临时文件 + `os.replace`」，与 ComfyUI 侧 `node_probe.py` 同款 ——
`os.replace` 在 Windows 上也是原子的，所以读的人要么看到旧文件、要么看到新文件，
绝不会读到半截。任务状态每次进度更新都要落盘，读的人（另一个请求）随时可能来。
"""
import json
import os
import tempfile
import time


def now_ms():
    return int(time.time() * 1000)


def atomic_write_bytes(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, text, encoding="utf-8"):
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path, obj, **kwargs):
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("indent", 2)
    atomic_write_text(path, json.dumps(obj, **kwargs) + "\n")


def append_jsonl(path, obj):
    """追加一行。单个写者 + 小于 PIPE_BUF，追加本身是原子的，不用锁。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_jsonl(path):
    """读 .jsonl，坏行跳过 —— 一行写崩不该让整个历史读不出来。"""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out
