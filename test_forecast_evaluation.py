"""Small synthetic fixtures test mechanics only; they never enter archive results."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import forecast_evaluation as ev


class EvaluationTests(unittest.TestCase):
    def observation(self, observed='2026-09-18T00:00Z', retrieved='2026-09-18T00:05Z', value=20):
        return dict(observation_time=ev.utc(observed), observation=value, upstream_receipt=ev.utc(observed),
                    observation_retrieval=ev.utc(retrieved), available_at=ev.utc(retrieved),
                    observation_source='fixture', quality_status='basic_screen_pass', observation_file='fixture',
                    observation_sha256='fixture')

    def test_matching_boundary_offset_and_missing(self):
        obs = pd.DataFrame([self.observation('2026-09-18T00:45Z'), self.observation('2026-09-18T02:44Z')])
        targets = pd.DataFrame({'target_time': pd.to_datetime(['2026-09-18T01:00Z', '2026-09-18T03:00Z'], utc=True)})
        matched = ev.match_hourly(targets, obs, 15)
        self.assertEqual(matched.iloc[0].time_offset_minutes, -15)
        self.assertTrue(pd.isna(matched.iloc[1].observation))

    def test_nearest_tie_earlier_and_qc_exclusion(self):
        obs = pd.DataFrame([self.observation('2026-09-18T00:50Z', value=10),
                            self.observation('2026-09-18T01:10Z', value=20),
                            self.observation('2026-09-18T01:00Z', value=30)])
        obs.loc[2, 'quality_status'] = 'temperature_out_of_range'
        targets = pd.DataFrame({'target_time': pd.to_datetime(['2026-09-18T01:00Z'], utc=True)})
        self.assertEqual(ev.match_hourly(targets, obs, 15).iloc[0].observation, 10)

    def test_observations_retrieved_after_cutoff_cannot_predict(self):
        obs = pd.DataFrame([self.observation(value=10), self.observation(retrieved='2026-09-19T00:00Z', value=30)])
        cutoff = ev.utc('2026-09-18T01:00Z')
        self.assertEqual(ev.persistence(obs, cutoff, 6).observation, 10)
        self.assertIsNone(ev.persistence(obs.iloc[1:], cutoff, 6))
        self.assertEqual(ev.observation_vintage(obs, ev.utc('2026-09-20T00:00Z')).iloc[0].observation, 30)
        self.assertIsNone(ev.persistence(obs, ev.utc('2026-09-18T12:00Z'), 6))

    def test_correction_qc_no_fallback(self):
        obs = pd.DataFrame([self.observation(), self.observation(retrieved='2026-09-18T00:10Z')])
        obs.loc[1, 'quality_status'] = 'raw_temperature_mismatch'
        self.assertIsNone(ev.persistence(obs, ev.utc('2026-09-18T01:00Z'), 6))

    def test_cutoff_and_stale(self):
        snapshots = pd.DataFrame([dict(model='nws', snapshot_id='a', retrieval_time=ev.utc('2026-09-18T00:00Z')),
                                  dict(model='nws', snapshot_id='b', retrieval_time=ev.utc('2026-09-18T06:01Z'))])
        s, reason = ev.select_snapshot(snapshots, 'nws', ev.utc('2026-09-18T06:00Z'), 24)
        self.assertEqual(s.snapshot_id, 'a')
        self.assertEqual(reason, '')
        self.assertEqual(ev.select_snapshot(snapshots, 'nws', ev.utc('2026-09-20T00:00Z'), 24)[1], 'stale_forecast')
        self.assertEqual(ev.select_snapshot(snapshots, 'nws', ev.utc('2026-09-17T23:00Z'), 24)[1], 'no_snapshot_by_cutoff')

    def test_duplicates_do_not_refresh_age_or_weight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = {'properties': {'periods': [dict(startTime='2026-09-18T12:00Z', endTime='2026-09-18T13:00Z', temperature=68, temperatureUnit='F')]}}
            for name in ['a', 'b']:
                folder = root / 'data/forecasts' / name
                folder.mkdir(parents=True)
                (folder / 'fixture_nws_hourly.json').write_text(json.dumps(raw))
            def metadata(path, source):
                return dict(retrieved_at_utc='2026-09-17T00:00Z' if path.parent.name == 'a' else '2026-09-18T00:00Z', sha256='fixture', url='fixture')
            with patch.object(ev.archive, 'validate_download', side_effect=metadata):
                _, receipts, snaps, _ = ev.load_forecasts(root, ev.Config())
            self.assertEqual(len(receipts), 2)
            self.assertEqual(len(snaps), 1)
            self.assertEqual(ev.select_snapshot(snaps, 'nws', ev.utc('2026-09-18T06:00Z'), 24)[1], 'stale_forecast')
            with patch.object(ev.archive, 'validate_download', side_effect=metadata):
                _, receipts, snaps, _ = ev.load_forecasts(root, ev.Config(as_of='2026-09-17T12:00Z'))
            self.assertEqual(len(receipts), 1)

    def test_dst_and_ghcn_fixed_standard_time(self):
        for date, civil_count in [('2026-03-08', 23), ('2026-11-01', 25)]:
            civil = ev.day_hours(date, 'America/New_York')
            self.assertEqual(len(civil), civil_count)
            self.assertEqual(civil.nunique(), civil_count)
            standard = ev.day_hours(date)
            self.assertEqual(len(standard), 24)
            self.assertEqual(standard[0].hour, 5)
        self.assertEqual(ev.day_hours('2026-07-01')[0].tz_convert('America/New_York').hour, 1)

    def member_rows(self, ids=(0, 1), forecasts=(0., 2.)):
        return pd.DataFrame([dict(model='test', snapshot_id='fixture', cutoff=ev.utc('2026-09-17T00:00Z'),
            target_time=ev.utc('2026-09-18T00:00Z'), quantity='hourly_temperature_c', member_id=i,
            forecast=x, observation=1., error=x-1, exclusion_reason='', target_date='2026-09-18',
            lead_hours=24., cutoff_lead_hours=24.) for i, x in zip(ids, forecasts)])

    def test_metric_formulas_and_complete_member_identity(self):
        s = ev.ensemble_metrics([0, 2], 1)
        self.assertAlmostEqual(s['crps'], 0.5)
        self.assertAlmostEqual(s['interval_width'], 1.6)
        self.assertEqual(s['interval_coverage'], 1)
        self.assertEqual(ev.ensemble_metrics([0, 2], 3)['interval_coverage'], 0)
        with patch.dict(ev.EXPECTED, {'test': {0, 1}}):
            scores = ev.summarize_scenarios(self.member_rows())
            self.assertTrue((scores.error == 0).all())
            for bad in [self.member_rows(ids=(0, 2)), self.member_rows(forecasts=(0., np.nan)),
                        self.member_rows(ids=(0, 0))]:
                scores = ev.summarize_scenarios(bad)
                self.assertTrue(scores.error.isna().all())
                self.assertTrue(scores.crps.isna().all())
                self.assertTrue(scores.exclusion_reason.str.contains('missing_members').all())
        good = pd.concat([scores.iloc[:1]] * 2, ignore_index=True)
        good['error'] = [1., -3.]
        m = ev.metrics(good).iloc[0]
        self.assertEqual(m.bias, -1)
        self.assertEqual(m.mae, 2)
        self.assertAlmostEqual(m.rmse, np.sqrt(5))

    def daily_fixture(self):
        return pd.DataFrame(dict(model='nws', snapshot_id='fixture', cutoff=ev.utc('2026-03-07T00:00Z'),
            member_id=0, target_time=ev.day_hours('2026-03-08'), forecast=np.arange(24.),
            retrieval_time=ev.utc('2026-03-06T23:00Z')))

    def test_complete_daily_proxies_and_partial_day(self):
        cfg = ev.Config(start='2026-03-01T00:00Z', as_of='2026-03-10T00:00Z')
        obs = pd.DataFrame([dict(target_date='2026-03-08', valid_day=True, timing_verified=True, tmin_c=0, tmax_c=24,
            observation_retrieval=ev.utc('2026-03-09T12:00Z'), observation_file='fixture', observation_sha256='fixture',
            TMAX_sflag='1', TMIN_sflag='1', TMAX_qflag='', TMIN_qflag='', TMAX_mflag='', TMIN_mflag='')])
        f = self.daily_fixture()
        daily = ev.daily_matches(f, obs, cfg).set_index('quantity')
        self.assertEqual(daily.loc['midpoint_proxy_c', 'error'], -0.5)
        self.assertAlmostEqual(daily.loc['hdd_proxy_f_days', 'error'], 0.9)
        bad = ev.daily_matches(f.iloc[:-1], obs, cfg)
        self.assertTrue(bad.error.isna().all())
        self.assertTrue(bad.exclusion_reason.str.contains('incomplete_day').all())
        obs['timing_verified'] = False
        self.assertTrue(ev.daily_matches(f, obs, cfg).exclusion_reason.str.contains('ambiguous_timing').all())

    def test_common_pairs_do_not_mix_cutoffs(self):
        rows = []
        for model in ev.MODELS:
            row = self.member_rows().iloc[0].to_dict()
            row.update(model=model, statistic='mean', crps=np.nan, interval_width=np.nan, interval_coverage=np.nan)
            rows.append(row)
        scores = pd.DataFrame(rows)
        self.assertEqual(len(ev.metrics(scores, common=True)), 3)
        scores.loc[2, 'cutoff'] += pd.Timedelta(hours=6)
        self.assertTrue(ev.metrics(scores, common=True).empty)

    def test_basic_quality_screen(self):
        self.assertEqual(ev.screen_metar(dict(temp=20., dewp=19., rawOb='KJFK 20/19')), 'basic_screen_pass')
        self.assertEqual(ev.screen_metar(dict(temp=20., dewp=21., rawOb='KJFK 20/21')), 'dewpoint_above_temperature')
        self.assertEqual(ev.screen_metar(dict(temp=20., rawOb='KJFK 25/19')), 'raw_temperature_mismatch')

    def test_config_requires_explicit_timezones(self):
        with self.assertRaises(ValueError):
            ev.Config(as_of='2026-09-21')
        with self.assertRaises(ValueError):
            ev.Config(tolerance_minutes=30)


if __name__ == '__main__':
    unittest.main()
