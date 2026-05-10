"""警告报告写入：每文件 .warn.log + 汇总文本。

类别（category）由调用方（pipeline）依据 tracker.mode / tracker.was_fallback /
tracker.is_suspicious() 综合判定，本模块只做格式化输出。
"""

from __future__ import annotations

import os

from .config import SUSPICIOUS_RULES


CATEGORY_WIDTH = 11  # 与 [{:11s}] 对齐


def classify_tracker(tracker) -> str:
    """把单个 tracker 归到 SUSPICIOUS / DOWNGRADED / WARN / OK。

    FATAL 由 pipeline 单独统计（失败组不会进 collector），不在此判定。
    """
    if tracker.mode == "compress":
        if tracker.is_suspicious():
            return "SUSPICIOUS"
        if tracker.total_warnings > 0 or tracker.unmatched_error_lines > 0:
            return "WARN"
        return "OK"
    # concat_copy
    if tracker.is_suspicious():
        # mode='concat_copy' 下 is_suspicious() 已忽略时间戳噪声，命中=真正画面损坏
        return "SUSPICIOUS"
    if getattr(tracker, "was_fallback", False):
        return "DOWNGRADED"
    if tracker.total_warnings > 0 or tracker.unmatched_error_lines > 0:
        return "WARN"
    return "OK"


def _write_per_file_warning_log(output_path, tracker, cmd):
    log_path = output_path + ".warn.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"output: {output_path}\n")
        fh.write(f"cmd: {' '.join(cmd)}\n")
        fh.write(f"mode: {getattr(tracker, 'mode', 'compress')}"
                 f"{' (fallback)' if getattr(tracker, 'was_fallback', False) else ''}\n\n")
        fh.write(tracker.format_detail())
        fh.write("\n")
    return log_path


def write_master_warning_report(report_path, target_folder_base, items, totals):
    """追加写入主报告（每次运行 append 一段，带日期时间分隔）。

    items: list of (output_path, tracker, category)
    totals: dict {completed, downgraded, failed}
    """
    from datetime import datetime
    is_new = not os.path.exists(report_path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(report_path, "a", encoding="utf-8") as fh:
        if is_new:
            fh.write("# 转码警告汇总（每次运行追加，按时间从旧到新）\n")
            fh.write("# 类别:\n")
            fh.write("#   FATAL      - 运行失败（非零返回 / 后置校验未通过；详见 .failure.log）\n")
            fh.write("#   SUSPICIOUS - 压制阶段命中可疑规则，强烈建议人工二次确认\n")
            fh.write("#   DOWNGRADED - 压制失败已降级到 concat copy 直拷（产物为输入流复制）\n")
            fh.write("#   WARN       - 有少量警告，通常无碍\n")
            fh.write(f"# SUSPICIOUS 判定规则 (compress 模式): {SUSPICIOUS_RULES}\n")
        fh.write("\n")
        fh.write("=" * 80 + "\n")
        fh.write(f"## 运行时间: {timestamp}\n")
        fh.write(
            f"## 本次: {totals.get('completed', 0)} 组完成, "
            f"{totals.get('downgraded', 0)} 组降级 concat copy, "
            f"{totals.get('failed', 0)} 组失败\n"
        )
        fh.write("=" * 80 + "\n")
        for output_path, tracker, category in items:
            rel = os.path.relpath(output_path, target_folder_base)
            fh.write(f"[{category:<{CATEGORY_WIDTH}s}] {rel} -- {tracker.format_oneline()}\n")
    return report_path
