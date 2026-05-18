#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcode_hw_main.py - 硬件转码工具 (nvenc/qsv/amf)

主要特性摘要：
 - 支持单文件或目录输入（--src 可为文件或目录）。
 - 支持 --query-params 单独查询 ffmpeg encoder 帮助 (可单独运行)。
 - 指定 --use-preset 时默认不做分组并输出到 <src>_comp / <src>_logs（可用 --group 强制分组）。
 - 默认交互；--skip 可跳过交互（并可细化跳过哪些项）。
 - 支持 --flat-output、--out-suffix（可指定为 preset1..preset8，语义化为 preset 描述）。
 - pre/post media info CSV，per-task log，实时进度输出。
"""

import argparse, csv, json, shlex, subprocess, sys, threading, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, OrderedDict

STOP_EVENT = threading.Event()
ACTIVE_PROCS_LOCK = threading.Lock()
ACTIVE_PROCS = set()
_FFMPEG_FILTERS_CACHE = None

DEFAULT_FORCED_QUALITY_POLICY = {
    "nvenc": {"preset": "p7"},
    "qsv": {"tu": "1"},
    "amf": {"quality": "quality"},
}
FORCED_QUALITY_POLICY = json.loads(json.dumps(DEFAULT_FORCED_QUALITY_POLICY))
# ---------------- presets ----------------
PRESETS_INFO = OrderedDict([
    ("preset1", {"name":"4k_prog_archive_1pass", "desc":"HEVC NVENC@P7, 1pass, vbr_hq(30/40), main10 p010, aq+lookahead"}),
    ("preset2", {"name":"1080p_prog_rel_1pass", "desc":"HEVC QSV@TU1, 1pass, VBR(6/8), lookahead, aac@320k"}),
    ("preset3", {"name":"4k_prog_archive_2pass", "desc":"HEVC NVENC@P7, multipass fullres, vbr_hq(30/40)"}),
    ("preset4", {"name":"1080p_prog_rel_2pass", "desc":"HEVC x265 slow, 2pass @6M"}),
    ("preset5", {"name":"fast_proxy_gen_halfres_avc_5m", "desc":"AVC NVENC(强制P7), tune ll, CBR 5M, half res, aac@128k"}),
    ("preset6", {"name":"fast_proxy_gen_fullres_avc_5m", "desc":"AVC NVENC(强制P7), tune ll, CBR 5M, full res, profile high"}),
    ("preset7", {"name":"social_plat_share_halfres", "desc":"HEVC QSV(强制TU1), ICQ 28, lookahead, half res, aac@320k"}),
    ("preset8", {"name":"social_plat_share_fullres", "desc":"HEVC QSV(强制TU1), ICQ 27, lookahead, full res, aac@320k"}),
])

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


def _probe_primary_audio_info(input_path: Path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,bit_rate:format=duration",
        "-of", "json", str(input_path)
    ]
    rc, out = run_cmd_capture(cmd)
    if rc != 0 or not out:
        return {}
    try:
        obj = json.loads(out) or {}
    except Exception:
        return {}
    streams = obj.get("streams") or []
    stream0 = streams[0] if streams else {}
    fmt = obj.get("format") or {}
    return {
        "codec_name": str(stream0.get("codec_name") or "").lower(),
        "bit_rate": _safe_float(stream0.get("bit_rate")),
        "duration": _safe_float(fmt.get("duration")),
    }


def _needs_pcm_safety_reencode(input_path: Path, output_path: Path):
    """
    避免超长素材里 PCM 直拷导致音频数据超过 4GiB 触发封装失败。
    策略：仅对 MOV + PCM 进行检查，估算音频大小超阈值时改为 AAC。
    """
    if output_path.suffix.lower() != ".mov":
        return False
    info = _probe_primary_audio_info(input_path)
    codec = info.get("codec_name", "")
    if not codec.startswith("pcm_"):
        return False
    br = info.get("bit_rate")
    dur = info.get("duration")
    if not br or not dur:
        return False
    estimated_bytes = (br / 8.0) * dur
    return estimated_bytes >= (4 * 1024 * 1024 * 1024)


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
        args = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        dec = nvdec_map.get(src_codec)
        if dec:
            args += ["-c:v", dec]
        return args
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
        args = ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
        dec = qsv_map.get(src_codec)
        if dec:
            args += ["-c:v", dec]
        return args
    if encoder == "amf":
        # FFmpeg 没有通用的 *_amf 解码器，AMD 通常走 D3D11VA 硬解路径
        return ["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"]
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
    """输出封装时始终直拷原始音频流。"""
    return ["-c:a", "copy"]


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
        "-show_entries", "stream=index,codec_name,channels,sample_rate,channel_layout,bit_rate",
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
        })
    return rows


def _audio_stream_hash(path: Path, stream_selector: str):
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-map", stream_selector, "-c", "copy",
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


def compare_audio_streams(src_path: Path, dst_path: Path):
    src_streams = _probe_audio_stream_brief(src_path)
    dst_streams = _probe_audio_stream_brief(dst_path)
    if len(src_streams) != len(dst_streams):
        return False, f"audio-stream-count-mismatch: src={len(src_streams)} dst={len(dst_streams)}"
    if not src_streams and not dst_streams:
        return True, "no-audio-stream"

    for i, (ss, ds) in enumerate(zip(src_streams, dst_streams)):
        for k in ["codec", "channels", "sample_rate", "layout"]:
            if str(ss.get(k)) != str(ds.get(k)):
                return False, f"audio-meta-mismatch[{i}] {k}: src={ss.get(k)} dst={ds.get(k)}"
        sh = _audio_stream_hash(src_path, f"0:a:{i}")
        dh = _audio_stream_hash(dst_path, f"0:a:{i}")
        if not sh or not dh:
            return False, f"audio-hash-failed[{i}] src_hash={bool(sh)} dst_hash={bool(dh)}"
        if sh != dh:
            return False, f"audio-hash-mismatch[{i}]"
    return True, "audio-identical"

def build_ffmpeg_cmd(input_path: Path, output_path: Path, opts: dict, custom_params: str=None):
    src_codec = _probe_video_codec_name(input_path)
    hw_scale_vf = _resolve_hw_scale_filter(opts.get("encoder"), opts.get("scale"))
    need_sw_decode_for_scale = False
    if custom_params and _has_scaling_in_custom_params(custom_params):
        # 自定义参数出现缩放滤镜时，默认走 CPU 解码以避免 HW 帧 + SW scale 不兼容。
        need_sw_decode_for_scale = not bool(hw_scale_vf)
    elif _scale_requested(opts.get("scale")):
        need_sw_decode_for_scale = not bool(hw_scale_vf)

    decode_args = [] if need_sw_decode_for_scale else _resolve_hw_decode_args(opts.get("encoder"), src_codec)
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
    # video encoder
    codec = opts.get("codec","hevc")
    enc = _resolve_video_encoder(opts.get("encoder"), codec)
    cmd += ["-c:v", enc]
    if opts.get("preset"): cmd += ["-preset", str(opts.get("preset"))]
    # rc
    rc = opts.get("rc_mode","vbr"); br_min=opts.get("br_min"); br_max=opts.get("br_max"); cqp=opts.get("cqp")
    if "nvenc" in enc:
        if rc=="vbr":
            if br_min: cmd += ["-rc","vbr","-b:v", human_bitrate(br_min)]
            if br_max: cmd += ["-maxrate", human_bitrate(br_max), "-bufsize", human_bitrate(br_max*2)]
        elif rc=="cbr":
            if br_min: cmd += ["-rc","cbr","-b:v", human_bitrate(br_min)]
        elif rc=="cqp" and cqp is not None:
            cmd += ["-rc","constqp","-qp", str(cqp)]
        elif rc=="icq" and cqp is not None:
            cmd += ["-rc","vbr","-cq", str(cqp)]
    elif "qsv" in enc:
        if rc=="vbr":
            if br_min: cmd += ["-rc","vbr","-b:v", human_bitrate(br_min)]
            if br_max: cmd += ["-maxrate", human_bitrate(br_max), "-bufsize", human_bitrate(br_max*2)]
        elif rc=="cqp" and cqp is not None:
            cmd += ["-rc","constqp","-qp", str(cqp)]
        elif rc=="icq" and cqp is not None:
            cmd += ["-global_quality", str(cqp)]
    elif "amf" in enc:
        if rc=="vbr":
            if br_min: cmd += ["-rc","vbr","-b:v", human_bitrate(br_min)]
            if br_max: cmd += ["-maxrate", human_bitrate(br_max), "-bufsize", human_bitrate(br_max*2)]
        elif rc=="cqp" and cqp is not None:
            cmd += ["-rc","constqp","-qp", str(cqp)]
    else:
        if rc=="cqp" and cqp is not None:
            # software encoders prefer CRF semantics over fixed QP for practical VOD use
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
    cmd += _forced_quality_args(opts.get("encoder"), manual_override=manual_quality_override)
    cmd += [str(output_path)]
    return cmd

# ---------------- utility: output path handling ----------------
def make_output_path(src: Path, src_root: Path, dst_root: Path, flat_output=False, out_suffix=None):
    name = src.stem
    parent = src.parent
    if out_suffix:
        name_out = f"{name}{out_suffix}"
    else:
        name_out = name
    ext = ".mov"
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
            for chunk in p.stdout:
                if STOP_EVENT.is_set():
                    p.kill()
                    return 130, "interrupted", round(time.time()-start,1)
                if chunk is None: continue
                f.write(chunk)
                if show_progress:
                    try:
                        line = chunk.decode(errors="ignore").strip()
                    except Exception:
                        line = ""
                    if "time=" in line:
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

    cmds = task.get("ffmpeg_cmds") or [task["ffmpeg_cmd"]]
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
        return 130, "interrupted", total_dur, used_cmd, used_log
    if rc != 0 and task.get("encoder") in {"nvenc", "qsv", "amf"} and not task.get("custom_params") and len(cmds) == 1:
        sw_opts = _normalize_sw_fallback_opts(task.get("opts", {}))
        fallback_cmd = build_ffmpeg_cmd(Path(task["src"]), Path(task["dst"]), sw_opts, custom_params=None)
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
            return rc2, f"fallback-ok: {task.get('encoder')} -> cpu", total_dur, fallback_cmd, fallback_log
        return rc2, f"fallback-failed: primary={rc} secondary={rc2}; {note2 or note}", total_dur, fallback_cmd, fallback_log

    return rc, note, total_dur, used_cmd, used_log


def execute_tasks(tasks, concurrency, work_root: Path, result_csv: Path, logs_root: Path, timeout=None, show_progress=True):
    STOP_EVENT.clear()
    total = len(tasks); lock = threading.Lock()
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
        except KeyboardInterrupt:
            STOP_EVENT.set()
            _terminate_active_procs()
            ex.shutdown(wait=False, cancel_futures=True)
            print("Interrupted by user (Ctrl+C). All transcoding processes are being stopped.")
            raise SystemExit(130)
    return

# ---------------- main ----------------
def main():
    preset_epilog = "\nPRESETS:\n"
    for k,v in PRESETS_INFO.items():
        preset_epilog += f"  {k}: {v['name']} -> {v['desc']}\n"
    preset_epilog += "\nQUALITY ORDER (high -> low):\n  x265 slow 2pass > NVENC P7 > QSV TU1 > QSV TU2 > QSV TU3 > NVENC P1\n"
    examples = """
EXAMPLES:
  Query encoder params only:
    transcode_hw_main.py --query-params nvenc --work ./work

  Single-file quick transcode using preset:
    transcode_hw_main.py --src ./video.mp4 --use-preset preset1

  Directory using preset (default no-group, output to <src>_comp):
    transcode_hw_main.py --src F:/Movies/Batch --use-preset preset5

  Directory with grouping and interactive per-group config:
    transcode_hw_main.py --src F:/Movies/Batch --dst F:/Out --work F:/Work --group

  Skip interactive prompts but still show suggested params (skip bitrate and encoder prompt):
    transcode_hw_main.py --src F:/Movies/Batch --work F:/Work --skip --skip-bitrate --skip-encfmt
"""
    parser = argparse.ArgumentParser(description="transcode_hw_main: hw-accelerated batch transcode (nvenc/qsv/amf). Default: grouping ON. Use --no-group to treat all as 1 group.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=preset_epilog + examples)
    parser.add_argument("--src", help="源路径（文件或目录）。可为单个文件或目录。若省略且只执行 --query-params，则可不指定。")
    parser.add_argument("--dst", help="目标根目录（默认：当使用 preset 并且未传 dst 时，自动使用 <src>_comp，单文件输出为 <name>_comp.ext）。")
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
    parser.add_argument("--use-preset", choices=list(PRESETS_INFO.keys()), default=None, help="使用快速预设（会覆盖其他参数并默认不分组）")
    parser.add_argument("--group", action="store_true", help="与 --use-preset 一起使用时强制启用分组（默认 use-preset 时不分组）")
    parser.add_argument("--no-group", action="store_true", help="跳过分组（将所有文件视为单组）；默认分组开启。")
    parser.add_argument("--custom-params", default=None, help="自定义 ffmpeg 参数字符串（直接插入到 -i <input> 之后）。不要包含输出文件名。")
    parser.add_argument("--query-params", choices=["nvenc","qsv","amf"], default=None, help="查询 ffmpeg encoder 参数并打印，支持单独运行（只需 --query-params 和 --work）。")
    parser.add_argument("--concurrency", type=int, default=1, help="并发 ffmpeg 进程数")
    parser.add_argument("--timeout", type=int, default=None, help="每任务超时（秒），默认无超时")
    parser.add_argument("--skip", action="store_true", help="跳过交互（采用建议参数并直接执行）。默认不跳过。")
    parser.add_argument("--skip-bitrate", action="store_true", help="skip 时忽略码率交互/修改（接受建议）")
    parser.add_argument("--skip-res", action="store_true", help="skip 时忽略分辨率交互/修改")
    parser.add_argument("--skip-encfmt", action="store_true", help="skip 时忽略编码格式（hevc/h264）交互/修改")
    parser.add_argument("--skip-hwaccel", action="store_true", help="skip 时忽略硬件加速器选择（nvenc/qsv/amf）交互/修改")
    parser.add_argument("--skip-builtin-checks", action="store_true", help="跳过部分内建检查（用于非交互/CI）")
    parser.add_argument("--show-groups-only", action="store_true", help="仅显示分组与 preflight CSV，然后退出")
    parser.add_argument("--flat-output", action="store_true", help="所有输出放在同一目标目录（不保留原始目录结构）；若冲突自动在文件名加源目录名后缀")
    parser.add_argument("--out-suffix", default="", help="输出文件名后缀（例如 _comp 或 preset1）。若值为 preset1..preset8，则后缀会被替换为该 preset 的描述文本。")
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
    if args.query_params and not args.src and not args.work:
        # require only work to save file; if not provided, use cwd/work_query
        work = Path(args.work) if args.work else Path.cwd().joinpath("transcode_hw_main_work")
        work.mkdir(parents=True, exist_ok=True)
        query_encoder(args.query_params, work)
        print("Query finished. Exiting.")
        sys.exit(0)

    # require src and work at minimum for normal flow
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

    # if query requested plus src/work provided, do query as well
    if args.query_params:
        query_encoder(args.query_params, work_root)
        # continue normal flow (user may want both)

    # detect single-file vs dir
    is_single_file = src.is_file()
    # default dst handling when use-preset without dst: auto <src>_comp
    if args.dst:
        dst_root = Path(args.dst).expanduser().resolve()
    else:
        if args.use_preset:
            if is_single_file:
                dst_root = src.parent  # single file -> output sibling (naming handled later)
            else:
                dst_root = src.parent.joinpath(f"{src.name}_comp")
        else:
            # require dst if not using preset
            print("Note: --dst not provided. Defaulting work sibling folder for outputs.")
            dst_root = src.parent.joinpath(f"{src.name}_comp")
    dst_root.mkdir(parents=True, exist_ok=True)

    # collect filters
    exts = [('.' + e.strip().lstrip('.').lower()) for e in args.extensions.split(",") if e.strip()]
    prefixes = [p for p in (x.strip() for x in args.prefixes.split(",") if x.strip())] if args.prefixes else []
    suffixes = [s for s in (x.strip() for x in args.suffixes.split(",") if x.strip())] if args.suffixes else []

    def make_task(srcp: Path, outp: Path, group_id, opts: dict, custom_params=None, encoder_label=None, ffmpeg_cmds=None, src_duration_sec=None):
        opts_local = dict(opts or {})
        enc_name = str(opts_local.get("encoder", "")).lower()
        manual_quality_override = (
            (enc_name == "nvenc" and args.nvenc_qual is not None) or
            (enc_name == "qsv" and args.qsv_qual is not None) or
            (enc_name == "amf" and args.amf_qual is not None)
        )
        if manual_quality_override:
            opts_local["manual_quality_override"] = True
        cmd = build_ffmpeg_cmd(srcp, outp, opts_local, custom_params=custom_params)
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
    src_duration_map = {str(e.get("path")): _safe_float(e.get("duration")) for e in entries}
    force_mov_map = {str(Path(e.get("path"))): _should_force_mov_output(Path(e.get("path"))) for e in entries}
    write_csv(preflight_csv, ["path","width","height","fps","codec","duration","bitrate"], entries)
    # write groups summary
    grp_rows = []
    for g in groups:
        grp_rows.append({"group":g["group_id"], "width":g["width"], "height":g["height"], "fps":g["fps"], "count":len(g["files"])})
    write_csv(groups_csv, ["group","width","height","fps","count"], grp_rows)
    write_csv(pre_media_csv, ["path","width","height","fps","codec","duration","bitrate"], entries)
    print("Preflight and media info written to work folder.")

    # handle out-suffix preset name mapping
    out_suffix = args.out_suffix or ""
    if out_suffix in PRESETS_INFO:
        out_suffix = "_" + PRESETS_INFO[out_suffix]["name"]
    elif out_suffix:
        # normalize to begin with underscore
        if not out_suffix.startswith("_"): out_suffix = "_" + out_suffix

    # Determine execution mode: preset/custom/interactive
    chosen_mode = "cli"
    if args.custom_params and args.use_preset:
        print("Both --use-preset and --custom-params provided. Choose: [1] preset, [2] custom, [3] cancel")
        c = input("choice (1/2/3) [3]: ").strip()
        if c == "1": chosen_mode = "preset"
        elif c == "2": chosen_mode = "custom"
        else: print("Cancelled"); sys.exit(0)
    elif args.custom_params:
        chosen_mode = "custom"
    elif args.use_preset:
        chosen_mode = "preset"
    else:
        chosen_mode = "interactive"

    # if use_preset and not group flag: default no-group
    grouping_enabled = True
    if chosen_mode == "preset" and not args.group:
        grouping_enabled = False
    if args.no_group:
        grouping_enabled = False

    # build tasks
    tasks = []
    def preset_to_opts(key):
        if key == "preset1":
            return {"encoder":"nvenc","codec":"hevc","preset":"p7","rc_mode":"vbr","br_min":30,"br_max":40,
                    "audio_bitrate":320,"scale":"same",
                    "extra":"-tune hq -profile:v main10 -pix_fmt p010le -rc vbr_hq -look_ahead 1 -look_ahead_depth 32 -bf 4 -b_ref_mode middle -g 240 -spatial_aq 1 -temporal_aq 1 -aq-strength 9"}
        if key == "preset2":
            return {"encoder":"qsv","codec":"hevc","rc_mode":"vbr","br_min":6,"br_max":8,
                    "audio_bitrate":320,"scale":"same",
                    "extra":"-tu 1 -look_ahead 1 -look_ahead_depth 30 -bf 3 -g 120 -profile:v main"}
        if key == "preset3":
            return {"encoder":"nvenc","codec":"hevc","preset":"p7","rc_mode":"vbr","br_min":30,"br_max":40,
                    "audio_bitrate":320,"scale":"same",
                    "extra":"-tune hq -profile:v main10 -pix_fmt p010le -rc vbr_hq -multipass fullres -look_ahead 1 -look_ahead_depth 32 -bf 4 -b_ref_mode middle -g 240"}
        if key == "preset4":
            return {"encoder":"cpu","codec":"hevc","preset":"slow","rc_mode":"vbr","br_min":6,"br_max":6,
                    "audio_bitrate":320,"scale":"same","pass_mode":2}
        if key == "preset5":
            return {"encoder":"nvenc","codec":"h264","preset":"p1","rc_mode":"cbr","br_min":5,"br_max":5,
                    "audio_bitrate":128,"scale":"half",
                    "extra":"-tune ll -g 60 -bf 2"}
        if key == "preset6":
            return {"encoder":"nvenc","codec":"h264","preset":"p1","rc_mode":"cbr","br_min":5,"br_max":5,
                    "audio_bitrate":128,"scale":"same",
                    "extra":"-tune ll -profile:v high -level 5.1 -g 60 -bf 2"}
        if key == "preset7":
            return {"encoder":"qsv","codec":"hevc","rc_mode":"icq","cqp":28,"audio_bitrate":320,"scale":"half",
                    "extra":"-tu 3 -look_ahead 1 -look_ahead_depth 30 -bf 3 -g 120"}
        if key == "preset8":
            return {"encoder":"qsv","codec":"hevc","rc_mode":"icq","cqp":27,"audio_bitrate":320,"scale":"same",
                    "extra":"-tu 2 -look_ahead 1 -look_ahead_depth 40 -bf 4 -g 240"}
        return {}

    def preset4_two_pass_cmds(srcp: Path, outp: Path):
        passlog = outp.parent.joinpath(outp.stem + ".x265_2pass")
        null_sink = "NUL" if sys.platform.startswith("win") else "/dev/null"
        cmd1 = ["ffmpeg","-y","-hide_banner","-loglevel","info","-i",str(srcp),
                "-c:v","libx265","-preset","slow","-b:v","6M","-pass","1",
                "-x265-params","rc-lookahead=40:aq-mode=3","-passlogfile",str(passlog),
                "-an","-f","null",null_sink]
        cmd2 = ["ffmpeg","-y","-hide_banner","-loglevel","info","-i",str(srcp)]
        cmd2 += _stream_copy_and_metadata_args(srcp, outp)
        cmd2 += _audio_codec_args_for_output(srcp, outp)
        cmd2 += _extra_stream_codec_args_for_output(outp)
        cmd2 += ["-c:v","libx265","-preset","slow","-b:v","6M","-pass","2",
                "-x265-params","rc-lookahead=60:aq-mode=3:aq-strength=0.9:psy-rd=2.0","-passlogfile",str(passlog),
                str(outp)]
        return [cmd1, cmd2]

    if chosen_mode == "preset":
        opts_map = preset_to_opts(args.use_preset)
        # if single file: output to same dir with original name/suffix; add _comp on conflict; logs into same-named _logs
        if is_single_file:
            outp = src.parent.joinpath(f"{src.stem}{out_suffix}{src.suffix}")
            outp = _apply_container_policy(src, outp)
            outp = _resolve_output_conflict(outp, src)
            log_root = src.parent.joinpath(f"{src.stem}_logs")
            tasks.append(make_task(src, outp, 0, opts_map, ffmpeg_cmds=preset4_two_pass_cmds(src, outp) if args.use_preset == "preset4" else None, src_duration_sec=src_duration_map.get(str(src))))
            work_logs_root = log_root
        else:
            work_logs_root = dst_root.parent.joinpath(f"{dst_root.name}_logs") if args.dst is None else dst_root.parent.joinpath(f"{dst_root.name}_logs")
            # if grouping disabled: iterate all files, apply same opts
            if not grouping_enabled:
                for f in files:
                    srcp = Path(f)
                    outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix)
                    outp = _apply_container_policy(srcp, outp)
                    outp = _resolve_output_conflict(outp, srcp)
                    tasks.append(make_task(srcp, outp, 0, opts_map, ffmpeg_cmds=preset4_two_pass_cmds(srcp, outp) if args.use_preset == "preset4" else None, src_duration_sec=src_duration_map.get(str(srcp))))
            else:
                # grouping enabled: build per-group tasks but with same preset applied per-file
                entries_groups = groups
                for g in entries_groups:
                    for f in g["files"]:
                        srcp = Path(f["path"])
                        outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix)
                        outp = _apply_container_policy(srcp, outp)
                        outp = _resolve_output_conflict(outp, srcp)
                        tasks.append(make_task(srcp, outp, g["group_id"], opts_map, ffmpeg_cmds=preset4_two_pass_cmds(srcp, outp) if args.use_preset == "preset4" else None, src_duration_sec=src_duration_map.get(str(srcp))))
    elif chosen_mode == "custom":
        if not args.custom_params:
            print("custom mode selected but no --custom-params given. Exiting."); sys.exit(2)
        # single file or many: apply custom params directly
        for f in files:
            srcp = Path(f)
            if is_single_file:
                outp = srcp.parent.joinpath(f"{srcp.stem}{out_suffix}{srcp.suffix}")
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
            # build suggested defaults and skip interactions according to skip flags
            for g in groups:
                rc_mode = "cqp"
                cfg = {"encoder": args.encoder if not args.skip_hwaccel else args.encoder, "codec": args.codec,
                       "rc_mode": rc_mode, "preset": args.preset, "br_min": None, "br_max": None,
                       "cqp": 24,
                       "audio_bitrate":320, "scale":"same", "extra":""}
                # skip specifics: if skip-bitrate then keep br_min/br_max as suggested; if skip-res skip scale edit (we already not interactive)
                group_configs[g["group_id"]] = cfg
            # print summary
            print("Skip mode: will apply the following per-group configs (suggested):")
            for k,v in group_configs.items():
                print(f"  Group {k}: encoder={v['encoder']}, codec={v['codec']}, rc={v['rc_mode']}, cqp={v['cqp']}, preset={v['preset']}, scale={v['scale']}")
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
                    outp = srcp.parent.joinpath(f"{srcp.stem}{out_suffix}{srcp.suffix}")
                else:
                    outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix)
                outp = _apply_container_policy(srcp, outp)
                outp = _resolve_output_conflict(outp, srcp)
                tasks.append(make_task(srcp, outp, g["group_id"], cfg, src_duration_sec=src_duration_map.get(str(srcp))))

    # write preflight tasks
    with tasks_json.open("w", encoding='utf-8') as jf:
        json.dump(tasks, jf, indent=2, ensure_ascii=False)
    print(f"Tasks preflight saved to: {tasks_json}  (total: {len(tasks)})")
    # confirm if not skip
    if not args.skip and chosen_mode != "preset":
        c = input("Proceed to execute tasks now? (y/N): ").strip().lower()
        if c != "y":
            print("Aborted"); sys.exit(0)
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
    execute_tasks(tasks, args.concurrency, work_root, result_csv, logs_dir, timeout=args.timeout, show_progress=True)
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
    print("Done. Results:", result_csv)

if __name__ == "__main__":
    main()
