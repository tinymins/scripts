"""FFmpeg 子进程执行 + 警告分类追踪 + 命令结果校验。"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .config import (
    FFPROBE,
    NEGATIVE_COMPRESSION_ABORT_RC,
    NEGATIVE_COMPRESSION_THRESHOLDS,
    SUSPICIOUS_RULES,
    WARNING_LABELS,
    WARNING_PATTERNS,
    format_eta,
    format_size,
)
from .console import (
    _ANSI_RE,
    _C_BOLD,
    _C_CYAN,
    _C_GRAY,
    _C_GREEN,
    _C_RED,
    _C_YELLOW,
    _color,
    _colors_enabled,
    _visible_len,
)


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
    """逐行扫描 ffmpeg 输出，归类并计数警告 / 错误。

    mode:
      - 'compress': NVENC 压制阶段，时间戳类计数也算可疑
      - 'concat_copy': 多源 stream-copy，时间戳不连续是物理常态，仅画面损坏类才算可疑
    was_fallback:
      仅在 mode='concat_copy' 时有意义；True 表示该 tracker 来自压制失败后降级的 concat copy。
    """

    # 真正画面损坏类（与 concat copy 时间戳噪声无关）
    FATAL_CATEGORIES = frozenset({
        "corrupt_frame", "concealing", "missing_ref", "missing_picture",
        "non_existing_pps", "application_invalid", "slice_header",
        "mb_decode", "co_located_poc", "bytestream", "decode_error",
    })

    def __init__(self, mode: str = "compress"):
        self.counts = {key: 0 for key, _ in WARNING_PATTERNS}
        self.first_examples = {}
        self.unmatched_error_lines = 0
        self.mode = mode
        self.was_fallback = False
        # 由监控状态机在主动中断时写入；非空字符串表示该 tracker 来自被中断的运行
        self.abort_reason: Optional[str] = None

    def feed(self, line):
        """喂入一行 ffmpeg stderr，更新计数。

        Returns (matched, was_unmatched_error)：
          matched=True 表示命中 14 类已知警告之一（调用方应静默该行）
          was_unmatched_error=True 表示未命中已知模式但形似错误日志
            （包含 'error' + '@' 且不是进度行；调用方应原样可见地打印）
          二者都为 False 则该行是噪声（banner/stream 元信息等），调用方静默即可。
        """
        stripped = line.rstrip("\r\n")
        if not stripped:
            return (False, False)
        for key, pattern in WARNING_PATTERNS:
            if pattern.search(stripped):
                self.counts[key] += 1
                if key not in self.first_examples:
                    self.first_examples[key] = stripped[:240]
                return (True, False)
        if "error" in stripped.lower() and "@" in stripped and "frame=" not in stripped:
            self.unmatched_error_lines += 1
            return (False, True)
        return (False, False)

    @property
    def total_warnings(self):
        return sum(self.counts.values())

    UNMATCHED_ERROR_SUSPICIOUS_THRESHOLD = 50

    def is_suspicious(self):
        """命中可疑规则或 unmatched_error_lines 超过绝对阈值。

        mode='concat_copy' 时，时间戳类（invalid_dts/nonmono_dts/guess_pts）等非画面损坏类
        计数不计入可疑判定，因为多源直拷天然有时间戳不连续。
        """
        if self.unmatched_error_lines > self.UNMATCHED_ERROR_SUSPICIOUS_THRESHOLD:
            return True
        for key, threshold in SUSPICIOUS_RULES.items():
            if self.mode == "concat_copy" and key not in self.FATAL_CATEGORIES:
                continue
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


_PROGRESS_FIELD_RES = {
    "frame": re.compile(r"frame=\s*(\d+)"),
    "fps": re.compile(r"fps=\s*([\d.]+)"),
    "time": re.compile(r"time=\s*([\d:.NA/-]+)"),
    "bitrate": re.compile(r"bitrate=\s*([\d.]+\s*\S*|N/A)"),
    "speed": re.compile(r"speed=\s*([\d.]+x|N/A)"),
    "size": re.compile(r"\bsize=\s*(\d+(?:\.\d+)?\s*[kKmMgG]?i?B|N/A)"),
}


_SIZE_UNIT_FACTORS = {
    "B": 1,
    "KB": 1000, "KIB": 1024,
    "MB": 1000 * 1000, "MIB": 1024 * 1024,
    "GB": 1000 * 1000 * 1000, "GIB": 1024 * 1024 * 1024,
}


def _parse_ffmpeg_size_to_bytes(s: Optional[str]) -> Optional[int]:
    """ffmpeg size= 字段（如 ``1234kB`` / ``1.5MiB`` / ``N/A``）→ 字节数；解析失败 → None。"""
    if not s or s == "N/A":
        return None
    s = s.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([kKmMgG]?i?B)?$", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "B").upper()
    factor = _SIZE_UNIT_FACTORS.get(unit)
    if factor is None:
        return None
    return int(num * factor)


# ============================================================
# 进度条 / spinner 渲染常量
# ============================================================

_BAR_LEN = 10
_BAR_FILLED = "█"
_BAR_EMPTY = "░"
_SPINNER_FRAMES = "|/-\\"


def _shutil_get_terminal_size():
    return shutil.get_terminal_size(fallback=(120, 24))


def _truncate_visible(s: str, max_visible: int) -> str:
    """按可见长度截断，保留 ANSI 转义。超出部分 + 重置序列。"""
    out = []
    visible = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\x1b":
            m = _ANSI_RE.match(s, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        if visible >= max_visible:
            break
        out.append(ch)
        visible += 1
        i += 1
    out.append("\x1b[0m")
    return "".join(out)


def _parse_ffmpeg_time_to_seconds(t: str):
    """ffmpeg time= 字段（HH:MM:SS.ms 或 N/A 或负数）→ float 秒；解析失败 → None。"""
    if not t or t == "N/A":
        return None
    s = t.strip()
    if s.startswith("-"):
        return None
    try:
        if ":" in s:
            parts = s.split(":")
            if len(parts) != 3:
                return None
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        return float(s)
    except (ValueError, TypeError):
        return None


def _percent_color(pct: float) -> int:
    # 进度条统一用青色：百分比只是处理进度，不是健康度，按比例红/黄/绿会让 1% 看起来像出错
    return _C_CYAN


def _render_progress_bar(pct: float):
    """返回 (染色后字符串, 染色后百分比字符串)。pct 已 clamp 0-100。"""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * _BAR_LEN))
    filled = max(0, min(_BAR_LEN, filled))
    color = _percent_color(pct)
    bar = _color(_BAR_FILLED * filled, color) + _color(_BAR_EMPTY * (_BAR_LEN - filled), _C_GRAY)
    pct_str = _color(f"{int(pct):3d}%", color)
    return bar, pct_str


def _is_progress_line(line: str) -> bool:
    """ffmpeg 自身的进度刷新行：典型形如
    ``frame=  123 fps= 30 q=28.0 size=...kB time=00:00:41 bitrate=...kbits/s speed=1.2x``。
    要求同时包含 time= 与 speed= 以避开误伤其它含 frame= 的诊断行。
    """
    return ("time=" in line and "speed=" in line
            and ("frame=" in line or "size=" in line))


def _format_elapsed(seconds: float) -> str:
    mm, ss = divmod(int(seconds), 60)
    return f"{mm:02d}:{ss:02d}"


def _evaluate_negative_compression_state(
    state: dict,
    *,
    cur_secs: float,
    cur_out_bytes: int,
    expected_duration: float,
    expected_input_bytes: int,
    wall_elapsed_s: float,
    now_s: float,
    thresholds: dict,
) -> Optional[str]:
    """Pure state-machine step for the negative-compression monitor.

    Mutates ``state`` (the same dict used by ``_run_ffmpeg_capturing_warnings``).
    Returns the abort reason string if this step triggers an abort, otherwise
    ``None``. No I/O — caller is responsible for terminating the ffmpeg
    process and printing the message.
    """
    if state.get("monitor_aborted"):
        return None
    if cur_secs <= 0:
        return None
    if wall_elapsed_s < thresholds["warmup_secs"]:
        return None
    if cur_secs / float(expected_duration) < thresholds["warmup_progress_pct"]:
        return None
    predicted_out = cur_out_bytes / cur_secs * float(expected_duration)
    ratio = predicted_out / float(expected_input_bytes)
    state["monitor_last_ratio"] = ratio

    if state.get("monitor_phase") == "OK":
        if ratio > thresholds["enter_bad_ratio"]:
            state["monitor_phase"] = "WARN"
            state["monitor_warn_started_at"] = now_s
            state["monitor_good_streak_started_at"] = None
        return None

    # WARN
    if ratio < thresholds["exit_good_ratio"]:
        if state.get("monitor_good_streak_started_at") is None:
            state["monitor_good_streak_started_at"] = now_s
        elif (now_s - state["monitor_good_streak_started_at"]) >= thresholds["exit_ok_secs"]:
            state["monitor_phase"] = "OK"
            state["monitor_warn_started_at"] = None
            state["monitor_good_streak_started_at"] = None
        return None
    state["monitor_good_streak_started_at"] = None

    warn_started = state.get("monitor_warn_started_at") or now_s
    if (now_s - warn_started) >= thresholds["abort_hold_secs"] and ratio > thresholds["abort_ratio"]:
        state["monitor_aborted"] = True
        reason = (
            f"negative compression: predicted ratio {ratio:.2f}x "
            f"sustained for {int(now_s - warn_started)}s "
            f"(out={format_size(cur_out_bytes)} at time={cur_secs:.1f}s, "
            f"input={format_size(expected_input_bytes)})"
        )
        state["monitor_abort_reason"] = reason
        return reason
    return None


def _run_ffmpeg_capturing_warnings(cmd, mode: str = "compress", verbose: bool = False,
                                   expected_duration=None,
                                   expected_input_bytes: Optional[int] = None,
                                   abort_on_negative_ratio: bool = False):
    """运行 ffmpeg，实时归类 stderr 警告并以单行覆盖式打印进度。

    verbose=True 时退化为老行为（每行原样透传），用于排障。
    expected_duration: 预计输出时长（秒），用于渲染进度条；None / 0 → 用 spinner。
    abort_on_negative_ratio: True 时启用迟滞状态机监控压缩率，反向膨胀持续超阈值即中断。
    返回 (returncode, elapsed_seconds, tracker)。
    mode 透传给 WarningTracker。
    被监控状态机主动中断时，returncode = NEGATIVE_COMPRESSION_ABORT_RC，
    tracker.abort_reason 含人类可读原因。
    """
    tracker = WarningTracker(mode=mode)
    start = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert proc.stderr is not None
    fd = proc.stderr.fileno()

    is_tty = sys.stdout.isatty()
    has_duration = bool(expected_duration and expected_duration > 0)
    monitor_enabled = bool(
        abort_on_negative_ratio
        and has_duration
        and expected_input_bytes
    )
    state = {
        "last_flush": 0.0,
        "last_line_len": 0,
        "feed_count_since_flush": 0,
        "any_progress": False,
        "spinner_idx": 0,
        # 负压缩监控状态机
        "monitor_phase": "OK",            # "OK" | "WARN"
        "monitor_warn_started_at": None,  # float wall-clock
        "monitor_good_streak_started_at": None,
        "monitor_last_ratio": None,
        "monitor_aborted": False,
        "monitor_abort_reason": None,
    }
    progress = {}  # type: dict

    def render_status(force: bool = False) -> None:
        elapsed_s = time.time() - start

        # ETA: 优先从 speed= 字段计算，回退到 elapsed 比例法。
        # 抽到外层是为了与 elapsed 合并到首段 [elapsed/ETA] 显示。
        eta_s: Optional[float] = None
        cur_secs = _parse_ffmpeg_time_to_seconds(progress.get("time", "")) if state["any_progress"] else None
        pct = 0.0
        if state["any_progress"] and has_duration and cur_secs is not None:
            pct = max(0.0, min(100.0, cur_secs / float(expected_duration) * 100.0))
            speed_val = None
            speed_raw = progress.get("speed", "")
            if speed_raw and speed_raw != "N/A":
                try:
                    speed_val = float(speed_raw.rstrip("x"))
                except ValueError:
                    pass
            if speed_val and speed_val > 0 and cur_secs < float(expected_duration):
                eta_s = (float(expected_duration) - cur_secs) / speed_val
            elif pct > 0 and elapsed_s > 0:
                eta_s = (elapsed_s / pct) * (100.0 - pct)

        head = f"[{_format_elapsed(elapsed_s)}/{format_eta(eta_s)}]" if eta_s is not None \
            else f"[{_format_elapsed(elapsed_s)}]"
        parts = [_color(head, _C_GRAY)]

        # 进度条 / spinner
        if state["any_progress"]:
            if has_duration and cur_secs is not None:
                bar, pct_str = _render_progress_bar(pct)
                parts.append(bar)
                parts.append(pct_str)

                # 字节量：按进度百分比折算 input 消耗量
                if expected_input_bytes:
                    processed_bytes = int(expected_input_bytes * pct / 100.0)
                    parts.append(_color(
                        f"{format_size(processed_bytes).replace(' ', '')}"
                        f"/{format_size(expected_input_bytes).replace(' ', '')}",
                        _C_CYAN,
                    ))
            else:
                spin = _SPINNER_FRAMES[state["spinner_idx"] % len(_SPINNER_FRAMES)]
                state["spinner_idx"] += 1
                parts.append(_color(spin, _C_CYAN))

            # 进度字段（紧凑：数字+单位/后缀，不带 key= 前缀）
            frame = progress.get("frame")
            fps = progress.get("fps")
            if frame is not None:
                parts.append(_color(f"{frame}frames", _C_CYAN))
            if fps is not None:
                parts.append(_color(f"{fps}fps", _C_CYAN))
            time_v = progress.get("time")
            if time_v is not None:
                secs = _parse_ffmpeg_time_to_seconds(time_v)
                time_disp = f"{secs:.2f}s" if secs is not None else f"{time_v}"
                parts.append(_color(time_disp, _C_GREEN))
            bitrate = progress.get("bitrate")
            if bitrate is not None:
                parts.append(_color(f"{bitrate}", _C_GRAY))
            speed = progress.get("speed")
            if speed is not None:
                # speed 字段已含 x 后缀（如 "3.18x"）
                parts.append(_color(speed, _C_GREEN))

        nonzero = [(k, c) for k, c in tracker.counts.items() if c > 0]
        nonzero.sort(key=lambda x: -x[1])
        if nonzero or tracker.unmatched_error_lines:
            parts.append(_color("|", _C_GRAY))
            shown = nonzero[:5]
            for k, c in shown:
                if k in WarningTracker.FATAL_CATEGORIES:
                    parts.append(_color(f"{k}={c}", _C_RED, bold=True))
                else:
                    parts.append(_color(f"{k}={c}", _C_YELLOW))
            if len(nonzero) > 5:
                parts.append(_color("...", _C_GRAY))
            if tracker.unmatched_error_lines:
                parts.append(_color(
                    f"err_lines={tracker.unmatched_error_lines}", _C_RED, bold=True,
                ))
        line = " ".join(parts)
        # 截断到终端宽度 - 1，避免 wrap 后 \r 擦不干净（会把首行残留）
        try:
            term_w = max(40, _shutil_get_terminal_size().columns - 1)
        except Exception:
            term_w = 119
        visible = _visible_len(line)
        if visible > term_w:
            line = _truncate_visible(line, term_w)
            visible = _visible_len(line)
        # \x1b[2K 擦整行 + \r 回到行首；比手动填空格更可靠
        sys.stdout.write("\r\x1b[2K" + line)
        sys.stdout.flush()
        state["last_line_len"] = visible
        state["last_flush"] = time.time()
        state["feed_count_since_flush"] = 0

    def clear_status_line() -> None:
        if state["last_line_len"]:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            state["last_line_len"] = 0

    def maybe_flush() -> None:
        # 唯一节流：1s 时钟。去掉行计数触发避免高密度警告刷屏。
        now = time.time()
        if now - state["last_flush"] >= 1.0:
            render_status()

    def evaluate_monitor() -> None:
        """每条进度行后调用：维护负压缩迟滞状态机，必要时 terminate ffmpeg。"""
        if not monitor_enabled or state["monitor_aborted"]:
            return
        cur_secs = _parse_ffmpeg_time_to_seconds(progress.get("time", ""))
        cur_out_bytes = _parse_ffmpeg_size_to_bytes(progress.get("size", ""))
        if cur_secs is None or cur_out_bytes is None or cur_secs <= 0:
            return
        reason = _evaluate_negative_compression_state(
            state,
            cur_secs=cur_secs,
            cur_out_bytes=cur_out_bytes,
            expected_duration=float(expected_duration),
            expected_input_bytes=int(expected_input_bytes),
            wall_elapsed_s=time.time() - start,
            now_s=time.time(),
            thresholds=NEGATIVE_COMPRESSION_THRESHOLDS,
        )
        if reason:
            clear_status_line()
            print(f"[abort] {reason}")
            try:
                proc.terminate()
            except OSError:
                pass

    def handle_line(raw: str) -> None:
        line = raw.rstrip("\r\n")
        if not line:
            return
        if verbose:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
            if not _is_progress_line(line):
                tracker.feed(line)
            else:
                # verbose 模式也要解析进度字段以驱动 monitor
                for k, regex in _PROGRESS_FIELD_RES.items():
                    m = regex.search(line)
                    if m:
                        progress[k] = m.group(1).strip()
                evaluate_monitor()
            return
        if _is_progress_line(line):
            for k, regex in _PROGRESS_FIELD_RES.items():
                m = regex.search(line)
                if m:
                    progress[k] = m.group(1).strip()
            state["any_progress"] = True
            state["feed_count_since_flush"] += 1
            evaluate_monitor()
            maybe_flush()
            return
        matched, was_err = tracker.feed(line)
        state["feed_count_since_flush"] += 1
        if was_err and not matched:
            clear_status_line()
            print(line)
            # ERROR 行打断进度条后立即重绘，避免用户看到"卡住"的孤立错误行
            render_status()
            return
        # matched 或纯噪声：静默
        maybe_flush()

    # 字符级读取 + 按 \r 或 \n 切片，确保 ffmpeg 用 \r 自刷的进度行能独立成"行"
    buf = bytearray()
    try:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                i_n = buf.find(b"\n")
                i_r = buf.find(b"\r")
                if i_n == -1 and i_r == -1:
                    break
                if i_n == -1:
                    i = i_r
                elif i_r == -1:
                    i = i_n
                else:
                    i = min(i_n, i_r)
                line_bytes = bytes(buf[:i])
                del buf[:i + 1]
                handle_line(line_bytes.decode("utf-8", "replace"))
    finally:
        # 若被监控状态机中断：terminate 已发，给点时间退出，超时再 kill
        if state["monitor_aborted"]:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        else:
            proc.wait()
        if buf:
            handle_line(bytes(buf).decode("utf-8", "replace"))
        # 中断 → 写入 tracker.abort_reason 并把返回码改为哨兵
        if state["monitor_aborted"]:
            tracker.abort_reason = state["monitor_abort_reason"]
            effective_rc = NEGATIVE_COMPRESSION_ABORT_RC
        else:
            effective_rc = proc.returncode
        if not verbose:
            clear_status_line()
            elapsed_s = time.time() - start
            summary_summary = tracker.format_oneline()
            print(
                f"[{_format_elapsed(elapsed_s)}] ffmpeg done "
                f"rc={effective_rc} | {summary_summary}"
            )
    elapsed = time.time() - start
    return effective_rc, elapsed, tracker


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


# ============================================================
# 内嵌自检（python -m car_replay.ffmpeg_runner）
# ============================================================


def _selftest():
    """对染色 / 进度条 / 字段渲染做轻量验证。

    通过 monkey-patch sys.stdout.isatty + 关闭 NO_COLOR 强制启用染色，
    然后调用 _color/_render_progress_bar 直接断言。
    """
    import io

    # ---- 1) _color: TTY=True 时染色，NO_COLOR 时关 ----
    saved_no_color = os.environ.pop("NO_COLOR", None)

    class _FakeTTY(io.StringIO):
        def isatty(self):  # noqa: D401
            return True

    saved_stdout = sys.stdout
    sys.stdout = _FakeTTY()
    try:
        s = _color("hi", _C_RED)
        assert s == "\x1b[91mhi\x1b[0m", s
        bold = _color("x", _C_RED, bold=True)
        assert bold == "\x1b[1;91mx\x1b[0m", bold

        # ---- 2) 进度条比例 ----
        bar0, pct0 = _render_progress_bar(0)
        bar50, pct50 = _render_progress_bar(50)
        bar100, pct100 = _render_progress_bar(100)
        assert _ANSI_RE.sub("", bar0) == _BAR_EMPTY * _BAR_LEN
        assert _ANSI_RE.sub("", bar100) == _BAR_FILLED * _BAR_LEN
        # 50% → half/half split
        plain50 = _ANSI_RE.sub("", bar50)
        assert plain50.count(_BAR_FILLED) == _BAR_LEN // 2 and plain50.count(_BAR_EMPTY) == _BAR_LEN - _BAR_LEN // 2, plain50
        # 颜色档位：进度条统一青色（不再按百分比切档）
        assert "\x1b[96m" in bar0
        assert "\x1b[96m" in bar50
        assert "\x1b[96m" in bar100

        # ---- 3) _parse_ffmpeg_time_to_seconds ----
        assert abs(_parse_ffmpeg_time_to_seconds("00:00:50.97") - 50.97) < 1e-6
        assert _parse_ffmpeg_time_to_seconds("N/A") is None
        assert _parse_ffmpeg_time_to_seconds("-00:00:01") is None

        # ---- 4) _visible_len 不计 ANSI ----
        s = _color("frame=975", _C_CYAN)
        assert _visible_len(s) == len("frame=975"), _visible_len(s)

        # ---- 5) NO_COLOR 短路 ----
        os.environ["NO_COLOR"] = "1"
        assert _color("hi", _C_RED) == "hi"
        os.environ.pop("NO_COLOR")
    finally:
        sys.stdout = saved_stdout
        if saved_no_color is not None:
            os.environ["NO_COLOR"] = saved_no_color
    print("ffmpeg_runner selftest OK")


if __name__ == "__main__":
    _selftest()
