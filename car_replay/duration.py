"""时长解析：DurationResolver 及 _effective_end。"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from datetime import timedelta

from .config import FFPROBE
from .naming import _basename, parse_video_filename


class DurationResolver:
    WINDOWS_METADATA_BATCH_SIZE = 500
    DURATION_MATCH_TOLERANCE_SECONDS = 1.0
    HEALTH_BAD_PATTERNS = (
        "Invalid DTS",
        "Invalid PTS",
        "corrupt frame",
        "corrupt decoded frame",
        "non-existing PPS",
        "missing reference",
        "Could not find ref",
        "application_invalid",
    )

    def __init__(
        self,
        enabled=True,
        fallback_seconds=None,
        use_windows_metadata=True,
        adaptive_sampling=True,
        track_health=False,
    ):
        self.enabled = enabled
        self.fallback_seconds = fallback_seconds
        self.use_windows_metadata = use_windows_metadata
        # 健康追踪要求每个文件单独 probe，与自适应抽样及 Windows metadata 快查冲突。
        self.track_health = track_health
        self.adaptive_sampling = adaptive_sampling and not track_health
        if track_health:
            self.use_windows_metadata = False
        self.cache = {}
        self.cache_sources = {}
        self.health_cache = {}
        self.windows_metadata_attempted = False
        self.stats = {
            "windows_metadata_hits": 0,
            "ffprobe_probes": 0,
            "adaptive_reused": 0,
            "unavailable": 0,
            "unhealthy": 0,
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
            f"unavailable={delta['unavailable']}, "
            f"unhealthy={delta['unhealthy']}"
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

    def is_healthy(self, path):
        """返回 True / False / None（None = 未检测）。"""
        return self.health_cache.get(self._cache_key(path))

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

        verbosity = "warning" if self.track_health else "error"
        command = [
            FFPROBE,
            "-v", verbosity,
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

        if self.track_health:
            stderr_text = result.stderr or ""
            healthy = not any(pat in stderr_text for pat in self.HEALTH_BAD_PATTERNS)
            self.health_cache[self._cache_key(path)] = healthy
            if not healthy:
                self.stats["unhealthy"] += 1
                first_match = next(
                    (pat for pat in self.HEALTH_BAD_PATTERNS if pat in stderr_text),
                    "?",
                )
                print(f"  Unhealthy input detected ({first_match}): {basename}")

        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
            # track_health 模式下 stdout 仍可能含有有效时长，先尝试解析。
            stdout_value = (result.stdout or "").strip()
            try:
                duration = float(stdout_value)
                if math.isfinite(duration) and duration > 0:
                    return duration
            except ValueError:
                pass
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
