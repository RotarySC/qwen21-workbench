# 测试快照（golden fixtures）

这些 JSON 是**重构之前**、用四个旧脚本跑出来的产物，用作 `qwen21_build.py`
的回归基准。

## 怎么来的

在 `qwen21-workbench/` 还不存在、`qwen21_build.py` 还没写的时候，
从工作区根目录（`ComfyUI/`）执行：

```powershell
$py = "%LOCALAPPDATA%\qwen21-venv\Scripts\python.exe"

# 旧 qwen21_run.py（当时还自带构建逻辑）
& $py qwen21_run.py "a small brown fox sitting in tall grass" --steps 30 --seed 111 `
      -o tests\fixtures\golden_t2i.json
& $py qwen21_run.py "a small brown fox sitting in tall grass" --steps 30 --seed 111 `
      --width 768 --height 512 -o tests\fixtures\golden_t2i_sized.json

# 旧 build_refine.py / build_2k.py（无参数，提示词烤在 qwen21_fashion_api.json 里）
& $py build_refine.py          # 写出 qwen21_refine_api.json
& $py build_2k.py              # 写出 qwen21_fashion_2k_api.json
Copy-Item qwen21_refine_api.json      tests\fixtures\golden_refine.json
Copy-Item qwen21_fashion_2k_api.json  tests\fixtures\golden_2k.json

# 旧 build_edit.py
& $py build_edit.py "make the apple bright blue" --src ref.png `
      --resolution 1024 --seed 7 --steps 25 -o tests\fixtures\golden_edit.json
```

跑之前先记下 `qwen21_refine_api.json` 与 `qwen21_fashion_2k_api.json` 的哈希，
两边一致 —— 说明旧脚本是确定性的，快照可用。

## 断言什么

`tests/test_build_golden.py` 与 `tests/test_legacy_cli.py` 都对这些快照断言两层：

1. **语义等价** —— 键排序后逐字相等（硬要求）
2. **逐字节相同** —— 更强的要求，当前四个脚本都满足

## 注意

`golden_refine.json` / `golden_2k.json` 的提示词来自 `qwen21_fashion_api.json`
（旧脚本没有提示词参数，是烤在文件里的）。所以这两个快照**间接依赖那份文件**：
如果它被改了，快照会失配 —— 那是预期行为，说明真源变了，需要重新生成快照。

`golden_t2i*.json` / `golden_edit.json` 的提示词是命令行传的，不依赖外部文件。
