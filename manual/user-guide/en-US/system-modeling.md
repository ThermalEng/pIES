# System Modeling

## Before You Begin

Before building a model, watch the currency and autosave status in the workbench header. Canvas edits autosave to the working draft, and the autosave indicator reflects actual HTTP request outcomes. If a save fails, keep the page open, check connectivity to the server, and retry. If concurrent edits from multiple sessions or windows cause a conflict, choose whether to reload the latest server revision as prompted.

## Page Loading and Refresh

- While opening the page or clicking **Refresh** in the toolbar, the canvas remains in a read-only loading state until data finishes loading.
- If loading fails, the page displays an error message and a **Retry** button rather than showing a blank canvas that masquerades as an empty project. Wait until the indicator shows **Autosaved** before manually refreshing so recent edits are not lost.

## Adding and Configuring Devices

1. Open **System Modeling** in the left sidebar.
2. Drag the required device from the left device panel onto the canvas (on an empty canvas, you can also double-click as prompted to add a device).
3. Click a device node on the canvas and configure it in the right **Edit device** inspector:
   - **Device name and asset kind**: Enter a clear instance name and select **Existing** (already-built capacity excluded from new construction capex) or **New** (eligible for new construction investment and capacity planning).
   - **Technical parameters**: Enter values according to the displayed units and valid ranges. Parameters marked as eligible optimization variables can later be configured as decision variables under **Planning config**.

### Using "Capacity-normalized Electric Output"

When a project already has a normalized power output curve rather than meteorological irradiance data, choose **Capacity-normalized Electric Output** in the device panel (distinct from **PV**, which uses solar irradiance and temperature):

1. On the **Data** page, upload and bind a curve dataset (device ID `ies.device.power_curve`, data column `unit_output`, unit declared as `1`, representing per-unit output in kW/kW).
2. Drag the device onto the canvas and enter the reference installed capacity `installed_capacity_kw` (kW) in the inspector. To let the optimizer size the installation, set this parameter as a planning variable later in **Planning config**.
3. Under predefined interface sources, choose `data_repeat` (typical day/week/year cycle) for `unit_output` and bind the corresponding dataset version.

*(Note: If the raw generation data is the absolute output in kW of an existing plant, divide by that plant's installed capacity to normalize into `0..1` per-unit values before uploading. If inverter AC limits or curtailment relations are needed, declare them explicitly in the full YAML via the project saved models entry.)*

### Planning Battery Storage Capacity

The built-in **Battery storage** device allows both `capacity_kwh` (kWh) and `rated_power_kw` (kW) to be optimized simultaneously in **Planning config**. Initial stored energy scales proportionally with the optimized capacity according to the initial SOC ratio. The software does not assume an upper storage-duration limit or an end-of-horizon state-of-charge target automatically; if such boundaries are required, enter them explicitly in **Planning config**.

### Using Saved Project Models

Custom models validated and saved on the project's model page appear alongside built-in devices in the device panel (models with identical names can be distinguished by the adjacent model ID and tooltip). Dragging one onto the canvas automatically populates the data sources declared in the template, which can still be adjusted individually in the inspector.

- Saved project models are visible only within the current project. Restoring a historical version created before a model existed displays only the model set active at that time (subsequent save or delete model actions rebuild the available list from the project model store).

## Connecting Ports and Network Boundaries

1. Drag from an output port of the source device to an input port of the target device to create a connection. Click an edge to inspect its energy carrier or click **Disconnect**.
2. Connections must match energy carriers and go from output to input (or compatible bidirectional ports). Self-loops, duplicate edges, and connecting predefined resource ports such as irradiance or loads are prohibited.
3. **Unconnected port rule**: Different ports on the same device are not automatically bridged. Any connectable input or output port left unconnected on the canvas is treated as zero flow (a closed boundary) during simulation and does not supply free external energy. External electricity purchases, fuel supply, or surplus exports must connect to explicit source/sink devices.

## Editing, Deletion, and Canvas Validation

- When editing multiple attributes or renaming rapidly, changes are saved sequentially and state refreshes after **Autosaved** completes. Deleting a device requires a second confirmation click within three seconds and removes its attached connections as well.
- Clicking **Validate graph** in the toolbar immediately surfaces modeling and connection issues (such as missing data bindings or carrier mismatches), but before submitting a calculation task you still need to run full project validation on the **Validation** page to check data and configuration completeness.

Next: [Data Preparation](data-preparation.md).
