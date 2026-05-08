"""单文件 NVENC 压缩。"""

from __future__ import annotations

import os

from .config import FFMPEG, format_size, get_compress_profile
from .ffmpeg_runner import _run_ffmpeg_capturing_warnings


def compress_video(input_path, output_path, camera_id, cq_override=None):
    """
    使用 hevc_nvenc 压缩视频文件。
    input_path: 输入文件（合并后的临时文件或单个源文件）
    output_path: 最终输出路径
    camera_id: 通道ID，用于选择压缩参数
    返回 True/False
    """
    profile = get_compress_profile(camera_id, cq_override)
    input_size = os.path.getsize(input_path)

    print(f"  Compressing [{camera_id or '??'}] CQ{profile['cq']}...")

    temp_output = output_path + ".compress_tmp.mp4"

    cmd = [
        FFMPEG,
        "-y",
        "-hwaccel", "cuda",
        "-fflags", "+genpts+igndts",
        "-err_detect", "ignore_err",
        "-i", input_path,
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

    if returncode != 0:
        if os.path.exists(temp_output):
            os.remove(temp_output)
        print(f"  ERROR: Compression failed!")
        return False, 0, 0, elapsed, tracker

    # 重命名为最终文件
    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(temp_output, output_path)

    output_size = os.path.getsize(output_path)
    ratio = input_size / output_size if output_size > 0 else 0
    saving = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    print(
        f"  Compressed: {format_size(input_size)} -> {format_size(output_size)} "
        f"({ratio:.1f}x ratio, -{saving:.0f}%) in {elapsed:.1f}s"
    )
    return True, input_size, output_size, elapsed, tracker
