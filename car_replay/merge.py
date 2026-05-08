"""concat 列表生成、stream-copy 合并、合并+压缩一体化。"""

from __future__ import annotations

import os
import shutil
import subprocess

from .compress import compress_video
from .config import FFMPEG, format_size, get_compress_profile
from .ffmpeg_runner import _run_ffmpeg_capturing_warnings
from .naming import _basename, extract_camera_id


def _concat_file_line(video_path):
    concat_path = os.path.abspath(video_path)
    if os.name == "nt":
        concat_path = concat_path.replace("\\", "/")
    concat_path = concat_path.replace("'", "'\\''")
    return f"file '{concat_path}'\n"


def _write_concat_list(concat_list_path, video_group):
    with open(concat_list_path, "w") as f:
        for video in video_group:
            f.write(_concat_file_line(video))


def _copy_merge_videos(video_group, combined_file):
    concat_list_path = combined_file + ".concat_list.txt"
    _write_concat_list(concat_list_path, video_group)

    try:
        command = [
            FFMPEG, "-y",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c", "copy",
            "-movflags", "+faststart",
            combined_file,
        ]
        returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(command)
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command)
        return elapsed, tracker
    finally:
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)


def merge_videos(video_group, combined_file, enable_compress=False, cq_override=None, warning_collector=None, duration_resolver=None):
    # 获取最后一个视频文件的时间属性
    last_video = video_group[-1]
    last_video_stats = os.stat(last_video)
    last_access_time = last_video_stats.st_atime
    last_mod_time = last_video_stats.st_mtime

    # 获取通道ID（从第一个视频文件名提取）
    camera_id = extract_camera_id(_basename(video_group[0]))

    # 预扫健康度：任一输入文件不健康则该组改走 -c copy（避免 NVENC 暂停画面问题）
    if enable_compress and duration_resolver is not None:
        unhealthy = [v for v in video_group if duration_resolver.is_healthy(v) is False]
        if unhealthy:
            print(
                f"  Group has {len(unhealthy)} unhealthy input(s) "
                f"e.g. {_basename(unhealthy[0])}; falling back to -c copy for safety"
            )
            enable_compress = False

    if len(video_group) == 1 and enable_compress:
        # 单文件 + 压缩：直接从源压缩到目标
        print(f"Compressing single file: {video_group[0]} to {combined_file}")
        success, in_sz, out_sz, elapsed, tracker = compress_video(video_group[0], combined_file, camera_id, cq_override)
        if warning_collector is not None and tracker is not None:
            warning_collector.append((combined_file, tracker))
        if success:
            os.utime(combined_file, (last_access_time, last_mod_time))
            return in_sz, out_sz, elapsed
        else:
            # 压缩失败时回退到无压缩输出；.ts 需要 remux 到 mp4，不能直接复制
            print("  Compression failed, falling back to copy/remux...")
            if os.path.splitext(video_group[0])[1].lower() == os.path.splitext(combined_file)[1].lower():
                shutil.copy2(video_group[0], combined_file)
            else:
                _copy_merge_videos(video_group, combined_file)
            return 0, 0, 0

    if len(video_group) == 1 and not enable_compress and os.path.splitext(video_group[0])[1].lower() == ".mp4":
        # 单文件 + 不压缩：直接复制
        print(f"Copying single file: {video_group[0]} to {combined_file}")
        shutil.copy2(video_group[0], combined_file)
        return 0, 0, 0

    print(f"Merging {len(video_group)} files into: {combined_file}")
    print(f"Files to merge: {[_basename(v) for v in video_group]}")

    # 写入 concat 列表
    concat_list_path = combined_file + ".concat_list.txt"
    _write_concat_list(concat_list_path, video_group)

    if enable_compress:
        # 合并+压缩一步完成：concat 直接输入到 hevc_nvenc
        profile = get_compress_profile(camera_id, cq_override)
        input_size = sum(os.path.getsize(v) for v in video_group)
        print(f"  Merge+Compress [{camera_id or '??'}] CQ{profile['cq']} ({len(video_group)} files)...")

        temp_output = combined_file + ".compress_tmp.mp4"
        cmd = [
            FFMPEG,
            "-y",
            "-hwaccel", "cuda",
            "-fflags", "+genpts+igndts",
            "-err_detect", "ignore_err",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c:v", "hevc_nvenc",
            "-preset", profile["preset"],
            "-rc", "vbr",
            "-cq", str(profile["cq"]),
            "-b:v", profile["bitrate"],
            "-maxrate", profile["maxrate"],
            "-bufsize", profile["bufsize"],
            "-multipass", "fullres",
            "-rc-lookahead", "32",
            "-spatial-aq", "1",
            "-temporal-aq", "1",
            "-bf", "4",
            "-b_ref_mode", "middle",
            "-c:a", "copy",
            "-movflags", "+faststart",
            temp_output,
        ]

        print(f"  CMD: {' '.join(cmd)}")
        returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(cmd)

        os.remove(concat_list_path)

        if warning_collector is not None:
            warning_collector.append((combined_file, tracker))

        if returncode != 0:
            if os.path.exists(temp_output):
                os.remove(temp_output)
            print(f"  ERROR: Merge+Compress failed!")
            return 0, 0, 0

        if os.path.exists(combined_file):
            os.remove(combined_file)
        os.rename(temp_output, combined_file)

        output_size = os.path.getsize(combined_file)
        ratio = input_size / output_size if output_size > 0 else 0
        saving = (1 - output_size / input_size) * 100 if input_size > 0 else 0
        print(
            f"  Compressed: {format_size(input_size)} -> {format_size(output_size)} "
            f"({ratio:.1f}x ratio, -{saving:.0f}%) in {elapsed:.1f}s"
        )

        # 设置时间属性
        os.utime(combined_file, (last_access_time, last_mod_time))
        return input_size, output_size, elapsed
    else:
        # 不压缩：仅 stream copy 合并；.ts 也会 remux 到 .mp4 输出
        elapsed, tracker = _copy_merge_videos(video_group, combined_file)
        if warning_collector is not None:
            warning_collector.append((combined_file, tracker))
        print("Merge complete.")

    # 设置合并后文件的时间属性为最后一个视频文件的时间属性
    os.utime(combined_file, (last_access_time, last_mod_time))
    return 0, 0, 0
