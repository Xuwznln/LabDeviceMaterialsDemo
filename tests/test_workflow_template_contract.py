"""模板必须显式实例化，不能把模板 UUID 当工作流 UUID 或轮询旧实例列表。"""

import time

import pytest

from materials_demo import smoke


def test_smoke_instantiates_reported_template_before_submission(monkeypatch):
    name = smoke.SITE_LOOP_WORKFLOW_NAME
    calls = []

    def api(port, path, payload=None):
        assert port == 8002
        calls.append((path, payload))
        if path == "/registry/workflow-templates":
            return {"templates": [{"uuid": "template", "display_name": name}]}
        if path == "/workflows/from-template":
            assert payload == {"template_uuid": "template", "bindings": {}}
            return {"workflow": {"uuid": "workflow", "name": name}}
        if path == "/workflow-tasks":
            assert payload["workflow_uuid"] == "workflow"
            return {"uuid": "task", "status": "succeeded"}
        if path == "/workflow-tasks/task":
            return {"uuid": "task", "status": "succeeded", "workflow_snapshot": {"nodes": []}}
        if path in {"/workflow-tasks/task/jobs", "/workflow-tasks/task/node-runs"}:
            return []
        raise AssertionError(f"意外调用旧接口或未知路由: {path}")

    monkeypatch.setattr(smoke, "_api_request", api)
    smoke.run_workflow_stage(8002, name, 2)
    assert calls[:2] == [
        ("/registry/workflow-templates", None),
        ("/workflows/from-template", {"template_uuid": "template", "bindings": {}}),
    ]

def test_smoke_rejects_ambiguous_template_name(monkeypatch):
    name = smoke.SITE_LOOP_WORKFLOW_NAME

    def api(port, path, payload=None):
        assert path == "/registry/workflow-templates"
        return {"templates": [
            {"uuid": "one", "display_name": name},
            {"uuid": "two", "display_name": name},
        ]}

    monkeypatch.setattr(smoke, "_api_request", api)
    with pytest.raises(AssertionError, match="重复"):
        smoke.run_workflow_stage(8002, name, 2)

