# JFK temperature history and forecast archive

Open **jfk_temperature_history.ipynb** with this project's `.venv/bin/python` kernel. The notebook has been executed and contains tables and charts. Default reruns are offline, reading the preserved downloads.

## Results at initialization (2026-09-17)

- NOAA GHCN-Daily `USW00094789`, JFK International Airport: 9,755 accepted days from 2000-01-01 through 2026-09-15. The calendar includes 2026-09-16 as missing, with no interpolation.
- 320 complete monthly HDD/CDD totals at a 65°F base. September 2026 is partial and its full-month totals are blank.
- NOAA Aviation Weather `KJFK`: downloaded the available 30-day window; 27 complete local days, with 26 accepted dates overlapping GHCN. The direct API cannot provide a comparable history back to 2000.
- First forecast archive saved at 2026-09-17 21:00 UTC. A second snapshot at 21:06 UTC uses NOAA's verified station coordinates (40.6392, -73.7639); the initial snapshot used a nearby JFK point (40.6398, -73.7789). Each snapshot retains its point lookup for provenance.
- Codex automation `archive-jfk-weather-forecasts` is active every six hours. It archives NWS hourly forecasts, KJFK TAF, and recent METAR observations, then rebuilds processed CSVs. The local host must be available; this is not an always-on remote service. Routine successes are quiet; failures require attention.

## Commands

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python weather_pipeline.py all       # archive, download, process
.venv/bin/python weather_pipeline.py archive   # new forecast + recent METAR snapshot
.venv/bin/python weather_pipeline.py refresh   # refresh GHCN + recent METAR window
.venv/bin/python weather_pipeline.py build     # rebuild CSVs offline
.venv/bin/python execute_notebook.py           # execute and save notebook outputs
.venv/bin/python -m unittest -v test_weather_pipeline.py
```

`requirements-lock.txt` records the tested environment. Select `.venv/bin/python` explicitly in the notebook editor. `make_notebook.py` regenerates the notebook structure and clears its outputs; use only when changing the template, then execute it again.

## Definitions and limitations

GHCN: reject missing sentinel values and nonblank quality flags, preserve source and measurement flags, reject reversed extrema, and compute daily mean as `(TMAX + TMIN)/2`. GHCN dates follow the source's observing-day convention. Recent records may lag or be revised.

METAR: latest receipt wins for duplicate observation timestamps; retain QC fields and raw reports. Screen numeric temperature to −60…55°C, average observations within UTC hourly bins, assign America/New_York dates, and require every local hour plus a completed day. Daily mean is the midpoint of sampled hourly extrema. The sampled hourly mean is also exported. This plausibility/completeness screen is not equivalent to GHCN climate QC, and sampled extremes may differ from actual daily extrema. Do not splice the two series silently.

HDD65 = max(65 − daily mean °F, 0); CDD65 = max(daily mean °F − 65, 0). Monthly totals require all calendar days. Available-day sums are labeled partial; missing days are not replaced by zero. Units are °F-days.

Neither observation source provides a temperature forecast. NOAA/NWS hourly grid forecasts near JFK are archived for that purpose; TAFs retain aviation forecast context. All snapshots preserve original bytes, URL, receipt time, HTTP metadata, and SHA-256. Forecast issue/update times and valid times stay separate. No historical forecast vintages are fabricated. A partial network failure retains successful products and records errors in its manifest.

## Files

- `data/processed/ghcn_daily.csv`, `ghcn_monthly.csv`
- `data/processed/metar_daily.csv`, `metar_monthly.csv`
- `data/processed/ghcn_quality_audit.csv`, `metar_observations.csv`, `daily_comparison.csv`
- `data/raw/`: timestamped original observations and request metadata
- `data/forecasts/`: timestamped forecast runs, normalized NWS hourly CSV, manifest
- `data/reference/`: NOAA format specification, station record, AWC API schema

Sources: [GHCN documentation](https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt), [station catalog](https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt), [AWC API](https://aviationweather.gov/data/api/), [NWS API](https://www.weather.gov/documentation/services-web-api).
