#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcode_hw_main.py - 硬件转码工具 (nvenc/qsv/amf)

主要特性摘要：
 - 支持单文件或目录输入（--src 可为文件或目录）。
 - 支持 --query-params 单独查询 ffmpeg encoder 帮助 (可单独运行)。
 - 默认交互；--skip 可跳过交互（并可细化跳过哪些项）。
 - 支持 --flat-output、--out-suffix 控制输出目录结构和文件名后缀。
 - pre/post media info CSV，per-task log，实时进度输出。
"""

import argparse, csv, json, shlex, shutil, subprocess, sys, threading, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

STOP_EVENT = threading.Event()
ACTIVE_PROCS_LOCK = threading.Lock()
ACTIVE_PROCS = set()
_FFMPEG_FILTERS_CACHE = None

# AMF note: 作者无 AMD 显卡进行实际验证，AMF 参数仅通过查阅 FFmpeg 文档及 AMD 官方资料整理，实际运行可能存在兼容性问题，欢迎 AMD 用户反馈。
ENCODER_PARAM_TEMPLATES = {
    "nvenc": ["-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "uhq", "-profile:v", "rext",
              "-rc", "vbr", "-cq", "18", "-b:v", "0", "-spatial_aq", "1", "-aq-strength", "8",
              "-temporal_aq", "1", "-rc-lookahead", "64", "-lookahead_level", "auto", "-bf", "4",
              "-b_ref_mode", "middle", "-multipass", "fullres", "-g", "240", "-keyint_min", "24"],
    "qsv": ["-c:v", "hevc_qsv", "-preset", "veryslow", "-profile:v", "rext", "-rc", "icq",
            "-global_quality", "21", "-look_ahead", "1", "-look_ahead_depth", "100", "-adaptive_i", "1",
            "-adaptive_b", "1", "-b_strategy", "1", "-bf", "5", "-refs", "5", "-rdo", "1",
            "-mbbrc", "1", "-extbrc", "1", "-low_power", "0", "-async_depth", "7", "-g", "240", "-keyint_min", "24"],
    "amf_10bit": ["-c:v", "hevc_amf", "-preset", "quality", "-profile:v", "rext", "-pix_fmt", "p010le",
                  "-rc", "cqp", "-qp_i", "18", "-qp_p", "18", "-qp_b", "18", "-vbaq", "1",
                  "-preanalysis", "1", "-pa_scene_change_detection", "1", "-bf", "3", "-max_num_reframes", "4",
                  "-g", "240", "-keyint_min", "24"],
    "amf_8bit": ["-c:v", "hevc_amf", "-preset", "quality", "-profile:v", "rext", "-pix_fmt", "yuv420p",
                 "-rc", "hqvbr", "-qvbr_quality_level", "18", "-b:v", "0", "-vbaq", "1",
                 "-preanalysis", "1", "-pa_scene_change_detection", "1", "-bf", "3", "-max_num_reframes", "4",
                 "-g", "240", "-keyint_min", "24"],
}

PIX_FMT_CAPS = {
    "yuv420p": (8, "4:2:0"), "nv12": (8, "4:2:0"), "p010le": (10, "4:2:0"),
    "yuv420p10le": (10, "4:2:0"), "yuv422p": (8, "4:2:2"), "yuv422p10le": (10, "4:2:2"),
    "yuv444p": (8, "4:4:4"), "yuv444p10le": (10, "4:4:4"),
}

DEFAULT_FORCED_QUALITY_POLICY = {
    "nvenc": {"preset": "p7"},
    "qsv": {"tu": "1"},
    "amf": {"quality": "quality"},
}
FORCED_QUALITY_POLICY = json.loads(json.dumps(DEFAULT_FORCED_QUALITY_POLICY))
# ---------------- helpers ----------------
def run_cmd_capture(cmd):
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        return 127, "cmd-not-found"
    out = b""
    try:
        for chunk in p.stdout:
            out += chunk
        p.wait()
    except Exception:
        try: p.kill()
        except: pass
        return -1, "error"
    return p.returncode, out.decode(errors="replace")

def probe_media(path: Path):
    cmd = ["ffprobe","-v","error","-select_streams","v:0","-show_entries",
           "stream=width,height,codec_name,avg_frame_rate,duration,bit_rate",
           "-of","default=noprint_wrappers=1:nokey=1", str(path)]
    rc, out = run_cmd_capture(cmd)
    if rc != 0 or not out:
        # try json fallback
        cmd2 = ["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream","-of","json",str(path)]
        rc2, out2 = run_cmd_capture(cmd2)
        if rc2 != 0:
            return None
        try:
            j = json.loads(out2)
            s = j.get("streams",[{}])[0]
            width = s.get("width"); height = s.get("height")
            codec = s.get("codec_name")
            avg = s.get("avg_frame_rate","0/0")
            fps = _parse_fps(avg)
            duration = s.get("duration"); bitrate = s.get("bit_rate")
            return {"width": width, "height": height, "codec": codec, "fps": round(fps,3), "duration": duration, "bitrate": bitrate}
        except Exception:
            return None
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    try:
        width = int(lines[0]) if lines and lines[0].isdigit() else None
        height = int(lines[1]) if len(lines)>1 and lines[1].isdigit() else None
        codec = lines[2] if len(lines)>2 else ""
        fps = _parse_fps(lines[3]) if len(lines)>3 else 0.0
        duration = lines[4] if len(lines)>4 else ""
        bitrate = lines[5] if len(lines)>5 else ""
        return {"width": width, "height": height, "codec": codec, "fps": round(fps,3), "duration": duration, "bitrate": bitrate}
    except Exception:
        return None


def probe_input_format(path: Path):
    cmd = ["ffprobe","-v","error","-select_streams","v:0","-show_entries",
           "stream=pix_fmt,bits_per_raw_sample", "-of", "json", str(path)]
    rc, out = run_cmd_capture(cmd)
    pix_fmt = ""; raw_bits = ""
    if rc == 0 and out:
        try:
            st = (json.loads(out).get("streams") or [{}])[0]
            pix_fmt = str(st.get("pix_fmt") or "")
            raw_bits = str(st.get("bits_per_raw_sample") or "")
        except Exception:
            pass
    bit_depth, chroma = PIX_FMT_CAPS.get(pix_fmt, (None, "unknown"))
    if not bit_depth:
        if raw_bits.isdigit(): bit_depth = int(raw_bits)
        elif "10" in pix_fmt or "p010" in pix_fmt: bit_depth = 10
        elif pix_fmt: bit_depth = 8
    return {"file_path": str(path), "bit_depth": f"{bit_depth}bit" if bit_depth else "unknown",
            "chroma_subsampling": chroma, "pixel_format": pix_fmt, "probe_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

def probe_encoder_capabilities():
    rc, encoders = run_cmd_capture(["ffmpeg", "-hide_banner", "-encoders"])
    rows = []
    for enc in ["hevc_nvenc", "hevc_qsv", "hevc_amf"]:
        available = int(rc == 0 and enc in encoders)
        profiles, pix_fmts = [], []
        if available:
            _, help_text = run_cmd_capture(["ffmpeg", "-hide_banner", "-h", f"encoder={enc}"])
            for line in help_text.splitlines():
                low = line.lower()
                if "supported pixel formats:" in low:
                    pix_fmts = line.split(":", 1)[1].strip().split()
                elif "profile" in low and any(x in low for x in ["main", "rext", "main10"]):
                    parts = line.strip().split()
                    if parts: profiles.append(parts[0])
        rows.append({"encoder_name": enc, "available": available,
                     "supported_profiles": ";".join(sorted(set(profiles))),
                     "supported_pixel_formats": ";".join(pix_fmts),
                     "probe_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return rows

def _cap_supports(cap_row, bit_depth, chroma):
    fmts = str(cap_row.get("supported_pixel_formats") or "").split(";")
    for fmt in fmts:
        bd, cs = PIX_FMT_CAPS.get(fmt, (None, None))
        if bd == bit_depth and cs == chroma:
            return True
    return not fmts

def _format_bit_depth(s):
    try: return int(str(s).replace("bit", ""))
    except Exception: return None

def _print_task_commands(tasks):
    print("\nFFmpeg commands to be executed:")
    for i, t in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {t['src']} -> {t['dst']}")
        print("  " + " ".join(shlex.quote(x) for x in t["ffmpeg_cmd"]))
        if t.get("actual_output_format"):
            print(f"  output-format: {t['actual_output_format']}")

def _summarize_params(tasks):
    print("\nActual parameter summary:")
    for t in tasks:
        print(f"- {Path(t['dst']).name}: encoder={t.get('encoder')} codec={t.get('codec')} output_format={t.get('actual_output_format','default/auto')}")

def _parse_fps(s):
    if not s: return 0.0
    if "/" in s:
        a,b = s.split("/")
        try: return float(a)/float(b) if float(b) != 0 else 0.0
        except: return 0.0
    try: return float(s)
    except: return 0.0



def _safe_float(value):
    try:
        if value is None:
            return None
        txt = str(value).strip()
        if not txt or txt.upper() == "N/A":
            return None
        return float(txt)
    except Exception:
        return None

def write_csv(path: Path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline='', encoding='utf-8') as f:
        w = csv.writer(f); w.writerow(headers)
        for r in rows: w.writerow([r.get(h,"") for h in headers])

def append_csv(path: Path, headers, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if not exists: w.writerow(headers)
        w.writerow([row.get(h,"") for h in headers])

def human_bitrate(mbps):
    return f"{int(mbps)}M" if mbps is not None else None

def suggest_bitrate_range(width, height):
    """Suggest bitrate by long side to avoid portrait/rotated misclassification."""
    candidates = [x for x in (width, height) if isinstance(x, (int, float))]
    long_side = max(candidates) if candidates else None
    if long_side and long_side >= 3840:
        return (30, 50)
    if long_side and long_side >= 1920:
        return (10, 20)
    return (5, 10)

def suggest_cqp(width, height):
    """Suggest CQP/CRF by long side for quality-first encoding."""
    candidates = [x for x in (width, height) if isinstance(x, (int, float))]
    long_side = max(candidates) if candidates else None
    if long_side and long_side >= 3840:
        return 20
    if long_side and long_side >= 1920:
        return 22
    return 24

def suggest_maxrate(width, height):
    """Hard-capped maxrate by long side (safety valve for CQP/CRF)."""
    candidates = [x for x in (width, height) if isinstance(x, (int, float))]
    long_side = max(candidates) if candidates else None
    if long_side and long_side >= 3840:
        return 50
    if long_side and long_side >= 1920:
        return 30
    return 15
# ---------------- query encoder ----------------
def query_encoder(backend: str, work: Path):
    mapping = {"nvenc":["h264_nvenc","hevc_nvenc"], "qsv":["h264_qsv","hevc_qsv"], "amf":["h264_amf","hevc_amf"]}
    encs = mapping.get(backend.lower())
    if not encs:
        print("Unknown backend for query:", backend); return
    work.joinpath("logs").mkdir(parents=True, exist_ok=True)
    out_text = ""
    for enc in encs:
        cmd = ["ffmpeg","-h","encoder="+enc]
        print(f"--- ffmpeg -h encoder={enc} ---")
        rc, out = run_cmd_capture(cmd)
        print(out)
        out_text += f"=== encoder {enc} (rc={rc}) ===\n{out}\n\n"
    path = work.joinpath(f"logs/query-{backend}.txt")
    path.write_text(out_text, encoding='utf-8')
    print("Saved query to:", path)

# ---------------- file collect & grouping ----------------
def collect_inputs(src: Path, recursive: bool, exts, prefixes, suffixes, invert_prefix, invert_suffix):
    import os
    files = []
    if src.is_file():
        files = [src]
        return files
    for root, dirs, filenames in os.walk(src):
        for fn in filenames:
            if not any(fn.lower().endswith(e.lower()) for e in exts): continue
            stem = Path(fn).stem
            ok = True
            if prefixes:
                matched = any(stem.startswith(p) for p in prefixes)
                ok = (not matched) if invert_prefix else matched
            if ok and suffixes:
                matched2 = any(stem.endswith(s) for s in suffixes)
                ok = (not matched2) if invert_suffix else matched2
            if ok: files.append(Path(root)/fn)
        if not recursive: break
    return files

def group_by_res_fps(files):
    entries = []
    for f in files:
        info = probe_media(f) or {"width":None,"height":None,"codec":"","fps":0.0,"duration":"","bitrate":""}
        entries.append({"path":str(f),"width":info["width"],"height":info["height"],"fps":info["fps"],"codec":info["codec"],"duration":info["duration"],"bitrate":info["bitrate"]})
    groups = defaultdict(list)
    for e in entries:
        key = (e["width"], e["height"], e["fps"])
        groups[key].append(e)
    # sort by short side desc (None -> -1)
    def short_side(k):
        w,h,_ = k
        if not w or not h: return -1
        return min(w,h)
    ordered = sorted(groups.keys(), key=lambda k: short_side(k), reverse=True)
    group_list = []
    for idx,k in enumerate(ordered):
        w,h,fps = k
        group_list.append({"group_id":idx,"width":w,"height":h,"fps":fps,"files":groups[k]})
    return entries, group_list

# ---------------- ffmpeg cmd builder ----------------
def _resolve_video_encoder(encoder: str, codec: str):
    if encoder == "nvenc":
        return "hevc_nvenc" if codec == "hevc" else "h264_nvenc"
    if encoder == "qsv":
        return "hevc_qsv" if codec == "hevc" else "h264_qsv"
    if encoder == "amf":
        return "hevc_amf" if codec == "hevc" else "h264_amf"
    return "libx265" if codec == "hevc" else "libx264"


def _probe_video_codec_name(input_path: Path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path)
    ]
    rc, out = run_cmd_capture(cmd)
    if rc != 0 or not out:
        return None
    return out.strip().splitlines()[0].strip().lower()


def _probe_streams(input_path: Path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "stream=index,codec_type,codec_tag_string,codec_name,disposition:stream_tags", "-of", "json", str(input_path)
    ]
    rc, out = run_cmd_capture(cmd)
    if rc != 0 or not out:
        return []
    try:
        return (json.loads(out) or {}).get("streams", []) or []
    except Exception:
        return []


def _should_force_mov_output(input_path: Path):
    """If probe finds metadata/data-like side streams, force MOV output for better container compatibility."""
    streams = _probe_streams(input_path)
    if not streams:
        return False
    data_tags = {"tmcd", "gpmd", "camm", "mett", "metx", "rtmd"}
    for st in streams:
        ctype = (st.get("codec_type") or "").lower()
        if ctype in {"data", "attachment"}:
            return True
        disp = st.get("disposition") or {}
        if isinstance(disp, dict) and int(disp.get("attached_pic", 0) or 0) == 1:
            return True
        tag = (st.get("codec_tag_string") or "").strip().lower()
        if tag in data_tags:
            return True
        tags = st.get("tags") or {}
        handler = str(tags.get("handler_name", "")).lower()
        if any(k in handler for k in ["meta", "timecode"]):
            return True
    return False


def _resolve_hw_decode_args(encoder: str, src_codec: str):
    """Return decoder args before `-i` so decode path matches selected HW encoder."""
    if encoder == "nvenc":
        # NVENC 对应 NVDEC/CUVID 解码
        nvdec_map = {
            "h264": "h264_cuvid",
            "hevc": "hevc_cuvid",
            "mpeg2video": "mpeg2_cuvid",
            "vc1": "vc1_cuvid",
            "vp8": "vp8_cuvid",
            "vp9": "vp9_cuvid",
            "av1": "av1_cuvid",
        }
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if encoder == "qsv":
        qsv_map = {
            "h264": "h264_qsv",
            "hevc": "hevc_qsv",
            "mpeg2video": "mpeg2_qsv",
            "vc1": "vc1_qsv",
            "vp8": "vp8_qsv",
            "vp9": "vp9_qsv",
            "av1": "av1_qsv",
        }
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    if encoder == "amf":
        return []
    return []


def _get_ffmpeg_filters_text():
    global _FFMPEG_FILTERS_CACHE
    if _FFMPEG_FILTERS_CACHE is not None:
        return _FFMPEG_FILTERS_CACHE
    rc, out = run_cmd_capture(["ffmpeg", "-hide_banner", "-filters"])
    _FFMPEG_FILTERS_CACHE = out.lower() if rc == 0 and out else ""
    return _FFMPEG_FILTERS_CACHE


def _has_filter(filter_name: str):
    text = _get_ffmpeg_filters_text()
    return f" {filter_name.lower()} " in text


def _scale_requested(scale_val):
    return bool(scale_val and str(scale_val).strip().lower() != "same")


def _has_scaling_in_custom_params(custom_params: str):
    if not custom_params:
        return False
    low = custom_params.lower()
    return any(x in low for x in ["-vf", "-filter:v", "-filter_complex", "scale="])


def _resolve_hw_scale_filter(encoder: str, scale_val):
    if not _scale_requested(scale_val):
        return None
    if scale_val == "half":
        w, h = "iw/2", "ih/2"
    elif isinstance(scale_val, str) and "x" in scale_val:
        parts = scale_val.lower().split("x", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        w, h = parts[0], parts[1]
    else:
        return None

    if encoder == "nvenc":
        if _has_filter("scale_cuda"):
            return ["-vf", f"scale_cuda={w}:{h}"]
        if _has_filter("scale_npp"):
            return ["-vf", f"scale_npp={w}:{h}"]
    if encoder == "qsv" and _has_filter("scale_qsv"):
        return ["-vf", f"scale_qsv=w={w}:h={h}"]
    return None


def _normalize_sw_fallback_opts(opts: dict):
    """Map HW-centric opts to software-safe defaults for robust fallback."""
    sw = dict(opts)
    sw["encoder"] = "cpu"
    sw["preset"] = "medium"
    if sw.get("rc_mode") == "cbr":
        sw["rc_mode"] = "vbr"
    if sw.get("rc_mode") == "icq":
        sw["rc_mode"] = "cqp" if sw.get("cqp") is not None else "vbr"
    return sw


def _strip_conflicting_quality_tokens(tokens, encoder: str, manual_override=False):
    """Remove tokens that conflict with forced HW quality policy."""
    remove_keys = {"-preset", "-quality", "-tu"}
    if encoder not in {"nvenc", "qsv", "amf"} or manual_override:
        return tokens
    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in remove_keys:
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def _forced_quality_args(encoder: str, manual_override=False):
    if manual_override:
        return []
    if encoder == "nvenc":
        return ["-preset", FORCED_QUALITY_POLICY["nvenc"]["preset"]]
    if encoder == "qsv":
        return ["-tu", FORCED_QUALITY_POLICY["qsv"]["tu"]]
    if encoder == "amf":
        return ["-quality", FORCED_QUALITY_POLICY["amf"]["quality"]]
    return []


def _stream_copy_and_metadata_args(input_path: Path, output_path: Path):
    """Map streams with container-aware extras: MP4 keeps safe timed metadata; MOV keeps all data/attachments."""
    ext = output_path.suffix.lower()
    args = [
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-copy_unknown",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
    ]
    if ext == ".mov":
        args += ["-map", "0:d?", "-map", "0:t?"]
        return args

    if ext == ".mp4":
        # Keep MP4-safe metadata-like data streams (e.g. tmcd timecode) but avoid private codecs that break muxing.
        safe_data_tags = {"tmcd", "gpmd", "camm", "mett", "metx", "rtmd"}
        for st in _probe_streams(input_path):
            if st.get("codec_type") != "data":
                continue
            tag = (st.get("codec_tag_string") or "").strip().lower()
            if tag in safe_data_tags:
                idx = st.get("index")
                if isinstance(idx, int):
                    args += ["-map", f"0:{idx}"]
    return args


def _audio_codec_args_for_output(input_path: Path, output_path: Path):
    """统一音频策略：音频流直拷贝。"""
    return ["-c:a", "copy"]


def _default_mux_audio_mode(input_path: Path, output_path: Path):
    """默认混流音频模式：音频流直拷贝。"""
    return "copy"


def _extra_stream_codec_args_for_output(output_path: Path):
    """Subtitle always copy; data/attachment copy for MOV and MP4-safe mapped data streams."""
    ext = output_path.suffix.lower()
    args = ["-c:s", "copy"]
    if ext in {".mov", ".mp4"}:
        args += ["-c:d", "copy"]
    if ext == ".mov":
        args += ["-c:t", "copy"]
    return args




def _probe_audio_stream_brief(path: Path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index,codec_name,channels,sample_rate,channel_layout,bit_rate,duration",
        "-of", "json", str(path)
    ]
    rc, out = run_cmd_capture(cmd)
    if rc != 0 or not out:
        return []
    try:
        streams = (json.loads(out) or {}).get("streams", []) or []
    except Exception:
        return []
    rows = []
    for st in streams:
        rows.append({
            "index": st.get("index"),
            "codec": str(st.get("codec_name") or "").lower(),
            "channels": st.get("channels"),
            "sample_rate": str(st.get("sample_rate") or ""),
            "layout": str(st.get("channel_layout") or ""),
            "bit_rate": str(st.get("bit_rate") or ""),
            "duration": str(st.get("duration") or ""),
        })
    return rows


def _audio_stream_hash(path: Path, stream_selector: str):
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-map", stream_selector, "-vn", "-sn", "-dn",
        "-c:a", "pcm_s16le",
        "-f", "hash", "-hash", "sha256", "-"
    ]
    rc, out = run_cmd_capture(cmd)
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SHA256="):
            return line.split("=", 1)[1].strip()
    return None


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def compare_audio_streams(src_path: Path, dst_path: Path):
    src_streams = _probe_audio_stream_brief(src_path)
    dst_streams = _probe_audio_stream_brief(dst_path)
    if len(src_streams) != len(dst_streams):
        return False, f"audio-stream-count-mismatch: src={len(src_streams)} dst={len(dst_streams)}"
    if not src_streams and not dst_streams:
        return True, "no-audio-stream"

    for i, (ss, ds) in enumerate(zip(src_streams, dst_streams)):
        for k in ["channels", "sample_rate"]:
            if str(ss.get(k)) != str(ds.get(k)):
                return False, f"audio-meta-mismatch[{i}] {k}: src={ss.get(k)} dst={ds.get(k)}"

        src_layout = str(ss.get("layout") or "")
        dst_layout = str(ds.get("layout") or "")
        if src_layout and dst_layout and src_layout != dst_layout:
            return False, f"audio-meta-mismatch[{i}] layout: src={src_layout} dst={dst_layout}"

        src_dur = _safe_float(ss.get("duration"), 0.0)
        dst_dur = _safe_float(ds.get("duration"), 0.0)
        if src_dur > 0 and dst_dur > 0 and abs(src_dur - dst_dur) > 1.0:
            return False, f"audio-duration-mismatch[{i}] src={src_dur:.3f}s dst={dst_dur:.3f}s"

        # Decode both streams to a deterministic PCM hash. This verifies copied audio
        # across container remuxes without depending on container packet framing.
        sh = _audio_stream_hash(src_path, f"0:a:{i}")
        dh = _audio_stream_hash(dst_path, f"0:a:{i}")
        if not sh or not dh:
            return False, f"audio-hash-failed[{i}] src_hash={bool(sh)} dst_hash={bool(dh)}"
        if sh != dh:
            return False, f"audio-hash-mismatch[{i}]"

    return True, "audio-verify-ok"


def verify_audio_presence_for_retry(src_path: Path, dst_path: Path):
    """校验转码后音频流数量。"""
    src_streams = _probe_audio_stream_brief(src_path)
    dst_streams = _probe_audio_stream_brief(dst_path)
    if len(src_streams) != len(dst_streams):
        return False, f"audio-stream-count-mismatch: src={len(src_streams)} dst={len(dst_streams)}"
    return True, "audio-stream-count-ok"

def build_ffmpeg_cmd(input_path: Path, output_path: Path, opts: dict, custom_params: str=None):
    src_codec = _probe_video_codec_name(input_path)
    hw_scale_vf = _resolve_hw_scale_filter(opts.get("encoder"), opts.get("scale"))
    need_sw_decode_for_scale = False
    if custom_params and _has_scaling_in_custom_params(custom_params):
        # 自定义参数出现缩放滤镜时，默认走 CPU 解码以避免 HW 帧 + SW scale 不兼容。
        need_sw_decode_for_scale = not bool(hw_scale_vf)
    elif _scale_requested(opts.get("scale")):
        need_sw_decode_for_scale = not bool(hw_scale_vf)

    decode_args = [] if (need_sw_decode_for_scale or opts.get("force_cpu_decode")) else _resolve_hw_decode_args(opts.get("encoder"), src_codec)
    base = ["ffmpeg","-y","-hide_banner","-loglevel","info", *decode_args, "-i", str(input_path)]
    copy_meta_args = _stream_copy_and_metadata_args(input_path, output_path)
    if custom_params:
        # 外部透传参数模式：保持用户参数原样，不做内部策略改写。
        return base + shlex.split(custom_params) + [str(output_path)]
    cmd = base.copy() + copy_meta_args
    cmd += _audio_codec_args_for_output(input_path, output_path)
    cmd += _extra_stream_codec_args_for_output(output_path)
    # scale
    scale = opts.get("scale")
    if scale:
        if hw_scale_vf:
            cmd += hw_scale_vf
        elif scale == "half":
            cmd += ["-vf","scale=iw/2:ih/2"]
        elif scale == "same":
            pass
        else:
            cmd += ["-vf", f"scale={scale}"]
    # video encoder: validated HEVC templates are centralized in ENCODER_PARAM_TEMPLATES.
    codec = opts.get("codec","hevc")
    enc_backend = opts.get("encoder")
    enc = _resolve_video_encoder(enc_backend, codec)
    if codec == "hevc" and enc_backend == "nvenc":
        cmd += ENCODER_PARAM_TEMPLATES["nvenc"]
    elif codec == "hevc" and enc_backend == "qsv":
        cmd += ENCODER_PARAM_TEMPLATES["qsv"]
    elif codec == "hevc" and enc_backend == "amf":
        in_depth = opts.get("input_bit_depth")
        cmd += ENCODER_PARAM_TEMPLATES["amf_10bit" if in_depth == 10 else "amf_8bit"]
    else:
        cmd += ["-c:v", enc]
        rc = opts.get("rc_mode","vbr"); br_min=opts.get("br_min"); br_max=opts.get("br_max"); cqp=opts.get("cqp")
        if rc=="cqp" and cqp is not None:
            cmd += ["-crf", str(cqp)]
            if br_max:
                cmd += ["-maxrate", human_bitrate(br_max), "-bufsize", human_bitrate(br_max*2)]
        elif rc=="vbr" and br_min:
            cmd += ["-b:v", human_bitrate(br_min)]
    manual_quality_override = bool(opts.get("manual_quality_override", False))
    if opts.get("extra"):
        cmd += _strip_conflicting_quality_tokens(
            shlex.split(opts.get("extra")),
            opts.get("encoder"),
            manual_override=manual_quality_override,
        )
    if not (opts.get("codec") == "hevc" and opts.get("encoder") in {"nvenc", "qsv", "amf"}):
        cmd += _forced_quality_args(opts.get("encoder"), manual_override=manual_quality_override)
    cmd += [str(output_path)]
    return cmd


def build_mux_cmd(video_only_path: Path, audio_source_path: Path, output_path: Path, audio_mode="aac320k"):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
        "-i", str(video_only_path), "-i", str(audio_source_path),
        "-map_metadata", "0", "-map_chapters", "0",
        "-copy_unknown",
        "-map", "0:v:0",
        "-map", "1:a?",
        "-map", "0:s?",
        "-map", "0:d?",
        "-map", "0:t?",
        "-c:v", "copy",
        "-c:s", "copy",
        "-c:d", "copy",
        "-c:t", "copy",
    ]
    cmd += ["-c:a", "copy"]
    cmd += [str(output_path)]
    return cmd


def build_audio_extract_cmd(src_path: Path, audio_only_path: Path):
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
        "-i", str(src_path),
        "-vn", "-sn", "-dn",
        "-map", "0:a?",
        "-c:a", "copy",
        str(audio_only_path),
    ]

# ---------------- utility: output path handling ----------------
def _default_output_suffix_for_source(src: Path):
    """
    默认输出容器策略：
    - 输入为 mp4/mov 时保持同容器；
    - 其他容器默认输出 mp4。
    """
    ext = src.suffix.lower()
    if ext in {".mp4", ".mov"}:
        return ext
    return ".mp4"


def make_output_path(src: Path, src_root: Path, dst_root: Path, flat_output=False, out_suffix=None):
    name = src.stem
    parent = src.parent
    if out_suffix:
        name_out = f"{name}{out_suffix}"
    else:
        name_out = name
    ext = _default_output_suffix_for_source(src)
    if flat_output:
        # put everything directly in dst_root; if name collision, append parent dir name
        dst_root.mkdir(parents=True, exist_ok=True)
        candidate = dst_root.joinpath(name_out + ext)
        if candidate.exists():
            candidate = dst_root.joinpath(f"{name_out}_{parent.name}{ext}")
        return candidate
    else:
        rel = src.relative_to(src_root)
        target = dst_root.joinpath(rel).with_name(name_out + ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


def _resolve_output_conflict(target: Path, src: Path):
    """Keep original filename by default; add _comp suffix only when target conflicts."""
    base_stem = target.stem
    suffix = target.suffix
    candidate = target
    idx = 0
    src_abs = str(src.resolve())
    while candidate.exists() or str(candidate.resolve()) == src_abs:
        idx += 1
        tail = "_comp" if idx == 1 else f"_comp{idx}"
        candidate = target.with_name(f"{base_stem}{tail}{suffix}")
    return candidate

# ---------------- execution ----------------
def _run_and_log(cmd, logp: Path, timeout=None, task_label="", src_duration=None, show_progress=True):
    if STOP_EVENT.is_set():
        return 130, "interrupted", 0.0
    start = time.time()
    p = None
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        logp.parent.mkdir(parents=True, exist_ok=True)
        logp.write_text("ffmpeg not found\n", encoding='utf-8')
        return 127, "ffmpeg-not-found", 0.0
    with ACTIVE_PROCS_LOCK:
        ACTIVE_PROCS.add(p)
    logp.parent.mkdir(parents=True, exist_ok=True)
    last_progress_print = 0.0
    with logp.open("wb") as f:
        try:
            text_buf = ""
            while True:
                if STOP_EVENT.is_set():
                    p.kill()
                    return 130, "interrupted", round(time.time()-start,1)
                chunk = p.stdout.read(4096)
                if not chunk:
                    break
                f.write(chunk)
                if show_progress:
                    text_buf += chunk.decode(errors="ignore")
                    parts = text_buf.replace("\r", "\n").split("\n")
                    text_buf = parts[-1]
                    for line in parts[:-1]:
                        line = line.strip()
                        if "time=" not in line:
                            continue
                        idx = line.find("time=")
                        tval = line[idx+5:].split()[0]
                        now = time.time()
                        if now - last_progress_print >= 1.0:
                            last_progress_print = now
                            sec = _ffmpeg_time_to_seconds(tval)
                            if src_duration and src_duration > 0:
                                pct = min(100.0, max(0.0, sec * 100.0 / src_duration))
                                print(f"\r    progress[{task_label}] {pct:5.1f}% ({sec:.1f}s/{src_duration:.1f}s)", end="", flush=True)
                            else:
                                print(f"\r    progress[{task_label}] t={tval}", end="", flush=True)
            p.wait(timeout=timeout)
            if show_progress:
                print("", flush=True)
        except subprocess.TimeoutExpired:
            p.kill()
            return -9, "timeout", round(time.time()-start,1)
        finally:
            with ACTIVE_PROCS_LOCK:
                ACTIVE_PROCS.discard(p)
    if STOP_EVENT.is_set() or p.returncode in (130, 143):
        return 130, "interrupted", round(time.time()-start,1)
    return p.returncode, "", round(time.time()-start,1)


def _terminate_active_procs():
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


def _ffmpeg_time_to_seconds(val: str):
    try:
        hh, mm, ss = val.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except Exception:
        return 0.0
def _execute_single_task(task, logs_root: Path, work_root: Path, timeout=None, show_progress=True):
    outp = Path(task["dst"])
    outp.parent.mkdir(parents=True, exist_ok=True)
    try:
        rel = outp.relative_to(work_root)
    except Exception:
        rel = Path(outp.name)
    primary_log = logs_root.joinpath(rel).with_suffix(".log")
    primary_log.parent.mkdir(parents=True, exist_ok=True)

    temp_files = []
    def _ret(code, note_txt, dur, cmd_used, log_used):
        for p in temp_files:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        return code, note_txt, dur, cmd_used, log_used

    if task.get("custom_params"):
        cmds = task.get("ffmpeg_cmds") or [task["ffmpeg_cmd"]]
    else:
        srcp = Path(task["src"])
        dstp = Path(task["dst"])
        src_audio_streams = _probe_audio_stream_brief(srcp)
        if not src_audio_streams:
            # 无音频源：直接按原路径执行视频转码，避免 audio-only 抽取步骤失败。
            cmds = [task["ffmpeg_cmd"]]
        else:
            tmp_video = dstp.with_name(dstp.stem + ".video_only" + dstp.suffix)
            tmp_audio = dstp.with_name(dstp.stem + ".audio_only.mka")
            temp_files.extend([tmp_video, tmp_audio])
            opts_for_video = dict(task.get("opts") or {})
            opts_for_video["extra"] = (opts_for_video.get("extra", "") + " -an").strip()
            video_cmd = build_ffmpeg_cmd(srcp, tmp_video, opts_for_video, custom_params=None)
            extract_audio_cmd = build_audio_extract_cmd(srcp, tmp_audio)
            mux_copy_cmd = build_mux_cmd(tmp_video, tmp_audio, dstp, audio_mode=_default_mux_audio_mode(srcp, dstp))
            cmds = [extract_audio_cmd, video_cmd, mux_copy_cmd]
    total_dur = 0.0
    rc = 0
    note = ""
    used_cmd = cmds[-1]
    used_log = primary_log
    for idx, cmd in enumerate(cmds, start=1):
        step_log = primary_log if len(cmds) == 1 else primary_log.with_name(primary_log.stem + f".pass{idx}.log")
        rc, note, dur = _run_and_log(
            cmd,
            step_log,
            timeout,
            task_label=Path(task["src"]).name,
            src_duration=task.get("src_duration_sec"),
            show_progress=show_progress,
        )
        total_dur += dur
        used_cmd = cmd
        used_log = step_log
        if rc != 0:
            break

    # Robust fallback: hw encoder failed -> retry once with software encoder.
    if STOP_EVENT.is_set() or rc == 130:
        return _ret(130, "interrupted", total_dur, used_cmd, used_log)
    if rc != 0 and task.get("encoder") in {"nvenc", "qsv", "amf"} and not task.get("custom_params"):
        sw_opts = _normalize_sw_fallback_opts(task.get("opts", {}))
        dstp = Path(task["dst"])
        srcp = Path(task["src"])
        tmp_video = dstp.with_name(dstp.stem + ".video_only" + dstp.suffix)
        if tmp_video not in temp_files:
            temp_files.append(tmp_video)
        sw_opts["extra"] = (sw_opts.get("extra", "") + " -an").strip()
        fallback_cmd = build_ffmpeg_cmd(srcp, tmp_video, sw_opts, custom_params=None)
        fallback_log = primary_log.with_name(primary_log.stem + ".fallback.log")
        rc2, note2, dur2 = _run_and_log(
            fallback_cmd,
            fallback_log,
            timeout,
            task_label=Path(task["src"]).name + "(fallback)",
            src_duration=task.get("src_duration_sec"),
            show_progress=show_progress,
        )
        total_dur += dur2
        if rc2 == 0:
            src_audio_streams = _probe_audio_stream_brief(srcp)
            if src_audio_streams:
                fallback_audio = dstp.with_name(dstp.stem + ".audio_only.mka")
                if fallback_audio not in temp_files:
                    temp_files.append(fallback_audio)
                extract_audio_cmd = build_audio_extract_cmd(srcp, fallback_audio)
                rc_audio, note_audio, dur_audio = _run_and_log(
                    extract_audio_cmd,
                    primary_log.with_name(primary_log.stem + ".fallback_audio.log"),
                    timeout,
                    task_label=Path(task["src"]).name + "(fallback-audio)",
                    src_duration=task.get("src_duration_sec"),
                    show_progress=show_progress,
                )
                total_dur += dur_audio
                if rc_audio != 0:
                    return _ret(rc_audio, f"fallback-audio-failed: {note_audio}", total_dur, extract_audio_cmd, fallback_log)
                mux_copy_cmd = build_mux_cmd(
                    tmp_video,
                    fallback_audio,
                    dstp,
                    audio_mode=_default_mux_audio_mode(srcp, dstp),
                )
                rc3, note3, dur3 = _run_and_log(
                    mux_copy_cmd,
                    primary_log.with_name(primary_log.stem + ".fallback_mux.log"),
                    timeout,
                    task_label=Path(task["src"]).name + "(fallback-mux)",
                    src_duration=task.get("src_duration_sec"),
                    show_progress=show_progress,
                )
                total_dur += dur3
                if rc3 == 0:
                    return _ret(rc3, f"fallback-ok: {task.get('encoder')} -> cpu", total_dur, mux_copy_cmd, fallback_log)
                return _ret(rc3, f"fallback-video-ok-but-mux-failed: {note3}", total_dur, mux_copy_cmd, fallback_log)
            try:
                tmp_video.replace(dstp)
            except Exception as ex:
                return _ret(66, f"fallback-video-move-failed: {ex}", total_dur, fallback_cmd, fallback_log)
            return _ret(0, f"fallback-ok-no-audio: {task.get('encoder')} -> cpu", total_dur, fallback_cmd, fallback_log)
        return _ret(rc2, f"fallback-failed: primary={rc} secondary={rc2}; {note2 or note}", total_dur, fallback_cmd, fallback_log)

    if rc == 0 and not task.get("custom_params"):
        srcp = Path(task["src"])
        dstp = Path(task["dst"])
        ok, note_audio = verify_audio_presence_for_retry(srcp, dstp)
        if not ok:
            return _ret(65, f"audio-verify-failed({note_audio})", total_dur, used_cmd, used_log)
        return _ret(0, f"audio-verify={note_audio}", total_dur, used_cmd, used_log)

    return _ret(rc, note, total_dur, used_cmd, used_log)


def execute_tasks(tasks, concurrency, work_root: Path, result_csv: Path, logs_root: Path, timeout=None, show_progress=True):
    STOP_EVENT.clear()
    total = len(tasks); lock = threading.Lock()
    failed_tasks = []
    headers = ["src","dst","group","encoder","codec","preset","rc_mode","br_min","br_max","cqp","ffmpeg_cmd","log","returncode","note","secs"]
    if result_csv.exists(): result_csv.unlink()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {}
        for t in tasks:
            futures[ex.submit(_execute_single_task, t, logs_root, work_root, timeout, show_progress)] = t
        completed = 0
        try:
            for fut in as_completed(futures):
                t = futures[fut]
                rc, note, dur, final_cmd, logp = fut.result()
                completed += 1
                entry = {"src":t["src"], "dst":t["dst"], "group":t.get("group",""), "encoder":t.get("encoder",""), "codec":t.get("codec",""),
                         "preset":t.get("preset",""), "rc_mode":t.get("rc_mode",""), "br_min":t.get("br_min",""), "br_max":t.get("br_max",""),
                         "cqp":t.get("cqp",""), "ffmpeg_cmd":" ".join(shlex.quote(x) for x in final_cmd), "log":str(logp),
                         "returncode":rc, "note":note, "secs":dur}
                with lock:
                    append_csv(result_csv, headers, entry)
                    status = "OK" if rc==0 else f"ERR({rc})"
                    print(f"[{completed}/{total}] {Path(t['src']).name} -> {Path(t['dst']).name} : {status}  log:{logp.name}")
                    if rc != 0:
                        failed_tasks.append(dict(t))
        except KeyboardInterrupt:
            STOP_EVENT.set()
            _terminate_active_procs()
            ex.shutdown(wait=False, cancel_futures=True)
            print("Interrupted by user (Ctrl+C). All transcoding processes are being stopped.")
            raise SystemExit(130)
    return failed_tasks


def move_failed_sources_to_error(failed_tasks, src_root: Path, error_root: Path):
    moved, skipped = [], []
    for t in failed_tasks:
        srcp = Path(t["src"])
        if not srcp.exists():
            skipped.append((str(srcp), "source-not-found"))
            continue
        try:
            rel = srcp.relative_to(src_root)
        except Exception:
            rel = Path(srcp.name)
        dstp = error_root.joinpath(rel)
        dstp.parent.mkdir(parents=True, exist_ok=True)
        if dstp.exists():
            dstp = dstp.with_name(f"{dstp.stem}_failed{dstp.suffix}")
        shutil.move(str(srcp), str(dstp))
        moved.append((str(srcp), str(dstp)))
    return moved, skipped

# ---------------- main ----------------
def main():
    examples = """
EXAMPLES:
  Query encoder params only:
    transcode_hw_main.py --query-params nvenc --work ./work

  Single-file transcode with explicit output folder:
    transcode_hw_main.py --src ./video.mp4 --dst ./out --skip

  Directory transcode with explicit output folder:
    transcode_hw_main.py --src F:/Movies/Batch --dst F:/Out --skip

  Directory with grouping and interactive per-group config:
    transcode_hw_main.py --src F:/Movies/Batch --dst F:/Out --work F:/Work --group

  Skip interactive prompts and run with default params (skip bitrate and encoder prompt):
    transcode_hw_main.py --src F:/Movies/Batch --work F:/Work --skip --skip-bitrate --skip-encfmt
"""
    parser = argparse.ArgumentParser(description="transcode_hw_main: hw-accelerated batch transcode (nvenc/qsv/amf). Default: grouping ON. Use --no-group to treat all as 1 group.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=examples)
    parser.add_argument("--src", help="源路径（文件或目录）。可为单个文件或目录。若省略且只执行 --query-params，则可不指定。")
    parser.add_argument("--dst", help="目标根目录。未指定时默认输出到源路径同级的 <src>_comp 目录；单文件输出到该目录中。")
    parser.add_argument("--work", help="工作目录（CSV, logs, mediainfo 等）。若只想 query encoder，也可用 --work 指定保存位置。")
    parser.add_argument("--recursive", action="store_true", help="递归遍历子文件夹（默认不递归）")
    parser.add_argument("--extensions", default="mp4,mov", help="要处理的文件扩展名，逗号分隔（例如 mp4,mov）")
    parser.add_argument("--prefixes", default="", help="文件名前缀白名单，逗号分隔（例如 DJI）。不指定则不过滤前缀。")
    parser.add_argument("--suffixes", default="", help="文件名后缀白名单，逗号分隔（例如 _EDIT）。不指定则不过滤后缀。")
    parser.add_argument("--invert-prefix", action="store_true", help="对前缀白名单使用反选逻辑（匹配的会被排除）")
    parser.add_argument("--invert-suffix", action="store_true", help="对后缀白名单使用反选逻辑（匹配的会被排除）")
    parser.add_argument("--encoder", choices=["nvenc","qsv","amf"], default="nvenc", help="默认硬件编码器")
    parser.add_argument("--codec", choices=["hevc","h264"], default="hevc", help="默认视频编码")
    parser.add_argument("--rc-mode", choices=["vbr","cbr","cqp","icq"], default=None, help="默认码率控制（若不指定按规则自动选择）")
    parser.add_argument("--min-br", type=float, default=None, help="默认最小比特率 Mbps（覆盖规则）")
    parser.add_argument("--max-br", type=float, default=None, help="默认最大比特率 Mbps（覆盖规则）")
    parser.add_argument("--cqp", type=int, default=None, help="默认 CQP/CQ 值")
    parser.add_argument("--preset", default=None, help="底层编码器 preset（nvenc:p1..p7, qsv:TU1..TU7 等）")
    parser.add_argument("--group", action="store_true", help="兼容旧命令的占位参数；分组默认已启用，使用 --no-group 可关闭。")
    parser.add_argument("--no-group", action="store_true", help="跳过分组（将所有文件视为单组）；默认分组开启。")
    parser.add_argument("--custom-params", default=None, help="自定义 ffmpeg 参数字符串（直接插入到 -i <input> 之后）。不要包含输出文件名。")
    parser.add_argument("--query-params", choices=["nvenc","qsv","amf"], default=None, help="查询 ffmpeg encoder 参数并打印，支持单独运行（只需 --query-params 和 --work）。")
    parser.add_argument("--concurrency", type=int, default=1, help="并发 ffmpeg 进程数")
    parser.add_argument("--timeout", type=int, default=None, help="每任务超时（秒），默认无超时")
    parser.add_argument("--skip", action="store_true", help="跳过交互（采用默认参数并直接执行）。默认不跳过。")
    parser.add_argument("--skip-bitrate", action="store_true", help="skip 时忽略码率交互/修改（接受建议）")
    parser.add_argument("--skip-res", action="store_true", help="skip 时忽略分辨率交互/修改")
    parser.add_argument("--skip-encfmt", action="store_true", help="skip 时忽略编码格式（hevc/h264）交互/修改")
    parser.add_argument("--skip-hwaccel", action="store_true", help="skip 时忽略硬件加速器选择（nvenc/qsv/amf）交互/修改")
    parser.add_argument("--skip-builtin-checks", "--skip-check", action="store_true", help="跳过执行前确认与严格输出兼容性终止；仍打印参数，并启用输出格式降级 fallback。")
    parser.add_argument("--show-groups-only", action="store_true", help="仅显示分组与 preflight CSV，然后退出")
    parser.add_argument("--flat-output", action="store_true", help="所有输出放在同一目标目录（不保留原始目录结构）；若冲突自动在文件名加源目录名后缀")
    parser.add_argument("--out-suffix", default="", help="输出文件名后缀（例如 _comp 或 deliver；未以下划线开头时会自动补 _）。")
    parser.add_argument("--nvenc-qual", default=None, help="快速手动覆盖 NVENC 质量档（例如 p7/p5）。设置后将覆盖默认强制策略。")
    parser.add_argument("--qsv-qual", default=None, help="快速手动覆盖 QSV TU 档位（例如 tu1/tu3 或 1/3）。设置后将覆盖默认强制策略。")
    parser.add_argument("--amf-qual", default=None, help="快速手动覆盖 AMF quality（例如 quality/balanced/speed）。设置后将覆盖默认强制策略。")
    args = parser.parse_args()

    # reset to default every run
    FORCED_QUALITY_POLICY["nvenc"]["preset"] = DEFAULT_FORCED_QUALITY_POLICY["nvenc"]["preset"]
    FORCED_QUALITY_POLICY["qsv"]["tu"] = DEFAULT_FORCED_QUALITY_POLICY["qsv"]["tu"]
    FORCED_QUALITY_POLICY["amf"]["quality"] = DEFAULT_FORCED_QUALITY_POLICY["amf"]["quality"]
    if args.nvenc_qual:
        FORCED_QUALITY_POLICY["nvenc"]["preset"] = str(args.nvenc_qual).strip().lower()
    if args.qsv_qual:
        qsv_q = str(args.qsv_qual).strip().lower()
        if qsv_q.startswith("tu"):
            qsv_q = qsv_q[2:]
        FORCED_QUALITY_POLICY["qsv"]["tu"] = qsv_q
    if args.amf_qual:
        FORCED_QUALITY_POLICY["amf"]["quality"] = str(args.amf_qual).strip().lower()

    print(
        "Forced quality policy: "
        f"NVENC preset={FORCED_QUALITY_POLICY['nvenc']['preset']}, "
        f"QSV TU={FORCED_QUALITY_POLICY['qsv']['tu']}, "
        f"AMF quality={FORCED_QUALITY_POLICY['amf']['quality']}"
    )
    if args.custom_params:
        print("Manual injected command detected (--custom-params): full manual params take precedence.")

    # handle query-only case
    if args.query_params and not args.src:
        work = Path(args.work).expanduser().resolve() if args.work else Path.cwd().joinpath("transcode_hw_main_work")
        work.mkdir(parents=True, exist_ok=True)
        query_encoder(args.query_params, work)
        print("Query finished. Exiting.")
        sys.exit(0)

    # require src for normal flow
    if not args.src:
        print("Error: --src required (or use --query-params only)."); sys.exit(2)
    src = Path(args.src).expanduser().resolve()
    # determine work root
    if args.work:
        work_root = Path(args.work).expanduser().resolve()
    else:
        # default work root: sibling of src or cwd
        work_root = (src.parent if src.exists() else Path.cwd()).joinpath(f"{src.name}_work")
    work_root.mkdir(parents=True, exist_ok=True)
    logs_root = work_root.joinpath("logs"); logs_root.mkdir(parents=True, exist_ok=True)
    preflight_csv = work_root.joinpath("preflight_files.csv")
    groups_csv = work_root.joinpath("groups_summary.csv")
    pre_media_csv = work_root.joinpath("pre_media_info.csv")
    post_media_csv = work_root.joinpath("post_media_info.csv")
    audio_verify_csv = work_root.joinpath("audio_verify.csv")
    tasks_json = work_root.joinpath("tasks_preflight.json")
    result_csv = work_root.joinpath("tasks_result.csv")
    input_probe_csv = work_root.joinpath("input_probe.csv")
    encoder_caps_csv = work_root.joinpath("encoder_capabilities.csv")

    # if query requested plus src/work provided, do query as well
    if args.query_params:
        query_encoder(args.query_params, work_root)
        # continue normal flow (user may want both)

    # detect single-file vs dir
    is_single_file = src.is_file()
    # default dst handling: write outputs to a sibling <src>_comp folder when --dst is omitted.
    if args.dst:
        dst_root = Path(args.dst).expanduser().resolve()
    else:
        print("Note: --dst not provided. Defaulting to sibling output folder: <src>_comp.")
        dst_root = src.parent.joinpath(f"{src.name}_comp")
    dst_root.mkdir(parents=True, exist_ok=True)

    # collect filters
    exts = [('.' + e.strip().lstrip('.').lower()) for e in args.extensions.split(",") if e.strip()]
    prefixes = [p for p in (x.strip() for x in args.prefixes.split(",") if x.strip())] if args.prefixes else []
    suffixes = [s for s in (x.strip() for x in args.suffixes.split(",") if x.strip())] if args.suffixes else []

    def make_task(srcp: Path, outp: Path, group_id, opts: dict, custom_params=None, encoder_label=None, ffmpeg_cmds=None, src_duration_sec=None):
        opts_local = dict(opts or {})
        probe_row = input_probe_map.get(str(srcp), {})
        opts_local["input_bit_depth"] = _format_bit_depth(probe_row.get("bit_depth"))
        opts_local["input_chroma_subsampling"] = probe_row.get("chroma_subsampling")
        actual_output_format = "default/auto"
        enc_name = str(opts_local.get("encoder", "")).lower()
        manual_quality_override = (
            (enc_name == "nvenc" and args.nvenc_qual is not None) or
            (enc_name == "qsv" and args.qsv_qual is not None) or
            (enc_name == "amf" and args.amf_qual is not None)
        )
        if manual_quality_override:
            opts_local["manual_quality_override"] = True
        if not custom_params and enc_name in {"nvenc", "qsv", "amf"}:
            cap = encoder_cap_map.get(_resolve_video_encoder(enc_name, opts_local.get("codec", "hevc")), {})
            bd = opts_local.get("input_bit_depth")
            chroma = opts_local.get("input_chroma_subsampling")
            if cap and not _cap_supports(cap, bd, chroma):
                print(f"WARNING: input {bd}bit {chroma} may not be supported by hardware decode/encode capability table; removing -hwaccel and using CPU software decode for {srcp}")
                opts_local["force_cpu_decode"] = True
            if args.skip_builtin_checks and bd == 10:
                opts_local["fallback_pix_fmt"] = "p010le"
                actual_output_format = "10bit 4:2:0 (p010le fallback candidate)"
            elif args.skip_builtin_checks:
                opts_local["fallback_pix_fmt"] = "yuv420p"
                actual_output_format = "8bit 4:2:0 (yuv420p fallback candidate)"
        cmd = build_ffmpeg_cmd(srcp, outp, opts_local, custom_params=custom_params)
        if opts_local.get("fallback_pix_fmt") and "-pix_fmt" not in cmd:
            cmd = cmd[:-1] + ["-pix_fmt", opts_local["fallback_pix_fmt"]] + cmd[-1:]
            print(f"WARNING: --skip-check enabled; output fallback format for {srcp} -> {opts_local['fallback_pix_fmt']}")
        task = {
            "src": str(srcp),
            "dst": str(outp),
            "group": group_id,
            "encoder": encoder_label if encoder_label is not None else opts_local.get("encoder", ""),
            "codec": opts_local.get("codec", ""),
            "preset": opts_local.get("preset", ""),
            "rc_mode": opts_local.get("rc_mode", ""),
            "br_min": opts_local.get("br_min", ""),
            "br_max": opts_local.get("br_max", ""),
            "cqp": opts_local.get("cqp", ""),
            "opts": opts_local,
            "custom_params": custom_params,
            "ffmpeg_cmd": cmd,
            "src_duration_sec": src_duration_sec,
            "actual_output_format": actual_output_format,
        }
        if ffmpeg_cmds:
            task["ffmpeg_cmds"] = ffmpeg_cmds
        return task

    def _apply_container_policy(srcp: Path, outp: Path):
        if force_mov_map.get(str(srcp), False):
            return outp.with_suffix(".mov")
        return outp

    # collect files
    files = []
    if is_single_file:
        files = [src]
    else:
        files = collect_inputs(src, args.recursive, exts, prefixes, suffixes, args.invert_prefix, args.invert_suffix)
    if not files:
        print("No files found. Exiting."); sys.exit(0)
    print(f"Found {len(files)} input files. Probing media info...")
    entries, groups = group_by_res_fps(files)
    input_probe_rows = [probe_input_format(Path(f)) for f in files]
    input_probe_map = {r["file_path"]: r for r in input_probe_rows}
    encoder_cap_rows = probe_encoder_capabilities()
    encoder_cap_map = {r["encoder_name"]: r for r in encoder_cap_rows}
    write_csv(input_probe_csv, ["file_path","bit_depth","chroma_subsampling","pixel_format","probe_time"], input_probe_rows)
    write_csv(encoder_caps_csv, ["encoder_name","available","supported_profiles","supported_pixel_formats","probe_time"], encoder_cap_rows)
    src_duration_map = {str(e.get("path")): _safe_float(e.get("duration")) for e in entries}
    force_mov_map = {str(Path(e.get("path"))): _should_force_mov_output(Path(e.get("path"))) for e in entries}
    write_csv(preflight_csv, ["path","width","height","fps","codec","duration","bitrate","bit_depth","chroma_subsampling","probe_time"], [{**e, **{"bit_depth": input_probe_map.get(str(Path(e["path"])),{}).get("bit_depth",""), "chroma_subsampling": input_probe_map.get(str(Path(e["path"])),{}).get("chroma_subsampling",""), "probe_time": input_probe_map.get(str(Path(e["path"])),{}).get("probe_time","")}} for e in entries])
    # write groups summary
    grp_rows = []
    for g in groups:
        grp_rows.append({"group":g["group_id"], "width":g["width"], "height":g["height"], "fps":g["fps"], "count":len(g["files"])})
    write_csv(groups_csv, ["group","width","height","fps","count"], grp_rows)
    write_csv(pre_media_csv, ["path","width","height","fps","codec","duration","bitrate"], entries)
    print("Preflight and media info written to work folder.")

    if args.show_groups_only:
        print("Groups summary:")
        for g in groups:
            print(f"  Group {g['group_id']}: {g['width']}x{g['height']} @ {g['fps']} fps, files={len(g['files'])}")
        print("Show-groups-only requested. Exiting before task generation.")
        sys.exit(0)

    # handle output suffix
    out_suffix = args.out_suffix or ""
    if out_suffix and not out_suffix.startswith("_"):
        out_suffix = "_" + out_suffix

    # Determine execution mode: custom params or interactive/default CLI.
    chosen_mode = "custom" if args.custom_params else "interactive"

    grouping_enabled = not args.no_group

    # build tasks
    tasks = []
    if chosen_mode == "custom":
        if not args.custom_params:
            print("custom mode selected but no --custom-params given. Exiting."); sys.exit(2)
        # single file or many: apply custom params directly
        for f in files:
            srcp = Path(f)
            if is_single_file:
                outp = dst_root.joinpath(f"{srcp.stem}{out_suffix}{_default_output_suffix_for_source(srcp)}")
                outp.parent.mkdir(parents=True, exist_ok=True)
            else:
                outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix)
            outp = _apply_container_policy(srcp, outp)
            outp = _resolve_output_conflict(outp, srcp)
            tasks.append(make_task(srcp, outp, 0, {}, custom_params=args.custom_params, encoder_label="custom", src_duration_sec=src_duration_map.get(str(srcp))))
        work_logs_root = dst_root.parent.joinpath(f"{dst_root.name}_logs") if args.dst is None else dst_root.parent.joinpath(f"{dst_root.name}_logs")
    else:
        # interactive/default CLI flow: if no-group -> one group; else per-group interactive (but can be skipped by --skip)
        if not grouping_enabled:
            groups = [{"group_id":0,"width":None,"height":None,"fps":None,"files":[{"path":str(f)} for f in files]}]
        # decide per-group configs
        group_configs = {}
        if args.skip:
            # build default configs for skip mode
            for g in groups:
                rc_mode = "cqp"
                cfg = {"encoder": args.encoder if not args.skip_hwaccel else args.encoder, "codec": args.codec,
                       "rc_mode": rc_mode, "preset": args.preset, "br_min": None, "br_max": None,
                       "cqp": 24,
                       "audio_bitrate":320, "scale":"same", "extra":""}
                # skip mode uses fixed defaults without interactive edits.
                group_configs[g["group_id"]] = cfg
            # print summary
            print("Skip mode: will apply the following per-group configs (defaults):")
            for k,v in group_configs.items():
                print(f"  Group {k}: encoder={v['encoder']}, codec={v['codec']}, cfg_rc={v['rc_mode']}, cfg_cqp={v['cqp']}, cfg_preset={v['preset']}, scale={v['scale']}")
                if str(v["encoder"]).lower() == "nvenc":
                    print("           effective NVENC: rc=vbr, cq=18, preset=p7, tune=uhq, aq=12, lookahead=64, multipass=fullres")
            if not args.skip_builtin_checks:
                c = input("Confirm and proceed? (y/N): ").strip().lower()
                if c != "y": print("Aborted"); sys.exit(0)
        else:
            # interactive: prompt per-group
            for g in groups:
                gid = g["group_id"]
                w,h = g["width"], g["height"]
                cqp_fixed = 24
                print(f"\nGroup {gid}: {w}x{h} @ {g['fps']} fps  files:{len(g['files'])}")
                print(f"Fixed CRF/CQ: {cqp_fixed}")
                enc = input(f"  encoder [{args.encoder}]: ").strip() or args.encoder
                codec = input(f"  codec [{args.codec}]: ").strip() or args.codec
                rc = "cqp"
                preset = input(f"  preset [{args.preset or ''}]: ").strip() or args.preset
                brmin = None
                brmax = None
                cqp = cqp_fixed
                scale = input("  scale (e.g. 1920x1080/half/same) [same]: ").strip() or "same"
                group_configs[gid] = {"encoder":enc,"codec":codec,"rc_mode":rc,"preset":preset,"br_min":brmin,"br_max":brmax,"cqp":cqp,"audio_bitrate":320,"scale":scale,"extra":""}
        # build tasks from group_configs
        for g in groups:
            cfg = group_configs.get(g["group_id"])
            for f in g["files"]:
                srcp = Path(f["path"])
                if is_single_file:
                    outp = dst_root.joinpath(f"{srcp.stem}{out_suffix}{_default_output_suffix_for_source(srcp)}")
                    outp.parent.mkdir(parents=True, exist_ok=True)
                else:
                    outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix)
                outp = _apply_container_policy(srcp, outp)
                outp = _resolve_output_conflict(outp, srcp)
                tasks.append(make_task(srcp, outp, g["group_id"], cfg, src_duration_sec=src_duration_map.get(str(srcp))))

    _print_task_commands(tasks)
    if not args.skip_builtin_checks:
        c = input("Confirm FFmpeg commands and proceed? (y/N): ").strip().lower()
        if c != "y":
            print("Aborted"); sys.exit(0)

    # write preflight tasks
    with tasks_json.open("w", encoding='utf-8') as jf:
        json.dump(tasks, jf, indent=2, ensure_ascii=False)
    print(f"Tasks preflight saved to: {tasks_json}  (total: {len(tasks)})")
    # determine logs root
    if is_single_file:
        logs_dir = src.parent.joinpath(f"{src.stem}_logs")
    else:
        if args.dst:
            logs_dir = Path(args.dst).parent.joinpath(f"{Path(args.dst).name}_logs")
        else:
            logs_dir = dst_root.parent.joinpath(f"{dst_root.name}_logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    # execute
    failed_tasks = execute_tasks(tasks, args.concurrency, work_root, result_csv, logs_dir, timeout=args.timeout, show_progress=True)
    _summarize_params(tasks)
    # post media info
    out_entries = []
    for t in tasks:
        dstp = Path(t["dst"])
        info = probe_media(dstp) or {"width":None,"height":None,"codec":"","fps":0.0,"duration":"","bitrate":""}
        out_entries.append({"path":str(dstp),"width":info["width"],"height":info["height"],"fps":info["fps"],"codec":info["codec"],"duration":info.get("duration"),"bitrate":info.get("bitrate")})
    write_csv(post_media_csv, ["path","width","height","fps","codec","duration","bitrate"], out_entries)

    audio_rows = []
    all_audio_ok = True
    for t in tasks:
        srcp = Path(t["src"])
        dstp = Path(t["dst"])
        ok, note = compare_audio_streams(srcp, dstp)
        if not ok:
            all_audio_ok = False
        audio_rows.append({"src": str(srcp), "dst": str(dstp), "audio_match": int(bool(ok)), "note": note})
    write_csv(audio_verify_csv, ["src", "dst", "audio_match", "note"], audio_rows)

    print("Post media info written to:", post_media_csv)
    print("Audio verify report written to:", audio_verify_csv)
    if not all_audio_ok:
        print("Warning: some output audio streams differ from source. See:", audio_verify_csv)
    if failed_tasks:
        src_root = src.parent if is_single_file else src
        error_root = src_root.joinpath("error")
        moved, skipped = move_failed_sources_to_error(failed_tasks, src_root, error_root)
        print(f"Failed tasks: {len(failed_tasks)}. Moved to error folder: {len(moved)}. Error root: {error_root}")
        if skipped:
            print(f"Warning: {len(skipped)} failed source files were not moved (missing or unavailable).")
    print("Done. Results:", result_csv)

if __name__ == "__main__":
    main()
