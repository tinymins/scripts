"""按摄像头/时间分组，构造合并文件名。"""

from __future__ import annotations

from datetime import datetime

from .duration import _effective_end
from . import console
from .naming import (
    _basename,
    _mp4_name_for_transport_stream,
    extract_camera_key,
    parse_video_filename,
    xiaomi_rest_for_path,
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


def group_videos_by_time(video_camera_groups, max_gap_seconds=None,
                         duration_resolver=None, *, broken_split=True,
                         max_group_duration_seconds=None):
    final_groups = []

    for series_index, video_series in enumerate(video_camera_groups, start=1):
        console.detail(
            f"Grouping camera series {series_index}/{len(video_camera_groups)} "
            f"({len(video_series)} files)..."
        )
        video_series.sort(key=_video_sort_key)
        if duration_resolver:
            duration_resolver.prepare_series(video_series)

        time_grouped = []
        current_group = []
        prev_video = None
        prev_info = None
        broken_files = []
        group_start_dt = None

        for video in video_series:
            if broken_split and duration_resolver is not None:
                try:
                    is_broken = duration_resolver.is_broken(video)
                except RuntimeError:
                    is_broken = False
                if is_broken:
                    broken_files.append(video)
                    if current_group:
                        time_grouped.append(current_group)
                        current_group = []
                    prev_video = None
                    prev_info = None
                    group_start_dt = None
                    continue

            current_info = parse_video_filename(_basename(video))
            if prev_video is None:
                current_group = [video]
                group_start_dt = current_info.datetime
            else:
                previous_effective_end = _effective_end(
                    prev_info, prev_video, duration_resolver,
                )
                if not current_info.datetime or not previous_effective_end:
                    time_grouped.append(current_group)
                    current_group = [video]
                    group_start_dt = current_info.datetime
                else:
                    time_diff = (
                        current_info.datetime - previous_effective_end
                    ).total_seconds()
                    max_time_difference = (
                        max_gap_seconds
                        if max_gap_seconds is not None
                        else current_info.max_time_difference
                    )
                    gap_ok = (
                        max_time_difference is not None
                        and time_diff <= max_time_difference
                    )
                    # 累计时长上限保护：以组首 start 到 candidate 末端衡量
                    duration_ok = True
                    if gap_ok and max_group_duration_seconds and group_start_dt:
                        candidate_end = _effective_end(
                            current_info, video, duration_resolver,
                        ) or current_info.datetime
                        accumulated = (candidate_end - group_start_dt).total_seconds()
                        if accumulated > max_group_duration_seconds:
                            duration_ok = False

                    if gap_ok and duration_ok:
                        current_group.append(video)
                    else:
                        time_grouped.append(current_group)
                        current_group = [video]
                        group_start_dt = current_info.datetime
            prev_video = video
            prev_info = current_info

        if current_group:
            time_grouped.append(current_group)

        if broken_files:
            console.warn(
                f"已剔除 {len(broken_files)} 个 broken 文件作为分组断点："
                f"e.g. {_basename(broken_files[0])}",
                indent=2,
            )

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

    rest = xiaomi_rest_for_path(first_video, first_info)
    rest = _mp4_name_for_transport_stream(rest)
    return f"{first_timestamp}_{last_timestamp}_{rest}"
