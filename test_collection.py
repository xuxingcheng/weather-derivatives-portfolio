"""Regression tests use isolated raw archives with real response metadata."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import pandas as pd
import requests
import weather_pipeline as w
from test_weather_pipeline import receipt, ghcn_payload
import test_weather_pipeline as fixtures


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(w, 'ROOT', self.root), patch.object(w, 'RAW', self.root/'data/raw'),
                        patch.object(w, 'OUT', self.root/'data/processed')]
        for p in self.patches: p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches: self.addCleanup(p.stop)

    def raw(self, name, body, timestamp='2026-09-17T21:00:00Z', status=200):
        p = self.root/'data'/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body if isinstance(body, str) else json.dumps(body))
        return receipt(p, timestamp, status)

    def metar(self, temp=20):
        return [{'icaoId':'KJFK','obsTime':1789678260,'receiptTime':'2026-09-17T20:54:00Z','temp':temp}]

    def test_failed_then_successful_then_unsuccessful_retries(self):
        old = self.raw('raw/ghcn/old.dly', ghcn_payload())
        bad = self.raw('raw/ghcn/bad_failed.dly', '<html>503</html>', '2026-09-18T21:00:00Z',503)
        selected, info = w.select_ghcn()
        self.assertEqual(selected,old); self.assertTrue(info['fallback'])
        self.assertGreater(info['age_hours'],0)
        good = self.raw('raw/ghcn/good.dly', ghcn_payload(210), '2026-09-18T22:00:00Z')
        self.assertEqual(w.select_ghcn()[0],good)
        self.assertFalse(w.select_ghcn()[1]['fallback'])
        self.raw('raw/ghcn/later.dly','<html>HTTP 200 error</html>', '2026-09-18T23:00:00Z')
        self.assertEqual(w.select_ghcn()[0],good)
        self.assertTrue(w.select_ghcn()[1]['fallback'])
        self.assertEqual(w.select_ghcn(cutoff='2026-09-17T23:00:00Z')[0],old)
        self.assertTrue(bad.exists())

    def test_metadata_status_hash_and_structure_required(self):
        p=self.raw('raw/ghcn/good.dly',ghcn_payload())
        w.validate_download(p,'ghcn')
        p.write_text(ghcn_payload(250))
        with self.assertRaisesRegex(ValueError,'checksum'): w.validate_download(p,'ghcn')
        receipt(p,status=503)
        with self.assertRaisesRegex(ValueError,'HTTP'): w.validate_download(p,'ghcn')
        receipt(p);p.with_suffix('.dly.meta.json').unlink()
        with self.assertRaises(FileNotFoundError):w.validate_download(p,'ghcn')

    def test_metar_failed_downloads_excluded_and_cutoff_provenance(self):
        good=self.raw('raw/metar/a_KJFK.json',self.metar())
        self.raw('raw/metar/b_KJFK_failed.json',self.metar(99), '2026-09-18T21:00:00Z',503)
        self.raw('forecasts/run/c_metar.json',{'error':True}, '2026-09-18T22:00:00Z')
        newer=self.raw('raw/metar/d_KJFK.json',self.metar(21), '2026-09-18T23:00:00Z')
        paths,rejected=w.valid_observation_paths('metar')
        self.assertEqual(paths,[good,newer]);self.assertEqual(len(rejected),2)
        _,obs=w.metar_daily(paths)
        self.assertEqual(obs.temp.iloc[0],21)
        self.assertEqual(obs.raw_file.iloc[0],w.relative_path(newer))
        _,obs=w.metar_daily(paths,cutoff='2026-09-18T00:00:00Z')
        self.assertEqual(obs.temp.iloc[0],20)
        self.assertEqual(obs.retrieved_at_utc.iloc[0],'2026-09-17T21:00:00Z')

    def test_http_retry_receipts_success_and_exhaustion(self):
        fail=Mock(ok=False,status_code=503,content=b'error',headers={})
        fail.raise_for_status.side_effect=requests.HTTPError('503')
        success=Mock(ok=True,status_code=200,content=json.dumps(self.metar()).encode(),headers={})
        success.raise_for_status.return_value=None
        with patch.object(w.requests,'get',side_effect=[fail,success]),patch.object(w.time,'sleep'):
            path=w.fetch('https://test',w.RAW/'metar','KJFK')
        w.validate_download(path,'metar')
        with patch.object(w.requests,'get',return_value=fail),patch.object(w.time,'sleep'):
            with self.assertRaises(requests.HTTPError): w.fetch('https://test',w.RAW/'metar','KJFK')
        valid,rejected=w.valid_observation_paths('metar')
        self.assertEqual(valid,[path]); self.assertEqual(len(rejected),4)

    def test_daily_due_and_revision_reprocessing(self):
        self.raw('raw/ghcn/old.dly',ghcn_payload())
        self.raw('raw/metar/a_KJFK.json',self.metar())
        with patch.object(w,'now',return_value=pd.Timestamp('2026-09-18T20:59:59Z').to_pydatetime()):
            self.assertFalse(w.ghcn_due())
        with patch.object(w,'now',return_value=pd.Timestamp('2026-09-18T21:00:00Z').to_pydatetime()):
            self.assertTrue(w.ghcn_due())
        before,_,_=w.build()
        self.raw('raw/ghcn/new.dly',ghcn_payload(220),'2026-09-18T22:00:00Z')
        after,_,_=w.build()
        self.assertEqual(before.loc['2024-02-01','tmean_c'],15)
        self.assertEqual(after.loc['2024-02-01','tmean_c'],16)
        self.assertEqual(len(list((w.RAW/'ghcn').glob('*.dly'))),2)

    def test_refresh_and_build_isolate_source_failures(self):
        def fetch(url,directory,stem,extension='json'):
            if extension=='dly': raise requests.ConnectionError('GHCN unavailable')
            return self.raw('raw/metar/a_KJFK.json',self.metar())
        with patch.object(w,'fetch',side_effect=fetch):
            with self.assertRaises(RuntimeError):w.refresh_observations()
        manifest=json.loads(next((w.RAW/'refreshes').glob('*.json')).read_text())
        self.assertEqual(len(manifest['products']),6)
        with self.assertRaisesRegex(RuntimeError,'ghcn'):w.build()
        self.assertTrue((w.OUT/'metar_observations.csv').exists())

    def test_health_unchanged_failure_grace_and_transport_error(self):
        fixture=fixtures.EnsembleTests().fixture()
        self.raw('forecasts/a/a_ensemble_gfs025.json',fixture)
        self.raw('forecasts/b/b_ensemble_gfs025.json',fixture,'2026-09-18T03:00:00Z')
        report=w.health_report(at='2026-09-18T10:59:00Z')['ensemble_gfs025']
        self.assertFalse(report['missed_collection']);self.assertTrue(report['unchanged_since_previous_success'])
        self.assertEqual(report['last_changed_forecast_snapshot'],'2026-09-17T21:00:00Z')
        self.assertTrue(w.health_report(at='2026-09-18T11:01:00Z')['ensemble_gfs025']['missed_collection'])
        self.raw('forecasts/c/c_ensemble_gfs025_failed.json',{},'2026-09-18T11:00:00Z',503)
        report=w.health_report(at='2026-09-18T11:01:00Z')['ensemble_gfs025']
        self.assertFalse(report['missed_collection']);self.assertTrue(report['stale_success'])
        self.assertEqual(report['last_attempt_error'],'HTTP 503')
        with patch.object(w.requests,'get',side_effect=requests.ConnectionError('offline')),patch.object(w.time,'sleep'):
            with self.assertRaises(requests.ConnectionError):w.fetch('https://test',w.RAW/'metar','KJFK')
        self.assertEqual(w.health_report()['metar']['last_attempt_error'],'offline')

    def test_parquet_nulls_members_receipts_exact_and_repeatable(self):
        fixture=fixtures.EnsembleTests().fixture()
        fixture['hourly']['temperature_2m_member03'][0]=None
        for i in range(2):
            raw=self.raw(f'forecasts/run{i}/raw_ensemble_gfs025.json',fixture,f'2026-09-17T2{i}:00:00Z')
            frame,summary=w.parse_ensemble(fixture,'gfs025',f'2026-09-17T2{i}:00:00Z')
            summary['raw_file']=raw.name
            (raw.parent/'ensemble_gfs025_summary.json').write_text(json.dumps(summary))
        table,receipts=w.build_ensemble_archive()
        loaded=w.load_ensemble_archive()
        pd.testing.assert_frame_equal(table,loaded,check_exact=True)
        self.assertEqual(table.temperature_c.isna().sum(),1)
        self.assertEqual(len(receipts),2);self.assertEqual(table.member_id.nunique(),31)
        again,_=w.build_ensemble_archive()
        pd.testing.assert_frame_equal(table,again,check_exact=True)
        self.assertEqual(len(list((w.OUT/'ensemble_temperature').rglob('values.parquet'))),1)
        self.assertFalse((w.OUT/'ensemble_temperature.csv').exists())

if __name__=='__main__':unittest.main()
