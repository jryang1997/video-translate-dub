"""Reliability checks for the IndexTTS cache and partial-preview contract."""
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "tts_mod", ROOT / "pipeline" / "tts_dub_indextts.py")
tts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tts)


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "pipeline" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 缓存与落轨已各自成模块（审计阶段2），测试直接对准其归属
cache = load("tts_cache")
layout = load("layout")


def args(**overrides):
    values = dict(lang="zh", interval_silence=200, max_text_tokens=120,
                  emo_alpha=1.0, duration_factor=1.0, pause_retake=True)
    values.update(overrides)
    return SimpleNamespace(**values)


class CacheContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ref = self.root / "ref.wav"
        self.ref.write_bytes(b"reference-a" * 200)
        self.seg = {"id": 5, "start": 4.0, "end": 5.0,
                    "zh": "测试文本", "spk": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, **arg_overrides):
        return cache.cache_payload(
            self.seg, 5.5, 5.66, 1.66, str(self.ref), args(**arg_overrides),
            {"top_k": 1}, [0.1, 0, 0, 0, 0, 0, 0, 0.9],
            {"engine": "IndexTTS-2.5", "config_sha256": "abc"})

    def test_fingerprint_is_stable_for_identical_inputs(self):
        self.assertEqual(cache.cache_fingerprint(self.payload()),
                         cache.cache_fingerprint(self.payload()))

    def test_fingerprint_changes_with_text_reference_and_parameters(self):
        baseline = cache.cache_fingerprint(self.payload())
        changed_text = dict(self.seg, zh="另一句文本")
        text_payload = cache.cache_payload(
            changed_text, 5.5, 5.66, 1.66, str(self.ref), args(), {"top_k": 1},
            [0.1, 0, 0, 0, 0, 0, 0, 0.9],
            {"engine": "IndexTTS-2.5", "config_sha256": "abc"})
        self.assertNotEqual(baseline, cache.cache_fingerprint(text_payload))

        self.ref.write_bytes(b"reference-b" * 200)
        self.assertNotEqual(baseline, cache.cache_fingerprint(self.payload()))
        self.ref.write_bytes(b"reference-a" * 200)
        self.assertNotEqual(
            baseline,
            cache.cache_fingerprint(self.payload(duration_factor=0.9)))
        self.assertNotEqual(
            baseline,
            cache.cache_fingerprint(self.payload(pause_retake=False)))

    def test_working_file_can_be_restored_from_clean_base(self):
        base = self.root / "cache" / "seg_0005.base.wav"
        work = self.root / "seg_0005.wav"
        base.parent.mkdir()
        clean = b"RIFF" + b"clean-audio" * 200
        base.write_bytes(clean)
        work.write_bytes(b"mutated")
        cache.materialize_base(str(base), str(work))
        self.assertEqual(clean, work.read_bytes())

    def test_sparse_selection_uses_real_next_segment(self):
        full = [
            {"id": 5, "start": 4.0, "end": 5.0},
            {"id": 6, "start": 5.5, "end": 6.0},
            {"id": 10, "start": 12.0, "end": 13.0},
        ]
        nxt, _, _ = layout.segment_window(full, 0)
        self.assertEqual(5.5, nxt)

    def test_partial_run_leaves_formal_artifacts_untouched(self):
        plan_path = self.root / "plan.json"
        dub_path = self.root / "dub_track.wav"
        plan_path.write_text(json.dumps({"sentinel": True}))
        dub_path.write_bytes(b"existing full track")
        result = tts.finalize_run({}, [], str(self.root), SimpleNamespace(), {},
                                  partial=True)
        self.assertIsNone(result)
        self.assertEqual({"sentinel": True}, json.loads(plan_path.read_text()))
        self.assertEqual(b"existing full track", dub_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
