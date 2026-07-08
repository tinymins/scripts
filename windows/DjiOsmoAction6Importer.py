import csv
import ctypes
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path


MEDIA_EXTENSIONS = {".MP4", ".LRF", ".MOV", ".JPG", ".JPEG", ".DNG", ".AAC"}
DJI_NAME_RE = re.compile(r"^DJI_(\d{8})\d{6}_.+\.[A-Z0-9]+$", re.IGNORECASE)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LOG_ROOT = REPO_ROOT / ".data" / "dji-imports"
CONFIG_PATH = REPO_ROOT / ".data" / "dji-importer-config.json"
CONFIG_EXAMPLE_PATH = SCRIPT_DIR / "DjiOsmoAction6Importer.config.example.json"


@dataclass
class DriveCandidate:
    root: str
    file_count: int
    total_bytes: int
    date_summary: str


@dataclass
class MediaItem:
    source: Path
    destination: Path
    date_folder: str
    size: int


@dataclass
class DryRunResult:
    source_root: str
    destination_root: str
    transfer_mode: str
    scan_root: Path
    matched: list
    would_copy: list
    existing_same_size: list
    conflicts: list
    ignored: list
    bytes_to_copy: int
    bytes_existing: int


CLEAR_LINE = "\r\x1b[2K"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
SECTION_LINE = "-" * 72
SUBSECTION_LINE = "." * 72


def clear_line():
    print(CLEAR_LINE, end="", flush=True)


def print_progress_line(message):
    print(CLEAR_LINE + message, end="", flush=True)


def progress(label, current, total, extra=""):
    total = max(total, 1)
    percent = min(100.0, current * 100.0 / total)
    msg = f"{label}: {current}/{total} ({percent:5.1f}%)"
    if extra:
        msg += f"  {extra}"
    print_progress_line(msg)


def finish_progress():
    clear_line()


def load_config():
    if not CONFIG_PATH.exists():
        return {"destinations": []}

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取配置失败：{CONFIG_PATH}")
        print(f"错误：{exc}")
        return {"destinations": []}

    destinations = []
    for item in config.get("destinations", []):
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            path = str(item.get("path", "")).strip()
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name = str(item[0]).strip()
            path = str(item[1]).strip()
        else:
            continue
        if name and path:
            destinations.append((name, path))
    return {"destinations": destinations}


def print_section(title):
    print("")
    print(SECTION_LINE)
    print(title)
    print(SECTION_LINE)


def print_subsection(title):
    print("")
    print(SUBSECTION_LINE)
    print(title)
    print(SUBSECTION_LINE)


def format_bytes(value):
    value = int(value or 0)
    for name, factor in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= factor:
            return f"{value / factor:.2f} {name}"
    return f"{value} B"


def format_speed(bytes_per_second):
    if bytes_per_second >= 1024 ** 3:
        return f"{bytes_per_second / (1024 ** 3):.2f} GB/s"
    if bytes_per_second >= 1024 ** 2:
        return f"{bytes_per_second / (1024 ** 2):.1f} MB/s"
    if bytes_per_second >= 1024:
        return f"{bytes_per_second / 1024:.1f} KB/s"
    return f"{bytes_per_second:.0f} B/s"


def transfer_progress(
    total_current_bytes,
    total_bytes,
    file_current_bytes,
    file_total_bytes,
    started_at,
    file_index,
    total_files,
    file_name,
):
    total_bytes = max(total_bytes, 1)
    file_total_bytes = max(file_total_bytes, 1)
    total_percent = min(100.0, total_current_bytes * 100.0 / total_bytes)
    file_percent = min(100.0, file_current_bytes * 100.0 / file_total_bytes)
    elapsed = max(time.monotonic() - started_at, 0.001)
    speed = total_current_bytes / elapsed
    print_progress_line(
        f"总 {format_bytes(total_current_bytes)}/{format_bytes(total_bytes)} {total_percent:.1f}% | "
        f"文件 {file_index}/{total_files} {format_bytes(file_current_bytes)}/{format_bytes(file_total_bytes)} {file_percent:.1f}% | "
        f"{format_speed(speed)} | {file_name}"
    )


def dji_date_folder(file_name):
    match = DJI_NAME_RE.match(file_name)
    return match.group(1) if match else None


def windows_drive_roots():
    if os.name != "nt":
        return []

    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    roots = []
    for letter_ord in range(ord("H"), ord("Z") + 1):
        index = letter_ord - ord("A")
        if bitmask & (1 << index):
            roots.append(f"{chr(letter_ord)}:\\")
    return roots


def enable_ansi_console():
    if os.name != "nt":
        return

    kernel32 = ctypes.windll.kernel32
    stdout = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(stdout, ctypes.byref(mode)):
        return
    kernel32.SetConsoleMode(stdout, mode.value | 0x0004)


def drive_type_name(root):
    if os.name != "nt":
        return "drive"

    value = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    return {
        2: "removable",
        3: "fixed",
        4: "network",
        5: "cdrom",
        6: "ramdisk",
    }.get(value, "unknown")


def unc_share_key(path_text):
    normalized = str(path_text).replace("/", "\\")
    if not normalized.startswith("\\\\"):
        return None
    parts = normalized.lstrip("\\").split("\\")
    if len(parts) < 2:
        return None
    return (parts[0].lower(), parts[1].lower())


def transfer_mode_for(source_root, destination_root):
    source_key = unc_share_key(source_root)
    destination_key = unc_share_key(destination_root)
    if source_key and source_key == destination_key:
        return "move"
    return "copy"


def scan_root_for_media(source_root):
    dcim = Path(source_root) / "DCIM"
    return dcim if dcim.is_dir() else Path(source_root)


def iter_media_files(scan_root):
    for current_root, _, files in os.walk(scan_root):
        for name in files:
            source = Path(current_root) / name
            if source.suffix.upper() in MEDIA_EXTENSIONS:
                yield source


def summarize_dates(dates):
    values = sorted(set(dates))
    if not values:
        return "-"
    if len(values) == 1:
        return values[0]
    return f"{values[0]}..{values[-1]} ({len(values)} days)"


def inspect_source(root):
    scan_root = scan_root_for_media(root)
    if not scan_root.exists():
        return None

    file_count = 0
    total_bytes = 0
    dates = []
    for source in iter_media_files(scan_root):
        date_folder = dji_date_folder(source.name)
        if not date_folder:
            continue
        try:
            stat = source.stat()
        except OSError:
            continue
        file_count += 1
        total_bytes += stat.st_size
        dates.append(date_folder)

    if file_count == 0:
        return None

    return DriveCandidate(
        root=root,
        file_count=file_count,
        total_bytes=total_bytes,
        date_summary=summarize_dates(dates),
    )


def inspect_drive(root, index, total):
    progress("扫描盘符", index, total, root)
    return inspect_source(root)


def find_tf_cards():
    roots = windows_drive_roots()
    candidates = []
    total = max(len(roots), 1)
    for index, root in enumerate(roots, start=1):
        candidate = inspect_drive(root, index, total)
        if candidate:
            candidates.append(candidate)
    finish_progress()
    return candidates


def read_custom_source():
    while True:
        value = input("\n请输入源目录路径：").strip().strip('"')
        if not value:
            print("源目录不能为空。")
            continue
        path = Path(value)
        if not path.exists() or not path.is_dir():
            print(f"源目录不存在或不可访问：{value}")
            continue
        candidate = inspect_source(value)
        if not candidate:
            print("这个目录下没有找到 DJI .MP4/.LRF 文件，请重新输入。")
            continue
        return candidate


def choose_source(candidates):
    print_section("1. 选择源目录")
    print("找到以下候选源目录：")
    for index, candidate in enumerate(candidates, start=1):
        print(
            f"  {index}. {candidate.root}  "
            f"{candidate.file_count} files, {format_bytes(candidate.total_bytes)}, "
            f"{candidate.date_summary}, {drive_type_name(candidate.root)}"
        )
    custom_index = len(candidates) + 1
    print(f"  {custom_index}. 手动输入源目录")

    while True:
        answer = input("\n请选择源目录序号：").strip()
        try:
            selected = int(answer)
        except ValueError:
            print("请输入数字序号。")
            continue
        if 1 <= selected <= len(candidates):
            return candidates[selected - 1]
        if selected == custom_index:
            return read_custom_source()
        print("序号不在范围内。")


def ensure_destination(path_text):
    path = Path(path_text)
    if path.exists() and path.is_dir():
        return str(path)
    if path.exists():
        print(f"目标路径存在但不是目录：{path_text}")
        return None
    if ask_yes_no(f"目标目录不存在，是否创建？\n{path_text}"):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return str(path)
        except OSError as exc:
            print(f"创建目标目录失败：{exc}")
            return None
    return None


def read_custom_destination():
    while True:
        value = input("\n请输入目标目录路径：").strip().strip('"')
        if not value:
            print("目标目录不能为空。")
            continue
        destination = ensure_destination(value)
        if destination:
            return destination


def choose_destination(destinations):
    print_section("2. 选择目标目录")
    print("请选择目标目录：")
    if not destinations:
        print(f"  未配置默认目标；可参考：{CONFIG_EXAMPLE_PATH}")
    for index, (name, path) in enumerate(destinations, start=1):
        status = "OK" if Path(path).is_dir() else "不可访问/不存在"
        print(f"  {index}. {name}  {path}  [{status}]")
    custom_index = len(destinations) + 1
    print(f"  {custom_index}. 手动输入目标目录")

    while True:
        answer = input("\n请选择目标目录序号：").strip()
        try:
            selected = int(answer)
        except ValueError:
            print("请输入数字序号。")
            continue
        if 1 <= selected <= len(destinations):
            _, path = destinations[selected - 1]
            destination = ensure_destination(path)
            if destination:
                return destination
            continue
        if selected == custom_index:
            return read_custom_destination()
        print("序号不在范围内。")


def build_dry_run(source_root, destination_root):
    scan_root = scan_root_for_media(source_root)
    transfer_mode = transfer_mode_for(source_root, destination_root)
    sources = list(iter_media_files(scan_root))
    total = len(sources)
    matched = []
    would_copy = []
    existing_same_size = []
    conflicts = []
    ignored = []
    bytes_to_copy = 0
    bytes_existing = 0

    for index, source in enumerate(sources, start=1):
        progress("预检查", index, total, source.name)
        date_folder = dji_date_folder(source.name)
        if not date_folder:
            ignored.append((source, "文件名不符合 DJI 时间戳格式"))
            continue

        try:
            size = source.stat().st_size
        except OSError as exc:
            ignored.append((source, f"无法读取源文件信息：{exc}"))
            continue

        destination = Path(destination_root) / date_folder / source.name
        item = MediaItem(source=source, destination=destination, date_folder=date_folder, size=size)
        matched.append(item)

        if destination.exists():
            try:
                dest_size = destination.stat().st_size
            except OSError as exc:
                conflicts.append((item, f"无法读取目标文件信息：{exc}"))
                continue

            if dest_size == size:
                existing_same_size.append(item)
                bytes_existing += size
            else:
                conflicts.append((item, f"大小不同：源={size}，目标={dest_size}"))
        else:
            would_copy.append(item)
            bytes_to_copy += size

    finish_progress()
    return DryRunResult(
        source_root=source_root,
        destination_root=destination_root,
        transfer_mode=transfer_mode,
        scan_root=scan_root,
        matched=matched,
        would_copy=would_copy,
        existing_same_size=existing_same_size,
        conflicts=conflicts,
        ignored=ignored,
        bytes_to_copy=bytes_to_copy,
        bytes_existing=bytes_existing,
    )


def make_log_path():
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return LOG_ROOT / f"dji-import-{time.strftime('%Y%m%d-%H%M%S')}.csv"


def add_row(rows, action, source="", destination="", bytes_value=0, message=""):
    rows.append({
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "Action": action,
        "Source": str(source),
        "Destination": str(destination),
        "Bytes": int(bytes_value or 0),
        "Message": message,
    })


def write_log(log_path, rows):
    fieldnames = ["Timestamp", "Action", "Source", "Destination", "Bytes", "Message"]
    with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def partial_destination_for(item):
    return item.destination.with_name(item.destination.name + ".copying")


def partial_summary(items):
    count = 0
    dirty_bytes = 0
    for item in items:
        temp_destination = partial_destination_for(item)
        if not temp_destination.exists():
            continue
        temp_size = temp_destination.stat().st_size
        count += 1
        dirty_bytes += temp_size
    return count, dirty_bytes


def log_dry_run(result, rows):
    missing_action = "DryRunWouldMove" if result.transfer_mode == "move" else "DryRunWouldCopy"
    missing_message = "同 SMB share 内移动" if result.transfer_mode == "move" else "目标不存在"
    for item in result.would_copy:
        add_row(rows, missing_action, item.source, item.destination, item.size, missing_message)
    for item in result.existing_same_size:
        add_row(rows, "DryRunExistingSameSize", item.source, item.destination, item.size, "同名同大小")
    for item, message in result.conflicts:
        add_row(rows, "DryRunConflict", item.source, item.destination, item.size, message)
    for source, message in result.ignored:
        add_row(rows, "DryRunIgnored", source, "", 0, message)


def print_dry_run_report(result, log_path):
    partial_count, dirty_bytes = partial_summary(result.would_copy)
    action_label = "移动" if result.transfer_mode == "move" else "复制"
    print_section("3. 预检查报告")
    print(f"  源目录:      {result.source_root}")
    print(f"  扫描目录:    {result.scan_root}")
    print(f"  目标目录:    {result.destination_root}")
    print(f"  传输方式:    {result.transfer_mode} ({'同 SMB share 内移动' if result.transfer_mode == 'move' else '复制到目标后再按需清理源文件'})")
    print(f"  匹配文件:    {len(result.matched)}")
    print(f"  将{action_label}:      {len(result.would_copy)} files, {format_bytes(result.bytes_to_copy)}")
    print(f"  已存在:      {len(result.existing_same_size)} files, {format_bytes(result.bytes_existing)}")
    print(f"  脏临时文件:  {partial_count} temp files, {format_bytes(dirty_bytes)}，导入时会删除后{'移动' if result.transfer_mode == 'move' else '重传'}")
    print(f"  冲突:        {len(result.conflicts)}")
    print(f"  忽略:        {len(result.ignored)}")
    print(f"  日志:        {log_path}")
    print("")


def ask_yes_no(prompt, default=False):
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = input(prompt + suffix).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("请输入 y 或 n。")


def copy_file_with_progress(item, copied_bytes_ref, total_bytes, started_at, rows, file_index, total_files):
    item.destination.parent.mkdir(parents=True, exist_ok=True)
    temp_destination = partial_destination_for(item)
    buffer_size = 8 * 1024 * 1024

    if temp_destination.exists():
        temp_size = temp_destination.stat().st_size
        temp_destination.unlink()
        add_row(rows, "DiscardPartial", item.source, temp_destination, temp_size, "发现旧临时文件，删除后完整重传")
        print(f"  发现旧临时文件 {format_bytes(temp_size)}，已删除并完整重传。")

    file_copied_bytes = 0
    with item.source.open("rb") as source_handle, temp_destination.open("wb") as dest_handle:
        while True:
            chunk = source_handle.read(buffer_size)
            if not chunk:
                break
            dest_handle.write(chunk)
            file_copied_bytes += len(chunk)
            copied_bytes_ref[0] += len(chunk)
            transfer_progress(
                copied_bytes_ref[0],
                total_bytes,
                file_copied_bytes,
                item.size,
                started_at,
                file_index,
                total_files,
                item.source.name,
            )

    shutil.copystat(item.source, temp_destination)
    if item.destination.exists():
        raise FileExistsError(f"目标文件已存在，拒绝覆盖：{item.destination}")
    os.replace(temp_destination, item.destination)


def move_file(item, rows):
    item.destination.parent.mkdir(parents=True, exist_ok=True)
    temp_destination = partial_destination_for(item)
    if temp_destination.exists():
        temp_size = temp_destination.stat().st_size
        temp_destination.unlink()
        add_row(rows, "DiscardPartial", item.source, temp_destination, temp_size, "move 模式下发现旧临时文件，删除后移动")
        print(f"  发现旧临时文件 {format_bytes(temp_size)}，已删除后移动。")

    if item.destination.exists():
        raise FileExistsError(f"目标文件已存在，拒绝覆盖：{item.destination}")

    item.source.rename(item.destination)
    dest_size = item.destination.stat().st_size
    if dest_size != item.size:
        raise OSError(f"移动后大小异常：源={item.size}，目标={dest_size}")


def import_files(result, rows):
    cleanable = []
    total_bytes = max(result.bytes_to_copy, 1)
    copied_bytes_ref = [0]
    copy_started_at = time.monotonic()
    move_mode = result.transfer_mode == "move"

    for item in result.existing_same_size:
        add_row(rows, "SkippedExisting", item.source, item.destination, item.size, "预检查时已同名同大小")
        cleanable.append(item.source)

    print(HIDE_CURSOR, end="", flush=True)
    try:
        total_files = len(result.would_copy)
        for index, item in enumerate(result.would_copy, start=1):
            clear_line()
            print(f"{'移动' if move_mode else '复制'}文件 {index}/{total_files}: {item.source.name} ({format_bytes(item.size)})")

            if item.destination.exists():
                dest_size = item.destination.stat().st_size
                if dest_size == item.size:
                    add_row(rows, "SkippedExisting", item.source, item.destination, item.size, "导入前已同名同大小")
                    cleanable.append(item.source)
                    print("  已存在，跳过。")
                    continue
                add_row(rows, "ImportConflict", item.source, item.destination, item.size, f"导入前目标出现且大小不同：{dest_size}")
                print("  目标已出现但大小不同，跳过。")
                continue

            try:
                if move_mode:
                    move_file(item, rows)
                    add_row(rows, "Moved", item.source, item.destination, item.size, "同 SMB share 内移动完成且大小校验通过")
                    print("  完成。")
                else:
                    copy_file_with_progress(item, copied_bytes_ref, total_bytes, copy_started_at, rows, index, total_files)
                    finish_progress()
                    dest_size = item.destination.stat().st_size
                    if dest_size == item.size:
                        add_row(rows, "Copied", item.source, item.destination, item.size, "复制完成且大小校验通过")
                        cleanable.append(item.source)
                        print("  完成。")
                    else:
                        add_row(rows, "CopyVerifyFailed", item.source, item.destination, item.size, f"目标大小={dest_size}")
                        print("  大小校验失败，源文件不会清理。")
            except Exception as exc:
                finish_progress()
                add_row(rows, "MoveError" if move_mode else "CopyError", item.source, item.destination, item.size, str(exc))
                print(f"  {'移动' if move_mode else '复制'}失败：{exc}")
    finally:
        print(SHOW_CURSOR, end="", flush=True)

    return cleanable


def delete_sources(cleanable, rows):
    deleted = 0
    errors = 0
    total = len(cleanable)
    for index, source in enumerate(cleanable, start=1):
        progress("清理源文件", index, total, source.name)
        try:
            if source.exists():
                size = source.stat().st_size
                source.unlink()
                deleted += 1
                add_row(rows, "DeletedSource", source, "", size, "已从源目录删除")
        except Exception as exc:
            errors += 1
            add_row(rows, "DeleteError", source, "", 0, str(exc))
    finish_progress()
    return deleted, errors


def pause_before_exit():
    if os.name == "nt":
        print_section("结束")
        input("按 Enter 退出...")


def main():
    enable_ansi_console()
    config = load_config()
    print_section("DJI 媒体导入器")

    print_subsection("扫描源目录")
    candidates = find_tf_cards()
    if not candidates:
        print("没有在 H: 到 Z: 自动找到包含 DJI 媒体文件的 TF 卡。")
        print("仍可选择手动输入源目录。")

    selected = choose_source(candidates)
    print(f"已选择源目录：{selected.root}")

    destination_root = choose_destination(config["destinations"])
    print(f"已选择目标目录：{destination_root}")

    log_path = make_log_path()
    rows = []

    print_subsection("开始预检查")
    result = build_dry_run(selected.root, destination_root)
    log_dry_run(result, rows)
    write_log(log_path, rows)
    print_dry_run_report(result, log_path)

    if result.conflicts:
        print("注意：存在冲突文件。冲突文件不会覆盖，也不会从 TF 卡清理。")
        print("")

    if not result.would_copy and not result.existing_same_size:
        print_section("完成")
        print("没有可导入或可确认已存在的文件。")
        pause_before_exit()
        return 0

    if not ask_yes_no("是否继续执行真实导入？", default=True):
        print_section("已取消")
        print("已取消导入。")
        pause_before_exit()
        return 0

    print_section("4. 开始导入")
    cleanable = import_files(result, rows)
    write_log(log_path, rows)

    cleanable = sorted(set(cleanable))
    cleanable_bytes = sum(path.stat().st_size for path in cleanable if path.exists())
    print_section("5. 导入完成")
    print(f"  可清理源文件: {len(cleanable)} files, {format_bytes(cleanable_bytes)}")
    print(f"  日志:         {log_path}")

    if cleanable and ask_yes_no("是否从源目录删除这些已确认导入/已存在的源文件？", default=True):
        print_section("6. 清理源文件")
        deleted, errors = delete_sources(cleanable, rows)
        write_log(log_path, rows)
        print(f"清理完成：已删除 {deleted} 个，错误 {errors} 个。")
        print(f"日志：{log_path}")
    else:
        print_section("6. 清理源文件")
        print("未清理源目录。")

    pause_before_exit()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("")
        print("用户中断。")
        pause_before_exit()
        raise SystemExit(130)
