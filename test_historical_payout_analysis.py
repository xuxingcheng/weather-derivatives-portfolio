"""Historical scenario boundary and regression tests, with synthetic GHCN receipts."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import historical_payout_analysis as h
import weather_pipeline as w
from test_weather_pipeline import receipt


def fixture(year=2024, month=10, missing=None, flagged=None, reversed_day=None, low=100, high=200):
    lines = []
    for element, value in [('TMIN', low), ('TMAX', high)]:
        blocks = []
        for day in range(1, 32):
            v = -9999 if day == missing else (300 if day == reversed_day and element == 'TMIN' else value)
            flag = 'X' if day == flagged and element == 'TMAX' else ' '
            blocks.append(f'{v:5d} {flag}0')
        lines.append(f'{w.GHCN_ID}{year:04d}{month:02d}{element}' + ''.join(blocks))
    return '\n'.join(lines)


def daily(start='2024-10-01', end='2024-10-31'):
    return pd.DataFrame(dict(tmin_c=10., tmax_c=20., valid_day=True), index=pd.date_range(start, end))


class HistoricalTests(unittest.TestCase):
    def test_conversion_hdd_and_custom_base(self):
        values = pd.DataFrame({'tmean_c': [0, 15, (65-32)/1.8, 30, np.nan]})
        result = w.degree_days(values)
        np.testing.assert_allclose(result.tmean_f[:4], [32, 59, 65, 86])
        np.testing.assert_allclose(result.hdd65_f_days[:4], [33, 6, 0, 0])
        self.assertTrue(pd.isna(result.hdd65_f_days.iloc[4]))
        self.assertEqual(w.degree_days(values, base_f=60).hdd65_f_days.iloc[1], 1)
        config = h.Config(as_of='2025-01-01T12:00:00Z', start_year=2024, base_f=60)
        self.assertEqual(h.monthly_indices(daily(), config).iloc[0].monthly_hdd_f_days, 31)

    def test_missing_day_and_qc_exclusions(self):
        config = h.Config(as_of='2025-01-01T12:00:00Z', start_year=2024)
        for frame in (daily().iloc[:-1], daily().assign(valid_day=lambda d: d.index.day != 3)):
            row = h.monthly_indices(frame, config).iloc[0]
            self.assertEqual(row.valid_days, 30)
            self.assertFalse(row.eligible)
            self.assertTrue(pd.isna(row.monthly_hdd_f_days))
            self.assertIn('1 missing or QC-invalid days', row.exclusion_reason)
            self.assertTrue(row.invalid_dates.startswith('2024-10-'))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'test.dly'
            p.write_text(fixture(missing=3, flagged=4, reversed_day=5))
            d, _ = w.ghcn_daily(p, '2024-10-01', '2024-10-31')
            row = h.monthly_indices(d, config).iloc[0]
            self.assertEqual(row.valid_days, 28)
            self.assertEqual(row.invalid_dates, '2024-10-03;2024-10-04;2024-10-05')

    def test_payoff_and_average_individual_payoffs(self):
        np.testing.assert_array_equal(h.hypothetical_payout([90, 100, 110], 100), [0, 0, 1000])
        values = [0, 200]
        self.assertEqual(h.hypothetical_payout(values, 100).mean(), 5000)
        self.assertEqual(h.hypothetical_payout(np.mean(values), 100), 0)
        self.assertTrue(np.isnan(h.hypothetical_payout([np.nan], 100)[0]))

    def test_month_completion_fixed_standard_time(self):
        c = h.Config(as_of='2024-11-01T04:59:59Z', start_year=2024)
        self.assertFalse(h.monthly_indices(daily(), c).iloc[0].eligible)
        c = replace(c, as_of='2024-11-01T05:00:00Z')
        self.assertTrue(h.monthly_indices(daily(), c).iloc[0].eligible)
        feb = h.monthly_indices(daily('2024-02-01', '2024-02-29'), replace(c, month=2)).iloc[0]
        self.assertEqual(feb.expected_days, 29)
        self.assertEqual(feb.monthly_hdd_f_days, 29 * 6)

    def test_calendar_windows_no_backfill_or_other_months(self):
        c = h.Config(as_of='2026-12-01T12:00:00Z')
        d = daily('2000-01-01', '2026-12-31')
        d.loc['2020-10-02', 'valid_day'] = False
        years = h.monthly_indices(d, c)
        self.assertEqual(len(years), 27)
        self.assertEqual(set(years.month), {10})
        windows = {name: window for name, _, _, window in h.historical_windows(years, c)}
        self.assertEqual(len(windows['all_history']), 26)
        self.assertEqual(len(windows['recent_20_calendar_years']), 19)
        self.assertEqual(len(windows['recent_10_calendar_years']), 9)
        self.assertEqual(windows['recent_10_calendar_years'].year.min(), 2016)
        self.assertEqual(windows['recent_10_calendar_years'].year.max(), 2025)
        self.assertNotIn(2020, windows['recent_10_calendar_years'].year.tolist())

    def test_snapshot_asof_validation_and_same_strike(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'data/raw/ghcn'
            raw.mkdir(parents=True)
            # Filename order intentionally opposes receipt order.
            old = raw / 'z_old.dly'
            old.write_text(fixture(high=200) + '\n' + fixture(year=2023, high=100))
            receipt(old, '2025-01-01T00:00:00Z')
            new = raw / 'a_new.dly'
            new.write_text(fixture(high=220) + '\n' + fixture(year=2023, high=100))
            receipt(new, '2025-02-01T00:00:00Z')
            bad = raw / 'bad.dly'
            bad.write_text(fixture())
            receipt(bad, '2025-02-02T00:00:00Z')
            bad.write_text('corrupt')
            with patch.object(w, 'ROOT', root), patch.object(w, 'RAW', root / 'data/raw'):
                c = h.Config(as_of='2025-01-15T00:00:00Z', start_year=2023)
                a = h.analyze(c)
                self.assertEqual(a['manifest']['input_provenance']['raw_file'], 'data/raw/ghcn/z_old.dly')
                self.assertEqual(a['yearly'].iloc[1].monthly_hdd_f_days, 186)
                self.assertEqual(a['window_summary'].strike.nunique(), 1)
                self.assertEqual(a['manifest']['selected_strike'], a['yearly'].monthly_hdd_f_days.median())
                self.assertAlmostEqual(a['window_summary'].iloc[0].payout_mean,
                                       a['yearly'].payout_usd.mean())
                b = h.analyze(replace(c, as_of='2025-02-03T00:00:00Z', strike=200))
                self.assertTrue(b['manifest']['source_selection']['fallback'])
                self.assertEqual(b['manifest']['input_provenance']['raw_file'], 'data/raw/ghcn/a_new.dly')
                self.assertTrue((b['window_summary'].strike == 200).all())
                self.assertNotEqual(a['yearly'].iloc[1].monthly_hdd_f_days, b['yearly'].iloc[1].monthly_hdd_f_days)
                with self.assertRaisesRegex(ValueError, 'No successful'):
                    h.analyze(replace(c, as_of='2024-12-01T00:00:00Z'))
                h.write_results(a, root / 'output')
                self.assertTrue((root / 'output/run_manifest.json').exists())

    def test_empty_sample_and_invalid_parameters(self):
        for kwargs in ({'as_of': '2026-09-21'}, {'month': 13}, {'strike': np.nan},
                       {'dollars_per_degree_day': -1}, {'strike_count': 1}):
            with self.assertRaises(ValueError):
                h.Config(**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'incomplete.dly'
            p.write_text(fixture(missing=1))
            receipt(p, '2025-01-01T00:00:00Z')
            with patch.object(w, 'observation_candidates', return_value=[p]):
                with self.assertRaisesRegex(ValueError, 'No complete eligible'):
                    h.analyze(h.Config(as_of='2025-01-02T00:00:00Z', start_year=2024))


if __name__ == '__main__':
    unittest.main()
