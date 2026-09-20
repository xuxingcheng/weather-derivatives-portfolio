# JFK temperature history and forecast archive

Open `jfk_temperature_history.ipynb` with the project's `.venv/bin/python` kernel. Default execution is offline. JFK (`USW00094789` / `KJFK`), the **2000-01-01** historical start and **65°F** degree-day base are unchanged. This project collects observations and forecast vintages; this change adds no pricing or forecast evaluation.

## Commands

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python weather_pipeline.py archive --grace-hours 2
.venv/bin/python weather_pipeline.py refresh
.venv/bin/python weather_pipeline.py ensembles
.venv/bin/python weather_pipeline.py all
.venv/bin/python weather_pipeline.py build
.venv/bin/python weather_pipeline.py health --grace-hours 2
.venv/bin/python -m unittest -v test_weather_pipeline test_collection
.venv/bin/python execute_notebook.py
```

- `archive`: collect NWS point lookup, NWS hourly, TAF, the last 48 hours of METAR, GEFS and ECMWF. Also fetch the full GHCN station file at the first poll when the latest **validated successful** retrieval is at least 24 hours old, or none exists. A failed GHCN refresh remains due at the next poll. Every download has a new immutable filename.
- `refresh`: force GHCN and six independent five-day METAR requests spanning the API's recent 30-day window. Overlapping requests preserve corrections and original receipts.
- `ensembles`: collect just GEFS and ECMWF, including separate before/after publication metadata.
- `all`: force the observation refresh, then run the archive workflow. A successful GHCN refresh satisfies that archive's daily gate.
- All four collection commands attempt an offline rebuild **even after partial collection failure**, then exit nonzero if collection/build failed. Each source is attempted independently. Successful raw responses and processed outputs survive other sources' failures.
- `build`: no network requests. Rebuild observation tables and ensemble Parquet from retained raw responses and legacy per-run tables; reparse the entire selected GHCN snapshot, including recent and older revised dates. It also writes selection, migration-check and health reports.
- `health`: recompute status from local receipts/manifests. `--grace-hours` (default 2; also `WEATHER_GRACE_HOURS`) flags no recorded attempt or no successful retrieval after **24 + grace** hours for GHCN and **6 + grace** hours for other products. Health status is reported as data; `health` itself does not fail merely because a source is stale.

`requirements-lock.txt` records the tested environment, including PyArrow. `make_notebook.py` regenerates the notebook and clears outputs; execute the notebook again after changing its template. Notebook network switches default to false and catch partial collection failures so offline processing can continue.

## Validated selection and observation provenance

A raw file is eligible only with a readable HTTP metadata receipt, a 2xx status, explicit UTC retrieval timestamp, matching SHA-256, and valid source structure. HTTP errors, transport errors, HTML/error payloads, wrong stations, malformed fixed-width GHCN records, missing extrema and malformed METAR arrays are excluded. Original bytes and error records remain available for diagnosis. HTTP failures retry up to three times, except nonretryable 4xx and long Retry-After requests; an HTTP 200 structural failure is retained and reported, then retried on the next collection.

GHCN selection is by **receipt timestamp**, not filename. The latest valid complete-format station snapshot wins; if a newer downloaded snapshot is unusable, the previous valid snapshot is selected explicitly. `data/processed/observation_selection.json` records the chosen file, retrieval time, age in hours, fallback flag, newer unusable downloads and excluded files. If no snapshot is usable, GHCN processing fails explicitly while METAR/ensembles can still be processed. Existing outputs for a failed source may remain on disk; always inspect the selection and health reports before treating them as current.

GHCN daily and quality-audit tables record source, observing date, receipt ID and our retrieval time. METAR observation rows retain observation UTC time, upstream receipt UTC time, our retrieval UTC time, source, receipt ID, original report and QC fields. The small `observation_receipts.csv` maps receipt IDs to raw-file paths, hashes, retrieval times, validation status and coverage, avoiding a repeated long path on every observation row. Duplicate METAR observations use latest upstream receipt, then latest local retrieval and a deterministic path tie-break. Failed downloads never enter the build's METAR parser. Daily aggregate provenance is recoverable through these observation rows and their raw receipts.

Raw snapshots retain every historical revision, even when current analysis tables change. Rebuild observations as known at a historical retrieval cutoff into a separate directory:

```sh
.venv/bin/python weather_pipeline.py build \
  --cutoff 2026-09-18T00:00:00Z --output /tmp/jfk-asof-20260918
```

The cutoff applies to **our observation retrievals**, not inferred upstream availability. Files retrieved later are excluded even if their observing dates are older. No GHCN vintage before the first retained receipt can be reconstructed. Ensemble rebuilds retain the full archive; this switch does not perform forecast evaluation or a forecast cutoff query.

## Temperature definitions

GHCN rejects missing sentinel values and nonblank quality flags, retains source/measurement flags, rejects reversed extrema, and uses `(TMAX + TMIN)/2`. Dates follow NOAA's observing-day convention. METAR screens temperatures to −60…55°C, averages reports into UTC hourly bins, assigns America/New_York dates, and requires every local hour and a completed day. DST days have 23 or 25 expected hours. The daily mean is the midpoint of sampled hourly extrema; the sampled hourly mean is exported separately. This is not equivalent to GHCN climate QC or measured daily extrema, and the series are not spliced.

HDD65 = max(65 − daily mean °F, 0); CDD65 = max(daily mean °F − 65, 0). Complete monthly indices require every calendar day. Partial sums are labeled; gaps are not imputed or zero-filled.

## Ensemble Parquet storage

Selected Open-Meteo products remain `gfs025` (GEFS, 31 members, requested 10 days) and `ecmwf_ifs025` (ECMWF IFS ENS, 51 members, requested 15 days). Requested coordinates are 40.6392, −73.7639 with 2.7 m elevation and land selection. Returned grid coordinates are preserved. Hourly API values may be interpolated from coarser model output.

The growing combined `data/processed/ensemble_temperature.csv` has been replaced by:

- `ensemble_temperature/model=<model>/snapshot_id=<hash>/values.parquet`: member IDs/roles, original variable, UTC targets, original values/units and normalized °C, including nulls.
- `ensemble_snapshots.parquet`: one row per snapshot with location, source provenance, first/last retrieval, receipt count, and initialization/advisory fields.
- `ensemble_receipts.parquet`: one row per validated archived model retrieval, including repeated unchanged polls and publication metadata. `archive_run` plus `raw_file` identifies its retained raw response.
- `ensemble_coverage.csv`: small human-readable receipt/coverage summary. `ensemble_schema.json` and the snapshot table index the current partitions. `ensemble_rejections.json` exposes unusable archives.

Use `weather_pipeline.load_ensemble_archive()` to join metadata onto values, as the notebook does. `build_ensemble_archive()` rebuilds and returns the same logical table and receipts. The snapshot index controls reads so obsolete unindexed partitions cannot leak into results.

A forecast-content hash includes model, requested/returned location, member/time axes, units, values and null masks. It excludes processing duration and retrieval time. An unchanged response creates a receipt, not another member partition. Changed responses remain separate vintages. Every build verifies exact Parquet round trips for all columns and receipts; rebuilding is deterministic. New collections stop writing redundant per-run ensemble CSVs. Existing historical per-run CSVs and all raw files remain intact; no Git history was rewritten.

Before removing the old combined derived CSV, all **103,200 rows, 8 snapshots, 1,104 null temperatures and 12 receipts** were checked against the migrated store. Values, nulls, IDs and provenance matched exactly. The original combined CSV occupied 49,764,221 bytes; the current Parquet values/metadata/receipts occupy approximately 0.36 MB. See `migration_verification.json` and `storage_verification.json`. The live collection added two receipts without adding snapshots.

Verified model initialization remains **null**: the forecast responses do not authenticate a per-target model run. Receipt time, HTTP Date, target start and `generationtime_ms` are never used as initialization. Separate metadata initialization/availability is advisory. Publication warnings include missing/changing metadata, availability within ten minutes, stale availability, missing members and null targets. Unchanged forecasts are distinguishable from collection failures. The notebook's model-spread band is not a calibrated confidence interval and is suppressed where expected members are unavailable.

## Scheduler verification and health

The existing local Codex heartbeat **Archive JFK weather forecasts** (`archive-jfk-weather-forecasts`) was inspected and updated in place; no duplicate was created. Its active six-hour cadence and target task were retained. The prompt now runs the integrated archive/rebuild command with daily GHCN gating and checks `source_health.json`. Routine successful runs and unchanged non-actionable warnings stay quiet; meaningful changes/failures require attention.

Inspection used the actual automation TOML, the read-only local scheduler database, and the target task's execution history. The database's `automation_runs` table had no rows for this heartbeat; target-task records nevertheless showed five archive/build executions with exit code zero. The last-run field before repair was 2026-09-20 02:55:28 UTC, with the next scheduled run at 08:55:26 UTC. Absence of a commit or standalone automation-run row is not evidence of scheduler failure.

| Scheduled task started (UTC) | Saved collection began (UTC) | Commands | Saved before this repair | In local Git HEAD |
|---|---|---|---|---|
| Sep 18 03:17:52 | Sep 19 02:37:40 | archive/build exit 0 | Yes | Yes |
| Sep 19 02:38:51 | Sep 19 02:38:59 | archive/build exit 0 | Yes | Yes |
| Sep 19 08:53:41 | Sep 19 13:22:16 | archive/build exit 0 | Yes | Yes |
| Sep 19 17:50:02 | Sep 20 02:42:12 | archive/build exit 0 | Yes | Yes |
| Sep 20 02:55:28 | Sep 20 02:55:37 | archive/build exit 0 | Yes | Yes |

All five collections retained NWS hourly, TAF, METAR and both ensembles successfully. The two earliest manual collections predated the ensemble workflow; a third manual collection included it. All **eight** pre-repair local archive manifests were already in HEAD, although commits were not six-hour events. The successful forecast-collection gaps included about **28.79, 10.72 and 13.33 hours**. Long task-start-to-download delays are verified, but the evidence does not establish whether sleep, app availability, approvals or other scheduling delays caused them. Raw command evidence is in `data/processed/scheduler_execution_evidence.json`. Remote GitHub state was not separately fetched; the committed comparison is against local HEAD.

Per-source health reports record last attempt, last validated successful retrieval, last changed forecast, observation/forecast coverage end, recent errors, publication warnings, successful retrieval gaps, and current missed/stale flags. Historical gaps are flagged when successful receipts are farther apart than cadence plus grace; current flags distinguish absent attempts from attempts that failed. A recent successful poll can clear current staleness while historical gaps remain visible. Error history can retain resolved errors after recovery. Health is computed when commands run; an offline host cannot alert during its own outage.

Keep the local host powered on, awake, online, with Codex running, this checkout and virtual environment available, and collection network permissions enabled. [Official scheduled-task documentation](https://learn.chatgpt.com/docs/automations?surface=app) requires the computer and app to remain running for local files. This repository installs no daemon or remote workflow. Future on-time execution after this repair has not yet been observed; missed forecast vintages cannot be recovered from current endpoints. These controls improve collection and diagnosis but do not guarantee every model cycle.

## Verification and current coverage

The authorized live collection at **2026-09-20 03:30:46–03:30:57 UTC** archived all seven products below. Seven counts the NWS point lookup separately; there are six meteorological sources. A preceding sandbox-blocked network attempt is retained with transport-error receipts and a failed manifest. It was followed by a successful authorized retry.

| Product | Latest usable coverage from that retrieval |
|---|---|
| GHCN daily extrema | Accepted daily temperatures through **2026-09-17** |
| METAR | Observation through **2026-09-20 02:51 UTC** |
| NWS point lookup | Successfully refreshed at **03:30:52 UTC**; no time-series horizon |
| NWS hourly | Period end **2026-09-26 14:00 UTC** |
| KJFK TAF | Validity end **2026-09-21 06:00 UTC** |
| GEFS `gfs025` | 31 members; non-null targets through **2026-09-29 20:00 UTC**; 93 nulls (last 3 hours/member) |
| ECMWF `ecmwf_ifs025` | 51 members; non-null targets through **2026-10-04 14:00 UTC**; 459 nulls (last 9 hours/member) |

Validation includes regression tests for HTTP failure/retry, metadata/hash/content selection, GHCN fallback/age and revision replacement, historical observation cutoffs, METAR failed-response exclusion, partial-source failures, daily refresh gating, repeated execution, freshness/grace reporting, unchanged forecasts, transport failures, and exact Parquet migration. The live collection was followed by a network-free rebuild and notebook execution. Observation availability still lags receipt, forecast tails remain missing, and ensemble run initialization remains unverified.

Sources: [GHCN format](https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt), [AWC API](https://aviationweather.gov/data/api/), [NWS API](https://www.weather.gov/documentation/services-web-api), [Open-Meteo ensembles](https://open-meteo.com/en/docs/ensemble-api), [publication metadata](https://open-meteo.com/en/docs/model-updates). Ensemble attribution: NOAA GEFS and ECMWF IFS via Open-Meteo, CC BY 4.0.
