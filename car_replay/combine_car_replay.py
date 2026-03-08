import argparse
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

# ============================================================
# 压缩参数配置 - 测试确认后可修改此处
# ============================================================

COMPRESS_PROFILES = {
    "AA": {  # 4K 前摄 3840x2160, 原始 ~30Mbps
        "cq": 32,
        "bitrate": "8M",
        "maxrate": "12M",
        "bufsize": "16M",
        "preset": "p7",
    },
    "AB": {  # 1080p 后摄, 原始 ~8.4Mbps
        "cq": 32,
        "bitrate": "3M",
        "maxrate": "5M",
        "bufsize": "8M",
        "preset": "p7",
    },
    "AC": {  # 1080p 车内, 原始 ~8.4Mbps
        "cq": 32,
        "bitrate": "3M",
        "maxrate": "5M",
        "bufsize": "8M",
        "preset": "p7",
    },
}

DEFAULT_PROFILE = {
    "cq": 32,
    "bitrate": "5M",
    "maxrate": "8M",
    "bufsize": "10M",
    "preset": "p7",
}

# ffmpeg 路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "..", ".vendor", "ffmpeg", "ffmpeg.exe")


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
    return f"{size_bytes / 1024:.1f} KB"


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

    start_time = time.time()
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL)
    elapsed = time.time() - start_time

    if result.returncode != 0:
        if os.path.exists(temp_output):
            os.remove(temp_output)
        print(f"  ERROR: Compression failed!")
        return False, 0, 0, elapsed

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
    return True, input_size, output_size, elapsed


class VideoInfo:
    def __init__(self, datetime_obj=None, rest_of_filename=None, max_time_difference=None):
        self.datetime = datetime_obj
        self.rest_of_filename = rest_of_filename
        self.max_time_difference = max_time_difference

def parse_video_filename(filename):
    # 原有格式：20250419195801_000785AC.MP4
    match = re.match(r"(\d{14})_(.+\.MP4)", filename, re.IGNORECASE)
    if match:
        datetime_str = match.group(1)
        rest_of_filename = match.group(2)
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename=rest_of_filename,
            max_time_difference=120
        )

    # 新格式：NO20200101-001521-002110B.mp4
    match = re.match(r"[A-Za-z]+(\d{8})-(\d{6})-(\d+[A-Za-z]+\.MP4)", filename, re.IGNORECASE)
    if match:
        date_str = match.group(1)
        time_str = match.group(2)
        rest_of_filename = match.group(3)
        datetime_str = date_str + time_str
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename=rest_of_filename,
            max_time_difference=200
        )

    return VideoInfo()

def extract_camera_id(filename):
    # 原有格式：从 "20250419195801_000785AC.MP4" 提取 "AC"
    match = re.match(r"\d{14}_\d+([A-Z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    # 新格式：从 "NO20200101-001521-002110B.mp4" 提取 "B"
    match = re.match(r"[A-Za-z]+\d{8}-\d{6}-\d+([A-Za-z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    return None

def group_videos_by_camera(videos):
    # 按照摄像机ID进行初始分组
    camera_groups = {}
    for video in videos:
        basename = os.path.basename(video)
        camera_id = extract_camera_id(basename)
        if camera_id not in camera_groups:
            camera_groups[camera_id] = []
        camera_groups[camera_id].append(video)

    # 返回所有分组
    return list(camera_groups.values())

def group_videos_by_time(video_camera_groups):
    final_groups = []

    # 对每个摄像机组内的视频按时间进行进一步分组
    for video_series in video_camera_groups:
        video_series.sort(key=lambda x: os.path.basename(x))
        time_grouped = []
        current_group = []

        for i, video in enumerate(video_series):
            if i == 0:
                current_group.append(video)
                continue

            current_info = parse_video_filename(os.path.basename(video))
            previous_info = parse_video_filename(os.path.basename(video_series[i - 1]))

            if current_info.datetime and previous_info.datetime:
                time_diff = (current_info.datetime - previous_info.datetime).total_seconds()
                if time_diff <= current_info.max_time_difference:
                    current_group.append(video)
                else:
                    time_grouped.append(current_group)
                    current_group = [video]

        if current_group:
            time_grouped.append(current_group)

        final_groups.extend(time_grouped)

    return final_groups

def check_file_exists(file_path):
    return os.path.exists(file_path)

def create_combined_filename(first_video, last_video):
    """创建合并后的文件名，格式为：第一个视频时间_最后一个视频时间_其余部分.MP4"""
    first_basename = os.path.basename(first_video)
    last_basename = os.path.basename(last_video)

    first_info = parse_video_filename(first_basename)
    last_info = parse_video_filename(last_basename)

    if not first_info.datetime or not last_info.datetime:
        return first_basename  # 如果无法提取时间，返回原始文件名

    # 将datetime对象转换为字符串格式
    first_timestamp = first_info.datetime.strftime("%Y%m%d%H%M%S")
    last_timestamp = last_info.datetime.strftime("%Y%m%d%H%M%S")

    return f"{first_timestamp}_{last_timestamp}_{first_info.rest_of_filename}"

def merge_videos(video_group, combined_file, enable_compress=False, cq_override=None):
    # 获取最后一个视频文件的时间属性
    last_video = video_group[-1]
    last_video_stats = os.stat(last_video)
    last_access_time = last_video_stats.st_atime
    last_mod_time = last_video_stats.st_mtime

    # 获取通道ID（从第一个视频文件名提取）
    camera_id = extract_camera_id(os.path.basename(video_group[0]))

    if len(video_group) == 1 and enable_compress:
        # 单文件 + 压缩：直接从源压缩到目标
        print(f"Compressing single file: {video_group[0]} to {combined_file}")
        success, in_sz, out_sz, elapsed = compress_video(video_group[0], combined_file, camera_id, cq_override)
        if success:
            os.utime(combined_file, (last_access_time, last_mod_time))
            return in_sz, out_sz, elapsed
        else:
            # 压缩失败时回退到直接复制
            print("  Compression failed, falling back to copy...")
            shutil.copy2(video_group[0], combined_file)
            return 0, 0, 0

    if len(video_group) == 1 and not enable_compress:
        # 单文件 + 不压缩：直接复制
        print(f"Copying single file: {video_group[0]} to {combined_file}")
        shutil.copy2(video_group[0], combined_file)
        return 0, 0, 0

    print(f"Merging {len(video_group)} files into: {combined_file}")
    print(f"Files to merge: {[os.path.basename(v) for v in video_group]}")

    # 写入 concat 列表
    concat_list_path = combined_file + ".concat_list.txt"
    with open(concat_list_path, "w") as f:
        for video in video_group:
            f.write(f"file '{video}'\n")

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
        start_time = time.time()
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL)
        elapsed = time.time() - start_time

        os.remove(concat_list_path)

        if result.returncode != 0:
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
        # 不压缩：仅 stream copy 合并
        command = [
            FFMPEG, '-f', 'concat', '-safe', '0', '-i', concat_list_path,
            '-c', 'copy', combined_file
        ]
        subprocess.run(command, check=True)
        os.remove(concat_list_path)
        print("Merge complete.")

    # 设置合并后文件的时间属性为最后一个视频文件的时间属性
    os.utime(combined_file, (last_access_time, last_mod_time))
    return 0, 0, 0

def process_videos_in_folder(src_folder, target_folder_base, enable_compress=False, cq_override=None):
    mp4_files = []
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
                if entry.name.lower().endswith('.mp4'):
                    mp4_files.append(entry.path)
                else:
                    other_files.append(entry.path)

    scan_folder(src_folder)
    print(f"Found {len(mp4_files)} MP4 files and {len(other_files)} other files.")

    # 处理MP4文件
    if mp4_files:
        # 按照摄像机ID进行初始分组
        camera_groups = group_videos_by_camera(mp4_files)
        print(f"MP4 files divided into {len(camera_groups)} different camera groups.")

        # 进一步按照时间关系进行分组
        grouped_videos = group_videos_by_time(camera_groups)
        total_groups = len(grouped_videos)
        print(f"Total video groups to process: {total_groups}")

        processed_groups = 0
        skipped_groups = 0
        failed_groups = 0
        total_input_size = 0
        total_output_size = 0
        total_elapsed = 0

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
            combined_file_name = create_combined_filename(first_video, last_video)
            combined_file_path = os.path.join(target_folder, combined_file_name)

            print(f"\nProcessing group {processed_groups}/{total_groups}: {combined_file_name}")
            print(f"Group contains {len(group)} files")

            if not check_file_exists(combined_file_path):
                in_sz, out_sz, elapsed = merge_videos(group, combined_file_path, enable_compress, cq_override)
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="行车记录仪视频合并（可选压缩）")
    parser.add_argument("--src", type=str, help="源文件夹路径")
    parser.add_argument("--compress", action="store_true", help="合并后进行NVENC压缩")
    parser.add_argument("--no-compress", action="store_true", help="合并后不压缩")
    parser.add_argument("--cq", type=int, help="全局覆盖CQ值（默认按通道使用预设值）")
    args = parser.parse_args()

    src_folder = args.src
    if not src_folder:
        src_folder = input("Please enter the source folder path: ").strip().strip('"')

    # 确定是否启用压缩
    if args.compress:
        enable_compress = True
    elif args.no_compress:
        enable_compress = False
    elif not args.src:
        # 交互模式：默认启用压缩
        compress_input = input("是否启用NVENC压缩？(Y/n): ").strip().lower()
        enable_compress = compress_input != "n"
    else:
        # CLI模式未指定：默认不压缩（向后兼容）
        enable_compress = False

    target_folder_base = os.path.join(
        os.path.dirname(src_folder), f"{os.path.basename(src_folder)}_Combined"
    )

    print(f"Output files will be placed in: {target_folder_base}")
    if enable_compress:
        cq_info = f"CQ override: {args.cq}" if args.cq else "使用通道默认值"
        print(f"Compression ENABLED ({cq_info})")
    else:
        print("Compression DISABLED")

    process_videos_in_folder(src_folder, target_folder_base, enable_compress, args.cq)
    os.system("pause")
