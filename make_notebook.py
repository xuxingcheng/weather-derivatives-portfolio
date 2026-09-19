from pathlib import Path
import nbformat as n
nb = n.v4.new_notebook()
md = n.v4.new_markdown_cell
code = n.v4.new_code_cell
nb.cells = [
md('''# JFK daily temperature & monthly degree days
Two NOAA observation versions: **GHCN-Daily USW00094789** and **Aviation Weather METAR KJFK**.

History starts **2000-01-01**; base **65°F**. Forecast snapshots are archived every six hours separately from observations.

GHCN has the long historical record. The Aviation Weather API exposes up to 30 recent days, so its direct version cannot recreate the 2000–present record. The archive grows forward from today. Neither observation feed supplies temperature forecasts: we retain NOAA/NWS hourly gridded forecasts near JFK and aviation TAF forecasts.

Run with the project's `.venv` Python. By default this notebook uses downloaded files without network access.'''),
code('''from pathlib import Path
import os
os.environ.setdefault('MPLCONFIGDIR', str(Path.cwd() / '.mplconfig'))
import pandas as pd
import matplotlib.pyplot as plt
from IPython.display import display
import weather_pipeline as w
pd.set_option('display.max_columns', 12)
plt.rcParams.update({'figure.figsize': (12, 4), 'axes.spines.top': False, 'axes.spines.right': False})
print({'station': w.ICAO, 'ghcn_id': w.GHCN_ID, 'start': w.START, 'base_f': w.BASE_F, 'timezone': w.TZ})'''),
md('''## 1. Download and preserve source data
Set the switches to `True` to refresh. Each response is retained with retrieval time, URL, headers, and SHA-256. Forecast runs keep independent snapshots and a manifest; a partial failure raises an error while preserving successful products. TAFs are aviation forecasts, not hourly temperature forecasts.'''),
code('''REFRESH_OBSERVATIONS = False
ARCHIVE_FORECAST_NOW = False
if ARCHIVE_FORECAST_NOW:
    w.archive_forecasts()
if REFRESH_OBSERVATIONS:
    w.refresh_observations()'''),
md('''## 2. Clean both daily versions
**GHCN:** parse tenths of °C; reject `-9999` and nonblank NOAA quality flags. Retain measurement/source flags in the audit CSV. Reject reversed extrema and leave gaps unfilled. Temperature is `(TMAX + TMIN) / 2` on NOAA's reported date; historical observing-day conventions may differ from a local calendar day.

**METAR:** keep the latest receipt for duplicate observation times; retain raw reports and QC fields. Apply a broad −60 to 55°C plausibility screen (not equivalent to NOAA climate QC). Average reports within each UTC hour to reduce extra SPECI report weighting. Convert to America/New_York dates, accounting for 23/25-hour DST days. Require every local hourly bin and a finished local day before accepting the daily index. Use the midpoint of the sampled hourly extrema; also export the hourly mean as a diagnostic. Sampled extrema may miss true extremes, so these versions are not interchangeable.'''),
code('''ghcn, metar, comparison = w.build()
summary = pd.DataFrame([
    {'version': name, 'first_date': f.index.min().date(), 'last_date': f.index.max().date(),
     'calendar_rows': len(f), 'accepted_days': int(f.valid_day.sum()), 'missing_or_rejected': int((~f.valid_day).sum())}
    for name, f in [('GHCN-Daily', ghcn), ('METAR', metar)]])
display(summary)
display(ghcn.tail(7))
display(metar.tail(7))'''),
md('''## 3. Monthly heating and cooling indices
For each accepted day, `HDD65 = max(65 − Tmean_F, 0)` and `CDD65 = max(Tmean_F − 65, 0)`, in **°F-days**. Sum daily values, not monthly mean temperatures. Published monthly indices require all calendar days, including leap days. `hdd_partial` and `cdd_partial` show available-day sums with coverage; they are not full-month totals and are never scaled or imputed.'''),
code('''ghcn_monthly = w.monthly(ghcn)
metar_monthly = w.monthly(metar)
display(ghcn_monthly.tail(15).round(2))
display(metar_monthly.round(2))'''),
code('''fig, axes = plt.subplots(2, 1, figsize=(12, 7), constrained_layout=True)
ghcn.tmean_f.resample('MS').mean().plot(ax=axes[0], color='#126782', lw=1)
axes[0].set(title='JFK monthly mean of available daily temperatures', ylabel='°F', xlabel='')
p = ghcn_monthly[['hdd65_f_days', 'cdd65_f_days']].copy()
p.index = p.index.to_timestamp()
p.tail(60).plot(ax=axes[1], color=['#386cb0', '#ef7c30'])
axes[1].set(title='Monthly degree days • complete months only', ylabel='°F-days', xlabel='')
plt.show()'''),
md('''## 4. Compare overlapping days
The difference combines observation timing, sampling, and source-processing effects. No bias correction is applied. GHCN reporting latency can reduce the overlap.'''),
code('''paired = comparison.dropna()
print(f'Paired accepted days: {len(paired)}')
if len(paired):
    display(paired.round(2))
    display(pd.Series({'mean_METAR_minus_GHCN_C': paired.difference_c.mean(),
                       'MAE_C': paired.difference_c.abs().mean(),
                       'RMSE_C': (paired.difference_c.pow(2).mean())**0.5}))
    paired[['tmean_c_ghcn', 'tmean_c_metar']].plot(marker='o', ylabel='°C', title='Daily mean temperature: overlap')
    plt.show()'''),
md('''## 5. Forecast archive
Each six-hour run saves the entire available NWS hourly horizon and current TAF, plus 48 hours of METAR for continued observation coverage. Retrieval, forecast update/generation, and valid times remain distinct. NWS is a grid forecast near JFK, not a station-issued thermometer forecast. The scheduler depends on the Codex desktop host being available; missed snapshots cannot be reconstructed from the current forecast endpoint.'''),
code('''import json
runs = sorted((w.ROOT / 'data/forecasts').glob('*/manifest.json'))
status = []
for path in runs:
    m = json.loads(path.read_text())
    status.append({'retrieved_utc': m['started_at_utc'], 'products': ', '.join(m['products']), 'errors': str(m['errors'])})
display(pd.DataFrame(status))
latest = sorted((w.ROOT / 'data/forecasts').glob('*/nws_hourly.csv'))[-1]
forecast = pd.read_csv(latest)
display(forecast[['startTime', 'endTime', 'temperature', 'temperatureUnit', 'update_time_utc', 'lead_hours_at_retrieval']].head(12))
print('Latest forecast archive:', latest.parent)'''),
md('''## 6. GEFS and ECMWF ensemble archive
Explicit Open-Meteo models: `gfs025` (31 members, requested 10 days) and `ecmwf_ifs025` (51 members, requested 15 days). The unsuffixed temperature variable is retained as control member 0. We request 2-meter temperatures in °C at JFK, with 2.7 m elevation and land grid selection; requested and returned grid coordinates are both retained.

All returned members and null values are preserved. Hourly API values can be interpolated from 3/6-hour model output. Every six hours we retrieve available data; this collection interval does not guarantee one snapshot of each model initialization. Both products are documented to update every six hours, with publication delays. Metadata is saved before and after the forecast request. An update younger than 10 minutes is flagged for possible server replication delay; another collection can be run later.

Forecast responses do not supply a verifiable initialization timestamp. `initialization_time_utc` remains null. Separate model metadata initialization/availability times are **advisory**, not assigned to every forecast target. `generationtime_ms` measures API computation time, not model initialization. These are retrieval snapshots, potentially containing data from successive runs, not guaranteed single-run hindcasts. Individual-member retention is only up to three past days; this does not reconstruct missed historical vintages.

Identical forecast-content snapshots share a content hash; the analysis table has one row per snapshot/member/target. All retrieval receipts and raw responses remain archived. Any changed temperature, null mask, member set, time axis, or grid creates a new snapshot. Matching individual values across distinct snapshots are deliberately retained.'''),
code('''ensemble, coverage = w.build_ensemble_archive()
if coverage.empty:
    print('No ensemble archive yet. Run: .venv/bin/python weather_pipeline.py ensembles')
else:
    display(coverage[['model', 'retrieved_at_utc', 'snapshot_id', 'expected_members', 'parsed_members',
                      'members_with_data', 'control_present', 'target_hours', 'missing_values',
                      'complete_requested_window', 'valid_start_utc', 'valid_end_utc', 'warnings']])
    print('Distinct snapshots:', ensemble.snapshot_id.nunique(), '| Unique member-target rows:', len(ensemble))
    print('Verified initialization timestamps:', ensemble.initialization_time_utc.notna().sum())
    display(ensemble[['model', 'member_id', 'member_role', 'requested_latitude', 'requested_longitude',
                      'grid_latitude', 'grid_longitude', 'units', 'target_time_utc', 'temperature_2m',
                      'retrieved_at_utc', 'initialization_time_utc', 'advisory_initialization_time_utc']].head())'''),
code('''if not coverage.empty:
    latest = coverage.sort_values('retrieved_at_utc').groupby('model', as_index=False).tail(1)
    fig, axes = plt.subplots(len(latest), 1, figsize=(12, 7), squeeze=False, constrained_layout=True)
    member_coverage = []
    for ax, (_, receipt) in zip(axes.flat, latest.iterrows()):
        f = ensemble[ensemble.snapshot_id == receipt.snapshot_id].copy()
        f['target_time_utc'] = pd.to_datetime(f.target_time_utc, utc=True)
        # Plot future targets relative to this retrieval, not an invented run time.
        f = f[f.target_time_utc >= pd.Timestamp(receipt.retrieved_at_utc)]
        wide = f.pivot(index='target_time_utc', columns='member_id', values='temperature_c')
        counts = wide.count(axis=1)
        enough = counts == int(receipt.expected_members)
        median = wide.median(axis=1).where(enough)
        lower = wide.quantile(0.1, axis=1).where(enough)
        upper = wide.quantile(0.9, axis=1).where(enough)
        ax.plot(wide.index, wide.values, color='#718096', alpha=0.12, lw=0.5)
        ax.fill_between(wide.index, lower.to_numpy(), upper.to_numpy(), color='#3182ce', alpha=0.25, label='10th–90th percentile model spread')
        ax.plot(wide.index, median, color='#125478', lw=1.8, label='Ensemble median')
        ax.set(title=f"{receipt.model} • {receipt.parsed_members} members • retrieved {receipt.retrieved_at_utc[:16]} UTC", ylabel='2 m temperature (°C)', xlabel='Target time (UTC)')
        ax.legend(fontsize=8)
        member_coverage.append({'model':receipt.model, 'future_target_hours':len(wide),
                               'min_available_members':int(counts.min()) if len(counts) else 0,
                               'hours_with_all_expected_members':int(enough.sum()),
                               'missing_values_in_future_window':int(wide.isna().sum().sum())})
    display(pd.DataFrame(member_coverage))
    plt.show()'''),
md('''The shaded 10th–90th percentile band describes **model spread**, not an automatically calibrated confidence interval. Median/band values are omitted where any expected member is missing. Each panel uses one retrieval snapshot; models and revisions are not pooled. Shared model biases and coastal grid/elevation effects can remain even when spread is narrow.

This stage does not calculate forecast monthly HDD/CDD or derivative prices. A 10- or 15-day forecast window is not a complete calendar month; the historical completeness rules remain unchanged.

Sources: [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api), [publication metadata](https://open-meteo.com/en/docs/model-updates), [control-member convention](https://github.com/open-meteo/open-meteo/discussions/366). Temperature data: NOAA GEFS and ECMWF IFS via Open-Meteo, CC BY 4.0; converted here to long-format tables and empirical member quantiles.'''),
md('''## Outputs and references
Daily and monthly tables, retained observations, quality audit, and source comparison are in `data/processed/`. Immutable source responses are in `data/raw/`; forecast snapshots are in `data/forecasts/`.

- [NOAA GHCN-Daily format and quality flags](https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt)
- [NOAA station metadata](https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt)
- [Aviation Weather API](https://aviationweather.gov/data/api/)
- [NWS API documentation](https://www.weather.gov/documentation/services-web-api)

Station metadata and API schema consulted for this build are retained in `data/reference/`.''')]
nb.metadata = {'kernelspec': {'display_name': 'Python 3 (project .venv)', 'language':'python', 'name':'python3'}, 'language_info': {'name':'python','version':'3.9'}}
n.write(nb, 'jfk_temperature_history.ipynb')
