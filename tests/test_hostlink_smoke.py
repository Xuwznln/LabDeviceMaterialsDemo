"""HostLink 集成冒烟：真实启动 host/slave 双进程并断言双闭环、工作流终态与出库装板阶段。"""

from materials_demo.smoke import (
    assert_authority_final_state,
    assert_bench_proof,
    assert_flow_bootstrap,
    assert_flow_failed_run,
    assert_flow_precheck,
    assert_flow_restock,
    assert_flow_succeeded_run,
    assert_material_flow_workflow,
    assert_rack_proof,
    assert_site_tour_workflow,
    run_smoke,
)


def test_hostlink_smoke_proof() -> None:
    proofs = run_smoke(backend="hostlink", timeout=120.0)
    assert_rack_proof(proofs["rack"], "hostlink")
    assert_bench_proof(proofs["bench"], "hostlink")
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
