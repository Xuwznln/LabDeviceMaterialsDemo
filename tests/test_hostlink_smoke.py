"""HostLink 集成冒烟：真实启动位点演示图并断言闭环与工作流终态。"""

from site_demo.smoke import assert_smoke_proof, assert_workflow_proof, run_smoke


def test_hostlink_smoke_proof() -> None:
    proof = run_smoke(backend="hostlink", timeout=40.0)
    assert_smoke_proof(proof, "hostlink")
    assert_workflow_proof(proof["workflow"])
