"""命令行入口：参数解析与装配 DurationResolver / pipeline。"""

from __future__ import annotations

import argparse
import os

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
        help="禁用 Windows Shell 元数据快速读取，直接使用 ffprobe/自适应 ffprobe",
    )
    parser.add_argument(
        "--exact-duration-probing",
        action="store_true",
        help="禁用自适应抽样；需要时对每个视频精确探测时长（会更慢）",
    )
    parser.add_argument(
        "--no-hybrid",
        action="store_true",
        help="禁用混合模式（不预扫输入健康度，启用 --compress 时坏输入也强制走 NVENC）",
    )
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
        use_windows_metadata=not args.no_windows_metadata_duration,
        adaptive_sampling=not args.exact_duration_probing,
        track_health=enable_compress and not args.no_hybrid,
    )

    if duration_resolver.track_health:
        print("Hybrid mode ON: unhealthy inputs will fall back to -c copy per group")

    process_videos_in_folder(
        src_folder,
        target_folder_base,
        enable_compress,
        args.cq,
        args.max_gap_seconds,
        duration_resolver,
    )
    _pause_before_exit()
