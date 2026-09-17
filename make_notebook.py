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
md('''## Outputs and references
Daily and monthly tables, retained observations, quality audit, and source comparison are in `data/processed/`. Immutable source responses are in `data/raw/`; forecast snapshots are in `data/forecasts/`.

- [NOAA GHCN-Daily format and quality flags](https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt)
- [NOAA station metadata](https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt)
- [Aviation Weather API](https://aviationweather.gov/data/api/)
- [NWS API documentation](https://www.weather.gov/documentation/services-web-api)

Station metadata and API schema consulted for this build are retained in `data/reference/`.''')]
nb.metadata = {'kernelspec': {'display_name': 'Python 3 (project .venv)', 'language':'python', 'name':'python3'}, 'language_info': {'name':'python','version':'3.9'}}
n.write(nb, 'jfk_temperature_history.ipynb')
