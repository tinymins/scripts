"""整体处理流程：扫描 → 分组 → 合并/压缩 → 警告汇总。"""

from __future__ import annotations

import os
import shutil
import time
from typing import Dict

from .config import (
    NEGATIVE_COMPRESSION_FOLDER_BREAKER,
    format_eta,
    format_size,
)
from . import console
from .grouping import (
    create_combined_filename,
    group_videos_by_camera,
    group_videos_by_time,
)
from .merge import merge_videos
from .naming import _is_video_file, extract_camera_key, parse_video_filename, _basename
from .report import (
    _write_per_file_warning_log,
    classify_tracker,
    write_master_warning_report,
)


def check_file_exists(file_path):
    return os.path.exists(file_path)


def _print_group_done(
    group_idx: int, total_groups: int,
    done_bytes: int, total_bytes: int,
    done_files: int, total_files: int,
    start_time: float,
) -> None:
    """整体进度：每组结束后打印一行进度摘要。"""
    pct = done_bytes / total_bytes * 100 if total_bytes > 0 else 0.0
    elapsed = time.time() - start_time
    eta_s = (elapsed / done_bytes) * (total_bytes - done_bytes) if done_bytes > 0 else None
    console.success(
        f"Group {group_idx}/{total_groups} done · "
        f"{format_size(done_bytes)}/{format_size(total_bytes)} ({pct:.1f}%) · "
        f"{done_files}/{total_files} files · ETA {format_eta(eta_s)}"
    )


def process_videos_in_folder(
    src_folder,
    target_folder_base,
    enable_compress=False,
    cq_override=None,
    max_gap_seconds=None,
    duration_resolver=None,
    broken_split=True,
    verbose_ffmpeg=False,
    verbose_cmd=False,
    monthly_subdirs="auto",
    max_group_duration_seconds=None,
    dry_run=False,
    overwrite=False,
):
    video_files = []
    other_files = []
    video_file_sizes: Dict[str, int] = {}

    # 优化扫描文件速度，使用os.scandir递归
    console.section("Scanning source")
    console.step("Scanning for files...")
    def scan_folder(folder):
        for entry in os.scandir(folder):
            if entry.is_dir():
                scan_folder(entry.path)
            elif entry.is_file():
                # 排除0B文件
                sz = entry.stat().st_size
                if sz == 0:
                    console.warn(f"Skipping 0B file: {entry.path}", indent=2)
                    continue
                if _is_video_file(entry.name):
                    video_files.append(entry.path)
                    video_file_sizes[entry.path] = sz
                else:
                    other_files.append(entry.path)

    scan_folder(src_folder)
    console.kvtable([
        ("视频文件", len(video_files)),
        ("其它文件", len(other_files)),
    ])

    # 处理视频文件
    warning_collector = []
    processed_groups = 0
    skipped_groups = 0
    failed_groups = 0
    negative_compression_count = 0
    breaker_tripped = False
    if video_files:
        # 按照摄像机ID进行初始分组
        camera_groups = group_videos_by_camera(video_files, src_folder)
        console.kv("摄像机分组数", len(camera_groups))

        # 进一步按照时间关系进行分组
        grouped_videos = group_videos_by_time(
            camera_groups, max_gap_seconds, duration_resolver,
            broken_split=broken_split,
            max_group_duration_seconds=max_group_duration_seconds,
        )
        total_groups = len(grouped_videos)
        console.kv("待处理视频组", total_groups)

        total_input_size = 0
        total_output_size = 0
        total_elapsed = 0

        total_bytes = sum(video_file_sizes.values())
        total_video_files = len(video_files)
        done_bytes = 0
        done_files = 0
        overall_start_time = time.time()

        # 处理每个视频组
        for group in grouped_videos:
            processed_groups += 1

            # 获取该组的第一个视频文件和最后一个视频文件
            first_video = group[0]
            last_video = group[-1]

            # 获取原文件的相对路径
            relative_dir = os.path.dirname(os.path.relpath(first_video, src_folder))

            # 是否按月分子目录：auto → 仅 XIAOMI_* 设备启用；on → 全部；off → 平铺
            use_monthly = False
            if monthly_subdirs == "on":
                use_monthly = True
            elif monthly_subdirs == "auto":
                camera_key = extract_camera_key(first_video, src_folder) or ""
                use_monthly = camera_key.startswith("XIAOMI_")

            if use_monthly:
                first_info = parse_video_filename(_basename(first_video))
                if first_info.datetime:
                    yyyymm = first_info.datetime.strftime("%Y%m")
                    target_folder = os.path.join(target_folder_base, yyyymm)
                else:
                    target_folder = os.path.join(target_folder_base, relative_dir)
            else:
                target_folder = os.path.join(target_folder_base, relative_dir)

            # 创建目标文件夹
            if not dry_run:
                if not os.path.exists(target_folder):
                    os.makedirs(target_folder)

            # 构建输出文件名 - 使用新的命名格式
            combined_file_name = create_combined_filename(first_video, last_video, duration_resolver)
            combined_file_path = os.path.join(target_folder, combined_file_name)

            console.section(
                f"Group {processed_groups}/{total_groups}: {combined_file_name}"
            )
            console.kv("Files in group", len(group))

            exists = check_file_exists(combined_file_path)
            should_process = (not exists) or overwrite

            if dry_run:
                action = "WOULD overwrite" if (exists and overwrite) else (
                    "WOULD skip (exists)" if exists else "WOULD merge"
                )
                console.detail(f"[dry-run] {action}: {combined_file_path}", indent=2)
                if should_process:
                    sample = group[:3]
                    for s in sample:
                        console.detail(f"  ↪ {os.path.relpath(s, src_folder)}", indent=4)
                    if len(group) > 3:
                        console.detail(f"  ↪ … (+{len(group)-3} more)", indent=4)
                else:
                    skipped_groups += 1
                group_bytes = sum(video_file_sizes.get(p, 0) for p in group)
                done_bytes += group_bytes
                done_files += len(group)
                _print_group_done(processed_groups, total_groups, done_bytes, total_bytes, done_files, total_video_files, overall_start_time)
                continue

            if should_process:
                # 文件夹级熔断生效后，本组直接走 copy
                effective_compress = enable_compress
                if breaker_tripped:
                    effective_compress = False
                try:
                    in_sz, out_sz, elapsed, neg_flag = merge_videos(
                        group, combined_file_path, effective_compress, cq_override,
                        warning_collector=warning_collector,
                        duration_resolver=duration_resolver,
                        verbose_ffmpeg=verbose_ffmpeg,
                        verbose_cmd=verbose_cmd,
                        src_folder=src_folder,
                    )
                except Exception as exc:  # 单组兜底，不影响后续组
                    import traceback
                    console.error(f"处理该组时抛出异常: {exc}", indent=2)
                    traceback.print_exc()
                    failed_groups += 1
                    group_bytes = sum(video_file_sizes.get(p, 0) for p in group)
                    done_bytes += group_bytes
                    done_files += len(group)
                    _print_group_done(processed_groups, total_groups, done_bytes, total_bytes, done_files, total_video_files, overall_start_time)
                    continue
                total_input_size += in_sz
                total_output_size += out_sz
                total_elapsed += elapsed
                if enable_compress and in_sz > 0 and out_sz == 0:
                    failed_groups += 1
                if neg_flag:
                    negative_compression_count += 1
                    if (NEGATIVE_COMPRESSION_FOLDER_BREAKER > 0
                            and not breaker_tripped
                            and negative_compression_count >= NEGATIVE_COMPRESSION_FOLDER_BREAKER):
                        breaker_tripped = True
                        console.warn(
                            f"本文件夹已累计 {negative_compression_count} 次反向膨胀，"
                            f"剩余视频组转入 copy 模式（NVENC 已对该路视频不再有效）",
                            indent=2,
                        )
            else:
                console.detail(f"Combined file already exists, skipping: {combined_file_path}")
                skipped_groups += 1

            group_bytes = sum(video_file_sizes.get(p, 0) for p in group)
            done_bytes += group_bytes
            done_files += len(group)
            _print_group_done(processed_groups, total_groups, done_bytes, total_bytes, done_files, total_video_files, overall_start_time)

        # 打印汇总
        console.section("处理完成汇总")
        rows = [
            ("视频组总数", total_groups),
            ("已处理", processed_groups - skipped_groups - failed_groups),
            ("已跳过", skipped_groups),
        ]
        if failed_groups > 0:
            rows.append(("失败", failed_groups))
        if negative_compression_count > 0:
            rows.append(("反向膨胀降级数", negative_compression_count))
        if breaker_tripped:
            rows.append(
                ("文件夹级熔断", f"已触发（阈值 {NEGATIVE_COMPRESSION_FOLDER_BREAKER}）"),
            )
        if enable_compress and total_input_size > 0:
            overall_ratio = total_input_size / total_output_size if total_output_size > 0 else 0
            overall_saving = (1 - total_output_size / total_input_size) * 100
            rows.extend([
                ("原始总大小", format_size(total_input_size)),
                ("压缩后总大小", format_size(total_output_size)),
                ("总体压缩比", f"{overall_ratio:.1f}x"),
                ("总体节省", f"{overall_saving:.0f}%"),
            ])
            hours = int(total_elapsed // 3600)
            minutes = int((total_elapsed % 3600) // 60)
            seconds = int(total_elapsed % 60)
            if hours > 0:
                elapsed_str = f"{hours}h{minutes:02d}m{seconds:02d}s"
            elif minutes > 0:
                elapsed_str = f"{minutes}m{seconds:02d}s"
            else:
                elapsed_str = f"{seconds}s"
            rows.append(("压缩总耗时", elapsed_str))
        console.kvtable(rows)

    # 处理其他类型文件（必须在写汇总报告之前完成，避免几百个 copy 行刷掉报告）
    if other_files:
        console.section(f"Processing other file types ({len(other_files)} files)")
        copied_count = 0
        skipped_count = 0
        total = len(other_files)

        for idx, file_path in enumerate(other_files, 1):
            relative_path = os.path.relpath(file_path, src_folder)
            target_file_path = os.path.join(target_folder_base, relative_path)
            target_dir = os.path.dirname(target_file_path)

            exists = check_file_exists(target_file_path)
            if dry_run:
                if exists and overwrite:
                    console.detail(
                        f"[dry-run] WOULD overwrite [{idx}/{total}] {relative_path}",
                        indent=2,
                    )
                elif exists:
                    console.copy_line(relative_path, idx=idx, total=total, action="skip")
                else:
                    console.copy_line(relative_path, idx=idx, total=total, action="copy")
                continue

            if not os.path.exists(target_dir):
                os.makedirs(target_dir)

            if (not exists) or overwrite:
                console.copy_line(relative_path, idx=idx, total=total, action="copy")
                shutil.copy2(file_path, target_file_path)
                copied_count += 1
            else:
                console.copy_line(relative_path, idx=idx, total=total, action="skip")
                skipped_count += 1

        console.kvtable([
            ("已复制", copied_count),
            ("已跳过", skipped_count),
        ])

    # ============ FFmpeg 警告汇总（最后输出，避免被 copy 行刷掉）============
    if warning_collector and not dry_run:
        os.makedirs(target_folder_base, exist_ok=True)
        master_report_path = os.path.join(target_folder_base, "_transcode_warnings.txt")

        classified = []
        ok_count = 0
        suspicious_items = []
        downgraded_items = []
        warn_items = []
        for output_path, tracker in warning_collector:
            category = classify_tracker(tracker)
            if category == "OK":
                ok_count += 1
                continue
            classified.append((output_path, tracker, category))
            _write_per_file_warning_log(output_path, tracker, ["(see master report)"])
            if category == "SUSPICIOUS":
                suspicious_items.append((output_path, tracker))
            elif category == "DOWNGRADED":
                downgraded_items.append((output_path, tracker))
            else:
                warn_items.append((output_path, tracker))

        downgrade_total = sum(
            1 for _, tracker in warning_collector
            if getattr(tracker, "was_fallback", False)
        )
        completed_total = processed_groups - skipped_groups - failed_groups
        totals = {
            "completed": completed_total,
            "downgraded": downgrade_total,
            "failed": failed_groups,
        }

        write_master_warning_report(
            master_report_path, target_folder_base, classified, totals,
        )

        console.section("FFmpeg 转码警告汇总")
        console.kvtable([
            ("完成", completed_total),
            ("降级到 concat copy", downgrade_total),
            ("失败", failed_groups),
            ("干净", ok_count),
            ("轻微警告 (WARN)", len(warn_items)),
            ("降级 (DOWNGRADED)", len(downgraded_items)),
            ("严重 (SUSPICIOUS)", len(suspicious_items)),
        ])
        if suspicious_items:
            console.warn("SUSPICIOUS 文件（画面可能损坏 / 暂停）:", indent=2)
            for output_path, tracker in suspicious_items:
                rel = os.path.relpath(output_path, target_folder_base)
                console.detail(f"- {rel}", indent=4)
                console.detail(tracker.format_oneline(), indent=8)
        if downgraded_items:
            console.info("")
            console.detail("↓ DOWNGRADED 文件（已从 NVENC 压制降级到 concat copy 直拷）:")
            for output_path, tracker in downgraded_items:
                rel = os.path.relpath(output_path, target_folder_base)
                console.detail(f"- {rel}", indent=4)
        if warn_items or suspicious_items or downgraded_items:
            console.kv("详细报告", master_report_path)
            console.detail("每个有警告的输出旁边还会有同名 .warn.log")

    if dry_run:
        console.success(f"Dry-run complete (no changes written under {target_folder_base})")
    else:
        console.success("All processing completed successfully!")
