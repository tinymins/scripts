"""整体处理流程：扫描 → 分组 → 合并/压缩 → 警告汇总。"""

from __future__ import annotations

import os
import shutil

from .config import format_size
from .grouping import (
    create_combined_filename,
    group_videos_by_camera,
    group_videos_by_time,
)
from .merge import merge_videos
from .naming import _is_video_file
from .report import _append_master_warning_report, _write_per_file_warning_log


def check_file_exists(file_path):
    return os.path.exists(file_path)


def process_videos_in_folder(
    src_folder,
    target_folder_base,
    enable_compress=False,
    cq_override=None,
    max_gap_seconds=None,
    duration_resolver=None,
    broken_split=True,
):
    video_files = []
    other_files = []

    # 优化扫描文件速度，使用os.scandir递归
    print("Scanning for files...")
    def scan_folder(folder):
        for entry in os.scandir(folder):
            if entry.is_dir():
                scan_folder(entry.path)
            elif entry.is_file():
                # 排除0B文件
                if entry.stat().st_size == 0:
                    print(f"Skipping 0B file: {entry.path}")
                    continue
                if _is_video_file(entry.name):
                    video_files.append(entry.path)
                else:
                    other_files.append(entry.path)

    scan_folder(src_folder)
    print(f"Found {len(video_files)} video files and {len(other_files)} other files.")

    # 处理视频文件
    if video_files:
        # 按照摄像机ID进行初始分组
        camera_groups = group_videos_by_camera(video_files, src_folder)
        print(f"Video files divided into {len(camera_groups)} different camera groups.")

        # 进一步按照时间关系进行分组
        grouped_videos = group_videos_by_time(
            camera_groups, max_gap_seconds, duration_resolver,
            broken_split=broken_split,
        )
        total_groups = len(grouped_videos)
        print(f"Total video groups to process: {total_groups}")

        processed_groups = 0
        skipped_groups = 0
        failed_groups = 0
        total_input_size = 0
        total_output_size = 0
        total_elapsed = 0
        warning_collector = []

        # 处理每个视频组
        for group in grouped_videos:
            processed_groups += 1

            # 获取该组的第一个视频文件和最后一个视频文件
            first_video = group[0]
            last_video = group[-1]

            # 获取原文件的相对路径
            relative_dir = os.path.dirname(os.path.relpath(first_video, src_folder))
            target_folder = os.path.join(target_folder_base, relative_dir)

            # 创建目标文件夹
            if not os.path.exists(target_folder):
                os.makedirs(target_folder)

            # 构建输出文件名 - 使用新的命名格式
            combined_file_name = create_combined_filename(first_video, last_video, duration_resolver)
            combined_file_path = os.path.join(target_folder, combined_file_name)

            print(f"\nProcessing group {processed_groups}/{total_groups}: {combined_file_name}")
            print(f"Group contains {len(group)} files")

            if not check_file_exists(combined_file_path):
                try:
                    in_sz, out_sz, elapsed = merge_videos(
                        group, combined_file_path, enable_compress, cq_override,
                        warning_collector=warning_collector,
                        duration_resolver=duration_resolver,
                    )
                except Exception as exc:  # 单组兜底，不影响后续组
                    import traceback
                    print(f"  ❌ 处理该组时抛出异常: {exc}")
                    traceback.print_exc()
                    failed_groups += 1
                    continue
                total_input_size += in_sz
                total_output_size += out_sz
                total_elapsed += elapsed
                if enable_compress and in_sz > 0 and out_sz == 0:
                    failed_groups += 1
            else:
                print(f"Combined file already exists: {combined_file_path}, skipping...")
                skipped_groups += 1

        # 打印汇总
        print(f"\n{'='*70}")
        print("处理完成汇总")
        print(f"{'='*70}")
        print(f"  视频组总数: {total_groups}")
        print(f"  已处理: {processed_groups - skipped_groups - failed_groups}")
        print(f"  已跳过: {skipped_groups}")
        if failed_groups > 0:
            print(f"  失败: {failed_groups}")
        if enable_compress and total_input_size > 0:
            overall_ratio = total_input_size / total_output_size if total_output_size > 0 else 0
            overall_saving = (1 - total_output_size / total_input_size) * 100
            print(f"  原始总大小: {format_size(total_input_size)}")
            print(f"  压缩后总大小: {format_size(total_output_size)}")
            print(f"  总体压缩比: {overall_ratio:.1f}x")
            print(f"  总体节省: {overall_saving:.0f}%")
            hours = int(total_elapsed // 3600)
            minutes = int((total_elapsed % 3600) // 60)
            seconds = int(total_elapsed % 60)
            if hours > 0:
                print(f"  压缩总耗时: {hours}h{minutes:02d}m{seconds:02d}s")
            elif minutes > 0:
                print(f"  压缩总耗时: {minutes}m{seconds:02d}s")
            else:
                print(f"  压缩总耗时: {seconds}s")
        print(f"{'='*70}")

        # ============ FFmpeg 警告汇总 ============
        if warning_collector:
            suspicious_items = []
            warn_items = []
            ok_count = 0
            os.makedirs(target_folder_base, exist_ok=True)
            master_report_path = os.path.join(target_folder_base, "_transcode_warnings.txt")
            if os.path.exists(master_report_path):
                os.remove(master_report_path)

            for output_path, tracker in warning_collector:
                severity = tracker.severity()
                if severity == "OK":
                    ok_count += 1
                    continue
                # 写每文件 .warn.log
                _write_per_file_warning_log(output_path, tracker, ["(see master report)"])
                _append_master_warning_report(target_folder_base, output_path, tracker)
                if severity == "SUSPICIOUS":
                    suspicious_items.append((output_path, tracker))
                else:
                    warn_items.append((output_path, tracker))

            print(f"\n{'='*70}")
            print("FFmpeg 转码警告汇总")
            print(f"{'='*70}")
            print(f"  干净: {ok_count}")
            print(f"  轻微警告 (WARN): {len(warn_items)}")
            print(f"  严重 (SUSPICIOUS, 强烈建议人工检查): {len(suspicious_items)}")
            if suspicious_items:
                print("\n  ⚠ SUSPICIOUS 文件（画面可能损坏 / 暂停）:")
                for output_path, tracker in suspicious_items:
                    rel = os.path.relpath(output_path, target_folder_base)
                    print(f"    - {rel}")
                    print(f"        {tracker.format_oneline()}")
            if warn_items or suspicious_items:
                print(f"\n  详细报告: {master_report_path}")
                print(f"  每个有警告的输出旁边还会有同名 .warn.log")
            print(f"{'='*70}")

    # 处理其他类型文件
    if other_files:
        print("\nProcessing other file types...")
        copied_count = 0
        skipped_count = 0

        for file_path in other_files:
            relative_path = os.path.relpath(file_path, src_folder)
            target_file_path = os.path.join(target_folder_base, relative_path)
            target_dir = os.path.dirname(target_file_path)

            # 确保目标目录存在
            if not os.path.exists(target_dir):
                os.makedirs(target_dir)

            if not check_file_exists(target_file_path):
                print(f"Copying: {relative_path}")
                shutil.copy2(file_path, target_file_path)
                copied_count += 1
            else:
                print(f"File already exists: {relative_path}, skipping...")
                skipped_count += 1

        print(f"\nOther files processing completed. {copied_count} files copied, {skipped_count} files skipped.")

    print("\nAll processing completed successfully!")
