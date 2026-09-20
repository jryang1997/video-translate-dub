#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import build_subs


class SubtitleTimingTests(unittest.TestCase):
    def setUp(self):
        self.segment = {
            "id": 7,
            "start": 1.0,
            "end": 1.4,
            "zh": "这是一段需要切成若干条但不能超出原始时间窗的中文文本。",
            "en": "This sentence must be split while staying inside its parent interval.",
        }

    def test_source_cues_remain_inside_parent_interval(self):
        cues = build_subs.build_cues([self.segment])
        self.assertGreater(len(cues), 1)
        self.assertTrue(all(1.0 <= cue["start"] <= cue["end"] <= 1.4 for cue in cues))
        self.assertAlmostEqual(cues[0]["start"], 1.0)
        self.assertAlmostEqual(cues[-1]["end"], 1.4)

    def test_dubbed_cues_follow_place_start_and_duration(self):
        timed = build_subs.apply_plan_timing(
            [self.segment], [{"id": 7, "start": 1.0, "place_start": 5.0, "dur": 1.2}]
        )
        cues = build_subs.build_cues(timed)
        self.assertAlmostEqual(cues[0]["start"], 5.0)
        self.assertAlmostEqual(cues[-1]["end"], 6.2)
        self.assertTrue(all(5.0 <= cue["start"] <= cue["end"] <= 6.2 for cue in cues))

    def test_plan_must_cover_every_segment(self):
        with self.assertRaises(ValueError):
            build_subs.apply_plan_timing([self.segment], [])

    def test_source_and_dubbed_outputs_are_separate(self):
        cues = build_subs.build_cues([self.segment])
        with tempfile.TemporaryDirectory() as outdir:
            source = build_subs.write_outputs(cues, outdir)
            dubbed = build_subs.write_outputs(cues, outdir, suffix="_dub")
            self.assertEqual(os.path.basename(source["bilingual_ass"]), "bilingual.ass")
            self.assertEqual(os.path.basename(dubbed["bilingual_ass"]), "bilingual_dub.ass")
            self.assertTrue(all(os.path.exists(path) for path in (*source.values(), *dubbed.values())))
            with open(source["cues"]) as f:
                self.assertEqual(len(json.load(f)), len(cues))

    def test_dub_outputs_preserve_source_files_and_avoid_banners(self):
        with tempfile.TemporaryDirectory() as run:
            translate = os.path.join(run, "04_translate")
            tts = os.path.join(run, "05_tts")
            os.makedirs(translate)
            os.makedirs(tts)
            with open(os.path.join(translate, "segments.zh.json"), "w") as f:
                json.dump({"segments": [self.segment]}, f)
            with open(os.path.join(tts, "plan.json"), "w") as f:
                json.dump({"plan": [{"id": 7, "start": 1.0, "place_start": 5.0, "dur": 1.2}]}, f)
            sentinels = ("zh.ass", "bilingual.ass", "zh.srt", "bilingual.srt")
            for name in sentinels:
                with open(os.path.join(translate, name), "w") as f:
                    f.write("SOURCE-SENTINEL")
            # 词卡窗口 [5.4, 6.0) 与配音 cue (5.0-6.2) 重叠 → 避让必须生效
            os.makedirs(os.path.join(run, "06_mix"))
            with open(os.path.join(run, "06_mix", "banner_filter.txt"), "w") as f:
                f.write("tag: card1 patch 4.0 5.2 text 5.4 6.0\n")

            plan = json.load(open(os.path.join(tts, "plan.json")))["plan"]
            timed = build_subs.apply_plan_timing(json.load(
                open(os.path.join(translate, "segments.zh.json")))["segments"], plan)
            cues = build_subs.build_cues(timed)
            outputs = build_subs.write_outputs(cues, translate, suffix="_dub")
            localized = build_subs.avoid_banners(outputs, run)

            for name in sentinels:
                with open(os.path.join(translate, name)) as f:
                    self.assertEqual(f.read(), "SOURCE-SENTINEL")
            self.assertTrue(all(os.path.exists(path) for path in outputs.values()))
            self.assertEqual(os.path.basename(localized), "zh_dub_localized.ass")
            # 与词卡重叠的行被抬高：localized 版 MarginV=270，双语版 MarginV=330
            with open(localized) as f:
                self.assertTrue(any(",270," in line for line in f if line.startswith("Dialogue:")))
            with open(outputs["bilingual_ass"]) as f:
                self.assertTrue(any(",330," in line for line in f if line.startswith("Dialogue:")))

    def test_avoid_banners_noops_without_filter_file(self):
        cues = build_subs.build_cues([self.segment])
        with tempfile.TemporaryDirectory() as run:
            translate = os.path.join(run, "04_translate")
            os.makedirs(translate)
            outputs = build_subs.write_outputs(cues, translate, suffix="_dub")
            localized = build_subs.avoid_banners(outputs, run)
            self.assertEqual(localized, outputs["zh_ass"].replace("_dub.ass", "_dub_localized.ass"))
            with open(outputs["bilingual_ass"]) as f:
                self.assertFalse(any(",330," in line for line in f if line.startswith("Dialogue:")))

    def test_incomplete_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            build_subs.apply_plan_timing([self.segment], [])


if __name__ == "__main__":
    unittest.main()
