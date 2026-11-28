# Seasonal Joint Multi-Target Predictor (`ies.predictor.seasonal_joint@1.0.0`)

Reference Python `predictor` plugin implementing deterministic seasonal profile blending and recent moving-average smoothing across multiple targets in a single stage execution.

## Contracts
- Consumes: `ies.stage-input@1.0.0` (`/work/in/request.json` and optional relative `history_csv_rel` CSV files)
- Produces: `ies.prediction-output@1.0.0` (`/work/out/prediction.json` and deterministic `artifacts/<instance>__<interface>.json` descriptors)
