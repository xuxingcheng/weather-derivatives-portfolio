"""JFK NOAA observations, degree days, and immutable forecast snapshots."""
from pathlib import Path
import argparse
import calendar
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
GHCN_ID = 'USW00094789'
ICAO = 'KJFK'
TZ = 'America/New_York'
BASE_F = 65.0
START = '2000-01-01'
POINT_URL = 'https://api.weather.gov/points/40.6392,-73.7639'
RAW = ROOT / 'data/raw'
OUT = ROOT / 'data/processed'

def now():
    return datetime.now(timezone.utc)

def fetch(url, directory, stem, extension='json'):
    """Keep original bytes, receipt time, URL, response headers, and checksum."""
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            r = requests.get(url, headers={'User-Agent': 'JFK-Weather-Research/1.0', 'Accept': '*/*'}, timeout=60)
            r.raise_for_status()
            if not r.content:
                raise ValueError(f'Empty response: {url}')
            break
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    stamp = now().strftime('%Y%m%dT%H%M%S%fZ')
    path = directory / f'{stamp}_{stem}.{extension}'
    path.write_bytes(r.content)
    metadata = {'retrieved_at_utc': now().isoformat(), 'url': url, 'status': r.status_code,
                'sha256': hashlib.sha256(r.content).hexdigest(), 'headers': dict(r.headers)}
    path.with_suffix(path.suffix + '.meta.json').write_text(json.dumps(metadata, indent=2))
    return path

def refresh_observations():
    paths = {}
    paths['ghcn'] = str(fetch(f'https://www.ncei.noaa.gov/pub/data/ghcn/daily/all/{GHCN_ID}.dly', RAW / 'ghcn', GHCN_ID, 'dly'))
    # Chunk requests to avoid the API's response-count limit. Receipt-time archives
    # overlap intentionally; processing resolves corrections deterministically.
    for days_back in range(0, 30, 5):
        ending = (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
        url = f'https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=120&date={ending}'
        paths[f'metar_{days_back}'] = str(fetch(url, RAW / 'metar', ICAO))
    return paths

def archive_forecasts():
    run = ROOT / 'data/forecasts' / (now().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:6])
    run.mkdir(parents=True)
    manifest = {'started_at_utc': now().isoformat(), 'station': ICAO, 'products': {}, 'errors': {}}
    for name in ['nws_hourly', 'taf', 'metar']:
        try:
            if name == 'nws_hourly':
                p = fetch(POINT_URL, run, 'nws_point')
                url = json.loads(p.read_text())['properties']['forecastHourly']
            elif name == 'taf':
                url = f'https://aviationweather.gov/api/data/taf?ids={ICAO}&format=json'
            else:
                url = f'https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=48'
            path = fetch(url, run, name)
            data = json.loads(path.read_text())
            if not data:
                raise ValueError('No records returned')
            manifest['products'][name] = path.name
            if name == 'nws_hourly':
                props = data['properties']
                table = pd.DataFrame(props['periods'])
                if table.empty:
                    raise ValueError('No hourly forecast periods')
                table['retrieved_at_utc'] = now().isoformat()
                table['update_time_utc'] = props.get('updateTime')
                table['generated_at_utc'] = props.get('generatedAt')
                table['lead_hours_at_retrieval'] = (pd.to_datetime(table.startTime, utc=True) - pd.Timestamp.now(tz='UTC')).dt.total_seconds() / 3600
                table.to_csv(run / 'nws_hourly.csv', index=False)
                manifest['forecast_valid_through'] = table.endTime.iloc[-1]
        except Exception as exc:
            manifest['errors'][name] = str(exc)
    manifest['completed_at_utc'] = now().isoformat()
    (run / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'archive': str(run), **manifest}, indent=2))
    if manifest['errors']:
        raise RuntimeError(f'Partial archive; see {run / "manifest.json"}')
    return run

def degree_days(frame):
    f = frame.copy()
    f['tmean_f'] = f.tmean_c * 1.8 + 32
    f['hdd65_f_days'] = (BASE_F - f.tmean_f).clip(lower=0)
    f['cdd65_f_days'] = (f.tmean_f - BASE_F).clip(lower=0)
    return f

def ghcn_daily(path, start=START, end=None):
    records = []
    for line in Path(path).read_text().splitlines():
        element = line[17:21]
        if line[:11] != GHCN_ID or element not in ('TMIN', 'TMAX'):
            continue
        year, month = int(line[11:15]), int(line[15:17])
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            block = line[21 + 8*(day-1):29 + 8*(day-1)]
            value = int(block[:5])
            records.append({'date': pd.Timestamp(year, month, day), 'element': element,
                            'raw_value': value, 'mflag': block[5].strip(), 'qflag': block[6].strip(),
                            'sflag': block[7].strip(),
                            'value_c': value / 10 if value != -9999 and block[6] == ' ' else float('nan')})
    audit = pd.DataFrame(records)
    if audit.duplicated(['date', 'element']).any():
        raise ValueError('Duplicate GHCN date/element records')
    end = pd.Timestamp(end) if end else pd.Timestamp.now(tz=TZ).tz_localize(None).normalize() - pd.Timedelta(days=1)
    audit = audit[audit.date.between(pd.Timestamp(start), end)]
    frame = audit.pivot(index='date', columns='element', values='value_c').rename(columns={'TMIN':'tmin_c','TMAX':'tmax_c'})
    frame = frame.reindex(pd.date_range(start, end, name='date'))
    frame['valid_day'] = frame.tmin_c.notna() & frame.tmax_c.notna() & (frame.tmin_c <= frame.tmax_c)
    frame['tmean_c'] = ((frame.tmin_c + frame.tmax_c) / 2).where(frame.valid_day)
    frame['source'] = 'GHCN-Daily'
    return degree_days(frame), audit

def metar_daily(paths):
    rows = []
    for path in sorted(paths):
        data = json.loads(Path(path).read_text())
        if not isinstance(data, list):
            raise ValueError(f'Unexpected METAR response: {path}')
        rows.extend(data)
    obs = pd.DataFrame(rows)
    obs = obs[obs.icaoId == ICAO].copy()
    obs['time_utc'] = pd.to_datetime(obs.obsTime, unit='s', utc=True)
    obs['receipt_utc'] = pd.to_datetime(obs.receiptTime, utc=True)
    obs = obs.sort_values('receipt_utc').drop_duplicates('time_utc', keep='last').sort_values('time_utc')
    obs['temp_c'] = pd.to_numeric(obs.temp, errors='coerce')
    obs['plausible_temp'] = obs.temp_c.between(-60, 55)
    obs['accepted_temp_c'] = obs.temp_c.where(obs.plausible_temp)
    # UTC bins handle DST repeated/skipped local hours without ambiguity.
    hourly = obs.set_index('time_utc').accepted_temp_c.resample('h').mean()
    local = hourly.index.tz_convert(TZ)
    h = pd.DataFrame({'temp': hourly.values, 'date': local.tz_localize(None).normalize()})
    grouped = h.groupby('date').temp
    daily = grouped.agg(tmin_c='min', tmax_c='max', sampled_hourly_mean_c='mean', observed_hours='count')
    today = pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
    dates = pd.date_range(daily.index.min(), today, name='date')
    daily = daily.reindex(dates)
    daily['expected_hours'] = [int(((d + pd.Timedelta(days=1)).tz_localize(TZ) - d.tz_localize(TZ)).total_seconds()/3600) for d in dates]
    daily['coverage'] = daily.observed_hours / daily.expected_hours
    # Strict completeness, no interpolation. Current local day never accepted.
    daily['valid_day'] = (daily.observed_hours == daily.expected_hours) & (daily.index < today)
    daily['tmean_c'] = ((daily.tmin_c + daily.tmax_c)/2).where(daily.valid_day)
    daily['source'] = 'METAR sampled hourly extrema'
    return degree_days(daily), obs

def monthly(daily):
    grouped = daily.groupby(daily.index.to_period('M'))
    result = grouped.agg(valid_days=('tmean_c', 'count'), hdd_partial=('hdd65_f_days', lambda s:s.sum(min_count=1)), cdd_partial=('cdd65_f_days', lambda s:s.sum(min_count=1)))
    result['calendar_days'] = result.index.days_in_month
    result['coverage'] = result.valid_days / result.calendar_days
    result['complete_month'] = result.valid_days == result.calendar_days
    result['hdd65_f_days'] = result.hdd_partial.where(result.complete_month)
    result['cdd65_f_days'] = result.cdd_partial.where(result.complete_month)
    result.index.name = 'month'
    return result

def build():
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted((RAW / 'ghcn').glob('*.dly'))
    if not files:
        raise FileNotFoundError('Run refresh first')
    ghcn, audit = ghcn_daily(files[-1])
    metar_paths = list((RAW / 'metar').glob('*.json')) + list((ROOT / 'data/forecasts').glob('*/*_metar.json'))
    metar_paths = [p for p in metar_paths if not p.name.endswith('.meta.json')]
    metar, observations = metar_daily(metar_paths)
    for name, frame in {'ghcn_daily':ghcn, 'ghcn_monthly':monthly(ghcn), 'metar_daily':metar, 'metar_monthly':monthly(metar)}.items():
        frame.to_csv(OUT / f'{name}.csv')
    audit.to_csv(OUT/'ghcn_quality_audit.csv', index=False)
    observations.to_csv(OUT/'metar_observations.csv', index=False)
    comparison = ghcn[['tmean_c']].join(metar[['tmean_c']], how='inner', lsuffix='_ghcn', rsuffix='_metar')
    comparison['difference_c'] = comparison.tmean_c_metar - comparison.tmean_c_ghcn
    comparison.to_csv(OUT / 'daily_comparison.csv')
    return ghcn, metar, comparison

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['refresh', 'archive', 'build', 'all'])
    args = parser.parse_args()
    if args.action in ('archive', 'all'):
        archive_forecasts()
    if args.action in ('refresh', 'all'):
        print(refresh_observations())
    if args.action in ('build', 'all'):
        g, m, c = build()
        print(f'GHCN: {g.valid_day.sum()}/{len(g)} valid days; METAR: {m.valid_day.sum()}/{len(m)}; paired: {c.dropna().shape[0]}')
