"""Offline tests for stage 2's vectorized pricing and blend fit."""
import unittest

import numpy as np

from polyfin.stage1 import prob_above
from polyfin.stage2 import Stage2, fit_blend, logit, prob_vec


class TestStage2(unittest.TestCase):
    def test_prob_vec_matches_stage1_when_neutral(self):
        x, v = np.array([0.004, -0.01, 0.0]), np.array([1e-4, 4e-4, 1e-4])
        got = prob_vec(x, v, np.ones(3), gamma=0.0, b=1.0)
        want = [prob_above(a, b) for a, b in zip(x, v)]
        np.testing.assert_allclose(got, want, atol=1e-12)

    def test_sharpening_and_zero_variance(self):
        p1 = prob_vec(np.array([0.005]), np.array([1e-4]), np.ones(1), 0.0, 1.0)[0]
        p2 = prob_vec(np.array([0.005]), np.array([1e-4]), np.ones(1), 0.0, 1.5)[0]
        self.assertGreater(p2, p1)
        self.assertEqual(prob_vec(np.array([0.001]), np.array([0.0]), np.ones(1), 0, 1)[0], 1.0)

    def test_strike_b_only_strikes_far_out(self):
        m = Stage2.__new__(Stage2)
        m.params = {"gamma": 0.0, "b": 1.0, "sharpen_hours": 0.0, "strike_b": 1.5}
        m.features = lambda s, t, ex=None: (0.0, 0.01, 1e-4, 1.0)   # +1 sd above
        far, near = 1000 + 10 * 3600, 1000 + 3600
        st = lambda kind, tgt: type("S", (), {"kind": kind, "target_ts": tgt})()
        base = prob_vec(np.array([0.01]), np.array([1e-4]), np.array([1.0]), 0.0, 1.0)[0]
        self.assertGreater(m.prob(st("strikes", far), 1000), base + 0.01)     # sharpened
        self.assertAlmostEqual(m.prob(st("strikes", near), 1000), base)      # late: untouched
        self.assertAlmostEqual(m.prob(st("updown", far), 1000), base)        # up/down: untouched

    def test_empty_input(self):
        # the late-sharpening subset is empty when SHARPEN_HOURS = 0 (refit crashed on it)
        self.assertEqual(prob_vec(np.array([]), np.array([]), np.array([]), 0.0, 1.0).size, 0)

    def test_vol_regime_widens(self):
        args = (np.array([0.005]), np.array([1e-4]))
        calm = prob_vec(*args, np.array([1.0]), 1.0, 1.0)[0]
        wild = prob_vec(*args, np.array([4.0]), 1.0, 1.0)[0]
        self.assertLess(wild, calm)

    def test_fit_blend_recovers_weights(self):
        rng = np.random.default_rng(0)
        a, b = rng.uniform(0.05, 0.95, 20000), rng.uniform(0.05, 0.95, 20000)
        p = 1 / (1 + np.exp(-(0.7 * logit(a) + 0.4 * logit(b))))
        y = (rng.uniform(size=p.size) < p).astype(float)
        np.testing.assert_allclose(fit_blend(a, b, y), [0.7, 0.4], atol=0.05)


if __name__ == "__main__":
    unittest.main()
