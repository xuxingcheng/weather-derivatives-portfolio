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

## GEFS and ECMWF ensembles through Open-Meteo

The existing `archive` command now collects both explicit ensemble products in addition to NWS hourly forecasts, TAF and METAR. JFK, the historical start date, and the 65°F base are unchanged. This extension collects and inspects forecasts; it does not price derivatives or calculate forecast monthly HDD/CDD.

### Models and API verification (2026-09-17)

| Selected API model | Product | Members including control | Requested window | Documented update frequency |
|---|---|---:|---:|---|
| `gfs025` | NOAA GEFS 0.25° | 31 | 10 days | Every 6 hours |
| `ecmwf_ifs025` | ECMWF IFS 0.25° ENS | 51 | 15 days | Every 6 hours |

Open-Meteo also offers GEFS 0.5° (31 members, up to 35 days), seamless GEFS, AIGEFS, ECMWF AIFS and Europe-specific products. We select the two global physical models explicitly, without best-match or seamless spatial/model selection. Their native output is coarser than hourly; Open-Meteo interpolates to hourly. Individual ensemble members have up to three past days of retention; longer-lived ensemble means/spreads do not preserve individual members or replace a vintage archive. The docs disagree on the overall maximum (35/36 days); our 10/15-day requests avoid that boundary. [Ensemble API documentation](https://open-meteo.com/en/docs/ensemble-api)

ECMWF's open-data cycles are 00/06/12/18 UTC; its documented ENS horizons are 360 hours for 00/12 and 144 hours for 06/18. A 15-day API response must not be assumed to belong entirely to the latest 06/18 run. [ECMWF's official download client documentation](https://github.com/ecmwf/ecmwf-opendata)

Open-Meteo exposes model initialization, modification and availability timestamps separately. Availability can differ across servers; the provider recommends allowing another 10 minutes for replication. These timestamps do not authenticate the run underlying each returned temperature. [Publication metadata documentation](https://open-meteo.com/en/docs/model-updates)

The free endpoint is for non-commercial use: limits are 600 calls/minute, 5,000/hour, 10,000/day and 300,000/month. Long requests can count as multiple calls; do not equate our two forecast HTTP requests with exactly two billing units. Ensemble commercial access requires Professional or higher. Data attribution is required under CC BY 4.0. [Usage terms](https://open-meteo.com/en/terms), [pricing and call accounting](https://open-meteo.com/en/pricing). Attribution: NOAA GEFS and ECMWF IFS via Open-Meteo; this project reshapes responses and computes member quantiles.

### Collection and publication timing

Our Codex automation still polls **every six hours**, independently of model initialization or publication. It now checks five products. Model runs take time to publish; a poll can see unchanged data, partially updated data, or older data. We fetch model metadata before and after each forecast response, retain it verbatim, and flag changing metadata, availability less than 10 minutes old, missing metadata, or availability over two model-update intervals old. We still retain the forecast. Run `ensembles` again after publication settles when a warning warrants it; the next scheduled collection also retries. This policy does not guarantee capture of every upstream cycle, particularly while the host is offline.

No initialization is inferred from receipt time, target start, HTTP Date, or `generationtime_ms` (API processing duration). `initialization_time_utc` is null because the current forecast response provides no verified per-target run timestamp. Advisory metadata times are separate columns and summary fields. These are **retrieval snapshots, not guaranteed single-initialization forecasts**.

Each source runs independently. Successful downloads survive failures elsewhere; the manifest reports failures and the command exits nonzero after attempting all sources. Raw HTTP error responses are retained, too. Transient requests retry up to three times; rate-limit responses honor short Retry-After delays and stop on longer waits. Empty, error, malformed, or all-null responses never become an apparent successful forecast. Partially valid member arrays are retained with warnings; invalid arrays remain in raw JSON, with omitted members identified. Run `build` even after an archive failure to process available data.

### Local commands and files

```sh
.venv/bin/python weather_pipeline.py archive    # all five products; no GHCN refresh
.venv/bin/python weather_pipeline.py ensembles  # only GEFS + ECMWF, useful for delayed publication
.venv/bin/python weather_pipeline.py build      # offline rebuild, including ensemble tables
.venv/bin/python execute_notebook.py            # refresh saved plots and tables
.venv/bin/python -m unittest -v test_weather_pipeline.py  # offline tests
```

The `refresh` and `all` commands retain their existing observation workflow. The updated automation runs `build` even after a partial archive failure. Schedule execution still requires this local Codex host; the repository itself does not install a daemon or GitHub Actions workflow.

Per-run files in `data/forecasts/<retrieval>/`:

- Timestamped raw ensemble JSON and HTTP metadata (URL, receipt time, headers and SHA-256).
- Separate raw model metadata before and after each forecast request.
- `ensemble_<model>.csv`: every parseable member/target, including null temperatures and control member 0. The unsuffixed `temperature_2m` is the control; numeric suffixes retain their original IDs. [Provider explanation](https://github.com/open-meteo/open-meteo/discussions/366)
- `ensemble_<model>_summary.json`: counts, coverage, nulls, publication diagnostics and source paths.
- Existing manifest and NOAA products.

`data/processed/ensemble_temperature.csv` is the combined analysis table. It contains model, member ID/role, original variable, station, requested and returned grid coordinates/elevation, original temperature and units, normalized °C, UTC target/retrieval times, null verified initialization, separate advisory timestamps, raw-file provenance, content hash, first/last retrieval and retrieval count.

`data/processed/ensemble_coverage.csv` retains one receipt per archived model retrieval. A content hash ignores response processing duration and retrieval time but includes the model, grid, member/time axes, units, temperatures and null mask. Repeated identical snapshots collapse to one set of analysis rows keyed by hash/member/target while retaining all receipts and raw responses. Changed responses create new snapshots. Equal values across different revisions are not collapsed. Rebuilding is deterministic and does not append duplicates.

The notebook reports requested and actual member/target coverage and missing values. It plots one latest snapshot per model with individual trajectories, the median, and the 10th–90th percentile range. The band is **model spread, not a calibrated confidence interval**; it is suppressed when any expected member is unavailable. Missing values are never imputed. The forecast grid near a coastal airport is not a thermometer measurement, and the requested 2.7 m elevation is used for Open-Meteo's downscaling.

### Live verification

The integrated collection on **2026-09-17 at 21:50 UTC** successfully retained NWS, TAF, METAR and both ensembles:

| Model | Members | Returned UTC target window (inclusive) | Hourly targets/member | Missing temperatures |
|---|---:|---|---:|---:|
| GEFS `gfs025` | 31 (0–30) | Sep 17 00:00 – Sep 26 23:00 | 240 | 0 |
| ECMWF `ecmwf_ifs025` | 51 (0–50) | Sep 17 00:00 – Oct 1 23:00 | 360 | 0 |

There are 7,440 GEFS and 18,360 ECMWF member-target rows. At retrieval, 218 and 338 hourly targets respectively were in the future; the earlier same-day targets are retained but excluded from the future forecast plot. Verified initialization remains unavailable. Advisory initialization was Sep 17 12 UTC for GEFS and 06 UTC for ECMWF, further illustrating why metadata must not be assigned as a common run time to the complete 15-day response. No complete calendar-month forecast index is claimed.
