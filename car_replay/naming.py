"""文件名解析、camera id / key 推断、路径辅助。"""

from __future__ import annotations

import os
import re
from datetime import datetime

from .config import VIDEO_EXTS


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

    # 小米模式 A（顶层平铺）：NN_<开始YYYYMMDDhhmmss>_<结束YYYYMMDDhhmmss>.mp4
    # 例：00_20250516070050_20250516072431.mp4
    match = re.match(r"(\d{2})_(\d{14})_(\d{14})\.(?:MP4|TS)$", filename, re.IGNORECASE)
    if match:
        return VideoInfo(
            datetime_obj=datetime.strptime(match.group(2), "%Y%m%d%H%M%S"),
            end_datetime=datetime.strptime(match.group(3), "%Y%m%d%H%M%S"),
            rest_of_filename=f"Xiaomi_{match.group(1)}.mp4",
            max_time_difference=180,
            camera_key="XIAOMI_A",
        )

    # 小米模式 B（按小时分桶）：MMMSS_<unix_ts>.mp4
    # 例：00M56S_1747350056.mp4
    match = re.match(r"\d{2}M\d{2}S_(\d{10})\.(?:MP4|TS)$", filename, re.IGNORECASE)
    if match:
        ts = int(match.group(1))
        return VideoInfo(
            datetime_obj=datetime.fromtimestamp(ts),
            rest_of_filename="Xiaomi.mp4",
            max_time_difference=180,
            camera_key="XIAOMI_B",
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
        # 小米模式 A 顶层目录：XiaomiCamera_NN_<MAC>
        if re.match(r"XiaomiCamera_\d{2}_[0-9A-Fa-f]{12}$", part):
            return part
        # 小米模式 B MAC 子目录（12 位十六进制）
        if re.match(r"^[0-9a-fA-F]{12}$", part):
            return part.lower()

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


def xiaomi_rest_for_path(video_path, info):
    """根据路径上下文为小米模式 A/B 生成 rest_of_filename，附带设备短标识。"""
    if info.camera_key not in ("XIAOMI_A", "XIAOMI_B"):
        return info.rest_of_filename

    components = _path_components(video_path)
    if info.camera_key == "XIAOMI_A":
        # 期望路径上有 XiaomiCamera_NN_<MAC>；文件名已带 NN
        for part in components:
            m = re.match(r"XiaomiCamera_(\d{2})_([0-9A-Fa-f]{12})$", part)
            if m:
                return f"Xiaomi_{m.group(1)}_{m.group(2)[-4:].upper()}.mp4"
        return info.rest_of_filename

    # XIAOMI_B
    for part in components:
        if re.match(r"^[0-9a-fA-F]{12}$", part):
            return f"Xiaomi_{part[-4:].upper()}.mp4"
    return info.rest_of_filename
