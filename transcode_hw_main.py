#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcode_hw_main.py - 硬件转码工具 (nvenc/qsv/amf)

核心流程：
  1. ffprobe 探测输入（位深、色度采样、creation_time）
  2. 探测编码器能力
  3. 分组（auto / regex / time / prefix / res-fps）
  4. 按硬件池分配编码器，构建 ffmpeg 命令
  5. 并发执行，音频时长验证，失败 fallback/error
"""

import argparse, csv, json, re, shlex, shutil, subprocess, sys, threading, time, os
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict


# ---------- 打包后 FFmpeg 路径检测 ----------
def _setup_ffmpeg_path():
    """若程序被打包分发，优先使用同级 ffmpeg/ 目录下的 ffmpeg.exe/ffprobe.exe"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后的运行目录
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    ffmpeg_dir = base / "ffmpeg"
    ffmpeg_exe = ffmpeg_dir / "ffmpeg.exe"
    ffprobe_exe = ffmpeg_dir / "ffprobe.exe"
    if ffmpeg_exe.exists() and ffprobe_exe.exists():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")
        return True
    return False

_FFMPEG_BUNDLED = _setup_ffmpeg_path()

STOP_EVENT = threading.Event()
ACTIVE_PROCS = set()
ACTIVE_PROCS_LOCK = threading.Lock()

MAX_FILES_PER_GROUP_DISPLAY = 5
MAX_GROUPS_DISPLAY = 10

DEFAULT_SEGMENTS = [
    ("dawn", dt_time(5, 0), dt_time(8, 0)),
    ("morning", dt_time(8, 0), dt_time(12, 0)),
    ("afternoon", dt_time(12, 0), dt_time(18, 0)),
    ("evening", dt_time(18, 0), dt_time(22, 0)),
    ("night", dt_time(22, 0), dt_time(5, 0)),
]

ENCODER_TEMPLATES = {
    "nvenc": ["-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "uhq", "-profile:v", "rext",
              "-rc", "vbr", "-cq", "18", "-b:v", "0", "-spatial_aq", "1", "-aq-strength", "8",
              "-temporal_aq", "1", "-rc-lookahead", "64", "-lookahead_level", "auto", "-bf", "4",
              "-b_ref_mode", "middle", "-multipass", "fullres", "-g", "240", "-keyint_min", "24"],
    "qsv": ["-c:v", "hevc_qsv", "-preset", "veryslow", "-profile:v", "rext", "-rc", "icq",
            "-global_quality", "21", "-look_ahead", "1", "-look_ahead_depth", "100", "-adaptive_i", "1",
            "-adaptive_b", "1", "-b_strategy", "1", "-bf", "5", "-refs", "5", "-rdo", "1",
            "-mbbrc", "1", "-extbrc", "1", "-low_power", "0", "-async_depth", "7",
            "-g", "240", "-keyint_min", "24"],
    "amf_10bit": ["-c:v", "hevc_amf", "-preset", "quality", "-profile:v", "rext", "-pix_fmt", "p010le",
                  "-rc", "cqp", "-qp_i", "18", "-qp_p", "18", "-qp_b", "18", "-vbaq", "1",
                  "-preanalysis", "1", "-pa_scene_change_detection", "1", "-bf", "3", "-max_num_reframes", "4",
                  "-g", "240", "-keyint_min", "24"],
    "amf_8bit": ["-c:v", "hevc_amf", "-preset", "quality", "-profile:v", "rext", "-pix_fmt", "yuv420p",
                 "-rc", "hqvbr", "-qvbr_quality_level", "18", "-b:v", "0", "-vbaq", "1",
                 "-preanalysis", "1", "-pa_scene_change_detection", "1", "-bf", "3", "-max_num_reframes", "4",
                 "-g", "240", "-keyint_min", "24"],
}

CPU_FALLBACK_TEMPLATE = {
    "hevc": ["-c:v", "libx265", "-preset", "slow", "-crf", "18", "-profile:v", "main10"],
    "h264": ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high"],
}

PIX_FMT_CAPS = {
    "yuv420p": (8, "4:2:0"), "nv12": (8, "4:2:0"), "p010le": (10, "4:2:0"),
    "yuv420p10le": (10, "4:2:0"), "yuv422p": (8, "4:2:2"), "yuv422p10le": (10, "4:2:2"),
    "yuv444p": (8, "4:4:4"), "yuv444p10le": (10, "4:4:4"),
}


@dataclass
class FileInfo:
    path: Path
    filename: str
    creation_time_utc: str = ""
    creation_time_local: str = ""


@dataclass
class Group:
    prefix: str
    files: list
    time_range: str = ""


# ---------- 基础工具 ----------
def run_cmd(cmd):
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = p.communicate()
        return p.returncode, out.decode(errors="replace")
    except FileNotFoundError:
        return 127, ""


def write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])


def append_csv(path, headers, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(headers)
        w.writerow([row.get(h, "") for h in headers])


# ---------- 探测 ----------
def probe_media(path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,codec_name,avg_frame_rate,duration,bit_rate",
           "-of", "json", str(path)]
    rc, out = run_cmd(cmd)
    if rc != 0:
        return None
    try:
        s = json.loads(out)["streams"][0]
        return {
            "width": s.get("width"), "height": s.get("height"),
            "codec": s.get("codec_name"), "fps": _parse_fps(s.get("avg_frame_rate", "0/0")),
            "duration": s.get("duration"), "bitrate": s.get("bit_rate"),
        }
    except Exception:
        return None


def _parse_fps(s):
    if not s:
        return 0.0
    if "/" in s:
        a, b = s.split("/")
        try:
            return float(a) / float(b) if float(b) != 0 else 0.0
        except:
            return 0.0
    try:
        return float(s)
    except:
        return 0.0


def _ffprobe_full(path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=pix_fmt,bits_per_raw_sample:format_tags=creation_time",
           "-of", "json", str(path)]
    rc, out = run_cmd(cmd)
    if rc != 0:
        return {}
    try:
        return json.loads(out)
    except Exception:
        return {}


def parse_creation_time(raw, tz_offset=8):
    if not raw:
        return "", ""
    try:
        normalized = raw.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(normalized).replace(tzinfo=None)
        dt_local = dt_utc + timedelta(hours=tz_offset)
        return dt_utc.isoformat(), dt_local.isoformat()
    except Exception:
        return "", ""


def probe_input(path, tz_offset=8):
    data = _ffprobe_full(path)
    st = (data.get("streams") or [{}])[0]
    pix = str(st.get("pix_fmt") or "")
    raw_bits = str(st.get("bits_per_raw_sample") or "")
    bit_depth, chroma = PIX_FMT_CAPS.get(pix, (None, "unknown"))
    if not bit_depth:
        if raw_bits.isdigit():
            bit_depth = int(raw_bits)
        elif "10" in pix or "p010" in pix:
            bit_depth = 10
        elif pix:
            bit_depth = 8
    utc, local = parse_creation_time(
        (((data.get("format") or {}).get("tags")) or {}).get("creation_time", ""),
        tz_offset,
    )
    return {
        "file_path": str(path), "bit_depth": f"{bit_depth}bit" if bit_depth else "unknown",
        "chroma_subsampling": chroma, "creation_time_utc": utc,
        "creation_time_local": local, "timezone_offset": f"UTC{tz_offset:+d}",
        "pixel_format": pix, "probe_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def probe_encoders():
    rc, out = run_cmd(["ffmpeg", "-hide_banner", "-encoders"])
    rows = []
    for enc in ["hevc_nvenc", "hevc_qsv", "hevc_amf"]:
        available = int(rc == 0 and enc in out)
        profiles, pix_fmts = [], []
        if available:
            _, help_text = run_cmd(["ffmpeg", "-hide_banner", "-h", f"encoder={enc}"])
            for line in help_text.splitlines():
                low = line.lower()
                if "supported pixel formats:" in low:
                    pix_fmts = line.split(":", 1)[1].strip().split()
                elif "profile" in low and any(x in low for x in ["main", "rext", "main10"]):
                    parts = line.strip().split()
                    if parts:
                        profiles.append(parts[0])
        rows.append({
            "encoder_name": enc, "available": available,
            "supported_profiles": ";".join(sorted(set(profiles))),
            "supported_pixel_formats": ";".join(pix_fmts),
            "probe_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    return rows


def _cap_supports(cap, bit_depth, chroma):
    fmts = str(cap.get("supported_pixel_formats") or "").split(";")
    for fmt in fmts:
        bd, cs = PIX_FMT_CAPS.get(fmt, (None, None))
        if bd == bit_depth and cs == chroma:
            return True
    return not fmts


def _format_bit_depth(s):
    try:
        return int(str(s).replace("bit", ""))
    except Exception:
        return None


# ---------- 音频探测 ----------
def probe_audio_duration(path):
    """返回音频流 duration（秒），无音频返回 None"""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    rc, out = run_cmd(cmd)
    if rc != 0 or not out:
        return None
    try:
        return float(out.strip().splitlines()[0])
    except Exception:
        return None


# ---------- 分组 ----------
def collect_files(src, recursive, exts):
    files = []
    if src.is_file():
        return [src]
    for root, _, filenames in __import__("os").walk(src):
        for fn in filenames:
            if any(fn.lower().endswith(e.lower()) for e in exts):
                files.append(Path(root) / fn)
        if not recursive:
            break
    return files


def group_by_res_fps(files):
    entries = []
    for f in files:
        info = probe_media(f)
        entries.append({
            "path": str(f), "width": info["width"] if info else None,
            "height": info["height"] if info else None, "fps": info["fps"] if info else 0.0,
            "codec": info["codec"] if info else "", "duration": info["duration"] if info else "",
            "bitrate": info["bitrate"] if info else "",
        })

    def short_side(k):
        w, h, _ = k
        return min(w, h) if w and h else -1

    groups_map = defaultdict(list)
    for e in entries:
        groups_map[(e["width"], e["height"], e["fps"])].append(e)
    ordered = sorted(groups_map.keys(), key=short_side, reverse=True)
    groups = []
    for idx, k in enumerate(ordered):
        w, h, fps = k
        groups.append({"group_id": idx, "width": w, "height": h, "fps": fps, "files": groups_map[k]})
    return entries, groups


def _parse_local_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _to_file_infos(files, probe_map):
    return [
        FileInfo(
            path=Path(f), filename=Path(f).name,
            creation_time_utc=probe_map.get(str(Path(f)), {}).get("creation_time_utc", ""),
            creation_time_local=probe_map.get(str(Path(f)), {}).get("creation_time_local", ""),
        )
        for f in files
    ]


def _groups_to_dicts(groups):
    return [
        {"group_id": i, "prefix": g.prefix, "time_range": g.time_range,
         "files": [{"path": str(f.path)} for f in g.files]}
        for i, g in enumerate(groups)
    ]


def _parse_interval_hours(value):
    m = re.fullmatch(r"(\d{1,2})h", str(value or "").strip().lower())
    if not m:
        raise ValueError("--grp-by-time must be like 2h/4h/6h")
    h = int(m.group(1))
    if h <= 0 or h > 24:
        raise ValueError("interval must be 1-24 hours")
    return h


def group_by_time_interval(files, interval_hours):
    groups, ungrouped = {}, []
    for f in files:
        dt = _parse_local_dt(f.creation_time_local)
        if not dt:
            ungrouped.append(f)
            continue
        date_str = dt.strftime("%Y%m%d")
        start = (dt.hour // interval_hours) * interval_hours
        end = start + interval_hours
        prefix = f"{date_str}_{start:02d}00-{end:02d}00"
        groups.setdefault(prefix, []).append(f)
    result = [Group(prefix=p, files=fs, time_range=p.split("_", 1)[-1]) for p, fs in sorted(groups.items())]
    if ungrouped:
        result.append(Group(prefix="ungrouped", files=ungrouped, time_range="unknown"))
    return result


def parse_time_segments(s):
    if not s:
        return DEFAULT_SEGMENTS
    segments = []
    for part in s.split(","):
        time_range, label = part.strip().split("=", 1)
        start_str, end_str = time_range.split("-", 1)
        start = datetime.strptime(start_str.strip(), "%H:%M").time()
        end = datetime.strptime(end_str.strip(), "%H:%M").time()
        segments.append((label.strip(), start, end))
    return segments


def match_time_segment(dt, segments):
    t = dt.time()
    for label, start, end in segments:
        if start < end:
            if start <= t < end:
                return label
        else:
            if t >= start or t < end:
                return label
    return None


def group_by_time_segments(files, segments):
    groups, ungrouped = {}, []
    label_ranges = {label: f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for label, start, end in segments}
    for f in files:
        dt = _parse_local_dt(f.creation_time_local)
        if not dt:
            ungrouped.append(f)
            continue
        label = match_time_segment(dt, segments)
        if not label:
            ungrouped.append(f)
            continue
        prefix = f"{dt.strftime('%Y%m%d')}_{label}"
        groups.setdefault(prefix, []).append(f)
    result = []
    for p, fs in sorted(groups.items()):
        label = p.split("_", 1)[-1]
        result.append(Group(prefix=p, files=fs, time_range=label_ranges.get(label, "")))
    if ungrouped:
        result.append(Group(prefix="ungrouped", files=ungrouped, time_range="unknown"))
    return result


def group_by_regex(files, pattern):
    regex = re.compile(pattern)
    groups, ungrouped = {}, []
    for f in files:
        m = regex.match(f.filename)
        if m:
            groups.setdefault(m.group(1), []).append(f)
        else:
            ungrouped.append(f)
    result = [Group(prefix=p, files=fs) for p, fs in sorted(groups.items())]
    if ungrouped:
        result.append(Group(prefix="ungrouped", files=ungrouped))
    return result


def group_by_auto(files):
    """自动识别：日期(YYYYMMDD/YYMMDD/YYYY-MM-DD) > 字母前缀 > 数字前缀"""
    date_patterns = [
        (r"^(\d{4}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))", "YYYYMMDD"),
        (r"^(\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))", "YYMMDD"),
        (r"^(\d{4}-\d{2}-\d{2})", "YYYY-MM-DD"),
    ]
    letter_re = re.compile(r"^([A-Za-z]{2,})")
    digit_re = re.compile(r"^(\d{2,})")
    groups, ungrouped = {}, []
    for f in files:
        matched = False
        for pattern, _ in date_patterns:
            m = re.match(pattern, f.filename)
            if m:
                groups.setdefault(m.group(1), []).append(f)
                matched = True
                break
        if matched:
            continue
        m = letter_re.match(f.filename)
        if m:
            groups.setdefault(m.group(1), []).append(f)
            continue
        m = digit_re.match(f.filename)
        if m:
            groups.setdefault(m.group(1), []).append(f)
            continue
        ungrouped.append(f)
    result = [Group(prefix=p, files=fs) for p, fs in sorted(groups.items())]
    if ungrouped:
        result.append(Group(prefix="ungrouped", files=ungrouped))
    return result


def group_by_prefix(files, n):
    groups = defaultdict(list)
    for f in files:
        groups[f.filename[:n]].append(f)
    return [Group(prefix=p, files=fs) for p, fs in sorted(groups.items())]


def resolve_strategy(args):
    if args.grp_auto:
        return "auto"
    if args.grp_regex:
        return "regex"
    if args.time_segments:
        return "time_segments"
    if args.grp_by_time:
        return "time_interval"
    if args.grp_prefix:
        return "prefix"
    return None


def _write_group_txt(groups, path, tz_offset):
    lines = [
        "分组详情", f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"时区: UTC{tz_offset:+d}", f"总计: {len(groups)} 组, {sum(len(g.files) for g in groups)} 文件", "",
    ]
    for i, g in enumerate(groups, 1):
        lines += [f"[组 {i}/{len(groups)}] {g.prefix}", f"时段: {g.time_range or '-'}", f"文件数: {len(g.files)}"]
        for j, f in enumerate(g.files, 1):
            lines.append(f"  {j}. {f.path} | UTC: {f.creation_time_utc or 'unknown'}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_group_csv(groups, path):
    rows = []
    for i, g in enumerate(groups, 1):
        for j, f in enumerate(g.files, 1):
            rows.append({
                "group_index": i, "group_prefix": g.prefix, "time_range": g.time_range,
                "file_index": j, "file_path": str(f.path),
                "creation_time_utc": f.creation_time_utc, "creation_time_local": f.creation_time_local,
            })
    write_csv(path, ["group_index", "group_prefix", "time_range", "file_index", "file_path",
                     "creation_time_utc", "creation_time_local"], rows)


def print_group_summary(groups, tz_offset, output_dir=None):
    total = len(groups)
    files_total = sum(len(g.files) for g in groups)
    print(f"[分组结果] 时区: UTC{tz_offset:+d} | 共 {total} 组, {files_total} 文件")
    for g in groups[:MAX_GROUPS_DISPLAY]:
        shown = g.files[:MAX_FILES_PER_GROUP_DISPLAY]
        names = ", ".join(f.filename for f in shown)
        if len(g.files) > len(shown):
            names += f" ... 等 {len(g.files) - len(shown)} 个文件"
        print(f"  [{g.prefix}] {len(g.files)} 文件 | {names}")
    if total > MAX_GROUPS_DISPLAY:
        print(f"  ... 等 {total - MAX_GROUPS_DISPLAY} 组未显示")
    if output_dir:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        txt = Path(output_dir) / f"group_detail_{ts}.txt"
        csv = Path(output_dir) / f"group_detail_{ts}.csv"
        _write_group_txt(groups, txt, tz_offset)
        _write_group_csv(groups, csv)
        print(f"详细分组: {txt}, {csv}")


# ---------- 参数覆写 ----------
def _merge_override(template, override):
    """override 中的 -key 覆盖 template 中的同名 -key，保留其他参数。"""
    if not override:
        return template
    override_map = {}
    i = 0
    while i < len(override):
        key = override[i]
        if key.startswith("-"):
            if i + 1 < len(override) and not override[i + 1].startswith("-"):
                override_map[key] = override[i + 1]
                i += 2
            else:
                override_map[key] = None
                i += 1
        else:
            i += 1
    seen = set()
    new_cmd = []
    i = 0
    while i < len(template):
        key = template[i]
        if key in override_map:
            new_cmd.append(key)
            if override_map[key] is not None:
                new_cmd.append(override_map[key])
            seen.add(key)
            i += 1
            if i < len(template) and not template[i].startswith("-"):
                i += 1
            continue
        new_cmd.append(key)
        i += 1
    insert_at = len(new_cmd) - 1 if new_cmd else 0
    additions = []
    for key, val in override_map.items():
        if key in seen:
            continue
        additions.append(key)
        if val is not None:
            additions.append(val)
    return new_cmd[:insert_at] + additions + new_cmd[insert_at:]


# ---------- FFmpeg 命令构建 ----------
def _resolve_encoder(backend, codec):
    if backend == "nvenc":
        return "hevc_nvenc" if codec == "hevc" else "h264_nvenc"
    if backend == "qsv":
        return "hevc_qsv" if codec == "hevc" else "h264_qsv"
    if backend == "amf":
        return "hevc_amf" if codec == "hevc" else "h264_amf"
    return "libx265" if codec == "hevc" else "libx264"


def _probe_video_codec(path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    rc, out = run_cmd(cmd)
    return out.strip().splitlines()[0].strip().lower() if rc == 0 and out else None


def _hw_decode_args(encoder, src_codec):
    if encoder == "nvenc":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if encoder == "qsv":
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    return []


def build_ffmpeg_cmd(input_path, output_path, opts, override_tokens=None):
    src_codec = _probe_video_codec(input_path)
    encoder = opts.get("encoder")
    codec = opts.get("codec", "hevc")
    enc = _resolve_encoder(encoder, codec)

    # 解码
    decode_args = [] if opts.get("force_cpu_decode") else _hw_decode_args(encoder, src_codec)
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", *decode_args, "-i", str(input_path)]

    # 音频
    audio_codec = opts.get("audio_codec", "copy")
    if audio_codec == "copy":
        base += ["-map", "0:v:0", "-map", "0:a?", "-c:a", "copy"]
    else:
        bitrate = opts.get("audio_bitrate")
        base += ["-map", "0:v:0", "-map", "0:a?", "-c:a", audio_codec]
        if bitrate:
            base +=["-b:a", bitrate]

    # 视频编码
    if codec == "hevc" and encoder == "nvenc":
        base += ENCODER_TEMPLATES["nvenc"]
    elif codec == "hevc" and encoder == "qsv":
        base += ENCODER_TEMPLATES["qsv"]
    elif codec == "hevc" and encoder == "amf":
        in_depth = opts.get("input_bit_depth")
        base += ENCODER_TEMPLATES["amf_10bit" if in_depth == 10 else "amf_8bit"]
    else:
        base += ["-c:v", enc]
        if opts.get("rc_mode") == "cqp" and opts.get("cqp") is not None:
            base += ["-crf", str(opts["cqp"])]

    # 覆写
    if override_tokens:
        base = _merge_override(base, override_tokens)

    # fallback 像素格式
    fallback = opts.get("fallback_pix_fmt")
    if fallback and "-pix_fmt" not in base:
        base += ["-pix_fmt", fallback]

    base += [str(output_path)]
    return base


def build_cpu_fallback_cmd(input_path, output_path, opts, override_tokens=None):
    """CPU fallback：x264/x265 slow + 相同音频策略 + 相同覆写"""
    codec = opts.get("codec", "hevc")
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-i", str(input_path)]

    audio_codec = opts.get("audio_codec", "copy")
    if audio_codec == "copy":
        base += ["-map", "0:v:0", "-map", "0:a?", "-c:a", "copy"]
    else:
        bitrate = opts.get("audio_bitrate")
        base += ["-map", "0:v:0", "-map", "0:a?", "-c:a", audio_codec]
        if bitrate:
            base += ["-b:a", bitrate]

    base += CPU_FALLBACK_TEMPLATE.get(codec, CPU_FALLBACK_TEMPLATE["hevc"])

    if override_tokens:
        base = _merge_override(base, override_tokens)

    base += [str(output_path)]
    return base


# ---------- 执行 ----------
def _run_ffmpeg(cmd, log_path, timeout=None, label="", src_duration=None):
    if STOP_EVENT.is_set():
        return 130, "interrupted", 0.0
    start = time.time()
    p = None
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        log_path.write_text("ffmpeg not found\n", encoding="utf-8")
        return 127, "ffmpeg-not-found", 0.0
    assert p is not None
    with ACTIVE_PROCS_LOCK:
        ACTIVE_PROCS.add(p)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as f:
        try:
            while True:
                if STOP_EVENT.is_set():
                    p.kill()
                    return 130, "interrupted", round(time.time() - start, 1)
                chunk = p.stdout.read(4096) if p.stdout else b""
                if not chunk:
                    break
                f.write(chunk)
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            return -9, "timeout", round(time.time() - start, 1)
        finally:
            with ACTIVE_PROCS_LOCK:
                ACTIVE_PROCS.discard(p)
    return p.returncode, "", round(time.time() - start, 1)


def _terminate_all():
    with ACTIVE_PROCS_LOCK:
        procs = list(ACTIVE_PROCS)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(0.2)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


def execute_pool(tasks, max_workers, logs_root, timeout=None):
    """执行单个硬件池的任务，返回 (成功列表, 失败列表)"""
    STOP_EVENT.clear()
    succeeded, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_ffmpeg, t["cmd"], logs_root / f"{Path(t['src']).stem}.log",
                             timeout, Path(t["src"]).name, t.get("src_duration")): t
                   for t in tasks}
        try:
            for fut in as_completed(futures):
                t = futures[fut]
                rc, note, dur = fut.result()
                t["returncode"] = rc
                t["note"] = note
                t["secs"] = dur
                if rc == 0:
                    succeeded.append(t)
                else:
                    failed.append(t)
        except KeyboardInterrupt:
            STOP_EVENT.set()
            _terminate_all()
            ex.shutdown(wait=False, cancel_futures=True)
            print("Interrupted by user.")
            raise SystemExit(130)
    return succeeded, failed


def move_to_error(failed_tasks, src_root):
    """将失败任务的源文件移入 error/ 目录"""
    error_root = src_root.parent / "error" if src_root.is_file() else src_root / "error"
    error_root.mkdir(parents=True, exist_ok=True)
    moved, skipped = 0, 0
    for t in failed_tasks:
        srcp = Path(t["src"])
        if not srcp.exists():
            skipped += 1
            t["note"] = f"{t.get('note', '')}; source-not-found"
            continue
        try:
            rel = srcp.relative_to(src_root) if not src_root.is_file() else Path(srcp.name)
        except Exception:
            rel = Path(srcp.name)
        dstp = error_root / rel
        dstp.parent.mkdir(parents=True, exist_ok=True)
        if dstp.exists():
            dstp = dstp.with_name(f"{dstp.stem}_failed{dstp.suffix}")
        shutil.move(str(srcp), str(dstp))
        moved += 1
    return moved, skipped


# ---------- 主函数 ----------
def main():
    parser = argparse.ArgumentParser(
        description="硬件转码工具 (nvenc/qsv/amf)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--src", required=True, help="源文件或目录")
    parser.add_argument("--dst", help="输出目录（默认 <src>_comp）")
    parser.add_argument("--work", help="工作目录（CSV/logs，默认 <src>_work）")
    parser.add_argument("--recursive", action="store_true", help="递归遍历")
    parser.add_argument("--timezone", type=int, default=8, choices=range(-12, 15),
                        metavar="[-12..14]", help="时区偏移，默认 +8（BJT）")
    parser.add_argument("--grp-auto", action="store_true", help="自动按文件名前缀分组")
    parser.add_argument("--grp-by-time", default="", help="按时间间隔分组，如 2h/4h/6h")
    parser.add_argument("--time-segments", default="", help="自定义时段，如 08:00-12:00=morning")
    parser.add_argument("--grp-regex", default="", help="按正则第一个捕获组分组")
    parser.add_argument("--grp-prefix", type=int, default=None, help="按文件名前 N 字符分组")
    parser.add_argument("--extensions", default="mp4,mov", help="文件扩展名，逗号分隔")
    parser.add_argument("--encoder", choices=["nvenc", "qsv", "amf"], default="nvenc")
    parser.add_argument("--codec", choices=["hevc", "h264"], default="hevc")
    parser.add_argument("--rc-mode", choices=["vbr", "cbr", "cqp", "icq"], default=None)
    parser.add_argument("--cqp", type=int, default=None, help="CQP/CQ 值")
    parser.add_argument("--concurrency", type=int, default=1, help="全局并发上限")
    parser.add_argument("--hardware-pool", default="", help="多硬件并发池，如 nvenc:2,qsv:1")
    parser.add_argument("--group-encoder", default="", help="按 group ID 分配编码器，如 0:nvenc,1:qsv")
    parser.add_argument("--audio-codec", default="copy", help="音频编码器，默认 copy")
    parser.add_argument("--audio-bitrate", default="", help="音频比特率，如 320k/256k")
    parser.add_argument("--override-params", default="", help='覆写模板参数，如 "-cq 20 -bf 3"')
    parser.add_argument("--timeout", type=int, default=None, help="单任务超时（秒）")
    parser.add_argument("--skip", action="store_true", help="跳过所有交互确认")
    parser.add_argument("--skip-check", action="store_true", help="跳过兼容性检查，启用 fallback")
    parser.add_argument("--show-groups-only", action="store_true", help="仅显示分组后退出")
    parser.add_argument("--flat-output", action="store_true", help="平铺输出（不保留目录结构）")
    parser.add_argument("--out-suffix", default="", help="输出文件名后缀")
    args = parser.parse_args()

    src = Path(args.src).expanduser().resolve()
    work_root = Path(args.work).expanduser().resolve() if args.work else src.parent / f"{src.name}_work"
    work_root.mkdir(parents=True, exist_ok=True)
    logs_root = work_root / "logs"
    logs_root.mkdir(parents=True, exist_ok=True)

    dst_root = Path(args.dst).expanduser().resolve() if args.dst else src.parent / f"{src.name}_comp"
    dst_root.mkdir(parents=True, exist_ok=True)

    # 解析音频参数
    audio_codec = args.audio_codec
    audio_bitrate = args.audio_bitrate
    if audio_codec == "copy" and audio_bitrate:
        print("WARNING: --audio-bitrate ignored when --audio-codec is copy")
        audio_bitrate = ""
    if audio_codec == "aac" and not audio_bitrate:
        audio_bitrate = "320k"  # AAC 默认 320k

    # 解析覆写参数
    override_tokens = shlex.split(args.override_params) if args.override_params else None

    # 解析硬件池
    hardware_pool = {"nvenc": 1, "qsv": 1, "amf": 1}
    if args.hardware_pool:
        for part in args.hardware_pool.split(","):
            enc, num = part.strip().split(":", 1)
            hardware_pool[enc.strip().lower()] = int(num.strip())

    # 解析 group-encoder 映射
    group_encoder_map = {}
    if args.group_encoder:
        for part in args.group_encoder.split(","):
            gid, enc = part.strip().split(":", 1)
            group_encoder_map[int(gid.strip())] = enc.strip().lower()

    # 收集文件
    exts = [f".{e.strip().lstrip('.').lower()}" for e in args.extensions.split(",") if e.strip()]
    files = [src] if src.is_file() else collect_files(src, args.recursive, exts)
    if not files:
        print("No files found.")
        sys.exit(0)

    # 探测
    print(f"Probing {len(files)} files...")
    probe_rows = [probe_input(Path(f), args.timezone) for f in files]
    probe_map = {r["file_path"]: r for r in probe_rows}
    cap_rows = probe_encoders()
    cap_map = {r["encoder_name"]: r for r in cap_rows}

    write_csv(work_root / "input_probe.csv",
              ["file_path", "bit_depth", "chroma_subsampling", "creation_time_utc",
               "creation_time_local", "timezone_offset", "pixel_format", "probe_time"],
              probe_rows)
    write_csv(work_root / "encoder_capabilities.csv",
              ["encoder_name", "available", "supported_profiles", "supported_pixel_formats", "probe_time"],
              cap_rows)

    # 分组
    strategy = resolve_strategy(args)
    entries, groups = group_by_res_fps(files)
    if strategy:
        infos = _to_file_infos(files, probe_map)
        detailed = None
        try:
            if strategy == "auto":
                detailed = group_by_auto(infos)
            elif strategy == "regex":
                detailed = group_by_regex(infos, args.grp_regex)
            elif strategy == "time_segments":
                detailed = group_by_time_segments(infos, parse_time_segments(args.time_segments))
            elif strategy == "time_interval":
                detailed = group_by_time_interval(infos, _parse_interval_hours(args.grp_by_time))
            elif strategy == "prefix":
                detailed = group_by_prefix(infos, args.grp_prefix)
        except Exception as e:
            print(f"Grouping error: {e}")
            sys.exit(2)
        if detailed is None:
            print("Grouping error: no strategy matched")
            sys.exit(2)
        groups = _groups_to_dicts(detailed)
        print_group_summary(detailed, args.timezone, output_dir=work_root)
    else:
        groups = [{"group_id": 0, "files": [{"path": str(f)} for f in files]}]

    if args.show_groups_only:
        for g in groups:
            print(f"  Group {g['group_id']} ({g.get('prefix', g['group_id'])}): {len(g['files'])} files")
        sys.exit(0)

    # 构建任务
    out_suffix = args.out_suffix
    if out_suffix and not out_suffix.startswith("_"):
        out_suffix = "_" + out_suffix

    all_tasks = []
    for g in groups:
        gid = g["group_id"]
        encoder = group_encoder_map.get(gid, args.encoder)
        cfg = {
            "encoder": encoder, "codec": args.codec,
            "rc_mode": args.rc_mode or "cqp", "cqp": args.cqp or 18,
            "input_bit_depth": _format_bit_depth(probe_map.get(str(Path(g["files"][0]["path"])), {}).get("bit_depth")),
            "audio_codec": audio_codec, "audio_bitrate": audio_bitrate,
        }
        for f in g["files"]:
            srcp = Path(f["path"])
            if src.is_file():
                outp = dst_root / f"{srcp.stem}{out_suffix}{srcp.suffix}"
            else:
                if args.flat_output:
                    outp = dst_root / f"{srcp.stem}{out_suffix}{srcp.suffix}"
                else:
                    rel = srcp.relative_to(src)
                    outp = dst_root / rel.with_name(f"{rel.stem}{out_suffix}{rel.suffix}")
            outp.parent.mkdir(parents=True, exist_ok=True)

            # 兼容性检查
            cap = cap_map.get(_resolve_encoder(encoder, args.codec), {})
            bd = cfg["input_bit_depth"]
            chroma = probe_map.get(str(srcp), {}).get("chroma_subsampling")
            if cap and not _cap_supports(cap, bd, chroma):
                print(f"WARNING: {srcp.name} {bd}bit {chroma} not supported by {encoder}; using CPU decode")
                cfg["force_cpu_decode"] = True
            if args.skip_check and bd == 10:
                cfg["fallback_pix_fmt"] = "p010le"
                print(f"WARNING: fallback to 10bit 420 for {srcp.name}")
            elif args.skip_check:
                cfg["fallback_pix_fmt"] = "yuv420p"
                print(f"WARNING: fallback to 8bit 420 for {srcp.name}")

            cmd = build_ffmpeg_cmd(srcp, outp, cfg, override_tokens=override_tokens)
            all_tasks.append({
                "src": str(srcp), "dst": str(outp), "group": gid,
                "encoder": encoder, "codec": args.codec, "cmd": cmd,
                "src_duration": _parse_fps(probe_map.get(str(srcp), {}).get("duration", "0")),
                "cfg": dict(cfg),  # 保留 cfg 用于 fallback
            })

    # 按编码器分组任务
    encoder_tasks = defaultdict(list)
    for t in all_tasks:
        encoder_tasks[t["encoder"]].append(t)

    # 打印命令
    print(f"\nCommands to execute: {len(all_tasks)} tasks")
    for i, t in enumerate(all_tasks, 1):
        print(f"[{i}] {Path(t['src']).name} -> {Path(t['dst']).name} [{t['encoder']}]")
        print("   " + " ".join(shlex.quote(x) for x in t["cmd"]))

    # 确认
    if args.skip:
        print("[--skip] 直接执行")
    else:
        c = input("Confirm? (y/N): ").strip().lower()
        if c != "y":
            print("Aborted")
            sys.exit(0)

    # 按硬件池执行
    result_csv = work_root / "tasks_result.csv"
    result_headers = ["src", "dst", "group", "encoder", "codec", "returncode", "note", "secs"]
    if result_csv.exists():
        result_csv.unlink()

    all_succeeded, all_failed = [], []

    for enc, tasks in encoder_tasks.items():
        pool_size = hardware_pool.get(enc, 1)
        # 全局并发限制：若剩余并发不足，取最小值
        remaining = args.concurrency - len(ACTIVE_PROCS)
        workers = min(pool_size, remaining, len(tasks))
        if workers <= 0:
            workers = 1
        print(f"\n[{enc}] 执行 {len(tasks)} 任务，并发 {workers}")
        succeeded, failed = execute_pool(tasks, workers, logs_root, timeout=args.timeout)
        all_succeeded.extend(succeeded)
        all_failed.extend(failed)
        for t in succeeded + failed:
            append_csv(result_csv, result_headers, {
                "src": t["src"], "dst": t["dst"], "group": t.get("group", ""),
                "encoder": t.get("encoder", ""), "codec": t.get("codec", ""),
                "returncode": t.get("returncode", ""), "note": t.get("note", ""), "secs": t.get("secs", ""),
            })

    # 音频时长验证（仅对成功任务）
    audio_verify_csv = work_root / "audio_verify.csv"
    audio_headers = ["src", "dst", "audio_match", "duration_diff", "note"]
    for t in all_succeeded:
        src_dur = probe_audio_duration(Path(t["src"]))
        dst_dur = probe_audio_duration(Path(t["dst"]))
        if src_dur is None and dst_dur is None:
            match, diff, note = 1, 0.0, "no-audio"
        elif src_dur is None or dst_dur is None:
            match, diff, note = 0, 0.0, "audio-stream-mismatch"
        else:
            diff = abs(src_dur - dst_dur)
            match = 1 if diff < 1.0 else 0
            note = f"diff={diff:.3f}s"
        append_csv(audio_verify_csv, audio_headers, {
            "src": t["src"], "dst": t["dst"], "audio_match": match,
            "duration_diff": round(diff, 3), "note": note,
        })
        if not match:
            print(f"WARNING: audio duration mismatch {t['src']} -> {t['dst']} ({note})")

    # CPU Fallback（对硬件失败任务）
    hw_failed = [t for t in all_failed if t.get("encoder") in {"nvenc", "qsv", "amf"}]
    if hw_failed:
        print(f"\n{len(hw_failed)} 个硬件编码任务失败")
        if args.skip:
            print("[--skip] 自动使用 CPU fallback (x265 slow)")
            do_fallback = True
        else:
            c = input("是否用 CPU (libx265 -preset slow) 重新编码失败任务? (y/N): ").strip().lower()
            do_fallback = (c == "y")

        if do_fallback:
            fallback_tasks = []
            for t in hw_failed:
                srcp = Path(t["src"])
                dstp = Path(t["dst"])
                fb_cfg = dict(t["cfg"])
                fb_cmd = build_cpu_fallback_cmd(srcp, dstp, fb_cfg, override_tokens=override_tokens)
                fallback_tasks.append({
                    "src": str(srcp), "dst": str(dstp), "group": t["group"],
                    "encoder": "cpu", "codec": t["codec"], "cmd": fb_cmd,
                    "src_duration": t.get("src_duration"),
                })
            print(f"\n[CPU fallback] 执行 {len(fallback_tasks)} 任务")
            fb_succeeded, fb_failed = execute_pool(fallback_tasks, args.concurrency, logs_root, timeout=args.timeout)
            for t in fb_succeeded + fb_failed:
                t["note"] = f"fallback-cpu-ok" if t.get("returncode") == 0 else f"fallback-cpu-failed: {t.get('note', '')}"
                append_csv(result_csv, result_headers, {
                    "src": t["src"], "dst": t["dst"], "group": t.get("group", ""),
                    "encoder": t.get("encoder", ""), "codec": t.get("codec", ""),
                    "returncode": t.get("returncode", ""), "note": t.get("note", ""), "secs": t.get("secs", ""),
                })
            # 合并到最终列表
            all_succeeded.extend(fb_succeeded)
            all_failed = [t for t in all_failed if t not in hw_failed]  # 移除已 fallback 的
            all_failed.extend(fb_failed)
        else:
            for t in hw_failed:
                t["note"] = f"hw-failed-no-fallback: {t.get('note', '')}"

    # 移动最终失败文件到 error/
    if all_failed:
        moved, skipped = move_to_error(all_failed, src)
        print(f"\nMoved {moved} failed sources to error/ (skipped {skipped})")

    # 汇总
    print(f"\nDone: {len(all_succeeded)} succeeded, {len(all_failed)} failed")
    print(f"Results: {result_csv}")
    if all_failed:
        for t in all_failed:
            print(f"  FAIL: {t['src']} -> {t.get('note', '')}")


if __name__ == "__main__":
    # 统一双击/无参数检测，跨平台保持窗口
    if len(sys.argv) <= 1:
        print("=" * 60)
        print(" transcode_hw_main - 硬件转码工具")
        print("=" * 60)
        print("\n请使用命令行运行本程序，或在命令行中输入 --help 查看完整帮助。")
        print("\n常用参数：")
        print("  --src PATH       源文件或目录（必需）")
        print("  --dst PATH       输出目录（默认 <src>_comp）")
        print("  --work PATH      工作目录（默认 <src>_work）")
        print("  --skip           跳过所有交互确认")
        print("  --grp-auto       自动按文件名前缀分组")
        print("  --encoder TYPE   nvenc/qsv/amf（默认 nvenc）")
        print("\n完整帮助：")
        print("  transcode_hw_main.exe --help")
        print("\n" + "-" * 60)
        print(" 窗口将保持打开，请直接输入命令运行...")
        print("-" * 60)
        
        if sys.platform == "win32":
            os.system("cmd /k")
        else:
            input("按 Enter 键退出...")
        sys.exit(0)
    
    main()