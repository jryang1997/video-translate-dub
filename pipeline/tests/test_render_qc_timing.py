#!/usr/bin/env python3
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import mix_render
import qc_dub_track


class RenderAndQCTimingTests(unittest.TestCase):
    def test_render_prefers_actual_placement(self):
        item = {"start": 1.0, "place_start": 2.5, "dur": 0.75}
        self.assertEqual(mix_render.effective_speech_interval(item), (2.5, 3.25))
        self.assertEqual(qc_dub_track.effective_speech_interval(item), (2.5, 3.25))

    def test_legacy_plan_falls_back_to_start(self):
        item = {"start": 1.0, "dur": 0.5}
        self.assertEqual(mix_render.effective_speech_interval(item), (1.0, 1.5))

    def test_render_uses_distinct_subtitle_files(self):
        source, dubbed = mix_render.subtitle_files("/tmp/subs")
        self.assertEqual(source, "/tmp/subs/bilingual.ass")
        self.assertEqual(dubbed, "/tmp/subs/bilingual_dub.ass")

    def test_qc_checks_audio_at_actual_placement(self):
        sr = 1000
        samples = np.zeros(3000, dtype=np.float32)
        samples[2000:2500] = 0.1
        segments = [{"id": 1}]
        plan = [{"id": 1, "start": 0.0, "place_start": 2.0, "dur": 0.5}]
        self.assertEqual(qc_dub_track.deterministic_problems(segments, plan, samples, sr), [])

    def test_silence_is_a_hard_failure(self):
        problems = qc_dub_track.deterministic_problems(
            [{"id": 1}], [{"id": 1, "start": 0.0, "place_start": 2.0, "dur": 0.5}],
            np.zeros(3000, dtype=np.float32), 1000,
        )
        self.assertTrue(problems)
        self.assertEqual(qc_dub_track.qc_exit_status(problems, []), 2)

    def test_asr_warning_is_non_blocking_status(self):
        self.assertEqual(qc_dub_track.qc_exit_status([], ["asr unavailable"]), 1)
        self.assertEqual(qc_dub_track.qc_exit_status([], []), 0)

    def test_asr_warning_threshold_is_configurable(self):
        self.assertTrue(qc_dub_track.asr_hit_is_warning(0.5, 0.6))
        self.assertFalse(qc_dub_track.asr_hit_is_warning(0.5, 0.4))


if __name__ == "__main__":
    unittest.main()
