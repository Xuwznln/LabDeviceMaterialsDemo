"""有限时启动真实图，验证固定位点实例化与占用流转，再运行默认子工作流。

阶段一（闭环）：sample_rack 等待开机图物料对齐完成后，断言权威位点与
@device 声明一致，随后执行「装载 A1 -> 转移到 B2」并把每一步的位点快照
写出 proof.json。

阶段二（工作流）：host 启动时已把 site_demo/workflows.py 里的 @workflow
幂等上报到本机 Workflow Authority；本脚本通过管理 HTTP API 找到它、创建
任务（装载 A2 -> 转移到 B1 -> 查看位点），断言任务成功且终态快照同时保留
两个阶段的占用结果（B1=wf-sample、B2=proof-sample）。
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
WORKFLOW_DISPLAY_NAME = "位点操作演示"

#: 与 sample_rack.RACK_SITES 声明一致的期望快照（label -> (x, y)）。
EXPECTED_SITE_POSITIONS = {
    "A1": (60.0, 40.0),
    "A2": (180.0, 40.0),
    "B1": (60.0, 160.0),
    "B2": (180.0, 160.0),
}


def _occupancy(sites: list[dict[str, Any]]) -> dict[str, str]:
    return {entry["label"]: entry["occupied_by"] for entry in sites}


def assert_smoke_proof(proof: dict[str, Any], backend: str) -> None:
    """对 HostLink/ROS2 共用的位点闭环做同一组断言。"""

    assert proof.get("success") is True, f"smoke 未成功: {proof}"
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
    assert all(
        entry["allowed_resource_categories"] == ["demo_sample"] for entry in initial
    )

    # 3) 装载：样品物料创建并占用 A1
    assert proof["load"]["success"] is True
    assert proof["load"]["site_label"] == "A1"
    after_load = _occupancy(proof["after_load"])
    assert after_load["A1"] == "proof-sample", after_load

    # 4) 转移：A1 释放、B2 占用，占用状态由权威维护
    moved = proof["moved"]
    assert moved["success"] is True
    assert (moved["from_label"], moved["to_label"]) == ("A1", "B2")
    after_transfer = _occupancy(proof["after_transfer"])
    assert after_transfer["A1"] == "", after_transfer
    assert after_transfer["B2"] == "proof-sample", after_transfer


def assert_workflow_proof(workflow_proof: dict[str, Any]) -> None:
    """断言默认子工作流：三步串行成功，终态快照保留两阶段占用。"""

    assert workflow_proof["workflow_name"] == WORKFLOW_DISPLAY_NAME
    assert workflow_proof["task_status"] == "succeeded", (
        f"工作流任务未成功: {workflow_proof}"
    )
    jobs = workflow_proof["jobs"]
    assert len(jobs) == 3, f"应有 3 个节点 job: {jobs}"
    assert all(job["status"] == "succeeded" for job in jobs), f"存在失败 job: {jobs}"

    # jobs 按 topological_index 返回；节点 uuid 序 == 声明序（load -> transfer -> inspect）
    load_result, transfer_result, inspect_result = [
        job["return_info"]["return_value"] for job in jobs
    ]
    # run_template 单实例自动填充 -> 真实装载到 A2
    assert load_result["success"] is True
    assert load_result["site_label"] == "A2"
    assert load_result["sample_name"] == "wf-sample"

    assert transfer_result["success"] is True
    assert (transfer_result["from_label"], transfer_result["to_label"]) == ("A2", "B1")
    assert transfer_result["sample_name"] == "wf-sample"

    # 终态快照：工作流的 B1 与闭环阶段的 B2 同时可见，A 行已清空
    snapshot = _occupancy(inspect_result["sites"])
    assert snapshot == {
        "A1": "",
        "A2": "",
        "B1": "wf-sample",
        "B2": "proof-sample",
    }, snapshot


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


def _graph_path(repo_root: Path) -> Path:
    """优先读取 wheel 安装的数据文件，editable/source 模式回退到仓库 graph。"""

    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "site_demo"
        / "graph"
        / "site_demo.json"
    )
    if installed.is_file():
        return installed
    source = repo_root / "graph" / "site_demo.json"
    if source.is_file():
        return source
    raise FileNotFoundError("Site demo graph 未随 distribution 安装")


def _base_command(
    repo_root: Path,
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
    command = [
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
        str(_graph_path(repo_root)),
    ]
    if backend == "ros2":
        command.append("--disable_hostlink")
    return command


# ---------------------------------------------------------------------------
# 管理 HTTP API（工作流阶段）
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


def run_workflow_stage(management_port: int, timeout: float) -> dict[str, Any]:
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
            item
            for item in listing["items"]
            if item["name"] == WORKFLOW_DISPLAY_NAME
        ]
        if matches:
            workflow_uuid = matches[0]["uuid"]
            break
        time.sleep(0.3)
    if not workflow_uuid:
        raise RuntimeError(
            f"{timeout}s 内未在管理 API 检索到工作流 {WORKFLOW_DISPLAY_NAME!r}"
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
        "workflow_name": WORKFLOW_DISPLAY_NAME,
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


def run_smoke(
    backend: str = "hostlink",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """启动真实图，等待位点闭环 proof，再运行默认子工作流。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=f"site-demo-{backend}-"
    ) as directory:
        root = Path(directory)
        proof_path = root / "proof.json"
        log_path = root / "runtime.log"
        environment = os.environ.copy()
        environment.update(
            {
                "SITE_DEMO_PROOF_FILE": str(proof_path),
                "SITE_DEMO_START_DELAY": (
                    "2.0" if backend == "ros2" else "0.2"
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        hostlink_port = _free_port()
        management_port = _free_port()
        command = _base_command(
            repo_root,
            root / "db",
            management_port,
            backend,
        )
        if backend == "hostlink":
            command += [
                "--hostlink_bind",
                "127.0.0.1",
                "--hostlink_port",
                str(hostlink_port),
            ]
        else:
            domain_id = str(10 + hostlink_port % 190)
            environment["ROS_DOMAIN_ID"] = domain_id
            command += ["--ros_domain_id", domain_id]

        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command,
                cwd=repo_root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                proof: dict[str, Any] | None = None
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if proof_path.is_file():
                        proof = json.loads(
                            proof_path.read_text(encoding="utf-8")
                        )
                        if proof.get("success") is not True:
                            raise RuntimeError(
                                f"{backend} smoke failed: {proof}\n"
                                + log_path.read_text(
                                    encoding="utf-8", errors="replace"
                                )
                            )
                        assert_smoke_proof(proof, backend)
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                if proof is None:
                    raise RuntimeError(
                        f"{backend} smoke did not complete within {timeout}s\n"
                        + log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    )

                # 阶段二：上报结果已在启动时完成，这里检索并真实运行工作流
                try:
                    proof["workflow"] = run_workflow_stage(
                        management_port, timeout
                    )
                    assert_workflow_proof(proof["workflow"])
                except Exception:
                    sys.stderr.write(
                        "WORKFLOW STAGE FAILED\n"
                        + log_path.read_text(encoding="utf-8", errors="replace")
                        + "\n"
                    )
                    raise
                return proof
            finally:
                _stop(process)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("hostlink", "ros2"),
        default="hostlink",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
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
