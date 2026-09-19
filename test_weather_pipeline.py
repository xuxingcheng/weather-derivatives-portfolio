import calendar
import json
from pathlib import Path
import tempfile
import unittest
import pandas as pd
import weather_pipeline as w

class WeatherTests(unittest.TestCase):
    def test_degree_days_and_incomplete_month(self):
        dates = pd.date_range('2024-02-01', '2024-02-29')
        d = w.degree_days(pd.DataFrame({'tmean_c': [(55-32)/1.8]*29}, index=dates))
        m = w.monthly(d)
        self.assertAlmostEqual(m.hdd65_f_days.iloc[0], 290)
        self.assertEqual(m.cdd65_f_days.iloc[0], 0)
        self.assertTrue(pd.isna(w.monthly(d.iloc[:-1]).hdd65_f_days.iloc[0]))
        self.assertAlmostEqual(w.monthly(d.iloc[:-1]).hdd_partial.iloc[0], 280)
        self.assertTrue(pd.isna(w.monthly(d.assign(tmean_c=float('nan'), hdd65_f_days=float('nan'))).hdd_partial.iloc[0]))

    def test_ghcn_flags_missing_and_reversed(self):
        lines=[]
        for element, value in [('TMIN',100), ('TMAX',200)]:
            blocks=[]
            for day in range(1,32):
                v = -9999 if day > 29 or day == 3 else (300 if day == 4 and element == 'TMIN' else value)
                q = 'X' if day == 2 and element == 'TMAX' else ' '
                blocks.append(f'{v:5d} {q}0')
            lines.append(f'{w.GHCN_ID}202402{element}' + ''.join(blocks))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.dly'; path.write_text('\n'.join(lines))
            d, audit=w.ghcn_daily(path, '2024-02-01','2024-02-29')
        self.assertEqual(d.tmean_c.iloc[0],15)
        self.assertTrue(d.tmean_c.iloc[1:4].isna().all())
        self.assertEqual(len(d),29)
        self.assertIn('X',audit.qflag.values)

    def test_metar_dst_and_correction(self):
        for date, hours in [('2024-03-10',23),('2024-11-03',25)]:
            start=pd.Timestamp(date,tz=w.TZ)
            end=(pd.Timestamp(date)+pd.Timedelta(days=1)).tz_localize(w.TZ)
            stamps=pd.date_range(start,end,freq='h',inclusive='left').tz_convert('UTC')
            rows=[{'icaoId':w.ICAO,'obsTime':int(t.timestamp()),'receiptTime':t.isoformat(),'temp':10} for t in stamps]
            correction=dict(rows[0],temp=20,receiptTime=(stamps[0]+pd.Timedelta(minutes=10)).isoformat())
            rows.append(correction)
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'test.json';path.write_text(json.dumps(rows))
                d,obs=w.metar_daily([path])
            first=d.iloc[0]
            self.assertEqual(first.expected_hours,hours)
            self.assertEqual(first.observed_hours,hours)
            self.assertTrue(first.valid_day)
            self.assertEqual(first.tmax_c,20)
            self.assertEqual(len(obs),hours)


class EnsembleTests(unittest.TestCase):
    def fixture(self, model='gfs025', unit='°C'):
        n = w.ENSEMBLE_MODELS[model]['members']
        keys = ['temperature_2m'] + [f'temperature_2m_member{i:02}' for i in range(1, n)]
        return {'latitude':40.75, 'longitude':-73.75, 'elevation':2.7, 'utc_offset_seconds':0,
                'generationtime_ms':2.5,
                'hourly_units':{'time':'unixtime', **{k:unit for k in keys}},
                'hourly':{'time':[1789682400, 1789686000], **{k:[32+i,33+i] for i,k in enumerate(keys)}}}

    def parse(self, data, model='gfs025', receipt='2026-09-17T21:00:00Z'):
        return w.parse_ensemble(data, model, receipt)

    def test_members_control_timestamps_and_units(self):
        for model in w.ENSEMBLE_MODELS:
            data = self.fixture(model)
            table, info = self.parse(data, model)
            self.assertEqual(len(table), 2*w.ENSEMBLE_MODELS[model]['members'])
            self.assertEqual(set(table.member_id), set(range(info['expected_members'])))
            self.assertEqual(table.loc[table.member_id==0, 'source_variable'].unique().tolist(), ['temperature_2m'])
            self.assertTrue(info['control_present'])
            self.assertEqual(str(table.target_time_utc.dt.tz), 'UTC')
            self.assertEqual(table.target_time_utc.iloc[0], pd.Timestamp('2026-09-17T22:00:00Z'))
            self.assertTrue(table.initialization_time_utc.isna().all())
            self.assertTrue((table.temperature_c==table.temperature_2m).all())
            self.assertEqual(table.requested_latitude.iloc[0],40.6392)
            self.assertEqual(table.grid_latitude.iloc[0],40.75)
        table,_=self.parse(self.fixture(unit='°F'))
        self.assertAlmostEqual(table.temperature_c.iloc[0],0)
        self.assertEqual(table.units.iloc[0],'°F')

    def test_reject_ambiguous_timestamps_units_and_duplicate_axes(self):
        mutations = [lambda d:d['hourly_units'].update(time='iso8601'),
                     lambda d:d.update(utc_offset_seconds=-14400),
                     lambda d:d['hourly']['time'].__setitem__(1,d['hourly']['time'][0]),
                     lambda d:d['hourly_units'].update(temperature_2m='K'),
                     lambda d:d['hourly'].update(temperature_2m_member00=[1,2]),
                     lambda d:d['hourly']['time'].__setitem__(0,None)]
        for change in mutations:
            d=self.fixture();change(d)
            with self.assertRaises(ValueError): self.parse(d)
        with self.assertRaises(ValueError): self.parse(self.fixture(),receipt='2026-09-17T21:00')
        with self.assertRaises(ValueError): self.parse({'error':True,'reason':'Unavailable'})

    def test_partial_members_nulls_and_bad_arrays(self):
        data=self.fixture()
        del data['hourly']['temperature_2m_member30']
        data['hourly']['temperature_2m_member29']=[1]
        data['hourly']['temperature_2m_member28']=[None,None]
        table,info=self.parse(data)
        self.assertEqual(info['parsed_members'],29)
        self.assertEqual(info['missing_member_ids'],[29,30])
        self.assertEqual(info['missing_values'],2)
        self.assertTrue(table.loc[table.member_id==28,'temperature_c'].isna().all())
        self.assertFalse(info['complete_requested_window'])
        for key in data['hourly']:
            if key!='time': data['hourly'][key]=[None,None]
        table,info=self.parse(data)
        self.assertEqual(info['members_with_data'],0)
        self.assertIsNone(info['valid_end_utc'])

    def test_snapshot_identity_and_idempotent_rebuild(self):
        import copy
        d=self.fixture(); first,s1=self.parse(d)
        d['generationtime_ms']=100
        repeated,s2=self.parse(d,receipt='2026-09-17T23:00:00Z')
        self.assertEqual(s1['snapshot_id'],s2['snapshot_id'])
        changed=copy.deepcopy(d);changed['hourly']['temperature_2m'][0]+=1
        revised,s3=self.parse(changed,receipt='2026-09-18T01:00:00Z')
        self.assertNotEqual(s1['snapshot_id'],s3['snapshot_id'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'runs';out=Path(tmp)/'out'
            for i,(frame,summary) in enumerate([(first,s1),(repeated,s2),(revised,s3)]):
                run=root/str(i);run.mkdir(parents=True)
                frame.to_csv(run/'ensemble_gfs025.csv',index=False)
                (run/'ensemble_gfs025_summary.json').write_text(json.dumps(summary))
            table,receipts=w.build_ensemble_archive(root,out)
            self.assertEqual(len(receipts),3)
            self.assertEqual(len(table),124)
            self.assertEqual(table[table.snapshot_id==s1['snapshot_id']].retrieval_count.unique().tolist(),[2])
            again,_=w.build_ensemble_archive(root,out)
            pd.testing.assert_frame_equal(table,again)

    def test_collection_metadata_failure_preserves_forecast(self):
        from unittest.mock import patch
        def fake_fetch(url, directory, stem, extension='json'):
            if 'static/meta' in url: raise RuntimeError('metadata unavailable')
            p=directory/f'raw_{stem}.json';p.write_text(json.dumps(self.fixture()))
            p.with_suffix('.json.meta.json').write_text(json.dumps({'retrieved_at_utc':'2026-09-17T21:00:00Z'}))
            return p
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'fetch',side_effect=fake_fetch):
            summary=w.collect_ensemble('gfs025',Path(tmp))
            self.assertEqual(summary['parsed_members'],31)
            self.assertGreater(summary['usable_future_values'],0)
            self.assertTrue(any('metadata before unavailable' in s for s in summary['warnings']))
            self.assertTrue((Path(tmp)/'ensemble_gfs025.csv').exists())

    def test_source_failure_does_not_stop_other_sources(self):
        from unittest.mock import patch
        def fake_fetch(url, directory, stem, extension='json'):
            if stem=='nws_point': payload={'properties':{'forecastHourly':'https://test/forecast'}}
            elif stem=='nws_hourly':payload={'properties':{'periods':[{'startTime':'2026-09-18T00:00:00Z','endTime':'2026-09-18T01:00:00Z','temperature':60}]}}
            elif stem=='taf':raise RuntimeError('TAF outage')
            else:payload=[{'icaoId':'KJFK'}]
            p=directory/f'raw_{stem}.json';p.write_text(json.dumps(payload));return p
        def fake_ensemble(model,run):
            if model=='gfs025':raise RuntimeError('GEFS outage')
            p=run/'ecmwf.json';p.write_text('{}')
            return {'raw_file':p.name,'usable_future_values':10}
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'ROOT',Path(tmp)), patch.object(w,'fetch',side_effect=fake_fetch), patch.object(w,'collect_ensemble',side_effect=fake_ensemble), patch.object(w,'build_ensemble_archive'):
            with self.assertRaises(RuntimeError): w.archive_forecasts()
            manifest=json.loads(next(Path(tmp).glob('data/forecasts/*/manifest.json')).read_text())
            self.assertEqual(set(manifest['errors']),{'taf','ensemble_gfs025'})
            self.assertEqual(set(manifest['products']),{'nws_hourly','metar','ensemble_ecmwf_ifs025'})

    def test_additional_member_retained_and_missing_control_flagged(self):
        data=self.fixture()
        del data['hourly']['temperature_2m']
        data['hourly']['temperature_2m_member99']=[4,5]
        data['hourly_units']['temperature_2m_member99']='°C'
        frame,summary=self.parse(data)
        self.assertIn(99,set(frame.member_id))
        self.assertFalse(summary['control_present'])
        self.assertEqual(summary['missing_member_ids'],[0])
        self.assertFalse(summary['complete_requested_window'])

    def test_publication_delay_and_all_null_archive(self):
        from unittest.mock import patch
        def fake_fetch(url, directory, stem, extension='json'):
            if 'static/meta' in url:
                data={'last_run_initialisation_time':1789660800,
                      'last_run_availability_time':1789678770,'update_interval_seconds':21600}
            else:
                model='ecmwf_ifs025' if 'ecmwf_ifs025' in url else 'gfs025'
                data=self.fixture(model)
                for key in data['hourly']:
                    if key!='time': data['hourly'][key]=[None,None]
            p=directory/f'raw_{stem}.json';p.write_text(json.dumps(data))
            p.with_suffix('.json.meta.json').write_text(json.dumps({'retrieved_at_utc':'2026-09-17T21:00:00Z'}))
            return p
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'ROOT',Path(tmp)), patch.object(w,'OUT',Path(tmp)/'processed'), patch.object(w,'fetch',side_effect=fake_fetch), patch('builtins.print'):
            with self.assertRaises(RuntimeError): w.archive_forecasts(ensembles_only=True)
            run=next(Path(tmp).glob('data/forecasts/*'))
            manifest=json.loads((run/'manifest.json').read_text())
            self.assertEqual(len(manifest['errors']),2)
            for model in w.ENSEMBLE_MODELS:
                summary=json.loads((run/f'ensemble_{model}_summary.json').read_text())
                self.assertEqual(summary['usable_future_values'],0)
                self.assertTrue(any('replication window' in x for x in summary['warnings']))
                self.assertTrue((run/f'ensemble_{model}.csv').exists())

    def test_http_failure_retains_body_and_retries(self):
        from unittest.mock import patch,Mock
        import requests
        response=Mock(ok=False,status_code=503,content=b'{"error":true}',headers={})
        response.raise_for_status.side_effect=requests.HTTPError('Unavailable')
        with tempfile.TemporaryDirectory() as tmp, patch.object(w.requests,'get',return_value=response) as get, patch.object(w.time,'sleep'):
            with self.assertRaises(requests.HTTPError): w.fetch('https://test',Path(tmp),'ensemble_test')
            self.assertEqual(get.call_count,3)
            self.assertEqual(len(list(Path(tmp).glob('*_failed.json'))),3)


if __name__=='__main__':
    unittest.main()
