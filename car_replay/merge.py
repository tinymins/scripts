"""concat 列表生成、stream-copy 合并、合并+压缩一体化。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import console
from .compress import compress_video
from .config import FFMPEG, format_size, get_compress_profile
from .ffmpeg_runner import CommandResult, _run_ffmpeg_capturing_warnings
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


def _copy_merge_videos(video_group, combined_file, verbose_ffmpeg=False,
                       expected_duration=None, expected_input_bytes=None):
    """直接 stream-copy concat 合并；失败抛 CalledProcessError。"""
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
        returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(
            command, mode="concat_copy", verbose=verbose_ffmpeg,
            expected_duration=expected_duration,
            expected_input_bytes=expected_input_bytes,
        )
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command)
        return elapsed, tracker
    finally:
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)


def _sum_durations(video_group, duration_resolver):
    if duration_resolver is None:
        return None
    total = 0.0
    has_any = False
    for v in video_group:
        d = duration_resolver.duration_for_file(v)
        if d is not None and d > 0:
            total += d
            has_any = True
    return total if has_any else None


def _remove_stale_warn_log(combined_file):
    """删除可能由前一次（已废弃）运行写下的 .warn.log，避免与新 tracker 内容混淆。"""
    stale = combined_file + ".warn.log"
    if os.path.exists(stale):
        try:
            os.remove(stale)
        except OSError:
            pass


def _write_failure_log(combined_file, video_group, reason, duration_resolver=None):
    """在期望输出旁写 .failure.log，列出该组所有成员（含 broken 标记）+ 失败原因。"""
    try:
        with open(combined_file + ".failure.log", "w", encoding="utf-8") as f:
            f.write(f"merge failed: {combined_file}\nreason: {reason}\n"
                    f"members: {len(video_group)}\n")
            for v in video_group:
                mark = ""
                if duration_resolver is not None:
                    try:
                        mark = " [BROKEN]" if duration_resolver.is_broken(v) else ""
                    except RuntimeError:
                        mark = " [unprobed]"
                f.write(f"  {v}{mark}\n")
    except OSError as exc:
        console.warn(f"无法写 failure.log: {exc}", indent=2)


def _downgrade_reason(result):
    """返回降级原因字符串（None=不降级）。"""
    if result.is_fatal():
        return f"fatal rc={result.returncode}"
    if result.is_suspicious():
        return f"suspicious ({result.tracker.format_oneline()})"
    ok, reason = result.post_validate()
    if not ok:
        return f"post-validate: {reason}"
    return None


def _concat_copy_fallback(
    video_group, combined_file, *,
    expected_duration=None, warning_collector=None,
    duration_resolver=None, allow_recover=True,
    was_fallback=False, verbose_ffmpeg=False,
    verbose_cmd=False, src_folder=None,
):
    """concat copy + post_validate；失败时按 broken 二次切组重试一次。

    was_fallback: True 表示这是从 NVENC 压制失败降级而来；用于在 collector 中标注 DOWNGRADED。
    返回 (ok, elapsed, tracker)。
    """
    elapsed, tracker, run_ok = 0.0, None, False
    group_input_bytes = sum(os.path.getsize(v) for v in video_group if os.path.exists(v))
    try:
        elapsed, tracker = _copy_merge_videos(
            video_group, combined_file,
            verbose_ffmpeg=verbose_ffmpeg,
            expected_duration=expected_duration,
            expected_input_bytes=group_input_bytes,
        )
        run_ok = True
    except subprocess.CalledProcessError as exc:
        console.error(f"concat copy failed (rc={exc.returncode})", indent=2)
    except OSError as exc:
        console.error(f"concat copy OSError: {exc}", indent=2)

    if warning_collector is not None and tracker is not None:
        tracker.was_fallback = was_fallback
        warning_collector.append((combined_file, tracker))

    fail_reason = "concat copy failed"
    if run_ok:
        result = CommandResult(0, elapsed, tracker, Path(combined_file), expected_duration)
        ok, reason = result.post_validate(tolerance_factor=0.10)
        if ok:
            return True, elapsed, tracker
        fail_reason = f"post-validate: {reason}"
        console.detail(f"[post-validate concat copy] {reason}")

    if os.path.exists(combined_file):
        try:
            os.remove(combined_file)
        except OSError:
            pass

    if not allow_recover or duration_resolver is None:
        _write_failure_log(combined_file, video_group, fail_reason, duration_resolver)
        return False, elapsed, tracker

    # 二次保护：重新 probe，按 broken 切子组，每个子组 allow_recover=False 单次重试
    console.detail("Fallback recovery: re-probing health for group members…")
    new_broken = []
    for v in video_group:
        try:
            duration_resolver.ensure_health_probed(v)
            if duration_resolver.is_broken(v):
                new_broken.append(v)
        except Exception as exc:
            console.detail(f"re-probe error for {_basename(v)}: {exc}", indent=4)

    if not new_broken:
        console.detail("No new broken detected; giving up on this group")
        _write_failure_log(combined_file, video_group, fail_reason, duration_resolver)
        return False, elapsed, tracker

    sub_groups, cur, broken_set = [], [], set(new_broken)
    for v in video_group:
        if v in broken_set:
            if cur:
                sub_groups.append(cur)
                cur = []
            continue
        cur.append(v)
    if cur:
        sub_groups.append(cur)

    console.detail(
        f"Detected {len(new_broken)} broken file(s); "
        f"recovered into {len(sub_groups)} sub-group(s); retrying each once..."
    )
    base, ext = os.path.splitext(combined_file)
    any_success = False
    for idx, sg in enumerate(sub_groups, start=1):
        if not sg:
            continue
        ok, _, _ = _concat_copy_fallback(
            sg, f"{base}.recover{idx}{ext}",
            expected_duration=_sum_durations(sg, duration_resolver),
            warning_collector=warning_collector,
            duration_resolver=duration_resolver,
            allow_recover=False,
            was_fallback=was_fallback,
            verbose_ffmpeg=verbose_ffmpeg,
            verbose_cmd=verbose_cmd,
            src_folder=src_folder,
        )
        any_success = any_success or ok

    if not any_success:
        _write_failure_log(combined_file, video_group, fail_reason, duration_resolver)
    return any_success, elapsed, tracker


def merge_videos(video_group, combined_file, enable_compress=False, cq_override=None,
                 warning_collector=None, duration_resolver=None,
                 verbose_ffmpeg=False, verbose_cmd=False, src_folder=None):
    last_video_stats = os.stat(video_group[-1])
    last_access_time, last_mod_time = last_video_stats.st_atime, last_video_stats.st_mtime
    camera_id = extract_camera_id(_basename(video_group[0]))

    # 预扫健康度：任一输入文件不健康则该组改走 -c copy
    if enable_compress and duration_resolver is not None:
        unhealthy = [v for v in video_group if duration_resolver.is_healthy(v) is False]
        if unhealthy:
            console.warn(
                f"Group has {len(unhealthy)} unhealthy input(s) "
                f"e.g. {_basename(unhealthy[0])}; falling back to -c copy for safety",
                indent=2,
            )
            enable_compress = False

    if len(video_group) == 1 and enable_compress:
        # 单文件 + 压缩
        console.step(f"Compressing single file: {_basename(video_group[0])}")
        console.detail(f"→ {combined_file}")
        single_dur = _sum_durations(video_group, duration_resolver)
        success, in_sz, out_sz, elapsed, tracker = compress_video(
            video_group[0], combined_file, camera_id, cq_override,
            expected_duration=single_dur,
            verbose_ffmpeg=verbose_ffmpeg,
            verbose_cmd=verbose_cmd, src_folder=src_folder,
        )
        if success:
            if warning_collector is not None and tracker is not None:
                warning_collector.append((combined_file, tracker))
            os.utime(combined_file, (last_access_time, last_mod_time))
            return in_sz, out_sz, elapsed
        # 压缩失败 → 丢弃压制阶段 tracker；同扩展名直接 copy；否则走 _concat_copy_fallback
        _remove_stale_warn_log(combined_file)
        console.warn("Compression failed, falling back to copy/remux...", indent=2)
        same_ext = (os.path.splitext(video_group[0])[1].lower()
                    == os.path.splitext(combined_file)[1].lower())
        if same_ext:
            try:
                shutil.copy2(video_group[0], combined_file)
                os.utime(combined_file, (last_access_time, last_mod_time))
                copied_size = os.path.getsize(combined_file) if os.path.exists(combined_file) else 0
                return in_sz, copied_size, elapsed
            except OSError as exc:
                console.error(f"copy fallback failed: {exc}", indent=2)
                _write_failure_log(combined_file, video_group, f"copy: {exc}", duration_resolver)
                return in_sz, 0, elapsed
        ok, _, _ = _concat_copy_fallback(
            video_group, combined_file,
            expected_duration=_sum_durations(video_group, duration_resolver),
            warning_collector=warning_collector, duration_resolver=duration_resolver,
            was_fallback=True,
            verbose_ffmpeg=verbose_ffmpeg,
            verbose_cmd=verbose_cmd, src_folder=src_folder,
        )
        if ok:
            os.utime(combined_file, (last_access_time, last_mod_time))
            out_sz = os.path.getsize(combined_file) if os.path.exists(combined_file) else 0
            return in_sz, out_sz, elapsed
        return in_sz, 0, elapsed

    if (len(video_group) == 1 and not enable_compress
            and os.path.splitext(video_group[0])[1].lower() == ".mp4"):
        # 单文件 + 不压缩：直接复制
        console.step(f"Copying single file: {_basename(video_group[0])}")
        console.detail(f"→ {combined_file}")
        shutil.copy2(video_group[0], combined_file)
        sz = os.path.getsize(combined_file) if os.path.exists(combined_file) else 0
        return sz, sz, 0

    console.step(f"Merging {len(video_group)} files into {_basename(combined_file)}")
    console.list_items(
        "Files to merge",
        [_basename(v) for v in video_group],
        max_show=3,
        verbose=verbose_cmd,
    )

    if enable_compress:
        # 合并+压缩一步完成
        concat_list_path = combined_file + ".concat_list.txt"
        _write_concat_list(concat_list_path, video_group)
        profile = get_compress_profile(camera_id, cq_override)
        input_size = sum(os.path.getsize(v) for v in video_group)
        expected_duration = _sum_durations(video_group, duration_resolver)
        console.step(
            f"Merge+Compress [{camera_id or '??'}] CQ{profile['cq']} "
            f"({len(video_group)} files)..."
        )

        temp_output = combined_file + ".compress_tmp.mp4"
        cmd = [
            FFMPEG, "-y", "-hwaccel", "cuda",
            "-fflags", "+genpts+igndts", "-err_detect", "ignore_err",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c:v", "hevc_nvenc", "-preset", profile["preset"],
            "-rc", "vbr", "-cq", str(profile["cq"]),
            "-b:v", profile["bitrate"], "-maxrate", profile["maxrate"],
            "-bufsize", profile["bufsize"], "-multipass", "fullres",
            "-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1",
            "-bf", "4", "-b_ref_mode", "middle",
            "-c:a", "copy", "-movflags", "+faststart", temp_output,
        ]

        console.cmd_line(cmd[0], cmd[1:], verbose=verbose_cmd, base_dir=src_folder)
        returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(
            cmd, verbose=verbose_ffmpeg, expected_duration=expected_duration,
            expected_input_bytes=input_size,
        )
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)

        result = CommandResult(returncode, elapsed, tracker, Path(temp_output), expected_duration)
        downgrade = _downgrade_reason(result)
        if downgrade:
            console.warn(
                f"Merge+Compress {downgrade}; falling back to concat copy", indent=2,
            )
            if os.path.exists(temp_output):
                try: os.remove(temp_output)
                except OSError: pass
            # 丢弃压制阶段 tracker；清理可能遗留的 .warn.log
            _remove_stale_warn_log(combined_file)
            ok, _, _ = _concat_copy_fallback(
                video_group, combined_file, expected_duration=expected_duration,
                warning_collector=warning_collector, duration_resolver=duration_resolver,
                was_fallback=True,
                verbose_ffmpeg=verbose_ffmpeg,
                verbose_cmd=verbose_cmd, src_folder=src_folder,
            )
            if ok:
                os.utime(combined_file, (last_access_time, last_mod_time))
                # 降级也算实绩：返回真实 input / output / elapsed
                output_size = os.path.getsize(combined_file) if os.path.exists(combined_file) else 0
                return input_size, output_size, elapsed
            return input_size, 0, elapsed

        # 压制成功，未降级 → 此时才把压制阶段 tracker 入 collector
        if warning_collector is not None:
            warning_collector.append((combined_file, tracker))

        if os.path.exists(combined_file):
            os.remove(combined_file)
        os.rename(temp_output, combined_file)
        output_size = os.path.getsize(combined_file)
        ratio = input_size / output_size if output_size > 0 else 0
        saving = (1 - output_size / input_size) * 100 if input_size > 0 else 0
        console.success(
            f"Compressed: {format_size(input_size)} → {format_size(output_size)} "
            f"({ratio:.1f}x, -{saving:.0f}%) in {elapsed:.1f}s",
            indent=2,
        )
        os.utime(combined_file, (last_access_time, last_mod_time))
        return input_size, output_size, elapsed

    # 不压缩：concat copy + post_validate（含二次保护）
    ok, _, _ = _concat_copy_fallback(
        video_group, combined_file,
        expected_duration=_sum_durations(video_group, duration_resolver),
        warning_collector=warning_collector, duration_resolver=duration_resolver,
        verbose_ffmpeg=verbose_ffmpeg,
        verbose_cmd=verbose_cmd, src_folder=src_folder,
    )
    if ok:
        console.success("Merge complete.", indent=2)
        os.utime(combined_file, (last_access_time, last_mod_time))
        in_sz = sum(os.path.getsize(v) for v in video_group if os.path.exists(v))
        out_sz = os.path.getsize(combined_file) if os.path.exists(combined_file) else 0
        return in_sz, out_sz, 0
    console.error("Merge failed (see .failure.log).", indent=2)
    in_sz = sum(os.path.getsize(v) for v in video_group if os.path.exists(v))
    return in_sz, 0, 0
