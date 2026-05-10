"""集中控制台输出 helper：染色 + 排版。

所有 car_replay 子模块的人类可读控制台输出都应走本模块，确保一致的视觉层次。
ffmpeg 进度行（ffmpeg_runner.py）的低层 ANSI 渲染原语也定义在这里、由它复用。

NO_COLOR 协议（https://no-color.org）+ stdout.isatty() 检查决定是否启用 ANSI。
Windows 在模块 import 时一次性触发 VT 序列处理（Win10 1607+）。
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional, Sequence, Tuple


# ============================================================
# Windows VT 启用（一次性）
# ============================================================

if sys.platform == "win32":
    try:
        import ctypes  # type: ignore
        _k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # GetStdHandle(-11) = STDOUT；mode 7 = ENABLE_PROCESSED_OUTPUT |
        # ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        _k32.SetConsoleMode(_k32.GetStdHandle(-11), 7)
    except Exception:
        pass


# ============================================================
# ANSI 颜色 / 基础渲染原语（被 ffmpeg_runner 复用）
# ============================================================

_C_GRAY = 90
_C_RED = 91
_C_GREEN = 92
_C_YELLOW = 93
_C_BLUE = 94
_C_MAGENTA = 95
_C_CYAN = 96
_C_BOLD = 1

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _color(text: str, code: int, bold: bool = False) -> str:
    """ANSI 染色；非 TTY / NO_COLOR 时返回原文。"""
    if not _colors_enabled():
        return text
    if bold:
        return f"\x1b[{_C_BOLD};{code}m{text}\x1b[0m"
    return f"\x1b[{code}m{text}\x1b[0m"


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


# ============================================================
# 高层 helper
# ============================================================

_HBAR = "━"


def section(title: str) -> None:
    """章节标题：空行 + ━━━ TITLE ━━━（蓝粗）。"""
    line = f"{_HBAR * 3} {title} {_HBAR * 3}"
    print()
    print(_color(line, _C_BLUE, bold=True))


def step(msg: str, n: Optional[int] = None, total: Optional[int] = None) -> None:
    """主步骤，前缀 →（绿粗）。可选 [n/total] 计数。"""
    prefix = _color("→", _C_GREEN, bold=True)
    if n is not None and total is not None:
        counter = _color(f"[{n}/{total}]", _C_GRAY)
        print(f"{prefix} {counter} {msg}")
    else:
        print(f"{prefix} {msg}")


def info(msg: str) -> None:
    """普通信息（默认色）。"""
    print(msg)


def detail(msg: str, indent: int = 2) -> None:
    """缩进次要信息，灰。"""
    pad = " " * indent
    print(pad + _color(msg, _C_GRAY))


def warn(msg: str, indent: int = 0) -> None:
    pad = " " * indent
    prefix = _color("⚠", _C_YELLOW, bold=True)
    print(f"{pad}{prefix} {_color(msg, _C_YELLOW)}")


def error(msg: str, indent: int = 0) -> None:
    pad = " " * indent
    prefix = _color("✖", _C_RED, bold=True)
    print(f"{pad}{prefix} {_color(msg, _C_RED, bold=True)}")


def success(msg: str, indent: int = 0) -> None:
    pad = " " * indent
    prefix = _color("✓", _C_GREEN, bold=True)
    print(f"{pad}{prefix} {_color(msg, _C_GREEN, bold=True)}")


def kv(label: str, value, indent: int = 2) -> None:
    """label: value（label 暗灰，value 默认色醒目）。"""
    pad = " " * indent
    label_s = _color(f"{label}:", _C_GRAY)
    print(f"{pad}{label_s} {value}")


def copy_line(path: str, idx: int = 0, total: int = 0, action: str = "copy",
              indent: int = 2) -> None:
    """文件复制 / 跳过的单行染色输出。

    action='copy' → 绿色 + 箭头；action='skip' → 灰色 + ·
    带计数前缀 [i/total]（gray）。
    """
    pad = " " * indent
    counter = ""
    if total:
        counter = _color(f"[{idx}/{total}] ", _C_GRAY)
    if action == "skip":
        icon = _color("·", _C_GRAY)
        body = _color(path, _C_GRAY) + _color(" (已存在)", _C_GRAY)
    else:
        icon = _color("→", _C_GREEN, bold=True)
        body = _color(path, _C_CYAN)
    print(f"{pad}{counter}{icon} {body}")


def kvtable(rows: Sequence[Tuple[str, object]], indent: int = 2) -> None:
    """多行对齐 kv 表格。"""
    if not rows:
        return
    width = max(len(str(label)) for label, _ in rows)
    pad = " " * indent
    for label, value in rows:
        label_s = _color(f"{str(label):<{width}}", _C_GRAY)
        print(f"{pad}{label_s}  {value}")


def list_items(label: str, items: Sequence[str], max_show: int = 3,
               indent: int = 2, verbose: bool = False) -> None:
    """折叠列表：verbose 或 ≤max_show 时全显；否则前 max_show 个 + … +N more。"""
    pad = " " * indent
    label_s = _color(f"{label}:", _C_GRAY)
    if verbose or len(items) <= max_show:
        print(f"{pad}{label_s} [{', '.join(items)}]")
        return
    head = ", ".join(items[:max_show])
    rest = len(items) - max_show
    extra = _color(f"… +{rest} more", _C_GRAY)
    print(f"{pad}{label_s} [{head}, {extra}]")


def _shorten_path(p: str, base_dir: Optional[str] = None) -> str:
    if not p:
        return p
    if base_dir:
        try:
            rel = os.path.relpath(p, base_dir)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass
    return os.path.basename(p) or p


def cmd_line(executable: str, args: Sequence[str], verbose: bool = False,
             base_dir: Optional[str] = None, indent: int = 2) -> None:
    """渲染 ffmpeg 命令行。

    verbose=True：完整命令一行打出；否则折叠为
    ``<exe-basename> … -i <input> → <output>``，路径相对 base_dir 或退化到 basename。
    """
    pad = " " * indent
    if verbose:
        full = " ".join([executable, *args])
        print(_color(f"{pad}CMD: {full}", _C_GRAY))
        return

    exe_short = os.path.basename(executable) or executable
    inputs: List[str] = []
    output: Optional[str] = None
    if args:
        # ffmpeg 调用约定：最后一个 token 是输出
        output = args[-1]
        for i, a in enumerate(args):
            if a == "-i" and i + 1 < len(args):
                inputs.append(args[i + 1])

    in_disp = (", ".join(_shorten_path(x, base_dir) for x in inputs)
               if inputs else "(stdin)")
    out_disp = _shorten_path(output, base_dir) if output else "(stdout)"

    arrow = _color("→", _C_GRAY)
    exe_s = _color(exe_short, _C_GRAY)
    in_s = _color(in_disp, _C_CYAN)
    out_s = _color(out_disp, _C_CYAN, bold=True)
    print(f"{pad}{exe_s} … -i {in_s} {arrow} {out_s}")
