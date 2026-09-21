"""Synthetic chronological boundaries and independent calculation checks."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import baseline_backtest as b
import historical_payout_analysis as h


def daily():
    dates = pd.date_range('2000-01-01', '2025-12-31')
    temperatures = 12 + .1 * (dates.year - 2000) + 4 * np.sin(dates.day.to_numpy())
    return pd.DataFrame(dict(tmin_c=temperatures - 5, tmax_c=temperatures + 5, valid_day=True), index=dates)


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daily = daily()
        cls.config = b.Config(as_of='2026-01-01T00:00:00Z')
        cls.result = b.backtest(cls.daily, cls.config)

    def test_future_and_test_year_do_not_change_predictions(self):
        changed = self.daily.copy()
        changed.loc[changed.index.year >= 2015, ['tmin_c', 'tmax_c']] += 20
        other = b.backtest(changed, self.config)
        cols = ['method', 'prediction_year', 'predicted_mean', 'p10', 'p90', 'strike', 'predicted_average_payout_usd']
        a, c = self.result['scores'], other['scores']
        pd.testing.assert_frame_equal(a.loc[a.prediction_year <= 2015, cols], c.loc[c.prediction_year <= 2015, cols])
        fa, fc = self.result['folds'], other['folds']
        pd.testing.assert_frame_equal(fa[fa.prediction_year <= 2015], fc[fc.prediction_year <= 2015])
        for row in fa.itertuples():
            self.assertTrue(all(y < row.prediction_year for y in json.loads(row.training_years)))
            self.assertTrue(all(y < row.prediction_year for y in json.loads(row.strike_training_years)))

    def test_calendar_windows_and_missing_year_no_replacement(self):
        changed = self.daily.copy()
        changed.loc['2015-10-03', 'valid_day'] = False
        result = b.backtest(changed, self.config)
        folds = result['folds'].set_index(['method', 'prediction_year'])
        for method, first, count in [('recent_10', 2010, 9), ('recent_20', 2000, 19)]:
            fold = folds.loc[(method, 2020)]
            self.assertEqual(fold.status, 'skipped')
            self.assertEqual(fold.n_training_years, count)
            self.assertEqual(min(json.loads(fold.training_years)), first)
            self.assertEqual(json.loads(fold.excluded_years)[0]['year'], 2015)
        self.assertEqual(folds.loc[('expanding', 2020)].status, 'scored')
        self.assertEqual(folds.loc[('expanding', 2015)].status, 'skipped')

    def test_minimum_history_and_empty_comparisons(self):
        scores = self.result['scores']
        self.assertEqual(scores[scores.method == 'recent_20'].prediction_year.min(), 2020)
        self.assertEqual(scores[scores.method == 'recent_10'].prediction_year.min(), 2010)
        empty = b.backtest(self.daily, replace(self.config, min_training_years=30))
        self.assertTrue(empty['scores'].empty)
        self.assertTrue(empty['metrics_common'].n_test_years.eq(0).all())
        self.assertTrue(empty['metrics_common'].mae.isna().all())
        for value in [0, 1, True, 2.5]:
            with self.assertRaises(ValueError):
                b.Config(min_training_years=value)

    def test_training_median_and_shared_fixed_strike(self):
        yearly = self.result['yearly']
        for year, group in self.result['scores'].groupby('prediction_year'):
            expected = yearly.loc[yearly.eligible & (yearly.year < year), 'monthly_hdd_f_days'].median()
            self.assertTrue(group.strike.eq(expected).all())
        fixed = b.backtest(self.daily, replace(self.config, strike=123.))
        self.assertTrue(fixed['scores'].strike.eq(123).all())

    def test_trend_temperature_shift_precedes_nonlinear_hdd(self):
        dates = pd.to_datetime([f'{year}-10-{day:02d}' for year in (2000, 2001) for day in range(1, 32)])
        f = pd.DataFrame({'tmean_c': [15.] * 15 + [20.] * 16 + [16.] * 15 + [21.] * 16}, index=dates)
        original = f.copy(deep=True)
        values, shifts, params = b.trend_scenarios(f, [2000, 2001], 2002)
        self.assertAlmostEqual(params['trend_slope_c_per_year'], 1.)
        np.testing.assert_allclose(shifts, [2., 1.])
        # Each adjusted sequence is 15 days at 62.6F and 16 days at 71.6F.
        np.testing.assert_allclose(values, [36., 36.], atol=1e-10)
        self.assertNotAlmostEqual(values[0], max(15 * 6 - 31 * 3.6, 0))
        pd.testing.assert_frame_equal(f, original)
        with self.assertRaises(ValueError):
            b.trend_scenarios(f.iloc[1:], [2000, 2001], 2002)

    def test_scenario_payoffs_and_empirical_scores(self):
        scores = self.result['scores']
        for row in scores.itertuples():
            f = self.result['scenarios']
            f = f[(f.method == row.method) & (f.prediction_year == row.prediction_year)]
            expected = 100 * np.maximum(f.monthly_hdd_f_days.to_numpy() - row.strike, 0)
            np.testing.assert_allclose(f.payout_usd, expected)
            self.assertAlmostEqual(row.predicted_average_payout_usd, expected.mean())
            self.assertAlmostEqual(row.positive_payout_frequency, (expected > 0).mean())
            self.assertAlmostEqual(row.realized_payout_usd, 100 * max(row.realized_hdd - row.strike, 0))
            self.assertAlmostEqual(row.payout_error_usd, expected.mean() - row.realized_payout_usd)
            self.assertAlmostEqual(f.weight.sum(), 1)
        metrics = b.ensemble_metrics([0, 200], 100)
        self.assertEqual(metrics['crps'], 50)
        self.assertEqual(metrics['interval_width'], 160)
        self.assertEqual(metrics['interval_coverage'], 1)
        self.assertEqual(h.hypothetical_payout([0, 200], 100).mean(), 5000)

    def test_common_years_are_intersection_including_empty_method(self):
        scores = self.result['scores'].copy()
        scores = scores[~((scores.method == 'recent_10') & (scores.prediction_year == 2022))]
        comparison = b.summarize(scores, b.METHODS, common=True)
        self.assertTrue(comparison.n_test_years.eq(5).all())
        self.assertEqual(comparison.test_years.nunique(), 1)
        self.assertNotIn('2022', comparison.test_years.iloc[0])
        row = comparison[comparison.method == 'expanding'].iloc[0]
        errors = scores.loc[(scores.method == 'expanding') & scores.prediction_year.isin([2020, 2021, 2023, 2024, 2025]), 'error']
        self.assertAlmostEqual(row.mae, errors.abs().mean())
        empty = b.summarize(scores, ['expanding', 'absent'], common=True)
        self.assertTrue(empty.n_test_years.eq(0).all())

    def test_offline_run_provenance_and_reproducible_exports(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Network prohibited')):
            result = b.analyze()
        self.assertEqual(result['manifest']['input_provenance']['station'], 'USW00094789')
        self.assertEqual(len(result['manifest']['input_provenance']['sha256']), 64)
        with tempfile.TemporaryDirectory() as tmp:
            h.write_results(result, tmp)
            first = {p.name: p.read_bytes() for p in Path(tmp).iterdir()}
            h.write_results(result, tmp)
            self.assertEqual(first, {p.name: p.read_bytes() for p in Path(tmp).iterdir()})


if __name__ == '__main__':
    unittest.main()
