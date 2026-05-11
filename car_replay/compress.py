"""单文件 NVENC 压缩。"""

from __future__ import annotations

import os
from pathlib import Path

from . import console
from .config import FFMPEG, format_size, get_compress_profile
from .ffmpeg_runner import CommandResult, _run_ffmpeg_capturing_warnings


def compress_video(input_path, output_path, camera_id, cq_override=None,
                   expected_duration=None, verbose_ffmpeg=False,
                   verbose_cmd=False, src_folder=None):
    """
    使用 hevc_nvenc 压缩视频文件。
    input_path: 输入文件（合并后的临时文件或单个源文件）
    output_path: 最终输出路径
    camera_id: 通道ID，用于选择压缩参数
    expected_duration: 输入合计时长（秒），用于 post_validate 时长比对；None 则跳过比对
    返回 (success, in_sz, out_sz, elapsed, tracker)
    """
    profile = get_compress_profile(camera_id, cq_override)
    input_size = os.path.getsize(input_path)

    console.step(f"Compressing [{camera_id or '??'}] CQ{profile['cq']}...")

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

    console.cmd_line(cmd[0], cmd[1:], verbose=verbose_cmd, base_dir=src_folder)

    returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(
        cmd, verbose=verbose_ffmpeg, expected_duration=expected_duration,
        expected_input_bytes=input_size,
    )

    result = CommandResult(
        returncode=returncode, elapsed=elapsed, tracker=tracker,
        output_path=Path(temp_output), expected_duration=expected_duration,
    )

    if result.is_fatal() or result.is_suspicious():
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except OSError:
                pass
        if result.is_fatal():
            console.error(f"Compression failed (fatal: rc={returncode})!", indent=2)
        else:
            console.warn(
                f"Compression looks suspicious "
                f"({tracker.format_oneline()}), discarding output",
                indent=2,
            )
        return False, 0, 0, elapsed, tracker

    ok, reason = result.post_validate()
    if not ok:
        console.detail(f"[post-validate] {reason}; falling back")
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except OSError:
                pass
        return False, 0, 0, elapsed, tracker

    # 重命名为最终文件
    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(temp_output, output_path)

    output_size = os.path.getsize(output_path)
    ratio = input_size / output_size if output_size > 0 else 0
    saving = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    console.success(
        f"Compressed: {format_size(input_size)} → {format_size(output_size)} "
        f"({ratio:.1f}x, -{saving:.0f}%) in {elapsed:.1f}s",
        indent=2,
    )
    return True, input_size, output_size, elapsed, tracker
