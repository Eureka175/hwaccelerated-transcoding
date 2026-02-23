# hwaccelerated-transcoding

一个面向素材批处理场景的 **FFmpeg 硬件转码脚本**，支持 `NVENC / QSV / AMF`，并提供：

- 单文件或目录批量转码
- 按分辨率 + 帧率自动分组（可关闭）
- 交互式参数确认，或 `--skip` 全自动执行
- 8 组内置预设（含 2-pass 档案/发布/代理/社媒）
- 任务级日志、前后媒体信息 CSV、结果汇总 CSV
- 硬件编码失败后的 CPU 自动回退（提高批量任务鲁棒性）

主脚本：`transcode_hw_main.py`。

---

## 1. 功能概览

### 转码输入模式
- `--src` 可是单个文件，也可以是目录。
- 目录模式支持：
  - 是否递归：`--recursive`
  - 扩展名筛选：`--extensions`
  - 文件名前缀/后缀白名单筛选：`--prefixes` / `--suffixes`
  - 前后缀反选：`--invert-prefix` / `--invert-suffix`

### 分组与参数策略
- 默认按 `width/height/fps` 分组。
- 可通过 `--no-group` 把所有素材视作单组。
- 使用 `--use-preset` 时默认不分组（可用 `--group` 强制开启分组）。
- 未显式指定码率时，会根据长边分辨率给出建议区间。

### 输出与可追踪性
运行时会输出多类工件，便于排障和复盘：

- `preflight_files.csv`：输入文件探测结果
- `groups_summary.csv`：分组摘要
- `tasks_preflight.json`：执行前完整任务列表（含 ffmpeg 命令）
- `tasks_result.csv`：执行结果（成功/失败、耗时、日志路径等）
- `pre_media_info.csv` / `post_media_info.csv`：转码前后媒体信息
- 每个任务独立 `.log`

---

## 2. 环境要求

- Python 3.8+
- FFmpeg（需包含你要使用的硬件编码器）
- FFprobe（通常随 FFmpeg 一起提供）

建议先确认可用编码器：

```bash
ffmpeg -encoders | rg -E "(nvenc|qsv|amf)"
```

若 `rg` 不可用，可改为：

```bash
ffmpeg -encoders
```

---

## 3. 快速开始

### 3.1 查询硬件编码器参数（不转码）

```bash
python3 transcode_hw_main.py --query-params nvenc --work ./work
```

会输出并保存：`./work/logs/query-nvenc.txt`。

### 3.2 单文件快速转码（预设）

```bash
python3 transcode_hw_main.py --src ./video.mp4 --use-preset preset1
```

### 3.3 目录批量转码（预设）

```bash
python3 transcode_hw_main.py --src ./input_dir --use-preset preset5
```

默认输出目录为 `./input_dir_comp`。

### 3.4 目录 + 分组 + 非交互执行

```bash
python3 transcode_hw_main.py \
  --src ./input_dir \
  --dst ./output_dir \
  --work ./work_dir \
  --group \
  --skip
```

---

## 4. 常用参数说明

### 核心路径
- `--src`：源文件/目录
- `--dst`：目标根目录（可省略，脚本会按规则自动生成）
- `--work`：工作目录（CSV/JSON/日志）

### 转码控制
- `--encoder {nvenc,qsv,amf}`：硬件编码后端
- `--codec {hevc,h264}`：视频编码格式
- `--rc-mode {vbr,cbr,cqp,icq}`：码控模式
- `--min-br / --max-br`：VBR/CBR 比特率（Mbps）
- `--cqp`：CQP/ICQ 质量值
- `--preset`：底层编码器 preset（如 `p1..p7`、`TU1..TU7`）

### 自动化与筛选
- `--recursive`：递归扫描目录
- `--extensions`：扩展名白名单
- `--prefixes / --suffixes`：文件名前/后缀白名单
- `--skip`：跳过交互，直接执行
- `--concurrency`：并发任务数
- `--timeout`：单任务超时秒数

### 输出命名
- `--out-suffix`：输出文件名后缀（自动补 `_`）
- `--flat-output`：平铺输出（不保留原目录结构）

---

## 5. 内置预设

| ID | 名称 | 说明 |
|---|---|---|
| preset1 | 4k_prog_archive_1pass | HEVC NVENC P7，1-pass，vbr_hq 30/40 |
| preset2 | 1080p_prog_rel_1pass | HEVC QSV TU1，1-pass，VBR 6/8 |
| preset3 | 4k_prog_archive_2pass | HEVC NVENC P7，multipass fullres |
| preset4 | 1080p_prog_rel_2pass | HEVC x265 slow，2-pass 6M |
| preset5 | fast_proxy_gen_halfres_avc_5m | AVC NVENC P1，CBR 5M，半分辨率代理 |
| preset6 | fast_proxy_gen_fullres_avc_5m | AVC NVENC P1，CBR 5M，全分辨率代理 |
| preset7 | social_plat_share_halfres | HEVC QSV TU3，ICQ 28，半分辨率 |
| preset8 | social_plat_share_fullres | HEVC QSV TU2，ICQ 27，全分辨率 |

> 说明：`preset4` 为 CPU 两遍编码方案，适合高质量发布归档类任务。

---

## 6. 推荐工作流

1. 先跑一次预检（可用 `--show-groups-only`）确认分组与素材识别是否正确。  
2. 小样本试转（1~3 个文件），确认画质/码率/耗时。  
3. 再开并发批量跑（`--concurrency` 根据 GPU/磁盘性能调节）。  
4. 批次结束后核对：
   - `tasks_result.csv`
   - `post_media_info.csv`
   - 失败任务日志

---

## 7. 故障排查

### 7.1 提示 ffmpeg/ffprobe 未找到
确认二者已安装并在 `PATH` 内：

```bash
ffmpeg -version
ffprobe -version
```

### 7.2 硬件编码初始化失败
- 检查驱动、运行环境（容器/远程桌面/虚拟机）和编码器权限。
- 先用 `--query-params nvenc|qsv|amf` 验证编码器是否可见。
- 脚本在硬件转码失败时会尝试自动回退到 CPU（单次重试）。

### 7.3 输出路径冲突
若使用 `--flat-output`，同名文件会自动加源目录名后缀避免覆盖。

---

## 8. 命令示例（可直接改路径使用）

```bash
# 1) 仅查询 QSV 参数
python3 transcode_hw_main.py --query-params qsv --work ./work

# 2) 目录转码 + 自定义后缀 + 平铺输出
python3 transcode_hw_main.py \
  --src ./material \
  --dst ./out \
  --flat-output \
  --out-suffix _deliver \
  --skip

# 3) 指定编码器与码率区间，非交互执行
python3 transcode_hw_main.py \
  --src ./material \
  --dst ./out \
  --encoder nvenc \
  --codec hevc \
  --rc-mode vbr \
  --min-br 12 \
  --max-br 18 \
  --skip
```

---

## 9. 文件结构

```text
.
├── README.md
└── transcode_hw_main.py
```

如果你希望，我也可以继续给这个仓库补一份：
- `README_EN.md`（英文版）
- `docs/recipes.md`（不同场景参数模板）
- `examples/`（可直接运行的命令清单）
