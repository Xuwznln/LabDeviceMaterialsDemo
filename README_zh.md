# UniLabOS 位点展示演示

[English](README.md) | **中文**

这个外部设备包演示 `@device(available_sites=...)` 固定位点的完整链路：

1. **声明**：`sample_rack.py` 用 `SiteDefinition` 字面量声明 2x2 网格的四个
   位点（A1/A2/B1/B2，各带坐标、尺寸、允许类目与行列元数据），AST 扫描进
   注册表——常量必须保持字面量构造，扫描器不执行任何函数；
2. **实例化**：host 启动时注册表同步为微后端资源模板，开机图物料对齐把
   rack 落库，每个位点获得权威 uuid 与 `occupied_material_uuid` 占用字段；
3. **占用流转**：设备动作经 materials gateway 读取位点快照
   （`inspect_sites`）、创建样品并放入位点（`load_sample`）、在位点间转移
   （`transfer_sample`）——占用状态全部由微后端权威维护，设备内存不持有副本。

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
python -m site_demo.smoke --backend hostlink --timeout 30
python -m site_demo.smoke --backend ros2 --timeout 60
```

阶段一（闭环 proof）：rack 等待开机对齐完成后，断言权威位点与装饰器声明
逐项一致（label、坐标、允许类目），随后执行「装载 A1 -> 转移到 B2」，把
每一步的位点快照写出 `proof.json`——初始四位全空，装载后 A1 被
`proof-sample` 占用，转移后 A1 释放、B2 占用。

阶段二（工作流）：通过管理 HTTP API 真实运行「位点操作演示」工作流
（装载 A2 -> 转移到 B1 -> 查看位点），断言任务成功且终态快照同时保留两个
阶段的结果（B1=wf-sample、B2=proof-sample、A 行清空）。

## 手动启动

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

## 图文件与位点声明的一致性

图中 rack 节点显式携带四个位点实例（固定 uuid、`material_uuid` 指向设备、
`occupied_material_uuid: null`），坐标与装饰器声明一致。开机对齐按 adopt
语义以图中 uuid 落库；启动时注册表模板与图中位点做一致性核验，声明漂移
会直接报「固定定义冲突」。图中不写 `sites` 时，微后端也会按模板自动实例化
位点（uuid 由权威分配）。

## 默认子工作流

`site_demo/workflows.py` 用主仓的 `@workflow` 装饰器声明了「位点操作演示」：

- `ctx.run_template("sample_rack_demo/load_sample")`：rack 类在图中只有一个
  实例，构建时自动填充 device_id，无需确认；
- 后续两步 `ctx.run("sample_rack/...")` 显式指定实例。

声明式步骤严格串行：每步节点的 `execution_policy.depends_on` 指向上一步，
调度器把它翻译成 DAG 依赖边，转移必然发生在装载完成之后。host 启动时按
函数相对路径派生稳定 uuid 幂等上报，smoke 经
`GET /api/v1/workflows` 检索、`POST /api/v1/workflow-tasks` 运行、
`GET /api/v1/workflow-tasks/{uuid}/jobs` 读取每步 `return_info`。

## 目录

```text
graph/site_demo.json               两种 backend 共用的一份图（含位点实例）
site_demo/
  sample_rack.py                   @device available_sites 声明 + 三个位点动作
  workflows.py                     @workflow 默认子工作流（装载/转移/查看）
  smoke.py                         有终止条件的真实运行时证明
tests/test_hostlink_smoke.py       HostLink 集成断言
```
