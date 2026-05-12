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
from typing import Dict, Iterable, List, Optional, Tuple

from .config import FFPROBE
from . import console
from .ffmpeg_runner import ProbeResult, _run_ffprobe
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
            console.warn(f"无法写入时长缓存 {self.cache_path}: {exc}")

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

    def put_probe(self, path, probe: ProbeResult) -> None:
        """存储完整 ffprobe 结果（含 codec/size_bytes/format_bps/width/height）。"""
        if not self.enabled:
            return
        entry = self._entry_with_stat(path)
        if entry is not None:
            entry["probe_codec"] = probe.codec
            entry["probe_format_bps"] = probe.format_bps
            entry["probe_size_bytes"] = probe.size_bytes
            entry["probe_width"] = probe.width
            entry["probe_height"] = probe.height
            entry["probe_done"] = True
            if probe.duration is not None:
                entry["duration"] = probe.duration
                entry["duration_source"] = "ffprobe"
            entry["health"] = {"broken": probe.broken, "probed_at": time.time()}


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
        self._mem_probe: Dict[str, ProbeResult] = {}
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
        probe = _run_ffprobe(path, timeout=self.probe_timeout)
        self.stats["ffprobe_probes"] += 1
        if probe.broken:
            self._record_broken(path)
            return self._record_unavailable(path, "ffprobe failed")
        self._record_duration(path, probe.duration, "ffprobe")
        return probe.duration

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
                console.step(f"📐 解析时长（ffprobe）: {len(pending_duration)} 个文件")
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
                console.step(f"🩺 健康探测: {len(pending_health)} 个文件")
                self._run_parallel(pending_health, self._probe_health_worker)

        self.cache.save()
        console.kvtable([
            ("cache 命中", self.stats['cache_hits']),
            ("filename 命中", self.stats['filename_hits']),
            ("ffprobe 探测", self.stats['ffprobe_probes']),
            ("broken", self.stats['broken']),
            ("unavailable", self.stats['unavailable']),
        ])

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

    def probe(self, p: Path) -> ProbeResult:
        """返回单个文件的完整 ProbeResult（codec/bitrate/size/duration）。

        优先从内存缓存取，cache miss 时调 ffprobe 并回写缓存。
        同时更新 duration 和 health 缓存。
        """
        path = str(p)
        key = _cache_key(path)
        if key in self._mem_probe:
            return self._mem_probe[key]
        # 尝试从磁盘缓存重建
        cached = self.cache.get_valid_entry(path)
        if cached is not None and cached.get("probe_done"):
            result = ProbeResult(
                duration=cached.get("duration"),
                broken=bool((cached.get("health") or {}).get("broken", True)),
                codec=cached.get("probe_codec"),
                format_bps=cached.get("probe_format_bps"),
                size_bytes=cached.get("probe_size_bytes"),
                width=cached.get("probe_width"),
                height=cached.get("probe_height"),
            )
            self._mem_probe[key] = result
            return result
        # ffprobe
        result = _run_ffprobe(path, timeout=self.probe_timeout)
        self._mem_probe[key] = result
        self.cache.put_probe(path, result)
        if not result.broken and result.duration is not None:
            self._record_duration(path, result.duration, "ffprobe")
        elif result.broken:
            key2 = _cache_key(path)
            if not self._mem_broken.get(key2):
                self.stats["broken"] += 1
                console.warn(f"broken 文件（ffprobe 失败/超时）: {_basename(path)}", indent=2)
            self._mem_broken[key2] = True
        return result

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
        probe = _run_ffprobe(path, timeout=self.probe_timeout)
        self.stats["ffprobe_probes"] += 1
        if probe.broken:
            self._record_broken(path)
            self._record_unavailable(path, "ffprobe failed")
            return
        self._record_duration(path, probe.duration, "ffprobe")

    def _probe_health_worker(self, path) -> None:
        if not os.path.exists(FFPROBE):
            self._record_broken(path)
            return
        probe = _run_ffprobe(path, timeout=self.probe_timeout)
        if probe.broken:
            self._record_broken(path)
            return
        key = _cache_key(path)
        if key not in self._mem_duration and probe.duration is not None:
            self._record_duration(path, probe.duration, "ffprobe")
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
            console.warn(f"broken 文件（ffprobe 失败/超时）: {_basename(path)}", indent=2)
        self._mem_broken[key] = True
        self.cache.put_health(path, broken=True)

    def _record_unavailable(self, path, reason: str) -> Optional[float]:
        basename = _basename(path)
        if self.fallback_seconds is not None:
            console.warn(
                f"{reason} for {basename}; using fallback duration {self.fallback_seconds}s",
            )
            self._record_duration(path, self.fallback_seconds, "ffprobe")
            return self.fallback_seconds
        console.error(f"{reason} for {basename}; duration unavailable")
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


def group_codec_and_bitrate(
    paths: List[Path], resolver
) -> Tuple[Optional[str], Optional[int]]:
    """聚合一组视频的 codec 与平均 bitrate。

    返回 (codec_result, avg_bps_result)：
      - codec_result: 全部一致返回该值；混合返回 "mixed"；全 None 返回 None
      - avg_bps_result: sum(size_bytes)*8/sum(duration)（使用 ffprobe format.size）；
        任一项缺失则跳过该文件；全部缺失则返回 None

    resolver 需实现 probe(p: Path) -> ProbeResult 接口（DurationResolver 已支持）。
    """
    codecs: List[str] = []
    total_bytes = 0
    total_secs = 0.0
    any_size = False

    for p in paths:
        pr = resolver.probe(Path(p))
        if pr.codec:
            codecs.append(pr.codec)
        if (pr.size_bytes is not None and pr.duration is not None and pr.duration > 0):
            total_bytes += pr.size_bytes
            total_secs += pr.duration
            any_size = True

    if not codecs:
        codec_result: Optional[str] = None
    elif len(set(codecs)) == 1:
        codec_result = codecs[0]
    else:
        codec_result = "mixed"

    if not any_size or total_secs <= 0:
        bps_result: Optional[int] = None
    else:
        bps_result = int(total_bytes * 8 / total_secs)

    return codec_result, bps_result
