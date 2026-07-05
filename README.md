# hwaccelerated-transcoding

一个面向素材批处理场景的 **FFmpeg 硬件转码脚本**。主脚本为 `transcode_hw_main.py`，支持 `NVENC / QSV / AMF` 硬件编码、自定义 FFmpeg 参数、分组批处理、失败回退和音频校验。

## 1. 当前能力

- 支持单文件或目录输入。
- 目录模式支持递归扫描、扩展名过滤、文件名前缀/后缀过滤与反选。
- 默认按 `width / height / fps` 分组；`--no-group` 可把所有输入视为一组。
- 支持交互配置，也支持 `--skip --skip-builtin-checks` 非交互执行。
- 支持 `--custom-params` 直接注入 FFmpeg 参数。
- 默认只重编码主视频流，音频流单独抽取后以 `copy` 方式混回输出文件。
- 硬件编码失败时会自动尝试一次 CPU 回退。
- 生成 preflight、分组、任务结果、转码前后媒体信息、音频校验和逐任务日志。

## 2. 环境要求

- Python 3.8+
- FFmpeg
- FFprobe
- 目标硬件编码器对应的驱动、运行时和设备权限

快速自检：

```bash
python3 --version
ffmpeg -version
ffprobe -version
ffmpeg -encoders | rg -E "(h264_nvenc|hevc_nvenc|h264_qsv|hevc_qsv|h264_amf|hevc_amf)"
```

没有 `rg` 时可直接运行 `ffmpeg -encoders` 后手动搜索 `nvenc/qsv/amf`。

## 3. 输出与工作目录

- `--src`：输入文件或输入目录。
- `--dst`：输出目录；省略时会在源路径同级创建 `<src>_comp`。
- `--work`：工作目录；省略时会在源路径同级创建 `<src>_work`。
- 单文件模式也会输出到 `--dst` 指定目录；没有 `--dst` 时输出到 `<src>_comp`。
- 默认保持输入文件名；目标冲突或目标等于源文件时自动追加 `_comp`、`_comp2` 等后缀。
- `--out-suffix` 可追加输出文件名后缀，例如 `--out-suffix deliver` 会生成 `_deliver` 后缀。
- `--flat-output` 可把目录批处理结果平铺到同一个输出目录。

## 4. 典型用法

### 4.1 查询编码器参数

```bash
python3 transcode_hw_main.py --query-params nvenc --work ./work
python3 transcode_hw_main.py --query-params qsv --work ./work
python3 transcode_hw_main.py --query-params amf --work ./work
```

### 4.2 单文件非交互转码

```bash
python3 transcode_hw_main.py \
  --src ./video.mp4 \
  --dst ./out \
  --work ./work \
  --skip \
  --skip-builtin-checks
```

### 4.3 目录批量非交互转码

```bash
python3 transcode_hw_main.py \
  --src ./materials \
  --dst ./out \
  --work ./work \
  --skip \
  --skip-builtin-checks
```

### 4.4 关闭分组，所有文件使用同一套参数

```bash
python3 transcode_hw_main.py \
  --src ./materials \
  --dst ./out \
  --work ./work \
  --no-group \
  --encoder nvenc \
  --codec hevc \
  --skip \
  --skip-builtin-checks
```

### 4.5 递归扫描并筛选扩展名

```bash
python3 transcode_hw_main.py \
  --src ./media \
  --recursive \
  --extensions mp4,mov,mxf \
  --dst ./out \
  --work ./work \
  --skip \
  --skip-builtin-checks
```

### 4.6 使用自定义 FFmpeg 参数

```bash
python3 transcode_hw_main.py \
  --src ./media \
  --dst ./out \
  --work ./work \
  --custom-params "-c:v libx264 -crf 20 -c:a aac -b:a 192k" \
  --skip
```

`--custom-params` 不要包含输出文件名。该模式会按用户提供的参数执行，不套用脚本内置的视频编码、音频 copy、强制质量等策略。

### 4.7 只生成分组和媒体探测信息

```bash
python3 transcode_hw_main.py \
  --src ./media \
  --work ./work \
  --show-groups-only
```

## 5. 关键参数

### 5.1 输入筛选

- `--recursive`：递归处理子目录。
- `--extensions`：扩展名白名单，默认 `mp4,mov`。
- `--prefixes` / `--suffixes`：文件名前缀/后缀白名单。
- `--invert-prefix` / `--invert-suffix`：排除命中白名单的文件。

### 5.2 编码参数

- `--encoder {nvenc,qsv,amf}`：硬件编码后端，默认 `nvenc`。
- `--codec {hevc,h264}`：视频编码格式，默认 `hevc`。
- `--rc-mode {vbr,cbr,cqp,icq}`：码控方式；非交互默认使用 `cqp`。
- `--min-br` / `--max-br`：VBR/CBR 码率参数，单位 Mbps。
- `--cqp`：CQP/ICQ 质量值。
- `--preset`：传给底层编码器的 FFmpeg 参数；NVENC 最终仍会受到默认强制质量策略影响，除非使用 `--nvenc-qual` 手动覆盖。

### 5.3 强制质量策略

普通模式下脚本会在最终命令中加入以下硬件编码质量参数：

- NVENC：`-preset p7`
- QSV：`-tu 1`
- AMF：`-quality quality`

可用以下参数改写：

- `--nvenc-qual p7`
- `--qsv-qual tu1` 或 `--qsv-qual 1`
- `--amf-qual quality`

`--custom-params` 模式不应用这些自动策略。

### 5.4 执行控制

- `--skip`：跳过交互配置。
- `--skip-builtin-checks`：跳过执行前确认，适合 CI 或无人值守任务。
- `--concurrency`：并发 FFmpeg 进程数。
- `--timeout`：单任务超时秒数。
- `--show-groups-only`：写出探测和分组文件后退出。

## 6. 音频、元数据与容器策略

普通模式下脚本会优先保护源音频：

1. 源文件有音频时，先把音频流抽取到临时 `.mka`，编码视频，再把视频和音频以 `-c:a copy` 混回输出。
2. 源文件无音频时跳过音频抽取步骤，只执行视频转码。
3. 任务完成后先校验音频流数量；全部任务结束后写出 `audio_verify.csv`，逐条校验音频流数量、声道数、采样率、可用声道布局、时长差异和解码后的 PCM 哈希。
4. 自定义参数模式由用户自己控制音频参数，脚本不会强制 `copy`。

容器与元数据策略：

- 默认保留输入元数据和章节。
- 默认只重编码主视频流 `0:v:0`，避免附加封面图等视频流被误重编码。
- MOV 输出会尽量保留数据流和附件流。
- MP4 输出仅保留常见可封装的数据流，例如 `tmcd/gpmd/camm/mett/metx/rtmd`。
- 探测到 metadata/data/附件等扩展流时，会自动把该文件输出为 MOV，以提高封装兼容性。

## 7. 产物说明

工作目录会包含：

- `preflight_files.csv`：输入文件探测结果。
- `groups_summary.csv`：分组汇总。
- `tasks_preflight.json`：执行前任务清单和命令。
- `tasks_result.csv`：每个任务的最终命令、日志、返回码和备注。
- `pre_media_info.csv` / `post_media_info.csv`：转码前后主视频流信息。
- `audio_verify.csv`：转码后音频校验结果。
- `logs/` 或输出目录同级 `*_logs/`：逐任务 FFmpeg 日志。

## 8. 故障排查

- `ffmpeg-not-found`：确认 FFmpeg/FFprobe 已安装并在 `PATH` 中。
- 硬件编码失败：先用 `--query-params` 检查编码器可用性，再确认驱动、设备映射和容器权限；脚本会自动尝试一次 CPU 回退。
- 音频校验失败：查看 `audio_verify.csv` 的 `note` 字段以及对应任务日志。常见原因包括自定义参数重编码音频、输出容器不支持某些音频流、源文件音频探测信息异常。
- 批量任务过慢或失败多：把 `--concurrency` 调低到 `1`，并使用 `--timeout` 防止单任务卡死。

## 9. 输入文件探测

脚本现在会在生成任何编码任务前先调用 `ffprobe` 探测每个输入文件主视频流的像素格式，并由此推导：

- 位深：例如 `8bit` / `10bit`。
- 色度采样：例如 `4:2:0` / `4:2:2` / `4:4:4`。
- 探测时间：UTC ISO 风格时间戳。

探测结果会写入工作目录：

- `input_probe.csv`：字段包含 `file_path,bit_depth,chroma_subsampling,pixel_format,probe_time`。
- `preflight_files.csv`：在原有媒体信息基础上同步附加 `bit_depth,chroma_subsampling,probe_time`，便于和任务日志一起归档。

## 10. 编码器兼容性与 fallback 策略

任务生成阶段会调用 `ffmpeg -encoders` 和 `ffmpeg -h encoder=<encoder>` 探测当前 FFmpeg 可用的 HEVC 硬件编码器能力，并写入 `encoder_capabilities.csv`：

- `hevc_nvenc`
- `hevc_qsv`
- `hevc_amf`

CSV 字段包含 `encoder_name,available,supported_profiles,supported_pixel_formats,probe_time`。

兼容性处理策略：

1. 当输入/输出位深与色度采样可由能力表支持时，脚本不主动指定 `-pix_fmt`，尽量让 FFmpeg 保持输入输出格式一致。
2. 当输入格式可能不被所选硬件路径支持时，脚本打印 `WARNING`，并自动移除 `-hwaccel` 相关参数，改用 CPU 软解后继续硬件编码。
3. 使用 `--skip-check`（等价于 `--skip-builtin-checks`）时会跳过确认提示，但仍打印完整 FFmpeg 命令；脚本会按阶梯设置输出格式 fallback：10bit 输入优先 `p010le`，否则使用 `yuv420p`。
4. 每次 fallback 都会打印 `WARNING`，并在编码后输出实际参数摘要。

## 11. AMD AMF 参数说明

AMF 会根据输入位深自动选择不同模板：

- 10bit 输入：使用 `hevc_amf` + `-pix_fmt p010le` + CQP 参数。
- 8bit 输入：使用 `hevc_amf` + `-pix_fmt yuv420p` + HQVBR 参数。

> 作者无 AMD 显卡进行实际验证，AMF 参数仅通过查阅 FFmpeg 文档及 AMD 官方资料整理，实际运行可能存在兼容性问题，欢迎 AMD 用户反馈。

## 12. 新增/更新参数

- `--skip-check`：`--skip-builtin-checks` 的别名。跳过执行前确认与严格检查，适合无人值守批处理；仍会打印完整 FFmpeg 命令和 fallback 后的实际参数摘要。
