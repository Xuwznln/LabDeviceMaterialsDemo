# Uni-Lab-OS Materials Demo

**English** | [中文](README_zh.md)

A dual-process external device package: a **host** process with a fixed-site
sample rack and a **slave** process with a material workbench. It demonstrates
three authority-backed chains end to end:

- **Fixed sites** (`@device(available_sites=...)`): declaration -> registry
  template -> authoritative site instances -> occupancy flow;
- **Material CRUD** (`@resource` labware + `materials.*` facade): deck / tip
  rack / well plate creation, `set_substance` reporting, transfer between
  sites, and authoritative deletion — from a slave process, across HostLink;
- **Outbound plate + fill** (`host_node/apply_deduct_resource` + inventory
  requirements), entirely through the web-style HTTP API: register two plates
  per item, register (deliberately too little) water by quantity, upload a
  three-node workflow, submit it — the scheduler's whole-task reservation
  fails (`plan_not_executable`, neither plate nor water left reserved) —
  restock, resubmit and succeed: a plate is picked, checked out and mounted
  cross-process on the slave deck's T1, water is deducted from its lot, the
  well contents land in the authority. The workflow references the deck by
  **name** only; no uuid appears anywhere in it.

## Processes and devices

| Process | Graph | Device | Chain it demonstrates |
| --- | --- | --- | --- |
| host | `graph/host.json` | `sample_rack` (2x2 sites A1-B2) | site declaration/instantiation/occupancy |
| slave | `graph/slave.json` | `material_bench` (deck with sites T1-T4) | material create/assign/set_substance/transfer/remove |

Both backends keep HostLink as the materials link: in `hostlink` mode it
carries everything; in `ros2` mode devices talk ROS2 while the slave still
reaches the host's materials authority through HostLink.

## Labware (`materials_demo/labware.py`)

- `demo_bench_deck` — 2x2 deck (T1-T4, SBS footprint sites), canonical
  `ResourceSite` semantics, occupancy owned by the microbackend authority;
- `demo_tips_24` — 6x4 tip rack, created **by registry class name**
  (`materials.create("demo_tips_24", name=...)`);
- `demo_plate_12` — 4x3 well plate (2200 ul wells), created **from a local
  draft** with `A1` pre-loaded via `set_substance`, well volumes reported
  live by the snapshot observer;
- `demo_plate_24` — 6x4 well plate (2000 ul wells), registered **per item** in
  stage 3 (`POST /materials/instantiate`, one uuid each) and picked by the
  workflow's `material` requirement;
- `demo_reagent_water` — water, registered **by quantity** in stage 3
  (`POST /materials/lots/inbound`, lots under the template, unit ul), reserved
  and deducted by the workflow's `reagent` requirement.

Per-item vs by-quantity are two ledger shapes, not a "consumable vs reagent"
split: a plate can be tracked by quantity (bulk) and a reagent bottle per item
(it has a uuid and sits on a site).

## Install from GitHub

```bash
unilab package install https://github.com/Xuwznln/LabDeviceMaterialsDemo --ref <commit-sha>
```

For local development:

```bash
git clone https://github.com/Xuwznln/LabDeviceMaterialsDemo.git
cd LabDeviceMaterialsDemo
python -m pip install -e .
```

No AK/SK and no cloud lab required.

## Terminating dual-runtime smoke

```bash
python -m materials_demo.smoke --backend hostlink --timeout 120
python -m materials_demo.smoke --backend ros2 --timeout 200
```

The smoke boots real host + slave processes and drives three stages:

1. **Closed-loop proofs** (parallel): the rack runs "load A1 -> transfer to
   B2" on the host; the bench runs "ensure deck -> create tips/plate ->
   hydrate well A2 -> relocate plate to T3 -> dispose tips" on the slave.
   Both write machine-readable proof files that are asserted field by field.
2. **Workflows + authority final state**: two `@workflow` templates were
   idempotently reported at host startup; the smoke runs both through the
   management HTTP API — "位点操作演示" (3 steps on the host rack) and
   "物料流转演示" (5 steps dispatched cross-process to the slave bench,
   provisioning a second round of labware). The deck tree is then read back
   from the materials authority; T3/T4 must hold the two plates, every tip
   rack must be gone, and well substances must match both stages' writes.
3. **Outbound plate + fill** (devices already exist; everything else is the
   same HTTP call a web button makes):
   - "Inventory → inbound → per item": `POST /api/v1/materials/instantiate`
     two `demo_plate_24`;
   - "Inventory → inbound → by quantity": `POST /api/v1/materials/lots/inbound`
     only **500 ul** of water;
   - "Editor → submit": `POST /api/v1/workflows` + `PUT /api/v1/workflows/{uuid}/graph`
     upload a three-node graph: `host_node/apply_deduct_resource` (`material`
     requirement: one plate; `mount_resource={"name": "bench_deck"}`,
     `slot_on_deck="T1"`) → `material_bench/fill_well` (`reagent` requirement:
     1200 ul water) → `material_bench/bench_report`;
   - client-side precheck (the editor's dry-run equivalent) reports "2 plates
     available, water short by 700 ul" — a hint only, it does not block;
   - `POST /api/v1/workflow-tasks`: the scheduler reserves the whole task
     all-or-nothing → task `failed` / `plan_not_executable` /
     `requirement 'water' is short by 700 ul`, all three node runs `canceled`;
     **the plate was not reserved either** (both still `active`), the lot is
     still `500 / 500 / 0`, no reservation exists, the bench was never called;
   - restock `POST /api/v1/materials/lots/inbound` 10000 ul → `10500 / 10500 / 0`;
   - `POST /api/v1/workflow-tasks` again: `succeeded` — `flow_plate_01` is
     picked (`active → reserved → in_use`), host_node fetches the deck by name
     from the authority, infers the target device from the deck's ownership and
     mounts the plate on T1 cross-process via `RESOURCE_APPEND`; the lot drops
     by 1200 to `9300 / 9300 / 0`; A1 holds `["Water", 1200, "ul"]` in the
     authority; the report `{"T1": "flow_plate_01", "T2": "", "T3": ..., "T4": ...}`
     matches a direct authority read.

   After the precheck failure the retry is a **new task**, not a retry of the
   old one: `plan_not_executable` is terminal; restock, then submit again.

## Manual start

```bash
# terminal 1 — host (owns the materials authority and the management API)
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./materials_demo --external_devices_only \
  --visual disable --disable_browser \
  --hostlink_bind 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/host.json

# terminal 2 — slave (material bench, reaches the authority via HostLink)
python -m unilabos --backend hostlink --skip_env_check --is_slave \
  --devices ./materials_demo --external_devices_only \
  --visual disable --disable_browser \
  --host_node_ip 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/slave.json
```

For `ros2` mode replace `--backend hostlink` with `--backend ros2` on both
sides and share a `ROS_DOMAIN_ID`; the HostLink flags stay — they carry the
materials link.

## Default sub-workflows (`materials_demo/workflows.py`)

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

## API-uploaded workflow (`materials_demo/material_flow_graph.py`)

Stage 3's graph is not reported by `@workflow`; it is uploaded over HTTP the
way the editor canvas does it, with the same node contract: `type=device_action`
+ placeholder `material_uuid` + `action_name`, ordering in
`execution_policy.depends_on`, empty `edges`, inventory requirements in
`meta_data.inventory_requirements`:

```python
# outbound node: no `resource` param — the scheduler injects the picked plate {"uuid": ...} under key=resource
{"key": "resource", "kind": "material", "template_uuid": <template uuid of demo_plate_24>}
# fill node: no `water` param — the scheduler injects {"quantity", "unit", "lots": [{"lot_uuid", "quantity"}]}
{"key": "water", "kind": "reagent", "template_uuid": <template uuid of demo_reagent_water>,
 "quantity": 1200.0, "unit": "ul"}
```

There is no handle edge between the two nodes (the local authority has no node
template catalog); they meet at the **deck site**: the outbound node mounts on
T1 and `fill_well(slot="T1")` takes whatever the bench's own resource tracker
finds on T1. The mount target carries no uuid either —
`mount_resource={"name": "bench_deck"}`: host_node fetches the deck tree from the
authority by name, infers the target device from the deck root's ownership
(the slave-side `material_bench`), and after the `RESOURCE_APPEND` downlink the
bench locates the deck by name in its own tracker and mounts the plate.

## Layout

```text
graph/host.json                    host graph: sample_rack (site instances included)
graph/slave.json                   slave graph: material_bench + deck config
materials_demo/
  sample_rack.py                   @device available_sites declaration + three site actions
  material_bench.py                slave-side material CRUD device (six actions + proof; fill_well consumes the inventory allocation)
  labware.py                       @resource deck / tip rack / 12-well plate / 24-well plate / water
  workflows.py                     two @workflow default sub-workflows (stage 2)
  material_flow_graph.py           stage-3 API-uploaded three-node graph + fixed identities and numbers (no unilabos import)
  smoke.py                         terminating dual-process real-runtime proof
tests/test_hostlink_smoke.py       HostLink integration assertions
```
