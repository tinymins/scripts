"""
行车记录仪视频批量压缩脚本

对行车记录仪视频进行 HEVC NVENC 压缩。
自动识别通道(AA/AB/AC)并使用对应的压缩参数。
输出到源目录同级的 _Compress 目录，保持目录结构。

例如：
  输入: \\server\share\360CARDVR
  输出: \\server\share\360CARDVR_Compress
  内部结构保持不变（如 REC/ 子目录）

用法：
  python combine_car_replay_compress_existing.py                           # 交互式输入源目录
  python combine_car_replay_compress_existing.py --src "路径"              # 指定源目录
  python combine_car_replay_compress_existing.py --src "路径" --cq 26      # 全局覆盖CQ值
  python combine_car_replay_compress_existing.py --src "路径" --parallel 2 # 并行压缩2个文件
  python combine_car_replay_compress_existing.py --src "路径" --dry-run    # 预览不实际处理
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 压缩参数配置 - 测试确认后可修改此处
# ============================================================

COMPRESS_PROFILES = {
    "AA": {  # 4K 前摄 3840x2160, 原始 ~30Mbps
        "cq": 32,
        "bitrate": "8M",
        "maxrate": "12M",
        "bufsize": "16M",
        "preset": "p5",
    },
    "AB": {  # 1080p 后摄, 原始 ~8.4Mbps
        "cq": 32,
        "bitrate": "3M",
        "maxrate": "5M",
        "bufsize": "8M",
        "preset": "p5",
    },
    "AC": {  # 1080p 车内, 原始 ~8.4Mbps
        "cq": 32,
        "bitrate": "3M",
        "maxrate": "5M",
        "bufsize": "8M",
        "preset": "p5",
    },
}

# 未识别通道时的默认参数
DEFAULT_PROFILE = {
    "cq": 32,
    "bitrate": "5M",
    "maxrate": "8M",
    "bufsize": "10M",
    "preset": "p5",
}

# ffmpeg/ffprobe 路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "..", "third_party", "ffmpeg", "ffmpeg.exe")
FFPROBE = os.path.join(SCRIPT_DIR, "..", "third_party", "ffmpeg", "ffprobe.exe")


# ============================================================
# 通用函数
# ============================================================


def extract_camera_id(filename):
    """从文件名中提取摄像机通道ID"""
    # 合并后格式：20250505201522_20250505213501_000001AA.MP4
    # 也匹配可能的裁剪后缀：...AA-00.11.38.000-00.11.41.000.MP4
    match = re.match(r"\d{14}_\d{14}_\d+([A-Z]+)[\-.]", filename, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # 原始单文件格式：20250419195801_000785AC.MP4
    match = re.match(r"\d{14}_\d+([A-Z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # 新格式：NO20200101-001521-002110B.mp4
    match = re.match(r"[A-Za-z]+\d{8}-\d{6}-\d+([A-Za-z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    return None


def get_compress_profile(camera_id, cq_override=None):
    """根据通道ID获取压缩参数"""
    profile = COMPRESS_PROFILES.get(camera_id, DEFAULT_PROFILE).copy()
    if cq_override is not None:
        profile["cq"] = cq_override
    return profile


def format_size(size_bytes):
    """格式化文件大小"""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.2f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def format_duration(seconds):
    """格式化时间"""
    if seconds >= 3600:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}h{m:02d}m{s:02d}s"
    elif seconds >= 60:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s:02d}s"
    return f"{seconds:.1f}s"


def compress_video(input_path, output_path, profile):
    """
    使用 hevc_nvenc 压缩单个视频文件。
    返回 (success, elapsed_seconds, input_size, output_size)
    """
    # 确保输出目录存在
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    # 获取输入文件信息
    input_size = os.path.getsize(input_path)
    input_stats = os.stat(input_path)

    # 使用临时文件名，避免中断导致不完整文件
    temp_output = output_path + ".tmp.mp4"

    cmd = [
        FFMPEG,
        "-y",
        "-hwaccel", "cuda",
        "-i", input_path,
        "-c:v", "hevc_nvenc",
        "-preset", profile["preset"],
        "-rc", "vbr",
        "-cq", str(profile["cq"]),
        "-b:v", profile["bitrate"],
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-c:a", "copy",
        "-movflags", "+faststart",
        temp_output,
    ]

    start_time = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start_time

    if result.returncode != 0:
        # 清理临时文件
        if os.path.exists(temp_output):
            os.remove(temp_output)
        print(f"  ERROR compressing {os.path.basename(input_path)}: {result.stderr[-300:]}")
        return False, elapsed, input_size, 0

    # 重命名临时文件为最终文件
    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(temp_output, output_path)

    # 保留原始文件时间戳
    os.utime(output_path, (input_stats.st_atime, input_stats.st_mtime))

    output_size = os.path.getsize(output_path)
    return True, elapsed, input_size, output_size


def scan_mp4_files(folder):
    """递归扫描文件夹中的所有文件，分为MP4和其他文件"""
    mp4_files = []
    other_files = []

    print(f"Scanning: {folder}")
    for root, dirs, files in os.walk(folder):
        for filename in files:
            filepath = os.path.join(root, filename)
            try:
                if os.path.getsize(filepath) == 0:
                    print(f"  Skipping 0B file: {filepath}")
                    continue
            except OSError:
                continue

            if filename.lower().endswith(".mp4"):
                mp4_files.append(filepath)
            else:
                other_files.append(filepath)

    return mp4_files, other_files


def process_single_file(src_path, dst_path, src_folder, target_folder, cq_override, dry_run):
    """处理单个MP4文件的压缩"""
    basename = os.path.basename(src_path)
    relative_path = os.path.relpath(src_path, src_folder)
    camera_id = extract_camera_id(basename)
    profile = get_compress_profile(camera_id, cq_override)

    if dry_run:
        src_size = os.path.getsize(src_path)
        channel_str = camera_id if camera_id else "??"
        print(f"  [DRY-RUN] {relative_path} ({format_size(src_size)}) -> CQ{profile['cq']} [{channel_str}]")
        return True, 0, src_size, 0

    if os.path.exists(dst_path):
        return None, 0, 0, 0  # None = skipped

    channel_str = camera_id if camera_id else "??"
    print(f"  Compressing [{channel_str}] {relative_path} (CQ{profile['cq']})...")

    success, elapsed, in_size, out_size = compress_video(src_path, dst_path, profile)

    if success:
        ratio = in_size / out_size if out_size > 0 else 0
        saving = (1 - out_size / in_size) * 100 if in_size > 0 else 0
        print(
            f"    Done: {format_size(in_size)} -> {format_size(out_size)} "
            f"({ratio:.1f}x, -{saving:.0f}%) in {format_duration(elapsed)}"
        )

    return success, elapsed, in_size, out_size


def main():
    parser = argparse.ArgumentParser(description="行车记录仪视频批量压缩")
    parser.add_argument("--src", type=str, help="源文件夹路径（行车记录仪根目录）")
    parser.add_argument("--dst", type=str, help="目标文件夹路径（默认：源文件夹_Compress）")
    parser.add_argument("--cq", type=int, help="全局覆盖CQ值（默认按通道使用预设值）")
    parser.add_argument(
        "--parallel", type=int, default=1, help="并行压缩数量（默认1，NVENC通常支持2-3路并行）"
    )
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际处理")
    args = parser.parse_args()

    # 获取源目录
    src_folder = args.src
    if not src_folder:
        src_folder = input("请输入源文件夹路径（行车记录仪根目录）: ").strip().strip('"')

    # 规范化路径：移除末尾斜杠
    src_folder = src_folder.rstrip("\\/")

    if not os.path.isdir(src_folder):
        print(f"ERROR: Source folder not found: {src_folder}")
        sys.exit(1)

    # 确定目标目录：源目录同级的 _Compress 目录
    # 例如: \\server\share\360CARDVR -> \\server\share\360CARDVR_Compress
    if args.dst:
        target_folder = args.dst
    else:
        target_folder = src_folder + "_Compress"

    print(f"\n{'='*70}")
    print("行车记录仪视频批量压缩")
    print(f"{'='*70}")
    print(f"源目录:   {src_folder}")
    print(f"目标目录: {target_folder}")
    print(f"CQ覆盖:  {args.cq if args.cq else '无（使用通道默认值）'}")
    print(f"并行数:   {args.parallel}")
    if args.dry_run:
        print("模式:     预览模式 (DRY-RUN)")
    print(f"{'='*70}")

    # 扫描文件
    mp4_files, other_files = scan_mp4_files(src_folder)
    print(f"\nFound {len(mp4_files)} MP4 files and {len(other_files)} other files.")

    if not mp4_files and not other_files:
        print("No files to process.")
        return

    # 统计
    total_files = len(mp4_files)
    processed = 0
    skipped = 0
    failed = 0
    total_input_size = 0
    total_output_size = 0
    total_elapsed = 0

    # 处理MP4文件
    if mp4_files:
        print(f"\n--- Processing {len(mp4_files)} MP4 files ---")

        if args.parallel > 1 and not args.dry_run:
            # 并行处理
            futures = {}
            with ThreadPoolExecutor(max_workers=args.parallel) as executor:
                for src_path in mp4_files:
                    relative_path = os.path.relpath(src_path, src_folder)
                    dst_path = os.path.join(target_folder, relative_path)

                    # 确保目标目录存在
                    dst_dir = os.path.dirname(dst_path)
                    if dst_dir:
                        os.makedirs(dst_dir, exist_ok=True)

                    if os.path.exists(dst_path):
                        skipped += 1
                        print(f"  Already exists, skipping: {relative_path}")
                        continue

                    future = executor.submit(
                        process_single_file,
                        src_path,
                        dst_path,
                        src_folder,
                        target_folder,
                        args.cq,
                        False,
                    )
                    futures[future] = relative_path

                for future in as_completed(futures):
                    relative = futures[future]
                    try:
                        success, elapsed, in_size, out_size = future.result()
                        if success:
                            processed += 1
                            total_input_size += in_size
                            total_output_size += out_size
                            total_elapsed += elapsed
                        else:
                            failed += 1
                    except Exception as e:
                        print(f"  ERROR processing {relative}: {e}")
                        failed += 1
        else:
            # 顺序处理
            for idx, src_path in enumerate(mp4_files):
                relative_path = os.path.relpath(src_path, src_folder)
                dst_path = os.path.join(target_folder, relative_path)

                # 确保目标目录存在
                dst_dir = os.path.dirname(dst_path)
                if dst_dir:
                    os.makedirs(dst_dir, exist_ok=True)

                print(f"\n[{idx + 1}/{total_files}] ", end="")

                if os.path.exists(dst_path) and not args.dry_run:
                    skipped += 1
                    print(f"Already exists, skipping: {relative_path}")
                    continue

                success, elapsed, in_size, out_size = process_single_file(
                    src_path, dst_path, src_folder, target_folder, args.cq, args.dry_run
                )

                if success is None:
                    skipped += 1
                elif success:
                    processed += 1
                    total_input_size += in_size
                    total_output_size += out_size
                    total_elapsed += elapsed
                else:
                    failed += 1

    # 处理其他类型文件
    if other_files and not args.dry_run:
        print(f"\n--- Copying {len(other_files)} non-MP4 files ---")
        copied_count = 0
        skip_other = 0

        for filepath in other_files:
            relative_path = os.path.relpath(filepath, src_folder)
            dst_path = os.path.join(target_folder, relative_path)
            dst_dir = os.path.dirname(dst_path)

            if dst_dir:
                os.makedirs(dst_dir, exist_ok=True)

            if not os.path.exists(dst_path):
                shutil.copy2(filepath, dst_path)
                copied_count += 1
            else:
                skip_other += 1

        print(f"  Copied {copied_count} files, skipped {skip_other} existing files.")

    # 打印汇总
    print(f"\n{'='*70}")
    print("压缩完成汇总")
    print(f"{'='*70}")
    print(f"  处理文件数: {processed}")
    print(f"  跳过文件数: {skipped}")
    print(f"  失败文件数: {failed}")
    if total_input_size > 0 and not args.dry_run:
        overall_ratio = total_input_size / total_output_size if total_output_size > 0 else 0
        overall_saving = (1 - total_output_size / total_input_size) * 100
        print(f"  原始总大小: {format_size(total_input_size)}")
        print(f"  压缩后总大小: {format_size(total_output_size)}")
        print(f"  总体压缩比: {overall_ratio:.1f}x")
        print(f"  总体节省: {overall_saving:.0f}%")
        print(f"  总耗时: {format_duration(total_elapsed)}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
    os.system("pause")
