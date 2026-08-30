# UniLabOS Site Show Demo

**English** | [中文](README_zh.md)

This external device package demonstrates the full `@device(available_sites=...)`
fixed-site chain:

1. **Declaration**: `sample_rack.py` declares a 2x2 grid of four sites
   (A1/A2/B1/B2, each with coordinates, size, allowed categories and row/column
   metadata) as `SiteDefinition` literals picked up by the AST registry scan —
   the constant must stay a literal construction because the scanner never
   executes functions;
2. **Instantiation**: at host startup the registry is synced into microbackend
   resource templates and the boot-graph material alignment persists the rack;
   every site receives an authoritative uuid and an
   `occupied_material_uuid` occupancy field;
3. **Occupancy flow**: device actions go through the materials gateway to read
   the site snapshot (`inspect_sites`), create a sample and place it
   (`load_sample`), and move it between sites (`transfer_sample`) — occupancy
   lives entirely in the microbackend authority, the device keeps no copy.

## Install from GitHub

```bash
unilab package install https://github.com/Xuwznln/LabDeviceSiteDemo --ref <commit-sha>
```

For local development:

```bash
git clone https://github.com/Xuwznln/LabDeviceSiteDemo.git
cd LabDeviceSiteDemo
python -m pip install -e .
```

No AK/SK and no cloud lab required.

## Terminating dual-runtime smoke

```bash
python -m site_demo.smoke --backend hostlink --timeout 30
python -m site_demo.smoke --backend ros2 --timeout 60
```

Stage one (closed-loop proof): after boot alignment the rack asserts the
authoritative sites match the decorator declaration item by item (label,
coordinates, allowed categories), then runs "load A1 -> transfer to B2",
writing each site snapshot into `proof.json` — four empty sites initially,
A1 occupied by `proof-sample` after loading, A1 released and B2 occupied
after the transfer.

Stage two (workflow): the smoke runs the "位点操作演示" workflow through the
management HTTP API (load A2 -> transfer to B1 -> inspect) and asserts the
task succeeds with the final snapshot holding both stages' results
(B1=wf-sample, B2=proof-sample, row A empty).

## Manual start

```bash
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./site_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/site_demo.json

python -m unilabos --backend ros2 --disable_hostlink --skip_env_check \
  --devices ./site_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/site_demo.json
```

## Graph file vs. site declaration

The rack node in the graph carries the four site instances explicitly (fixed
uuids, `material_uuid` pointing at the device, `occupied_material_uuid: null`)
with coordinates matching the decorator declaration. Boot alignment adopts the
graph uuids; startup verifies the registry template against the graph sites
and any declaration drift fails fast with a "fixed definition conflict". If
the graph omits `sites`, the microbackend instantiates them from the template
automatically (uuids assigned by the authority).

## Default sub-workflow

`site_demo/workflows.py` declares the "位点操作演示" workflow with the core
repo's `@workflow` decorator:

- `ctx.run_template("sample_rack_demo/load_sample")`: the rack class has a
  single instance in the graph, so the device_id is auto-filled at build time;
- the following two `ctx.run("sample_rack/...")` steps address the instance
  explicitly.

Declarative steps run strictly serially: each node's
`execution_policy.depends_on` points at the previous step and the scheduler
translates it into DAG dependency edges, so the transfer always happens after
loading completes. At host startup the workflow is idempotently upserted
under a stable uuid derived from the function's relative path; the smoke
finds it via `GET /api/v1/workflows`, runs it via
`POST /api/v1/workflow-tasks`, and reads each step's `return_info` from
`GET /api/v1/workflow-tasks/{uuid}/jobs`.

## Layout

```text
graph/site_demo.json               one graph shared by both backends (site instances included)
site_demo/
  sample_rack.py                   @device available_sites declaration + three site actions
  workflows.py                     @workflow default sub-workflow (load/transfer/inspect)
  smoke.py                         terminating real-runtime proof
tests/test_hostlink_smoke.py       HostLink integration assertions
```
