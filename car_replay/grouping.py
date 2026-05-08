"""按摄像头/时间分组，构造合并文件名。"""

from __future__ import annotations

from datetime import datetime

from .duration import _effective_end
from .naming import (
    _basename,
    _mp4_name_for_transport_stream,
    extract_camera_key,
    parse_video_filename,
)


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
    for series_index, video_series in enumerate(video_camera_groups, start=1):
        print(
            f"Grouping camera series {series_index}/{len(video_camera_groups)} "
            f"({len(video_series)} files)..."
        )
        video_series.sort(key=_video_sort_key)
        if duration_resolver:
            duration_resolver.prepare_series(video_series)
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
