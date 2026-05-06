import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import combine_car_replay as replay


def make_ls_ar_videos(count, start, interval_seconds=60):
    return [
        (
            f"/clips/MOV{index:04d}_"
            f"{(start + timedelta(seconds=index * interval_seconds)):%Y%m%d%H%M%S}.mp4"
        )
        for index in range(count)
    ]


class FakeDurationResolver(replay.DurationResolver):
    def __init__(self, durations=None, metadata=None, adaptive_sampling=True):
        super().__init__(
            enabled=True,
            use_windows_metadata=False,
            adaptive_sampling=adaptive_sampling,
        )
        self.durations = durations or {}
        self.metadata = metadata or {}
        self.ffprobe_calls = []

    def _prefetch_windows_metadata(self, paths):
        for path in paths:
            if path in self.metadata:
                self._cache_duration(path, self.metadata[path], "windows_metadata")
                self.stats["windows_metadata_hits"] += 1

    def _resolve_duration(self, path):
        self.ffprobe_calls.append(path)
        self.stats["ffprobe_probes"] += 1
        return self.durations[path]


class DurationResolverTests(unittest.TestCase):
    def test_stable_duration_series_uses_endpoint_sampling(self):
        videos = make_ls_ar_videos(5, datetime(2026, 5, 3, 12, 0, 0))
        resolver = FakeDurationResolver({path: 60 for path in videos})

        groups = replay.group_videos_by_time([videos], duration_resolver=resolver)

        self.assertEqual(groups, [videos])
        self.assertEqual(resolver.ffprobe_calls, [videos[0], videos[-1]])
        self.assertEqual(resolver.stats["adaptive_reused"], 3)

    def test_windows_metadata_cache_avoids_ffprobe(self):
        videos = make_ls_ar_videos(5, datetime(2026, 5, 3, 12, 0, 0))
        resolver = FakeDurationResolver(metadata={path: 60 for path in videos})

        groups = replay.group_videos_by_time([videos], duration_resolver=resolver)

        self.assertEqual(groups, [videos])
        self.assertEqual(resolver.ffprobe_calls, [])
        self.assertEqual(resolver.stats["windows_metadata_hits"], len(videos))

    def test_changed_tail_duration_is_probed_and_used_for_output_name(self):
        videos = make_ls_ar_videos(5, datetime(2026, 5, 3, 12, 0, 0))
        durations = {path: 60 for path in videos}
        durations[videos[-1]] = 30
        resolver = FakeDurationResolver(durations)

        groups = replay.group_videos_by_time([videos], duration_resolver=resolver)
        output_name = replay.create_combined_filename(videos[0], videos[-1], resolver)

        self.assertEqual(groups, [videos])
        self.assertIn("20260503120430", output_name)
        self.assertIn(videos[2], resolver.ffprobe_calls)
        self.assertIn(videos[3], resolver.ffprobe_calls)
        self.assertLess(len(resolver.ffprobe_calls), len(videos))


if __name__ == "__main__":
    unittest.main()
