"""FFmpeg 子进程执行 + 警告分类追踪。"""

from __future__ import annotations

import subprocess
import sys
import time

from .config import SUSPICIOUS_RULES, WARNING_LABELS, WARNING_PATTERNS


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

    def is_suspicious(self):
        for key, threshold in SUSPICIOUS_RULES.items():
            if self.counts.get(key, 0) >= threshold:
                return True
        return False

    def is_clean(self):
        return self.total_warnings == 0 and self.unmatched_error_lines == 0

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
