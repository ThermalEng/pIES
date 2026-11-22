# Data Preparation

## Ways to Prepare Data

On the **Data Management** (**Data**) page of the workbench, time-series data can be prepared in three ways:

- **Download template**: Obtain standard CSV headers and sample files.
- **Built-in sample data**: Generate a demonstration hourly dataset in one click (for learning the workflow only; do not use directly in production engineering projects).
- **Upload data**: Upload actual project CSV data files.

## CSV Time and Format Requirements

- **Generic timestamp CSV**: Follows a standard 365-day calendar structure supporting `15min` (35,040 rows), `30min` (17,520 rows), and `60min` (8,760 rows) resolutions. The first column must contain strictly increasing timestamps with a single fixed UTC offset (daylight saving time jumps are not supported).
- **Device-data step CSV**: A self-describing device series file containing `# schema: ies.device-data`, `# resolution`, `# unit.<column>`, and a contiguous `step` column incrementing from `0`. `data_repeat` supports complete day, week, or year periodic baselines; `data_full` must cover every point of the full operating horizon; `data_predict` provides prediction inputs (see [Device Data CSV](../../developer-guide/en-US/formats/device-data-csv.md) for format details).
  *(Note: The modeling inspector currently supports binding `constant`, `data_repeat`, and `data_predict` sources; `data_full` files can still be uploaded and validated here.)*

## Uploading Data Files

1. Click **Upload data** and enter a recognizable dataset name.
2. Select the CSV file to upload:
   - **Generic timestamp CSV**: Select the resolution and fixed UTC offset in the form, and declare each column's field name and unit under **Field descriptions**.
   - **Self-describing step CSV / price series**: The form automatically reads resolution and units from the file's metadata header; no manual UTC offset or field list is needed.
3. Add optional license and description notes as needed, then submit the upload.

## Price Series Files

To configure full-horizon time-of-use electricity tariffs or calendar-month daily price curves, upload a dedicated price CSV file containing `# schema: ies.price-series`, `# resolution`, `# unit`, and the header `step,price`, where `step` increments continuously from `0` and prices support positive, zero, and negative values (see [Price Series CSV](../../developer-guide/en-US/formats/price-series-csv.md)):

- **Full-horizon prices**: Row count matches the total number of operating steps across the project's operating years.
- **Monthly daily curves**: File length equals one complete day (`24` points for `1h`, `48` for `30min`, `96` for `15min`).

After upload passes validation, copy the **Price object ID** from the upload result or version details and paste it under [Finance configuration](calculation-configuration.md#configuring-monthly-daily-price-curves) (price objects do not use the **Bind to project** button for device datasets below).

## Inspecting Quality and Binding to a Project

1. **View Quality report**: Inspect diagnostics after upload. A version can be used in the project only when its quality status is **Good quality** or **Pass**. If blocking errors appear, fix the local CSV file and upload a new version.
2. **Bind to project**: Click **Bind to project** on a passing version (or bind/unbind a specific version in **Versions**). Newly uploaded versions do not switch the active binding automatically; verify and re-bind explicitly.
3. **Inspect version details**: **Versions** records upload history and metadata (version preview displays field definitions rather than loading all rows).

## Common Upload Failure Causes

- **Row count or resolution mismatch**: Verify the sampling interval matches the project baseline and check for extra summary rows or leap-day points (Feb 29).
- **Timestamp / step error**: Check for duplicate or non-increasing timestamps, or whether `step` in a step file fails to increment strictly by `+1` starting from `0`.
- **Missing step file metadata**: Confirm the top of the file declares `# schema`, `# schema_version`, `# resolution`, and `# unit.<column>` for every data column.
- **Units placed inside data cells or undeclared**: Keep data cells purely numeric and declare standard units in the metadata header or field form.

Next: [Calculation Configuration](calculation-configuration.md).
