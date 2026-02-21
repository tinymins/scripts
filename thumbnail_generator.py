"""
Generate contact sheet thumbnails for one or more video files.

The script accepts these parameters (flags or interactive input):
- M / --cols: number of thumbnails per row
- N / --rows: number of thumbnails per column
- W / --width: maximum thumbnail width in pixels
- H / --height: maximum thumbnail height in pixels
- --gap: spacing between thumbnails in pixels
- --margin: outer border around the contact sheet

Usage examples:
    python thumbnail_generator.py -M 4 -N 5 -W 320 -H 180 --gap 5 video.mp4
    python thumbnail_generator.py (drag videos onto the script)
"""

from __future__ import annotations

import argparse
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List

from PIL import Image

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".wmv",
    ".flv",
    ".ts",
    ".m4v",
    ".webm",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".m2ts",
    ".mts",
    ".ogv",
    ".vob",
    ".f4v",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate contact sheet thumbnails for video files."
    )
    parser.add_argument("videos", nargs="*", help="Video files to process.")
    parser.add_argument(
        "-M",
        "--cols",
        type=int,
        default=3,
        help="Thumbnails per row (default: 3).",
    )
    parser.add_argument(
        "-N",
        "--rows",
        type=int,
        default=4,
        help="Thumbnails per column (default: 4).",
    )
    parser.add_argument(
        "-W",
        "--width",
        type=int,
        default=320,
        help="Maximum thumbnail width in pixels (default: 320).",
    )
    parser.add_argument(
        "-H",
        "--height",
        type=int,
        default=180,
        help="Maximum thumbnail height in pixels (default: 180).",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=5,
        help="Spacing between thumbnails in pixels (default: 5).",
    )
    parser.add_argument(
        "--margin",
        type=int,
        default=5,
        help="Outer margin around contact sheet in pixels (default: 5).",
    )
    return parser.parse_args()


def ensure_positive(value: int | None, label: str, interactive: bool) -> int:
    if value is not None:
        if value <= 0:
            print(f"参数 {label} 必须为正整数。", file=sys.stderr)
            sys.exit(2)
        return value
    if not interactive:
        print(f"缺少必需的参数 {label}。", file=sys.stderr)
        sys.exit(2)
    while True:
        raw = input(f"请输入 {label} (正整数): ").strip()
        try:
            candidate = int(raw)
            if candidate <= 0:
                raise ValueError
            return candidate
        except ValueError:
            print("输入无效，请重新输入。")


def ensure_non_negative(
    value: int | None,
    label: str,
    interactive: bool,
) -> int:
    if value is not None:
        if value < 0:
            print(f"参数 {label} 不能为负数。", file=sys.stderr)
            sys.exit(2)
        return value
    if not interactive:
        print(f"缺少参数 {label}。", file=sys.stderr)
        sys.exit(2)
    while True:
        raw = input(f"请输入 {label} (非负整数): ").strip()
        try:
            candidate = int(raw)
            if candidate < 0:
                raise ValueError
            return candidate
        except ValueError:
            print("输入无效，请重新输入。")


def strip_wrapping_quotes(text: str) -> str:
    stripped = text.strip()
    if (
        len(stripped) >= 2
        and stripped[0] == stripped[-1]
        and stripped[0] in {"'", '"'}
    ):
        return stripped[1:-1]
    return stripped


def prompt_for_videos(interactive: bool) -> List[Path]:
    if not interactive:
        print("未提供视频文件路径。", file=sys.stderr)
        sys.exit(2)
    raw = input("请输入视频文件路径，多个文件使用分号分隔: ").strip()
    if not raw:
        print("未提供视频文件路径。", file=sys.stderr)
        sys.exit(2)
    normalized = raw.replace(";", " ")
    try:
        tokens = shlex.split(normalized, posix=False)
    except ValueError:
        print("输入格式无效，请检查路径引用。", file=sys.stderr)
        sys.exit(2)
    entries = [
        strip_wrapping_quotes(token)
        for token in tokens
        if token.strip()
    ]
    if not entries:
        print("未提供视频文件路径。", file=sys.stderr)
        sys.exit(2)
    return [Path(entry).expanduser() for entry in entries]


def locate_tool(tool_name: str) -> str:
    script_dir = Path(__file__).resolve().parent
    candidates: Iterable[Path] = (
        script_dir / "third_party" / "ffmpeg" / f"{tool_name}.exe",
        script_dir / "third_party" / "ffmpeg" / tool_name,
        script_dir / f"{tool_name}.exe",
        script_dir / tool_name,
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    located = shutil.which(tool_name)
    if located:
        return located
    alt = shutil.which(f"{tool_name}.exe")
    if alt:
        return alt
    raise FileNotFoundError(tool_name)


def get_video_duration(video: Path, ffprobe_path: str) -> float:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "无法获取视频时长。"
        raise RuntimeError(message)
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("ffprobe 返回的时长无效。") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("视频时长无效。")
    return duration


def get_video_dimensions(video: Path, ffprobe_path: str) -> tuple[int, int]:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(video),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "无法获取视频分辨率。"
        raise RuntimeError(message)
    text = result.stdout.strip()
    if not text:
        raise RuntimeError("未从 ffprobe 获得视频分辨率。")
    line = text.splitlines()[0]
    parts = line.split("x")
    if len(parts) != 2:
        raise RuntimeError("ffprobe 返回的分辨率格式无效。")
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:  # noqa: BLE001
        raise RuntimeError("ffprobe 返回的分辨率无效。") from exc
    if width <= 0 or height <= 0:
        raise RuntimeError("视频分辨率无效。")
    return width, height


def compute_scaled_dimensions(
    source_width: int,
    source_height: int,
    max_width: int,
    max_height: int,
) -> tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        raise ValueError("视频分辨率无效。")
    width_scale = max_width / source_width
    height_scale = max_height / source_height
    scale = min(width_scale, height_scale)
    if scale <= 0:
        raise ValueError("无法根据设定的最大尺寸计算缩略图大小。")
    scaled_width = max(1, min(max_width, int(source_width * scale)))
    scaled_height = max(1, min(max_height, int(source_height * scale)))
    return scaled_width, scaled_height


def compute_time_points(duration: float, total_frames: int) -> List[float]:
    interval = duration / (total_frames + 1)
    return [interval * (index + 1) for index in range(total_frames)]


def format_timestamp(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    whole_seconds = int(seconds)
    milliseconds = int(round((seconds - whole_seconds) * 1000))
    if milliseconds == 1000:
        whole_seconds += 1
        milliseconds = 0
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def extract_frame(
    ffmpeg_path: str,
    video: Path,
    timestamp: float,
    width: int,
    height: int,
    output_path: Path,
) -> None:
    vf_filter = f"scale={width}:{height}"
    command = [
        ffmpeg_path,
        "-loglevel",
        "error",
        "-ss",
        format_timestamp(timestamp),
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        vf_filter,
        "-y",
        str(output_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "无法截取缩略图帧。"
        raise RuntimeError(message)
    if not output_path.exists():
        raise RuntimeError("未生成缩略图帧文件。")


def load_images(image_paths: Iterable[Path]) -> List[Image.Image]:
    images: List[Image.Image] = []
    for path in image_paths:
        with Image.open(path) as img:
            images.append(img.convert("RGB"))
    return images


def compose_contact_sheet(
    images: List[Image.Image],
    cols: int,
    rows: int,
    gap: int,
    margin: int,
) -> Image.Image:
    if not images:
        raise ValueError("没有可用的缩略图图像。")
    thumb_width, thumb_height = images[0].size
    sheet_width = cols * thumb_width + (cols - 1) * gap + 2 * margin
    sheet_height = rows * thumb_height + (rows - 1) * gap + 2 * margin
    sheet = Image.new(
        "RGB",
        (sheet_width, sheet_height),
        color=(0, 0, 0),
    )
    for index, image in enumerate(images):
        row = index // cols
        col = index % cols
        x_offset = margin + col * (thumb_width + gap)
        y_offset = margin + row * (thumb_height + gap)
        sheet.paste(image, (x_offset, y_offset))
    return sheet


def set_file_times(target: Path, source: Path) -> None:
    source_stat = source.stat()
    os.utime(target, (source_stat.st_atime, source_stat.st_mtime))
    if os.name != "nt":
        return
    try:
        set_windows_creation_time(
            target,
            source_stat.st_ctime,
            source_stat.st_atime,
            source_stat.st_mtime,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"警告: 无法设置文件创建时间 ({exc}).")


def set_windows_creation_time(
    target: Path,
    ctime: float,
    atime: float,
    mtime: float,
) -> None:
    import ctypes
    from ctypes import wintypes

    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_WRITE_ATTRIBUTES = 0x00000100
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    def to_filetime(timestamp: float) -> wintypes.FILETIME:
        epoch_as_filetime = 116444736000000000
        intervals = int(timestamp * 10_000_000) + epoch_as_filetime
        return wintypes.FILETIME(intervals & 0xFFFFFFFF, intervals >> 32)

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(
        str(target),
        FILE_WRITE_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        creation_time = to_filetime(ctime)
        access_time = to_filetime(atime)
        modified_time = to_filetime(mtime)
        if not kernel32.SetFileTime(
            handle,
            ctypes.byref(creation_time),
            ctypes.byref(access_time),
            ctypes.byref(modified_time),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def process_video(
    video: Path,
    ffmpeg_path: str,
    ffprobe_path: str,
    cols: int,
    rows: int,
    max_width: int,
    max_height: int,
    gap: int,
    margin: int,
) -> None:
    try:
        source_width, source_height = get_video_dimensions(video, ffprobe_path)
    except RuntimeError as exc:
        print(f"[{video}] 获取视频分辨率失败: {exc}")
        return
    try:
        duration = get_video_duration(video, ffprobe_path)
    except RuntimeError as exc:
        print(f"[{video}] 获取视频时长失败: {exc}")
        return

    try:
        thumb_width, thumb_height = compute_scaled_dimensions(
            source_width,
            source_height,
            max_width,
            max_height,
        )
    except ValueError as exc:
        print(f"[{video}] 计算缩略图尺寸失败: {exc}")
        return

    total_frames = cols * rows
    time_points = compute_time_points(duration, total_frames)
    with tempfile.TemporaryDirectory(prefix="thumbs_") as tmp_dir:
        frame_paths: List[Path] = []
        for index, timestamp in enumerate(time_points):
            frame_path = Path(tmp_dir) / f"frame_{index:03d}.png"
            try:
                extract_frame(
                    ffmpeg_path,
                    video,
                    timestamp,
                    thumb_width,
                    thumb_height,
                    frame_path,
                )
            except RuntimeError as exc:
                print(f"[{video}] 截取第 {index + 1} 张缩略图失败: {exc}")
                return
            frame_paths.append(frame_path)
        try:
            images = load_images(frame_paths)
        except Exception as exc:  # noqa: BLE001
            print(f"[{video}] 读取缩略图图像失败: {exc}")
            return
    contact_sheet = compose_contact_sheet(images, cols, rows, gap, margin)
    output_path = video.with_suffix(".png")
    try:
        contact_sheet.save(output_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[{video}] 保存缩略图图像失败: {exc}")
        return
    try:
        set_file_times(output_path, video)
    except Exception as exc:  # noqa: BLE001
        print(f"[{video}] 设置文件时间失败: {exc}")
    print(f"已生成缩略图: {output_path}")


def main() -> None:
    args = parse_args()
    interactive = sys.stdin.isatty()

    cols = ensure_positive(args.cols, "M (横向数量)", interactive)
    rows = ensure_positive(args.rows, "N (纵向数量)", interactive)
    max_width = ensure_positive(args.width, "W (缩略图最大宽度)", interactive)
    max_height = ensure_positive(args.height, "H (缩略图最大高度)", interactive)
    gap = ensure_non_negative(args.gap, "间隔像素", interactive)
    margin = ensure_non_negative(args.margin, "外边距像素", interactive)

    videos = [Path(entry).expanduser() for entry in args.videos]
    if not videos:
        videos = prompt_for_videos(interactive)

    try:
        ffmpeg_path = locate_tool("ffmpeg")
        ffprobe_path = locate_tool("ffprobe")
    except FileNotFoundError as exc:
        print(
            f"未找到工具 {exc.args[0]}，请确保 ffmpeg/ffprobe 在 PATH 中或放在脚本目录。",
            file=sys.stderr,
        )
        sys.exit(1)

    for video in videos:
        resolved = (
            video
            if video.is_absolute()
            else (Path.cwd() / video).resolve()
        )
        if not resolved.exists() or not resolved.is_file():
            print(f"文件不存在: {resolved}")
            continue
        if resolved.suffix.lower() not in VIDEO_EXTENSIONS:
            print(f"不支持的文件类型，已跳过: {resolved}")
            continue
        output_path = resolved.with_suffix(".png")
        if output_path.exists():
            print(f"输出文件已存在，已跳过: {output_path}")
            continue
        process_video(
            resolved,
            ffmpeg_path,
            ffprobe_path,
            cols,
            rows,
            max_width,
            max_height,
            gap,
            margin,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("操作已取消。")
        sys.exit(130)
