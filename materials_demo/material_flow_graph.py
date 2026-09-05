"""阶段三「出库装板并加液」：经 API 上传的工作流图 + 全部固定身份与数字。

与网页编排画布提交的是同一份契约（``POST /api/v1/workflows`` → ``PUT
/api/v1/workflows/{uuid}/graph`` → ``POST /api/v1/workflow-tasks``）：

- 节点不引用 workflow_node_template，靠 ``type=device_action`` + ``material_uuid`` +
  ``action_name`` 描述；``material_uuid`` 用与后端 ``DeviceCatalog.material_uuid_of``
  相同的 ``uuid5(WORKFLOW_NAMESPACE, "device:<id>")`` 占位，调度以
  ``meta_data.target_device_id`` 解析目标设备；
- 执行顺序写在 ``execution_policy.depends_on``，``edges`` 为空——本机权威没有节点模板
  目录，连线不能传参数，所以出库节点与加液节点之间靠**台面位点**衔接：出库挂到 T1，
  加液取 T1 上那块板；
- 库存需求写在 ``meta_data.inventory_requirements``（``InventoryRequirement`` 形态）：
  出库节点声明一件 ``material``（按模板选一块 active 的板），加液节点声明一份
  ``reagent``（按模板 FIFO 选 lot）。调度器在任务启动时对整张任务 all-or-nothing 预留，
  任一不足则整张任务 ``plan_not_executable``，两个节点都不会派发；预留成功后把解析出的
  分配注入需求 ``key`` 同名的动作参数（``apply_deduct_resource(resource={"uuid": ...})``、
  ``fill_well(water={"quantity", "unit", "lots": [...]})``）。

挂载目标 ``mount_resource`` 只给台面**名字** ``{"name": "bench_deck"}``：工作流不需要知道
任何 uuid。host_node 按名字回权威取到台面树，由台面根树的归属推断目标设备（slave 侧
material_bench），下发后 bench 用自己的 resource tracker 按名字定位台面并挂载。

本模块不依赖 unilabos，smoke 与外部脚本都可以直接 import；``@resource`` 装饰器里的注册表
id 必须写字面量（注册表按 AST 扫描），labware.py 的定义要与这里的模板名保持一致。
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any, Dict, List, Mapping, Optional

#: uuid5(NAMESPACE_URL, "unilabos://workflow")：与后端 registry/workflows.py 的 WORKFLOW_NAMESPACE 相同。
WORKFLOW_NAMESPACE = uuid_module.uuid5(uuid_module.NAMESPACE_URL, "unilabos://workflow")

#: 注册表模板名（= 权威模板 name；template_uuid 由权威分配，运行时按 name 查）。
FLOW_PLATE_TEMPLATE_NAME = "demo_plate_24"
WATER_TEMPLATE_NAME = "demo_reagent_water"

#: 固定的批次身份：入库、预检、补料、扣减都指向这一瓶水。
WATER_LOT_UUID = "9d2c7e40-5b1a-4f76-a3c8-000000005101"
#: 液体单位与孔位 VolumeTracker 一致（unilab 定制 PLR 只认 ul/ug），不做换算。
WATER_UNIT = "ul"

HOST_NODE_ID = "host_node"
BENCH_DEVICE_ID = "material_bench"
#: 与 graph/slave.json config.deck_name 一致；工作流只按名字引用台面。
BENCH_DECK_NAME = "bench_deck"

FLOW_WORKFLOW_NAME = "出库装板并加液：1200 ul 水"
FLOW_WORKFLOW_TAGS = ["materials-demo", "apply_deduct_resource", "inventory"]
#: 阶段二结束后 T1/T2 空、T3/T4 各留一块板：阶段三把新板挂到 T1。
FLOW_SLOT = "T1"
FLOW_WELL = "A1"
FLOW_SUBSTANCE = "Water"
FILL_VOLUME_UL = 1200.0
#: lot 先只入 500 ul 让预检失败（短缺 700），补 10000 ul 后成功（扣 1200 剩 9300）。
INITIAL_WATER_UL = 500.0
RESTOCK_WATER_UL = 10000.0

#: 按件登记的两块板（第二块用来证明预检失败时没有任何一块被预留）。
FLOW_PLATE_NAMES = ("flow_plate_01", "flow_plate_02")


def placeholder_material_uuid(device_id: str) -> str:
    """设备节点的 material_uuid 占位：与后端 ``DeviceCatalog.material_uuid_of`` 同一推导。"""

    return str(uuid_module.uuid5(WORKFLOW_NAMESPACE, f"device:{device_id}"))


def step_node_uuid(index: int) -> str:
    """稳定的节点 uuid：重复上传时 PUT graph 按 uuid 对齐节点，不会越攒越多。"""

    return str(uuid_module.uuid5(WORKFLOW_NAMESPACE, f"materials_demo:{FLOW_WORKFLOW_NAME}:{index}"))


def _node(
    index: int,
    *,
    name: str,
    device_id: str,
    action: str,
    param: Dict[str, Any],
    depends_on: List[str],
    inventory: Optional[List[Dict[str, Any]]] = None,
    action_type: str = "",
) -> Dict[str, Any]:
    meta_data: Dict[str, Any] = {"target_device_id": device_id, "editor_node_id": f"n{index + 1}"}
    if inventory:
        meta_data["inventory_requirements"] = inventory
    policy: Dict[str, Any] = {}
    if depends_on:
        policy["depends_on"] = depends_on
    return {
        "uuid": step_node_uuid(index),
        "name": name,
        "type": "device_action",
        "material_uuid": placeholder_material_uuid(device_id),
        "action_name": action,
        "action_type": action_type,
        "param": param,
        "pose": {"x": 80 + index * 320, "y": 120},
        "execution_policy": policy,
        "meta_data": meta_data,
    }


def build_flow_graph(
    *,
    plate_template_uuid: str,
    water_template_uuid: str,
    action_types: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """构建可直接 ``PUT /workflows/{uuid}/graph`` 的图（nodes + 空 edges）。

    ``action_types`` 形如 ``{"host_node/apply_deduct_resource": "UniLabJsonCommandAsync"}``，
    HostLink 派发不看它，ROS2 派发用它选消息类型；查不到留空即可。
    """

    types = dict(action_types or {})
    deduct = _node(
        0,
        name=f"出库孔板并挂到 {FLOW_SLOT}",
        device_id=HOST_NODE_ID,
        action="apply_deduct_resource",
        # resource 不写：由调度器按需求 key=resource 注入选中的那块板（{"uuid": ...}）；
        # mount_resource 只给名字：host_node 按名字回权威，目标设备由台面归属推断。
        param={"mount_resource": {"name": BENCH_DECK_NAME}, "slot_on_deck": FLOW_SLOT},
        depends_on=[],
        inventory=[{"key": "resource", "kind": "material", "template_uuid": plate_template_uuid}],
        action_type=types.get(f"{HOST_NODE_ID}/apply_deduct_resource", ""),
    )
    fill = _node(
        1,
        name=f"{FLOW_SLOT} 板 {FLOW_WELL} 加液 {FILL_VOLUME_UL:g} ul",
        device_id=BENCH_DEVICE_ID,
        action="fill_well",
        # water 不写：由调度器按需求 key=water 注入 {"quantity", "unit", "lots": [...]}
        param={"slot": FLOW_SLOT, "well": FLOW_WELL, "substance": FLOW_SUBSTANCE},
        depends_on=[deduct["uuid"]],
        inventory=[
            {
                "key": "water",
                "kind": "reagent",
                "template_uuid": water_template_uuid,
                "quantity": FILL_VOLUME_UL,
                "unit": WATER_UNIT,
            }
        ],
        action_type=types.get(f"{BENCH_DEVICE_ID}/fill_well", ""),
    )
    report = _node(
        2,
        name="台面报告",
        device_id=BENCH_DEVICE_ID,
        action="bench_report",
        param={},
        depends_on=[fill["uuid"]],
        action_type=types.get(f"{BENCH_DEVICE_ID}/bench_report", ""),
    )
    return {
        "name": FLOW_WORKFLOW_NAME,
        "description": (
            f"host_node/apply_deduct_resource 出库一块 {FLOW_PLATE_TEMPLATE_NAME} 挂到台面 {FLOW_SLOT} → "
            f"material_bench/fill_well 把 {FILL_VOLUME_UL:g} ul 水加进 {FLOW_WELL} → bench_report。"
            "两个库存需求（一件板 + 一份水）在任务启动时整张预留，任一不足则整张任务不派发。"
        ),
        "tags": list(FLOW_WORKFLOW_TAGS),
        "nodes": [deduct, fill, report],
        "edges": [],
    }


__all__ = [
    "BENCH_DECK_NAME",
    "BENCH_DEVICE_ID",
    "FILL_VOLUME_UL",
    "FLOW_PLATE_NAMES",
    "FLOW_PLATE_TEMPLATE_NAME",
    "FLOW_SLOT",
    "FLOW_SUBSTANCE",
    "FLOW_WELL",
    "FLOW_WORKFLOW_NAME",
    "FLOW_WORKFLOW_TAGS",
    "HOST_NODE_ID",
    "INITIAL_WATER_UL",
    "RESTOCK_WATER_UL",
    "WATER_LOT_UUID",
    "WATER_TEMPLATE_NAME",
    "WATER_UNIT",
    "WORKFLOW_NAMESPACE",
    "build_flow_graph",
    "placeholder_material_uuid",
    "step_node_uuid",
]
