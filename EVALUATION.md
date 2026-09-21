# Archived JFK forecast evaluation

Run `jfk_forecast_evaluation.ipynb` for separate hourly and daily verification. `forecast_evaluation.py` is the single evaluation module; it reuses authenticated raw-response validation and ensemble/GHCN parsing from `weather_pipeline.py`. It never constructs past forecasts from realized weather, fetches present forecasts as past vintages, or trains a correction. The existing history notebook remains unchanged.

## Reproduction

Use the existing project environment (`requirements-lock.txt` records its package versions):

```sh
.venv/bin/python forecast_evaluation.py
.venv/bin/python execute_notebook.py jfk_forecast_evaluation.ipynb
.venv/bin/python -m unittest -v test_forecast_evaluation test_weather_pipeline test_collection
```

Default execution is offline, pinned to **2026-09-21 00:30 UTC**, with cutoffs beginning September 17. The notebook explicitly disables Requests network calls during evaluation. Jupyter needs a local kernel socket, which can require permission outside a filesystem sandbox; that is not a forecast download. The executor still defaults to the original history notebook when no filename is supplied.

For a different retrospective vintage:

```sh
.venv/bin/python forecast_evaluation.py \
  --start 2026-09-17T00:00:00Z --as-of 2026-09-20T04:00:00Z \
  --cutoff-hours 0,6,12,18 --tolerance-minutes 15 --max-age-hours 24 \
  --output /tmp/jfk-evaluation-asof
```

`Config` also exposes persistence input maximum age (6 hours) and the degree-day base (65°F). Times must include a timezone. Tolerance must be below 30 minutes so an observation cannot match two distinct hourly target times within a cutoff. Cutoffs are fixed UTC hours and do not shift with DST. New later archive receipts cannot change a pinned evaluation. Corrections to input files or receipt metadata change the manifest hashes; raw files should remain immutable.

## Forecast identity and information boundaries

Only original raw NWS hourly and Open-Meteo ensemble JSON with valid 2xx receipts, explicit retrieval times, matching SHA-256 hashes and valid structure enter the analysis. Existing derived observation/forecast CSVs are not forecast inputs. Open-Meteo `gfs025` is the archived GEFS product (31 members); `ecmwf_ifs025` is IFS ENS (51). Grid coordinates, units and requested location are retained in the original responses/URLs; this evaluates the archived grid products at JFK, not an independently extracted native model-grid archive.

NWS content identity hashes normalized target times, period ends, temperature values and geometry. The existing ensemble hash covers model, requested/returned location, time/member axes, values, units and null masks. Processing durations and download timestamps are excluded. Repeated identical content has multiple receipt rows but one snapshot identity. For each distinct snapshot, its **first actual retrieval** is the availability time and age origin. A re-download cannot refresh stale content. Even an exact later reversion to earlier content retains its original age: this is deliberately conservative.

For each model and 00/06/12/18 UTC cutoff, choose the newest distinct snapshot whose first retrieval is at or before cutoff and whose age is at most 24 hours. Ties are deterministic by snapshot hash. If none qualifies, record no snapshot or stale forecast. Selection is snapshot-wide, not target-wise; missing tails are not backfilled from older snapshots. At most one score per model, statistic, cutoff, quantity and target is allowed. A snapshot may serve several **different planned cutoffs**; those are distinct issuance decisions, not new independent observations.

All initialization timestamps remain unverified. NWS `generatedAt`/`updateTime`, HTTP Date, Open-Meteo `generationtime_ms`, first target and advisory model-publication metadata are not used as initialization. **Lead hours = target UTC − first actual retrieval UTC.** Cutoff-relative horizon is retained separately. Targets at or before cutoff are excluded conservatively, as are unobserved future targets beyond as-of.

Outcomes use the most recent eligible observation revision available by the evaluation as-of, even if retrieved after forecast issuance. Predictors/baselines instead resolve observation revisions **after filtering by cutoff availability**. An old observation downloaded after cutoff cannot become a historical predictor. Persistence holds the most recent screened temperature whose observation and receipt times precede cutoff and whose observation is no more than six hours old; no historical availability is inferred.

## Hourly target and quality

Open-Meteo documents `temperature_2m` as instantaneous. Its hourly grid values may interpolate coarser native model time steps. NWS hourly forecasts expose start/end intervals; the explicit evaluation convention is to represent their temperature at **period start** and retain the interval end. Non-hourly or duplicate intervals are rejected as ambiguous. This convention introduces representativeness uncertainty: these are not claimed to be hourly averages or independently verified instantaneous NWS measurements. Sources: [Open-Meteo variables](https://open-meteo.com/en/docs/ensemble-api), [NWS API](https://www.weather.gov/documentation/services-web-api).

METAR targets use the actual `obsTime`, not nominal `reportTime`. Local availability is the maximum of observation, upstream receipt and local retrieval time. Inconsistent temporal order is excluded. Within a vintage, corrections resolve by observation time, then latest upstream receipt, latest local retrieval, and path. A rejected latest correction cannot silently fall back to an older accepted value.

Screening requires KJFK, finite temperature within −60…55°C, dewpoint no more than 0.5°C above temperature, and agreement with the raw temperature group (0.11°C tolerance for the precise tenths group; 0.61°C for rounded whole-degree reporting). Missing or contradictory reports are excluded. Raw reports, `qcField`, quality reason and receipts are retained. `qcField` is opaque here; no undocumented bit decoding or claim of full climate QC is made. The [AWC API](https://aviationweather.gov/data/api/) supplies the observation timestamps and raw reports.

For each hourly target, nearest screened report within an inclusive ±15 minutes wins; equidistant reports select the earlier time. The signed offset is observation time minus target time. Unmatched targets remain null. No interpolation, averaging of nearby reports, observation filling, or zero substitution occurs. Different models use the same report at a common target. Member rows inherit that one outcome; members are never counted as independent verification observations.

## GHCN day alignment and daily quantities

Timing was checked against **the source flags actually present**, not assumed from civil dates. The archived September 2026 JFK TMAX and TMIN values have source flag `1`. The [NCEI GHCN readme](https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt) identifies `1` as NWS CF6 and `D` as the short-delay CF6 feed. The [NWS CF6 specification](https://www.weather.gov/box/product_descriptions) states: “All data are for midnight to midnight LST.” The [NWS observation FAQ](https://www.weather.gov/lot/weather_observations_faq) confirms the window does not move with daylight saving time. These sources were checked on 2026-09-21 UTC.

At JFK, midnight EST to midnight EST is **[05:00 UTC on date D, 05:00 UTC on D+1)** throughout the year. During summer it corresponds to 01:00–01:00 EDT. This evaluation allows only verified CF6 source flags `1`/`D` for both extrema; other sources produce `ambiguous_timing` until their conventions are separately verified. Do not generalize this mapping to all GHCN stations or sources. NCEI documents different windows for some other sources, including UTC-based synoptic summaries.

Every day needs the exact hourly axis for the fixed-standard-time window and finite values for each expected member. The entire day must start after cutoff and finish by as-of. Daily lead is window start minus first retrieval; window end is also retained. Unknown GHCN timing, missing extrema, nonblank QFLAG, reversed extrema, incomplete forecast days, and missing members prevent scoring. Measurement/source flags and raw-file provenance remain in the member tables. Daily member matches are retained even when the overall ensemble cannot be scored.

Daily forecast TMAX/TMIN are **sampled-hourly proxies** that may miss true daily extrema. Forecast midpoint is `(sampled max + sampled min)/2`, not the arithmetic mean of hourly temperatures. Observed midpoint is `(GHCN TMAX + GHCN TMIN)/2`. HDD = max(base°F − midpoint°F, 0), CDD = max(midpoint°F − base°F, 0); output units are °F-days. Each member is transformed to daily quantities before calculating ensemble mean/median, intervals or CRPS. In particular, mean member HDD is not HDD of mean temperature.

GHCN standard-time days always have 24 hours. The window helper also correctly handles civil `America/New_York` days with 23 or 25 distinct UTC hours at DST transitions, tested on March 8 and November 1, 2026. Civil-day METAR aggregates in the existing history notebook are not substituted for GHCN. The evaluations stay separate and no monthly totals are produced.

## Metrics, pairing and counts

Error = forecast − observation. Bias is mean error, MAE is mean absolute error, and RMSE is the square root of mean squared error. NWS is deterministic; ensembles have separate mean and median scores. Every ensemble summary requires exactly member IDs 0…30 or 0…50 (including control) once each and finite values. Unexpected, duplicate, absent or null members prevent scoring; an available-member mean is not silently substituted.

For equally weighted member scenarios x and observation y, empirical CRPS is `mean(|x−y|) − 0.5*mean(|x_i−x_j|)` including diagonal pairs. This is empirical CRPS, not the finite-ensemble “fair” estimator. The 10th and 90th quantiles use linear interpolation. Coverage is the mean inclusive indicator `p10 <= y <= p90`; width is `p90−p10`. Distribution diagnostics appear alongside both ensemble point statistics but must not be pooled twice. An 80% nominal interval is not automatically calibrated, and coverage should be considered alongside width and CRPS.

Lead bins are left-closed/right-open: [0,24), [24,48), [48,72), [72,120), [120,168), [168,240), [240,360) hours, plus overflow. Individual coverage bins use retrieval-based lead. Common-model comparisons intersect **cutoff, target and quantity** across all three products, and bin by **cutoff-relative** horizon to preserve exact pairing despite differing retrieval times. Neither lead definition is called a verified model-cycle lead.

Persistence comparisons are separately restricted to pairs where the model and a cutoff-available baseline both exist. Individual persistence and full-coverage model averages have different samples and must not be treated as paired skill differences.

Tables report cutoff–target pairs, distinct target times, distinct local target dates, matched snapshot counts, and cutoffs. Target dates for hourly coverage use New York civil dates; daily target dates use the GHCN date. Overlapping horizons and serial weather dependence mean pair counts are not independent sample sizes. No significance tests, fitted bias corrections, winning-model claims or pricing are supported here.

## Outputs and audit

`data/evaluation/` contains:

- `hourly_members.parquet`, `daily_members.parquet`: model, content snapshot ID, member, cutoff, retrieval, target, lead, forecast, outcome, signed error, quality, observation provenance and exclusion reason. Hourly rows include signed matching offset and actual observation time; daily rows include expected/sample hours, GHCN flags and window definition. Rows excluded from scoring have null errors.
- `scores.parquet`: one deterministic/mean/median row per decision and target, plus eligible/ineligible persistence rows with predictor provenance. Missing ensemble scenarios exclude the summary even when available member-level matches exist.
- `metrics_individual.csv`, `metrics_common.csv`, `metrics_persistence_paired.csv`, `coverage.csv`: interpretable metrics and their denominators. Empty results remain empty; no zero-skill or perfect-accuracy placeholders.
- `forecast_receipts.csv`, `forecast_snapshots.csv`, `selection.csv`: all as-of validated receipts, deduplicated content and every planned cutoff decision. `metar_vintages.parquet`, `metar_as_of.csv`, `ghcn_as_of.csv` preserve observations used to construct vintage-specific inputs/outcomes.
- `archive_exclusions.csv`, `exclusion_counts.csv`: invalid archives, no forecast/stale cutoff decisions, missing outcomes, past/future targets, incomplete days, missing members and ambiguous timing. Multiple reasons can apply to one target, so reason counts are not additive. Empty archive-rejection output means the candidate raw forecast files passed validation; failed transport attempts have no raw forecast and remain in the collector's existing health logs.
- `run_manifest.json`: pinned parameters, input/receipt/code hashes, package versions, archive date range, sample counts and leakage/uniqueness assertions. Paths are repository-relative.

`test_forecast_evaluation.py` uses small artificial fixtures **only for tests**, never as historical forecasts or evaluation inputs. It checks exact tolerance boundaries, signed offsets, ties, QC rejection, cutoff selection, stale forecasts, content deduplication, as-of filtering, predictor leakage, correction ordering, DST days, daily completeness, unknown source timing, expected member identities/nulls/duplicates, known CRPS/coverage/width/bias/MAE/RMSE, and common-cutoff pairing. Archive-level invariants additionally require future targets, pre-cutoff forecast availability, as-of outcome availability, unique score keys, and complete scored ensembles.

## Executed archive results — as-of 2026-09-21 00:30 UTC

The validated forecast retrieval range is **2026-09-17 21:00:10.787225 UTC through 2026-09-21 00:17:01.697152 UTC**. There are 26 receipts and 17 distinct content snapshots: NWS 10 receipts/7 snapshots, GEFS 8/5, ECMWF 8/5. Later September 21 receipts occur after the last evaluated midnight cutoff and do not enter that cutoff's selection.

Each model has **420 matched cutoff–hour pairs**, covering **72 distinct hourly targets**, **four New York calendar dates (September 17–20)**, **11 scored cutoffs** and **four scored snapshots**. Target times are September 18 01:00 UTC through September 21 00:00 UTC; matched METAR times can be earlier within tolerance. All three products share the same 420 pairs in this run. The initial September 17 cutoffs have no snapshots, and September 19 00:00 UTC has stale forecasts for all three models.

The archive's as-of METAR observation range is August 18 21:51 UTC through September 20 23:51 UTC, with 852 distinct screened reports. Only observations near eligible future forecast targets contribute to forecast scores. All 852 pass the implemented basic screen; that is not a claim of full upstream QC. Persistence has 162 matched pairs per model after availability and age restrictions, so its full-coverage average is not directly comparable with the 420-pair model averages.

| Point forecast | Bias (°C) | MAE (°C) | RMSE (°C) | Cutoff–hour pairs |
|---|---:|---:|---:|---:|
| NWS | -0.027 | 1.157 | 1.375 | 420 |
| GEFS mean | -0.077 | 0.640 | 0.831 | 420 |
| GEFS median | 0.020 | 0.638 | 0.833 | 420 |
| ECMWF mean | -0.487 | 1.152 | 1.320 | 420 |
| ECMWF median | -0.460 | 1.153 | 1.326 | 420 |

These are pooled descriptive errors for this short sample, not evidence of a winning model. The common-cutoff horizon bins have 220 pairs at 0–24h, 136 at 24–48h, 63 at 48–72h, and **only one at 3–5d**. A 3–5d model comparison or calibration inference is unsupported. Retrieval-based 3–5d bins contain 12 pairs but just three unique hours on one local date, also insufficient for a useful comparison. All bins from 5–7d onward are unsupported. Even shorter horizons cover too few weather regimes for stable performance or calibration conclusions.

GHCN accepted extrema stop on **September 17**, before the first wholly forecast daily window. There are **zero complete matched daily days** and therefore no daily TMAX/TMIN/midpoint, HDD/CDD or monthly skill results. The machinery and synthetic known-example tests exist, but no missing historical forecast or daily outcome has been fabricated to populate a chart.

Verification: **32 tests passed** (12 new evaluation tests plus 20 existing collection/pipeline regressions). See `data/evaluation/test_results.txt`. The executed notebook contains nine successfully executed code cells and three plotted figures, inspected for readable axes and legends. Notebook execution had Requests disabled. A second independent offline evaluation exactly reproduced `hourly_members`, `daily_members`, `scores` and `metar_vintages`, with leakage/uniqueness assertions passing; evidence is in `data/evaluation/reproducibility.json`. Existing environment warnings about LibreSSL and sandboxed CPU-feature probes do not affect these offline computations; no network download or new dependency installation was needed.
