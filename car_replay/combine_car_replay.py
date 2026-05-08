import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta


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

# ffmpeg / ffprobe 路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "..", ".vendor", "ffmpeg", "ffmpeg.exe")
FFPROBE = os.path.join(SCRIPT_DIR, "..", ".vendor", "ffmpeg", "ffprobe.exe")


def _pause_before_exit():
    if os.name == "nt":
        os.system("pause")
        return

    try:
        input("Press Enter to exit...")
    except EOFError:
        pass


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


class DurationResolver:
    WINDOWS_METADATA_BATCH_SIZE = 500
    DURATION_MATCH_TOLERANCE_SECONDS = 1.0

    def __init__(
        self,
        enabled=True,
        fallback_seconds=None,
        use_windows_metadata=True,
        adaptive_sampling=True,
    ):
        self.enabled = enabled
        self.fallback_seconds = fallback_seconds
        self.use_windows_metadata = use_windows_metadata
        self.adaptive_sampling = adaptive_sampling
        self.cache = {}
        self.cache_sources = {}
        self.windows_metadata_attempted = False
        self.stats = {
            "windows_metadata_hits": 0,
            "ffprobe_probes": 0,
            "adaptive_reused": 0,
            "unavailable": 0,
        }

    def duration_for_file(self, path):
        cache_key = os.path.abspath(os.path.normpath(path))
        if cache_key not in self.cache:
            self._cache_duration(path, self._resolve_duration(path), "ffprobe")
        return self.cache[cache_key]

    def prepare_series(self, video_series):
        if not self.enabled:
            return

        candidates = []
        for video in video_series:
            info = parse_video_filename(_basename(video))
            if info.datetime and not info.end_datetime:
                candidates.append(video)

        if not candidates:
            return

        before = self.stats.copy()
        print(f"Resolving durations for {len(candidates)} clips...")

        self._prefetch_windows_metadata(candidates)

        if self.adaptive_sampling:
            self._adaptive_fill(candidates, 0, len(candidates) - 1)

        delta = {
            key: self.stats[key] - before.get(key, 0)
            for key in self.stats
        }
        print(
            "Duration resolving summary: "
            f"Windows metadata hits={delta['windows_metadata_hits']}, "
            f"ffprobe probes={delta['ffprobe_probes']}, "
            f"adaptive reused={delta['adaptive_reused']}, "
            f"unavailable={delta['unavailable']}"
        )

    def _cache_key(self, path):
        return os.path.abspath(os.path.normpath(path))

    def _cache_duration(self, path, duration, source):
        cache_key = self._cache_key(path)
        self.cache[cache_key] = duration
        self.cache_sources[cache_key] = source

    def _has_cached_duration(self, path):
        return self._cache_key(path) in self.cache

    def _adaptive_fill(self, paths, start, end):
        if start > end:
            return

        if start == end:
            self.duration_for_file(paths[start])
            return

        first_duration = self.duration_for_file(paths[start])
        last_duration = self.duration_for_file(paths[end])

        if (
            first_duration is not None
            and last_duration is not None
            and abs(first_duration - last_duration) <= self.DURATION_MATCH_TOLERANCE_SECONDS
        ):
            duration = (first_duration + last_duration) / 2
            reused = 0
            for path in paths[start:end + 1]:
                if not self._has_cached_duration(path):
                    self._cache_duration(path, duration, "adaptive")
                    reused += 1
            self.stats["adaptive_reused"] += reused
            return

        if end - start == 1:
            return

        mid = (start + end) // 2
        self._adaptive_fill(paths, start, mid)
        self._adaptive_fill(paths, mid + 1, end)

    def _prefetch_windows_metadata(self, paths):
        if not self._can_use_windows_metadata():
            return

        uncached_paths = [path for path in paths if not self._has_cached_duration(path)]
        if not uncached_paths:
            return

        self.windows_metadata_attempted = True
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            print("Windows metadata duration lookup skipped: powershell.exe not found")
            return

        print(f"Trying Windows metadata duration lookup for {len(uncached_paths)} clips...")
        for start in range(0, len(uncached_paths), self.WINDOWS_METADATA_BATCH_SIZE):
            batch = uncached_paths[start:start + self.WINDOWS_METADATA_BATCH_SIZE]
            durations = self._read_windows_metadata_batch(powershell, batch)
            for path, duration in durations.items():
                self._cache_duration(path, duration, "windows_metadata")
                self.stats["windows_metadata_hits"] += 1
            print(
                f"  Windows metadata batch "
                f"{start // self.WINDOWS_METADATA_BATCH_SIZE + 1}/"
                f"{math.ceil(len(uncached_paths) / self.WINDOWS_METADATA_BATCH_SIZE)}: "
                f"{len(durations)}/{len(batch)} durations found"
            )

    def _can_use_windows_metadata(self):
        return self.enabled and self.use_windows_metadata and os.name == "nt"

    def _read_windows_metadata_batch(self, powershell, paths):
        script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
$shell = New-Object -ComObject Shell.Application
$folders = @{}
$result = @{}
foreach ($path in $paths) {
    $folderPath = [System.IO.Path]::GetDirectoryName($path)
    $fileName = [System.IO.Path]::GetFileName($path)
    if (-not $folders.ContainsKey($folderPath)) {
        $folders[$folderPath] = $shell.Namespace($folderPath)
    }
    $folder = $folders[$folderPath]
    if ($null -eq $folder) { continue }
    $item = $folder.ParseName($fileName)
    if ($null -eq $item) { continue }
    $duration = $item.ExtendedProperty('System.Media.Duration')
    if ($null -eq $duration) { continue }
    try {
        $seconds = [double]$duration / 10000000.0
        if ($seconds -gt 0) {
            $result[$path] = $seconds
        }
    } catch {
        continue
    }
}
$result | ConvertTo-Json -Compress
"""
        try:
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                input=json.dumps(paths),
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"  Windows metadata duration lookup failed: {exc}")
            return {}

        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            print(f"  Windows metadata duration lookup failed: {detail}")
            return {}

        output = result.stdout.strip()
        if not output:
            return {}

        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            print(f"  Windows metadata duration lookup returned invalid JSON: {exc}")
            return {}

        if not isinstance(parsed, dict):
            return {}

        durations = {}
        for path, duration in parsed.items():
            try:
                duration_float = float(duration)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration_float) and duration_float > 0:
                durations[path] = duration_float
        return durations

    def _resolve_duration(self, path):
        basename = _basename(path)
        if not self.enabled:
            return self._fallback(
                basename,
                "ffprobe duration probing disabled",
                warn_without_fallback=True,
            )

        if not os.path.exists(FFPROBE):
            return self._fallback(basename, f"ffprobe not found: {FFPROBE}")

        command = [
            FFPROBE,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        try:
            self.stats["ffprobe_probes"] += 1
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return self._fallback(basename, f"ffprobe failed: {exc}")

        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            return self._fallback(basename, f"ffprobe failed: {detail}")

        try:
            duration = float(result.stdout.strip())
        except ValueError:
            return self._fallback(basename, f"invalid ffprobe duration: {result.stdout.strip()!r}")

        if math.isfinite(duration) and duration > 0:
            return duration
        return self._fallback(basename, f"invalid ffprobe duration: {duration!r}")

    def _fallback(self, basename, reason, warn_without_fallback=False):
        if self.fallback_seconds is not None:
            print(
                f"WARNING: {reason} for {basename}; "
                f"using fallback duration {self.fallback_seconds}s"
            )
            return self.fallback_seconds
        if warn_without_fallback:
            print(f"WARNING: {reason} for {basename}; duration unavailable")
        else:
            print(f"ERROR: {reason} for {basename}; duration unavailable")
        self.stats["unavailable"] += 1
        return None


def compress_video(input_path, output_path, camera_id, cq_override=None):
    """
    使用 hevc_nvenc 压缩视频文件。
    input_path: 输入文件（合并后的临时文件或单个源文件）
    output_path: 最终输出路径
    camera_id: 通道ID，用于选择压缩参数
    返回 True/False
    """
    profile = get_compress_profile(camera_id, cq_override)
    input_size = os.path.getsize(input_path)

    print(f"  Compressing [{camera_id or '??'}] CQ{profile['cq']}...")

    temp_output = output_path + ".compress_tmp.mp4"

    cmd = [
        FFMPEG,
        "-y",
        "-hwaccel", "cuda",
        "-fflags", "+genpts+igndts",
        "-err_detect", "ignore_err",
        "-i", input_path,
        "-c:v", "hevc_nvenc",
        "-preset", profile["preset"],
        "-rc", "vbr",
        "-cq", str(profile["cq"]),
        "-b:v", profile["bitrate"],
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-multipass", "fullres",
        "-rc-lookahead", "32",
        "-spatial-aq", "1",
        "-temporal-aq", "1",
        "-bf", "4",
        "-b_ref_mode", "middle",
        "-c:a", "copy",
        "-movflags", "+faststart",
        temp_output,
    ]

    print(f"  CMD: {' '.join(cmd)}")

    returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(cmd)

    if returncode != 0:
        if os.path.exists(temp_output):
            os.remove(temp_output)
        print(f"  ERROR: Compression failed!")
        return False, 0, 0, elapsed, tracker

    # 重命名为最终文件
    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(temp_output, output_path)

    output_size = os.path.getsize(output_path)
    ratio = input_size / output_size if output_size > 0 else 0
    saving = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    print(
        f"  Compressed: {format_size(input_size)} -> {format_size(output_size)} "
        f"({ratio:.1f}x ratio, -{saving:.0f}%) in {elapsed:.1f}s"
    )
    return True, input_size, output_size, elapsed, tracker


class VideoInfo:
    def __init__(
        self,
        datetime_obj=None,
        end_datetime=None,
        rest_of_filename=None,
        max_time_difference=None,
        camera_key=None,
    ):
        self.datetime = datetime_obj
        self.end_datetime = end_datetime
        self.rest_of_filename = rest_of_filename
        self.max_time_difference = max_time_difference
        self.camera_key = camera_key

def parse_video_filename(filename):
    # 合并后格式：20250419195801_20250419200101_000785AC.MP4
    # 必须先于旧格式解析，否则第二个时间戳会被当成 rest_of_filename。
    match = re.match(r"(\d{14})_(\d{14})_(.+\.(?:MP4|TS))$", filename, re.IGNORECASE)
    if match:
        start_datetime_str = match.group(1)
        end_datetime_str = match.group(2)
        rest_of_filename = _mp4_name_for_transport_stream(match.group(3))
        return VideoInfo(
            datetime_obj=datetime.strptime(start_datetime_str, "%Y%m%d%H%M%S"),
            end_datetime=datetime.strptime(end_datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename=rest_of_filename,
            max_time_difference=120,
        )

    # 原有格式：20250419195801_000785AC.MP4
    match = re.match(r"(\d{14})_(.+\.MP4)", filename, re.IGNORECASE)
    if match:
        datetime_str = match.group(1)
        rest_of_filename = match.group(2)
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename=rest_of_filename,
            max_time_difference=120
        )

    # 新格式：NO20200101-001521-002110B.mp4
    match = re.match(r"[A-Za-z]+(\d{8})-(\d{6})-(\d+[A-Za-z]+\.(?:MP4|TS))", filename, re.IGNORECASE)
    if match:
        date_str = match.group(1)
        time_str = match.group(2)
        rest_of_filename = _mp4_name_for_transport_stream(match.group(3))
        datetime_str = date_str + time_str
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename=rest_of_filename,
            max_time_difference=200
        )

    # LS_AR_IMX335: MOV2084_20260503165638.mp4 / LOK0051_20260318094443.mp4
    # Prefixes such as MOV/LOK/LOCK only indicate protection state and must not
    # split continuous clips.
    match = re.match(r"[A-Za-z]*\d+_(\d{14})\.(?:MP4|TS)$", filename, re.IGNORECASE)
    if match:
        datetime_str = match.group(1)
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename="AR_IMX335.mp4",
            max_time_difference=120,
            camera_key="AR_IMX335",
        )

    # LS_S3: 20260503_15h10m04s.ts / 20260429_11h20m35s-2.ts
    match = re.match(r"(\d{8})_(\d{2})h(\d{2})m(\d{2})s(?:-\d+)?\.(?:MP4|TS)$", filename, re.IGNORECASE)
    if match:
        datetime_str = "".join(match.groups())
        return VideoInfo(
            datetime_obj=datetime.strptime(datetime_str, "%Y%m%d%H%M%S"),
            rest_of_filename="LS_S3.mp4",
            max_time_difference=120,
            camera_key="LS_S3",
        )

    return VideoInfo()

def _basename(path):
    components = _path_components(path)
    return components[-1] if components else os.path.basename(path)

def _mp4_name_for_transport_stream(filename):
    root, ext = os.path.splitext(filename)
    if ext.lower() == ".ts":
        return root + ".mp4"
    return filename

def _is_video_file(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTS

def _effective_end(info, video_path=None, duration_resolver=None):
    if info.end_datetime:
        return info.end_datetime
    if not info.datetime:
        return None
    if video_path and duration_resolver:
        duration_seconds = duration_resolver.duration_for_file(video_path)
        if duration_seconds is not None:
            return info.datetime + timedelta(seconds=duration_seconds)
    else:
        duration_seconds = None
    if duration_seconds is not None:
        return info.datetime + timedelta(seconds=duration_seconds)
    return None

def _contains_combined_path(path):
    return "_combined" in path.lower()

def extract_camera_id(filename):
    # 合并后格式：从 "20250419195801_20250419200101_000785AC.MP4" 提取 "AC"
    match = re.match(r"\d{14}_\d{14}_\d+([A-Z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    # 原有格式：从 "20250419195801_000785AC.MP4" 提取 "AC"
    match = re.match(r"\d{14}_\d+([A-Z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    # 新格式：从 "NO20200101-001521-002110B.mp4" 提取 "B"
    match = re.match(r"[A-Za-z]+\d{8}-\d{6}-\d+([A-Za-z]+)\.MP4", filename, re.IGNORECASE)
    if match:
        return match.group(1)

    return None

def _path_components(path):
    return [part for part in re.split(r"[\\/]+", path) if part]

def _infer_device_key(path, src_folder=None):
    components = _path_components(path)
    for part in components:
        if re.match(r"LS_[A-Za-z0-9_]+$", part, re.IGNORECASE):
            return part.upper()

    if src_folder:
        try:
            relative = os.path.relpath(path, src_folder)
            relative_parts = _path_components(relative)
            if len(relative_parts) > 1:
                return relative_parts[0].upper()
        except ValueError:
            pass

    parent = os.path.basename(os.path.dirname(path))
    return parent.upper() if parent else "SINGLE_CAMERA"

def extract_camera_key(video_path, src_folder=None):
    basename = _basename(video_path)
    camera_id = extract_camera_id(basename)
    if camera_id:
        return camera_id

    info = parse_video_filename(basename)
    if info.camera_key:
        return f"{info.camera_key}:{_infer_device_key(video_path, src_folder)}"

    return None

def group_videos_by_camera(videos, src_folder=None):
    # 按照摄像机ID进行初始分组
    camera_groups = {}
    for video in videos:
        camera_key = extract_camera_key(video, src_folder)
        if camera_key not in camera_groups:
            camera_groups[camera_key] = []
        camera_groups[camera_key].append(video)

    # 返回所有分组
    return list(camera_groups.values())

def _video_sort_key(path):
    basename = _basename(path)
    info = parse_video_filename(basename)
    if info.datetime:
        return (info.datetime, basename)
    return (datetime.max, basename)

def group_videos_by_time(video_camera_groups, max_gap_seconds=None, duration_resolver=None):
    final_groups = []

    # 对每个摄像机组内的视频按时间进行进一步分组
    for series_index, video_series in enumerate(video_camera_groups, start=1):
        print(
            f"Grouping camera series {series_index}/{len(video_camera_groups)} "
            f"({len(video_series)} files)..."
        )
        video_series.sort(key=_video_sort_key)
        if duration_resolver:
            duration_resolver.prepare_series(video_series)
        time_grouped = []
        current_group = []

        for i, video in enumerate(video_series):
            if i == 0:
                current_group.append(video)
                continue

            current_info = parse_video_filename(_basename(video))
            previous_info = parse_video_filename(_basename(video_series[i - 1]))

            previous_video = video_series[i - 1]
            previous_effective_end = _effective_end(
                previous_info,
                previous_video,
                duration_resolver,
            )
            if not current_info.datetime or not previous_effective_end:
                time_grouped.append(current_group)
                current_group = [video]
                continue

            time_diff = (current_info.datetime - previous_effective_end).total_seconds()
            max_time_difference = (
                max_gap_seconds
                if max_gap_seconds is not None
                else current_info.max_time_difference
            )
            if max_time_difference is not None and time_diff <= max_time_difference:
                current_group.append(video)
            else:
                time_grouped.append(current_group)
                current_group = [video]

        if current_group:
            time_grouped.append(current_group)

        final_groups.extend(time_grouped)

    return final_groups

def check_file_exists(file_path):
    return os.path.exists(file_path)

def create_combined_filename(first_video, last_video, duration_resolver=None):
    """创建合并后的文件名，格式为：第一个视频时间_最后一个视频结束时间_其余部分.MP4"""
    first_basename = _basename(first_video)
    last_basename = _basename(last_video)

    first_info = parse_video_filename(first_basename)
    last_info = parse_video_filename(last_basename)

    if not first_info.datetime or not last_info.datetime:
        return _mp4_name_for_transport_stream(first_basename)  # 如果无法提取时间，返回原始文件名

    # 将datetime对象转换为字符串格式
    first_timestamp = first_info.datetime.strftime("%Y%m%d%H%M%S")
    last_effective_end = _effective_end(last_info, last_video, duration_resolver)
    if not last_effective_end:
        last_effective_end = last_info.datetime
    last_timestamp = last_effective_end.strftime("%Y%m%d%H%M%S")

    return f"{first_timestamp}_{last_timestamp}_{_mp4_name_for_transport_stream(first_info.rest_of_filename)}"

def _concat_file_line(video_path):
    concat_path = os.path.abspath(video_path)
    if os.name == "nt":
        concat_path = concat_path.replace("\\", "/")
    concat_path = concat_path.replace("'", "'\\''")
    return f"file '{concat_path}'\n"

def _write_concat_list(concat_list_path, video_group):
    with open(concat_list_path, "w") as f:
        for video in video_group:
            f.write(_concat_file_line(video))

def _copy_merge_videos(video_group, combined_file):
    concat_list_path = combined_file + ".concat_list.txt"
    _write_concat_list(concat_list_path, video_group)

    try:
        command = [
            FFMPEG, "-y",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c", "copy",
            "-movflags", "+faststart",
            combined_file,
        ]
        subprocess.run(command, check=True)
    finally:
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)

def merge_videos(video_group, combined_file, enable_compress=False, cq_override=None, warning_collector=None):
    # 获取最后一个视频文件的时间属性
    last_video = video_group[-1]
    last_video_stats = os.stat(last_video)
    last_access_time = last_video_stats.st_atime
    last_mod_time = last_video_stats.st_mtime

    # 获取通道ID（从第一个视频文件名提取）
    camera_id = extract_camera_id(_basename(video_group[0]))

    if len(video_group) == 1 and enable_compress:
        # 单文件 + 压缩：直接从源压缩到目标
        print(f"Compressing single file: {video_group[0]} to {combined_file}")
        success, in_sz, out_sz, elapsed, tracker = compress_video(video_group[0], combined_file, camera_id, cq_override)
        if warning_collector is not None and tracker is not None:
            warning_collector.append((combined_file, tracker))
        if success:
            os.utime(combined_file, (last_access_time, last_mod_time))
            return in_sz, out_sz, elapsed
        else:
            # 压缩失败时回退到无压缩输出；.ts 需要 remux 到 mp4，不能直接复制
            print("  Compression failed, falling back to copy/remux...")
            if os.path.splitext(video_group[0])[1].lower() == os.path.splitext(combined_file)[1].lower():
                shutil.copy2(video_group[0], combined_file)
            else:
                _copy_merge_videos(video_group, combined_file)
            return 0, 0, 0

    if len(video_group) == 1 and not enable_compress and os.path.splitext(video_group[0])[1].lower() == ".mp4":
        # 单文件 + 不压缩：直接复制
        print(f"Copying single file: {video_group[0]} to {combined_file}")
        shutil.copy2(video_group[0], combined_file)
        return 0, 0, 0

    print(f"Merging {len(video_group)} files into: {combined_file}")
    print(f"Files to merge: {[_basename(v) for v in video_group]}")

    # 写入 concat 列表
    concat_list_path = combined_file + ".concat_list.txt"
    _write_concat_list(concat_list_path, video_group)

    if enable_compress:
        # 合并+压缩一步完成：concat 直接输入到 hevc_nvenc
        profile = get_compress_profile(camera_id, cq_override)
        input_size = sum(os.path.getsize(v) for v in video_group)
        print(f"  Merge+Compress [{camera_id or '??'}] CQ{profile['cq']} ({len(video_group)} files)...")

        temp_output = combined_file + ".compress_tmp.mp4"
        cmd = [
            FFMPEG,
            "-y",
            "-hwaccel", "cuda",
            "-fflags", "+genpts+igndts",
            "-err_detect", "ignore_err",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c:v", "hevc_nvenc",
            "-preset", profile["preset"],
            "-rc", "vbr",
            "-cq", str(profile["cq"]),
            "-b:v", profile["bitrate"],
            "-maxrate", profile["maxrate"],
            "-bufsize", profile["bufsize"],
            "-multipass", "fullres",
            "-rc-lookahead", "32",
            "-spatial-aq", "1",
            "-temporal-aq", "1",
            "-bf", "4",
            "-b_ref_mode", "middle",
            "-c:a", "copy",
            "-movflags", "+faststart",
            temp_output,
        ]

        print(f"  CMD: {' '.join(cmd)}")
        returncode, elapsed, tracker = _run_ffmpeg_capturing_warnings(cmd)

        os.remove(concat_list_path)

        if warning_collector is not None:
            warning_collector.append((combined_file, tracker))

        if returncode != 0:
            if os.path.exists(temp_output):
                os.remove(temp_output)
            print(f"  ERROR: Merge+Compress failed!")
            return 0, 0, 0

        if os.path.exists(combined_file):
            os.remove(combined_file)
        os.rename(temp_output, combined_file)

        output_size = os.path.getsize(combined_file)
        ratio = input_size / output_size if output_size > 0 else 0
        saving = (1 - output_size / input_size) * 100 if input_size > 0 else 0
        print(
            f"  Compressed: {format_size(input_size)} -> {format_size(output_size)} "
            f"({ratio:.1f}x ratio, -{saving:.0f}%) in {elapsed:.1f}s"
        )

        # 设置时间属性
        os.utime(combined_file, (last_access_time, last_mod_time))
        return input_size, output_size, elapsed
    else:
        # 不压缩：仅 stream copy 合并；.ts 也会 remux 到 .mp4 输出
        _copy_merge_videos(video_group, combined_file)
        print("Merge complete.")

    # 设置合并后文件的时间属性为最后一个视频文件的时间属性
    os.utime(combined_file, (last_access_time, last_mod_time))
    return 0, 0, 0

def process_videos_in_folder(
    src_folder,
    target_folder_base,
    enable_compress=False,
    cq_override=None,
    max_gap_seconds=None,
    duration_resolver=None,
):
    video_files = []
    other_files = []

    # 优化扫描文件速度，使用os.scandir递归
    print("Scanning for files...")
    def scan_folder(folder):
        for entry in os.scandir(folder):
            if entry.is_dir():
                scan_folder(entry.path)
            elif entry.is_file():
                # 排除0B文件
                if entry.stat().st_size == 0:
                    print(f"Skipping 0B file: {entry.path}")
                    continue
                if _is_video_file(entry.name):
                    video_files.append(entry.path)
                else:
                    other_files.append(entry.path)

    scan_folder(src_folder)
    print(f"Found {len(video_files)} video files and {len(other_files)} other files.")

    # 处理视频文件
    if video_files:
        # 按照摄像机ID进行初始分组
        camera_groups = group_videos_by_camera(video_files, src_folder)
        print(f"Video files divided into {len(camera_groups)} different camera groups.")

        # 进一步按照时间关系进行分组
        grouped_videos = group_videos_by_time(camera_groups, max_gap_seconds, duration_resolver)
        total_groups = len(grouped_videos)
        print(f"Total video groups to process: {total_groups}")

        processed_groups = 0
        skipped_groups = 0
        failed_groups = 0
        total_input_size = 0
        total_output_size = 0
        total_elapsed = 0
        warning_collector = []

        # 处理每个视频组
        for group in grouped_videos:
            processed_groups += 1

            # 获取该组的第一个视频文件和最后一个视频文件
            first_video = group[0]
            last_video = group[-1]

            # 获取原文件的相对路径
            relative_dir = os.path.dirname(os.path.relpath(first_video, src_folder))
            target_folder = os.path.join(target_folder_base, relative_dir)

            # 创建目标文件夹
            if not os.path.exists(target_folder):
                os.makedirs(target_folder)

            # 构建输出文件名 - 使用新的命名格式
            combined_file_name = create_combined_filename(first_video, last_video, duration_resolver)
            combined_file_path = os.path.join(target_folder, combined_file_name)

            print(f"\nProcessing group {processed_groups}/{total_groups}: {combined_file_name}")
            print(f"Group contains {len(group)} files")

            if not check_file_exists(combined_file_path):
                in_sz, out_sz, elapsed = merge_videos(group, combined_file_path, enable_compress, cq_override, warning_collector=warning_collector)
                total_input_size += in_sz
                total_output_size += out_sz
                total_elapsed += elapsed
                if enable_compress and in_sz > 0 and out_sz == 0:
                    failed_groups += 1
            else:
                print(f"Combined file already exists: {combined_file_path}, skipping...")
                skipped_groups += 1

        # 打印汇总
        print(f"\n{'='*70}")
        print("处理完成汇总")
        print(f"{'='*70}")
        print(f"  视频组总数: {total_groups}")
        print(f"  已处理: {processed_groups - skipped_groups - failed_groups}")
        print(f"  已跳过: {skipped_groups}")
        if failed_groups > 0:
            print(f"  失败: {failed_groups}")
        if enable_compress and total_input_size > 0:
            overall_ratio = total_input_size / total_output_size if total_output_size > 0 else 0
            overall_saving = (1 - total_output_size / total_input_size) * 100
            print(f"  原始总大小: {format_size(total_input_size)}")
            print(f"  压缩后总大小: {format_size(total_output_size)}")
            print(f"  总体压缩比: {overall_ratio:.1f}x")
            print(f"  总体节省: {overall_saving:.0f}%")
            hours = int(total_elapsed // 3600)
            minutes = int((total_elapsed % 3600) // 60)
            seconds = int(total_elapsed % 60)
            if hours > 0:
                print(f"  压缩总耗时: {hours}h{minutes:02d}m{seconds:02d}s")
            elif minutes > 0:
                print(f"  压缩总耗时: {minutes}m{seconds:02d}s")
            else:
                print(f"  压缩总耗时: {seconds}s")
        print(f"{'='*70}")

        # ============ FFmpeg 警告汇总 ============
        if enable_compress and warning_collector:
            suspicious_items = []
            warn_items = []
            ok_count = 0
            os.makedirs(target_folder_base, exist_ok=True)
            master_report_path = os.path.join(target_folder_base, "_transcode_warnings.txt")
            if os.path.exists(master_report_path):
                os.remove(master_report_path)

            for output_path, tracker in warning_collector:
                severity = tracker.severity()
                if severity == "OK":
                    ok_count += 1
                    continue
                # 写每文件 .warn.log
                _write_per_file_warning_log(output_path, tracker, ["(see master report)"])
                _append_master_warning_report(target_folder_base, output_path, tracker)
                if severity == "SUSPICIOUS":
                    suspicious_items.append((output_path, tracker))
                else:
                    warn_items.append((output_path, tracker))

            print(f"\n{'='*70}")
            print("FFmpeg 转码警告汇总")
            print(f"{'='*70}")
            print(f"  干净: {ok_count}")
            print(f"  轻微警告 (WARN): {len(warn_items)}")
            print(f"  严重 (SUSPICIOUS, 强烈建议人工检查): {len(suspicious_items)}")
            if suspicious_items:
                print("\n  ⚠ SUSPICIOUS 文件（画面可能损坏 / 暂停）:")
                for output_path, tracker in suspicious_items:
                    rel = os.path.relpath(output_path, target_folder_base)
                    print(f"    - {rel}")
                    print(f"        {tracker.format_oneline()}")
            if warn_items or suspicious_items:
                print(f"\n  详细报告: {master_report_path}")
                print(f"  每个有警告的输出旁边还会有同名 .warn.log")
            print(f"{'='*70}")

    # 处理其他类型文件
    if other_files:
        print("\nProcessing other file types...")
        copied_count = 0
        skipped_count = 0

        for file_path in other_files:
            relative_path = os.path.relpath(file_path, src_folder)
            target_file_path = os.path.join(target_folder_base, relative_path)
            target_dir = os.path.dirname(target_file_path)

            # 确保目标目录存在
            if not os.path.exists(target_dir):
                os.makedirs(target_dir)

            if not check_file_exists(target_file_path):
                print(f"Copying: {relative_path}")
                shutil.copy2(file_path, target_file_path)
                copied_count += 1
            else:
                print(f"File already exists: {relative_path}, skipping...")
                skipped_count += 1

        print(f"\nOther files processing completed. {copied_count} files copied, {skipped_count} files skipped.")

    print("\nAll processing completed successfully!")

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
    )

    process_videos_in_folder(
        src_folder,
        target_folder_base,
        enable_compress,
        args.cq,
        args.max_gap_seconds,
        duration_resolver,
    )
    _pause_before_exit()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        is_error = isinstance(code, str) or code not in (None, 0)
        if is_error:
            if isinstance(code, str) and code:
                print(f"ERROR: {code}")
            _pause_before_exit()
        raise
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        _pause_before_exit()
        raise SystemExit(130)
    except Exception:
        print("\nFATAL ERROR: An unexpected error occurred.")
        traceback.print_exc()
        _pause_before_exit()
        raise SystemExit(1)
