"""Offline retrospective chronological October JFK HDD65 baseline tests."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
import argparse
import hashlib
import json

import numpy as np
import pandas as pd
import weather_pipeline as archive
import historical_payout_analysis as history
from forecast_evaluation import ensemble_metrics

ROOT = Path(__file__).resolve().parent
METHODS = ('expanding', 'recent_10', 'recent_20', 'temperature_trend')
# Fixed diagnostic assumptions, not selected by test performance.
SENSITIVITY = ('trend_half_shift', 'trend_recent_20')
LABEL = 'Retrospective chronological tests using revised observations from a retained snapshot'


@dataclass(frozen=True)
class Config:
    as_of: str = history.Config.as_of
    start_year: int = 2000
    min_training_years: int = 10
    strike: float | None = None

    def __post_init__(self):
        history.Config(as_of=self.as_of, start_year=self.start_year, strike=self.strike)
        if isinstance(self.min_training_years, bool) or not isinstance(self.min_training_years, int) or self.min_training_years < 2:
            raise ValueError('min_training_years must be an integer >= 2')
        if self.strike is not None:
            object.__setattr__(self, 'strike', float(self.strike))


def training_years(yearly, prediction_year, method, config):
    prior = yearly[yearly.year.between(config.start_year, prediction_year - 1)]
    window = 10 if method == 'recent_10' else 20 if method in ('recent_20', 'trend_recent_20') else None
    first = max(config.start_year, prediction_year - window) if window else config.start_year
    selected = prior[prior.year >= first]
    train = selected[selected.eligible].copy()
    excluded = selected[~selected.eligible][['year', 'exclusion_reason']].to_dict('records')
    required = max(config.min_training_years, window or 0)
    return train, excluded, first, required


def trend_scenarios(daily, years, prediction_year, factor=1.0):
    """OLS on one October mean per year; retain annual residuals and daily shape."""
    sequences = [daily.loc[(daily.index.year == y) & (daily.index.month == 10), 'tmean_c'] for y in years]
    if any(len(s) != 31 or not np.isfinite(s).all() for s in sequences):
        raise ValueError('Trend scenarios require 31 finite daily means per October')
    x = np.asarray(years, dtype=float)
    means = np.array([s.mean() for s in sequences])
    center = float(x.mean())
    slope, level = np.polyfit(x - center, means, 1)
    shifts = factor * slope * (prediction_year - x)
    totals = [archive.degree_days(pd.DataFrame({'tmean_c': s.to_numpy() + shift})).hdd65_f_days.sum()
              for s, shift in zip(sequences, shifts)]
    return np.asarray(totals), shifts, dict(trend_slope_c_per_year=float(slope),
        trend_center_year=center, trend_level_c=float(level), trend_shift_factor=factor,
        target_trend_level_c=float(level + slope * (prediction_year - center)))


def summarize(scores, methods, common=False):
    """Equal weight per held-out year; common requires the exact year intersection."""
    chosen = scores[scores.method.isin(methods)]
    if common:
        sets = [set(chosen.loc[chosen.method == m, 'prediction_year']) for m in methods]
        years = set.intersection(*sets) if sets else set()
        chosen = chosen[chosen.prediction_year.isin(years)]
    rows = []
    for method in methods:
        f = chosen[chosen.method == method]
        rows.append(dict(method=method, n_test_years=len(f),
            first_test_year=f.prediction_year.min(), last_test_year=f.prediction_year.max(),
            test_years=';'.join(f.prediction_year.astype(str)), bias=f.error.mean(),
            mae=f.error.abs().mean(), rmse=np.sqrt(f.error.pow(2).mean()), crps=f.crps.mean(),
            interval_coverage=f.interval_coverage.mean(), interval_width=f.interval_width.mean(),
            payout_bias_usd=f.payout_error_usd.mean(), payout_mae_usd=f.payout_error_usd.abs().mean(),
            payout_rmse_usd=np.sqrt(f.payout_error_usd.pow(2).mean())))
    return pd.DataFrame(rows)


def backtest(daily, config=None, provenance=None):
    config = config or Config()
    yearly = history.monthly_indices(daily, history.Config(as_of=config.as_of, start_year=config.start_year), provenance)
    # Recalculate from validated extrema on a copy; never alter observed inputs.
    daily = daily.copy()
    daily['tmean_c'] = ((daily.tmin_c + daily.tmax_c) / 2).where(daily.valid_day)
    folds, scenarios, scores = [], [], []
    for row in yearly.itertuples():
        year = int(row.year)
        prior = yearly[yearly.eligible & (yearly.year < year)]
        strike = config.strike if config.strike is not None else (float(prior.monthly_hdd_f_days.median()) if len(prior) else None)
        for method in METHODS + SENSITIVITY:
            train, excluded, first, required = training_years(yearly, year, method, config)
            years = train.year.astype(int).tolist()
            reasons = []
            if not row.eligible:
                reasons.append('ineligible held-out October: ' + row.exclusion_reason)
            if len(years) < required:
                reasons.append(f'insufficient history: {len(years)} complete Octobers; require {required}')
            fold = dict(method=method, prediction_year=year, status='skipped' if reasons else 'scored',
                reason='; '.join(reasons), calendar_start=first, calendar_end=year - 1,
                training_years=json.dumps(years), excluded_years=json.dumps(excluded),
                outside_window_years=json.dumps(prior.loc[prior.year < first, 'year'].astype(int).tolist()),
                n_training_years=len(years), required_training_years=required, strike=strike,
                strike_basis='fixed' if config.strike is not None else 'expanding training median',
                strike_training_years=json.dumps([] if config.strike is not None else prior.year.astype(int).tolist()))
            folds.append(fold)
            if reasons:
                continue
            values = train.monthly_hdd_f_days.to_numpy()
            shifts = np.zeros(len(values))
            if method.startswith('trend_') or method == 'temperature_trend':
                values, shifts, params = trend_scenarios(daily, years, year, .5 if method == 'trend_half_shift' else 1.)
                fold.update(params)
            payouts = history.hypothetical_payout(values, strike)
            realized = float(row.monthly_hdd_f_days)
            realized_payout = float(history.hypothetical_payout(realized, strike))
            scores.append(dict(method=method, prediction_year=year, n_training_years=len(years),
                predicted_mean=float(values.mean()), realized_hdd=realized, error=float(values.mean() - realized),
                **ensemble_metrics(values, realized), strike=strike,
                predicted_average_payout_usd=float(payouts.mean()), realized_payout_usd=realized_payout,
                payout_error_usd=float(payouts.mean() - realized_payout), positive_payout_frequency=float((payouts > 0).mean())))
            scenarios.extend(dict(method=method, prediction_year=year, historical_year=y,
                weight=1 / len(years), temperature_shift_c=float(shift), monthly_hdd_f_days=float(value),
                payout_usd=float(pay)) for y, shift, value, pay in zip(years, shifts, values, payouts))
    score_columns = ['method', 'prediction_year', 'n_training_years', 'predicted_mean', 'realized_hdd', 'error',
                     'p10', 'p90', 'crps', 'interval_coverage', 'interval_width', 'strike',
                     'predicted_average_payout_usd', 'realized_payout_usd', 'payout_error_usd', 'positive_payout_frequency']
    scores = pd.DataFrame(scores, columns=score_columns)
    comparisons = []
    for group in [METHODS, *combinations(METHODS, 2), ('expanding', 'temperature_trend', *SENSITIVITY)]:
        table = summarize(scores, group, common=True)
        table.insert(0, 'comparison', ' + '.join(group))
        comparisons.append(table)
    return dict(yearly=yearly, folds=pd.DataFrame(folds), scores=scores,
        scenarios=pd.DataFrame(scenarios, columns=['method', 'prediction_year', 'historical_year', 'weight',
                                                 'temperature_shift_c', 'monthly_hdd_f_days', 'payout_usd']),
        metrics_individual=summarize(scores, METHODS), metrics_common=pd.concat(comparisons, ignore_index=True))


def analyze(config=None):
    config = config or Config()
    path, selection = archive.select_ghcn(cutoff=config.as_of)
    meta = archive.response_metadata(path)
    end_year = archive.utc_timestamp(config.as_of).tz_convert('Etc/GMT+5').year
    daily, audit = archive.ghcn_daily(path, start=f'{config.start_year}-01-01', end=f'{end_year}-12-31')
    provenance = dict(source='GHCN-Daily', station=archive.GHCN_ID, receipt_id=archive.receipt_key(path),
        raw_file=archive.relative_path(path), retrieved_at_utc=meta['retrieved_at_utc'], sha256=meta['sha256'])
    result = backtest(daily, config, provenance)
    result['quality_audit'] = audit[audit.date.dt.month == 10].copy()
    result['manifest'] = dict(parameters=asdict(config), analysis=LABEL, input_provenance=provenance,
        source_selection=selection, raw_receipt=meta, date_convention=history.DATE_CONVENTION,
        contract=dict(month=10, base_f=65, dollars_per_degree_day=100),
        methods=list(METHODS), sensitivity_methods=list(SENSITIVITY),
        window_rule='Require max(min_training_years, window_length) complete prior Octobers inside the calendar window; never backfill.',
        trend='OLS on one October average daily-mean temperature per training year. Shift each daily sequence by slope * (prediction_year - historical_year), then recalculate HDD65. Half-shift and recent-20 fits are fixed sensitivity assumptions.',
        weighting='Equal historical-year scenario weights; equal test-year score weights.',
        metrics='Error = predicted minus realized. Empirical CRPS; linear p10/p90 interpolation; inclusive interval coverage.',
        uncertainty='Intervals describe weather outcomes, not confidence in performance estimates. No performance confidence intervals or bootstrap are estimated.',
        limitations='Revised snapshot, not historical publication vintages. Only a few common years for recent_20. Overlapping training samples and possible serial dependence. No established winner, climate attribution, test-driven tuning, live ensembles, market prices or investment returns.',
        code_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                     (Path(__file__), Path(history.__file__), Path(archive.__file__), ROOT / 'forecast_evaluation.py')})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', default=Config.as_of)
    parser.add_argument('--start-year', type=int, default=2000)
    parser.add_argument('--min-training-years', type=int, default=10)
    parser.add_argument('--strike', type=float)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/baseline_backtest')
    args = vars(parser.parse_args())
    output = args.pop('output')
    result = analyze(Config(**args))
    history.write_results(result, output)
    print(LABEL)
    print(result['metrics_individual'].to_string(index=False))
    print('\nAll four methods on the same years (small sample):')
    print(result['metrics_common'].iloc[:4].to_string(index=False))


if __name__ == '__main__':
    main()
