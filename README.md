# Uni-Lab-OS Site Show Demo

**English** | [中文](README_zh.md)

A dual-process external device package: a **host** process with a fixed-site
sample rack and a **slave** process with a material workbench. It demonstrates
the two authority-backed chains end to end:

- **Fixed sites** (`@device(available_sites=...)`): declaration -> registry
  template -> authoritative site instances -> occupancy flow;
- **Material CRUD** (`@resource` labware + `materials.*` facade): deck / tip
  rack / well plate creation, `set_substance` reporting, transfer between
  sites, and authoritative deletion — from a slave process, across HostLink.

## Processes and devices

| Process | Graph | Device | Chain it demonstrates |
| --- | --- | --- | --- |
| host | `graph/host.json` | `sample_rack` (2x2 sites A1-B2) | site declaration/instantiation/occupancy |
| slave | `graph/slave.json` | `material_bench` (deck with sites T1-T4) | material create/assign/set_substance/transfer/remove |

Both backends keep HostLink as the materials link: in `hostlink` mode it
carries everything; in `ros2` mode devices talk ROS2 while the slave still
reaches the host's materials authority through HostLink.

## Labware (`site_demo/labware.py`)

- `demo_bench_deck` — 2x2 deck (T1-T4, SBS footprint sites), canonical
  `ResourceSite` semantics, occupancy owned by the microbackend authority;
- `demo_tips_24` — 6x4 tip rack, created **by registry class name**
  (`materials.create("demo_tips_24", name=...)`);
- `demo_plate_12` — 4x3 well plate (2200 ul wells), created **from a local
  draft** with `A1` pre-loaded via `set_substance`, well volumes reported
  live by the snapshot observer.

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
python -m site_demo.smoke --backend hostlink --timeout 60
python -m site_demo.smoke --backend ros2 --timeout 150
```

The smoke boots real host + slave processes and drives three stages:

1. **Closed-loop proofs** (parallel): the rack runs "load A1 -> transfer to
   B2" on the host; the bench runs "ensure deck -> create tips/plate ->
   hydrate well A2 -> relocate plate to T3 -> dispose tips" on the slave.
   Both write machine-readable proof files that are asserted field by field.
2. **Workflows**: two `@workflow` templates were idempotently reported at
   host startup; the smoke runs both through the management HTTP API —
   "位点操作演示" (3 steps on the host rack) and "物料流转演示" (5 steps
   dispatched cross-process to the slave bench, provisioning a second round
   of labware).
3. **Authority final state**: the deck tree is read back from the materials
   authority; T3/T4 must hold the two plates, every tip rack must be gone,
   and well substances must match both stages' writes.

## Manual start

```bash
# terminal 1 — host (owns the materials authority and the management API)
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./site_demo --external_devices_only \
  --visual disable --disable_browser \
  --hostlink_bind 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/host.json

# terminal 2 — slave (material bench, reaches the authority via HostLink)
python -m unilabos --backend hostlink --skip_env_check --is_slave \
  --devices ./site_demo --external_devices_only \
  --visual disable --disable_browser \
  --host_node_ip 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/slave.json
```

For `ros2` mode replace `--backend hostlink` with `--backend ros2` on both
sides and share a `ROS_DOMAIN_ID`; the HostLink flags stay — they carry the
materials link.

## Default sub-workflows (`site_demo/workflows.py`)

- **位点操作演示** — `ctx.run_template("sample_rack_demo/load_sample")`
  auto-fills the device id (single instance of the class in the host graph),
  the next steps use explicit `ctx.run("sample_rack/...")`;
- **物料流转演示** — all five steps use explicit
  `ctx.run("material_bench/...")`: the bench lives in the slave graph, so
  class-based auto-fill is not available to the host at report time.

Site parameters demonstrate both styles: `load_sample(site=...)` and
`relocate_plate(to_site=...)` are annotated with the `SiteSlot` placeholder
type — the registry emits a string schema plus
`placeholder_keys: unilabos_sites`, so the frontend renders a Site picker
(submitting the authoritative ResourceSite uuid) while workflows/scripts may
still pass the label shorthand (consumers resolve uuid/label uniformly).
`transfer_sample(from_label/to_label)` and
`provision_labware(tips_site/plate_site)` keep plain label strings as the
contrasting style.

Declarative steps run strictly serially (`execution_policy.depends_on`
chains each node to the previous one). Workflows get stable uuids derived
from the function's repo-relative path, are upserted at host startup, and
are executed via `POST /api/v1/workflow-tasks`.

## Layout

```text
graph/host.json                    host graph: sample_rack (site instances included)
graph/slave.json                   slave graph: material_bench + deck config
site_demo/
  sample_rack.py                   @device available_sites declaration + three site actions
  material_bench.py                slave-side material CRUD device (five actions + proof)
  labware.py                       @resource deck / tip rack / well plate
  workflows.py                     two @workflow default sub-workflows
  smoke.py                         terminating dual-process real-runtime proof
tests/test_hostlink_smoke.py       HostLink integration assertions
```
