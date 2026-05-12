"""单文件 NVENC 压缩 + 负压缩 pre-flight 预检。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import console
from .config import (
    FFMPEG,
    NEGATIVE_COMPRESSION_ABORT_RC,
    PREFLIGHT_BITRATE_MARGIN,
    X265_DEFAULT_CRF,
    _parse_profile_bitrate_to_bps,
    derive_h264_nvenc_profile,
    format_size,
    get_compress_profile,
)
from .ffmpeg_runner import CommandResult, _run_ffmpeg_capturing_warnings


@dataclass
class PreflightDecision:
    """codec-aware preflight 的统一决策结果。"""

    action: str               # "encode" 或 "copy"
    profile: Optional[dict]   # action="encode" 时必填；action="copy" 时为 None
    reason: str               # 人类可读描述
    count_as_negative: bool   # folder breaker 是否计数（规则性 skip = False）


def decide_compression(
    default_profile: dict,
    group_codec: Optional[str],
    group_bps: Optional[int],
    encoder: str = "nvenc",
) -> PreflightDecision:
    """codec-aware preflight 决策函数。

    决策规则（严格按 plan.md Phase 2 表格）：
    1. mixed codec group → COPY count=False
    2. codec/bitrate unknown → COPY count=False（fail-closed）
    3. hevc + input_bps ≤ target×MARGIN → COPY count=False
    4. hevc + input_bps > target×MARGIN → ENCODE default_profile
    5. 非 hevc + nvenc → ENCODE derive_h264_nvenc_profile(...)
    6. 非 hevc + x265 → ENCODE default_profile（CRF 模式）
    """
    # 1. 混合 codec
    if group_codec == "mixed":
        return PreflightDecision(
            action="copy", profile=None,
            reason="mixed codec group", count_as_negative=False,
        )

    # 2. metadata 未知
    if group_codec is None or group_bps is None or group_bps <= 0:
        return PreflightDecision(
            action="copy", profile=None,
            reason="metadata unknown, fail-closed", count_as_negative=False,
        )

    # 3 & 4. hevc
    if group_codec == "hevc":
        target_bps = _parse_profile_bitrate_to_bps(default_profile.get("bitrate", ""))
        if target_bps and group_bps <= target_bps * PREFLIGHT_BITRATE_MARGIN:
            return PreflightDecision(
                action="copy", profile=None,
                reason="hevc input bitrate at or below target", count_as_negative=False,
            )
        return PreflightDecision(
            action="encode", profile=default_profile,
            reason="hevc input bitrate above target", count_as_negative=False,
        )

    # 5 & 6. 非 hevc（h264 或其他已知 codec）
    if encoder == "nvenc":
        derived = derive_h264_nvenc_profile(default_profile, group_bps)
        return PreflightDecision(
            action="encode", profile=derived,
            reason=f"non-hevc ({group_codec}), nvenc derived profile",
            count_as_negative=False,
        )
    else:  # x265-veryslow
        return PreflightDecision(
            action="encode", profile=default_profile,
            reason=f"non-hevc ({group_codec}), x265 crf",
            count_as_negative=False,
        )


def preflight_should_skip_nvenc(
    video_group: Iterable[str],
    profile: dict,
    duration_resolver=None,
) -> Optional[str]:
    """检查输入平均码率是否已经低于 profile.bitrate × MARGIN，命中即返原因字符串。

    返回 None = 应正常进入 NVENC；返回字符串 = 应直接走 copy，字符串可记入日志。
    使用 duration_resolver 缓存的 duration（不会触发新的 ffprobe），文件大小用 os.path.getsize。
    任何信息不全 → 返回 None（保守不跳过）。
    """
    target_bps = _parse_profile_bitrate_to_bps(profile.get("bitrate", ""))
    if not target_bps or target_bps <= 0:
        return None
    threshold_bps = target_bps * PREFLIGHT_BITRATE_MARGIN
    total_bytes = 0
    total_secs = 0.0
    counted = 0
    for v in video_group:
        try:
            size = os.path.getsize(v)
        except OSError:
            continue
        dur = None
        if duration_resolver is not None:
            try:
                dur = duration_resolver.duration_for_file(v)
            except Exception:
                dur = None
        if dur is None or dur <= 0 or size <= 0:
            continue
        total_bytes += size
        total_secs += dur
        counted += 1
    if counted == 0 or total_secs <= 0:
        return None
    avg_bps = total_bytes * 8.0 / total_secs
    if avg_bps <= threshold_bps:
        return (
            f"input avg bitrate {avg_bps / 1_000_000:.2f}Mbps "
            f"<= target {target_bps / 1_000_000:.2f}Mbps "
            f"x {PREFLIGHT_BITRATE_MARGIN} ({counted} file(s) sampled)"
        )
    return None


def _build_video_codec_args(
    profile: dict, encoder: str = "nvenc", x265_crf: Optional[int] = None
) -> list:
    """构建 ffmpeg 视频编码参数列表。

    encoder="nvenc" → hevc_nvenc VBR 模式（使用 profile 中的 cq/bitrate/maxrate/bufsize）。
    encoder="x265-veryslow" → libx265 CRF 模式（不使用 bitrate/maxrate/bufsize）。
    """
    if encoder == "x265-veryslow":
        crf = x265_crf if x265_crf is not None else X265_DEFAULT_CRF
        return ["-c:v", "libx265", "-preset", "veryslow", "-crf", str(crf)]
    # nvenc
    return [
        "-c:v", "hevc_nvenc",
        "-preset", profile.get("preset", "p7"),
        "-rc", "vbr",
        "-cq", str(profile.get("cq", 32)),
        "-b:v", str(profile.get("bitrate", "5M")),
        "-maxrate", str(profile.get("maxrate", "8M")),
        "-bufsize", str(profile.get("bufsize", "10M")),
        "-multipass", "fullres",
        "-rc-lookahead", "32",
        "-spatial-aq", "1",
        "-temporal-aq", "1",
        "-bf", "4",
        "-b_ref_mode", "middle",
    ]


def compress_video(input_path, output_path, camera_id, cq_override=None,
                   expected_duration=None, verbose_ffmpeg=False,
                   verbose_cmd=False, src_folder=None,
                   profile_override: Optional[dict] = None,
                   encoder: str = "nvenc",
                   x265_crf: Optional[int] = None):
    """
    压缩视频文件（NVENC 或 x265）。
    input_path: 输入文件（合并后的临时文件或单个源文件）
    output_path: 最终输出路径
    camera_id: 通道ID，用于选择压缩参数（profile_override 不为 None 时忽略）
    expected_duration: 输入合计时长（秒），用于 post_validate 时长比对；None 则跳过比对
    profile_override: 不为 None 时覆盖 get_compress_profile() 返回值
    encoder: "nvenc"（默认）或 "x265-veryslow"
    x265_crf: x265 CRF 值；None 时使用 X265_DEFAULT_CRF
    返回 (success, in_sz, out_sz, elapsed, tracker, negative_flag)
    negative_flag=True 表示该次运行因负压缩被中断或事后丢弃，调用方应据此累计文件夹熔断计数。
    """
    profile = profile_override if profile_override is not None else get_compress_profile(camera_id, cq_override)
    input_size = os.path.getsize(input_path)

    cq_disp = profile.get("cq", "?")
    console.step(f"Compressing [{camera_id or '??'}] CQ{cq_disp} encoder={encoder}...")

    temp_output = output_path + ".compress_tmp.mp4"

    video_codec_args = _build_video_codec_args(profile, encoder=encoder, x265_crf=x265_crf)
    input_flags = ["-hwaccel", "cuda"] if encoder == "nvenc" else []
    cmd = [
        FFMPEG,
        "-y",
        *input_flags,
        "-fflags", "+genpts+igndts",
        "-err_detect", "ignore_err",
        "-i", input_path,
        *video_codec_args,
        "-c:a", "copy",
        "-movflags", "+faststart",
        temp_output,
    ]

    console.cmd_line(cmd[0], cmd[1:], verbose=verbose_cmd, base_dir=src_folder)

    returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(
        cmd, verbose=verbose_ffmpeg, expected_duration=expected_duration,
        expected_input_bytes=input_size,
        abort_on_negative_ratio=True,
    )

    # 监控状态机主动中断：删 temp，按负压缩降级
    if returncode == NEGATIVE_COMPRESSION_ABORT_RC:
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except OSError:
                pass
        console.warn(
            f"Compression aborted by monitor: "
            f"{tracker.abort_reason or 'negative compression detected'}",
            indent=2,
        )
        return False, 0, 0, elapsed, tracker, True

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
        return False, 0, 0, elapsed, tracker, False

    ok, reason = result.post_validate()
    if not ok:
        console.detail(f"[post-validate] {reason}; falling back")
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except OSError:
                pass
        return False, 0, 0, elapsed, tracker, False

    # 兜底：跑完发现输出比输入还大 → 视为负压缩，丢弃
    try:
        tmp_size = os.path.getsize(temp_output)
    except OSError:
        tmp_size = 0
    if tmp_size > input_size and input_size > 0:
        console.warn(
            f"Post-run negative compression detected: "
            f"{format_size(input_size)} -> {format_size(tmp_size)} "
            f"({tmp_size / input_size:.2f}x); discarding output",
            indent=2,
        )
        try:
            os.remove(temp_output)
        except OSError:
            pass
        return False, 0, 0, elapsed, tracker, True

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
    return True, input_size, output_size, elapsed, tracker, False
