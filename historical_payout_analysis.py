"""Offline, unadjusted JFK historical HDD scenarios, never market pricing."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import Path
import argparse
import hashlib

import numpy as np
import pandas as pd
import weather_pipeline as archive

ROOT = Path(__file__).resolve().parent
LABEL = 'historical expected payout before premium, discounting, and risk adjustments'
DATE_CONVENTION = ('NOAA GHCN observing-date labels, not UTC or METAR civil-date rebins; '
                   'month completion gated at next-month midnight fixed EST (UTC-05), '
                   'consistent with the repository GHCN evaluation boundary.')


@dataclass(frozen=True)
class Config:
    as_of: str = '2026-09-21T00:30:00Z'
    month: int = 10
    start_year: int = 2000
    base_f: float = 65.0
    strike: float | None = None
    dollars_per_degree_day: float = 100.0
    strike_min: float = 0.0
    strike_max: float = 500.0
    strike_count: int = 51

    def __post_init__(self):
        at = archive.utc_timestamp(self.as_of).tz_convert('Etc/GMT+5')
        if self.month not in range(1, 13):
            raise ValueError('month must be 1..12')
        if not 1678 <= self.start_year <= at.year <= 2261:
            raise ValueError('start_year must precede as_of and fit pandas calendar bounds')
        values = [self.base_f, self.dollars_per_degree_day, self.strike_min, self.strike_max]
        if self.strike is not None:
            values.append(self.strike)
        if not all(np.isfinite(v) for v in values):
            raise ValueError('Contract parameters must be finite')
        if self.dollars_per_degree_day < 0 or self.strike_min > self.strike_max or self.strike_count < 2:
            raise ValueError('Require nonnegative dollars, ordered strike bounds and at least two strikes')
        # Canonicalize numeric types so CLI and notebook configurations serialize alike.
        for name in ('base_f', 'strike', 'dollars_per_degree_day', 'strike_min', 'strike_max'):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, float(getattr(self, name)))


def hypothetical_payout(hdd, strike, dollars_per_degree_day=100):
    """Apply the call payoff to each observation, preserving missing values."""
    return dollars_per_degree_day * np.maximum(np.asarray(hdd, dtype=float) - strike, 0)


def monthly_indices(daily, config, provenance=None):
    """One row per selected calendar month, including explicit exclusion reasons."""
    at = archive.utc_timestamp(config.as_of)
    last_year = at.tz_convert('Etc/GMT+5').year
    if not daily.index.is_unique:
        raise ValueError('Daily observations must have unique dates')
    rows = []
    for year in range(config.start_year, last_year + 1):
        start = pd.Timestamp(year, config.month, 1)
        end = start + pd.offsets.MonthBegin(1)
        dates = pd.date_range(start, end, inclusive='left')
        frame = daily.reindex(dates).copy()
        valid = (frame.valid_day.eq(True) & frame.tmin_c.notna() & frame.tmax_c.notna()
                 & (frame.tmin_c <= frame.tmax_c))
        frame['tmean_c'] = ((frame.tmin_c + frame.tmax_c) / 2).where(valid)
        degree_days = archive.degree_days(frame, base_f=config.base_f)
        valid &= degree_days.hdd65_f_days.notna()
        complete = bool(valid.all())
        completed = end.tz_localize('Etc/GMT+5').tz_convert('UTC') <= at
        reasons = []
        if not completed:
            reasons.append('selected month not completed by as_of')
        if not complete:
            reasons.append(f'{len(dates) - int(valid.sum())} missing or QC-invalid days')
        rows.append(dict(year=year, month=config.month, expected_days=len(dates),
                         valid_days=int(valid.sum()), completeness=float(valid.mean()),
                         complete=complete, month_completed=completed, eligible=complete and completed,
                         monthly_hdd_f_days=float(degree_days.hdd65_f_days.sum()) if complete and completed else np.nan,
                         base_f=config.base_f, exclusion_reason='; '.join(reasons),
                         invalid_dates=';'.join(dates[~valid].strftime('%Y-%m-%d')),
                         **(provenance or {})))
    return pd.DataFrame(rows)


def historical_windows(yearly, config):
    """Fixed year bounds: missing years never cause a window to extend."""
    last = archive.utc_timestamp(config.as_of).tz_convert('Etc/GMT+5').year - 1
    bounds = [('all_history', config.start_year, last + 1),
              ('recent_20_calendar_years', last - 19, last),
              ('recent_10_calendar_years', last - 9, last)]
    return [(name, first, end, yearly[yearly.eligible & yearly.year.between(max(first, config.start_year), end)])
            for name, first, end in bounds]


def distribution(values):
    s = pd.Series(values, dtype=float)
    return dict(n=len(s), mean=s.mean(), median=s.median(), std=s.std(ddof=1),
                minimum=s.min(), maximum=s.max(),
                **{f'p{q}': s.quantile(q / 100, interpolation='linear') for q in (10, 25, 75, 90)})


def analyze(config=None):
    config = config or Config()
    # The established selector validates HTTP status, checksum, structure and station.
    path, selection = archive.select_ghcn(cutoff=config.as_of)
    meta = archive.response_metadata(path)
    selection['age_hours'] = (archive.utc_timestamp(config.as_of) - archive.utc_timestamp(meta['retrieved_at_utc'])).total_seconds() / 3600
    local_year = archive.utc_timestamp(config.as_of).tz_convert('Etc/GMT+5').year
    daily, audit = archive.ghcn_daily(path, start=f'{config.start_year}-01-01', end=f'{local_year}-12-31')
    provenance = dict(source='GHCN-Daily', station=archive.GHCN_ID,
                      receipt_id=archive.receipt_key(path), raw_file=archive.relative_path(path),
                      retrieved_at_utc=meta['retrieved_at_utc'], sha256=meta['sha256'])
    yearly = monthly_indices(daily, config, provenance)
    sample = yearly.loc[yearly.eligible, 'monthly_hdd_f_days']
    if sample.empty:
        raise ValueError('No complete eligible historical months in the selected snapshot and period')
    strike = float(sample.median()) if config.strike is None else config.strike
    yearly['payout_usd'] = hypothetical_payout(yearly.monthly_hdd_f_days, strike, config.dollars_per_degree_day)
    summary, sensitivity = [], []
    strikes = np.linspace(config.strike_min, config.strike_max, config.strike_count)
    for name, first, end, window in historical_windows(yearly, config):
        row = dict(window=name, first_calendar_year=first, last_calendar_year=end,
                   effective_start_year=max(first, config.start_year), strike=strike,
                   actual_valid_years=len(window), valid_years=';'.join(window.year.astype(str)),
                   payout_label=LABEL)
        row.update({f'hdd_{k}': v for k, v in distribution(window.monthly_hdd_f_days).items()})
        row.update({f'payout_{k}': v for k, v in distribution(window.payout_usd).items()})
        row['positive_payout_fraction'] = (window.payout_usd > 0).mean() if len(window) else np.nan
        summary.append(row)
        for k in strikes:
            payouts = hypothetical_payout(window.monthly_hdd_f_days, k, config.dollars_per_degree_day)
            sensitivity.append(dict(window=name, strike=float(k), actual_valid_years=len(window),
                                    average_payout_usd=float(payouts.mean()) if len(window) else np.nan))
    manifest = dict(parameters=asdict(config), selected_strike=strike,
                    strike_basis='illustrative full eligible sample median HDD' if config.strike is None else 'user supplied',
                    analysis='unadjusted historical scenario analysis', payout_label=LABEL,
                    date_convention=DATE_CONVENTION, source_selection=selection, input_provenance=provenance,
                    raw_receipt=meta, standard_deviation='sample (ddof=1)', percentiles='linear interpolation',
                    windows='Recent windows end in the calendar year before as_of; no replacement of missing years. All history includes a completed current-year selected month if available.',
                    limitations='Changing climate and station history can affect representativeness. No climate or station adjustments, premium, discounting, risk adjustment, forecast integration or market pricing. These are revised historical observations available at analysis as_of, not vintages known in each historical year.',
                    code_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (Path(__file__), Path(archive.__file__))})
    # Keep only the selected month in the supporting daily and QC exports.
    daily = daily[daily.index.month == config.month].copy()
    daily = archive.degree_days(daily, base_f=config.base_f).rename(columns={
        'hdd65_f_days': 'hdd_f_days', 'cdd65_f_days': 'cdd_f_days'})
    for key, value in provenance.items():
        daily[key] = value
    audit = audit[audit.date.dt.month == config.month].copy()
    return dict(yearly=yearly, excluded_years=yearly[~yearly.eligible].copy(),
                window_summary=pd.DataFrame(summary), strike_sensitivity=pd.DataFrame(sensitivity),
                daily=daily.reset_index(), quality_audit=audit, manifest=manifest)


def write_results(result, output=ROOT / 'data/historical_payout'):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, table in result.items():
        if isinstance(table, pd.DataFrame):
            table.to_csv(output / f'{name}.csv', index=False)
    manifest = dict(result['manifest'])
    manifest['output_sha256'] = {f'{name}.csv': hashlib.sha256((output / f'{name}.csv').read_bytes()).hexdigest()
                                 for name, table in result.items() if isinstance(table, pd.DataFrame)}
    archive.write_json(output / 'run_manifest.json', manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', default=Config.as_of)
    parser.add_argument('--month', type=int, default=10)
    parser.add_argument('--start-year', type=int, default=2000)
    parser.add_argument('--base-f', type=float, default=65)
    parser.add_argument('--strike', type=float)
    parser.add_argument('--dollars-per-degree-day', type=float, default=100)
    parser.add_argument('--strike-min', type=float, default=0)
    parser.add_argument('--strike-max', type=float, default=500)
    parser.add_argument('--strike-count', type=int, default=51)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/historical_payout')
    args = vars(parser.parse_args())
    output = args.pop('output')
    result = analyze(Config(**args))
    write_results(result, output)
    print(result['manifest']['strike_basis'], result['manifest']['selected_strike'])
    print(LABEL)
    print(result['window_summary'][['window', 'actual_valid_years', 'hdd_mean', 'hdd_std', 'payout_mean', 'positive_payout_fraction']].to_string(index=False))
    print(result['excluded_years'][['year', 'valid_days', 'exclusion_reason']].to_string(index=False))


if __name__ == '__main__':
    main()
