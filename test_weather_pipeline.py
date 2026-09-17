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

if __name__=='__main__':
    unittest.main()
