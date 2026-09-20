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
import os
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
        attempt_stamp = now().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:6]
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
        except (requests.RequestException, ValueError) as exc:
            if r is None:
                candidate = directory / f'{attempt_stamp}_{stem}.{extension}'
                write_json(candidate.with_suffix('.attempt.json'), {'attempted_at_utc': now().isoformat(),
                    'url': url, 'candidate': str(candidate), 'error': str(exc), 'attempt': attempt + 1})
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
    """Force a full GHCN snapshot and six overlapping METAR windows independently."""
    manifest = {'started_at_utc': now().isoformat(), 'products': {}, 'errors': {}}
    jobs = [('ghcn', f'https://www.ncei.noaa.gov/pub/data/ghcn/daily/all/{GHCN_ID}.dly', RAW / 'ghcn', GHCN_ID, 'dly')]
    for days_back in range(0, 30, 5):
        ending = (pd.Timestamp(now()) - pd.Timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
        jobs.append((f'metar_{days_back}', f'https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=120&date={ending}', RAW / 'metar', ICAO, 'json'))
    for name, url, directory, stem, extension in jobs:
        try:
            path = fetch(url, directory, stem, extension)
            validate_download(path, 'ghcn' if name == 'ghcn' else 'metar')
            manifest['products'][name] = relative_path(path)
        except Exception as exc:
            manifest['errors'][name] = str(exc)
    manifest['completed_at_utc'] = now().isoformat()
    write_json(RAW / 'refreshes' / (uuid.uuid4().hex + '.json'), manifest)
    if manifest['errors']:
        raise RuntimeError(str(manifest['errors']))
    return manifest

def archive_forecasts(ensembles_only=False, force_ghcn=False):
    run = ROOT / 'data/forecasts' / (now().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:6])
    run.mkdir(parents=True)
    manifest = {'started_at_utc': now().isoformat(), 'station': ICAO, 'products': {}, 'errors': {}}
    if not ensembles_only and (force_ghcn or ghcn_due()):
        try:
            path = fetch(f'https://www.ncei.noaa.gov/pub/data/ghcn/daily/all/{GHCN_ID}.dly', RAW / 'ghcn', GHCN_ID, 'dly')
            validate_download(path, 'ghcn')
            manifest['products']['ghcn'] = str(path.relative_to(ROOT))
        except Exception as exc:
            manifest['errors']['ghcn'] = str(exc)
    elif not ensembles_only:
        manifest['ghcn_refresh'] = 'not due: valid retrieval within 24 hours'
    for name in ([] if ensembles_only else ['nws_hourly', 'taf', 'metar']):
        try:
            if name == 'nws_hourly':
                p = fetch(POINT_URL, run, 'nws_point')
                validate_download(p, 'nws_point')
                manifest['products']['nws_point'] = p.name
                url = json.loads(p.read_text())['properties']['forecastHourly']
            elif name == 'taf':
                url = f'https://aviationweather.gov/api/data/taf?ids={ICAO}&format=json'
            else:
                url = f'https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=48'
            path = fetch(url, run, name)
            validate_download(path, name)
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
    # Normalized member data is written once per content snapshot during build.
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
    frames, summaries, rejected = [], [], []
    for path in sorted(root.glob('*/ensemble_*_summary.json')):
        try:
            summary = json.loads(path.read_text())
            table_path = path.parent / f'ensemble_{summary["model"]}.csv'
            raw_path = path.parent / summary['raw_file']
            validate_download(raw_path, f'ensemble_{summary["model"]}')
            if table_path.exists():
                frame = pd.read_csv(table_path)  # preserve legacy CSV numeric representation
            else:
                frame, _ = parse_ensemble(json.loads(raw_path.read_text()), summary['model'], summary['retrieved_at_utc'])
                frame['raw_file'] = relative_path(raw_path)
                frame['target_time_utc'] = frame.target_time_utc.astype(str)
                frame['initialization_time_utc'] = float('nan')
        except Exception as exc:
            rejected.append({'summary': str(path), 'error': str(exc)})
            continue
        frame['advisory_initialization_time_utc'] = summary.get('advisory_last_run_initialisation_time_utc')
        frame['advisory_availability_time_utc'] = summary.get('advisory_last_run_availability_time_utc')
        if frame.duplicated(['snapshot_id', 'member_id', 'target_time_utc']).any():
            raise ValueError(f'Duplicate member/target in {table_path}')
        frames.append(frame)
        summaries.append({**summary, 'archive_run': path.parent.name})
    write_json(output / 'ensemble_rejections.json', rejected)
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
    write_ensemble_store(table, coverage, output)
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

def metar_daily(paths, cutoff=None):
    rows = []
    for path in sorted(paths):
        data = json.loads(Path(path).read_text())
        if not isinstance(data, list):
            raise ValueError(f'Unexpected METAR response: {path}')
        meta_path = Path(path).with_suffix(Path(path).suffix + '.meta.json')
        receipt = json.loads(meta_path.read_text())['retrieved_at_utc'] if meta_path.exists() else None
        if cutoff is not None and (receipt is None or utc_timestamp(receipt) > utc_timestamp(cutoff)):
            continue
        rows.extend({**row, 'source': 'METAR', 'raw_file': relative_path(Path(path)),
                     'retrieved_at_utc': receipt} for row in data)
    obs = pd.DataFrame(rows)
    obs = obs[obs.icaoId == ICAO].copy()
    obs['time_utc'] = pd.to_datetime(obs.obsTime, unit='s', utc=True)
    obs['receipt_utc'] = pd.to_datetime(obs.receiptTime, utc=True)
    obs = obs.sort_values(['receipt_utc', 'retrieved_at_utc', 'raw_file'], kind='stable').drop_duplicates('time_utc', keep='last').sort_values('time_utc')
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

def build(cutoff=None):
    OUT.mkdir(parents=True, exist_ok=True)
    errors = {}
    try:
        build_ensemble_archive()
    except Exception as exc:
        errors['ensembles'] = str(exc)
    ghcn = metar = comparison = pd.DataFrame()
    selected = {}
    try:
        path, selected = select_ghcn(cutoff=cutoff)
        ghcn, audit = ghcn_daily(path)
        meta = response_metadata(path)
        for frame in (ghcn, audit):
            frame['receipt_id'] = receipt_key(path)
            frame['retrieved_at_utc'] = meta['retrieved_at_utc']
            frame['source'] = 'GHCN-Daily'
        ghcn.to_csv(OUT / 'ghcn_daily.csv')
        monthly(ghcn).to_csv(OUT / 'ghcn_monthly.csv')
        audit.to_csv(OUT / 'ghcn_quality_audit.csv', index=False)
    except Exception as exc:
        errors['ghcn'] = str(exc)
    paths, rejected = valid_observation_paths('metar', cutoff)
    try:
        if not paths:
            raise ValueError('No validated METAR downloads')
        metar, observations = metar_daily(paths, cutoff)
        metar.to_csv(OUT / 'metar_daily.csv')
        monthly(metar).to_csv(OUT / 'metar_monthly.csv')
        observations['receipt_id'] = observations.raw_file.map(lambda value: receipt_key(Path(value) if Path(value).is_absolute() else ROOT / value))
        observations.drop(columns='raw_file').to_csv(OUT / 'metar_observations.csv', index=False)
    except Exception as exc:
        errors['metar'] = str(exc)
    if not ghcn.empty and not metar.empty:
        comparison = ghcn[['tmean_c']].join(metar[['tmean_c']], how='inner', lsuffix='_ghcn', rsuffix='_metar')
        comparison['difference_c'] = comparison.tmean_c_metar - comparison.tmean_c_ghcn
        comparison.to_csv(OUT / 'daily_comparison.csv')
    write_json(OUT / 'observation_selection.json', {'ghcn': selected, 'excluded_metar': rejected, 'errors': errors, 'cutoff': cutoff})
    health_report()
    if errors:
        raise RuntimeError(f'Partial build: {errors}; successful outputs retained')
    return ghcn, metar, comparison


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, default=str, allow_nan=False))
    temp.replace(path)


def relative_path(path):
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def receipt_key(path):
    return hashlib.sha256(relative_path(path).encode()).hexdigest()[:24]


def response_metadata(path):
    meta = json.loads(path.with_suffix(path.suffix + '.meta.json').read_text())
    utc_timestamp(meta['retrieved_at_utc'])
    if not 200 <= meta['status'] < 300:
        raise ValueError(f'HTTP {meta["status"]}')
    if hashlib.sha256(path.read_bytes()).hexdigest() != meta['sha256']:
        raise ValueError('Response checksum mismatch')
    return meta


def validate_download(path, source):
    """A receipt alone is insufficient: reject wrong station, shape and empty payloads."""
    path = Path(path)
    meta = response_metadata(path)
    if source == 'ghcn':
        lines = path.read_text().splitlines()
        if not lines:
            raise ValueError('Empty GHCN response')
        elements = set()
        for line in lines:
            if len(line) != 269 or line[:11] != GHCN_ID:
                raise ValueError('Malformed GHCN fixed-width record or wrong station')
            year, month = int(line[11:15]), int(line[15:17])
            calendar.monthrange(year, month)
            for offset in range(21, 269, 8):
                int(line[offset:offset + 5])
            elements.add(line[17:21])
        if not {'TMIN', 'TMAX'} <= elements:
            raise ValueError('Missing GHCN extrema elements')
        daily, _ = ghcn_daily(path)
        if not daily.valid_day.any():
            raise ValueError('No usable GHCN daily temperatures')
        return meta
    data = json.loads(path.read_text())
    if source == 'metar':
        if not isinstance(data, list) or not data:
            raise ValueError('METAR must be a nonempty record array')
        for row in data:
            if not isinstance(row, dict) or row.get('icaoId') != ICAO:
                raise ValueError('Unexpected METAR record/station')
            if isinstance(row.get('obsTime'), bool) or not isinstance(row.get('obsTime'), (int, float)) or not math.isfinite(row['obsTime']):
                raise ValueError('Invalid METAR observation timestamp')
            pd.to_datetime(row['obsTime'], unit='s', utc=True, errors='raise')
            utc_timestamp(row['receiptTime'])
            if 'temp' not in row:
                raise ValueError('Missing METAR temperature field')
            value = row['temp']
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError('Invalid METAR temperature field')
        if not any(pd.notna(r['obsTime']) for r in data):
            raise ValueError('Missing observation timestamps')
    elif source == 'taf':
        if not isinstance(data, list) or not data:
            raise ValueError('TAF must be a nonempty record array')
        for row in data:
            if row.get('icaoId') != ICAO or not row.get('rawTAF') or not row.get('fcsts'):
                raise ValueError('Invalid TAF station/report/forecast periods')
            for key in ('validTimeFrom', 'validTimeTo'):
                if not isinstance(row.get(key), (int, float)):
                    raise ValueError('Missing TAF validity time')
    elif source == 'nws_point':
        url = data['properties']['forecastHourly']
        if not isinstance(url, str) or not url.startswith('https://api.weather.gov/'):
            raise ValueError('Invalid NWS hourly link')
    elif source == 'nws_hourly':
        periods = data['properties']['periods']
        if not isinstance(periods, list) or not periods:
            raise ValueError('Missing NWS periods')
        for period in periods:
            utc_timestamp(period['startTime']); utc_timestamp(period['endTime'])
            if not isinstance(period.get('temperature'), (int, float)):
                raise ValueError('Missing NWS temperature')
    elif source.startswith('ensemble_'):
        table, _ = parse_ensemble(data, source[len('ensemble_'):], meta['retrieved_at_utc'])
        if not table.temperature_c.notna().any():
            raise ValueError('All-null ensemble response')
    return meta


def observation_candidates(source):
    if source == 'ghcn':
        return list((RAW / 'ghcn').glob('*.dly'))
    return [p for p in list((RAW / 'metar').glob('*.json')) + list((ROOT / 'data/forecasts').glob('*/*_metar*.json'))
            if not p.name.endswith(('.meta.json', '.attempt.json'))]


def valid_observation_paths(source, cutoff=None):
    valid, rejected = [], []
    for path in observation_candidates(source):
        try:
            meta = validate_download(path, source)
            if cutoff and utc_timestamp(meta['retrieved_at_utc']) > utc_timestamp(cutoff):
                continue
            valid.append((utc_timestamp(meta['retrieved_at_utc']), str(path), path))
        except Exception as exc:
            rejected.append({'raw_file': relative_path(path), 'error': str(exc)})
    return [p for _, _, p in sorted(valid)], rejected


def select_ghcn(cutoff=None):
    paths, rejected = valid_observation_paths('ghcn', cutoff)
    if not paths:
        raise ValueError(f'No successful structurally valid GHCN snapshot; {rejected}')
    path = paths[-1]
    receipt = utc_timestamp(response_metadata(path)['retrieved_at_utc'])
    newer_rejections = []
    for row in rejected:
        candidate = Path(row['raw_file'])
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        try:
            timestamp = utc_timestamp(json.loads(candidate.with_suffix(candidate.suffix + '.meta.json').read_text())['retrieved_at_utc'])
            if timestamp > receipt and (cutoff is None or timestamp <= utc_timestamp(cutoff)):
                newer_rejections.append(row)
        except Exception:
            # Unknown receipt cannot be ordered; still exposed in the rejection list.
            pass
    return path, {'raw_file': relative_path(path), 'retrieved_at_utc': receipt.isoformat(),
                  'age_hours': (pd.Timestamp(now()) - receipt).total_seconds() / 3600,
                  'fallback': bool(newer_rejections), 'newer_unusable': newer_rejections, 'excluded': rejected}


def ghcn_due():
    try:
        _, selection = select_ghcn()
        return selection['age_hours'] >= 24
    except ValueError:
        return True


ENSEMBLE_VALUE_COLUMNS = ['member_id', 'member_role', 'source_variable', 'target_time_utc',
                          'temperature_2m', 'units', 'temperature_c']


def write_ensemble_store(table, coverage, output):
    """Partition by model/snapshot; dimensions/receipts carry long provenance once."""
    metadata = []
    for (model, snapshot), frame in table.groupby(['model', 'snapshot_id'], sort=True):
        columns = [c for c in table.columns if c not in ENSEMBLE_VALUE_COLUMNS]
        for column in columns:
            if frame[column].nunique(dropna=False) != 1:
                raise ValueError(f'Nonconstant snapshot metadata: {column}')
        metadata.append(frame[columns].iloc[0].to_dict())
        destination = output / 'ensemble_temperature' / f'model={model}' / f'snapshot_id={snapshot}' / 'values.parquet'
        destination.parent.mkdir(parents=True, exist_ok=True)
        values = frame[ENSEMBLE_VALUE_COLUMNS].reset_index(drop=True)
        temp = destination.with_suffix('.tmp')
        values.to_parquet(temp, index=False, compression='zstd')
        restored = pd.read_parquet(temp)
        pd.testing.assert_frame_equal(values, restored, check_exact=True)
        temp.replace(destination)
    pd.DataFrame(metadata).to_parquet(output / 'ensemble_snapshots.parquet', index=False)
    coverage.to_parquet(output / 'ensemble_receipts.parquet', index=False)
    write_json(output / 'ensemble_schema.json', {'columns': list(table.columns), 'version': 1})
    restored = load_ensemble_archive(output)
    keys = ['snapshot_id', 'member_id', 'target_time_utc']
    pd.testing.assert_frame_equal(table.sort_values(keys).reset_index(drop=True),
                                  restored.sort_values(keys).reset_index(drop=True), check_exact=True)
    pd.testing.assert_frame_equal(coverage.reset_index(drop=True), pd.read_parquet(output / 'ensemble_receipts.parquet').reset_index(drop=True), check_exact=True)
    write_json(output / 'storage_verification.json', {'rows': len(table), 'snapshots': len(metadata),
               'receipts': len(coverage), 'null_temperatures': int(table.temperature_c.isna().sum()),
               'exact_round_trip': True})


def load_ensemble_archive(output=None):
    output = Path(output) if output else OUT
    metadata = pd.read_parquet(output / 'ensemble_snapshots.parquet')
    frames = []
    # Read only indexed partitions, never stale/orphaned files from interrupted builds.
    for row in metadata.to_dict('records'):
        path = output / 'ensemble_temperature' / f'model={row["model"]}' / f'snapshot_id={row["snapshot_id"]}' / 'values.parquet'
        frame = pd.read_parquet(path)
        for key, value in row.items():
            frame[key] = value
        frames.append(frame)
    columns = json.loads((output / 'ensemble_schema.json').read_text())['columns']
    return pd.concat(frames, ignore_index=True)[columns]


def classify_response(path):
    name = path.name
    if path.suffix == '.dly': return 'ghcn'
    for source in ['ensemble_ecmwf_ifs025', 'ensemble_gfs025', 'nws_point', 'nws_hourly', 'taf', 'metar']:
        if re.search('_' + source + r'(?:_failed)?\.json$', name): return source
    if path.parent.name == 'metar' and ICAO in name: return 'metar'
    return None


def health_report(grace_hours=2, at=None):
    """Reconstruct health from raw receipts, including failures and unchanged polls."""
    if grace_hours < 0:
        raise ValueError('Grace period must be nonnegative')
    at = utc_timestamp(at) if at else pd.Timestamp(now())
    sources = ['ghcn', 'metar', 'nws_point', 'nws_hourly', 'taf', 'ensemble_gfs025', 'ensemble_ecmwf_ifs025']
    events = {s: [] for s in sources}
    raw_roots = [RAW, ROOT / 'data/forecasts']
    observation_receipts = []
    for directory in raw_roots:
        for path in directory.rglob('*.meta.json'):
            raw = path.with_suffix('').with_suffix('')
            source = classify_response(raw)
            if source is None: continue
            meta = json.loads(path.read_text())
            event = {'time': meta['retrieved_at_utc'], 'raw_file': relative_path(raw), 'error': None,
                     'coverage_end': None, 'warnings': [], 'identity': None}
            try:
                validate_download(raw, source)
                event['identity'] = meta['sha256']
                if source == 'ghcn':
                    frame, _ = ghcn_daily(raw)
                    event['coverage_end'] = frame.index[frame.valid_day].max().date().isoformat()
                else:
                    data = json.loads(raw.read_text())
                    if source == 'metar':
                        event['coverage_end'] = pd.to_datetime(max(r['obsTime'] for r in data), unit='s', utc=True).isoformat()
                    elif source == 'nws_hourly':
                        event['coverage_end'] = utc_timestamp(data['properties']['periods'][-1]['endTime']).isoformat()
                        event['identity'] = hashlib.sha256(json.dumps(data['properties']['periods'], sort_keys=True).encode()).hexdigest()
                    elif source == 'taf':
                        event['coverage_end'] = pd.to_datetime(max(r['validTimeTo'] for r in data), unit='s', utc=True).isoformat()
                        event['identity'] = hashlib.sha256(json.dumps([r['rawTAF'] for r in data], sort_keys=True).encode()).hexdigest()
                    elif source.startswith('ensemble_'):
                        _, info = parse_ensemble(data, source[9:], meta['retrieved_at_utc'])
                        event['identity'] = info['snapshot_id']
                        event['coverage_end'] = info['valid_end_utc']
                        summary_path = raw.parent / (source + '_summary.json')
                        summary = json.loads(summary_path.read_text()) if summary_path.exists() else info
                        event['warnings'] = summary['warnings']
            except Exception as exc:
                event['error'] = str(exc)
            events[source].append(event)
            if source in ('ghcn', 'metar'):
                observation_receipts.append({'receipt_id': receipt_key(raw), 'source': source,
                    'raw_file': relative_path(raw), 'retrieved_at_utc': event['time'],
                    'status': meta.get('status'), 'sha256': meta.get('sha256'),
                    'validated': event['error'] is None, 'error': event['error'],
                    'observation_coverage_end': event['coverage_end']})
        for path in directory.rglob('*.attempt.json'):
            entry = json.loads(path.read_text())
            source = classify_response(Path(entry['candidate']))
            if source:
                events[source].append({'time': entry['attempted_at_utc'], 'error': entry['error'], 'raw_file': None})
    # Source manifests also capture structural/dependency errors without an HTTP response.
    for path in list((ROOT / 'data/forecasts').glob('*/manifest.json')) + list((RAW / 'refreshes').glob('*.json')):
        manifest = json.loads(path.read_text())
        for name, error in manifest.get('errors', {}).items():
            source = 'metar' if name.startswith('metar_') else name
            if source in events:
                events[source].append({'time': manifest.get('completed_at_utc', manifest['started_at_utc']), 'error': error, 'raw_file': relative_path(path)})
    report = {}
    for source, history in events.items():
        history.sort(key=lambda e: utc_timestamp(e['time']))
        successes = [e for e in history if e['error'] is None]
        last = history[-1] if history else None
        success = successes[-1] if successes else None
        interval = 24 if source == 'ghcn' else 6
        age = lambda e: (at - utc_timestamp(e['time'])).total_seconds()/3600 if e else None
        changes = [e for i, e in enumerate(successes) if i == 0 or e['identity'] != successes[i-1]['identity']]
        gaps = []
        for previous, current in zip(successes, successes[1:]):
            hours = (utc_timestamp(current['time']) - utc_timestamp(previous['time'])).total_seconds()/3600
            if hours > interval + grace_hours:
                gaps.append({'from_utc': previous['time'], 'to_utc': current['time'], 'hours': hours})
        report[source] = {'last_attempt': last['time'] if last else None,
            'last_successful_retrieval': success['time'] if success else None,
            'last_changed_forecast_snapshot': changes[-1]['time'] if changes and source not in ('ghcn', 'metar', 'nws_point') else None,
            'coverage_end': success['coverage_end'] if success else None,
            'observation_coverage_end': success['coverage_end'] if success and source in ('ghcn', 'metar') else None,
            'raw_file': success['raw_file'] if success else None,
            'last_attempt_error': last['error'] if last else 'No recorded attempt',
            'errors': [e for e in history if e['error']][-10:],
            'publication_warnings': success['warnings'] if success else [],
            'unchanged_since_previous_success': len(successes) > 1 and successes[-1]['identity'] == successes[-2]['identity'],
            'missed_collection': last is None or age(last) > interval + grace_hours,
            'stale_success': success is None or age(success) > interval + grace_hours,
            'successful_receipts': len(successes), 'recorded_events': len(history),
            'successful_retrieval_gaps': gaps,
            'expected_interval_hours': interval, 'grace_hours': grace_hours}
    OUT.mkdir(parents=True, exist_ok=True)
    if observation_receipts:
        pd.DataFrame(observation_receipts).sort_values(['retrieved_at_utc', 'raw_file']).to_csv(OUT / 'observation_receipts.csv', index=False)
    write_json(OUT / 'source_health.json', {'checked_at_utc': at.isoformat(), 'sources': report})
    pd.DataFrame.from_dict(report, orient='index').rename_axis('source').to_csv(OUT / 'source_health.csv')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['refresh', 'archive', 'ensembles', 'build', 'all', 'health'])
    parser.add_argument('--grace-hours', type=float, default=float(os.getenv('WEATHER_GRACE_HOURS', '2')))
    parser.add_argument('--cutoff', help='UTC retrieval cutoff for observation rebuild (use with --output)')
    parser.add_argument('--output', type=Path, help='Separate derived output directory, e.g. for cutoff rebuild')
    args = parser.parse_args()
    if args.cutoff and not args.output:
        parser.error('--cutoff requires --output to protect current tables')
    if args.output:
        OUT = args.output
    failures = []
    if args.action in ('refresh', 'all'):
        try:
            print(refresh_observations())
        except Exception as exc:
            failures.append(str(exc))
    if args.action in ('archive', 'ensembles', 'all'):
        try:
            archive_forecasts(ensembles_only=args.action == 'ensembles')
        except Exception as exc:
            failures.append(str(exc))
    if args.action != 'health':
        try:
            build(cutoff=args.cutoff)
        except Exception as exc:
            failures.append(str(exc))
    report = health_report(grace_hours=args.grace_hours)
    print(json.dumps({'health': report, 'errors': failures}, indent=2))
    if failures:
        raise SystemExit(1)
