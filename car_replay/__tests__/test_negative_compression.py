"""负压缩防护单元测试。

覆盖两层关键逻辑：
1. 运行时迟滞状态机 (`_evaluate_negative_compression_state`)
2. Pre-flight 码率预检 (`preflight_should_skip_nvenc`)

post-check（成功路径但 out_size > in_size）逻辑过于简单
（一个 if 比较），且嵌在 compress_video / merge_videos 主流程里，
这里不单独 mock subprocess 测试，留给真机 smoke 验证。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from car_replay.compress import (
    _parse_profile_bitrate_to_bps,
    preflight_should_skip_nvenc,
)
from car_replay.config import NEGATIVE_COMPRESSION_THRESHOLDS
from car_replay.ffmpeg_runner import (
    _evaluate_negative_compression_state,
    _parse_ffmpeg_size_to_bytes,
)


def _fresh_state() -> dict:
    return {
        "monitor_phase": "OK",
        "monitor_warn_started_at": None,
        "monitor_good_streak_started_at": None,
        "monitor_last_ratio": None,
        "monitor_aborted": False,
        "monitor_abort_reason": None,
    }


# ---------------- 状态机 ----------------


class TestNegativeCompressionStateMachine(unittest.TestCase):
    """`_evaluate_negative_compression_state` 的纯逻辑测试。"""

    EXPECTED_DURATION = 600.0  # 10 min
    EXPECTED_INPUT_BYTES = 600_000_000  # 600 MB

    def _step(self, state, *, ratio, wall_elapsed_s, now_s, progress_pct=0.5):
        """根据目标 ratio 反推 cur_out_bytes，喂给状态机。"""
        cur_secs = self.EXPECTED_DURATION * progress_pct
        # predicted_out = cur_out_bytes / cur_secs * expected_duration
        # ratio = predicted_out / expected_input_bytes
        # → cur_out_bytes = ratio * expected_input_bytes * cur_secs / expected_duration
        cur_out_bytes = int(ratio * self.EXPECTED_INPUT_BYTES * cur_secs / self.EXPECTED_DURATION)
        return _evaluate_negative_compression_state(
            state,
            cur_secs=cur_secs,
            cur_out_bytes=cur_out_bytes,
            expected_duration=self.EXPECTED_DURATION,
            expected_input_bytes=self.EXPECTED_INPUT_BYTES,
            wall_elapsed_s=wall_elapsed_s,
            now_s=now_s,
            thresholds=NEGATIVE_COMPRESSION_THRESHOLDS,
        )

    def test_warmup_skipped_by_wall_clock(self):
        """warmup_secs 之内即使 ratio 很高也不进入 WARN。"""
        state = _fresh_state()
        warmup = NEGATIVE_COMPRESSION_THRESHOLDS["warmup_secs"]
        reason = self._step(state, ratio=2.0, wall_elapsed_s=warmup - 1, now_s=1000.0)
        self.assertIsNone(reason)
        self.assertEqual(state["monitor_phase"], "OK")

    def test_warmup_skipped_by_progress_pct(self):
        """progress 不足 warmup_progress_pct 时不评估。"""
        state = _fresh_state()
        # wall_elapsed 已过 warmup，但 progress 太小
        thr = NEGATIVE_COMPRESSION_THRESHOLDS
        small_pct = thr["warmup_progress_pct"] / 2
        reason = self._step(
            state, ratio=3.0, wall_elapsed_s=thr["warmup_secs"] + 10, now_s=1000.0,
            progress_pct=small_pct,
        )
        self.assertIsNone(reason)
        self.assertEqual(state["monitor_phase"], "OK")

    def test_ok_to_warn_to_abort(self):
        """ratio 持续超过 enter_bad_ratio + abort_hold_secs 后必须 abort。"""
        state = _fresh_state()
        thr = NEGATIVE_COMPRESSION_THRESHOLDS
        wall = thr["warmup_secs"] + 10
        # 第 1 步：进入 WARN
        r1 = self._step(state, ratio=1.05, wall_elapsed_s=wall, now_s=1000.0)
        self.assertIsNone(r1)
        self.assertEqual(state["monitor_phase"], "WARN")

        # 第 2 步：abort_hold_secs - 1 还不够
        r2 = self._step(state, ratio=1.05, wall_elapsed_s=wall + thr["abort_hold_secs"] - 1,
                        now_s=1000.0 + thr["abort_hold_secs"] - 1)
        self.assertIsNone(r2)
        self.assertFalse(state["monitor_aborted"])

        # 第 3 步：跨过 abort_hold_secs，且 ratio > abort_ratio → abort
        r3 = self._step(state, ratio=1.10, wall_elapsed_s=wall + thr["abort_hold_secs"] + 1,
                        now_s=1000.0 + thr["abort_hold_secs"] + 1)
        self.assertIsNotNone(r3)
        self.assertTrue(state["monitor_aborted"])
        self.assertIn("negative compression", r3 or "")

    def test_warn_recovers_to_ok_then_no_abort(self):
        """WARN 状态下 ratio 持续低于 exit_good_ratio 满 exit_ok_secs 应回 OK。"""
        state = _fresh_state()
        thr = NEGATIVE_COMPRESSION_THRESHOLDS
        wall = thr["warmup_secs"] + 5
        # 进 WARN
        self._step(state, ratio=1.0, wall_elapsed_s=wall, now_s=2000.0)
        self.assertEqual(state["monitor_phase"], "WARN")

        # ratio 回落到 good：第 1 次开始 streak
        self._step(state, ratio=0.5, wall_elapsed_s=wall + 1, now_s=2001.0)
        self.assertEqual(state["monitor_phase"], "WARN")
        self.assertIsNotNone(state["monitor_good_streak_started_at"])

        # streak 持续 exit_ok_secs 以上 → 回 OK
        self._step(state, ratio=0.5, wall_elapsed_s=wall + 1 + thr["exit_ok_secs"] + 1,
                   now_s=2001.0 + thr["exit_ok_secs"] + 1)
        self.assertEqual(state["monitor_phase"], "OK")
        self.assertFalse(state["monitor_aborted"])

    def test_warn_streak_resets_on_bad_sample(self):
        """好样本 streak 期间出现一次坏样本应清零，下次再坏从头算 abort 计时。"""
        state = _fresh_state()
        thr = NEGATIVE_COMPRESSION_THRESHOLDS
        wall = thr["warmup_secs"] + 5
        self._step(state, ratio=1.05, wall_elapsed_s=wall, now_s=3000.0)
        self.assertEqual(state["monitor_phase"], "WARN")

        # 一个 good 样本：建立 streak
        self._step(state, ratio=0.5, wall_elapsed_s=wall + 1, now_s=3001.0)
        self.assertIsNotNone(state["monitor_good_streak_started_at"])

        # 但 streak 还没满 exit_ok_secs，又来一个坏样本：streak 清零
        self._step(state, ratio=1.05, wall_elapsed_s=wall + 2, now_s=3002.0)
        self.assertIsNone(state["monitor_good_streak_started_at"])
        self.assertEqual(state["monitor_phase"], "WARN")


# ---------------- size 解析 ----------------


class TestParseFfmpegSize(unittest.TestCase):
    def test_kib(self):
        # ffmpeg 约定：kB = 1000，KiB = 1024
        self.assertEqual(_parse_ffmpeg_size_to_bytes("1024kB"), 1024 * 1000)
        self.assertEqual(_parse_ffmpeg_size_to_bytes("1024KiB"), 1024 * 1024)

    def test_mib(self):
        self.assertEqual(_parse_ffmpeg_size_to_bytes("2MiB"), 2 * 1024 * 1024)

    def test_na(self):
        self.assertIsNone(_parse_ffmpeg_size_to_bytes("N/A"))

    def test_empty(self):
        self.assertIsNone(_parse_ffmpeg_size_to_bytes(""))


# ---------------- bitrate 解析 ----------------


class TestParseProfileBitrate(unittest.TestCase):
    def test_megabits(self):
        self.assertEqual(_parse_profile_bitrate_to_bps("8M"), 8_000_000)

    def test_kilobits(self):
        self.assertEqual(_parse_profile_bitrate_to_bps("3000K"), 3_000_000)

    def test_raw_bits(self):
        self.assertEqual(_parse_profile_bitrate_to_bps("5000000"), 5_000_000)

    def test_garbage(self):
        self.assertIsNone(_parse_profile_bitrate_to_bps("not-a-bitrate"))


# ---------------- preflight ----------------


class _FakeDurationResolver:
    def __init__(self, mapping):
        self._mapping = mapping

    def duration_for_file(self, p):
        return self._mapping.get(p)


class TestPreflightShouldSkipNvenc(unittest.TestCase):
    """preflight 决策：低码率输入应跳过 NVENC。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cr_preflight_")
        self.addCleanup(self._cleanup)
        self.files = []
        self.durations = {}
        for i in range(3):
            p = os.path.join(self.tmpdir, f"clip_{i}.bin")
            with open(p, "wb") as fh:
                # 1 MB per file
                fh.write(b"\0" * (1024 * 1024))
            self.files.append(p)
            self.durations[p] = 60.0  # 60s, so 1MB/60s ≈ 139.8 kbps per file

    def _cleanup(self):
        for p in self.files:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def test_low_input_bitrate_triggers_skip(self):
        # target 8M, input ≈140 kbps 远低于阈值 → 必须跳过
        profile = {"bitrate": "8M"}
        reason = preflight_should_skip_nvenc(
            self.files, profile, _FakeDurationResolver(self.durations),
        )
        self.assertIsNotNone(reason)
        self.assertIn("input avg bitrate", reason or "")

    def test_high_input_bitrate_proceeds(self):
        # 把 duration 改到极小，让平均码率远超 target
        durations = {p: 0.1 for p in self.files}
        profile = {"bitrate": "8M"}
        reason = preflight_should_skip_nvenc(
            self.files, profile, _FakeDurationResolver(durations),
        )
        self.assertIsNone(reason)

    def test_missing_durations_does_not_skip(self):
        """duration 全拿不到 → 保守不跳过 (返回 None)。"""
        profile = {"bitrate": "8M"}
        reason = preflight_should_skip_nvenc(
            self.files, profile, _FakeDurationResolver({}),
        )
        self.assertIsNone(reason)

    def test_invalid_target_bitrate_does_not_skip(self):
        profile = {"bitrate": "garbage"}
        reason = preflight_should_skip_nvenc(
            self.files, profile, _FakeDurationResolver(self.durations),
        )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
