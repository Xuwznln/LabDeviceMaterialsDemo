"""有限时启动真实 host/slave 双进程，验证位点与物料两条闭环，再运行默认子工作流。

阶段一（双闭环，并行推进）：

- host 进程 ``sample_rack``：available_sites 位点闭环（装载 A1 -> 转移 B2），
  写出 SITE_DEMO_PROOF_FILE；
- slave 进程 ``material_bench``：物料 CRUD 闭环（ensure deck -> 创建 tip
  rack/孔板 -> set_substance 上报 -> transfer 换位 -> remove 删除），全部经
  HostLink 访问 host 上的微后端物料权威，写出 SITE_DEMO_BENCH_PROOF_FILE。
  ros2 backend 同样走该链路（ROS2 承载设备通信，HostLink 承载物料）。

阶段二（工作流）：host 启动时已把 site_demo/workflows.py 里的两个 @workflow
幂等上报到本机 Workflow Authority；本脚本经管理 HTTP API 逐个创建任务：

- 「位点操作演示」三步指向 host 侧 sample_rack；
- 「物料流转演示」五步跨进程分发到 slave 侧 material_bench（第二轮耗材）。

终局：经管理 API 直读物料权威中 deck 树，断言 T3/T4 分别留下两轮孔板、
枪头盒全部删除、孔位内容物与两阶段写入一致。
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
import urllib.request
from typing import Any, Sequence

#: 与 site_demo/workflows.py 保持一致（smoke 独立运行，不 import 设备包）。
SITE_TOUR_WORKFLOW_NAME = "位点操作演示"
MATERIAL_FLOW_WORKFLOW_NAME = "物料流转演示"

#: 与 graph/slave.json config.deck_uuid 保持一致（权威终态直查用）。
DECK_UUID = "9d2c7e40-5b1a-4f76-a3c8-000000003001"

#: 与 sample_rack.RACK_SITES 声明一致的期望快照（label -> (x, y)）。
EXPECTED_SITE_POSITIONS = {
    "A1": (60.0, 40.0),
    "A2": (180.0, 40.0),
    "B1": (60.0, 160.0),
    "B2": (180.0, 160.0),
}

#: 物料闭环里两轮耗材的命名（第 1 轮 = bench proof，第 2 轮 = 工作流）。
ROUND1_TIPS, ROUND1_PLATE = "bench_tips_r1", "bench_plate_r1"
ROUND2_TIPS, ROUND2_PLATE = "bench_tips_r2", "bench_plate_r2"

WATER_40 = ["Water", 40.0, "ul"]
BUFFER_25 = ["Buffer", 25.0, "ul"]
DYE_15 = ["Dye", 15.0, "ul"]


def _occupancy(sites: list[dict[str, Any]]) -> dict[str, str]:
    return {entry["label"]: entry["occupied_by"] for entry in sites}


def assert_rack_proof(proof: dict[str, Any], backend: str) -> None:
    """host 侧 sample_rack 位点闭环断言（HostLink/ROS2 共用）。"""

    assert proof.get("success") is True, f"rack 闭环未成功: {proof}"
    assert proof.get("backend") == backend, f"backend 不匹配: {proof}"

    # 1) 权威位点与 @device(available_sites=...) 声明逐项一致
    definition_check = proof["definition_check"]
    assert definition_check["matches"] is True, f"位点定义漂移: {definition_check}"

    # 2) 初始 4 个位点按声明的网格坐标实例化且全部空闲
    initial = proof["initial_sites"]
    assert [entry["label"] for entry in initial] == ["A1", "A2", "B1", "B2"]
    assert {
        entry["label"]: (entry["x"], entry["y"]) for entry in initial
    } == EXPECTED_SITE_POSITIONS
    assert all(entry["occupied_by"] == "" for entry in initial), initial

    # 3) 装载 A1 -> 转移 B2，占用状态由权威维护
    assert proof["load"]["success"] is True
    assert _occupancy(proof["after_load"])["A1"] == "proof-sample"
    moved = proof["moved"]
    assert (moved["from_label"], moved["to_label"]) == ("A1", "B2")
    after_transfer = _occupancy(proof["after_transfer"])
    assert after_transfer["A1"] == "", after_transfer
    assert after_transfer["B2"] == "proof-sample", after_transfer


def assert_bench_proof(proof: dict[str, Any], backend: str) -> None:
    """slave 侧 material_bench 物料 CRUD 闭环断言（第 1 轮耗材）。"""

    assert proof.get("success") is True, f"bench 闭环未成功: {proof}"
    assert proof.get("backend") == backend, f"backend 不匹配: {proof}"

    # 1) 台面 ensure：固定 uuid 幂等创建，四个位点 T1-T4
    prepared = proof["prepared"]
    assert prepared["deck_uuid"] == DECK_UUID
    assert prepared["site_labels"] == ["T1", "T2", "T3", "T4"]

    # 2) 补给：类名创建的 tip rack 上 T1，草稿创建的孔板上 T2（A1 预置 Water）
    provisioned = proof["provisioned"]
    assert provisioned["round"] == 1
    assert provisioned["plate_a1_substances"] == [WATER_40], provisioned
    after_provision = proof["after_provision"]
    assert after_provision == {
        "T1": ROUND1_TIPS,
        "T2": ROUND1_PLATE,
        "T3": "",
        "T4": "",
    }, after_provision

    # 3) 孔位加液：本地 add_liquid + commit 由快照观察者自动上报权威
    hydrated = proof["hydrated"]
    assert hydrated["well"] == "A2"
    assert hydrated["substances"] == [BUFFER_25], hydrated

    # 4) 换位：权威先落位，unload -> load 投影重建了本地实例
    relocated = proof["relocated"]
    assert relocated["to_site"] == "T3"
    assert relocated["instance_rebuilt"] is True
    assert relocated["sites"]["T2"] == "" and relocated["sites"]["T3"] == ROUND1_PLATE

    # 5) 删除：权威递归删除整棵树（根 + 24 个枪头位）并自动释放位点
    disposed = proof["disposed"]
    assert disposed["deleted_in_authority"] is True
    assert disposed["tips_uuid"] == provisioned["tips_uuid"]
    assert disposed["root_removed"] is True
    assert disposed["removed_count"] == 25, disposed
    assert disposed["sites"]["T1"] == "", disposed

    # 6) 终态报告：只剩 T3 上的第一轮孔板，孔位内容物齐全
    report = proof["report"]
    assert report["sites"] == {"T1": "", "T2": "", "T3": ROUND1_PLATE, "T4": ""}
    assert report["wells"] == {
        ROUND1_PLATE: {"A1": [WATER_40], "A2": [BUFFER_25]}
    }, report


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
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _graph_path(repo_root: Path, filename: str) -> Path:
    """优先读取 wheel 安装的数据文件，editable/source 模式回退到仓库 graph。"""

    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "site_demo"
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
        str(repo_root / "site_demo"),
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


def _api_request(
    port: int, path: str, payload: dict[str, Any] | None = None
) -> Any:
    """请求管理 API；workflow 风格 {"code":0,"data":...} 自动解包。"""

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    # GET 不能带 JSON Content-Type：服务端 Backend 路由会尝试解码空 body 而报错
    headers = {} if payload is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    if isinstance(body, dict) and "code" in body:
        if body["code"] != 0:
            raise RuntimeError(f"管理 API {path} 返回错误: {body}")
        return body.get("data")
    return body


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
        except (urllib.error.URLError, OSError):
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


def run_smoke(
    backend: str = "hostlink",
    timeout: float = 45.0,
) -> dict[str, Any]:
    """启动 host/slave 真实双进程，等待两条闭环 proof，再运行两个工作流。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=f"site-demo-{backend}-"
    ) as directory:
        root = Path(directory)
        rack_proof_path = root / "rack-proof.json"
        bench_proof_path = root / "bench-proof.json"
        host_log_path = root / "host.log"
        slave_log_path = root / "slave.log"
        environment = os.environ.copy()
        environment.update(
            {
                "SITE_DEMO_PROOF_FILE": str(rack_proof_path),
                "SITE_DEMO_BENCH_PROOF_FILE": str(bench_proof_path),
                "SITE_DEMO_START_DELAY": (
                    "2.0" if backend == "ros2" else "0.5"
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
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
                while time.monotonic() < deadline:
                    if "rack" not in proofs and rack_proof_path.is_file():
                        proofs["rack"] = json.loads(
                            rack_proof_path.read_text(encoding="utf-8")
                        )
                    if "bench" not in proofs and bench_proof_path.is_file():
                        proofs["bench"] = json.loads(
                            bench_proof_path.read_text(encoding="utf-8")
                        )
                    if len(proofs) == 2:
                        break
                    if host.poll() is not None or (
                        slave is not None and slave.poll() is not None
                    ):
                        break
                    time.sleep(0.1)
                if len(proofs) != 2:
                    missing = {"rack", "bench"} - set(proofs)
                    raise RuntimeError(
                        f"{backend} smoke 未在 {timeout}s 内产出闭环 proof "
                        f"(缺 {sorted(missing)})\n" + dump_logs()
                    )
                for name, proof in proofs.items():
                    if proof.get("success") is not True:
                        raise RuntimeError(
                            f"{name} 闭环失败: {proof}\n" + dump_logs()
                        )
                assert_rack_proof(proofs["rack"], backend)
                assert_bench_proof(proofs["bench"], backend)

                # 阶段二：上报结果已在 host 启动时完成，这里逐个真实运行
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
