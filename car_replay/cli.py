"""命令行入口：参数解析与装配 DurationResolver / pipeline。"""

from __future__ import annotations

import argparse
import os
import sys

from . import console
from .duration import DurationResolver
from .naming import _contains_combined_path
from .pipeline import process_videos_in_folder


def _pause_before_exit():
    if os.name == "nt":
        os.system("pause")
        return

    try:
        input("Press Enter to exit...")
    except EOFError:
        pass


def main():
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
    parser.add_argument(
        "--no-ffprobe-duration",
        action="store_true",
        help="禁用时长探测（Windows 元数据/ffprobe），只使用 --clip-duration-seconds 兜底",
    )
    parser.add_argument(
        "--no-windows-metadata-duration",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--exact-duration-probing",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-hybrid",
        action="store_true",
        help="禁用混合模式（不预扫输入健康度，启用 --compress 时坏输入也强制走 NVENC）",
    )
    parser.add_argument("--allow-combined-input", action="store_true", help="允许从路径包含 _Combined 的目录读取")
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="时长/健康缓存目录（默认 <src>/.car_replay_cache）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="禁用磁盘缓存（每次重新 probe，调试用）",
    )
    parser.add_argument(
        "--probe-workers",
        type=int,
        default=4,
        help="ffprobe 并发线程数（默认 4）",
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=60.0,
        help="单文件 ffprobe 超时秒数（默认 60）",
    )
    parser.add_argument(
        "--no-broken-split",
        action="store_true",
        help="跳过整体损坏文件健康探测（快速但可能漏识别坏文件）",
    )
    parser.add_argument(
        "--verbose-ffmpeg",
        action="store_true",
        help="原样透传 ffmpeg stderr（关闭单行覆盖式进度，用于排障）",
    )
    parser.add_argument(
        "--verbose-cmd",
        action="store_true",
        help="完整打印 ffmpeg 命令行 + 文件列表（默认折叠为简短形式）",
    )
    args = parser.parse_args()

    if args.no_windows_metadata_duration:
        print(
            "[deprecated] --no-windows-metadata-duration is now a no-op "
            "(Windows shell metadata path removed)",
            file=sys.stderr,
        )
    if args.exact_duration_probing:
        print(
            "[deprecated] --exact-duration-probing is now a no-op "
            "(adaptive duration sampling removed)",
            file=sys.stderr,
        )

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

    console.kv("Output folder", target_folder_base)
    if enable_compress:
        cq_info = f"CQ override: {args.cq}" if args.cq else "使用通道默认值"
        console.kv("Compression", f"ENABLED ({cq_info})")
    else:
        console.kv("Compression", "DISABLED")

    if args.cache_dir:
        cache_path = os.path.join(args.cache_dir, "cache.json")
    else:
        cache_path = os.path.join(src_folder, ".car_replay_cache", "cache.json")

    duration_resolver = DurationResolver(
        enabled=not args.no_ffprobe_duration,
        fallback_seconds=args.clip_duration_seconds,
        track_health=enable_compress and not args.no_hybrid,
        cache_path=cache_path,
        use_cache=not args.no_cache,
        probe_workers=args.probe_workers,
        probe_timeout=args.probe_timeout,
        with_health=not args.no_broken_split,
    )

    if duration_resolver.track_health:
        console.kv("Hybrid mode", "ON (unhealthy inputs → -c copy per group)")

    process_videos_in_folder(
        src_folder,
        target_folder_base,
        enable_compress,
        args.cq,
        args.max_gap_seconds,
        duration_resolver,
        broken_split=not args.no_broken_split,
        verbose_ffmpeg=args.verbose_ffmpeg,
        verbose_cmd=args.verbose_cmd,
    )
    _pause_before_exit()
