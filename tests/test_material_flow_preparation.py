"""流转模板不依赖前一次演示的台面；缺少前置时不创建孤立耗材。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from materials_demo.material_bench import MaterialBenchDemo
from materials_demo.workflows import material_flow
from unilabos.resources import materials


def test_material_flow_keeps_original_five_steps():
    context = Mock()
    material_flow(context)
    actions = [call.args[0] for call in context.run.call_args_list]
    assert actions == [
        "material_bench/provision_labware",
        "material_bench/hydrate_well", "material_bench/relocate_plate",
        "material_bench/dispose_tips", "material_bench/bench_report",
    ]


def test_missing_deck_does_not_create_labware(monkeypatch):
    monkeypatch.setenv("MATERIALS_DEMO_SKIP_AUTO_PREPARE", "1")
    driver = MaterialBenchDemo()
    driver.post_init(SimpleNamespace(resource_tracker=SimpleNamespace(uuid_to_resources={})))
    create = Mock()
    monkeypatch.setattr(materials, "create", create)
    with pytest.raises(ValueError, match="prepare_bench"):
        driver.provision_labware()
    create.assert_not_called()
    assert driver._round == 0


def test_default_startup_prepares_bench(monkeypatch):
    monkeypatch.delenv("MATERIALS_DEMO_SKIP_AUTO_PREPARE", raising=False)
    driver = MaterialBenchDemo()
    prepare = Mock()
    monkeypatch.setattr(driver, "prepare_bench", prepare)
    node = object()
    driver.post_init(node)
    assert driver._device_node is node
    prepare.assert_called_once_with()


@pytest.mark.parametrize("value", ["1", "0", ""])
def test_explicit_environment_skips_preparation(monkeypatch, value):
    monkeypatch.setenv("MATERIALS_DEMO_SKIP_AUTO_PREPARE", value)
    driver = MaterialBenchDemo()
    prepare = Mock()
    monkeypatch.setattr(driver, "prepare_bench", prepare)
    driver.post_init(object())
    prepare.assert_not_called()
