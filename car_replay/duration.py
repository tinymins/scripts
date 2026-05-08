"""时长解析 + 健康探测：DurationResolver + DurationCache + _effective_end。

duration 与 broken 路径分离（plan §1）；JSON 持久化缓存按 (size, mtime_ns, ctime_ns) 失效；
ffprobe 调用统一走 `_run_ffprobe`（含 timeout）。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from .config import FFPROBE
from .ffmpeg_runner import _run_ffprobe
from .naming import _basename, parse_video_filename


def _cache_key(path) -> str:
    """归一化路径作 key（处理 Windows 盘符大小写、UNC、WSL）。"""
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _stat_fingerprint(path) -> Optional[Tuple[int, int, int]]:
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class DurationCache:
    """JSON 持久化缓存。entry 包含 size/mtime_ns/ctime_ns + duration/duration_source + health。"""

    def __init__(self, cache_path: Optional[Path], enabled: bool = True):
        self.cache_path: Optional[Path] = Path(cache_path) if cache_path else None
        self.enabled = enabled and self.cache_path is not None
        self._entries: Dict[str, dict] = {}
        if self.enabled:
            self._load()

    def _load(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            with self.cache_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._entries = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError, json.JSONDecodeError):
            self._entries = {}

    def save(self) -> None:
        if not self.enabled or not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._entries, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(str(tmp), str(self.cache_path))
        except OSError as exc:
            print(f"WARNING: 无法写入时长缓存 {self.cache_path}: {exc}")

    def get_valid_entry(self, path) -> Optional[dict]:
        """stat 匹配返回 entry；不匹配 / 不存在 → None。"""
        if not self.enabled:
            return None
        entry = self._entries.get(_cache_key(path))
        if not entry:
            return None
        fp = _stat_fingerprint(path)
        if fp is None:
            return None
        if (entry.get("size"), entry.get("mtime_ns"), entry.get("ctime_ns")) != fp:
            return None
        return entry

    def _entry_with_stat(self, path) -> Optional[dict]:
        fp = _stat_fingerprint(path)
        if fp is None:
            return None
        key = _cache_key(path)
        entry = self._entries.get(key)
        if not entry or (entry.get("size"), entry.get("mtime_ns"), entry.get("ctime_ns")) != fp:
            entry = {"size": fp[0], "mtime_ns": fp[1], "ctime_ns": fp[2],
                     "duration": None, "duration_source": None, "health": None}
            self._entries[key] = entry
        return entry

    def put_duration(self, path, duration: Optional[float], source: Optional[str]) -> None:
        if not self.enabled:
            return
        entry = self._entry_with_stat(path)
        if entry is not None:
            entry["duration"] = duration
            entry["duration_source"] = source

    def put_health(self, path, broken: bool) -> None:
        if not self.enabled:
            return
        entry = self._entry_with_stat(path)
        if entry is not None:
            entry["health"] = {"broken": bool(broken), "probed_at": time.time()}


class DurationResolver:
    def __init__(
        self, enabled: bool = True, fallback_seconds: Optional[float] = None,
        track_health: bool = False, cache_path: Optional[Path] = None,
        use_cache: bool = True, probe_workers: int = 4,
        probe_timeout: float = 60.0, with_health: bool = True,
    ):
        self.enabled = enabled
        self.fallback_seconds = fallback_seconds
        self.track_health = track_health
        self.probe_workers = max(1, int(probe_workers))
        self.probe_timeout = float(probe_timeout)
        self.with_health = bool(with_health)
        self.cache = DurationCache(cache_path, enabled=use_cache)
        self._mem_duration: Dict[str, Optional[float]] = {}
        self._mem_source: Dict[str, Optional[str]] = {}
        self._mem_broken: Dict[str, bool] = {}
        self.stats = {"ffprobe_probes": 0, "cache_hits": 0, "filename_hits": 0,
                      "unavailable": 0, "broken": 0}

    # ---- 公共接口 ----
    def duration_for_file(self, path) -> Optional[float]:
        key = _cache_key(path)
        if key in self._mem_duration:
            return self._mem_duration[key]
        cached = self.cache.get_valid_entry(path)
        if cached is not None and cached.get("duration_source") in ("filename", "ffprobe"):
            duration = cached.get("duration")
            self._mem_duration[key] = duration
            self._mem_source[key] = cached.get("duration_source")
            health = cached.get("health")
            if isinstance(health, dict):
                self._mem_broken[key] = bool(health.get("broken"))
            self.stats["cache_hits"] += 1
            return duration
        # filename fast-path
        info = parse_video_filename(_basename(path))
        if info.datetime and info.end_datetime:
            duration = (info.end_datetime - info.datetime).total_seconds()
            if duration > 0:
                self._record_duration(path, duration, "filename")
                self.stats["filename_hits"] += 1
                return duration
        # ffprobe
        if not self.enabled:
            return self._record_unavailable(path, "ffprobe duration probing disabled")
        duration, broken = _run_ffprobe(path, timeout=self.probe_timeout)
        self.stats["ffprobe_probes"] += 1
        if broken:
            self._record_broken(path)
            return self._record_unavailable(path, "ffprobe failed")
        self._record_duration(path, duration, "ffprobe")
        return duration

    def prepare_series(self, video_series: Iterable[str], *, with_health: Optional[bool] = None) -> None:
        """两遍并发解析：① duration ② 可选 health。"""
        if with_health is None:
            with_health = self.with_health
        videos = list(video_series)
        if not videos:
            return

        # 第一遍 duration
        pending_duration = []
        for video in videos:
            key = _cache_key(video)
            if key in self._mem_duration:
                continue
            cached = self.cache.get_valid_entry(video)
            if cached is not None and cached.get("duration_source") in ("filename", "ffprobe"):
                self._mem_duration[key] = cached.get("duration")
                self._mem_source[key] = cached.get("duration_source")
                health = cached.get("health")
                if isinstance(health, dict):
                    self._mem_broken[key] = bool(health.get("broken"))
                self.stats["cache_hits"] += 1
                continue
            info = parse_video_filename(_basename(video))
            if info.datetime and info.end_datetime:
                duration = (info.end_datetime - info.datetime).total_seconds()
                if duration > 0:
                    self._record_duration(video, duration, "filename")
                    self.stats["filename_hits"] += 1
                    continue
            pending_duration.append(video)

        if pending_duration:
            if self.enabled:
                print(f"📐 解析时长（ffprobe）: {len(pending_duration)} 个文件")
                self._run_parallel(pending_duration, self._probe_duration_worker)
            else:
                for v in pending_duration:
                    self._record_unavailable(v, "ffprobe duration probing disabled")

        # 第二遍 health
        if with_health:
            pending_health = []
            for video in videos:
                key = _cache_key(video)
                if key in self._mem_broken:
                    continue
                cached = self.cache.get_valid_entry(video)
                if cached is not None and isinstance(cached.get("health"), dict):
                    self._mem_broken[key] = bool(cached["health"].get("broken"))
                    continue
                pending_health.append(video)
            if pending_health:
                print(f"🩺 健康探测: {len(pending_health)} 个文件")
                self._run_parallel(pending_health, self._probe_health_worker)

        self.cache.save()
        print(
            f"📊 时长/健康解析汇总: cache={self.stats['cache_hits']}, "
            f"filename={self.stats['filename_hits']}, ffprobe={self.stats['ffprobe_probes']}, "
            f"broken={self.stats['broken']}, unavailable={self.stats['unavailable']}"
        )

    def ensure_health_probed(self, path) -> None:
        key = _cache_key(path)
        if key in self._mem_broken:
            return
        cached = self.cache.get_valid_entry(path)
        if cached is not None and isinstance(cached.get("health"), dict):
            self._mem_broken[key] = bool(cached["health"].get("broken"))
            return
        self._probe_health_worker(path)

    def is_broken(self, path) -> bool:
        """要求先 ensure_health_probed；未知绝不当 False。"""
        key = _cache_key(path)
        if key not in self._mem_broken:
            raise RuntimeError(
                f"path not health-probed yet: {path!r}; call ensure_health_probed() first"
            )
        return self._mem_broken[key]

    def is_healthy(self, path):
        """向后兼容：True / False / None（None = 未检测）。"""
        key = _cache_key(path)
        if key in self._mem_broken:
            return not self._mem_broken[key]
        return None

    # ---- 内部 ----
    def _run_parallel(self, items, worker) -> None:
        if self.probe_workers <= 1 or len(items) == 1:
            for item in items:
                worker(item)
            return
        with ThreadPoolExecutor(max_workers=self.probe_workers) as pool:
            list(pool.map(worker, items))

    def _probe_duration_worker(self, path) -> None:
        if not os.path.exists(FFPROBE):
            self._record_unavailable(path, f"ffprobe not found: {FFPROBE}")
            return
        duration, broken = _run_ffprobe(path, timeout=self.probe_timeout)
        self.stats["ffprobe_probes"] += 1
        if broken:
            self._record_broken(path)
            self._record_unavailable(path, "ffprobe failed")
            return
        self._record_duration(path, duration, "ffprobe")

    def _probe_health_worker(self, path) -> None:
        if not os.path.exists(FFPROBE):
            self._record_broken(path)
            return
        duration, broken = _run_ffprobe(path, timeout=self.probe_timeout)
        if broken:
            self._record_broken(path)
            return
        key = _cache_key(path)
        if key not in self._mem_duration and duration is not None:
            self._record_duration(path, duration, "ffprobe")
        self._mem_broken[key] = False
        self.cache.put_health(path, broken=False)

    def _record_duration(self, path, duration: Optional[float], source: Optional[str]) -> None:
        key = _cache_key(path)
        self._mem_duration[key] = duration
        self._mem_source[key] = source
        self.cache.put_duration(path, duration, source)

    def _record_broken(self, path) -> None:
        key = _cache_key(path)
        if not self._mem_broken.get(key):
            self.stats["broken"] += 1
            print(f"  ⚠ broken 文件（ffprobe 失败/超时）: {_basename(path)}")
        self._mem_broken[key] = True
        self.cache.put_health(path, broken=True)

    def _record_unavailable(self, path, reason: str) -> Optional[float]:
        basename = _basename(path)
        if self.fallback_seconds is not None:
            print(f"WARNING: {reason} for {basename}; using fallback duration {self.fallback_seconds}s")
            self._record_duration(path, self.fallback_seconds, "ffprobe")
            return self.fallback_seconds
        print(f"ERROR: {reason} for {basename}; duration unavailable")
        self.stats["unavailable"] += 1
        self._record_duration(path, None, None)
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
    return None
