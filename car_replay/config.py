"""配置常量与小工具：警告分类、压缩参数、ffmpeg 路径、通用 helper。"""

from __future__ import annotations

import os
import re
from typing import Optional

# ============================================================
# FFmpeg 警告分类与汇总
# ============================================================

# 严重门槛（命中任一即标 SUSPICIOUS，需要人工二次确认）
SUSPICIOUS_RULES = {
    "corrupt_frame": 1,
    "concealing": 8,
    "decode_error": 8,
    "slice_header": 8,
    "mb_decode": 8,
    "missing_ref": 1,
    "missing_picture": 1,
    "non_existing_pps": 1,
    "application_invalid": 1,
    "invalid_dts": 8,
    "nonmono_dts": 8,
    "guess_pts": 8,
    "bytestream": 8,
    "co_located_poc": 8,
}

# 模式按优先级匹配（先匹配的胜出，避免一行被算两次）
WARNING_PATTERNS = [
    ("corrupt_frame", re.compile(r"corrupt decoded frame|corrupt input|Corrupted frame", re.I)),
    ("concealing", re.compile(r"concealing\s+\d+|error concealment", re.I)),
    ("missing_ref", re.compile(r"reference picture missing|Missing reference picture|reference frame missing", re.I)),
    ("missing_picture", re.compile(r"missing picture in access unit|No start code|missing picture", re.I)),
    ("non_existing_pps", re.compile(r"non-existing PPS|non-existing SPS|sps_id .* out of range|pps_id .* out of range", re.I)),
    ("application_invalid", re.compile(r"Application provided invalid", re.I)),
    ("slice_header", re.compile(r"decode_slice_header error|slice header damaged", re.I)),
    ("mb_decode", re.compile(r"\bmb decoding\b|MB decoding error|cbp too large|ac-tex damaged|AC tex damaged|dc-tex damaged", re.I)),
    ("co_located_poc", re.compile(r"co located POCs unavailable|co-located", re.I)),
    ("bytestream", re.compile(r"bytestream", re.I)),
    ("decode_error", re.compile(r"error while decoding|error decoding|Error decoding|decoding error", re.I)),
    ("nonmono_dts", re.compile(r"non[- ]monoton(ous|ic) (DTS|PTS)|out of order", re.I)),
    ("invalid_dts", re.compile(r"Invalid (DTS|PTS)", re.I)),
    ("guess_pts", re.compile(r"replacing by guess|generating non-monotonous|generating non-monotonic", re.I)),
]

WARNING_LABELS = {
    "corrupt_frame": "画面损坏帧（corrupt decoded frame）",
    "concealing": "错误遮蔽（concealing）",
    "missing_ref": "参考帧丢失（reference picture missing）",
    "missing_picture": "图像缺失（missing picture）",
    "non_existing_pps": "流参数集错误（non-existing PPS/SPS）",
    "application_invalid": "应用层无效输入（Application provided invalid）",
    "slice_header": "切片头损坏（decode_slice_header error）",
    "mb_decode": "宏块解码错（MB decoding/AC tex/DC tex damaged）",
    "co_located_poc": "共置 POC 不可用（co located POCs unavailable）",
    "bytestream": "字节流错（bytestream）",
    "decode_error": "解码错误（error while decoding）",
    "nonmono_dts": "时间戳非单调（non-monotonous DTS/PTS）",
    "invalid_dts": "时间戳无效（Invalid DTS/PTS）",
    "guess_pts": "时间戳猜测替代（replacing by guess）",
}


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

# ============================================================
# 负压缩防护参数
# ============================================================

# Pre-flight：若输入平均码率 ≤ profile.bitrate × MARGIN，直接跳过 NVENC 走 copy
PREFLIGHT_BITRATE_MARGIN = 1.1

# 运行时迟滞状态机阈值
NEGATIVE_COMPRESSION_THRESHOLDS = {
    "warmup_secs": 30,           # 编码已运行的真实秒数门槛（wall-clock）
    "warmup_progress_pct": 0.05,  # 输出 time= 占 expected_duration 比例门槛
    "enter_bad_ratio": 0.95,     # predicted_final_ratio 超过此值进入 WARN
    "exit_good_ratio": 0.85,     # predicted_final_ratio 低于此值视作恢复
    "exit_ok_secs": 10,          # WARN 中连续多少秒 ratio < exit_good_ratio 则回 OK
    "abort_hold_secs": 20,       # WARN 持续多少秒后判定真正反向膨胀
    "abort_ratio": 1.0,          # 触发中断时还要求最近采样 ratio > 此值（兜底防抖）
}

# 文件夹级熔断：累计 N 次反向膨胀后，本文件夹剩余组全部跳过 NVENC（0 = 禁用）
NEGATIVE_COMPRESSION_FOLDER_BREAKER = 5

# runner 哨兵返回码：表示被监控状态机主动中断
NEGATIVE_COMPRESSION_ABORT_RC = -2

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


def format_eta(seconds: Optional[float]) -> str:
    """格式化剩余时间：None/0/负数 → '--'; <60s → '45s'; <3600s → '5m23s'; else → '2h15m'。"""
    if seconds is None or seconds <= 0:
        return "--"
    s = int(seconds)
    if s <= 0:
        return "--"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"
