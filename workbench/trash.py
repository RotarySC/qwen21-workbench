"""删图 —— 进回收站，不进黑洞。

**为什么不是 `os.remove`**：画廊里那些图是**产物**，不是缓存。一次误点的
「全选 → 删除」在硬删下就是 210 MB 的图没了 —— 而且没有任何东西能证明它们
曾经存在过（生成参数与提示词就在 PNG 的 tEXt 块里，跟着文件一起走）。
回收站是这台机器上唯一一个「删了还能回来」的地方，所以这条路径**只有**
这一种删除语义，没有开关。

**为什么 `send2trash` 是惰性 import 的**：照 `vision.py` 对 Pillow 那套 ——
少一个包只许**这一条路**报错，不许把整个工作台（出图、画廊、日志台）一起
打死；而 `import` 期就炸的那种写法，会把「删除不可用」变成「工作台起不来」。
而且**绝不退回硬删**：包不在就一张都不删，如实说装什么。宁可删不掉，
也不能把「删不掉」悄悄变成「永久删掉了」。

（Windows 上它就是 `SHFileOperationW` + `FOF_ALLOWUNDO`。自己拿 ctypes 写要
拼「双 NUL 结尾的路径表」加一堆标志位，那是一处没人会重新读的代码 ——
让一个被广泛用过的小包去承担这件事。）
"""
import os

# 「怎么装」的那半句。三个 kind 的文案都带上它 —— 用户在页面上看到的应当是
# 一句能照做的话，而不是一段 traceback。
INSTALL_HINT = "装它：pip install send2trash"


class TrashError(RuntimeError):
    """删除失败。`kind` 是给判据用的稳定分类，`str(e)` 是给人看的一句话。

    kind 只有三种，因为调用方对它们**要说的话不一样**：

      missing     —— 文件已经不在了（别人删了 / 同名覆盖过 / 路径变了）
      unavailable —— 压根没有 send2trash，**一张都没删**
      failed      —— 回收站不收（被占用、权限、跨卷……），原因在消息里
    """

    def __init__(self, message, kind="failed"):
        super().__init__(message)
        self.kind = kind


def _load():
    """惰性拿 send2trash 的入口。拿不到 → `kind="unavailable"`。

    单独一个函数是为了**让判据能把它换掉**：单测不可能真往自己的回收站里丢
    文件，而「包不在会怎样」这条分支在装好包的机器上永远走不到。
    """
    try:
        from send2trash import send2trash
    except Exception as exc:                 # ImportError 与其它导入期错误都要接
        raise TrashError("没有装 send2trash（%s），一张都没删。%s"
                         % (exc, INSTALL_HINT), "unavailable")
    return send2trash


def available():
    """这台机器现在能不能删。返回 `(能不能, 为什么不能)`。

    给页面用：删不掉这件事应当**在点之前**就知道，而不是点完收到一句报错。
    """
    try:
        _load()
    except TrashError as exc:
        return False, str(exc)
    return True, None


def trash(path):
    """把 `path` 移进回收站。**失败一律抛**，绝不返回假成功。

    「已经不在」也抛，不算成功：那个数字（「已移入回收站 N 张」）是要给用户
    看的账，把找不到的算进去，账面就比磁盘上真实发生的事多。
    """
    fn = _load()
    if not os.path.exists(path):
        raise TrashError("文件已经不在了", "missing")
    try:
        fn(path)
    except Exception as exc:
        raise TrashError("回收站不收：%s" % exc, "failed")
