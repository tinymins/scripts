"""警告报告写入：每文件 .warn.log + 汇总文本。"""

from __future__ import annotations

import os

from .config import SUSPICIOUS_RULES


def _write_per_file_warning_log(output_path, tracker, cmd):
    log_path = output_path + ".warn.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"output: {output_path}\n")
        fh.write(f"cmd: {' '.join(cmd)}\n\n")
        fh.write(tracker.format_detail())
        fh.write("\n")
    return log_path


def _append_master_warning_report(target_folder_base, output_path, tracker):
    severity = tracker.severity()
    report_path = os.path.join(target_folder_base, "_transcode_warnings.txt")
    new_file = not os.path.exists(report_path)
    with open(report_path, "a", encoding="utf-8") as fh:
        if new_file:
            fh.write("# 转码警告汇总\n")
            fh.write("# 列表: [严重程度] 输出文件 -- 各类警告计数\n")
            fh.write("# 严重程度:\n")
            fh.write("#   SUSPICIOUS - 强烈建议人工二次确认（画面可能损坏 / 暂停）\n")
            fh.write("#   WARN       - 有少量警告，通常无碍\n")
            fh.write(f"# 严重判定规则: {SUSPICIOUS_RULES}\n\n")
        rel = os.path.relpath(output_path, target_folder_base)
        fh.write(f"[{severity:11s}] {rel} -- {tracker.format_oneline()}\n")
    return report_path
