"""
行车记录仪视频压缩参数测试脚本

对三个通道(AA/AB/AC)各取一个短视频，分别用不同CQ值进行压缩，
输出到 compress_test/ 目录下供对比画质和文件大小。

使用 NVIDIA NVENC (hevc_nvenc) 硬件加速编码。

通道信息：
  AA - 前摄 3840x2160 4K, 原始码率 ~30Mbps
  AB - 后摄 1920x1080, 原始码率 ~8.4Mbps
  AC - 车内 1920x1080, 原始码率 ~8.4Mbps
"""

import os
import subprocess
import sys
import time

# ============================================================
# 配置区 - 可根据需要修改
# ============================================================

# 源视频目录
SOURCE_DIR = r"\\10.8.28.10\iot\360CAR_Combined\360CARDVR\REC"

# 输出目录（相对于本脚本所在目录）
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compress_test")

# ffmpeg 路径（与本脚本同目录）
FFMPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "third_party", "ffmpeg", "ffmpeg.exe")
FFPROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "third_party", "ffmpeg", "ffprobe.exe")

# 测试用的样本文件（约1分钟的短视频，三个通道各一个）
SAMPLE_FILES = {
    "AA": "20260205110136_20260205113236_000664AA.MP4",
    "AB": "20260205110136_20260205113237_000665AB.MP4",
    "AC": "20260205110139_20260205113236_000666AC.MP4",
}

# 要测试的CQ值列表（值越小画质越高、文件越大）
CQ_VALUES = [24, 28, 32]

# 各通道的压缩参数模板
# cq 会被测试循环替换，这里定义码率上限等参数
CHANNEL_PROFILES = {
    "AA": {  # 4K 前摄
        "bitrate": "8M",
        "maxrate": "12M",
        "bufsize": "16M",
        "preset": "p5",
    },
    "AB": {  # 1080p 后摄
        "bitrate": "3M",
        "maxrate": "5M",
        "bufsize": "8M",
        "preset": "p5",
    },
    "AC": {  # 1080p 车内
        "bitrate": "3M",
        "maxrate": "5M",
        "bufsize": "8M",
        "preset": "p5",
    },
}


def get_file_size_mb(filepath):
    """获取文件大小(MB)"""
    return os.path.getsize(filepath) / (1024 * 1024)


def get_video_info(filepath):
    """使用ffprobe获取视频信息"""
    cmd = [
        FFPROBE,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        import json

        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        bitrate = int(info.get("format", {}).get("bit_rate", 0)) // 1000  # kbps

        video_stream = None
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                video_stream = s
                break

        resolution = "unknown"
        codec = "unknown"
        if video_stream:
            resolution = f"{video_stream.get('width', '?')}x{video_stream.get('height', '?')}"
            codec = video_stream.get("codec_name", "unknown")

        return {
            "duration": duration,
            "bitrate_kbps": bitrate,
            "resolution": resolution,
            "codec": codec,
        }
    except Exception as e:
        print(f"  Warning: ffprobe failed: {e}")
        return {"duration": 0, "bitrate_kbps": 0, "resolution": "unknown", "codec": "unknown"}


def compress_video(input_path, output_path, cq, profile):
    """使用hevc_nvenc压缩单个视频"""
    cmd = [
        FFMPEG,
        "-y",  # 覆盖输出
        "-hwaccel", "cuda",
        "-i", input_path,
        "-c:v", "hevc_nvenc",
        "-preset", profile["preset"],
        "-rc", "vbr",
        "-cq", str(cq),
        "-b:v", profile["bitrate"],
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]

    print(f"  Command: {' '.join(cmd)}")
    start_time = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start_time

    if result.returncode != 0:
        print("  ERROR: ffmpeg failed!")
        print(f"  stderr: {result.stderr[-500:]}")
        return None

    return elapsed


def run_test():
    print("=" * 70)
    print("行车记录仪视频压缩参数测试")
    print("=" * 70)

    # 检查ffmpeg
    if not os.path.exists(FFMPEG):
        print(f"ERROR: ffmpeg not found at {FFMPEG}")
        sys.exit(1)

    # 创建输出目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 检查源文件
    print(f"\n源目录: {SOURCE_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")

    for channel, filename in SAMPLE_FILES.items():
        src = os.path.join(SOURCE_DIR, filename)
        if not os.path.exists(src):
            print(f"ERROR: Sample file not found: {src}")
            sys.exit(1)
        print(f"  {channel}: {filename} ({get_file_size_mb(src):.1f} MB)")

    # 汇总报告
    report_lines = []
    report_lines.append("=" * 80)
    report_lines.append("行车记录仪视频压缩参数测试报告")
    report_lines.append("=" * 80)

    # 对每个通道、每个CQ值进行测试
    for channel in ["AA", "AB", "AC"]:
        filename = SAMPLE_FILES[channel]
        src_path = os.path.join(SOURCE_DIR, filename)
        profile = CHANNEL_PROFILES[channel]

        src_size_mb = get_file_size_mb(src_path)
        src_info = get_video_info(src_path)

        print(f"\n{'='*70}")
        print(f"通道 {channel}: {filename}")
        print(f"  分辨率: {src_info['resolution']}, 编码: {src_info['codec']}")
        print(f"  时长: {src_info['duration']:.1f}s, 码率: {src_info['bitrate_kbps']} kbps")
        print(f"  文件大小: {src_size_mb:.1f} MB")
        print(f"  压缩参数: bitrate={profile['bitrate']}, maxrate={profile['maxrate']}")
        print(f"{'='*70}")

        report_lines.append("")
        report_lines.append(f"通道 {channel}: {filename}")
        report_lines.append(
            f"  原始: {src_info['resolution']}, {src_info['codec']}, "
            f"{src_info['bitrate_kbps']} kbps, {src_info['duration']:.1f}s, "
            f"{src_size_mb:.1f} MB"
        )
        report_lines.append(
            f"  参数: preset={profile['preset']}, "
            f"bitrate={profile['bitrate']}, maxrate={profile['maxrate']}"
        )

        for cq in CQ_VALUES:
            output_name = f"{channel}_CQ{cq}.MP4"
            output_path = os.path.join(OUTPUT_DIR, output_name)

            print(f"\n  测试 CQ={cq} -> {output_name}")

            if os.path.exists(output_path):
                print("  已存在，跳过压缩")
                out_size_mb = get_file_size_mb(output_path)
                out_info = get_video_info(output_path)
                elapsed = 0
            else:
                elapsed = compress_video(src_path, output_path, cq, profile)
                if elapsed is None:
                    report_lines.append(f"  CQ {cq}: FAILED")
                    continue
                out_size_mb = get_file_size_mb(output_path)
                out_info = get_video_info(output_path)

            ratio = src_size_mb / out_size_mb if out_size_mb > 0 else 0
            saving_pct = (1 - out_size_mb / src_size_mb) * 100 if src_size_mb > 0 else 0

            result_text = (
                f"  CQ {cq}: {out_size_mb:.1f} MB, "
                f"{out_info['bitrate_kbps']} kbps, "
                f"压缩比 {ratio:.1f}x, "
                f"节省 {saving_pct:.0f}%, "
                f"耗时 {elapsed:.1f}s"
            )
            print(result_text)
            report_lines.append(result_text)

    # 写入报告文件
    report_lines.append("")
    report_lines.append("=" * 80)
    report_lines.append("评估建议:")
    report_lines.append("  1. 用播放器对比各CQ值的画质（重点关注车牌可读性、路面细节）")
    report_lines.append("  2. CQ值越小画质越好但文件越大，推荐从CQ28开始评估")
    report_lines.append("  3. 如果CQ28画质满足需求，可以尝试CQ30/32进一步减小文件")
    report_lines.append("  4. 如果CQ28画质不够，可以降到CQ24/26")
    report_lines.append("  5. 确定CQ值后修改 compress_existing.py 和 combine-car-replay.py 中的参数")
    report_lines.append("=" * 80)

    report_path = os.path.join(OUTPUT_DIR, "compression_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    print(f"\n{'='*70}")
    print(f"测试完成！报告已保存到: {report_path}")
    print(f"输出文件目录: {OUTPUT_DIR}")
    print(f"{'='*70}")

    # 打印汇总表格
    print("\n" + "\n".join(report_lines))


if __name__ == "__main__":
    run_test()
    os.system("pause")
