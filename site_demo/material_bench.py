"""物料工作台（Slave 侧）— 演示物料的创建、挂载、内容物上报、换位与删除。

设备驻留在 slave 进程，所有物料操作经 ``materials.*`` 门面走 HostLink
访问 Host 上的微后端权威（hostlink backend 与 ros2 backend 共用同一链路）：

- ``prepare_bench``      ensure 台面 Deck（固定 uuid，幂等）并挂到设备自身；
- ``provision_labware``  每轮补给一套耗材：按注册表类名创建枪头盒
  （materials.create("demo_tips_24", ...)）、按草稿实例创建 12 孔板
  （A1 预置 Water），随后 assign 上台面位点（权威落父子 + Site 占用）；
- ``hydrate_well``       对孔位 add_liquid + commit，由快照观察者自动上报，
  动作内轮询权威直至内容物可见（set_substance 实时上行的可观测证明）；
- ``relocate_plate``     materials.transfer 换位（权威先落位，再 unload ->
  load 投影回设备，本地实例被权威重建替换）；
- ``dispose_tips``       materials.remove 删除权威物料树（位点自动释放），
  随后本地卸载实例；
- ``bench_report``       从权威读取台面终态：位点占用 + 各板孔位内容物。

启动后台线程执行「prepare -> provision -> hydrate -> relocate -> dispose ->
report」完整闭环，逐步断言并写出 SITE_DEMO_BENCH_PROOF_FILE 终态 JSON。
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.registry.placeholder_type import SiteSlot

from .labware import BENCH_SITE_LAYOUT, build_bench_deck, demo_plate_12

#: 台面 Deck 的固定 uuid：图 config 与 smoke 断言共享同一出处。
DEFAULT_DECK_UUID = "9d2c7e40-5b1a-4f76-a3c8-000000003001"


@device(
    id="material_bench_demo",
    display_name="物料工作台",
    category=["virtual_device", "workbench"],
    description="物料 CRUD 演示：deck/tip rack/well 创建、挂载、set_substance 上报、换位与删除",
    supported_backends=["hostlink", "ros2"],
)
class MaterialBenchDemo:
    """物料状态完全以微后端权威为准的工作台。"""

    run_in_test_mode = True

    def __init__(
        self,
        device_id: Optional[str] = None,
        deck_name: str = "bench_deck",
        deck_uuid: str = DEFAULT_DECK_UUID,
        **kwargs: Any,
    ) -> None:
        """初始化物料工作台。

        Args:
            device_id[设备ID]: 设备实例 ID，默认 material_bench_demo。
            deck_name[台面名称]: 台面 Deck 的物料名。
            deck_uuid[台面UUID]: 台面 Deck 的固定权威 uuid（ensure 幂等）。
        """
        self.device_id = device_id or "material_bench_demo"
        self.deck_name = deck_name
        self.deck_uuid = deck_uuid
        self.logger = logging.getLogger(f"MaterialBench.{self.device_id}")
        self._start_time = time.time()
        self._phase: str = "idle"
        self._round = 0
        self._tips_uuid: str = ""
        self._plate_uuid: str = ""

    @not_action
    def post_init(self, node: Any) -> None:
        self._device_node = node
        proof_file = os.environ.get("SITE_DEMO_BENCH_PROOF_FILE", "").strip()
        if proof_file:
            threading.Thread(
                target=self._run_proof,
                args=(Path(proof_file),),
                name="bench-demo-proof",
                daemon=True,
            ).start()

    @property
    @topic_config(period=1.0)
    def heartbeat(self) -> int:
        """自启动以来的心跳秒数。"""
        return int(time.time() - self._start_time)

    @property
    @topic_config()
    def phase(self) -> str:
        """当前演示阶段：idle / running / done / failed。"""
        return self._phase

    # ── 权威访问 ────────────────────────────────────────────

    @staticmethod
    @not_action
    def _gateway() -> Any:
        from unilabos.resources.materials import resolve_materials_gateway

        return resolve_materials_gateway()

    @not_action
    def _deck_sites(self) -> Dict[str, str]:
        """权威台面位点占用（label -> 占用物料名，空位为 ""）。"""

        gateway = self._gateway()
        deck = gateway.get_material(self.deck_uuid)
        occupancy: Dict[str, str] = {}
        for site in sorted(deck.sites, key=lambda item: int(item.site_index)):
            name = ""
            if site.occupied_material_uuid:
                name = gateway.get_material(
                    site.occupied_material_uuid
                ).material.name
            occupancy[site.label] = name
        return occupancy

    @not_action
    def _authority_substances(
        self, root_uuid: str, material_uuid: str
    ) -> Optional[List[List[Any]]]:
        """权威树中某节点当前落库的内容物三元组（节点缺失返回 None）。"""

        tree = self._gateway().get_tree(root_uuid)
        node = next(
            (
                item
                for item in tree.nodes
                if item.material.material_uuid == material_uuid
            ),
            None,
        )
        if node is None:
            return None
        return [
            [entry.name, float(entry.quantity), entry.quantity_unit]
            for entry in node.data.substances
        ]

    @staticmethod
    @not_action
    def _wait_until(
        predicate: Callable[[], bool], timeout: float = 10.0, note: str = ""
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        if not predicate():
            raise TimeoutError(f"{timeout}s 内未满足条件: {note}")

    @not_action
    def _well_uuid(self, plate: Any, well_label: str) -> str:
        return str(plate.get_well(well_label).unilabos_uuid)

    # ── 动作 ────────────────────────────────────────────────

    @action(
        display_name="准备台面",
        description="ensure 固定 uuid 的台面 Deck（幂等）并挂载到设备自身",
        always_free=True,
        feedback_interval=1.0,
    )
    def prepare_bench(self) -> Dict[str, Any]:
        from unilabos.resources import materials

        node = self._device_node
        existing = node.resource_tracker.uuid_to_resources.get(self.deck_uuid)
        if existing is None:
            ensured = materials.ensure(
                build_bench_deck(self.deck_name, self.deck_uuid)
            )
            assert ensured.trees[0].root_node.res_content.uuid == self.deck_uuid
            # 挂到设备自身：按 uuid 权威拉取实例化并登记 tracker。
            materials.assign(node, self.deck_uuid)
        deck = node.resource_tracker.uuid_to_resources[self.deck_uuid]
        self.logger.info(f"[Bench] 台面就绪: {deck.name} ({self.deck_uuid})")
        return {
            "success": True,
            "deck_uuid": self.deck_uuid,
            "site_labels": list(BENCH_SITE_LAYOUT),
        }

    @action(
        display_name="补给耗材",
        description="创建新一轮枪头盒（注册表类名）与 12 孔板（草稿 + A1 预置 Water）并挂上台面",
        always_free=True,
        feedback_interval=1.0,
    )
    def provision_labware(
        self,
        tips_site: str = "T1",
        plate_site: str = "T2",
        water_volume: float = 40.0,
    ) -> Dict[str, Any]:
        from unilabos.resources import materials

        node = self._device_node
        self._round += 1
        current_round = self._round

        # 路径一：按注册表类名创建（后端按类目录实例化草稿再发号）。
        tips = materials.create(
            "demo_tips_24",
            name=f"bench_tips_r{current_round}",
            node=node,
        )
        materials.assign(node, tips, parent=self.deck_name, slot=tips_site)

        # 路径二：本地草稿创建（A1 预置 Water），权威发号返回权威实例。
        draft = demo_plate_12(f"bench_plate_r{current_round}")
        materials.set_substance_on_target(
            draft.get_well("A1"), "Water", float(water_volume)
        )
        plate = materials.create(draft, node=node)
        materials.assign(node, plate, parent=self.deck_name, slot=plate_site)

        self._tips_uuid = str(tips.unilabos_uuid)
        self._plate_uuid = str(plate.unilabos_uuid)
        self.logger.info(
            f"[Bench] 第 {current_round} 轮补给完成: "
            f"{tips.name}->{tips_site}, {plate.name}->{plate_site}"
        )
        return {
            "success": True,
            "round": current_round,
            "tips_uuid": self._tips_uuid,
            "tips_site": tips_site,
            "plate_uuid": self._plate_uuid,
            "plate_site": plate_site,
            "plate_a1_substances": self._authority_substances(
                self._plate_uuid, self._well_uuid(plate, "A1")
            ),
        }

    @action(
        display_name="孔位加液",
        description="对当前板的孔位 add_liquid + commit，由快照观察者自动上报权威并等待可见",
        always_free=True,
        feedback_interval=1.0,
    )
    def hydrate_well(
        self,
        well: str = "A2",
        substance: str = "Buffer",
        volume: float = 25.0,
    ) -> Dict[str, Any]:
        node = self._device_node
        plate = node.resource_tracker.uuid_to_resources[self._plate_uuid]
        target = plate.get_well(well)
        target.tracker.add_liquid(substance, float(volume))
        target.tracker.commit()

        well_uuid = str(target.unilabos_uuid)
        expected = [substance, float(volume), "ul"]
        self._wait_until(
            lambda: expected
            in (self._authority_substances(self._plate_uuid, well_uuid) or []),
            note=f"observer 自动快照（{well} 加液）未到达权威",
        )
        self.logger.info(f"[Bench] {plate.name} {well} += {substance} {volume}ul")
        return {
            "success": True,
            "well": well,
            "well_uuid": well_uuid,
            "substances": self._authority_substances(self._plate_uuid, well_uuid),
        }

    @action(
        display_name="转移板位",
        description="materials.transfer 把当前板换到目标位点（权威先落位，再投影回设备）",
        always_free=True,
        feedback_interval=1.0,
    )
    def relocate_plate(self, to_site: SiteSlot = "T3") -> Dict[str, Any]:
        """把当前板换到台面目标位点。

        Args:
            to_site[目标位点]: SiteSlot——前端 Site 选择器提交权威 ResourceSite
                的 uuid；工作流/脚本可直接传 label 便捷形态（如 "T3"）。
                ``materials.transfer`` 的 Site 选择器对 uuid/label/索引统一兼容。
        """
        from unilabos.resources import materials

        node = self._device_node
        before = node.resource_tracker.uuid_to_resources[self._plate_uuid]
        result = asyncio.run(
            materials.transfer(
                self._plate_uuid,
                self.device_id,
                self.deck_uuid,
                to_site,
                source_device_id=self.device_id,
            )
        )
        if not result.get("success"):
            raise RuntimeError(f"transfer 失败: {result}")
        # unload -> load 投影完成后，tracker 中实例已被权威重建替换。
        self._wait_until(
            lambda: node.resource_tracker.uuid_to_resources.get(self._plate_uuid)
            is not before,
            note="transfer 投影（unload->load 重建实例）未完成",
        )
        moved = node.resource_tracker.uuid_to_resources[self._plate_uuid]
        self.logger.info(f"[Bench] {moved.name} 已换位到 {to_site}")
        return {
            "success": True,
            "plate_uuid": self._plate_uuid,
            "to_site": to_site,
            "instance_rebuilt": moved is not before,
            "sites": self._deck_sites(),
        }

    @action(
        display_name="废弃枪头盒",
        description="materials.remove 删除当前枪头盒（权威删除 + 位点自动释放），随后本地卸载",
        always_free=True,
        feedback_interval=1.0,
    )
    def dispose_tips(self) -> Dict[str, Any]:
        from unilabos.resources import materials

        node = self._device_node
        tips_uuid = self._tips_uuid
        removed = materials.remove(tips_uuid, source_device_id=self.device_id)

        instance = node.resource_tracker.uuid_to_resources.get(tips_uuid)
        if instance is not None:
            # 权威已删。先从 tracker 退场（解除快照监听），再物理卸载——
            # 否则卸载事件会给已删除的物料排队快照，把它重新写回权威。
            node.resource_tracker.remove_resource(instance)
            if instance.parent is not None:
                instance.parent.unassign_child_resource(instance)

        deleted_in_authority = False
        try:
            self._gateway().get_material(tips_uuid)
        except Exception:  # noqa: BLE001 - 本地/HostLink 网关 404 形态不同
            deleted_in_authority = True
        self.logger.info(f"[Bench] 枪头盒 {tips_uuid} 已删除并本地卸载")
        return {
            "success": True,
            "tips_uuid": tips_uuid,
            # recursive 删除返回整棵树的 uuid（根 + 24 个枪头位）
            "removed_count": len(removed),
            "root_removed": tips_uuid in removed,
            "deleted_in_authority": deleted_in_authority,
            "sites": self._deck_sites(),
        }

    @action(
        display_name="台面报告",
        description="从权威读取台面终态：位点占用 + 台面上各板的孔位内容物",
        always_free=True,
        feedback_interval=1.0,
    )
    def bench_report(self) -> Dict[str, Any]:
        gateway = self._gateway()
        tree = gateway.get_tree(self.deck_uuid)
        wells: Dict[str, Dict[str, List[List[Any]]]] = {}
        plates = {
            node.material.material_uuid: node.material.name
            for node in tree.nodes
            if node.material.class_name == "Plate"
        }
        for node in tree.nodes:
            parent_uuid = node.material.parent_material_uuid
            if parent_uuid in plates and node.data.substances:
                label = node.material.name.rsplit("_", 1)[-1]
                wells.setdefault(plates[parent_uuid], {})[label] = [
                    [entry.name, float(entry.quantity), entry.quantity_unit]
                    for entry in node.data.substances
                ]
        return {
            "success": True,
            "deck_uuid": self.deck_uuid,
            "sites": self._deck_sites(),
            "wells": wells,
        }

    # ── 闭环证明 ────────────────────────────────────────────

    @not_action
    def _await_authority(self, timeout: float = 20.0) -> None:
        """等待 HostLink 物料链路就绪（权威可访问即可，不要求台面存在）。"""

        def probe() -> bool:
            try:
                self._gateway().list_templates()
                return True
            except Exception:  # noqa: BLE001 - 链路尚未建立
                return False

        self._wait_until(probe, timeout=timeout, note="物料权威链路未就绪")

    @not_action
    def _run_proof(self, proof_file: Path) -> None:
        """真实运行时里跑一遍物料 CRUD 闭环，并原子写出可机读终态。"""

        delay = float(os.environ.get("SITE_DEMO_START_DELAY", "1.0"))
        time.sleep(max(0.0, delay))
        self._phase = "running"
        try:
            self._await_authority()
            prepared = self.prepare_bench()
            provisioned = self.provision_labware(
                tips_site="T1", plate_site="T2", water_volume=40.0
            )
            after_provision = self._deck_sites()
            hydrated = self.hydrate_well(well="A2", substance="Buffer", volume=25.0)
            relocated = self.relocate_plate(to_site="T3")
            disposed = self.dispose_tips()
            report = self.bench_report()
            proof = {
                "success": True,
                "backend": str(
                    getattr(self._device_node, "backend_name", "unknown")
                ),
                "prepared": prepared,
                "provisioned": provisioned,
                "after_provision": after_provision,
                "hydrated": hydrated,
                "relocated": relocated,
                "disposed": disposed,
                "report": report,
            }
            self._phase = "done"
        except Exception as exc:  # noqa: BLE001 - 演示用，报告任何失败
            self.logger.exception("物料闭环失败")
            proof = {
                "success": False,
                "backend": str(
                    getattr(self._device_node, "backend_name", "unknown")
                ),
                "error": f"{type(exc).__name__}: {exc}",
            }
            self._phase = "failed"
        proof_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = proof_file.with_suffix(proof_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(proof, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(proof_file)
