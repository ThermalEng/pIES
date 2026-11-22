# Price Series CSV

> Contract ID: `ies.price-series`; Target `schema_version`: `"1.0.0"`; Recommended Filename: `<price-id>.price.csv`

Price Series CSV delivers full-horizon energy price series or monthly representative daily price curves used across calendar months. Users prepare the raw series before import; the upload endpoint validates file format and produces an immutable object reference, while checking point counts against the project's operating years or single-day length occurs at the task submission boundary.

## File Structure Example

```csv
# schema: ies.price-series
# schema_version: 1.0.0
# resolution: 1h
# unit: CNY/kWh
step,price
0,0.7
1,0.7
2,0.35
```

*(Note: This snippet shows sample rows only; production files MUST include the complete row count required by their target mode.)*

## Metadata and Header Specification

A file consists of `# ` metadata headers, one CSV header line, and at least one data row (comments MUST NOT appear inside the data section; unknown or missing metadata keys are rejected):

| Position | Specification |
|---|---|
| `schema` | Fixed to `ies.price-series` |
| `schema_version` | Fixed to `"1.0.0"` |
| `resolution` | Sampling interval: `15min` / `30min` / `1h`; MUST match the upload form |
| `unit` | Price unit (e.g., `CNY/kWh`); MUST match the currency and energy carrier unit of the referencing finance entry |
| Header | Strictly two columns `step,price` (case-sensitive) |
| `step` column | Contiguous integers incrementing strictly by `+1` starting from `0` |
| `price` column | Finite decimal fixed-point strings (positive, zero, and negative values allowed; `NaN`/`Infinity` prohibited), preserving exact original precision |

## Upload, Referencing, and Materialization

1. The upload endpoint identifies the file via `# schema: ies.price-series`, validates it, writes it to object storage, and returns an opaque `price_object_id` in the quality report.
2. `energy_prices` entries in Finance Profile or Overrides record only the object reference `ref` and metadata `series_meta` (`resolution`, `point_count`, `unit`):
   - When used as a full-horizon series (`time_series`), the actual row count MUST equal the total operating-period step count confirmed on the task;
   - When used for [Monthly Daily Prices](finance-yaml.md#monthly-daily-prices) (`monthly_daily`), the actual row count MUST equal one complete day (`24`, `48`, or `96` points for `1h`, `30min`, or `15min` respectively).
3. During billing, the materialized price series multiplies point-by-point with energy flow along the same axis (`Σ price[t] * e[t]`); positive, zero, and negative prices combine uniformly with the price entry's `direction` (`purchase` / `sale`) to determine accounting sign.
