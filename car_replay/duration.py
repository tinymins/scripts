"""时长解析：DurationResolver 及 _effective_end。"""

from __future__ import annotations

import math
import os
import subprocess
from datetime import timedelta

from .config import FFPROBE
from .naming import _basename, parse_video_filename


class DurationResolver:
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
        track_health=False,
    ):
        self.enabled = enabled
        self.fallback_seconds = fallback_seconds
        self.track_health = track_health
        self.cache = {}
        self.cache_sources = {}
        self.health_cache = {}
        self.stats = {
            "ffprobe_probes": 0,
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

        delta = {
            key: self.stats[key] - before.get(key, 0)
            for key in self.stats
        }
        print(
            "Duration resolving summary: "
            f"ffprobe probes={delta['ffprobe_probes']}, "
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
