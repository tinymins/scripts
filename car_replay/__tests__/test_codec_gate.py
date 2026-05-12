"""codec-aware preflight (Phase 2 根因修复) 单元测试。

覆盖：
1. `decide_compression()` 决策矩阵：hevc 边界、非 hevc 派生 profile、混合、unknown
2. `derive_h264_nvenc_profile()` 数学：target/maxrate/bufsize 派生
3. x265 路径的 encoder 分支
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from car_replay.compress import PreflightDecision, decide_compression
from car_replay.config import (
    DEFAULT_PROFILE,
    H264_TARGET_RATIO,
    PREFLIGHT_BITRATE_MARGIN,
    derive_h264_nvenc_profile,
)


# DEFAULT_PROFILE: bitrate=5M, maxrate=8M, bufsize=10M
DEF = DEFAULT_PROFILE
DEF_TARGET_BPS = 5_000_000
DEF_MAXRATE_BPS = 8_000_000


class TestDeriveH264NvencProfile(unittest.TestCase):
    """derive_h264_nvenc_profile 数学验证。"""

    def test_low_input_bitrate_clamped_below_default_target(self):
        # input 0.77M, ratio 0.65 → target 0.5M, maxrate 0.77M, bufsize max(1M, 0.77M)=1M
        d = derive_h264_nvenc_profile(DEF, 770_000)
        self.assertEqual(int(d["bitrate"]), int(770_000 * H264_TARGET_RATIO))
        self.assertEqual(int(d["maxrate"]), 770_000)
        self.assertEqual(int(d["bufsize"]), max(int(d["bitrate"]) * 2, int(d["maxrate"])))

    def test_high_input_bitrate_clamped_to_default(self):
        # input 20M, ratio 0.65 → target wants 13M but capped at 5M
        d = derive_h264_nvenc_profile(DEF, 20_000_000)
        self.assertEqual(int(d["bitrate"]), DEF_TARGET_BPS)
        self.assertEqual(int(d["maxrate"]), DEF_MAXRATE_BPS)
        # bufsize = max(target*2, maxrate) = max(10M, 8M) = 10M
        self.assertEqual(int(d["bufsize"]), max(DEF_TARGET_BPS * 2, DEF_MAXRATE_BPS))

    def test_inherits_other_fields(self):
        d = derive_h264_nvenc_profile(DEF, 1_000_000)
        # cq / preset 等字段必须继承
        self.assertEqual(d.get("cq"), DEF.get("cq"))
        self.assertEqual(d.get("preset"), DEF.get("preset"))

    def test_does_not_mutate_default_profile(self):
        snapshot = DEF.copy()
        _ = derive_h264_nvenc_profile(DEF, 1_000_000)
        self.assertEqual(DEF, snapshot)


class TestDecideCompressionMixedAndUnknown(unittest.TestCase):
    """混合 / unknown metadata 始终走 copy 且不计 breaker。"""

    def test_mixed_codec_group_copy_not_counted(self):
        d = decide_compression(DEF, group_codec="mixed", group_bps=2_000_000)
        self.assertEqual(d.action, "copy")
        self.assertFalse(d.count_as_negative)
        self.assertIn("mixed", d.reason.lower())

    def test_codec_unknown_copy(self):
        d = decide_compression(DEF, group_codec=None, group_bps=2_000_000)
        self.assertEqual(d.action, "copy")
        self.assertFalse(d.count_as_negative)

    def test_bitrate_unknown_copy(self):
        d = decide_compression(DEF, group_codec="hevc", group_bps=None)
        self.assertEqual(d.action, "copy")
        self.assertFalse(d.count_as_negative)

    def test_bitrate_zero_copy(self):
        d = decide_compression(DEF, group_codec="hevc", group_bps=0)
        self.assertEqual(d.action, "copy")
        self.assertFalse(d.count_as_negative)


class TestDecideCompressionHevc(unittest.TestCase):
    """hevc 输入 → 按 PREFLIGHT_BITRATE_MARGIN 边界。"""

    def test_hevc_low_bitrate_skip(self):
        d = decide_compression(DEF, group_codec="hevc", group_bps=700_000)
        self.assertEqual(d.action, "copy")
        self.assertFalse(d.count_as_negative)
        self.assertIn("hevc", d.reason.lower())

    def test_hevc_at_margin_boundary_inclusive_skip(self):
        # 5M × 1.1 = 5.5M，恰好 5_500_000 应 skip（≤）
        boundary = int(DEF_TARGET_BPS * PREFLIGHT_BITRATE_MARGIN)
        d = decide_compression(DEF, group_codec="hevc", group_bps=boundary)
        self.assertEqual(d.action, "copy")

    def test_hevc_just_above_margin_encode(self):
        boundary = int(DEF_TARGET_BPS * PREFLIGHT_BITRATE_MARGIN) + 1
        d = decide_compression(DEF, group_codec="hevc", group_bps=boundary)
        self.assertEqual(d.action, "encode")
        self.assertEqual(d.profile, DEF)
        self.assertFalse(d.count_as_negative)

    def test_hevc_far_above_margin_encode_dashcam_case(self):
        # 行车记录仪 hevc 30M vs DEFAULT 5M → 必须 encode
        d = decide_compression(DEF, group_codec="hevc", group_bps=30_000_000)
        self.assertEqual(d.action, "encode")


class TestDecideCompressionNonHevcNvenc(unittest.TestCase):
    """非 hevc 输入 + nvenc → 永远 encode + 派生 profile。"""

    def test_h264_low_bitrate_encodes_with_derived(self):
        d = decide_compression(DEF, group_codec="h264", group_bps=770_000, encoder="nvenc")
        self.assertEqual(d.action, "encode")
        self.assertIsNotNone(d.profile)
        # 派生：target 应该 ≤ input × 0.65（不会被 clamp 到 default 因为 input 低）
        self.assertLessEqual(int(d.profile["bitrate"]), int(770_000 * H264_TARGET_RATIO))
        # maxrate 必须 ≤ input（硬约束）
        self.assertLessEqual(int(d.profile["maxrate"]), 770_000)

    def test_h264_high_bitrate_clamped_to_default(self):
        d = decide_compression(DEF, group_codec="h264", group_bps=20_000_000, encoder="nvenc")
        self.assertEqual(d.action, "encode")
        self.assertEqual(int(d.profile["bitrate"]), DEF_TARGET_BPS)
        self.assertEqual(int(d.profile["maxrate"]), DEF_MAXRATE_BPS)

    def test_unknown_codec_treated_as_non_hevc_encodes(self):
        # 例如 av1 输入：当前规则视为非 hevc → encode（用户："只要不是目标编码 hevc 都过一遍"）
        d = decide_compression(DEF, group_codec="av1", group_bps=2_000_000, encoder="nvenc")
        self.assertEqual(d.action, "encode")


class TestDecideCompressionX265(unittest.TestCase):
    """x265-veryslow 编码器路径。"""

    def test_hevc_low_bitrate_still_skip_with_x265(self):
        # x265 路径下 hevc 仍按 margin skip（plan.md 规定）
        d = decide_compression(DEF, group_codec="hevc", group_bps=700_000, encoder="x265-veryslow")
        self.assertEqual(d.action, "copy")
        self.assertFalse(d.count_as_negative)

    def test_h264_x265_encode_uses_default_profile_no_derive(self):
        # x265 用 CRF，不需要派生 bitrate/maxrate，profile 直接传 default
        d = decide_compression(DEF, group_codec="h264", group_bps=770_000, encoder="x265-veryslow")
        self.assertEqual(d.action, "encode")
        # 不派生：profile.bitrate 应该是 default 的 "5M" 字面值
        self.assertEqual(d.profile.get("bitrate"), DEF.get("bitrate"))


class TestDecideCompressionReturnType(unittest.TestCase):
    def test_returns_preflight_decision(self):
        d = decide_compression(DEF, group_codec="hevc", group_bps=1_000_000)
        self.assertIsInstance(d, PreflightDecision)
        self.assertIn(d.action, {"encode", "copy"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
