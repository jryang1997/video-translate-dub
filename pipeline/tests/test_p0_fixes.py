"""核心关键修复回归测试套件

覆盖三项:
  ① compress_pauses 产出合法 WAV（wave/ffprobe 双读一致、保留 target 静音、语音完整）
 ② precheck 与 assemble_track 完整 placement 逐段一致（共享 cursor contract）
  ③ 段级 provenance: plan item 携带 synth_fingerprint，与 cache manifest 一致

用法: index-tts/.venv/bin/python pipeline/tests/test_p0_fixes.py
"""
import importlib.util, json, os, subprocess, sys, tempfile, unittest, wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("FFMPEG", "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
os.environ.setdefault("FFPROBE", "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe")

SPEC = importlib.util.spec_from_file_location(
    "tts_mod", ROOT / "pipeline" / "tts_dub_indextts.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "pipeline" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 缓存与落轨已各自成模块（审计阶段2），测试直接对准其归属
cache = load("tts_cache")
layout = load("layout")


def make_wav(path, sr=44100, ch=2, sil=0.6):
    mono = np.concatenate([np.sin(np.linspace(0, 100, int(0.5 * sr))) * 0.3,
                           np.zeros(int(sil * sr)),
                           np.sin(np.linspace(0, 100, int(0.5 * sr))) * 0.3])
    pcm = (np.clip(np.stack([mono] * ch, axis=1), -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "w") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return pcm


class CompressPausesTests(unittest.TestCase):
    def test_output_is_valid_wav_with_target_silence(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "seg.wav")
            make_wav(p)
            cut = m.compress_pauses(p)
            self.assertEqual(1, cut)
            # wave 模块读出的时长 == ffprobe 读出的时长（容器自洽）
            with wave.open(p) as w:
                dur_wave = w.getnframes() / w.getframerate()
                sr, ch = w.getframerate(), w.getnchannels()
            r = subprocess.run([os.environ["FFPROBE"], "-v", "error",
                                "-show_entries", "format=duration", "-of", "csv=p=0", p],
                               capture_output=True, text=True)
            self.assertAlmostEqual(dur_wave, float(r.stdout), delta=0.01)
            # 压到 target(0.26s) 而非全删: 总长 ≈ 0.5+0.26+0.5-交叉淡化
            self.assertAlmostEqual(dur_wave, 1.24, delta=0.05)
            # 语音内容完整: 前后两段正弦都在
            r2 = subprocess.run([os.environ["FFMPEG"], "-v", "error", "-i", p,
                                 "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                                capture_output=True)
            x = np.frombuffer(r2.stdout, dtype=np.float32)
            self.assertGreater(np.abs(x[int(0.1 * 16000):int(0.4 * 16000)]).max(), 0.2)
            self.assertGreater(np.abs(x[int(0.7 * 16000):int(1.0 * 16000)]).max(), 0.2)
            self.assertEqual((sr, ch), (44100, 2))


class SharedShiftTests(unittest.TestCase):
    def test_precheck_matches_assemble_per_segment(self):
        """完整 placement 必须包含正式落轨的 cursor 推移。"""
        segs = [{"id": 1, "start": 0.0, "end": 0.5},
                {"id": 2, "start": 1.0, "end": 1.5},
                {"id": 3, "start": 2.0, "end": 2.5}]
        durations = {1: 1.6, 2: 0.5, 3: 0.5}
        k_use, placements = layout.compute_placements(
            segs, durations, 1.25, 0.5, 0.22, 6.0)
        self.assertGreater(k_use, 0.0)
        cursor = 0.0
        pushed = False
        for sid in (1, 2, 3):
            item = placements[sid]
            expected = max(cursor, item["base_start"])
            self.assertAlmostEqual(item["place_start"], expected, places=6)
            pushed |= item["place_start"] > item["base_start"] + 1e-6
            cursor = item["place_start"] + durations[sid] + 0.22
        self.assertTrue(pushed)

    def test_shifts_zero_when_no_slack(self):
        segs = [{"id": 1, "start": 0.0, "end": 1.0},
                {"id": 2, "start": 1.0, "end": 2.0}]
        k_use, placements = layout.compute_placements(
            segs, {1: 2.0, 2: 2.0}, 1.25, 0.5, 0.22, 4.0)
        self.assertEqual(0.0, k_use)
        self.assertEqual(0.0, placements[1]["place_start"])
        self.assertEqual(2.22, placements[2]["place_start"])


class ProvenanceTests(unittest.TestCase):
    def test_plan_item_carries_fingerprint_matching_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            seg = {"id": 3, "start": 2.0, "end": 3.0, "zh": "测试文本", "spk": 0}
            ref = os.path.join(td, "ref.wav")
            Path(ref).write_bytes(b"reference" * 200)
            payload = cache.cache_payload(seg, 3.5, 3.66, 1.66, ref,
                                      SimpleNamespace(lang="zh", interval_silence=200,
                                                      max_text_tokens=120, emo_alpha=1.0,
                                                      duration_factor=1.0, pause_retake=True),
                                      {}, [0.1, 0, 0, 0, 0, 0, 0, 0.9],
                                      {"engine": "IndexTTS-2.5", "config_sha256": "abc"})
            fp = cache.cache_fingerprint(payload)
            manifest = {"schema": cache.CACHE_SCHEMA, "fingerprint": fp, "inputs": payload}
            base_path = os.path.join(td, "cache", "seg_0003.base.wav")
            mpath = os.path.join(td, "cache", "seg_0003.json")
            cache.publish_cache(ref, base_path, mpath, payload)
            plan = [{"id": 3}]
            cache.backfill_plan_provenance(plan, td)
            self.assertEqual(cache.param_fingerprint(payload),
                             plan[0]["synth_fingerprint"])
            with open(mpath) as f:
                self.assertEqual(fp, json.load(f)["fingerprint"])


if __name__ == "__main__":
    unittest.main()
