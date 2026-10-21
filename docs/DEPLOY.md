# 从零部署

这份指南的目标：在一台**空机器**上把 ComfyUI + 插件 + 模型装起来，让这个工作台能跑通
五条链路。每一步都给了**判据** —— 别只看「服务起来了」。

- 需要 **Windows**（出图与放大超分那两个加速插件是 Windows 原生 `.pyd`，没有 Linux 构建）。
- 需要 **NVIDIA GPU**。峰值显存实测：Qwen 1024² 约 18.9 GiB；H3 视频 **21.24 / 22.00 GiB
  （96.5%）** —— 22 GB 卡刚好，跑之前先腾显存。
- 需要 **ComfyUI ≥ 0.39**：H3 用的 `MiniMaxH3ImageToVideo` / `CreateVideo` / `SaveVideo` /
  `VAEDecodeAudio` 是 0.39 才有的。低于这个版本，工作台的体检会报「后端版本太旧」，
  而不是「缺插件」。
- 磁盘：模型合计约 **90 GiB**（见下表），另留缓存空间。

> 下面凡写 `<ComfyUI 安装目录>` 的地方，都指 ComfyUI checkout 所在的目录（它的下一层就是
> `models/`、`custom_nodes/`、`main.py`）。`<你的模型库>` 指可选的共享模型目录。

---

## 1. 装 ComfyUI

1. 装好 ComfyUI（≥ 0.39），确认它能起来：

   ```
   <ComfyUI 安装目录>\.venv\Scripts\python.exe -s main.py --port 8188
   ```

   判据：`http://127.0.0.1:8188/system_stats` 返回 200，且
   `http://127.0.0.1:8188/object_info` 里能搜到 `MiniMaxH3ImageToVideo`。

2. **模型放哪**：可以全部铺进 `<ComfyUI 安装目录>\models\`，也可以用
   `--extra-model-paths-config <yaml>` 指向一个共享库。**推荐前者** —— 少一个
   「yaml 指错地方」的失败模式，而那种失败的表现是**模型下拉框是空的**，不是报错。

   ⚠ 但有两个目录**只能**放安装目录（`extra_model_paths` 不认识它们的键）：
   `models/vosr2/` 与 `models/background_removal/`。详见 §3。

---

## 2. 装插件

| 插件目录名（`custom_nodes/` 下） | 来源 | 服务于 | 缺了会怎样 |
|---|---|---|---|
| `TE-Speed-QwenImage21` | `github.com/tl2012tl/TE-Speed-QwenImage21` | Qwen 出图（**默认开**） | 提交必然失败；而且它默认开着 ⇒ 不装就得显式关掉 |
| `TE-Speed-VOSR2` | `github.com/tl2012tl/TE-Speed-VOSR2` | 放大超分 | 提交必然失败 |
| `ComfyUI-See-through` | `github.com/jtydhr88/ComfyUI-See-through` | 拆层 | 提交必然失败 |
| `ComfyUI-KJNodes` | `kijai/ComfyUI-KJNodes` | H3（`ImageResizeKJv2`） | 提交必然失败 |

**三条注意：**

- **TE-Speed 那两个插件是闭源 Windows 二进制**（`.pyd`），仓库里**没有 LICENSE 文件**。
  用之前请自行确认许可。它们也是「Windows only」的原因。
- **See-through 用 tarball 装，别 `git clone`**：它的仓库 vendored 了整棵
  `see-through/common/` 树（不是 submodule），一个 tarball 就是完整安装包。
  若 `github.com` 不通，用 `codeload.github.com` 的 tarball（实测约 7.5 MB）。
- **KJNodes 的版本要够新**：`ImageResizeKJv2` 在 1.4.8 里**没有**。

装完**重启后端**（新插件要重新扫描）。判据：`/object_info` 里能搜到
`TESpeedQwenImage21`、`TESpeedVOSR2Image`、`SeeThrough_GenerateLayers`、`ImageResizeKJv2`。

**See-through 还要装它的 Python 依赖**（装进 **ComfyUI 的** Python，不是工作台那个 venv）：

```
<ComfyUI 安装目录>\.venv\Scripts\python.exe -m pip install --no-input \
  "diffusers>=0.37.0" "peft>=0.18.0" "accelerate>=0.20.0" \
  "opencv-python>=4.0.0" "scikit-learn>=1.0.0" matplotlib "bitsandbytes>=0.49.2"
```

判据（**算 diff，不靠「装完了」这种感觉**）：装完 `pip freeze` 与装之前比，
**只应新增上面这些包及其传递依赖**（实测 21 条），外加至多一条已装包的版本升级
（实测是 `huggingface_hub` `1.30.0 → 1.33.0`）；`transformers` / `torch` / `numpy`
**一行都不许动**。

`bitsandbytes` 是唯一有平台风险的一环（Windows + 老架构），确认它能加载 4-bit 权重。

---

## 3. 下模型

四条链路的权重**互不共用**，别拿一条链路的体检表去对另一条。

### 3.1 Qwen 出图（文生图 / 图生图）

| 文件名 | 放在 `models/` 下 | 字节数 | sha256（前 16 位） |
|---|---|---|---|
| `qwen_image_2.1_int8_convrot.safetensors` | `diffusion_models/` | 7,256,783,064 | `cb74113cb03faecd` |
| `qwen3vl_8b_int8_convrot.safetensors` | `text_encoders/` | 9,350,798,360 | `8bfd0f6e12abf2d2` |
| `qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors` | `text_encoders/` | 9,471,072,252 | `9182abae56fe0545` |
| `qwen_image_2.1_vae_bf16.safetensors` | `vae/` | 675,509,688 | `bb21f7473051e1ac` |

> 这四个文件由 `Comfy-Org` 在 HuggingFace 上发布（Qwen-Image-2.1 的 ComfyUI 打包版）。
> **本项目的工作记录里没有留下这四个文件的确切仓库 id**，所以这里不替它猜一个：
> 请以**字节数 + sha256** 为准（工作台的体检也会在「环境体检」里逐个核对它们在不在一
> —— 那是**对着后端自己的候选表**核，比路径可靠）。
>
> 第三个是**提示词改写器**，只在勾上「改写器」时才载入（8.8 GiB，一百多秒）。
> 不打算用改写器的话可以先不下它。

判据：起工作台后，**日志台 → 环境体检**里 `DiT / 文本编码器 / 改写器 / VAE` 四项都是 ✓。

### 3.2 放大超分（VOSR2）

**只能放 `<ComfyUI 安装目录>\models\vosr2\VOSR2\`** —— `vosr2` 不是
`extra_model_paths` 认识的键（插件自己拿安装目录的 `models/` 拼路径）。

| 文件 | 相对 `models/vosr2/VOSR2/` | 字节数 |
|---|---|---|
| `args.json` | 根 | 816（**缺了插件扫不到整个 bundle**） |
| `dinov2_vitl14_pretrain.pth` | 根 | 1,217,586,395 |
| `ema_model.safetensors` | `checkpoints/` | 5,576,385,424 |
| `diffusion_pytorch_model.safetensors` | `Qwen-Image-vae-2d/` | 178,418,892 |
| `config.json` | `Qwen-Image-vae-2d/` | 811 |

来源：HuggingFace repo **`CSWRY/VOSR`**（约 6.49 GiB）。
判据：**放大模式下「放大方式」下拉里能选到 `VOSR2`**（那是插件扫出来的候选表，
不是我们猜的路径）。

### 3.3 拆层（See-through）

**放 `<ComfyUI 安装目录>\models\SeeThrough\<目录名>\`**，下一层那两个目录名就是
Load 节点下拉里要选的值。

| 目录名 | 来源（HuggingFace repo） | 文件数 / 体积 |
|---|---|---|
| `seethroughv0.0.2_layerdiff3d_nf4` | `24yearsold/seethroughv0.0.2_layerdiff3d_nf4` | 24 个 / 3.51 GiB |
| `seethroughv0.0.1_marigold_nf4` | `24yearsold/seethroughv0.0.1_marigold_nf4` | 16 个 / 1.80 GiB |

合计 **40 个文件 / 5,693,320,049 字节**（这是当时的验收判据：总字节逐位相等 +
两个 `model_index.json` 都在 + 没有残留 `.incomplete`）。

**下载方式：逐文件 `curl`，别用 `snapshot_download`。**

```bash
curl -sS -L --fail --retry 4 --retry-delay 2 -C - -o <目标文件> \
  <镜像站>/<repo>/resolve/main/<仓库内路径>
```

- `-L` 必需（`resolve/main` 会 307 到真身）。
- 判据落在**字节数**上（镜像给的是真权重）。
- **`huggingface_hub.snapshot_download` 在这条路上会卡死，不是慢**：`.incomplete`
  文件 0 字节、日志三十秒纹丝不动。别用。

**★ 必须做 fp16 副本，否则拆层会 OOM（这不是优化）**

老一代 GPU（Turing / sm_75）上，PyTorch 的 SDPA **内存高效注意力后端只吃 fp16/fp32、
不吃 bf16**；而插件那两个 Load 节点写死 `dtype = torch.bfloat16` ⇒ 只能落到会
**把注意力矩阵整个实体化**的后端 ⇒ 深度那一段必 OOM。改成 fp16 之后总耗时
**20:51 → 4:12（约 5×）**，显存也降一半多。

三个动作（**零额外磁盘**）：

1. 用**硬链接**复制两份目录，名字加 `_fp16` 后缀：
   `cp -rl <原目录> <原目录>_fp16`
   判据：大权重的 `stat -c %h` = 2。
   ⚠ **之后改副本里的文件必须写临时文件 + `os.replace`** —— `cp -rl` 之后副本的
   `config.json` 与原目录是**同一个 inode**，用 `open(p,"w")` 覆写会把原目录一起改掉。
2. 改副本里 **5 个 `config.json`** 的 `quantization_config.bnb_4bit_compute_dtype`：
   `bfloat16` → `float16`（layerdiff 的 `unet` / `text_encoder` / `text_encoder_2`
   + marigold 的 `unet` / `text_encoder`）。两个 VAE 没有量化配置，不用改。
3. 给插件打一个小补丁，让它的 `dtype` **从模型配置里读**而不是写死：
   新增一个 `_quant_compute_dtype(model_path, fallback, use_nf4)`，读
   `<模型目录>/unet/config.json` 的 `bnb_4bit_compute_dtype` 映射成 torch dtype，
   两个 Load 节点各加一行。这样**两个目录都能用**，切换 = 下拉里换个名字。

工作台的基准图里冻结的目录名是 **带 `_fp16` 的那两个**（见 `layers_build.py`）。
判据：拆层模式下 Load 节点的下拉里能选到它们，且日志里
`quant_mode=nf4, local=True, local_files_only=True`（`local_files_only=True` 证明没走网络）。

**★ LaMa 检查点必须预放**（这是唯一一条换 HF 端点救不了的依赖）

插件里有个节点会 `torch.hub.load_state_dict_from_url` 一个**写死的完整 URL**；
torch 读自己的缓存，所以只能把文件预放进去：

```
$TORCH_HOME/hub/checkpoints/lama_large_512px.ckpt     # 204,544,673 字节
```

来源：HuggingFace repo `dreMaz/AnimeMangaInpainting` 里的
`lama_large_512px.ckpt`。判据：**204,544,673 字节**，且前 4 字节是 `PK\x03\x04`
（合法 torch zip）。
**缺了不会崩，只会静默地不拆前后发**（少一层、结果看起来「也对」）。
工作台的体检会检查它 —— 注意它检查的是 **`TORCH_HOME` 指的那个位置**。

### 3.4 MiniMax H3 视频

| 文件名 | 放在 `models/` 下 | 字节数 |
|---|---|---|
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `diffusion_models/Minimax_H3/` | 20,970,379,616 |
| `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | `text_encoders/` | 27,141,342,152 |
| `minimax_h3_video_vae_fp16.safetensors` | `vae/` | 5,207,808,496 |
| `minimax_h3_audio_vae_fp32.safetensors` | `vae/` | 605,254,808 |
| `minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors` | `loras/minimax_h3/` | 1,956,171,984 |

合计 **52.04 GiB**。前四个来自 HuggingFace repo **`Comfy-Org/MiniMax-H3`**；
**最后一个（4 步加速 LoRA）不在官方仓库**，在 **`Kijai/MiniMax-H3_comfy`**。

**别下错变体**：要 `fl2va` 不是 `ref2va`；文本编码器要 `int8_convrot` 不是别的量化格式。
**不要走 GGUF 那条路**：GGUF 加载器不打磁盘直读的标记 ⇒ 模型会整块压进内存，
24 GB 内存会被打穿（实测四次尝试全失败）。**用 safetensors。**

判据：H3 模式下提交前体检里 `H3 主链` 与 `ImageResizeKJv2` 两项都不是 error。

### 3.5 可选：Turbo 加速（Qwen）

| 文件名 | 放在 `models/` 下 | 字节数 | 来源 |
|---|---|---|---|
| `qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors` | `loras/` | 913,314,512 | `Comfy-Org/Qwen-Image-2.1` |

**默认关**，只在勾上「Turbo」时才需要。勾了但文件不在 ⇒ 提交必然失败（体检会拦住）。

---

## 4. 装工作台

```powershell
# 1) 解释器装在仓库**之外**（见下面那条 Windows 坑）
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" -m venv "$env:LOCALAPPDATA\qwen21-venv"
& "$env:LOCALAPPDATA\qwen21-venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
```

> **装 `requirements-lock.txt`，不是 `requirements.txt`**：前者是实测跑通的那一组完整版本，
> `send2trash` / `Pillow` / `psutil` 都在里面；后者只是直接依赖的分组说明，用来理解
> 「为什么是这些包」。若你只装后者，`send2trash` 与 `Pillow` 要自己补上
> （`psutil` 已经在 `requirements.txt` 里）。
>
> **为什么它们值得单独说**：源码里是**惰性 import**，所以「忘了装」的症状**不是工作台起不来**，
> 而是「删除按钮用不了」「缩略图变成原图」—— 没有症状的坏。
>
> **Windows 那条坑**：如果仓库所在的目录带 **Low 完整性标签**（某些沙箱会给工作目录
> 打这种标签），从它里面创建出来的 `python.exe` 也带这个标签，于是它写 `%TEMP%` 与
> ComfyUI 安装目录**一律被拒** —— 拦人的是**强制标签**，不是权限位（DACL 上明明写着
> 你有完全控制）。把解释器装在仓库外就没有这个问题。

```powershell
# 2) 告诉它后端在哪、产物写哪（日常只需要这两个）
$env:QWEN21_COMFY_ROOT = "<ComfyUI 安装目录>"
$env:QWEN21_OUTPUT_DIR = "<产物目录>"

# 3) 起工作台（要在**仓库根目录**下执行 —— `-m` 从当前目录找包）
& "$env:LOCALAPPDATA\qwen21-venv\Scripts\python.exe" -m workbench.server --port 8189
#    → http://127.0.0.1:8189
```

也可以直接双击 `run.bat`（纯 ASCII）。

### 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `QWEN21_COMFY_ROOT` | Comfy Desktop 的默认实例路径 | **ComfyUI 安装目录**。实例名不是默认那个时必须显式设 |
| `QWEN21_OUTPUT_DIR` | `<仓库上一层>/output` | 出图落点。不设的话画廊是空的（因为 ComfyUI 写的是安装目录的 `output/`） |
| `QWEN21_WORKSPACE` | 仓库的上一层 | 「现场」根：产物、归档、探针报告都在这儿 |
| `QWEN21_STATE` | `<仓库>/state` | 工作台状态目录（任务账、提示词库、缩略图缓存） |
| `QWEN21_HOST` / `QWEN21_PORT` | `127.0.0.1` / `8189` | 工作台监听地址与端口 |
| `QWEN21_COMFY_HOST` / `QWEN21_COMFY_PORT` | `127.0.0.1` / `8188` | 后端地址与端口（同机时不用改） |
| `QWEN21_ARCHIVE_DIR` | `<工作区>/output-archived` | 画廊的归档目录（「删除」= 挪到这里，不碰回收站） |
| `QWEN21_CACHE_DIR` | `<安装目录的上一层>/cache` | 缓存根：`HF_HOME` / `TORCH_HOME` 都钉在它下面 |
| `QWEN21_BACKEND_CACHE` | `<工作区>/.cache/comfy-backend` | **受限模式**下后端的临时 / 用户 / 输入目录 |
| `QWEN21_PROBE_REPORT` | `<工作区>/node_probe_report.json` | 逐节点探针的报告 |
| `QWEN21_PROBE_STATE` | 报告旁边那份 `probe_state.json` | 探针的活文件（与报告同目录） |
| `QWEN21_BASE` / `QWEN21_H3_BASE` / `QWEN21_LAYERS_BASE` | 仓库自带 `workflows/` 里那三张图 | 基准工作流图的完整路径（**给了就以它为准，不做存在性检查**） |
| `QWEN21_PYTHON` | — | `run.bat` 选解释器的第一优先级 |
| `QWEN21_READY_TIMEOUT` | `180` | 一键启动后端时等它就绪的上限（秒） |

**起后端时必须有这四个**（工作台会自动带上；**如果你自己写启动脚本，必须照抄**）：

| 变量 | 值 | 为什么 |
|---|---|---|
| `HF_ENDPOINT` | 你的 HuggingFace 镜像站（例如 `https://hf-mirror.com`） | 拆层插件源码里写死了 4 个外部 HF 仓库，其中一个的加载**没有 try/except** ⇒ 连不上就退避 5 次后整次跑死 |
| `HF_HUB_DISABLE_TELEMETRY` | `1` | 关遥测 |
| `HF_HOME` | `QWEN21_CACHE_DIR/hf` | HF 缓存位置。**别让它落在 `~` 下** |
| `TORCH_HOME` | `QWEN21_CACHE_DIR/torch` | torch 缓存位置。**LaMa 检查点就住在这里** |

> **为什么缓存位置要钉死**：`~` / `%APPDATA%` / `%LOCALAPPDATA%` 都是**账户相对**的。
> 如果后端由计划任务以另一个账户（例如 SYSTEM）启动，它看到的 `~` 与你的不是同一个目录
> ⇒ 表现为「模型明明下过，它却去联网重下」「检查点明明在，体检说不在」。
> 工作台的「环境体检」里有一行**开机脚本**：它会读你的启动脚本、与这四个变量对照，
> 不一致就标黄点名 —— 这条就是为这个坑准备的。

---

## 5. 验收

**别只看「服务起来了」。** 按顺序做：

1. **环境体检**（页面 → 日志 → 环境体检）：解释器、状态目录、后端三态、四个模型文件、
   探针、启动命令。四项模型都 ✓ 才算 Qwen 那条链路可用。
2. **文生图**跑一张 1024²：约 **61~73 秒**（不含改写器）。产物出现在画廊里。
3. **改提示词再跑一张，比像素**：两张逐像素**相同** ⇒ 提示词没生效（多半是图构建器
   的问题，不是模型）。**`status: completed` 只说明进程没崩，不说明出图正确。**
4. **图生图**：参考图先放进后端读的那个 `input/`（页面上传就是走这条路），
   1024² / 25 步约 **111 秒**。
5. **放大**：源图 + 倍数，约 **13 秒**（2048² 输出）。判据是下拉里能选到 `VOSR2`。
6. **拆层**：1024² 约 **252 秒 / 23 层**；1280² 约 **403 秒 / 24 层**。
   **层数不要预判** —— 它随分辨率与种子变。跑完把层文件**用眼睛看**：
   脸 / 眼 / 发 / 衣服分对了没有、α 干不干净。
7. **H3 视频**：4 步 / 5 秒 / 480×864，约 **310 秒**。跑之前腾显存（峰值 96.5%）。
   时长会被**吸附到 17 帧网格**（5 秒 → 124 帧）。**10 秒那一档从没实测过**，别当已知。

---

## 6. 最容易「静默坏掉」的清单

这些都不报错、不崩溃，只是结果不对 —— 部署时逐条对一遍：

| 现象 | 原因 |
|---|---|
| 画廊一张都删不掉 | 漏装 `send2trash`（**它绝不退回直接删文件** —— 那会让「删不掉」变成「永久删掉」） |
| 缩略图变成原图 | 漏装 `Pillow`（缩图失败时发原图是**设计**，不是 500） |
| 拆层少了一层、前后发没拆开 | LaMa 检查点不在 `TORCH_HOME` 指的位置 |
| 拆层整次跑死、GPU 0% | 没设 `HF_ENDPOINT`（它在够一个连不上的域名，退避 5 次） |
| 「模型明明下过，它却去联网重下」 | `HF_HOME` / `TORCH_HOME` 落在 `~` 下，而启动账户不是你 |
| 提示词没生效、输出只由种子决定 | 喂给后端的图**断边**了（本项目自带的 `workflows/` 里那三张图是修好的；自己从 UI 导出要当心） |
| 同一份参数再提一遍，磁盘上什么都没多 | ComfyUI 的节点缓存命中。工作台每次提交会换收尾前缀来避开它 |
| 画质被抹平 | 开了 LazyCache（省一半时间，代价是细节） |
| 出图慢了约 2× | TE-Speed 插件没装/没生效（它默认开） |

---

## 7. 已知未核实项

诚实列出来，别当成已知：

- **Qwen 那四个权重的确切 HuggingFace repo id** 没有留在记录里（字节数与 sha256 有）。
- **See-through 40 个文件的逐文件 sha256** 没有（当时的判据是总字节数 + `model_index.json`）。
- **拆层基准图里冻结的目录名**（带 `_fp16` 后缀）需要在**活的**后端上对着
  Load 节点的候选表核一遍 —— 源码里那两行自己也标着「待核实」。
- **H3 权重在候选表里带不带 `Minimax_H3\` 那层子目录**（决定 `unet_name` 要不要写反斜杠）
  同样要对着活后端核。
- **H3 的 10 秒档**没有实测数据。
- **TE-Speed / VOSR2 插件的许可**：仓库里没有 LICENSE 文件，`.pyd` 是闭源二进制。
