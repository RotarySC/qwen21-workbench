"""五条链路的**静态输入**在哪 —— 基准工作流图的落点规则只此一处。

为什么单独一个文件：三张基准图分别被 `qwen21_build` / `h3_build` / `layers_build`
在**导入时**读走，而这三个模块是刻意平级的（互不 import）。落点规则要是各写一份，
「仓库自带的那份」与「工作区那份」的优先级迟早会分家 —— 而分家的表现是
**构建出另一张图**，不报错、不崩溃。

顺序（先命中先用）：

1. **环境变量**（`QWEN21_BASE` / `QWEN21_H3_BASE` / `QWEN21_LAYERS_BASE`）——
   测试与迁移用；给了就以它为准，不做存在性检查（给了错路径要当场炸，不是静默回落）
2. **仓库自带的** `workflows/<名字>` —— 让 `git clone` 下来就能用：这几张图是
   **图的定义**（链路长什么样），不是运行时产物，所以它属于这个仓库
3. **工作区**（`QWEN21_WORKSPACE`，默认 = 仓库的上一层）—— 旧的现场布局：
   图放工作区根、仓库只放代码

第 3 条保留是有意的：那种布局不是错，只是**克隆下来不会有** —— 有些部署里
基准图与产物、提示词库放在一起。仓库自带那份存在时优先，因为它才是随仓库走的定义。
"""
import os

#: 仓库自带的基准图目录名（相对仓库根）。
WORKFLOWS_DIRNAME = "workflows"


def base_file(env_var, name, *, repo, workspace):
    """基准图的完整路径。三个来源按 docstring 里的顺序取第一个命中的。"""
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    in_repo = os.path.join(repo, WORKFLOWS_DIRNAME, name)
    if os.path.exists(in_repo):
        return in_repo
    return os.path.join(workspace, name)


def workflows_dir(repo):
    """仓库自带的基准图目录（可能不存在 —— 开发机的布局里图在工作区根）。"""
    return os.path.join(repo, WORKFLOWS_DIRNAME)
