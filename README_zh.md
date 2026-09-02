# Uni-Lab-OS 位点展示演示

[English](README.md) | **中文**

双进程外部设备包：**host** 进程带固定位点样品架，**slave** 进程带物料
工作台。端到端演示两条权威链路：

- **固定位点**（`@device(available_sites=...)`）：声明 -> 注册表模板 ->
  权威位点实例 -> 占用流转；
- **物料 CRUD**（`@resource` 耗材 + `materials.*` 门面）：deck / 枪头盒 /
  孔板创建、`set_substance` 上报、位点间转移、权威删除——全部从 slave
  进程发起，跨 HostLink 访问 host 上的物料权威。

## 进程与设备

| 进程 | 图 | 设备 | 演示链路 |
| --- | --- | --- | --- |
| host | `graph/host.json` | `sample_rack`（2x2 位点 A1-B2） | 位点声明/实例化/占用 |
| slave | `graph/slave.json` | `material_bench`（deck 位点 T1-T4） | 物料创建/挂载/加液/转移/删除 |

两种 backend 都保留 HostLink 作为物料链路：`hostlink` 模式它承载全部
通信；`ros2` 模式设备走 ROS2，slave 仍经 HostLink 访问物料权威。

## 耗材（`site_demo/labware.py`）

- `demo_bench_deck` —— 2x2 台面（T1-T4，SBS footprint 位点），canonical
  `ResourceSite` 语义，占用由微后端权威维护；
- `demo_tips_24` —— 6x4 枪头盒，演示**按注册表类名创建**
  （`materials.create("demo_tips_24", name=...)`）；
- `demo_plate_12` —— 4x3 孔板（单孔 2200 ul），演示**本地草稿创建**，
  `A1` 预置 `set_substance`，孔位液量由快照观察者实时上报。

## 从 GitHub 安装

```bash
unilab package install https://github.com/Xuwznln/LabDeviceSiteDemo --ref <commit-sha>
```

本地开发可使用：

```bash
git clone https://github.com/Xuwznln/LabDeviceSiteDemo.git
cd LabDeviceSiteDemo
python -m pip install -e .
```

本地演示不需要 AK/SK，也不依赖云端实验室。

## 有终止条件的双运行时 smoke

```bash
python -m site_demo.smoke --backend hostlink --timeout 60
python -m site_demo.smoke --backend ros2 --timeout 150
```

smoke 启动真实的 host + slave 双进程，推进三个阶段：

1. **闭环 proof**（并行）：host 上 rack 执行「装载 A1 -> 转移 B2」；
   slave 上 bench 执行「ensure 台面 -> 创建枪头盒/孔板 -> A2 孔加液 ->
   孔板换位到 T3 -> 废弃枪头盒」。两侧各写出可机读 proof 文件，逐字段
   断言。
2. **工作流**：host 启动时已幂等上报两个 `@workflow` 模板，smoke 经管理
   HTTP API 逐个真实运行——「位点操作演示」（3 步，host 侧 rack）与
   「物料流转演示」（5 步，跨进程分发到 slave 侧 bench，补给第二轮
   耗材）。
3. **权威终态**：直读物料权威中的 deck 树，断言 T3/T4 分别留下两轮
   孔板、枪头盒全部删除、孔位内容物与两阶段写入一致。

## 手动启动

```bash
# 终端 1 —— host（持有物料权威与管理 API）
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./site_demo --external_devices_only \
  --visual disable --disable_browser \
  --hostlink_bind 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/host.json

# 终端 2 —— slave（物料工作台，经 HostLink 访问权威）
python -m unilabos --backend hostlink --skip_env_check --is_slave \
  --devices ./site_demo --external_devices_only \
  --visual disable --disable_browser \
  --host_node_ip 127.0.0.1 --hostlink_port 18010 \
  -g ./graph/slave.json
```

`ros2` 模式把两侧 `--backend hostlink` 换成 `--backend ros2` 并共享
`ROS_DOMAIN_ID`；HostLink 参数保留——它承载物料链路。

## 默认子工作流（`site_demo/workflows.py`）

- **位点操作演示** —— `ctx.run_template("sample_rack_demo/load_sample")`
  自动填充 device_id（该类在 host 图中只有一个实例），后续步骤用
  `ctx.run("sample_rack/...")` 显式指定；
- **物料流转演示** —— 五步全部用显式 `ctx.run("material_bench/...")`：
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

## 目录

```text
graph/host.json                    host 图：sample_rack（含位点实例）
graph/slave.json                   slave 图：material_bench + deck 配置
site_demo/
  sample_rack.py                   @device available_sites 声明 + 三个位点动作
  material_bench.py                slave 侧物料 CRUD 设备（五个动作 + proof）
  labware.py                       @resource 台面 / 枪头盒 / 孔板
  workflows.py                     两个 @workflow 默认子工作流
  smoke.py                         有终止条件的双进程真实运行时证明
tests/test_hostlink_smoke.py       HostLink 集成断言
```
