"""Offline tests for stage 2's vectorized pricing and blend fit."""
import unittest

import numpy as np

from polyfin.stage1 import prob_above
from polyfin.stage2 import fit_blend, logit, prob_vec


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
