"""Offline JFK forecast verification. See EVALUATION.md for contracts and evidence."""
from dataclasses import asdict, dataclass
from pathlib import Path
import argparse
import hashlib
import json
import re
import sys

import numpy as np
import pandas as pd
import weather_pipeline as archive

ROOT = Path(__file__).resolve().parent
MODELS = ('nws', 'gfs025', 'ecmwf_ifs025')
EXPECTED = {'nws': {0}, 'gfs025': set(range(31)), 'ecmwf_ifs025': set(range(51))}
BINS = [0, 24, 48, 72, 120, 168, 240, 360, np.inf]
LABELS = ['0–24h', '24–48h', '48–72h', '3–5d', '5–7d', '7–10d', '10–15d', '>15d']
KEY = ['model', 'snapshot_id', 'cutoff', 'target_time', 'quantity']


@dataclass(frozen=True)
class Config:
    as_of: str = '2026-09-21T00:30:00Z'
    start: str = '2026-09-17T00:00:00Z'
    cutoff_hours: tuple = (0, 6, 12, 18)
    tolerance_minutes: float = 15
    max_age_hours: float = 24
    persistence_max_age_hours: float = 6
    degree_day_base_f: float = 65

    def __post_init__(self):
        if utc(self.start) > utc(self.as_of):
            raise ValueError('start must precede as_of')
        if not 0 <= self.tolerance_minutes < 30:
            raise ValueError('Tolerance must be less than half the hourly spacing')
        if not self.cutoff_hours or any(h not in range(24) for h in self.cutoff_hours):
            raise ValueError('Cutoff hours must be UTC integer hours 0..23')
        if self.max_age_hours <= 0 or self.persistence_max_age_hours <= 0:
            raise ValueError('Age limits must be positive')


def utc(value):
    return archive.utc_timestamp(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def day_hours(date, timezone='Etc/GMT+5'):
    """Half-open day, localized boundaries separately; supports 23/25h civil days."""
    d = pd.Timestamp(date).tz_localize(None).normalize()
    a = d.tz_localize(timezone).tz_convert('UTC')
    b = (d + pd.Timedelta(days=1)).tz_localize(timezone).tz_convert('UTC')
    return pd.date_range(a, b, freq='h', inclusive='left')


def cutoffs(config):
    return [d + pd.Timedelta(hours=h)
            for d in pd.date_range(utc(config.start).normalize(), utc(config.as_of).normalize(), freq='D')
            for h in sorted(set(config.cutoff_hours))
            if utc(config.start) <= d + pd.Timedelta(hours=h) <= utc(config.as_of)]


def load_forecasts(root, config):
    """Reparse authenticated raw responses, never forecast-like observation tables."""
    values, receipts, exclusions = {}, [], []
    for model in MODELS:
        stem = 'nws_hourly' if model == 'nws' else 'ensemble_' + model
        for path in sorted((root / 'data/forecasts').glob(f'*/*_{stem}.json')):
            try:
                meta = archive.validate_download(path, stem)
                retrieved = utc(meta['retrieved_at_utc'])
                if retrieved > utc(config.as_of):
                    exclusions.append(dict(scope='archive', model=model, raw_file=str(path.relative_to(root)), reason='retrieved_after_as_of'))
                    continue
                data = json.loads(path.read_text())
                if model == 'nws':
                    rows = []
                    for p in data['properties']['periods']:
                        start, end = utc(p['startTime']), utc(p['endTime'])
                        if end - start != pd.Timedelta(hours=1) or start != start.floor('h'):
                            raise ValueError('ambiguous_timing: NWS non-hourly interval')
                        if p['temperatureUnit'] not in ('C', 'F'):
                            raise ValueError('Unsupported NWS temperature unit')
                        t = p['temperature']
                        rows.append(dict(target_time=start, target_end=end, member_id=0,
                                         forecast=(t - 32) / 1.8 if p['temperatureUnit'] == 'F' else t))
                    frame = pd.DataFrame(rows)
                    if frame.target_time.duplicated().any():
                        raise ValueError('ambiguous_timing: duplicate target')
                    sid = digest({'model': model, 'geometry': data.get('geometry'),
                                  'rows': frame.astype(str).to_dict('records')})
                    semantics = 'NWS one-hour period start; point-match convention'
                else:
                    frame, summary = archive.parse_ensemble(data, model, retrieved)
                    sid = summary['snapshot_id']
                    frame = frame.rename(columns={'target_time_utc': 'target_time', 'temperature_c': 'forecast'})
                    frame = frame[['target_time', 'member_id', 'forecast']]
                    frame['target_end'] = frame.target_time
                    semantics = 'Open-Meteo instantaneous 2m temperature; may be interpolated'
                frame['model'], frame['snapshot_id'] = model, sid
                frame['timestamp_semantics'] = semantics
                values[(model, sid)] = frame
                receipts.append(dict(model=model, snapshot_id=sid, retrieval_time=retrieved,
                    raw_file=str(path.relative_to(root)), sha256=meta['sha256'], source_url=meta['url'],
                    initialization_time=None, lead_basis='first_content_retrieval'))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                exclusions.append(dict(scope='archive', model=model, raw_file=str(path.relative_to(root)),
                                       reason='ambiguous_timing' if 'timing' in str(exc) else 'invalid_archive', detail=str(exc)))
    cols = ['model', 'snapshot_id', 'retrieval_time', 'raw_file', 'sha256', 'source_url', 'initialization_time', 'lead_basis']
    receipts = pd.DataFrame(receipts, columns=cols).sort_values(['retrieval_time', 'raw_file'])
    # Deduplicate BEFORE age/selection: unchanged polling cannot renew the forecast.
    snapshots = receipts.drop_duplicates(['model', 'snapshot_id'], keep='first').copy()
    return values, receipts, snapshots, exclusions


def select_snapshot(snapshots, model, cutoff, max_age_hours):
    eligible = snapshots[(snapshots.model == model) & (snapshots.retrieval_time <= cutoff)]
    if eligible.empty:
        return None, 'no_snapshot_by_cutoff'
    selected = eligible.sort_values(['retrieval_time', 'snapshot_id']).iloc[-1]
    if cutoff - selected.retrieval_time > pd.Timedelta(hours=max_age_hours):
        return None, 'stale_forecast'
    return selected, ''


def screen_metar(row):
    """Transparent basic screening, not an undocumented interpretation of qcField."""
    t = row.get('temp')
    if t is None or not np.isfinite(t):
        return 'missing_temperature'
    if not -60 <= t <= 55:
        return 'temperature_out_of_range'
    dew = row.get('dewp')
    if dew is not None and np.isfinite(dew) and dew > t + 0.5:
        return 'dewpoint_above_temperature'
    raw = row.get('rawOb', '')
    precise = re.search(r'\bT([01])(\d{3})[01]\d{3}\b', raw)
    coarse = re.search(r'\b(M?\d{2})/(?:M?\d{2}|//)\b', raw)
    if precise:
        rt = (-1 if precise[1] == '1' else 1) * int(precise[2]) / 10
        tolerance = 0.11
    elif coarse:
        rt = float(coarse[1].replace('M', '-'))
        tolerance = 0.61
    else:
        return 'unverifiable_raw_temperature'
    return 'basic_screen_pass' if abs(rt - t) <= tolerance else 'raw_temperature_mismatch'


def load_metar(root, config):
    rows, exclusions = [], []
    paths = list((root / 'data/raw/metar').glob('*.json')) + list((root / 'data/forecasts').glob('*/*_metar.json'))
    for path in sorted(paths):
        if path.name.endswith(('.meta.json', '.attempt.json')):
            continue
        try:
            meta = archive.validate_download(path, 'metar')
            retrieved = utc(meta['retrieved_at_utc'])
            if retrieved > utc(config.as_of):
                continue
            for r in json.loads(path.read_text()):
                observed, receipt = pd.to_datetime(r['obsTime'], unit='s', utc=True), utc(r['receiptTime'])
                quality = screen_metar(r)
                if observed > retrieved or receipt > retrieved or receipt < observed:
                    quality = 'ambiguous_timing'
                rows.append(dict(observation_time=observed, observation=r['temp'], upstream_receipt=receipt,
                    observation_retrieval=retrieved, available_at=max(retrieved, receipt, observed),
                    observation_source='KJFK METAR', quality_status=quality, qc_field=r.get('qcField'),
                    raw_report=r.get('rawOb'), observation_file=str(path.relative_to(root)),
                    observation_sha256=meta['sha256']))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            exclusions.append(dict(scope='observation_archive', reason='invalid_archive', raw_file=str(path.relative_to(root)), detail=str(exc)))
    columns = ['observation_time', 'observation', 'upstream_receipt', 'observation_retrieval', 'available_at',
               'observation_source', 'quality_status', 'qc_field', 'raw_report', 'observation_file', 'observation_sha256']
    return pd.DataFrame(rows, columns=columns), exclusions


def observation_vintage(observations, at):
    """Resolve corrections AFTER filtering availability, then screen (no old-value fallback)."""
    f = observations[(observations.available_at <= at) & (observations.observation_time <= at)].copy()
    return (f.sort_values(['observation_time', 'upstream_receipt', 'observation_retrieval', 'observation_file'])
            .drop_duplicates('observation_time', keep='last').sort_values('observation_time'))


def match_hourly(targets, observations, tolerance_minutes):
    targets = targets.sort_values('target_time')
    obs = observations[observations.quality_status == 'basic_screen_pass'].sort_values('observation_time')
    if obs.empty:
        out = targets.copy()
        for c in observations.columns:
            out[c] = pd.NaT if c in ('observation_time', 'available_at', 'upstream_receipt', 'observation_retrieval') else np.nan
    else:
        # merge_asof resolves an equal-distance tie to the earlier observation.
        out = pd.merge_asof(targets, obs, left_on='target_time', right_on='observation_time',
                            tolerance=pd.Timedelta(minutes=tolerance_minutes), direction='nearest')
    out['time_offset_minutes'] = (out.observation_time - out.target_time).dt.total_seconds() / 60
    out['observation_source'] = out.observation_source.fillna('KJFK METAR')
    out['quality_status'] = out.quality_status.fillna('unmatched')
    out['quantity'] = 'hourly_temperature_c'
    out['target_date'] = out.target_time.dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d')
    return out


def persistence(observations, cutoff, max_age_hours):
    f = observation_vintage(observations, cutoff)
    f = f[(f.quality_status == 'basic_screen_pass') &
          (f.observation_time >= cutoff - pd.Timedelta(hours=max_age_hours))]
    return None if f.empty else f.iloc[-1]


def load_ghcn(root, config):
    candidates, exclusions = [], []
    for p in sorted((root / 'data/raw/ghcn').glob('*.dly')):
        try:
            m = archive.validate_download(p, 'ghcn')
            if utc(m['retrieved_at_utc']) <= utc(config.as_of):
                candidates.append((utc(m['retrieved_at_utc']), str(p), p, m))
        except (ValueError, KeyError, OSError) as exc:
            exclusions.append(dict(scope='observation_archive', raw_file=str(p.relative_to(root)), reason='invalid_archive', detail=str(exc)))
    if not candidates:
        return pd.DataFrame(), exclusions
    retrieved, _, path, meta = sorted(candidates)[-1]
    daily, audit = archive.ghcn_daily(path, start=str(utc(config.start).date()), end=str(utc(config.as_of).date()))
    daily = daily.reset_index()
    for element in ('TMAX', 'TMIN'):
        flags = audit[audit.element == element].set_index('date')
        for flag in ('sflag', 'qflag', 'mflag'):
            daily[element + '_' + flag] = daily.date.map(flags[flag])
    # Only CF6 source conventions have been verified for this evaluation.
    daily['timing_verified'] = daily.TMAX_sflag.isin(['1', 'D']) & daily.TMIN_sflag.isin(['1', 'D'])
    daily['observation_retrieval'] = retrieved
    daily['observation_file'] = str(path.relative_to(root))
    daily['observation_sha256'] = meta['sha256']
    daily['observation_source'] = 'GHCN-Daily USW00094789'
    daily['target_date'] = daily.date.dt.strftime('%Y-%m-%d')
    return daily, exclusions


def daily_quantities(low, high, base=65):
    midpoint = (low + high) / 2
    return {'tmin_proxy_c': low, 'tmax_proxy_c': high, 'midpoint_proxy_c': midpoint,
            'hdd_proxy_f_days': max(base - (midpoint * 1.8 + 32), 0),
            'cdd_proxy_f_days': max((midpoint * 1.8 + 32) - base, 0)}


def daily_matches(selected, ghcn, config):
    rows = []
    if selected.empty:
        return pd.DataFrame()
    daily_obs = ghcn.set_index('target_date') if not ghcn.empty else pd.DataFrame()
    keys = ['model', 'snapshot_id', 'cutoff', 'member_id']
    for _, f in selected.groupby(keys, sort=True):
        f = f.sort_values('target_time')
        dates = pd.date_range(f.target_time.min().tz_convert('Etc/GMT+5').date(),
                              f.target_time.max().tz_convert('Etc/GMT+5').date())
        for date in dates:
            hours = day_hours(date)
            day = f[f.target_time.isin(hours)]
            base = f.iloc[0].to_dict()
            base.update(target_time=hours[0], target_end=hours[-1] + pd.Timedelta(hours=1),
                        target_date=str(date.date()), expected_hours=len(hours), sampled_hours=len(day),
                        lead_hours=(hours[0] - base['retrieval_time']).total_seconds() / 3600,
                        cutoff_lead_hours=(hours[0] - base['cutoff']).total_seconds() / 3600,
                        daily_window='midnight EST to midnight EST (UTC-05 fixed)',
                        observation_source='GHCN-Daily USW00094789', quality_status='unmatched')
            reasons = []
            if hours[0] <= base['cutoff']:
                reasons.append('day_started_by_cutoff')
            if len(day) != len(hours) or set(day.target_time) != set(hours) or not np.isfinite(day.forecast).all():
                reasons.append('incomplete_day')
            if base['target_end'] > utc(config.as_of):
                reasons.append('day_not_finished_as_of')
            obs = daily_obs.loc[base['target_date']] if not daily_obs.empty and base['target_date'] in daily_obs.index else None
            if obs is None or not obs.valid_day:
                reasons.append('missing_observation')
            elif not obs.timing_verified:
                reasons.append('ambiguous_timing')
            if obs is not None:
                for c in ['observation_retrieval', 'observation_file', 'observation_sha256',
                          'TMAX_sflag', 'TMIN_sflag', 'TMAX_qflag', 'TMIN_qflag', 'TMAX_mflag', 'TMIN_mflag']:
                    base[c] = obs[c]
                base['quality_status'] = 'GHCN_QFLAG_blank' if obs.valid_day else 'GHCN_missing_or_QC_failed'
            complete = 'incomplete_day' not in reasons
            forecasts = daily_quantities(day.forecast.min(), day.forecast.max(), config.degree_day_base_f) if complete else dict.fromkeys(daily_quantities(0, 0), np.nan)
            outcomes = daily_quantities(obs.tmin_c, obs.tmax_c, config.degree_day_base_f) if obs is not None and obs.valid_day and obs.timing_verified else {}
            for quantity, forecast in forecasts.items():
                observation = outcomes.get(quantity, np.nan)
                rows.append(dict(base, quantity=quantity, forecast=forecast, observation=observation,
                    error=forecast - observation if not reasons else np.nan, exclusion_reason=';'.join(reasons),
                    observation_definition=quantity.replace('_proxy', ''), forecast_definition='hourly-sampled extrema proxy'))
    return pd.DataFrame(rows)


def ensemble_metrics(values, observation):
    x = np.asarray(values, dtype=float)
    if not len(x) or not np.isfinite(x).all() or not np.isfinite(observation):
        return dict(crps=np.nan, p10=np.nan, p90=np.nan, interval_coverage=np.nan, interval_width=np.nan)
    # Empirical (not fair-estimator) CRPS for equally weighted scenarios.
    crps = np.abs(x - observation).mean() - np.abs(x[:, None] - x[None, :]).mean() / 2
    lo, hi = np.quantile(x, [0.1, 0.9], method='linear')
    return dict(crps=float(crps), p10=lo, p90=hi,
                interval_coverage=float(lo <= observation <= hi), interval_width=hi - lo)


def summarize_scenarios(members):
    rows = []
    if members.empty:
        return pd.DataFrame()
    for _, f in members.groupby(KEY, sort=True):
        first = f.iloc[0].to_dict()
        model = first['model']
        complete = (set(f.member_id) == EXPECTED[model] and len(f) == len(EXPECTED[model])
                    and np.isfinite(f.forecast).all())
        reasons = set(filter(None, ';'.join(f.exclusion_reason.fillna('')).split(';')))
        if not complete:
            reasons.add('invalid_forecast' if model == 'nws' else 'missing_members')
        first.pop('member_id', None)
        first.update(expected_members=len(EXPECTED[model]), valid_members=int(np.isfinite(f.forecast).sum()),
                     complete_ensemble=complete, exclusion_reason=';'.join(sorted(reasons)))
        stats = ('deterministic',) if model == 'nws' else ('mean', 'median')
        for statistic in stats:
            forecast = (f.forecast.median() if statistic == 'median' else f.forecast.mean()) if complete else np.nan
            rows.append(dict(first, statistic=statistic, forecast=forecast,
                error=forecast - first['observation'] if not reasons else np.nan,
                **(ensemble_metrics(f.forecast, first['observation']) if model != 'nws' and not reasons
                   else ensemble_metrics([], np.nan))))
    return pd.DataFrame(rows)


def metrics(scores, common=False):
    columns = ['model', 'statistic', 'quantity', 'lead_bin', 'matched_targets', 'unique_target_dates',
               'forecast_snapshots', 'cutoffs', 'unique_target_times', 'bias', 'mae', 'rmse', 'crps', 'interval_coverage', 'interval_width']
    if scores.empty:
        return pd.DataFrame(columns=columns)
    good = scores[scores.error.notna()].copy()
    if common:
        # Comparable issuance cutoffs AND cutoff-relative horizon strata. Retrieval leads
        # differ by model, so using them for common bins would silently unpair targets.
        counts = good[good.model.isin(MODELS)].groupby(['cutoff', 'target_time', 'quantity']).model.nunique()
        pairs = counts[counts == len(MODELS)].reset_index()[['cutoff', 'target_time', 'quantity']]
        good = good.merge(pairs, on=['cutoff', 'target_time', 'quantity'])
    good['lead_bin'] = pd.cut(good.cutoff_lead_hours if common else good.lead_hours,
                              BINS, labels=LABELS, right=False)
    rows = []
    for group, f in good.groupby(['model', 'statistic', 'quantity', 'lead_bin'], observed=True):
        rows.append(dict(zip(columns[:4], group), matched_targets=len(f), unique_target_dates=f.target_date.nunique(),
            forecast_snapshots=f.snapshot_id.nunique(), cutoffs=f.cutoff.nunique(), unique_target_times=f.target_time.nunique(), bias=f.error.mean(),
            mae=f.error.abs().mean(), rmse=np.sqrt(np.square(f.error).mean()),
            crps=f.crps.mean(), interval_coverage=f.interval_coverage.mean(), interval_width=f.interval_width.mean()))
    return pd.DataFrame(rows, columns=columns)


def paired_persistence_metrics(scores):
    pairs = []
    for model in MODELS:
        baseline = scores[(scores.model == 'persistence_for_' + model) & scores.error.notna()]
        eligible = baseline[['cutoff', 'target_time', 'quantity']]
        model_scores = scores[scores.model == model].merge(eligible, on=['cutoff', 'target_time', 'quantity'])
        pairs.extend([baseline, model_scores])
    return metrics(pd.concat(pairs, ignore_index=True))


def verify_result(result):
    """Fail closed on leakage/duplicate scoring; these are archive-level invariants."""
    scores = result['scores']
    if scores.empty:
        return {'scored_rows': 0, 'invariants': 'passed (empty evaluation)'}
    good = scores[scores.error.notna()]
    assert not scores.duplicated(['model', 'statistic', 'cutoff', 'target_time', 'quantity']).any()
    assert (good.target_time > good.cutoff).all()
    assert (good.retrieval_time <= good.cutoff).all()
    assert (good.target_time <= utc(result['config']['as_of'])).all()
    assert (good.observation_retrieval <= utc(result['config']['as_of'])).all()
    baseline = good[good.statistic == 'persistence']
    if len(baseline):
        assert (baseline.predictor_available_at <= baseline.cutoff).all()
        assert (baseline.predictor_time <= baseline.cutoff).all()
    ensembles = good[good.model.isin(['gfs025', 'ecmwf_ifs025'])]
    assert ensembles.complete_ensemble.all()
    assert (ensembles.expected_members == ensembles.valid_members).all()
    return {'scored_rows': len(good), 'invariants': 'passed'}


def evaluate(root=ROOT, config=None):
    config = config or Config()
    root = Path(root)
    values, receipts, snapshots, exclusions = load_forecasts(root, config)
    observations, rejected = load_metar(root, config)
    exclusions.extend(rejected)
    ghcn, rejected = load_ghcn(root, config)
    exclusions.extend(rejected)
    outcomes = observation_vintage(observations, utc(config.as_of))
    chosen, selection = [], []
    for cutoff in cutoffs(config):
        for model in MODELS:
            snap, reason = select_snapshot(snapshots, model, cutoff, config.max_age_hours)
            selection.append(dict(model=model, cutoff=cutoff, reason=reason or 'selected',
                                  snapshot_id=None if snap is None else snap.snapshot_id))
            if snap is None:
                continue
            f = values[(model, snap.snapshot_id)].copy()
            for key in ['retrieval_time', 'raw_file', 'sha256', 'source_url', 'lead_basis']:
                f[key] = snap[key]
            f['cutoff'] = cutoff
            f['lead_hours'] = (f.target_time - snap.retrieval_time).dt.total_seconds() / 3600
            f['cutoff_lead_hours'] = (f.target_time - cutoff).dt.total_seconds() / 3600
            chosen.append(f)
    selected = pd.concat(chosen, ignore_index=True) if chosen else pd.DataFrame()
    hourly = match_hourly(selected, outcomes, config.tolerance_minutes) if len(selected) else pd.DataFrame()
    if len(hourly):
        reasons = []
        for r in hourly.itertuples():
            reason = []
            if r.target_time <= r.cutoff:
                reason.append('target_not_future_at_cutoff')
            if r.target_time > utc(config.as_of):
                reason.append('target_after_as_of')
            if not np.isfinite(r.forecast):
                reason.append('invalid_forecast')
            if pd.isna(r.observation):
                reason.append('missing_observation')
            reasons.append(';'.join(reason))
        hourly['exclusion_reason'] = reasons
        hourly['error'] = (hourly.forecast - hourly.observation).where(hourly.exclusion_reason == '')
    daily = daily_matches(selected, ghcn, config)
    hourly_scores, daily_scores = summarize_scenarios(hourly), summarize_scenarios(daily)
    baseline = []
    if not hourly_scores.empty:
        # One baseline per model/cutoff/target keeps each individual comparison paired.
        for cutoff, f in hourly_scores[hourly_scores.statistic.isin(['mean', 'deterministic'])].groupby('cutoff'):
            past = persistence(observations, cutoff, config.persistence_max_age_hours)
            for _, row in f.iterrows():
                b = row.to_dict()
                b['model'] = 'persistence_for_' + row.model
                b['statistic'] = 'persistence'
                b['forecast'] = np.nan if past is None else past.observation
                b['predictor_available_at'] = pd.NaT if past is None else past.available_at
                b['predictor_time'] = pd.NaT if past is None else past.observation_time
                b['predictor_file'] = None if past is None else past.observation_file
                b['predictor_sha256'] = None if past is None else past.observation_sha256
                b['exclusion_reason'] = ';'.join(filter(None, [row.exclusion_reason, 'no_available_persistence_input' if past is None else '']))
                b['error'] = b['forecast'] - row.observation if not b['exclusion_reason'] else np.nan
                b.update(ensemble_metrics([], np.nan))
                baseline.append(b)
    baseline = pd.DataFrame(baseline)
    scores = pd.concat([hourly_scores, daily_scores, baseline], ignore_index=True)
    coverage, target_exclusions = [], []
    if len(scores):
        for (model, statistic, quantity), f in scores.groupby(['model', 'statistic', 'quantity']):
            m = f[f.error.notna()]
            coverage.append(dict(model=model, statistic=statistic, quantity=quantity, candidate_targets=len(f),
                matched_targets=len(m), unique_target_dates=m.target_date.nunique(), forecast_snapshots=m.snapshot_id.nunique(),
                unique_target_times=m.target_time.nunique(), scored_cutoffs=m.cutoff.nunique(),
                matched_start=m.target_time.min(), matched_end=m.target_time.max()))
            for reason, count in f.exclusion_reason.str.split(';').explode().value_counts().items():
                if reason:
                    target_exclusions.append(dict(scope='target', model=model, statistic=statistic, quantity=quantity, reason=reason, count=int(count)))
    selection = pd.DataFrame(selection)
    selection_counts = selection[selection.reason != 'selected'].groupby(['model', 'reason']).size().reset_index(name='count')
    selection_counts['scope'] = 'cutoff'
    exclusion_counts = pd.concat([pd.DataFrame(target_exclusions), selection_counts], ignore_index=True)
    return dict(config=asdict(config), forecast_receipts=receipts, forecast_snapshots=snapshots,
        selection=selection, metar_vintages=observations, metar_as_of=outcomes, ghcn_as_of=ghcn,
        hourly_members=hourly, daily_members=daily, scores=scores, coverage=pd.DataFrame(coverage),
        metrics_individual=metrics(scores), metrics_common=metrics(scores[scores.model.isin(MODELS)], common=True) if len(scores) else metrics(scores),
        metrics_persistence_paired=paired_persistence_metrics(scores) if len(scores) else metrics(scores),
        exclusion_counts=exclusion_counts, archive_exclusions=pd.DataFrame(exclusions))


def write_results(result, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, frame in result.items():
        if isinstance(frame, pd.DataFrame):
            if name in ('hourly_members', 'daily_members', 'scores', 'metar_vintages'):
                frame.to_parquet(output / (name + '.parquet'), index=False)
            else:
                frame.to_csv(output / (name + '.csv'), index=False)
    inputs = result['forecast_receipts'][['raw_file', 'sha256']].to_dict('records')
    for name in ('metar_vintages', 'ghcn_as_of'):
        f = result[name]
        if not f.empty:
            inputs += f[['observation_file', 'observation_sha256']].drop_duplicates().rename(
                columns={'observation_file': 'raw_file', 'observation_sha256': 'sha256'}).to_dict('records')
    inputs = sorted({x['raw_file']: x for x in inputs}.values(), key=lambda x: x['raw_file'])
    for item in inputs:
        receipt = ROOT / (item['raw_file'] + '.meta.json')
        if receipt.exists():
            item['receipt_sha256'] = hashlib.sha256(receipt.read_bytes()).hexdigest()
    report = dict(config=result['config'], input_files=inputs, verification=verify_result(result),
        environment=dict(python=sys.version, pandas=pd.__version__, numpy=np.__version__),
        code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        parser_sha256=hashlib.sha256(Path(archive.__file__).read_bytes()).hexdigest(),
        forecast_receipts=len(result['forecast_receipts']), forecast_snapshots=len(result['forecast_snapshots']),
        receipt_start=str(result['forecast_receipts'].retrieval_time.min()), receipt_end=str(result['forecast_receipts'].retrieval_time.max()),
        coverage=result['coverage'].astype(str).to_dict('records'),
        limitations=['Preliminary descriptive evaluation only; dependent hours and cutoffs are not independent samples.',
                     'No calibrated interval claim, model winner, fitted correction, pricing, or monthly totals.'])
    (output / 'run_manifest.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--as-of', default=Config.as_of)
    p.add_argument('--start', default=Config.start)
    p.add_argument('--cutoff-hours', default='0,6,12,18')
    p.add_argument('--tolerance-minutes', type=float, default=15)
    p.add_argument('--max-age-hours', type=float, default=24)
    p.add_argument('--output', type=Path, default=ROOT / 'data/evaluation')
    a = p.parse_args()
    cfg = Config(as_of=a.as_of, start=a.start, cutoff_hours=tuple(map(int, a.cutoff_hours.split(','))),
                 tolerance_minutes=a.tolerance_minutes, max_age_hours=a.max_age_hours)
    result = evaluate(config=cfg)
    write_results(result, a.output)
    print(result['coverage'].to_string(index=False))
