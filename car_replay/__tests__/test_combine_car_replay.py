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


class WarningTrackerTests(unittest.TestCase):
    def test_clean_run_is_ok(self):
        t = replay.WarningTracker()
        t.feed("frame=  100 fps= 30 q=28.0 size=    1024kB time=00:00:03.33 bitrate=2517kbits/s")
        t.feed("[hevc_nvenc @ 0x1] using NVENC capabilities")
        self.assertEqual(t.severity(), "OK")
        self.assertEqual(t.total_warnings, 0)

    def test_a_single_corrupt_frame_is_suspicious(self):
        t = replay.WarningTracker()
        t.feed("[h264 @ 0xabc] corrupt decoded frame in stream 0")
        self.assertEqual(t.severity(), "SUSPICIOUS")
        self.assertEqual(t.counts["corrupt_frame"], 1)

    def test_few_invalid_dts_are_warn_not_suspicious(self):
        t = replay.WarningTracker()
        for _ in range(7):
            t.feed("[mov @ 0x1] Invalid DTS: 100 PTS: 99, replacing by guess")
        self.assertEqual(t.severity(), "WARN")
        self.assertEqual(t.counts["invalid_dts"], 7)

    def test_many_invalid_dts_become_suspicious(self):
        t = replay.WarningTracker()
        for _ in range(20):
            t.feed("[mov @ 0x1] Invalid DTS: 100 PTS: 99, replacing by guess")
        self.assertEqual(t.severity(), "SUSPICIOUS")

    def test_each_line_only_classified_once(self):
        t = replay.WarningTracker()
        # 同时含 "Invalid DTS" 和 "replacing by guess"，应只算 invalid_dts 一次
        t.feed("[mov @ 0x1] Invalid DTS: 100 PTS: 99, replacing by guess")
        self.assertEqual(t.counts["invalid_dts"], 1)
        self.assertEqual(t.counts["guess_pts"], 0)
        self.assertEqual(t.total_warnings, 1)

    def test_oneline_summary_lists_categories(self):
        t = replay.WarningTracker()
        t.feed("[h264 @ 0x1] error while decoding MB 1 2")
        t.feed("[h264 @ 0x2] concealing 1234 DC, 0 AC")
        t.feed("[h264 @ 0x3] concealing 999 DC")
        summary = t.format_oneline()
        self.assertIn("decode_error=1", summary)
        self.assertIn("concealing=2", summary)





if __name__ == "__main__":
    unittest.main()
