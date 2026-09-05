"""有限时启动真实 host/slave 双进程，全部经管理 HTTP API 运行工作流并断言。

设备启动后不自跑任何动作。host 启动时已把 materials_demo/workflows.py 里的四个
@workflow 幂等上报到本机 Workflow Authority，本脚本按顺序经 ``POST /workflow-tasks``
真实运行，断言只看各节点返回值（``/workflow-tasks/{uuid}/jobs``）：

阶段一（第一轮闭环）：

- 「位点闭环演示」四步指向 host 侧 sample_rack：核对权威位点与 available_sites 声明
  逐项一致 -> 装载 A1 -> 转移 B2 -> 复查；
- 「物料闭环演示」六步跨进程分发到 slave 侧 material_bench：ensure deck -> 创建 tip
  rack/孔板 -> set_substance 上报 -> transfer 换位到 T3 -> remove 删除 -> 报告，全部经
  HostLink 访问 host 上的微后端物料权威。ros2 backend 同样走该链路（ROS2 承载设备
  通信，HostLink 承载物料）。

阶段二（第二轮）：「位点操作演示」三步（A2 -> B1）与「物料流转演示」五步（第二轮
耗材，板留 T4）。

终局：经管理 API 直读物料权威中 deck 树，断言 T3/T4 分别留下两轮孔板、
枪头盒全部删除、孔位内容物与两阶段写入一致。

阶段三（出库装板并加液，全部是网页按钮背后的同一 HTTP 调用）：设备已在
（host/slave 双进程），接着复现网页操作序列——

1. 「库存 → 入库 → 按件登记」``POST /materials/instantiate`` 两块 24 孔板；
2. 「库存 → 入库 → 按量登记」``POST /materials/lots/inbound`` 只入 500 ul 水（故意不够）；
3. 「编排画布 → 提交」``POST /workflows`` + ``PUT /workflows/{uuid}/graph`` 上传三节点图
   （host_node/apply_deduct_resource 出库挂到 T1 → material_bench/fill_well → bench_report；
   一件 material 需求 + 一份 lot 需求；挂载目标只给台面名字，不写任何 uuid）；
4. 客户端预检（画布 dry-run 等价物）：水短缺 700 ul、板可用 2 件——只提示不阻断；
5. ``POST /workflow-tasks``：调度器整任务 all-or-nothing 预留 → 水不足 → ``failed`` /
   ``plan_not_executable`` / ``short by 700 ul``；三个节点 ``canceled``；**板也没被预留**、
   lot 原样、没有 reservation、bench 没被调用；
6. 补料 ``POST /materials/lots/inbound`` 10000 ul → 10500；
7. 再 ``POST /workflow-tasks``：``succeeded``——板被选中（active → reserved → in_use），
   host_node 按名字取到台面、由归属推断目标设备、跨进程 RESOURCE_APPEND 挂到 T1；
   lot 扣 1200 → 9300；A1 内容物 ``["Water", 1200, "ul"]`` 落权威；报告与权威直查一致。

失败后是**新任务**而不是原任务重试：``plan_not_executable`` 是终态，补料后重新提交。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence
from uuid import uuid4

from .material_flow_graph import (
    BENCH_DECK_NAME,
    FILL_VOLUME_UL,
    FLOW_PLATE_NAMES,
    FLOW_PLATE_TEMPLATE_NAME,
    FLOW_SLOT,
    FLOW_SUBSTANCE,
    FLOW_WELL,
    FLOW_WORKFLOW_NAME,
    HOST_NODE_ID,
    INITIAL_WATER_UL,
    RESTOCK_WATER_UL,
    WATER_LOT_UUID,
    WATER_TEMPLATE_NAME,
    WATER_UNIT,
    BENCH_DEVICE_ID,
    build_flow_graph,
    step_node_uuid,
)

#: 与 materials_demo/workflows.py 保持一致（smoke 独立运行，不 import 设备包）。
SITE_LOOP_WORKFLOW_NAME = "位点闭环演示"
MATERIAL_LOOP_WORKFLOW_NAME = "物料闭环演示"
SITE_TOUR_WORKFLOW_NAME = "位点操作演示"
MATERIAL_FLOW_WORKFLOW_NAME = "物料流转演示"

#: 阶段三的期望数字（全新数据库）。
EXPECTED_SHORT_UL = FILL_VOLUME_UL - INITIAL_WATER_UL
WATER_AFTER_RESTOCK_UL = INITIAL_WATER_UL + RESTOCK_WATER_UL
WATER_AFTER_FILL_UL = WATER_AFTER_RESTOCK_UL - FILL_VOLUME_UL
EXPECTED_WELL_CONTENT = [FLOW_SUBSTANCE, FILL_VOLUME_UL, WATER_UNIT]

#: 与 graph/slave.json config.deck_uuid 保持一致（权威终态直查用）。
DECK_UUID = "9d2c7e40-5b1a-4f76-a3c8-000000003001"

#: 与 sample_rack.RACK_SITES 声明一致的期望快照（label -> (x, y)）。
EXPECTED_SITE_POSITIONS = {
    "A1": (60.0, 40.0),
    "A2": (180.0, 40.0),
    "B1": (60.0, 160.0),
    "B2": (180.0, 160.0),
}

#: 物料闭环里两轮耗材的命名（第 1 轮 =「物料闭环演示」，第 2 轮 =「物料流转演示」）。
ROUND1_TIPS, ROUND1_PLATE = "bench_tips_r1", "bench_plate_r1"
ROUND2_TIPS, ROUND2_PLATE = "bench_tips_r2", "bench_plate_r2"

WATER_40 = ["Water", 40.0, "ul"]
BUFFER_25 = ["Buffer", 25.0, "ul"]
DYE_15 = ["Dye", 15.0, "ul"]


def _occupancy(sites: list[dict[str, Any]]) -> dict[str, str]:
    return {entry["label"]: entry["occupied_by"] for entry in sites}


def _job_values(workflow_proof: dict[str, Any], expected_name: str, count: int) -> list[Any]:
    assert workflow_proof["workflow_name"] == expected_name
    assert workflow_proof["task_status"] == "succeeded", f"工作流任务未成功: {workflow_proof}"
    jobs = workflow_proof["jobs"]
    assert len(jobs) == count, f"应有 {count} 个节点 job: {jobs}"
    assert all(job["status"] == "succeeded" for job in jobs), f"存在失败 job: {jobs}"
    # jobs 按 topological_index 返回；节点 uuid 序 == 声明序
    return [job["return_info"]["return_value"] for job in jobs]


def assert_site_loop_workflow(workflow_proof: dict[str, Any], backend: str) -> None:
    """「位点闭环演示」：核对声明 -> 装载 A1 -> 转移 B2 -> 复查（host 侧 rack，四步）。"""

    verify, load, moved, inspected = _job_values(workflow_proof, SITE_LOOP_WORKFLOW_NAME, 4)

    # 1) 权威位点与 @device(available_sites=...) 声明逐项一致（不一致该步会直接失败）
    assert verify["success"] is True and verify["matches"] is True, verify
    assert verify["backend"] == backend, verify
    assert [entry["label"] for entry in verify["actual"]] == ["A1", "A2", "B1", "B2"], verify
    assert {entry["label"]: (entry["x"], entry["y"]) for entry in verify["actual"]} == EXPECTED_SITE_POSITIONS

    # 2) 装载 A1 -> 转移 B2，占用状态由权威维护
    assert load["success"] is True and load["site_label"] == "A1" and load["sample_name"] == "proof-sample", load
    assert (moved["from_label"], moved["to_label"]) == ("A1", "B2"), moved
    assert moved["sample_uuid"] == load["sample_uuid"], (moved, load)
    after_transfer = _occupancy(inspected["sites"])
    assert after_transfer == {"A1": "", "A2": "", "B1": "", "B2": "proof-sample"}, after_transfer


def assert_material_loop_workflow(workflow_proof: dict[str, Any]) -> None:
    """「物料闭环演示」：六步跨进程分发到 slave 侧 bench（第 1 轮耗材）。"""

    prepared, provisioned, hydrated, relocated, disposed, report = _job_values(
        workflow_proof, MATERIAL_LOOP_WORKFLOW_NAME, 6
    )

    # 1) 台面 ensure：固定 uuid 幂等创建，四个位点 T1-T4
    assert prepared["deck_uuid"] == DECK_UUID
    assert prepared["site_labels"] == ["T1", "T2", "T3", "T4"]

    # 2) 补给：类名创建的 tip rack 上 T1，草稿创建的孔板上 T2（A1 预置 Water）
    assert provisioned["round"] == 1
    assert provisioned["plate_a1_substances"] == [WATER_40], provisioned
    assert provisioned["sites"] == {"T1": ROUND1_TIPS, "T2": ROUND1_PLATE, "T3": "", "T4": ""}, provisioned

    # 3) 孔位加液：本地 add_liquid + commit 由快照观察者自动上报权威
    assert hydrated["well"] == "A2"
    assert hydrated["substances"] == [BUFFER_25], hydrated

    # 4) 换位：权威先落位，unload -> load 投影重建了本地实例
    assert relocated["to_site"] == "T3"
    assert relocated["instance_rebuilt"] is True
    assert relocated["sites"]["T2"] == "" and relocated["sites"]["T3"] == ROUND1_PLATE

    # 5) 删除：权威递归删除整棵树（根 + 24 个枪头位）并自动释放位点
    assert disposed["deleted_in_authority"] is True
    assert disposed["tips_uuid"] == provisioned["tips_uuid"]
    assert disposed["root_removed"] is True
    assert disposed["removed_count"] == 25, disposed
    assert disposed["sites"]["T1"] == "", disposed

    # 6) 终态报告：只剩 T3 上的第一轮孔板，孔位内容物齐全
    assert report["sites"] == {"T1": "", "T2": "", "T3": ROUND1_PLATE, "T4": ""}
    assert report["wells"] == {ROUND1_PLATE: {"A1": [WATER_40], "A2": [BUFFER_25]}}, report


def assert_site_tour_workflow(workflow_proof: dict[str, Any]) -> None:
    """「位点操作演示」：三步串行成功，终态快照保留两阶段占用。"""

    assert workflow_proof["workflow_name"] == SITE_TOUR_WORKFLOW_NAME
    assert workflow_proof["task_status"] == "succeeded", (
        f"工作流任务未成功: {workflow_proof}"
    )
    jobs = workflow_proof["jobs"]
    assert len(jobs) == 3, f"应有 3 个节点 job: {jobs}"
    assert all(job["status"] == "succeeded" for job in jobs), f"存在失败 job: {jobs}"

    load_result, transfer_result, inspect_result = [
        job["return_info"]["return_value"] for job in jobs
    ]
    assert load_result["success"] is True
    assert load_result["site_label"] == "A2"
    assert load_result["sample_name"] == "wf-sample"
    assert (transfer_result["from_label"], transfer_result["to_label"]) == (
        "A2",
        "B1",
    )
    snapshot = _occupancy(inspect_result["sites"])
    assert snapshot == {
        "A1": "",
        "A2": "",
        "B1": "wf-sample",
        "B2": "proof-sample",
    }, snapshot


def assert_material_flow_workflow(workflow_proof: dict[str, Any]) -> None:
    """「物料流转演示」：五步跨进程串行成功，第二轮耗材落到 T4。"""

    assert workflow_proof["workflow_name"] == MATERIAL_FLOW_WORKFLOW_NAME
    assert workflow_proof["task_status"] == "succeeded", (
        f"工作流任务未成功: {workflow_proof}"
    )
    jobs = workflow_proof["jobs"]
    assert len(jobs) == 5, f"应有 5 个节点 job: {jobs}"
    assert all(job["status"] == "succeeded" for job in jobs), f"存在失败 job: {jobs}"

    provision, hydrate, relocate, dispose, report = [
        job["return_info"]["return_value"] for job in jobs
    ]
    assert provision["round"] == 2
    assert provision["plate_a1_substances"] == [WATER_40]
    assert hydrate["well"] == "B1" and hydrate["substances"] == [DYE_15]
    assert relocate["to_site"] == "T4" and relocate["instance_rebuilt"] is True
    assert dispose["deleted_in_authority"] is True
    assert report["sites"] == {
        "T1": "",
        "T2": "",
        "T3": ROUND1_PLATE,
        "T4": ROUND2_PLATE,
    }, report
    assert report["wells"] == {
        ROUND1_PLATE: {"A1": [WATER_40], "A2": [BUFFER_25]},
        ROUND2_PLATE: {"A1": [WATER_40], "B1": [DYE_15]},
    }, report


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _stop(process: subprocess.Popen[Any]) -> None:
    """结束 unilab 进程树（权威进程 + 它拉起的 Host 子进程），不留孤儿占着数据库文件。"""

    if process.poll() is None:
        if os.name == "nt":
            # Windows 的 TerminateProcess 不传递给子进程，按进程树整体结束
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _graph_path(repo_root: Path, filename: str) -> Path:
    """优先读取 wheel 安装的数据文件，editable/source 模式回退到仓库 graph。"""

    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "materials_demo"
        / "graph"
        / filename
    )
    if installed.is_file():
        return installed
    source = repo_root / "graph" / filename
    if source.is_file():
        return source
    raise FileNotFoundError(f"Site demo graph 未随 distribution 安装: {filename}")


def _base_command(
    repo_root: Path,
    graph: Path,
    database_root: Path,
    management_port: int,
    backend: str,
) -> list[str]:
    import unilabos

    config_path = (
        Path(unilabos.__file__).resolve().parent
        / "config"
        / "example_config.py"
    )
    return [
        sys.executable,
        "-m",
        "unilabos",
        "--backend",
        backend,
        "--skip_env_check",
        "--devices",
        str(repo_root / "materials_demo"),
        "--external_devices_only",
        "--visual",
        "disable",
        "--disable_browser",
        "--port",
        str(management_port),
        "--server_database_root",
        str(database_root),
        "--working_dir",
        str(database_root / "work"),
        "--config",
        str(config_path),
        "-g",
        str(graph),
    ]


# ---------------------------------------------------------------------------
# 管理 HTTP API（工作流阶段 + 权威终态直查）
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    """管理 API 返回了非 2xx；``status`` / ``body`` 供调用方分支。"""

    def __init__(self, status: int, body: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _api_request(
    port: int,
    path: str,
    payload: Any = None,
    *,
    method: str | None = None,
    timeout: float = 15.0,
) -> Any:
    """请求管理 API；workflow 风格 {"code":0,"data":...} 自动解包，其余路由原样返回。"""

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    method = method or ("GET" if payload is None else "POST")
    # GET 不能带 JSON Content-Type：服务端 Backend 路由会尝试解码空 body 而报错
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise ApiError(exc.code, text, f"{method} {path} -> HTTP {exc.code}: {text}") from None
    body = json.loads(raw.decode("utf-8")) if raw else None
    if isinstance(body, dict) and "code" in body and ("data" in body or "error" in body):
        if body["code"] != 0:
            raise RuntimeError(f"管理 API {method} {path} 返回错误: {body}")
        return body.get("data")
    return body


def _wait_runtime_ready(
    port: int,
    host: subprocess.Popen[Any],
    slave: subprocess.Popen[Any] | None,
    deadline: float,
) -> None:
    """等管理 API 就绪、执行面在线、slave 经 HostLink 接入且其设备已登记进权威。

    工作流要跨进程派发到 slave 侧 bench，只有这三件都成立才不会因"设备缺失"在派发前失败。
    """

    def actions_online(action_names: set[str]) -> bool:
        # 执行端点快照已上报这些动作能力：调度器此刻才能把 job 派发到设备（ROS2 节点起得慢）
        endpoints = _api_request(port, "/runtime/endpoints?state=online&limit=100")
        reported = {
            capability["action_name"]
            for endpoint in endpoints
            for capability in endpoint.get("action_capabilities", [])
            if capability.get("state", "active") == "active"
        }
        return action_names <= reported

    def ready() -> bool:
        health = _api_request(port, "/health")
        if health.get("status") != "ok" or health.get("execution") != "ready":
            return False
        wanted = {"verify_site_definition", "load_sample"}
        if slave is not None:
            if not _api_request(port, "/hostlink/peers").get("peers"):
                return False
            # slave 开机图对齐完成：bench 设备根物料出现在权威中
            _api_request(port, "/materials/instances/by-resource-id/material_bench")
            wanted |= {"prepare_bench", "fill_well"}
        return actions_online(wanted)

    while time.monotonic() < deadline:
        if host.poll() is not None or (slave is not None and slave.poll() is not None):
            raise RuntimeError("runtime process exited before it became ready")
        try:
            if ready():
                return
        except (urllib.error.URLError, OSError, ApiError, RuntimeError, AttributeError, KeyError, TypeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("管理 API / slave 接入 / 设备动作能力未在时限内就绪")


def run_workflow_stage(
    management_port: int, workflow_name: str, timeout: float
) -> dict[str, Any]:
    """检索上报的默认子工作流 -> 创建任务 -> 等待终态 -> 汇总节点结果。"""

    deadline = time.monotonic() + timeout

    workflow_uuid = ""
    while time.monotonic() < deadline:
        try:
            listing = _api_request(
                management_port, "/workflows?page=1&page_size=50"
            )
        except (urllib.error.URLError, OSError, ApiError):
            time.sleep(0.3)
            continue
        matches = [
            item for item in listing["items"] if item["name"] == workflow_name
        ]
        if matches:
            workflow_uuid = matches[0]["uuid"]
            break
        time.sleep(0.3)
    if not workflow_uuid:
        raise RuntimeError(
            f"{timeout}s 内未在管理 API 检索到工作流 {workflow_name!r}"
        )

    task = _api_request(
        management_port,
        "/workflow-tasks",
        {"workflow_uuid": workflow_uuid, "run_mode": "normal"},
    )
    task_uuid = task["uuid"]

    status = str(task.get("status") or "")
    while time.monotonic() < deadline and status not in {"succeeded", "failed"}:
        time.sleep(0.3)
        current = _api_request(management_port, f"/workflow-tasks/{task_uuid}")
        status = str(current.get("status") or "")
    if status not in {"succeeded", "failed"}:
        raise RuntimeError(f"工作流任务 {task_uuid} 未在 {timeout}s 内结束: {status}")

    jobs = _api_request(management_port, f"/workflow-tasks/{task_uuid}/jobs")
    return {
        "workflow_uuid": workflow_uuid,
        "workflow_name": workflow_name,
        "task_uuid": task_uuid,
        "task_status": status,
        # task 级 output 不进公开 HTTP 契约，节点结果一律取 job.return_info
        "jobs": [
            {
                "uuid": job["uuid"],
                "status": job["status"],
                "return_info": dict(job.get("return_info") or {}),
                "error_info": list(job.get("error_info") or []),
            }
            for job in jobs
        ],
    }


def read_authority_final_state(management_port: int) -> dict[str, Any]:
    """经管理 API 直读物料权威的 deck 树，返回可断言的精简终态。"""

    tree = _api_request(
        management_port, f"/materials/instances/{DECK_UUID}/tree"
    )
    nodes = tree["nodes"]
    by_uuid = {node["material"]["material_uuid"]: node for node in nodes}
    deck = by_uuid[DECK_UUID]
    site_occupancy = {
        site["label"]: (
            by_uuid[site["occupied_material_uuid"]]["material"]["name"]
            if site.get("occupied_material_uuid")
            else ""
        )
        for site in deck["sites"]
    }
    return {
        "site_occupancy": dict(sorted(site_occupancy.items())),
        "class_names": sorted(
            {node["material"]["class_name"] for node in nodes}
        ),
        "root_children": sorted(
            node["material"]["name"]
            for node in nodes
            if node["material"]["parent_material_uuid"] == DECK_UUID
        ),
    }


def assert_authority_final_state(state: dict[str, Any]) -> None:
    """终局：两轮孔板留在 T3/T4，枪头盒全部删除（TipRack 不在树内）。"""

    assert state["site_occupancy"] == {
        "T1": "",
        "T2": "",
        "T3": ROUND1_PLATE,
        "T4": ROUND2_PLATE,
    }, state
    assert state["root_children"] == [ROUND1_PLATE, ROUND2_PLATE], state
    assert "TipRack" not in state["class_names"], state


# ---------------------------------------------------------------------------
# 阶段三：出库装板并加液（按件 + 按量库存、API 上传工作流、预检失败 → 补料 → 成功）
# ---------------------------------------------------------------------------


def _mutation(operation: str, effect_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """materials.v1 写信封；浏览器 / 操作员发起的写操作 actor_type 用 human。"""

    return {
        "protocol_version": "materials.v1",
        "command_uuid": str(uuid4()),
        "effect_key": effect_key,
        "operation": operation,
        "actor_type": "human",
        "actor_uuid": "materials-demo-operator",
        "observed_at_ms": int(time.time() * 1000),
        "preconditions": [],
        "payload": payload,
    }


def flow_templates(port: int) -> dict[str, str]:
    """注册表 @resource 在 host 启动时已同步为权威模板；按 name 取运行时分配的 uuid。"""

    wanted = {FLOW_PLATE_TEMPLATE_NAME, WATER_TEMPLATE_NAME}
    found = {
        item["name"]: item["template_uuid"]
        for item in _api_request(port, "/materials/templates?include_definition=false")
        if item["name"] in wanted
    }
    missing = wanted - set(found)
    if missing:
        raise RuntimeError(f"权威中没有模板 {sorted(missing)}（registry 资源未同步）")
    return found


def instantiate_plate(port: int, name: str) -> str:
    """「入库 → 按件登记」：按注册表资源类实例化一块板，权威发 uuid，进入在库物料。"""

    result = _api_request(
        port,
        "/materials/instantiate",
        _mutation(
            "instantiate_material",
            f"instantiate:{name}",
            {"registry_class": FLOW_PLATE_TEMPLATE_NAME, "name": name},
        ),
    )
    root = next(node for node in result["data"]["nodes"] if node["material"]["parent_material_uuid"] is None)
    return str(root["material"]["material_uuid"])


def inbound_water(port: int, template_uuid: str, quantity: float) -> dict[str, Any]:
    """「入库 → 按量登记」/「补料」：向固定 lot 追加数量（同一 lot 重复入库累加）。"""

    result = _api_request(
        port,
        "/materials/lots/inbound",
        _mutation(
            "inbound_inventory_lot",
            f"inbound:{WATER_LOT_UUID}:{uuid4()}",
            # 权威把 payload 与类型化请求体逐字段比对，InventoryLotInbound 的字段要给全
            {
                "lot_uuid": WATER_LOT_UUID,
                "template_uuid": template_uuid,
                "batch_no": "demo-water",
                "unit": WATER_UNIT,
                "quantity": float(quantity),
                "expiry_at_ms": None,
            },
        ),
    )
    return lot_view(port)


def lot_view(port: int) -> dict[str, Any]:
    lot = _api_request(port, f"/materials/lots/{WATER_LOT_UUID}")
    return {
        "lot_uuid": lot["lot_uuid"],
        "quantity_total": float(lot["quantity_total"]),
        "quantity_available": float(lot["quantity_available"]),
        "quantity_reserved": float(lot["quantity_reserved"]),
    }


def plate_states(port: int, plates: dict[str, str]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for name, material_uuid in plates.items():
        aggregate = _api_request(port, f"/materials/instances/{material_uuid}")["material"]
        states[name] = {
            "material_uuid": material_uuid,
            "lifecycle_status": aggregate["lifecycle_status"],
            "parent_material_uuid": aggregate.get("parent_material_uuid"),
        }
    return states


def _action_types(port: int) -> dict[str, str]:
    """尽力从注册表权威取动作类型（ROS2 派发选消息类型用）；查不到留空。"""

    wanted = {
        f"{HOST_NODE_ID}/apply_deduct_resource": HOST_NODE_ID,
        f"{BENCH_DEVICE_ID}/fill_well": "material_bench_demo",
        f"{BENCH_DEVICE_ID}/bench_report": "material_bench_demo",
    }
    types: dict[str, str] = {}
    for key, entry_name in wanted.items():
        action = key.split("/", 1)[1]
        try:
            detail = _api_request(port, f"/registry/entries/{urllib.parse.quote(entry_name)}")
            payload = detail.get("active_payload") or {}
            mappings = (payload.get("class") or {}).get("action_value_mappings") or {}
            types[key] = str((mappings.get(action) or {}).get("type") or "")
        except Exception:  # noqa: BLE001 - 注册表权威不可用时按空类型透传
            types[key] = ""
    return types


def upload_flow_workflow(port: int, templates: dict[str, str]) -> dict[str, Any]:
    """「编排画布 → 提交」：POST /workflows 建定义，PUT graph 写三节点图（revision 乐观锁）。"""

    graph = build_flow_graph(
        plate_template_uuid=templates[FLOW_PLATE_TEMPLATE_NAME],
        water_template_uuid=templates[WATER_TEMPLATE_NAME],
        action_types=_action_types(port),
    )
    workflow = _api_request(
        port,
        "/workflows",
        {
            "name": graph["name"],
            "tags": graph["tags"],
            "description": graph["description"],
            "meta_data": {"source": "materials-demo-smoke"},
        },
    )
    saved = _api_request(
        port,
        f"/workflows/{workflow['uuid']}/graph",
        {"revision": workflow["revision"], "nodes": graph["nodes"], "edges": graph["edges"]},
        method="PUT",
    )
    return {
        "workflow_uuid": workflow["uuid"],
        "name": graph["name"],
        "revision": saved["workflow"]["revision"],
        "node_count": len(saved["nodes"]),
        "graph": graph,
    }


def flow_precheck(port: int, templates: dict[str, str]) -> dict[str, Any]:
    """提交前的客户端预检（OpenLab 画布 dry-run 的等价物）：只提示，不阻断。"""

    lots = _api_request(
        port, f"/materials/lots?template_uuid={templates[WATER_TEMPLATE_NAME]}&unit={WATER_UNIT}"
    )
    available = sum(float(lot["quantity_available"]) for lot in lots if not lot.get("quarantined"))
    roots = _api_request(port, "/materials/instances?roots_only=true")
    plates = [
        item
        for item in roots
        if item["material"]["template_uuid"] == templates[FLOW_PLATE_TEMPLATE_NAME]
        and item["material"]["lifecycle_status"] == "active"
    ]
    return {
        "plates_active": len(plates),
        "water_available": available,
        "water_required": FILL_VOLUME_UL,
        "water_short": max(0.0, FILL_VOLUME_UL - available),
    }


def deck_occupancy(port: int) -> dict[str, str]:
    """按名字从权威取台面（工作流也只知道这个名字），返回位点 -> 占用物料名。"""

    matches = _api_request(port, f"/materials/instances?name={urllib.parse.quote(BENCH_DECK_NAME)}")
    if not matches:
        raise RuntimeError(f"权威中没有台面 {BENCH_DECK_NAME!r}")
    deck = matches[0]
    occupancy: dict[str, str] = {}
    for site in sorted(deck["sites"], key=lambda item: int(item["site_index"])):
        occupant = ""
        if site.get("occupied_material_uuid"):
            occupant = _api_request(port, f"/materials/instances/{site['occupied_material_uuid']}")["material"]["name"]
        occupancy[site["label"]] = occupant
    return occupancy


def run_flow_task(port: int, workflow_uuid: str, plates: dict[str, str], deadline: float) -> dict[str, Any]:
    """创建任务 -> 等待终态 -> 汇总节点结果与权威账目（板状态 / lot / reservation / 台面）。"""

    task = _api_request(port, "/workflow-tasks", {"workflow_uuid": workflow_uuid, "run_mode": "normal"})
    task_uuid = task["uuid"]
    status = ""
    final: dict[str, Any] = task
    while time.monotonic() < deadline and status not in {"succeeded", "failed"}:
        final = _api_request(port, f"/workflow-tasks/{task_uuid}")
        status = str(final.get("status") or "")
        if status in {"succeeded", "failed"}:
            break
        held = [item for item in _api_request(port, "/error-decisions")["items"] if item.get("task_id") == task_uuid]
        if held:
            raise AssertionError(f"任务的 attempt 进入了错误决策链: {held[0]}")
        time.sleep(0.3)
    if status not in {"succeeded", "failed"}:
        raise RuntimeError(f"工作流任务 {task_uuid} 未在时限内结束: {status}")
    by_node = {step_node_uuid(index): index for index in range(3)}
    node_runs = sorted(
        (
            {
                "node_index": by_node[str(run["workflow_node_uuid"])],
                "uuid": run["uuid"],
                "status": run["status"],
                "attempt_count": int(run.get("attempt_count") or 0),
                "return_info": dict(run.get("return_info") or {}),
                "error_info": list(run.get("error_info") or []),
            }
            for run in _api_request(port, f"/workflow-tasks/{task_uuid}/node-runs")
        ),
        key=lambda item: item["node_index"],
    )
    reservations = [
        {"reservation_uuid": item["reservation_uuid"], "job_uuid": item["job_uuid"], "status": item["status"]}
        for item in _api_request(port, f"/materials/reservations?task_uuid={task_uuid}")
    ]
    return {
        "task_uuid": task_uuid,
        "task_status": status,
        "task_error_info": list(final.get("error_info") or []),
        "node_runs": node_runs,
        "plates": plate_states(port, plates),
        "lot": lot_view(port),
        "reservations": reservations,
        "deck": deck_occupancy(port),
    }


def run_material_flow_stage(port: int, deadline: float) -> dict[str, Any]:
    """阶段三全流程：按件/按量登记 → 上传 → 预检 → 失败 → 补料 → 成功。"""

    templates = flow_templates(port)
    plates = {name: instantiate_plate(port, name) for name in FLOW_PLATE_NAMES}
    lot_initial = inbound_water(port, templates[WATER_TEMPLATE_NAME], INITIAL_WATER_UL)
    workflow = upload_flow_workflow(port, templates)
    bootstrap = {
        "templates": templates,
        "plates": plate_states(port, plates),
        "lot_initial": lot_initial,
        "workflow": {key: value for key, value in workflow.items() if key != "graph"},
        "deck": deck_occupancy(port),
    }
    assert_flow_bootstrap(bootstrap)

    precheck = flow_precheck(port, templates)
    assert_flow_precheck(precheck)

    failed = run_flow_task(port, workflow["workflow_uuid"], plates, deadline)
    assert_flow_failed_run(failed)

    restock = {"lot": inbound_water(port, templates[WATER_TEMPLATE_NAME], RESTOCK_WATER_UL)}
    assert_flow_restock(restock)

    succeeded = run_flow_task(port, workflow["workflow_uuid"], plates, deadline)
    assert_flow_succeeded_run(succeeded)
    return {
        "bootstrap": bootstrap,
        "workflow_graph": workflow["graph"],
        "precheck": precheck,
        "failed_run": failed,
        "restock": restock,
        "succeeded_run": succeeded,
    }


def _lot_tuple(lot: dict[str, Any]) -> tuple[float, float, float]:
    return (float(lot["quantity_total"]), float(lot["quantity_available"]), float(lot["quantity_reserved"]))


def assert_flow_bootstrap(proof: dict[str, Any]) -> None:
    """按件 / 按量登记 + 上传后的起点：两块 active 根板、lot 500/500/0、三节点图、T1/T2 空。"""

    assert set(proof["templates"]) == {FLOW_PLATE_TEMPLATE_NAME, WATER_TEMPLATE_NAME}, proof["templates"]
    assert sorted(proof["plates"]) == sorted(FLOW_PLATE_NAMES), proof["plates"]
    for name, state in proof["plates"].items():
        assert state["lifecycle_status"] == "active" and state["parent_material_uuid"] is None, (name, state)
    assert _lot_tuple(proof["lot_initial"]) == (INITIAL_WATER_UL, INITIAL_WATER_UL, 0.0), proof["lot_initial"]
    assert proof["workflow"]["node_count"] == 3, proof["workflow"]
    assert proof["deck"] == {"T1": "", "T2": "", "T3": ROUND1_PLATE, "T4": ROUND2_PLATE}, proof["deck"]


def assert_flow_precheck(proof: dict[str, Any]) -> None:
    """客户端预检：板够、水差 700 ul（只提示，不阻断提交）。"""

    assert proof["plates_active"] == len(FLOW_PLATE_NAMES), proof
    assert proof["water_available"] == INITIAL_WATER_UL, proof
    assert proof["water_required"] == FILL_VOLUME_UL, proof
    assert proof["water_short"] == EXPECTED_SHORT_UL, proof


def assert_flow_failed_run(proof: dict[str, Any]) -> None:
    """权威预检：整张任务在派发前失败，板与水都没有任何预留痕迹，设备没被调用。"""

    assert proof["task_status"] == "failed", proof
    (error,) = proof["task_error_info"]
    assert error["code"] == "plan_not_executable", error
    assert f"short by {EXPECTED_SHORT_UL:g}" in error["message"], error
    runs = proof["node_runs"]
    assert [run["node_index"] for run in runs] == [0, 1, 2], runs
    for run in runs:
        assert run["status"] == "canceled", run
        assert run["return_info"] == {}, run
        assert run["attempt_count"] == 1, run
    # 水不够导致板也没被预留：两块板都还是 active 根物料，台面 T1 仍空
    for name, state in proof["plates"].items():
        assert state["lifecycle_status"] == "active" and state["parent_material_uuid"] is None, (name, state)
    assert _lot_tuple(proof["lot"]) == (INITIAL_WATER_UL, INITIAL_WATER_UL, 0.0), proof["lot"]
    assert proof["reservations"] == [], proof["reservations"]
    assert proof["deck"][FLOW_SLOT] == "", proof["deck"]


def assert_flow_restock(proof: dict[str, Any]) -> None:
    assert _lot_tuple(proof["lot"]) == (WATER_AFTER_RESTOCK_UL, WATER_AFTER_RESTOCK_UL, 0.0), proof["lot"]


def assert_flow_succeeded_run(proof: dict[str, Any]) -> None:
    """补料后再提交：出库挂载 + 加液 + 报告全部成功，权威账目与设备回报一致。"""

    assert proof["task_status"] == "succeeded", proof
    runs = {run["node_index"]: run for run in proof["node_runs"]}
    assert set(runs) == {0, 1, 2} and all(run["status"] == "succeeded" for run in runs.values()), runs

    deduct = runs[0]["return_info"]["return_value"]
    assert deduct["created_resource_tree"], deduct
    assert deduct["mount_resource"], deduct
    assert deduct["substance_resource_tree"] == [], deduct

    fill = runs[1]["return_info"]["return_value"]
    plate_name = fill["plate_name"]
    assert plate_name in FLOW_PLATE_NAMES, fill
    assert (fill["slot"], fill["well"]) == (FLOW_SLOT, FLOW_WELL), fill
    assert (fill["volume"], fill["unit"]) == (FILL_VOLUME_UL, WATER_UNIT), fill
    # lot 与体积不是工作流参数：来自调度器按需求 key=water 注入的权威分配
    assert fill["lots"] == [{"lot_uuid": WATER_LOT_UUID, "quantity": FILL_VOLUME_UL}], fill
    assert fill["substances"] == [EXPECTED_WELL_CONTENT], fill
    assert fill["fills"] == 1, fill

    report = runs[2]["return_info"]["return_value"]
    assert report["sites"] == {"T1": plate_name, "T2": "", "T3": ROUND1_PLATE, "T4": ROUND2_PLATE}, report
    assert report["wells"][plate_name] == {FLOW_WELL: [EXPECTED_WELL_CONTENT]}, report
    assert report["fills"] == 1, report

    # 权威直查：被选中的板 in_use 且挂在台面下，另一块仍 active；lot 扣减后 reserved 归零
    plates = proof["plates"]
    selected = plates[plate_name]
    assert selected["lifecycle_status"] == "in_use" and selected["parent_material_uuid"] == DECK_UUID, selected
    for name, state in plates.items():
        if name != plate_name:
            assert state["lifecycle_status"] == "active" and state["parent_material_uuid"] is None, (name, state)
    assert _lot_tuple(proof["lot"]) == (WATER_AFTER_FILL_UL, WATER_AFTER_FILL_UL, 0.0), proof["lot"]
    reservations = proof["reservations"]
    assert len(reservations) == 2 and all(item["status"] == "consumed" for item in reservations), reservations
    assert proof["deck"] == report["sites"], proof["deck"]


def run_smoke(
    backend: str = "hostlink",
    timeout: float = 45.0,
) -> dict[str, Any]:
    """启动 host/slave 真实双进程，等 slave 接入后按顺序经管理 API 运行四个工作流与阶段三。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=f"materials-demo-{backend}-", ignore_cleanup_errors=True
    ) as directory:
        root = Path(directory)
        host_log_path = root / "host.log"
        slave_log_path = root / "slave.log"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        hostlink_port = _free_port()
        host_management_port = _free_port()
        host_command = _base_command(
            repo_root,
            _graph_path(repo_root, "host.json"),
            root / "host-db",
            host_management_port,
            backend,
        )
        slave_command = _base_command(
            repo_root,
            _graph_path(repo_root, "slave.json"),
            root / "slave-db",
            _free_port(),
            backend,
        ) + ["--is_slave"]
        # 两种 backend 都保留 HostLink：hostlink 模式它承载全部通信；
        # ros2 模式它只承载物料链路（slave 经 HostLink 访问 host 物料权威），
        # 设备动作、Topic 仍走 ROS2。
        host_command += [
            "--hostlink_bind",
            "127.0.0.1",
            "--hostlink_port",
            str(hostlink_port),
        ]
        slave_command += [
            "--host_node_ip",
            "127.0.0.1",
            "--hostlink_port",
            str(hostlink_port),
        ]
        if backend == "ros2":
            domain_id = str(10 + hostlink_port % 190)
            environment["ROS_DOMAIN_ID"] = domain_id
            host_command += ["--ros_domain_id", domain_id]
            slave_command += ["--ros_domain_id", domain_id]

        with host_log_path.open("w", encoding="utf-8") as host_log, \
                slave_log_path.open("w", encoding="utf-8") as slave_log:
            host = subprocess.Popen(
                host_command,
                cwd=repo_root,
                env=environment,
                stdout=host_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            slave: subprocess.Popen[Any] | None = None

            def dump_logs() -> str:
                return (
                    f"HOST:\n{host_log_path.read_text(encoding='utf-8', errors='replace')}\n"
                    f"SLAVE:\n{slave_log_path.read_text(encoding='utf-8', errors='replace')}"
                )

            try:
                # HostLink 端口就绪即代表 host 网络栈可接受 slave 接入。
                startup_deadline = time.monotonic() + min(20.0, timeout / 2)
                while time.monotonic() < startup_deadline:
                    if host.poll() is not None:
                        break
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", hostlink_port), timeout=0.2
                        ):
                            break
                    except OSError:
                        time.sleep(0.1)
                if host.poll() is not None:
                    host_log.flush()
                    raise RuntimeError(
                        "host process exited before slave startup\n" + dump_logs()
                    )
                slave = subprocess.Popen(
                    slave_command,
                    cwd=repo_root,
                    env=environment,
                    stdout=slave_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                proofs: dict[str, dict[str, Any]] = {}
                deadline = time.monotonic() + timeout
                try:
                    _wait_runtime_ready(host_management_port, host, slave, deadline)
                except Exception:
                    sys.stderr.write("STARTUP FAILED\n" + dump_logs() + "\n")
                    raise

                # 阶段一：第一轮闭环——设备不自跑，全部由工作流经管理 API 触发
                try:
                    proofs["site_loop_workflow"] = run_workflow_stage(
                        host_management_port, SITE_LOOP_WORKFLOW_NAME, timeout
                    )
                    assert_site_loop_workflow(proofs["site_loop_workflow"], backend)
                    proofs["material_loop_workflow"] = run_workflow_stage(
                        host_management_port, MATERIAL_LOOP_WORKFLOW_NAME, timeout
                    )
                    assert_material_loop_workflow(proofs["material_loop_workflow"])
                except Exception:
                    sys.stderr.write("LOOP STAGE FAILED\n" + dump_logs() + "\n")
                    raise

                # 阶段二：第二轮
                try:
                    proofs["site_tour_workflow"] = run_workflow_stage(
                        host_management_port, SITE_TOUR_WORKFLOW_NAME, timeout
                    )
                    assert_site_tour_workflow(proofs["site_tour_workflow"])
                    proofs["material_flow_workflow"] = run_workflow_stage(
                        host_management_port,
                        MATERIAL_FLOW_WORKFLOW_NAME,
                        timeout,
                    )
                    assert_material_flow_workflow(
                        proofs["material_flow_workflow"]
                    )
                    proofs["authority_final_state"] = (
                        read_authority_final_state(host_management_port)
                    )
                    assert_authority_final_state(
                        proofs["authority_final_state"]
                    )
                except Exception:
                    sys.stderr.write(
                        "WORKFLOW STAGE FAILED\n" + dump_logs() + "\n"
                    )
                    raise

                # 阶段三：设备已在，复现网页的「按件/按量登记 → 上传工作流 →
                # 预检失败 → 补料 → 出库挂载成功」，全部经管理 HTTP API
                try:
                    proofs["material_flow_stage"] = run_material_flow_stage(
                        host_management_port, time.monotonic() + timeout
                    )
                except Exception:
                    sys.stderr.write(
                        "MATERIAL FLOW STAGE FAILED\n" + dump_logs() + "\n"
                    )
                    raise
                return proofs
            finally:
                if slave is not None:
                    _stop(slave)
                _stop(host)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("hostlink", "ros2"),
        default="hostlink",
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            run_smoke(args.backend, args.timeout),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
