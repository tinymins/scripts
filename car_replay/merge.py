"""concat 列表生成、stream-copy 合并、合并+压缩一体化。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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


def _copy_merge_videos(video_group, combined_file):
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
        returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(command)
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
        print(f"  WARN: 无法写 failure.log: {exc}")


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
):
    """concat copy + post_validate；失败时按 broken 二次切组重试一次。

    返回 (ok, elapsed, tracker)。
    """
    elapsed, tracker, run_ok = 0.0, None, False
    try:
        elapsed, tracker = _copy_merge_videos(video_group, combined_file)
        run_ok = True
    except subprocess.CalledProcessError as exc:
        print(f"  ERROR: concat copy failed (rc={exc.returncode})")
    except OSError as exc:
        print(f"  ERROR: concat copy OSError: {exc}")

    if warning_collector is not None and tracker is not None:
        warning_collector.append((combined_file, tracker))

    fail_reason = "concat copy failed"
    if run_ok:
        result = CommandResult(0, elapsed, tracker, Path(combined_file), expected_duration)
        ok, reason = result.post_validate(tolerance_factor=0.10)
        if ok:
            return True, elapsed, tracker
        fail_reason = f"post-validate: {reason}"
        print(f"  [post-validate concat copy] {reason}")

    if os.path.exists(combined_file):
        try:
            os.remove(combined_file)
        except OSError:
            pass

    if not allow_recover or duration_resolver is None:
        _write_failure_log(combined_file, video_group, fail_reason, duration_resolver)
        return False, elapsed, tracker

    # 二次保护：重新 probe，按 broken 切子组，每个子组 allow_recover=False 单次重试
    print("  Fallback recovery: re-probing health for group members…")
    new_broken = []
    for v in video_group:
        try:
            duration_resolver.ensure_health_probed(v)
            if duration_resolver.is_broken(v):
                new_broken.append(v)
        except Exception as exc:
            print(f"    re-probe error for {_basename(v)}: {exc}")

    if not new_broken:
        print("  No new broken detected; giving up on this group")
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

    print(
        f"  Detected {len(new_broken)} broken file(s); "
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
        )
        any_success = any_success or ok

    if not any_success:
        _write_failure_log(combined_file, video_group, fail_reason, duration_resolver)
    return any_success, elapsed, tracker


def merge_videos(video_group, combined_file, enable_compress=False, cq_override=None,
                 warning_collector=None, duration_resolver=None):
    last_video_stats = os.stat(video_group[-1])
    last_access_time, last_mod_time = last_video_stats.st_atime, last_video_stats.st_mtime
    camera_id = extract_camera_id(_basename(video_group[0]))

    # 预扫健康度：任一输入文件不健康则该组改走 -c copy
    if enable_compress and duration_resolver is not None:
        unhealthy = [v for v in video_group if duration_resolver.is_healthy(v) is False]
        if unhealthy:
            print(f"  Group has {len(unhealthy)} unhealthy input(s) "
                  f"e.g. {_basename(unhealthy[0])}; falling back to -c copy for safety")
            enable_compress = False

    if len(video_group) == 1 and enable_compress:
        # 单文件 + 压缩
        print(f"Compressing single file: {video_group[0]} to {combined_file}")
        single_dur = _sum_durations(video_group, duration_resolver)
        success, in_sz, out_sz, elapsed, tracker = compress_video(
            video_group[0], combined_file, camera_id, cq_override,
            expected_duration=single_dur,
        )
        if warning_collector is not None and tracker is not None:
            warning_collector.append((combined_file, tracker))
        if success:
            os.utime(combined_file, (last_access_time, last_mod_time))
            return in_sz, out_sz, elapsed
        # 压缩失败 → 同扩展名直接 copy；否则走 _concat_copy_fallback
        print("  Compression failed, falling back to copy/remux...")
        same_ext = (os.path.splitext(video_group[0])[1].lower()
                    == os.path.splitext(combined_file)[1].lower())
        if same_ext:
            try:
                shutil.copy2(video_group[0], combined_file)
                os.utime(combined_file, (last_access_time, last_mod_time))
                return 0, 0, 0
            except OSError as exc:
                print(f"  ERROR: copy fallback failed: {exc}")
                _write_failure_log(combined_file, video_group, f"copy: {exc}", duration_resolver)
                return in_sz, 0, elapsed
        ok, _, _ = _concat_copy_fallback(
            video_group, combined_file,
            expected_duration=_sum_durations(video_group, duration_resolver),
            warning_collector=warning_collector, duration_resolver=duration_resolver,
        )
        if ok:
            os.utime(combined_file, (last_access_time, last_mod_time))
            return 0, 0, 0
        return in_sz, 0, elapsed

    if (len(video_group) == 1 and not enable_compress
            and os.path.splitext(video_group[0])[1].lower() == ".mp4"):
        # 单文件 + 不压缩：直接复制
        print(f"Copying single file: {video_group[0]} to {combined_file}")
        shutil.copy2(video_group[0], combined_file)
        return 0, 0, 0

    print(f"Merging {len(video_group)} files into: {combined_file}")
    print(f"Files to merge: {[_basename(v) for v in video_group]}")

    if enable_compress:
        # 合并+压缩一步完成
        concat_list_path = combined_file + ".concat_list.txt"
        _write_concat_list(concat_list_path, video_group)
        profile = get_compress_profile(camera_id, cq_override)
        input_size = sum(os.path.getsize(v) for v in video_group)
        expected_duration = _sum_durations(video_group, duration_resolver)
        print(
            f"  Merge+Compress [{camera_id or '??'}] CQ{profile['cq']} "
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

        print(f"  CMD: {' '.join(cmd)}")
        returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(cmd)
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)

        if warning_collector is not None:
            warning_collector.append((combined_file, tracker))

        result = CommandResult(returncode, elapsed, tracker, Path(temp_output), expected_duration)
        downgrade = _downgrade_reason(result)
        if downgrade:
            print(f"  Merge+Compress {downgrade}; falling back to concat copy")
            if os.path.exists(temp_output):
                try: os.remove(temp_output)
                except OSError: pass
            ok, _, _ = _concat_copy_fallback(
                video_group, combined_file, expected_duration=expected_duration,
                warning_collector=warning_collector, duration_resolver=duration_resolver,
            )
            if ok:
                os.utime(combined_file, (last_access_time, last_mod_time))
                return 0, 0, 0
            return input_size, 0, elapsed

        if os.path.exists(combined_file):
            os.remove(combined_file)
        os.rename(temp_output, combined_file)
        output_size = os.path.getsize(combined_file)
        ratio = input_size / output_size if output_size > 0 else 0
        saving = (1 - output_size / input_size) * 100 if input_size > 0 else 0
        print(f"  Compressed: {format_size(input_size)} -> {format_size(output_size)} "
              f"({ratio:.1f}x ratio, -{saving:.0f}%) in {elapsed:.1f}s")
        os.utime(combined_file, (last_access_time, last_mod_time))
        return input_size, output_size, elapsed

    # 不压缩：concat copy + post_validate（含二次保护）
    ok, _, _ = _concat_copy_fallback(
        video_group, combined_file,
        expected_duration=_sum_durations(video_group, duration_resolver),
        warning_collector=warning_collector, duration_resolver=duration_resolver,
    )
    if ok:
        print("Merge complete.")
        os.utime(combined_file, (last_access_time, last_mod_time))
    else:
        print("Merge failed (see .failure.log).")
    return 0, 0, 0
