# Uni-Lab-OS 物料演示

[English](README.md) | **中文**

双进程外部设备包：**host** 进程带固定位点样品架，**slave** 进程带物料
工作台。端到端演示三条权威链路：

- **固定位点**（`@device(available_sites=...)`）：声明 -> 注册表模板 ->
  权威位点实例 -> 占用流转；
- **物料 CRUD**（`@resource` 耗材 + `materials.*` 门面）：deck / 枪头盒 /
  孔板创建、`set_substance` 上报、位点间转移、权威删除——全部从 slave
  进程发起，跨 HostLink 访问 host 上的物料权威；
- **出库装板并加液**（`host_node/apply_deduct_resource` + 库存需求）：全部经
  网页式 HTTP API——按件登记两块板、按量登记（故意不够的）水、上传三节点工作流、
  提交后调度器整任务预留失败（`plan_not_executable`，板与水都无预留痕迹）、
  补料、再提交成功——板被选中出库并跨进程挂到 slave 台面 T1，水按 lot 扣减，
  孔位内容物落权威。工作流只按**名字**引用台面，不写任何 uuid。

## 进程与设备

| 进程 | 图 | 设备 | 演示链路 |
| --- | --- | --- | --- |
| host | `graph/host.json` | `sample_rack`（2x2 位点 A1-B2） | 位点声明/实例化/占用 |
| slave | `graph/slave.json` | `material_bench`（deck 位点 T1-T4） | 物料创建/挂载/加液/转移/删除 |

两种 backend 都保留 HostLink 作为物料链路：`hostlink` 模式它承载全部
通信；`ros2` 模式设备走 ROS2，slave 仍经 HostLink 访问物料权威。

## 耗材（`materials_demo/labware.py`）

- `demo_bench_deck` —— 2x2 台面（T1-T4，SBS footprint 位点），canonical
  `ResourceSite` 语义，占用由微后端权威维护；
- `demo_tips_24` —— 6x4 枪头盒，演示**按注册表类名创建**
  （`materials.create("demo_tips_24", name=...)`）；
- `demo_plate_12` —— 4x3 孔板（单孔 2200 ul），演示**本地草稿创建**，
  `A1` 预置 `set_substance`，孔位液量由快照观察者实时上报；
- `demo_plate_24` —— 6x4 孔板（单孔 2000 ul），阶段三**按件登记**
  （`POST /materials/instantiate`，每件一个 uuid），由工作流的 `material` 需求选中出库；
- `demo_reagent_water` —— 水，阶段三**按量登记**（`POST /materials/lots/inbound`，
  以 lot 挂在模板下，单位 ul），由工作流的 `lot` 需求预留与扣减。

按件 / 按量是两种账目形态，与"耗材 / 试剂"无关：板也可以按量记（散装），试剂瓶也
可以按件记（有 uuid、能放到位点）。

## 从 GitHub 安装

```bash
unilab package install https://github.com/Xuwznln/LabDeviceMaterialsDemo --ref <commit-sha>
```

本地开发可使用：

```bash
git clone https://github.com/Xuwznln/LabDeviceMaterialsDemo.git
cd LabDeviceMaterialsDemo
python -m pip install -e .
```

本地演示不需要 AK/SK，也不依赖云端实验室。

## 有终止条件的双运行时 smoke

```bash
python -m materials_demo.smoke --backend hostlink --timeout 120
python -m materials_demo.smoke --backend ros2 --timeout 200
```

smoke 启动真实的 host + slave 双进程（`unilab -g` 按图创建设备），**设备启动后不自跑任何
动作**：host 启动时已幂等上报四个 `@workflow` 模板，smoke 全部经管理 HTTP API
（`POST /api/v1/workflow-tasks`）真实运行并从节点结果断言，推进三个阶段：

1. **第一轮闭环**：「位点闭环演示」（4 步，host 侧 rack）——`verify_site_definition`
   把权威位点与 `@device(available_sites=...)` 声明逐项核对 -> 装载 A1 -> 转移 B2 ->
   复查；「物料闭环演示」（6 步，跨进程分发到 slave 侧 bench）——ensure 台面 -> 创建
   枪头盒/孔板 -> A2 孔加液 -> 孔板换位到 T3 -> 废弃枪头盒 -> 台面报告。
2. **第二轮 + 权威终态**：「位点操作演示」（3 步，A2 -> B1）与「物料流转演示」
   （5 步，补给第二轮耗材，板留 T4）；随后直读物料权威中的 deck 树，断言 T3/T4 分别
   留下两轮孔板、枪头盒全部删除、孔位内容物与两轮写入一致。
3. **出库装板并加液**（设备已在，其余全是网页按钮背后的同一 HTTP 调用）：
   - 「库存 → 入库 → 按件登记」`POST /api/v1/materials/instantiate` 两块 `demo_plate_24`；
   - 「库存 → 入库 → 按量登记」`POST /api/v1/materials/lots/inbound` 只入 **500 ul** 水；
   - 「编排画布 → 提交」`POST /api/v1/workflows` + `PUT /api/v1/workflows/{uuid}/graph`
     上传三节点图：`host_node/apply_deduct_resource`（`material` 需求：一块板；
     `mount_resource={"name": "bench_deck"}`，`slot_on_deck="T1"`）→
     `material_bench/fill_well`（`lot` 需求：1200 ul 水）→ `material_bench/bench_report`；
   - 客户端预检（画布 dry-run 等价物）算出「板可用 2 件、水短缺 700 ul」——只提示不阻断；
   - `POST /api/v1/workflow-tasks`：调度器整任务 all-or-nothing 预留 → 任务 `failed` /
     `plan_not_executable` / `requirement 'water' is short by 700 ul`，三个节点 `canceled`；
     **板也没被预留**（两块仍 `active`）、lot 仍 `500 / 500 / 0`、没有 reservation、bench 没被调用；
   - 补料 `POST /api/v1/materials/lots/inbound` 10000 ul → `10500 / 10500 / 0`；
   - 再 `POST /api/v1/workflow-tasks`：`succeeded`——`flow_plate_01` 被选中（`active → reserved →
     in_use`），host_node 按名字从权威取到台面、由归属推断目标设备、跨进程 `RESOURCE_APPEND`
     挂到 T1；lot 扣 1200 → `9300 / 9300 / 0`；A1 内容物 `["Water", 1200, "ul"]` 落权威；
     报告 `{"T1": "flow_plate_01", "T2": "", "T3": ..., "T4": ...}` 与权威直查一致。

   预检失败后是**新任务**而不是原任务重试：`plan_not_executable` 是终态，补料后重新提交。

## 手动启动

```bash
# 终端 1 —— host（持有物料权威与管理 API）
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./materials_demo --external_devices_only \
  --visual disable --disable_browser \
  --hostlink_bind 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/host.json

# 终端 2 —— slave（物料工作台，经 HostLink 访问权威）
python -m unilabos --backend hostlink --skip_env_check --is_slave \
  --devices ./materials_demo --external_devices_only \
  --visual disable --disable_browser \
  --host_node_ip 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/slave.json
```

`ros2` 模式把两侧 `--backend hostlink` 换成 `--backend ros2` 并共享
`ROS_DOMAIN_ID`；HostLink 参数保留——它承载物料链路。

## 默认子工作流（`materials_demo/workflows.py`）

设备启动后不自跑任何动作，两轮闭环都是 `@workflow`，由管理 API 触发：

- **位点闭环演示** —— 首步 `ctx.run_template("sample_rack_demo/verify_site_definition")`
  核对权威位点与声明一致（不一致该步直接失败），随后装载 A1 -> 转移 B2 -> 复查；
- **物料闭环演示** —— 六步全部用显式 `ctx.run("material_bench/...")`：准备台面 ->
  补给 -> A2 加液 -> 换位 T3 -> 废弃枪头盒 -> 报告；
- **位点操作演示** —— `ctx.run_template("sample_rack_demo/load_sample")`
  自动填充 device_id（该类在 host 图中只有一个实例），后续步骤用
  `ctx.run("sample_rack/...")` 显式指定；
- **物料流转演示** —— 第二轮五步，同样显式 `ctx.run("material_bench/...")`：
  bench 在 slave 图中，host 上报时无法按类名解析实例。

位点参数演示两种风格：`load_sample(site=...)` 与 `relocate_plate(to_site=...)`
标注 `SiteSlot` 占位类型——注册表生成字符串 schema 并注入
`placeholder_keys: unilabos_sites`，前端按 Site 选择器渲染（提交权威
ResourceSite uuid），工作流/脚本仍可直接传 label 便捷形态（消费侧对
uuid/label 统一解析）；`transfer_sample(from_label/to_label)` 与
`provision_labware(tips_site/plate_site)` 保留纯 label 字符串作对照。

声明式步骤严格串行（`execution_policy.depends_on` 逐步链接）。工作流按
函数相对路径派生稳定 uuid，host 启动时幂等上报，经
`POST /api/v1/workflow-tasks` 真实运行。

## 经 API 上传的工作流（`materials_demo/material_flow_graph.py`）

阶段三的图不是 `@workflow` 上报的，而是像编排画布一样经 HTTP 上传，节点写法与
画布提交契约一致：`type=device_action` + 占位 `material_uuid` + `action_name`，
顺序写 `execution_policy.depends_on`，`edges` 为空，库存需求写
`meta_data.inventory_requirements`：

```python
# 出库节点：resource 参数不写，由调度器按需求 key=resource 注入选中的板 {"uuid": ...}
{"key": "resource", "kind": "material", "template_uuid": <demo_plate_24 的模板 uuid>}
# 加液节点：water 参数不写，由调度器注入 {"quantity", "unit", "lots": [{"lot_uuid", "quantity"}]}
{"key": "water", "kind": "lot", "template_uuid": <demo_reagent_water 的模板 uuid>,
 "quantity": 1200.0, "unit": "ul"}
```

两个节点之间没有 handle 连线（本机权威没有节点模板目录），靠**台面位点**衔接：
出库挂到 T1，`fill_well(slot="T1")` 用 bench 自己的 resource tracker 取 T1 上那块板。
挂载目标同样不写 uuid——`mount_resource={"name": "bench_deck"}`，host_node 按名字
回权威取台面树，由台面根树的归属推断目标设备（slave 侧 `material_bench`），下发
`RESOURCE_APPEND` 后 bench 按名字在自己的 tracker 里定位台面并挂载。

## 目录

```text
graph/host.json                    host 图：sample_rack（含位点实例）
graph/slave.json                   slave 图：material_bench + deck 配置
materials_demo/
  sample_rack.py                   @device available_sites 声明 + 四个位点动作（含 verify_site_definition；不自跑）
  material_bench.py                slave 侧物料 CRUD 设备（七个动作；fill_well 消费库存分配；不自跑）
  labware.py                       @resource 台面 / 枪头盒 / 12 孔板 / 24 孔板 / 试剂水
  workflows.py                     四个 @workflow 默认子工作流（两轮闭环）
  material_flow_graph.py           阶段三经 API 上传的三节点图 + 固定身份与数字（不依赖 unilabos）
  smoke.py                         有终止条件的双进程真实运行时证明
tests/test_hostlink_smoke.py       HostLink 集成断言
```
