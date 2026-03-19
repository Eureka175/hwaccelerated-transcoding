# hwaccelerated-transcoding

一个面向素材批处理场景的 **FFmpeg 硬件转码脚本**，支持 `NVENC / QSV / AMF`，并提供：

- 单文件与目录批量转码
- 按分辨率 + 帧率自动分组（可关闭）
- 交互式配置、非交互 `--skip`、预设模式、自定义参数模式
- 并发执行、超时控制、失败自动回退 CPU
- 前后媒体信息、任务结果、分组摘要、逐任务日志的完整追踪

主脚本：`transcode_hw_main.py`。

---

## 1. 功能概览

### 1.1 输入与筛选
- `--src` 支持：
  - 单个视频文件
  - 目录批处理
- 目录模式可配置：
  - `--recursive`：递归子目录
  - `--extensions`：扩展名白名单（默认常见视频格式）
  - `--prefixes` / `--suffixes`：文件名前后缀白名单
  - `--invert-prefix` / `--invert-suffix`：对白名单反选

### 1.2 执行模式
程序支持 3 类执行路径：
1. **Preset 模式**：`--use-preset preset1..preset8`，适合快速落地。
2. **Custom 模式**：`--custom-params "..."`，直接把参数注入 ffmpeg 命令。
3. **Interactive/CLI 模式**：按分组交互配置，或结合 `--skip` 非交互执行。

### 1.3 分组策略
- 默认按 `width / height / fps` 自动分组。
- `--no-group` 可关闭分组，把所有输入视为一组。
- 与 `--use-preset` 联用时默认不分组；可用 `--group` 强制分组。

### 1.4 结果可追踪性
每次任务会生成结构化产物，便于审计、回溯、排障：
- `preflight_files.csv`：输入探测信息
- `groups_summary.csv`：分组汇总
- `tasks_preflight.json`：执行前任务清单（含命令）
- `tasks_result.csv`：任务执行结果
- `pre_media_info.csv` / `post_media_info.csv`：转码前后媒体信息
- `*_logs/*.log`：每个任务独立日志


### 1.5 强制质量策略（本版本默认）
无论你在命令行、交互模式、预设模式里如何设置，脚本都会在最终 ffmpeg 命令层面强制：
- NVENC：`-preset p7`
- QSV：`-tu 1`
- AMF：`-quality quality`（AMF 最高质量档）

另外：
- 默认仅压缩主视频流（`0:v:0`），避免 attached pic 等附加视频流触发误重编码。
- 默认保留全部输入元数据与章节（含常见运动相机型号信息）。流保留按容器能力处理：MOV 尽量保留数据/附件流；MP4 会在保证可封装前提下尽量保留安全的数据流（如 timecode/tmcd），并规避会导致封装失败的私有 data track。
- 输出默认保持原始文件名和后缀；仅在目标冲突时自动追加 `_comp`（如 `A.mp4 -> A_comp.mp4`）。
- 若 `ffprobe` 发现 metadata/data/附件等扩展流（如 `djmd/dbgi/tmcd`），该文件自动改为输出 `MOV`，以规避 MP4 容器兼容问题。
- 音频策略：`MP4 -> AAC 320k`（不强制 `-ac`，保留原始声道布局）；`MOV -> audio copy`。

---

## 2. 环境要求

### 2.1 必备组件
- Python 3.8+
- FFmpeg（含目标硬件编码器）
- FFprobe（通常随 FFmpeg 安装）

### 2.2 快速自检
```bash
python3 --version
ffmpeg -version
ffprobe -version
```

### 2.3 硬件编码能力检查
```bash
ffmpeg -encoders | rg -E "(h264_nvenc|hevc_nvenc|h264_qsv|hevc_qsv|h264_amf|hevc_amf)"
```

如果本机没有 `rg`：
```bash
ffmpeg -encoders
```
然后手动搜索 `nvenc/qsv/amf`。

---

## 3. 快速开始（扩展版）

> 下面命令均可直接复制，按你的路径替换即可。

### 3.1 仅查询编码器参数（不执行转码）
```bash
python3 transcode_hw_main.py --query-params nvenc --work ./work
python3 transcode_hw_main.py --query-params qsv   --work ./work
python3 transcode_hw_main.py --query-params amf   --work ./work
```
用途：先确认当前环境下编码器参数/能力，再决定具体策略。

### 3.2 单文件快速转码（最省心）
```bash
python3 transcode_hw_main.py --src ./video.mp4 --use-preset preset1
```
特点：
- 自动生成输出名（`*_comp`）
- 自动生成日志目录与工作工件

### 3.3 目录批量 + 预设（自动输出到 `<src>_comp`）
```bash
python3 transcode_hw_main.py --src ./materials --use-preset preset5
```
适合：代理文件快速批量生成。

### 3.4 目录批量 + 指定输出目录 + 非交互
```bash
python3 transcode_hw_main.py \
  --src ./materials \
  --dst ./out \
  --work ./work \
  --skip
```
特点：
- 不再交互询问，直接按规则执行
- 工作工件集中到 `./work`

### 3.5 目录批量 + 分组执行
```bash
python3 transcode_hw_main.py \
  --src ./materials \
  --dst ./out \
  --work ./work \
  --group
```
特点：
- 按分辨率/帧率分组
- 每组可独立配置参数

### 3.6 先看分组，不执行
```bash
python3 transcode_hw_main.py \
  --src ./materials \
  --work ./work \
  --show-groups-only
```
适合：先检查素材结构和建议配置是否符合预期。

---

## 4. 命令示例

### 4.1 单文件：高质量归档（NVENC HEVC）
```bash
python3 transcode_hw_main.py --src ./a.mov --use-preset preset1
```

### 4.2 单文件：社媒分享（QSV HEVC）
```bash
python3 transcode_hw_main.py --src ./a.mov --use-preset preset8
```

### 4.3 目录：代理文件半分辨率批量生成
```bash
python3 transcode_hw_main.py --src ./rushes --use-preset preset5
```

### 4.4 目录：代理文件全分辨率批量生成
```bash
python3 transcode_hw_main.py --src ./rushes --use-preset preset6
```

### 4.5 目录：强制递归 + 扩展名筛选
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --recursive \
  --extensions mp4,mov,mxf \
  --dst ./out \
  --skip
```

### 4.6 目录：仅处理指定前缀文件
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --prefixes DJI,SONY \
  --dst ./out \
  --skip
```

### 4.7 目录：排除指定后缀（反选）
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --suffixes _proxy,_tmp \
  --invert-suffix \
  --dst ./out \
  --skip
```

### 4.8 不分组：所有文件按统一参数
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --no-group \
  --encoder nvenc \
  --codec hevc \
  --rc-mode vbr \
  --min-br 10 \
  --max-br 16 \
  --skip
```

### 4.9 分组 + 并发执行 + 超时控制
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --work ./work \
  --group \
  --concurrency 2 \
  --timeout 3600 \
  --skip
```

### 4.10 平铺输出（不保留目录树）
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out_flat \
  --flat-output \
  --skip
```

### 4.11 自定义输出后缀
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --out-suffix _deliver \
  --skip
```

### 4.12 用 preset 名作为语义化后缀
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --out-suffix preset1 \
  --use-preset preset1
```

### 4.13 指定 AMF 编码
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --encoder amf \
  --codec h264 \
  --rc-mode vbr \
  --min-br 8 \
  --max-br 12 \
  --skip
```

### 4.14 使用自定义 ffmpeg 参数
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --custom-params "-c:v libx264 -preset slow -crf 20 -c:a aac -b:a 192k" \
  --skip
```
> 注意：`--custom-params` 中不要包含输出文件名。

### 4.15 预设与自定义参数二选一（交互确认）
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --use-preset preset3 \
  --custom-params "-c:v libx265 -crf 22"
```
运行后程序会提示选择 preset / custom / cancel。

### 4.16 单文件 + 指定工作目录（便于定位日志）
```bash
python3 transcode_hw_main.py \
  --src ./clip.mp4 \
  --work ./clip_work \
  --use-preset preset2
```

### 4.17 目录 + 跳过内建确认（CI/自动化场景）
```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --work ./work \
  --skip \
  --skip-builtin-checks
```


---

## 5. 常用参数说明

### 5.1 路径与输入
- `--src`：输入路径（文件或目录）。
- `--dst`：输出根目录。目录模式下建议显式指定，便于资产管理。
- `--work`：工作目录。所有 preflight、CSV、JSON、查询文本等工件集中放置。

### 5.2 扫描与筛选
- `--recursive`：目录递归扫描。
- `--extensions`：扩展名白名单，如 `mp4,mov,mxf`。
- `--prefixes`：仅处理文件名以这些前缀开头的文件。
- `--suffixes`：仅处理文件名以这些后缀结尾（不含扩展名）的文件。
- `--invert-prefix` / `--invert-suffix`：把“命中白名单”改为“排除命中项”。

### 5.3 编码参数
- `--encoder {nvenc,qsv,amf}`：硬件编码器后端。
- `--codec {hevc,h264}`：视频编码格式。
- `--rc-mode {vbr,cbr,cqp,icq}`：码控方式。
- `--min-br/--max-br`：VBR/CBR 比特率范围（Mbps）。
- `--cqp`：质量量化参数（CQP/ICQ 场景）。
- `--preset`：传递给底层编码器的 preset（如 NVENC `p1..p7`，QSV `TU1..TU7`）。

### 5.4 模式控制
- `--use-preset`：启用内置预设（preset1..preset8）。
- `--custom-params`：直接注入 ffmpeg 参数（高级用法）。
- `--group`：在 preset 模式下强制启用分组。
- `--no-group`：关闭分组，所有文件按一套策略处理。
- `--skip`：非交互执行，适合批处理/自动化。

### 5.5 性能与稳定性
- `--concurrency`：并发进程数（建议从 1~2 起测）。
- `--timeout`：单任务超时秒数，防止异常卡死。
- `--skip-builtin-checks`：跳过部分内建确认（CI场景常用）。

### 5.6 输出组织
- `--flat-output`：所有输出平铺在同一目录；同名冲突时自动加后缀。
- `--out-suffix`：输出文件名后缀（自动补 `_`）。
  - 若填 `preset1..preset8`，会映射为预设语义化名称。

### 5.7 查询能力
- `--query-params {nvenc,qsv,amf}`：打印并保存 ffmpeg 编码器帮助信息。
- 可单独执行查询，不必携带完整转码参数。

---

## 6. 内置预设

| ID | 名称 | 说明 |
|---|---|---|
| preset1 | 4k_prog_archive_1pass | HEVC NVENC@P7，1pass，vbr_hq(30/40)，main10 p010 |
| preset2 | 1080p_prog_rel_1pass | HEVC QSV@TU1，1pass，VBR(6/8)，aac@320k |
| preset3 | 4k_prog_archive_2pass | HEVC NVENC@P7，multipass fullres，vbr_hq(30/40) |
| preset4 | 1080p_prog_rel_2pass | HEVC x265 slow，2pass @6M（CPU） |
| preset5 | fast_proxy_gen_halfres_avc_5m | AVC NVENC@P1，CBR 5M，half res，aac@128k |
| preset6 | fast_proxy_gen_fullres_avc_5m | AVC NVENC@P1，CBR 5M，full res，profile high |
| preset7 | social_plat_share_halfres | HEVC QSV@TU3，ICQ 28，half res |
| preset8 | social_plat_share_fullres | HEVC QSV@TU2，ICQ 27，full res |

---

## 7. 故障排查

### 7.1 ffmpeg/ffprobe 未找到
```bash
ffmpeg -version
ffprobe -version
```
确保二者已安装并在 `PATH` 中。

### 7.2 硬件编码器不可用
- 先执行：
  ```bash
  python3 transcode_hw_main.py --query-params nvenc --work ./work
  ```
  或替换为 `qsv/amf`。
- 检查 GPU 驱动、系统权限、容器环境映射。
- 若硬件失败，脚本会自动尝试一次 CPU 回退。

### 7.3 批量任务慢或失败多
- 将 `--concurrency` 调低到 1 或 2 再观察。
- 使用 `--timeout` 限制异常卡死任务。
- 重点看：`tasks_result.csv` + 对应任务日志。

### 7.4 输出文件名冲突
- 平铺输出时同名冲突会自动重命名；
- 可配合 `--out-suffix` 让命名更可控。
