"""HostLink 集成冒烟：真实启动 host/slave 双进程并断言双闭环与工作流终态。"""

from site_demo.smoke import (
    assert_authority_final_state,
    assert_bench_proof,
    assert_material_flow_workflow,
    assert_rack_proof,
    assert_site_tour_workflow,
    run_smoke,
)


def test_hostlink_smoke_proof() -> None:
    proofs = run_smoke(backend="hostlink", timeout=60.0)
    assert_rack_proof(proofs["rack"], "hostlink")
    assert_bench_proof(proofs["bench"], "hostlink")
    assert_site_tour_workflow(proofs["site_tour_workflow"])
    assert_material_flow_workflow(proofs["material_flow_workflow"])
    assert_authority_final_state(proofs["authority_final_state"])
