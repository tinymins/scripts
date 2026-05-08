"""FFmpeg 子进程执行 + 警告分类追踪 + 命令结果校验。"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .config import FFPROBE, SUSPICIOUS_RULES, WARNING_LABELS, WARNING_PATTERNS


def _run_ffprobe(path, timeout: float = 60.0) -> Tuple[Optional[float], bool]:
    """跑一次 ffprobe 取 format.duration。

    Returns (duration_or_None, broken_bool)。失败 / 非零返回码 / 解析失败 / 超时 / OSError → broken=True。
    """
    if not os.path.exists(FFPROBE):
        return (None, True)
    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return (None, True)
    if r.returncode != 0:
        return (None, True)
    raw = (r.stdout or "").strip()
    try:
        duration = float(raw)
    except ValueError:
        return (None, True)
    if not math.isfinite(duration) or duration <= 0:
        return (None, True)
    return (duration, False)


class WarningTracker:
    """逐行扫描 ffmpeg 输出，归类并计数警告 / 错误。"""

    def __init__(self):
        self.counts = {key: 0 for key, _ in WARNING_PATTERNS}
        self.first_examples = {}
        self.unmatched_error_lines = 0

    def feed(self, line):
        stripped = line.rstrip("\r\n")
        if not stripped:
            return
        for key, pattern in WARNING_PATTERNS:
            if pattern.search(stripped):
                self.counts[key] += 1
                if key not in self.first_examples:
                    self.first_examples[key] = stripped[:240]
                return
        if "error" in stripped.lower() and "@" in stripped and "frame=" not in stripped:
            self.unmatched_error_lines += 1

    @property
    def total_warnings(self):
        return sum(self.counts.values())

    UNMATCHED_ERROR_SUSPICIOUS_THRESHOLD = 50

    def is_suspicious(self):
        """三档之一：命中可疑规则或 unmatched_error_lines 超过绝对阈值。"""
        if self.unmatched_error_lines > self.UNMATCHED_ERROR_SUSPICIOUS_THRESHOLD:
            return True
        for key, threshold in SUSPICIOUS_RULES.items():
            if self.counts.get(key, 0) >= threshold:
                return True
        return False

    def is_clean(self):
        """三档之一：无任何 error 计数 + 不可疑。"""
        return (
            self.total_warnings == 0
            and self.unmatched_error_lines == 0
            and not self.is_suspicious()
        )

    def is_fatal(self):
        """三档之一：tracker 自身可识别的致命计数（当前无 fatal/panic 类正则，恒 False）。

        调用方应另行结合 returncode != 0 做最终致命判定。
        """
        return False

    def severity(self):
        if self.is_clean():
            return "OK"
        if self.is_suspicious():
            return "SUSPICIOUS"
        return "WARN"

    def category_summary(self):
        rows = []
        for key, _ in WARNING_PATTERNS:
            count = self.counts.get(key, 0)
            if count > 0:
                rows.append((key, count, WARNING_LABELS[key]))
        return rows

    def format_oneline(self):
        rows = self.category_summary()
        parts = [f"{key}={count}" for key, count, _ in rows]
        if self.unmatched_error_lines:
            parts.append(f"other_error_lines={self.unmatched_error_lines}")
        return ", ".join(parts) if parts else "no warnings"

    def format_detail(self):
        lines = [f"severity: {self.severity()}", f"total: {self.total_warnings}"]
        for key, count, label in self.category_summary():
            lines.append(f"  {label}: {count}")
            example = self.first_examples.get(key)
            if example:
                lines.append(f"    e.g.: {example}")
        if self.unmatched_error_lines:
            lines.append(f"  其它包含 'error' 的输出行: {self.unmatched_error_lines}")
        return "\n".join(lines)


def _run_ffmpeg_capturing_warnings(cmd):
    """运行 ffmpeg，实时把 stderr 透传到控制台并归类警告。

    返回 (returncode, elapsed_seconds, tracker)。
    """
    tracker = WarningTracker()
    start = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=1,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stderr is not None
    try:
        for raw_line in proc.stderr:
            sys.stderr.write(raw_line)
            sys.stderr.flush()
            tracker.feed(raw_line)
    finally:
        proc.wait()
    elapsed = time.time() - start
    return proc.returncode, elapsed, tracker


# ============================================================
# CommandResult：统一的可疑/失败判定 + 后探测校验
# ============================================================


# 频率阈值：ffmpeg 输出 error 行 / 分钟
ERROR_LINES_PER_MINUTE_THRESHOLD = 30


@dataclass
class CommandResult:
    """ffmpeg 一次运行的结果，便于上层做"是否需要降级"判定。

    expected_duration: 输入合计时长（秒）；用于 ±tolerance 比对。None 表示不做时长校验。
    """

    returncode: int
    elapsed: float
    tracker: WarningTracker
    output_path: Path
    expected_duration: Optional[float]

    def is_fatal(self) -> bool:
        if self.returncode != 0:
            return True
        if self.tracker.is_fatal():
            return True
        return False

    def is_suspicious(self) -> bool:
        """tracker 命中可疑规则 / 绝对阈值 / 频率阈值（每分钟 error 行数）。"""
        if self.tracker.is_suspicious():
            return True
        error_lines = self.tracker.unmatched_error_lines + self.tracker.total_warnings
        minutes = max(self.elapsed / 60.0, 0.1)
        if error_lines / minutes > ERROR_LINES_PER_MINUTE_THRESHOLD:
            return True
        return False

    def post_validate(self, tolerance_factor: float = 0.05) -> Tuple[bool, str]:
        """跑 ffprobe 校验输出文件。返回 (ok, reason)。

        tolerance_factor: 时长容差因子（0.05 = ±5%）；最低 1.0 秒。
        失败原因举例: "output missing" / "output empty" /
        "ffprobe failed" / "duration mismatch (expected X, got Y)"。
        本方法不抛异常，所有内部失败收敛为 (False, reason)。
        """
        try:
            p = Path(self.output_path)
            if not p.exists():
                return (False, "output missing")
            try:
                size = p.stat().st_size
            except OSError as exc:
                return (False, f"stat failed: {exc}")
            if size <= 0:
                return (False, "output empty")
            duration, broken = _run_ffprobe(str(p))
            if broken or duration is None:
                return (False, "ffprobe failed")
            if self.expected_duration is not None:
                tolerance = max(self.expected_duration * tolerance_factor, 1.0)
                if abs(duration - self.expected_duration) > tolerance:
                    return (
                        False,
                        f"duration mismatch (expected {self.expected_duration:.1f}, "
                        f"got {duration:.1f})",
                    )
            return (True, "")
        except Exception as exc:  # 任意意外都收敛
            return (False, f"post_validate exception: {exc}")
