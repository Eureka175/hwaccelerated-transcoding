#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcode_hw_main.py - 硬件转码工具 (nvenc/qsv/amf)，增强版

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

# ---------------- presets ----------------
PRESETS_INFO = OrderedDict([
    ("preset1", {"name":"4k_prog_archive_1pass", "desc":"HEVC NVENC@P7, 1pass, vbr_hq(30/40), main10 p010, aq+lookahead"}),
    ("preset2", {"name":"1080p_prog_rel_1pass", "desc":"HEVC QSV@TU1, 1pass, VBR(6/8), lookahead, aac@320k"}),
    ("preset3", {"name":"4k_prog_archive_2pass", "desc":"HEVC NVENC@P7, multipass fullres, vbr_hq(30/40)"}),
    ("preset4", {"name":"1080p_prog_rel_2pass", "desc":"HEVC x265 slow, 2pass @6M"}),
    ("preset5", {"name":"fast_proxy_gen_halfres_avc_5m", "desc":"AVC NVENC@P1, tune ll, CBR 5M, half res, aac@128k"}),
    ("preset6", {"name":"fast_proxy_gen_fullres_avc_5m", "desc":"AVC NVENC@P1, tune ll, CBR 5M, full res, profile high"}),
    ("preset7", {"name":"social_plat_share_halfres", "desc":"HEVC QSV@TU3, ICQ 28, lookahead, half res, aac@320k"}),
    ("preset8", {"name":"social_plat_share_fullres", "desc":"HEVC QSV@TU2, ICQ 27, lookahead, full res, aac@320k"}),
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


def build_ffmpeg_cmd(input_path: Path, output_path: Path, opts: dict, custom_params: str=None):
    base = ["ffmpeg","-y","-hide_banner","-loglevel","info","-i", str(input_path)]
    if custom_params:
        extra = shlex.split(custom_params)
        return base + extra + [str(output_path)]
    # audio
    ab = opts.get("audio_bitrate",320)
    cmd = base.copy()
    # scale
    scale = opts.get("scale")
    if scale:
        if scale == "half":
            cmd += ["-vf","scale=iw/2:ih/2"]
        elif scale == "same":
            pass
        else:
            cmd += ["-vf", f"scale={scale}"]
    # audio
    cmd += ["-c:a","aac","-b:a",f"{int(ab)}k"]
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
            cmd += ["-qp", str(cqp)]
        elif rc=="vbr" and br_min:
            cmd += ["-b:v", human_bitrate(br_min)]
    if opts.get("extra"):
        cmd += shlex.split(opts.get("extra"))
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
    ext = src.suffix
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

# ---------------- execution ----------------
def _run_and_log(cmd, logp: Path, timeout=None):
    start = time.time()
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        logp.parent.mkdir(parents=True, exist_ok=True)
        logp.write_text("ffmpeg not found\n", encoding='utf-8')
        return 127, "ffmpeg-not-found", 0.0
    logp.parent.mkdir(parents=True, exist_ok=True)
    with logp.open("wb") as f:
        try:
            for chunk in p.stdout:
                if chunk is None: continue
                f.write(chunk)
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            return -9, "timeout", round(time.time()-start,1)
    return p.returncode, "", round(time.time()-start,1)

def _execute_single_task(task, logs_root: Path, work_root: Path, timeout=None):
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
        rc, note, dur = _run_and_log(cmd, step_log, timeout)
        total_dur += dur
        used_cmd = cmd
        used_log = step_log
        if rc != 0:
            break

    # Robust fallback: hw encoder failed -> retry once with software encoder.
    if rc != 0 and task.get("encoder") in {"nvenc", "qsv", "amf"} and not task.get("custom_params") and len(cmds) == 1:
        sw_opts = _normalize_sw_fallback_opts(task.get("opts", {}))
        fallback_cmd = build_ffmpeg_cmd(Path(task["src"]), Path(task["dst"]), sw_opts, custom_params=None)
        fallback_log = primary_log.with_name(primary_log.stem + ".fallback.log")
        rc2, note2, dur2 = _run_and_log(fallback_cmd, fallback_log, timeout)
        total_dur += dur2
        if rc2 == 0:
            return rc2, f"fallback-ok: {task.get('encoder')} -> cpu", total_dur, fallback_cmd, fallback_log
        return rc2, f"fallback-failed: primary={rc} secondary={rc2}; {note2 or note}", total_dur, fallback_cmd, fallback_log

    return rc, note, total_dur, used_cmd, used_log


def execute_tasks(tasks, concurrency, work_root: Path, result_csv: Path, logs_root: Path, timeout=None):
    total = len(tasks); lock = threading.Lock()
    headers = ["src","dst","group","encoder","codec","preset","rc_mode","br_min","br_max","cqp","ffmpeg_cmd","log","returncode","note","secs"]
    if result_csv.exists(): result_csv.unlink()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {}
        for t in tasks:
            futures[ex.submit(_execute_single_task, t, logs_root, work_root, timeout)] = t
        completed = 0
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
    args = parser.parse_args()

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

    def make_task(srcp: Path, outp: Path, group_id, opts: dict, custom_params=None, encoder_label=None, ffmpeg_cmds=None):
        cmd = build_ffmpeg_cmd(srcp, outp, opts or {}, custom_params=custom_params)
        task = {
            "src": str(srcp),
            "dst": str(outp),
            "group": group_id,
            "encoder": encoder_label if encoder_label is not None else (opts or {}).get("encoder", ""),
            "codec": (opts or {}).get("codec", ""),
            "preset": (opts or {}).get("preset", ""),
            "rc_mode": (opts or {}).get("rc_mode", ""),
            "br_min": (opts or {}).get("br_min", ""),
            "br_max": (opts or {}).get("br_max", ""),
            "cqp": (opts or {}).get("cqp", ""),
            "opts": opts or {},
            "custom_params": custom_params,
            "ffmpeg_cmd": cmd,
        }
        if ffmpeg_cmds:
            task["ffmpeg_cmds"] = ffmpeg_cmds
        return task

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
        cmd2 = ["ffmpeg","-y","-hide_banner","-loglevel","info","-i",str(srcp),
                "-c:v","libx265","-preset","slow","-b:v","6M","-pass","2",
                "-x265-params","rc-lookahead=60:aq-mode=3:aq-strength=0.9:psy-rd=2.0","-passlogfile",str(passlog),
                "-c:a","aac","-b:a","320k",str(outp)]
        return [cmd1, cmd2]

    if chosen_mode == "preset":
        opts_map = preset_to_opts(args.use_preset)
        # if single file: output to same dir with _comp suffix; logs into same-named _logs
        if is_single_file:
            outp = src.parent.joinpath(f"{src.stem}{out_suffix}_comp{src.suffix}")
            log_root = src.parent.joinpath(f"{src.stem}_logs")
            tasks.append(make_task(src, outp, 0, opts_map, ffmpeg_cmds=preset4_two_pass_cmds(src, outp) if args.use_preset == "preset4" else None))
            work_logs_root = log_root
        else:
            work_logs_root = dst_root.parent.joinpath(f"{dst_root.name}_logs") if args.dst is None else dst_root.parent.joinpath(f"{dst_root.name}_logs")
            # if grouping disabled: iterate all files, apply same opts
            if not grouping_enabled:
                for f in files:
                    srcp = Path(f)
                    outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix+"_comp")
                    tasks.append(make_task(srcp, outp, 0, opts_map, ffmpeg_cmds=preset4_two_pass_cmds(srcp, outp) if args.use_preset == "preset4" else None))
            else:
                # grouping enabled: build per-group tasks but with same preset applied per-file
                entries_groups = groups
                for g in entries_groups:
                    for f in g["files"]:
                        srcp = Path(f["path"])
                        outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix+"_comp")
                        tasks.append(make_task(srcp, outp, g["group_id"], opts_map, ffmpeg_cmds=preset4_two_pass_cmds(srcp, outp) if args.use_preset == "preset4" else None))
    elif chosen_mode == "custom":
        if not args.custom_params:
            print("custom mode selected but no --custom-params given. Exiting."); sys.exit(2)
        # single file or many: apply custom params directly
        for f in files:
            srcp = Path(f)
            if is_single_file:
                outp = srcp.parent.joinpath(f"{srcp.stem}{out_suffix}_comp{srcp.suffix}")
            else:
                outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix+"_comp")
            tasks.append(make_task(srcp, outp, 0, {}, custom_params=args.custom_params, encoder_label="custom"))
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
                w,h = g["width"], g["height"]
                # rules: width>=3840 -> 30-50; width>=1920 -> 10-20; else 5-10
                if w and w>=3840:
                    br_min, br_max = 30,50
                elif w and w>=1920:
                    br_min, br_max = 10,20
                else:
                    br_min, br_max = 5,10
                cfg = {"encoder": args.encoder if not args.skip_hwaccel else args.encoder, "codec": args.codec,
                       "rc_mode": args.rc_mode or "vbr", "preset": args.preset, "br_min": args.min_br or br_min, "br_max": args.max_br or br_max,
                       "cqp": args.cqp, "audio_bitrate":320, "scale":"same", "extra":""}
                # skip specifics: if skip-bitrate then keep br_min/br_max as suggested; if skip-res skip scale edit (we already not interactive)
                group_configs[g["group_id"]] = cfg
            # print summary
            print("Skip mode: will apply the following per-group configs (suggested):")
            for k,v in group_configs.items():
                print(f"  Group {k}: encoder={v['encoder']}, codec={v['codec']}, rc={v['rc_mode']}, br={v['br_min']}-{v['br_max']}, preset={v['preset']}, scale={v['scale']}")
            if not args.skip_builtin_checks:
                c = input("Confirm and proceed? (y/N): ").strip().lower()
                if c != "y": print("Aborted"); sys.exit(0)
        else:
            # interactive: prompt per-group
            for g in groups:
                gid = g["group_id"]
                w,h = g["width"], g["height"]
                if w and w>=3840: br_sugg=(30,50)
                elif w and w>=1920: br_sugg=(10,20)
                else: br_sugg=(5,10)
                print(f"\nGroup {gid}: {w}x{h} @ {g['fps']} fps  files:{len(g['files'])}")
                print(f"Suggested bitrate: {br_sugg[0]}-{br_sugg[1]} Mbps")
                enc = input(f"  encoder [{args.encoder}]: ").strip() or args.encoder
                codec = input(f"  codec [{args.codec}]: ").strip() or args.codec
                rc = input(f"  rc_mode [vbr]: ").strip() or "vbr"
                preset = input(f"  preset [{args.preset or ''}]: ").strip() or args.preset
                brmin = input(f"  br_min ({br_sugg[0]}): ").strip()
                brmax = input(f"  br_max ({br_sugg[1]}): ").strip()
                brmin = float(brmin) if brmin else br_sugg[0]
                brmax = float(brmax) if brmax else br_sugg[1]
                cqp = input("  cqp (leave empty if none): ").strip()
                cqp = int(cqp) if cqp else None
                scale = input("  scale (e.g. 1920x1080/half/same) [same]: ").strip() or "same"
                group_configs[gid] = {"encoder":enc,"codec":codec,"rc_mode":rc,"preset":preset,"br_min":brmin,"br_max":brmax,"cqp":cqp,"audio_bitrate":320,"scale":scale,"extra":""}
        # build tasks from group_configs
        for g in groups:
            cfg = group_configs.get(g["group_id"])
            for f in g["files"]:
                srcp = Path(f["path"])
                if is_single_file:
                    outp = srcp.parent.joinpath(f"{srcp.stem}{out_suffix}_comp{srcp.suffix}")
                else:
                    outp = make_output_path(srcp, src, dst_root, flat_output=args.flat_output, out_suffix=out_suffix+"_comp")
                tasks.append(make_task(srcp, outp, g["group_id"], cfg))

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
    execute_tasks(tasks, args.concurrency, work_root, result_csv, logs_dir, timeout=args.timeout)
    # post media info
    out_entries = []
    for t in tasks:
        dstp = Path(t["dst"])
        info = probe_media(dstp) or {"width":None,"height":None,"codec":"","fps":0.0,"duration":"","bitrate":""}
        out_entries.append({"path":str(dstp),"width":info["width"],"height":info["height"],"fps":info["fps"],"codec":info["codec"],"duration":info.get("duration"),"bitrate":info.get("bitrate")})
    write_csv(post_media_csv, ["path","width","height","fps","codec","duration","bitrate"], out_entries)
    print("Post media info written to:", post_media_csv)
    print("Done. Results:", result_csv)

if __name__ == "__main__":
    main()
