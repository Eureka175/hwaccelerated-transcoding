# transcode_hw_main

面向素材批处理场景的 **FFmpeg 硬件转码工具**，支持 `NVENC` / `QSV` / `AMF` 三平台硬件编码，内置最大化画质参数模板，支持智能分组、多硬件并发调度、音频时长验证与失败自动隔离。

---

## 1. 获取最新版本

从 [GitHub Releases](https://github.com/Eureka175/hwaccelerated-transcoding/releases/) 下载最新版本：

- **Windows 分发包**: `transcode_hw_vX.X.zip`（内含 `transcode_hw_main.exe` + `ffmpeg` + `README.md`）
- 解压到任意目录即可使用，无需安装 Python 或 FFmpeg

> 若首次使用，建议下载最新 Release 而非手动克隆仓库。

---

## 1. 环境要求

- **Windows 10/11**（x64）
- **Python 3.8+**（仅开发/调试时需要，分发版无需安装）
- **FFmpeg + FFprobe**（打包时自动包含，或系统已安装）
- **对应显卡驱动**（NVENC 需 NVIDIA 驱动，QSV 需 Intel 驱动，AMF 需 AMD 驱动）

快速自检：

```bash
ffmpeg -version
ffprobe -version
ffmpeg -encoders | findstr "hevc_nvenc hevc_qsv hevc_amf"
```

---

## 2. 快速开始

### 解压即用

1. 下载 `transcode_hw_vX.X.zip` 并解压到任意目录（如 `D:\tools\transcode_hw`）
2. 双击 `transcode_hw_main.exe` 查看帮助提示
3. 在弹出的 CMD 窗口中输入命令运行

```cmd
transcode_hw_main.exe --src "F:\素材" --dst "F:\输出" --skip
```

> 首次双击会弹出 CMD 窗口并显示使用说明，窗口保持打开，可直接输入命令运行。

### 2.1 单文件转码

```bash
transcode_hw_main.exe --src "F:\素材\video.mp4" --dst "F:\输出" --skip
```

### 2.2 目录批量（默认 NVENC，不分组）

```bash
transcode_hw_main.exe --src "F:\素材" --dst "F:\输出" --work "F:\工作" --skip
```

### 2.3 按拍摄日期自动分组 + 多硬件并发

```bash
transcode_hw_main.exe --src "F:\素材" --dst "F:\输出" --work "F:\工作" ^
  --grp-auto --hardware-pool "nvenc:2,qsv:1" --skip
```

### 2.4 按时段分组（解决"从早拍到晚"）

```bash
transcode_hw_main.exe --src "F:\素材" --dst "F:\输出" --work "F:\工作" ^
  --time-segments "05:00-08:00=dawn,08:00-12:00=morning,12:00-18:00=afternoon,18:00-22:00=evening,22:00-05:00=night" ^
  --skip
```

### 2.5 覆写模板参数

```bash
transcode_hw_main.exe --src "F:\素材" --encoder nvenc ^
  --override-params "-cq 16 -rc-lookahead 32" --skip
```

### 2.6 音频转码（非 copy）

```bash
transcode_hw_main.exe --src "F:\素材" --audio-codec aac --audio-bitrate 320k --skip
```

### 2.7 按组分配不同编码器

```bash
transcode_hw_main.exe --src "F:\素材" --grp-auto ^
  --group-encoder "0:nvenc,1:qsv" --skip
```

---

## 3. 分发与运行

### 3.1 直接使用（推荐）

下载 `transcode_hw_v1.0.zip`，解压到任意目录：

```
D:\tools\transcode_hw\
├── transcode_hw_main.exe
├── ffmpeg\
│   ├── ffmpeg.exe
│   └── ffprobe.exe
└── README.md
```

**双击运行**：会弹出 CMD 窗口并显示使用提示，**窗口保持打开**，可直接输入命令。

**命令行运行**：
```bash
cd D:\tools\transcode_hw
transcode_hw_main.exe --src "F:\素材" --skip
```

### 3.2 自行打包

```bash
# 确保 build.py 与 transcode_hw_main.py 同目录
python build.py
```

`build.py` 会自动：
1. 使用指定 Python 环境（默认 `C:\Users\wwr\AppData\Local\Programs\Python\Python313`）
2. 查找系统中的 `ffmpeg.exe` / `ffprobe.exe`
3. 安装 PyInstaller（若未安装）
4. 打包为单文件 `transcode_hw_main.exe`
5. 生成 `transcode_hw_v1.0.zip`

---

## 4. 分组策略

脚本支持五种分组方式，**优先级固定**：

```
--grp-auto > --grp-regex > --time-segments > --grp-by-time > --grp-prefix
```

未指定任何分组参数时，**不分组**，所有文件视为一组。

### 4.1 自动前缀识别（`--grp-auto`）

按文件名自动识别前缀，优先级：

1. `YYYYMMDD` / `YYMMDD` / `YYYY-MM-DD`
2. 连续字母前缀（如 `DSC_`, `IMG_`）
3. 连续数字前缀

```bash
transcode_hw_main.exe --src "F:\素材" --grp-auto --skip
```

### 4.2 正则分组（`--grp-regex`）

按正则第一个捕获组分组，未匹配归入 `ungrouped`。

```bash
transcode_hw_main.exe --src "F:\素材" --grp-regex "^(\d{8})" --skip
```

### 4.3 自定义时段分组（`--time-segments`）

基于 `ffprobe` 读取的 `creation_time`（UTC 转本地），按时段分组。

```bash
transcode_hw_main.exe --src "F:\素材" --time-segments "08:00-12:00=morning,12:00-18:00=afternoon" --skip
```

支持跨天时段（如 `22:00-05:00`），未匹配文件归入 `ungrouped`。

**时区**：默认 `--timezone 8`（BJT），范围 `-12` ~ `+14`。

### 4.4 固定时间间隔（`--grp-by-time`）

```bash
transcode_hw_main.exe --src "F:\素材" --grp-by-time 4h --skip
```

前缀格式：`YYYYMMDD_HHMM-HHMM`

### 4.5 固定前缀长度（`--grp-prefix`）

```bash
transcode_hw_main.exe --src "F:\素材" --grp-prefix 8 --skip
```

### 4.6 分组输出行为

- **仅1组**：静默，不打印分组信息
- **≥2组**：终端限量打印（最多10组，每组最多5个文件），完整明细写入外部文件

---

## 5. 多硬件调度

支持为不同分组分配不同编码器，并限制每编码器的并发数。

### 5.1 硬件并发池（`--hardware-pool`）

```bash
transcode_hw_main.exe --src "F:\素材" --hardware-pool "nvenc:2,qsv:1" --skip
```

### 5.2 按组分配编码器（`--group-encoder`）

```bash
transcode_hw_main.exe --src "F:\素材" --grp-auto --group-encoder "0:nvenc,1:qsv,2:amf" --skip
```

未指定的组使用 `--encoder` 默认值。

### 5.3 全局并发上限（`--concurrency`）

```bash
transcode_hw_main.exe --src "F:\素材" --concurrency 3 --skip
```

---

## 6. 音频编码策略

| 参数 | 默认 | 说明 |
|------|------|------|
| `--audio-codec` | `copy` | 可选 `aac` / `flac` / `opus` / `pcm_s16le` 等 |
| `--audio-bitrate` | 无 | 仅对非 copy 生效 |

**特殊规则**：
- `--audio-codec aac` 且未指定 `--audio-bitrate` 时，**默认 320k**
- `--audio-codec copy` 时 `--audio-bitrate` 被忽略并打印 WARNING

---

## 7. 参数覆写（`--override-params`）

基于内置模板，覆写同名参数，保留其他参数。使用双引号包裹。

```bash
# NVENC 模板默认 cq 18，覆写为 20
transcode_hw_main.exe --src "F:\素材" --encoder nvenc --override-params "-cq 20 -rc-lookahead 32" --skip

# QSV 模板默认 global_quality 21，覆写为 18
transcode_hw_main.exe --src "F:\素材" --encoder qsv --override-params "-global_quality 18" --skip
```

与 `--custom-params` 的区别：
- `--override-params`：基于模板微调，只改差异项
- `--custom-params`：完全替换整个命令（从 `-i` 之后）

---

## 8. 兼容性 Fallback

### 8.1 硬件解码不支持 → CPU 软解

当输入文件的位深/色度采样不被硬件解码器支持时，自动移除 `-hwaccel`，改用 CPU 软解后继续进行硬件编码。

```
WARNING: 10bit 4:2:2 not supported by nvenc; using CPU decode
```

### 8.2 输出格式降级（`--skip-check`）

启用 `--skip-check` 后，遇到不支持的输出格式时自动降级：

| 输入 | 降级目标 | 说明 |
|------|----------|------|
| 10bit | `p010le` (10bit 420) | 保留位深，牺牲色度采样 |
| 仍不支持 | `yuv420p` (8bit 420) | 最终降级 |

每次降级打印 WARNING。

---

## 9. CPU Fallback

硬件编码失败后，可选择用 CPU 重新编码。

**参数**：`libx265` / `libx264` `-preset slow` `-crf 18`，保持与硬件相同的音频策略。

**交互行为**：
- 非 `--skip` 模式：询问用户 `是否用 CPU 重新编码失败任务? (y/N)`
- `--skip` 模式：自动执行，打印 WARNING

**失败文件隔离**：最终仍失败的任务，源文件自动移入 `error/` 目录。

---

## 10. 输出文件说明

### 10.1 工作目录（`--work` 或 `<src>_work/`）

| 文件 | 说明 |
|------|------|
| `input_probe.csv` | 输入文件探测结果（位深、色度采样、creation_time 等） |
| `encoder_capabilities.csv` | 系统硬件编码器能力表 |
| `group_detail_YYYYMMDD_HHMMSS.txt` | 分组详情（人类可读） |
| `group_detail_YYYYMMDD_HHMMSS.csv` | 分组详情（机器可读） |
| `tasks_result.csv` | 每个任务的执行结果（返回码、耗时、备注） |
| `audio_verify.csv` | 音频时长验证结果 |
| `logs/*.log` | 每个任务的 FFmpeg 完整输出日志 |

### 10.2 输出目录（`--dst` 或 `<src>_comp/`）

转码后的视频文件，目录结构取决于 `--flat-output` 和分组策略。

### 10.3 终端打印限制

大批量素材时，终端只打印有限信息，完整明细写入外部文件：
- 分组摘要：最多 10 组，每组最多 5 个文件
- 完整列表：`group_detail_*.txt` / `*.csv`

---

## 11. 完整参数列表

```
--src PATH                    源文件或目录（必需）
--dst PATH                    输出目录（默认 <src>_comp）
--work PATH                   工作目录（默认 <src>_work）
--recursive                   递归遍历子目录
--timezone [-12..14]          时区偏移，默认 +8（BJT）

分组：
  --grp-auto                  自动按文件名前缀分组
  --grp-by-time 2h/4h/6h     按固定时间间隔分组
  --time-segments "HH:MM-HH:MM=label,..."  自定义时段分组
  --grp-regex "PATTERN"       按正则第一个捕获组分组
  --grp-prefix N              按文件名前 N 字符分组

硬件：
  --encoder {nvenc,qsv,amf}   默认编码器
  --hardware-pool "nvenc:2,qsv:1"  多硬件并发池
  --group-encoder "0:nvenc,1:qsv"  按组分配编码器
  --codec {hevc,h264}         视频编码格式
  --rc-mode {vbr,cbr,cqp,icq} 码控模式
  --cqp INT                   CQP/CQ 质量值
  --concurrency INT           全局并发上限

音频：
  --audio-codec CODEC         默认 copy，可选 aac/flac/opus/...
  --audio-bitrate RATE        如 320k/256k（copy 时忽略）

覆写：
  --override-params "STRING"  覆写模板参数，如 "-cq 20 -bf 3"

控制：
  --skip                      跳过所有交互确认
  --skip-check                跳过兼容性检查，启用输出格式降级
  --timeout SECONDS           单任务超时
  --show-groups-only          仅显示分组后退出
  --flat-output               平铺输出（不保留目录结构）
  --out-suffix SUFFIX         输出文件名后缀
  --extensions "ext1,ext2"    文件扩展名过滤，默认 mp4,mov
```

---

## 12. 编码器参数模板

### NVENC

```bash
-c:v hevc_nvenc -preset p7 -tune uhq -profile:v rext
-rc vbr -cq 18 -b:v 0
-spatial_aq 1 -aq-strength 8 -temporal_aq 1
-rc-lookahead 64 -lookahead_level auto
-bf 4 -b_ref_mode middle -multipass fullres
-g 240 -keyint_min 24
```

### QSV

```bash
-c:v hevc_qsv -preset veryslow -profile:v rext
-rc icq -global_quality 21
-look_ahead 1 -look_ahead_depth 100
-adaptive_i 1 -adaptive_b 1 -b_strategy 1
-bf 5 -refs 5 -rdo 1 -mbbrc 1 -extbrc 1
-low_power 0 -async_depth 7
-g 240 -keyint_min 24
```

### AMF（10bit 输入）

```bash
-c:v hevc_amf -preset quality -profile:v rext -pix_fmt p010le
-rc cqp -qp_i 18 -qp_p 18 -qp_b 18
-vbaq 1 -preanalysis 1 -pa_scene_change_detection 1
-bf 3 -max_num_reframes 4
-g 240 -keyint_min 24
```

### AMF（8bit 输入）

```bash
-c:v hevc_amf -preset quality -profile:v rext -pix_fmt yuv420p
-rc hqvbr -qvbr_quality_level 18 -b:v 0
-vbaq 1 -preanalysis 1 -pa_scene_change_detection 1
-bf 3 -max_num_reframes 4
-g 240 -keyint_min 24
```

### CPU Fallback

```bash
-c:v libx265 -preset slow -crf 18 -profile:v main10
# 或 -c:v libx264 -preset slow -crf 18 -profile:v high
```

---

## 13. 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 双击 exe 弹出提示后无法输入 | 被安全软件拦截 | 以管理员运行或添加白名单 |
| `ffmpeg-not-found` | FFmpeg 未在同级目录或 PATH | 确保 `ffmpeg/` 目录与 exe 同级 |
| 硬件编码失败 | 驱动/权限/格式不支持 | 查看 `.log`，脚本会自动 fallback CPU |
| 音频时长不匹配 | 容器封装问题或帧丢失 | 查看 `audio_verify.csv` |
| 分组结果不对 | 文件名无规律或 creation_time 缺失 | 使用 `--grp-regex` 或 `--grp-prefix` |
| 终端输出被截断 | 大批量素材的打印限制 | 查看 `group_detail_*.txt` |
| AMD 编码失败 | 作者无 AMD 显卡验证 | 欢迎反馈，可改用 `--encoder qsv/nvenc` |
| 打包失败 | Python 路径错误或 FFmpeg 未找到 | 修改 `build.py` 中 `TARGET_PYTHON` 变量 |

---

