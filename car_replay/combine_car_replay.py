import argparse
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta

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

VIDEO_EXTS = {".mp4", ".ts"}

# ffmpeg / ffprobe 路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "..", ".vendor", "ffmpeg", "ffmpeg.exe")
FFPROBE = os.path.join(SCRIPT_DIR, "..", ".vendor", "ffmpeg", "ffprobe.exe")


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


class DurationResolver:
    def __init__(self, enabled=True, fallback_seconds=None):
        self.enabled = enabled
        self.fallback_seconds = fallback_seconds
        self.cache = {}

    def duration_for_file(self, path):
        cache_key = os.path.abspath(os.path.normpath(path))
        if cache_key not in self.cache:
            self.cache[cache_key] = self._resolve_duration(path)
        return self.cache[cache_key]

    def _resolve_duration(self, path):
        basename = _basename(path)
        if not self.enabled:
            return self._fallback(
                basename,
                "ffprobe duration probing disabled",
                warn_without_fallback=True,
            )

        if not os.path.exists(FFPROBE):
            return self._fallback(basename, f"ffprobe not found: {FFPROBE}")

        command = [
            FFPROBE,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return self._fallback(basename, f"ffprobe failed: {exc}")

        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            return self._fallback(basename, f"ffprobe failed: {detail}")

        try:
            duration = float(result.stdout.strip())
        except ValueError:
            return self._fallback(basename, f"invalid ffprobe duration: {result.stdout.strip()!r}")

        if math.isfinite(duration) and duration > 0:
            return duration
        return self._fallback(basename, f"invalid ffprobe duration: {duration!r}")

    def _fallback(self, basename, reason, warn_without_fallback=False):
        if self.fallback_seconds is not None:
            print(
                f"WARNING: {reason} for {basename}; "
                f"using fallback duration {self.fallback_seconds}s"
            )
            return self.fallback_seconds
        if warn_without_fallback:
            print(f"WARNING: {reason} for {basename}; duration unavailable")
        else:
            print(f"ERROR: {reason} for {basename}; duration unavailable")
        return None


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
    def __init__(
        self,
        datetime_obj=None,
        end_datetime=None,
        rest_of_filename=None,
        max_time_difference=None,
        camera_key=None,
    ):
        self.datetime = datetime_obj
        self.end_datetime = end_datetime
        self.rest_of_filename = rest_of_filename
        self.max_time_difference = max_time_difference
        self.camera_key = camera_key

def parse_video_filename(filename):
    # 合并后格式：20250419195801_20250419200101_000785AC.MP4
    # 必须先于旧格式解析，否则第二个时间戳会被当成 rest_of_filename。
    match = re.match(r"(\d{14})_(\d{14})_(.+\.(?:MP4|TS))$", filename, re.IGNORECASE)
    if match:
        start_datetime_str = match.group(1)
        end_datetime_str = match.group(2)
        rest_of_filename = _mp4_name_for_transport_stream(match.group(3))
        return VideoInfo(
            datetime_obj=datetime.strptime(start_datetime_str, "%Y%m%d%H%M%S"),
            end_datetime=datetime.strptime(end_datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename=rest_of_filename,
            max_time_difference=120,
        )

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
    match = re.match(r"[A-Za-z]+(\d{8})-(\d{6})-(\d+[A-Za-z]+\.(?:MP4|TS))", filename, re.IGNORECASE)
    if match:
        date_str = match.group(1)
        time_str = match.group(2)
        rest_of_filename = _mp4_name_for_transport_stream(match.group(3))
        datetime_str = date_str + time_str
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename=rest_of_filename,
            max_time_difference=200
        )

    # LS_AR_IMX335: MOV2084_20260503165638.mp4 / LOK0051_20260318094443.mp4
    # Prefixes such as MOV/LOK/LOCK only indicate protection state and must not
    # split continuous clips.
    match = re.match(r"[A-Za-z]*\d+_(\d{14})\.(?:MP4|TS)$", filename, re.IGNORECASE)
    if match:
        datetime_str = match.group(1)
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename="AR_IMX335.mp4",
            max_time_difference=120,
            camera_key="AR_IMX335",
        )

    # LS_S3: 20260503_15h10m04s.ts / 20260429_11h20m35s-2.ts
    match = re.match(r"(\d{8})_(\d{2})h(\d{2})m(\d{2})s(?:-\d+)?\.(?:MP4|TS)$", filename, re.IGNORECASE)
    if match:
        datetime_str = "".join(match.groups())
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename="LS_S3.mp4",
            max_time_difference=120,
            camera_key="LS_S3",
        )

    return VideoInfo()

def _basename(path):
    components = _path_components(path)
    return components[-1] if components else os.path.basename(path)

def _mp4_name_for_transport_stream(filename):
    root, ext = os.path.splitext(filename)
    if ext.lower() == ".ts":
        return root + ".mp4"
    return filename

def _is_video_file(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTS

def _effective_end(info, video_path=None, duration_resolver=None):
    if info.end_datetime:
        return info.end_datetime
    if not info.datetime:
        return None
    if video_path and duration_resolver:
        duration_seconds = duration_resolver.duration_for_file(video_path)
        if duration_seconds is not None:
            return info.datetime + timedelta(seconds=duration_seconds)
    else:
        duration_seconds = None
    if duration_seconds is not None:
        return info.datetime + timedelta(seconds=duration_seconds)
    return None

def _contains_combined_path(path):
    return "_combined" in path.lower()

def extract_camera_id(filename):
    # 合并后格式：从 "20250419195801_20250419200101_000785AC.MP4" 提取 "AC"
    match = re.match(r"\d{14}_\d{14}_\d+([A-Z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    # 原有格式：从 "20250419195801_000785AC.MP4" 提取 "AC"
    match = re.match(r"\d{14}_\d+([A-Z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    # 新格式：从 "NO20200101-001521-002110B.mp4" 提取 "B"
    match = re.match(r"[A-Za-z]+\d{8}-\d{6}-\d+([A-Za-z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    return None

def _path_components(path):
    return [part for part in re.split(r"[\\/]+", path) if part]

def _infer_device_key(path, src_folder=None):
    components = _path_components(path)
    for part in components:
        if re.match(r"LS_[A-Za-z0-9_]+$", part, re.IGNORECASE):
            return part.upper()

    if src_folder:
        try:
            relative = os.path.relpath(path, src_folder)
            relative_parts = _path_components(relative)
            if len(relative_parts) > 1:
                return relative_parts[0].upper()
        except ValueError:
            pass

    parent = os.path.basename(os.path.dirname(path))
    return parent.upper() if parent else "SINGLE_CAMERA"

def extract_camera_key(video_path, src_folder=None):
    basename = _basename(video_path)
    camera_id = extract_camera_id(basename)
    if camera_id:
        return camera_id

    info = parse_video_filename(basename)
    if info.camera_key:
        return f"{info.camera_key}:{_infer_device_key(video_path, src_folder)}"

    return None

def group_videos_by_camera(videos, src_folder=None):
    # 按照摄像机ID进行初始分组
    camera_groups = {}
    for video in videos:
        camera_key = extract_camera_key(video, src_folder)
        if camera_key not in camera_groups:
            camera_groups[camera_key] = []
        camera_groups[camera_key].append(video)

    # 返回所有分组
    return list(camera_groups.values())

def _video_sort_key(path):
    basename = _basename(path)
    info = parse_video_filename(basename)
    if info.datetime:
        return (info.datetime, basename)
    return (datetime.max, basename)

def group_videos_by_time(video_camera_groups, max_gap_seconds=None, duration_resolver=None):
    final_groups = []

    # 对每个摄像机组内的视频按时间进行进一步分组
    for video_series in video_camera_groups:
        video_series.sort(key=_video_sort_key)
        time_grouped = []
        current_group = []

        for i, video in enumerate(video_series):
            if i == 0:
                current_group.append(video)
                continue

            current_info = parse_video_filename(_basename(video))
            previous_info = parse_video_filename(_basename(video_series[i - 1]))

            previous_video = video_series[i - 1]
            previous_effective_end = _effective_end(
                previous_info,
                previous_video,
                duration_resolver,
            )
            if not current_info.datetime or not previous_effective_end:
                time_grouped.append(current_group)
                current_group = [video]
                continue

            time_diff = (current_info.datetime - previous_effective_end).total_seconds()
            max_time_difference = (
                max_gap_seconds
                if max_gap_seconds is not None
                else current_info.max_time_difference
            )
            if max_time_difference is not None and time_diff <= max_time_difference:
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

def create_combined_filename(first_video, last_video, duration_resolver=None):
    """创建合并后的文件名，格式为：第一个视频时间_最后一个视频结束时间_其余部分.MP4"""
    first_basename = _basename(first_video)
    last_basename = _basename(last_video)

    first_info = parse_video_filename(first_basename)
    last_info = parse_video_filename(last_basename)

    if not first_info.datetime or not last_info.datetime:
        return _mp4_name_for_transport_stream(first_basename)  # 如果无法提取时间，返回原始文件名

    # 将datetime对象转换为字符串格式
    first_timestamp = first_info.datetime.strftime("%Y%m%d%H%M%S")
    last_effective_end = _effective_end(last_info, last_video, duration_resolver)
    if not last_effective_end:
        last_effective_end = last_info.datetime
    last_timestamp = last_effective_end.strftime("%Y%m%d%H%M%S")

    return f"{first_timestamp}_{last_timestamp}_{_mp4_name_for_transport_stream(first_info.rest_of_filename)}"

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
        subprocess.run(command, check=True)
    finally:
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)

def merge_videos(video_group, combined_file, enable_compress=False, cq_override=None):
    # 获取最后一个视频文件的时间属性
    last_video = video_group[-1]
    last_video_stats = os.stat(last_video)
    last_access_time = last_video_stats.st_atime
    last_mod_time = last_video_stats.st_mtime

    # 获取通道ID（从第一个视频文件名提取）
    camera_id = extract_camera_id(_basename(video_group[0]))

    if len(video_group) == 1 and enable_compress:
        # 单文件 + 压缩：直接从源压缩到目标
        print(f"Compressing single file: {video_group[0]} to {combined_file}")
        success, in_sz, out_sz, elapsed = compress_video(video_group[0], combined_file, camera_id, cq_override)
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
            "-fflags", "+genpts",
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
        # 不压缩：仅 stream copy 合并；.ts 也会 remux 到 .mp4 输出
        _copy_merge_videos(video_group, combined_file)
        print("Merge complete.")

    # 设置合并后文件的时间属性为最后一个视频文件的时间属性
    os.utime(combined_file, (last_access_time, last_mod_time))
    return 0, 0, 0

def process_videos_in_folder(
    src_folder,
    target_folder_base,
    enable_compress=False,
    cq_override=None,
    max_gap_seconds=None,
    duration_resolver=None,
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
        grouped_videos = group_videos_by_time(camera_groups, max_gap_seconds, duration_resolver)
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
            combined_file_name = create_combined_filename(first_video, last_video, duration_resolver)
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
    parser = argparse.ArgumentParser(
        description=(
            "行车记录仪视频合并（可选压缩）。源片段结束时间优先使用文件名中的显式结束时间；"
            "否则通过 ffprobe 读取真实时长。ffprobe 失败时仅在提供 --clip-duration-seconds "
            "时使用该显式兜底值。"
        )
    )
    parser.add_argument("--src", type=str, help="源文件夹路径")
    parser.add_argument("--compress", action="store_true", help="合并后进行NVENC压缩")
    parser.add_argument("--no-compress", action="store_true", help="合并后不压缩")
    parser.add_argument("--cq", type=int, help="全局覆盖CQ值（默认按通道使用预设值）")
    parser.add_argument("--max-gap-seconds", type=int, help="覆盖连续视频分组的最大时间间隔（秒）")
    parser.add_argument(
        "--clip-duration-seconds",
        type=int,
        help="ffprobe 不可用/失败或禁用时使用的显式兜底片段时长（秒）；未提供则不猜测时长",
    )
    parser.add_argument("--no-ffprobe-duration", action="store_true", help="禁用 ffprobe 时长探测，只使用 --clip-duration-seconds 兜底")
    parser.add_argument("--allow-combined-input", action="store_true", help="允许从路径包含 _Combined 的目录读取")
    args = parser.parse_args()

    src_folder = args.src
    if not src_folder:
        src_folder = input("Please enter the source folder path: ").strip().strip('"')
    src_folder = src_folder.rstrip("\\/")

    if _contains_combined_path(src_folder) and not args.allow_combined_input:
        raise SystemExit(
            "Refusing to process a source path containing _Combined. "
            "Use --allow-combined-input if this is intentional."
        )

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

    duration_resolver = DurationResolver(
        enabled=not args.no_ffprobe_duration,
        fallback_seconds=args.clip_duration_seconds,
    )

    process_videos_in_folder(
        src_folder,
        target_folder_base,
        enable_compress,
        args.cq,
        args.max_gap_seconds,
        duration_resolver,
    )
    os.system("pause")
