"""JFK NOAA observations, degree days, and immutable forecast snapshots."""
from pathlib import Path
import argparse
import calendar
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from urllib.parse import urlencode
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
    """Preserve every HTTP response, including errors, before validation/retry."""
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        r = None
        try:
            r = requests.get(url, headers={'User-Agent': 'JFK-Weather-Research/1.0', 'Accept': '*/*'}, timeout=60)
            stamp = now().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:6]
            suffix = '' if r.ok else '_failed'
            path = directory / f'{stamp}_{stem}{suffix}.{extension}'
            path.write_bytes(r.content)
            metadata = {'retrieved_at_utc': now().isoformat(), 'url': url, 'status': r.status_code,
                        'sha256': hashlib.sha256(r.content).hexdigest(), 'headers': dict(r.headers)}
            path.with_suffix(path.suffix + '.meta.json').write_text(json.dumps(metadata, indent=2))
            r.raise_for_status()
            if not r.content:
                raise ValueError(f'Empty response: {url}')
            return path
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            if r is not None and r.status_code == 429:
                # Do not aggressively retry a rate limit or hold the archive for minutes.
                wait = r.headers.get('Retry-After', '2')
                if not wait.isdigit() or int(wait) > 30:
                    raise
                time.sleep(max(int(wait), 2 ** attempt))
            elif r is not None and 400 <= r.status_code < 500:
                raise
            else:
                time.sleep(2 ** attempt)


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

def archive_forecasts(ensembles_only=False):
    run = ROOT / 'data/forecasts' / (now().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:6])
    run.mkdir(parents=True)
    manifest = {'started_at_utc': now().isoformat(), 'station': ICAO, 'products': {}, 'errors': {}}
    for name in ([] if ensembles_only else ['nws_hourly', 'taf', 'metar']):
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
    manifest['ensemble_summaries'] = {}
    for model in ENSEMBLE_MODELS:
        name = f'ensemble_{model}'
        try:
            summary = collect_ensemble(model, run)
            manifest['products'][name] = summary['raw_file']
            manifest['ensemble_summaries'][model] = summary
            if not summary['usable_future_values']:
                manifest['errors'][name] = 'No usable future temperatures; raw and parsed data retained'
        except Exception as exc:
            manifest['errors'][name] = str(exc)
    manifest['completed_at_utc'] = now().isoformat()
    (run / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    build_ensemble_archive()
    print(json.dumps({'archive': str(run), **manifest}, indent=2))
    if manifest['errors']:
        raise RuntimeError(f'Partial archive; see {run / "manifest.json"}')
    return run

# Explicit global grids; never best_match or seamless model blending.
ENSEMBLE_MODELS = {
    'gfs025': {'label': 'NOAA GEFS 0.25°', 'members': 31, 'days': 10, 'domain': 'ncep_gefs025'},
    'ecmwf_ifs025': {'label': 'ECMWF IFS 0.25° ENS', 'members': 51, 'days': 15, 'domain': 'ecmwf_ifs025_ensemble'},
}
LATITUDE, LONGITUDE = 40.6392, -73.7639


def ensemble_url(model):
    config = ENSEMBLE_MODELS[model]
    return 'https://ensemble-api.open-meteo.com/v1/ensemble?' + urlencode({
        'latitude': LATITUDE, 'longitude': LONGITUDE, 'hourly': 'temperature_2m',
        'models': model, 'forecast_days': config['days'], 'temperature_unit': 'celsius',
        'timezone': 'GMT', 'timeformat': 'unixtime', 'cell_selection': 'land', 'elevation': 2.7,
    })


def utc_timestamp(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('Timestamp must include an explicit timezone')
    return stamp.tz_convert('UTC')


def parse_ensemble(data, model, retrieved_at):
    """Parse every returned temperature member. Keep nulls, reject ambiguous axes.

    The endpoint does not document response-bound initialization metadata. Its
    generationtime_ms is processing duration, NOT a forecast issue timestamp.
    """
    config = ENSEMBLE_MODELS[model]
    retrieved = utc_timestamp(retrieved_at)
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError(f'Invalid ensemble response: {data}')
    hourly, units = data.get('hourly', {}), data.get('hourly_units', {})
    times = hourly.get('time')
    if not isinstance(times, list) or not times:
        raise ValueError('Missing hourly time axis')
    if units.get('time') != 'unixtime' or data.get('utc_offset_seconds') != 0:
        raise ValueError('Expected UTC Unix seconds, not local or ambiguous timestamps')
    if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in times):
        raise ValueError('Invalid Unix timestamps')
    target = pd.to_datetime(times, unit='s', utc=True)
    if target.has_duplicates or not target.is_monotonic_increasing:
        raise ValueError('Duplicate or unordered target times')
    for key, low, high in [('latitude', -90, 90), ('longitude', -180, 180)]:
        if not isinstance(data.get(key), (int, float)) or not low <= data[key] <= high:
            raise ValueError(f'Invalid returned {key}')
    frames, warnings, ids = [], [], set()
    for key, values in hourly.items():
        if key == 'time':
            continue
        match = re.fullmatch(r'temperature_2m(?:_member(\d+))?', key)
        if not match:
            raise ValueError(f'Unexpected hourly field: {key}')
        member = int(match.group(1) or 0)
        if member in ids:
            raise ValueError('Duplicate member identity')
        ids.add(member)
        if units.get(key) not in ('°C', '°F'):
            raise ValueError(f'Unsupported or missing temperature unit for {key}')
        if not isinstance(values, list) or len(values) != len(times):
            warnings.append(f'{key}: invalid array length; retained raw, omitted malformed member')
            continue
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)) for v in values):
            warnings.append(f'{key}: invalid numeric values; retained raw, omitted malformed member')
            continue
        frames.append(pd.DataFrame({'member_id': member, 'member_role': 'control' if member == 0 else 'perturbed',
            'source_variable': key, 'target_time_utc': target, 'temperature_2m': values, 'units': units[key]}))
    if not frames:
        raise ValueError('No parseable temperature members')
    table = pd.concat(frames, ignore_index=True)
    table['temperature_2m'] = pd.to_numeric(table.temperature_2m)
    table['temperature_c'] = table.temperature_2m.where(table.units == '°C', (table.temperature_2m - 32) / 1.8)
    table['model'] = model
    table['station'] = ICAO
    table['requested_latitude'] = LATITUDE
    table['requested_longitude'] = LONGITUDE
    table['requested_elevation_m'] = 2.7
    table['grid_latitude'] = data['latitude']
    table['grid_longitude'] = data['longitude']
    table['response_elevation_m'] = data.get('elevation')
    table['retrieved_at_utc'] = retrieved.isoformat()
    table['initialization_time_utc'] = pd.NaT
    table['initialization_status'] = 'not_provided_in_forecast_response'
    # Forecast-content identity excludes retrieval time and generationtime_ms.
    # A changed value, null mask, target axis, member set, or grid is a revision.
    canonical = {'model': model, 'requested_location': [LATITUDE, LONGITUDE, 2.7],
                 'grid': [data['latitude'], data['longitude'], data.get('elevation')],
                 'hourly': hourly, 'hourly_units': units}
    snapshot = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    table['snapshot_id'] = snapshot
    valid = table[table.temperature_c.notna()]
    expected_ids = set(range(config['members']))
    parsed_ids = set(table.member_id)
    missing = sorted(expected_ids - parsed_ids)
    if missing:
        warnings.append(f'Missing/unparseable member IDs: {missing}')
    if parsed_ids - expected_ids:
        warnings.append(f'Additional member IDs retained: {sorted(parsed_ids - expected_ids)}')
    if table.temperature_c.isna().any():
        warnings.append(f'{int(table.temperature_c.isna().sum())} missing temperature values; no imputation')
    if len(times) != config['days'] * 24 or (len(times) > 1 and not (pd.Series(times).diff().dropna() == 3600).all()):
        warnings.append('Returned target axis does not cover the requested consecutive hourly window')
    summary = {'snapshot_id': snapshot, 'model': model, 'retrieved_at_utc': retrieved.isoformat(),
        'expected_members': config['members'], 'returned_members': len(ids), 'parsed_members': len(parsed_ids),
        'members_with_data': int(valid.member_id.nunique()), 'control_present': 0 in parsed_ids,
        'requested_days': config['days'], 'target_hours': len(times), 'rows': len(table),
        'missing_values': int(table.temperature_c.isna().sum()), 'missing_member_ids': missing,
        'target_start_utc': target.min().isoformat(), 'target_end_utc': target.max().isoformat(),
        'valid_start_utc': valid.target_time_utc.min().isoformat() if len(valid) else None,
        'valid_end_utc': valid.target_time_utc.max().isoformat() if len(valid) else None,
        'complete_requested_window': not warnings, 'warnings': warnings}
    return table, summary


def collect_ensemble(model, run):
    """Metadata is advisory only: separate servers cannot prove response run ID."""
    run = Path(run)
    config = ENSEMBLE_MODELS[model]
    metadata_url = f'https://ensemble-api.open-meteo.com/data/{config["domain"]}/static/meta.json'
    metadata, notes = {}, []
    for phase in ('before', 'after'):
        if phase == 'after':
            forecast_path = fetch(ensemble_url(model), run, f'ensemble_{model}')
            receipt = json.loads(forecast_path.with_suffix('.json.meta.json').read_text())['retrieved_at_utc']
            table, summary = parse_ensemble(json.loads(forecast_path.read_text()), model, receipt)
        try:
            path = fetch(metadata_url, run, f'model_metadata_{model}_{phase}')
            parsed_metadata = json.loads(path.read_text())
            if not isinstance(parsed_metadata, dict) or parsed_metadata.get('error'):
                raise ValueError('Invalid publication metadata')
            metadata[phase] = parsed_metadata
        except Exception as exc:
            notes.append(f'Model metadata {phase} unavailable: {exc}')
    summary['raw_file'] = forecast_path.name
    summary['metadata_before'] = metadata.get('before')
    summary['metadata_after'] = metadata.get('after')
    advisory = metadata.get('after', {})
    for field in ('last_run_initialisation_time', 'last_run_availability_time'):
        value = advisory.get(field)
        if isinstance(value, (int, float)):
            summary[f'advisory_{field}_utc'] = pd.to_datetime(value, unit='s', utc=True).isoformat()
    if metadata.get('before') != metadata.get('after'):
        notes.append('Publication metadata changed during retrieval; run identity unverified')
    available = advisory.get('last_run_availability_time')
    if isinstance(available, (int, float)):
        age = utc_timestamp(receipt).timestamp() - available
        if age < 600:
            notes.append('Within publication/replication window; collect again next run (10-minute provider buffer)')
        elif age > 2 * advisory.get('update_interval_seconds', 21600) + 600:
            notes.append('Model availability metadata more than two update intervals old')
    else:
        notes.append('Publication readiness unavailable; run identity unverified')
    summary['warnings'].extend(notes)
    summary['initialization_status'] = 'not_provided_in_forecast_response; separate metadata is advisory'
    table['advisory_initialization_time_utc'] = summary.get('advisory_last_run_initialisation_time_utc')
    table['advisory_availability_time_utc'] = summary.get('advisory_last_run_availability_time_utc')
    table['raw_file'] = str(forecast_path.relative_to(ROOT)) if forecast_path.is_relative_to(ROOT) else str(forecast_path)
    # Avoid confusing advisory metadata with a verified initialization on rows.
    table.to_csv(run / f'ensemble_{model}.csv', index=False)
    summary['usable_future_values'] = int(((table.target_time_utc > utc_timestamp(receipt)) & table.temperature_c.notna()).sum())
    (run / f'ensemble_{model}_summary.json').write_text(json.dumps(summary, indent=2))
    return summary


def build_ensemble_archive(root=None, output=None):
    """Offline deterministic rebuild; one row per distinct snapshot/member/target.

    All retrieval receipts are retained separately, including identical polls.
    Changed snapshots are never overwritten or mixed in quantile calculations.
    """
    root = Path(root) if root else ROOT / 'data/forecasts'
    output = Path(output) if output else OUT
    output.mkdir(parents=True, exist_ok=True)
    frames, summaries = [], []
    for path in sorted(root.glob('*/ensemble_*_summary.json')):
        summary = json.loads(path.read_text())
        table_path = path.parent / f'ensemble_{summary["model"]}.csv'
        frame = pd.read_csv(table_path)
        frame['advisory_initialization_time_utc'] = summary.get('advisory_last_run_initialisation_time_utc')
        frame['advisory_availability_time_utc'] = summary.get('advisory_last_run_availability_time_utc')
        if frame.duplicated(['snapshot_id', 'member_id', 'target_time_utc']).any():
            raise ValueError(f'Duplicate member/target in {table_path}')
        frames.append(frame)
        summaries.append({**summary, 'archive_run': path.parent.name})
    if not frames:
        return pd.DataFrame(), pd.DataFrame()
    all_rows = pd.concat(frames, ignore_index=True).sort_values('retrieved_at_utc')
    receipts = pd.DataFrame(summaries).sort_values('retrieved_at_utc')
    coverage = receipts.copy()
    for field in ('metadata_before', 'metadata_after', 'warnings', 'missing_member_ids'):
        if field in coverage:
            coverage[field] = coverage[field].apply(json.dumps)
    coverage.to_csv(output / 'ensemble_coverage.csv', index=False)
    observations = receipts.groupby('snapshot_id').agg(first_retrieved_at_utc=('retrieved_at_utc', 'min'),
        last_retrieved_at_utc=('retrieved_at_utc', 'max'), retrieval_count=('retrieved_at_utc', 'size'))
    table = all_rows.drop_duplicates(['snapshot_id', 'member_id', 'target_time_utc'], keep='first').merge(observations, on='snapshot_id', validate='many_to_one')
    table.to_csv(output / 'ensemble_temperature.csv', index=False)
    return table, receipts


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
    build_ensemble_archive()
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
    parser.add_argument('action', choices=['refresh', 'archive', 'ensembles', 'build', 'all'])
    args = parser.parse_args()
    if args.action in ('archive', 'ensembles', 'all'):
        archive_forecasts(ensembles_only=args.action == 'ensembles')
    if args.action in ('refresh', 'all'):
        print(refresh_observations())
    if args.action in ('build', 'all'):
        g, m, c = build()
        print(f'GHCN: {g.valid_day.sum()}/{len(g)} valid days; METAR: {m.valid_day.sum()}/{len(m)}; paired: {c.dropna().shape[0]}')
