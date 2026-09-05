"""HostLink 集成冒烟：真实启动 host/slave 双进程，全部经管理 API 运行工作流并断言。"""

from materials_demo.smoke import (
    assert_authority_final_state,
    assert_flow_bootstrap,
    assert_flow_failed_run,
    assert_flow_precheck,
    assert_flow_restock,
    assert_flow_succeeded_run,
    assert_material_flow_workflow,
    assert_material_loop_workflow,
    assert_site_loop_workflow,
    assert_site_tour_workflow,
    run_smoke,
)


def test_hostlink_smoke_proof() -> None:
    proofs = run_smoke(backend="hostlink", timeout=120.0)
    # 阶段一：第一轮闭环（设备不自跑，由 @workflow 经 API 触发）
    assert_site_loop_workflow(proofs["site_loop_workflow"], "hostlink")
    assert_material_loop_workflow(proofs["material_loop_workflow"])
    # 阶段二：第二轮 + 权威终态
    assert_site_tour_workflow(proofs["site_tour_workflow"])
    assert_material_flow_workflow(proofs["material_flow_workflow"])
    assert_authority_final_state(proofs["authority_final_state"])
    # 阶段三：按件/按量登记 → API 上传工作流 → 预检失败（板与水都无预留痕迹）→ 补料 → 出库挂载成功
    stage = proofs["material_flow_stage"]
    assert_flow_bootstrap(stage["bootstrap"])
    assert_flow_precheck(stage["precheck"])
    assert_flow_failed_run(stage["failed_run"])
    assert_flow_restock(stage["restock"])
    assert_flow_succeeded_run(stage["succeeded_run"])
