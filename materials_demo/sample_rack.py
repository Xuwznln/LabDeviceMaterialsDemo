"""四位样品架 — 演示 @device available_sites 固定位点的声明、实例化与占用流转。

位点链路（本演示的核心）：

1. ``@device(available_sites=RACK_SITES)`` 声明固定位点，AST 扫描进注册表；
2. host 启动时注册表同步为微后端资源模板，图中 rack 物料按模板/图落库，
   每个位点获得权威 uuid 与 ``occupied_material_uuid`` 占用字段；
3. 设备动作通过 materials gateway 核对位点定义（verify_site_definition）、读取自身位点
   快照（inspect_sites）、创建样品并放入位点（load_sample）、在位点间转移
   （transfer_sample）——所有占用状态都由微后端权威维护，设备内存不持有副本。

设备启动后不自跑任何动作：「核对声明 -> 装载 A1 -> 转移到 B2 -> 复查」闭环由
``workflows.py`` 的 @workflow 经管理 API 触发，断言只看各节点返回值。
"""

import logging
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.registry.placeholder_type import SiteSlot
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
    ResourceDictPositionSize,
)
from unilabos.resources.objects.site import SiteDefinition
from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialDataWrite,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)

#: 样品物料的模板名（rack 模板由注册表自动同步，样品模板由设备自行确保）。
SAMPLE_TEMPLATE_NAME = "materials_demo_sample"


#: 2x2 网格的四个固定位点；图与断言共享同一出处。
#: 必须保持字面量构造——AST 注册表扫描静态解析该常量，不执行任何函数。
RACK_SITES = [
    SiteDefinition(
        index=1,
        label="A1",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=60.0, y=40.0, z=0.0),
            position3d=ResourceDictPositionObject(x=60.0, y=40.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["demo_sample"],
        parent_link="A1",
        description="样品架 A1 位",
        meta_data={"row": "A", "column": 1},
    ),
    SiteDefinition(
        index=2,
        label="A2",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=180.0, y=40.0, z=0.0),
            position3d=ResourceDictPositionObject(x=180.0, y=40.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["demo_sample"],
        parent_link="A2",
        description="样品架 A2 位",
        meta_data={"row": "A", "column": 2},
    ),
    SiteDefinition(
        index=3,
        label="B1",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=60.0, y=160.0, z=0.0),
            position3d=ResourceDictPositionObject(x=60.0, y=160.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["demo_sample"],
        parent_link="B1",
        description="样品架 B1 位",
        meta_data={"row": "B", "column": 1},
    ),
    SiteDefinition(
        index=4,
        label="B2",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=180.0, y=160.0, z=0.0),
            position3d=ResourceDictPositionObject(x=180.0, y=160.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["demo_sample"],
        parent_link="B2",
        description="样品架 B2 位",
        meta_data={"row": "B", "column": 2},
    ),
]


@device(
    id="sample_rack_demo",
    display_name="四位样品架",
    category=["virtual_device", "storage"],
    description="available_sites 固定位点演示：位点实例化、样品装载与位点间转移",
    available_sites=RACK_SITES,
    supported_backends=["hostlink", "ros2"],
)
class SampleRackDemo:
    """位点占用完全以微后端权威为准的样品架。"""

    run_in_test_mode = True

    def __init__(
        self,
        device_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """初始化样品架。

        Args:
            device_id[设备ID]: 设备实例 ID，默认 sample_rack_demo。
        """
        self.device_id = device_id or "sample_rack_demo"
        self.logger = logging.getLogger(f"SampleRack.{self.device_id}")
        self._start_time = time.time()
        self._template_ready = False

    @not_action
    def post_init(self, node: Any) -> None:
        self._device_node = node

    @property
    @topic_config(period=1.0)
    def heartbeat(self) -> int:
        """自启动以来的心跳秒数。"""
        return int(time.time() - self._start_time)

    # ── 微后端网关 ──────────────────────────────────────────

    @staticmethod
    @not_action
    def _gateway() -> Any:
        from unilabos.resources.materials import resolve_materials_gateway

        return resolve_materials_gateway()

    @not_action
    def _mutation(self, operation: str, effect_key: str) -> InventoryMutation:
        return InventoryMutation(
            command_uuid=str(uuid4()),
            effect_key=effect_key,
            operation=operation,
            actor_type="virtual_device",
            actor_uuid=self.device_id,
            observed_at_ms=int(time.time() * 1000),
        )

    @not_action
    def _rack(self) -> Any:
        """读取自身权威物料聚合（图 ensure 后即存在，含全部位点）。"""

        return self._gateway().get_material_by_resource_id(self.device_id)

    @not_action
    def _resolve_site(self, rack: Any, selector: str) -> Any:
        """SiteSlot 选择器：优先按权威 Site uuid 精确匹配，回退 label 便捷形态。"""

        for site in rack.sites:
            if str(site.site_uuid) == selector:
                return site
        for site in rack.sites:
            if site.label == selector:
                return site
        raise ValueError(
            f"位点 {selector!r} 不存在，可用: {[site.label for site in rack.sites]}"
        )

    @not_action
    def _ensure_sample_template(self) -> None:
        if self._template_ready:
            return
        gateway = self._gateway()
        if not any(
            template.name == SAMPLE_TEMPLATE_NAME
            for template in gateway.list_templates()
        ):
            gateway.create_template(
                self._mutation("put_template", f"ensure-template:{SAMPLE_TEMPLATE_NAME}"),
                ResourceTemplateWrite(
                    name=SAMPLE_TEMPLATE_NAME,
                    display_name="演示样品",
                    class_name="Resource",
                    category=["demo_sample"],
                    definition={"demo": "site-show", "schema_version": 1},
                ),
            )
        self._template_ready = True

    @not_action
    def _sample_by_resource_id(self, resource_id: str) -> Any | None:
        try:
            return self._gateway().get_material_by_resource_id(resource_id)
        except Exception as exc:  # noqa: BLE001 - 本地/HTTP 网关 404 形态不同
            if (
                "not found" in str(exc).lower()
                or getattr(exc, "status_code", None) == 404
            ):
                return None
            raise

    @not_action
    def _site_snapshot(self, rack: Any) -> List[Dict[str, Any]]:
        gateway = self._gateway()
        snapshot: List[Dict[str, Any]] = []
        for site in sorted(rack.sites, key=lambda item: int(item.site_index)):
            occupied_by = ""
            if site.occupied_material_uuid:
                occupied_by = gateway.get_material(
                    site.occupied_material_uuid
                ).material.name
            position = dict(site.pose.get("position") or {})
            snapshot.append(
                {
                    "label": site.label,
                    "site_index": int(site.site_index),
                    "x": float(position.get("x", 0.0)),
                    "y": float(position.get("y", 0.0)),
                    "allowed_resource_categories": list(
                        site.allowed_resource_categories
                    ),
                    "meta_data": dict(site.meta_data),
                    "occupied_by": occupied_by,
                }
            )
        return snapshot

    # ── 动作 ────────────────────────────────────────────────

    @action(
        display_name="查看位点",
        description="读取微后端权威中的位点快照：label、坐标、允许类目与占用者",
        always_free=True,
        feedback_interval=1.0,
    )
    def inspect_sites(self) -> Dict[str, Any]:
        rack = self._rack()
        return {
            "success": True,
            "rack_uuid": rack.material.material_uuid,
            "sites": self._site_snapshot(rack),
        }

    @action(
        display_name="装载样品",
        description="创建（或复用）样品物料并放入指定位点；位点被他人占用则报错",
        always_free=True,
        feedback_interval=1.0,
    )
    def load_sample(
        self, site: SiteSlot = "A1", sample_name: str = "demo-sample"
    ) -> Dict[str, Any]:
        """把样品装载到指定位点。

        Args:
            site[目标位点]: SiteSlot——前端 Site 选择器提交权威 ResourceSite
                的 uuid；工作流/脚本可直接传 label 便捷形态（如 "A2"）。
            sample_name[样品名]: 样品物料的 resource_id / 展示名，重复调用复用同名物料。
        """
        gateway = self._gateway()
        rack = self._rack()
        resolved = self._resolve_site(rack, site)

        self._ensure_sample_template()
        sample = self._sample_by_resource_id(sample_name)
        if sample is None:
            created = gateway.create_tree(
                self._mutation(
                    "create_material_tree", f"create-sample:{sample_name}"
                ),
                MaterialTreeCreate(
                    nodes=[
                        MaterialNodeCreate(
                            client_ref="sample",
                            identity=MaterialIdentityWrite(
                                resource_id=sample_name,
                                name=sample_name,
                                description="site-show 演示样品",
                                resource_type="resource",
                                class_name="Resource",
                                template_name=SAMPLE_TEMPLATE_NAME,
                                meta_data={"demo": "site-show"},
                            ),
                            data=MaterialDataWrite(
                                data={},
                                sites_initialized=True,
                                state_status="ready",
                            ),
                        )
                    ]
                ),
            )
            sample = created.data.nodes[0]
        sample_uuid = sample.material.material_uuid

        if resolved.occupied_material_uuid not in (None, sample_uuid):
            raise ValueError(f"位点 {resolved.label} 已被占用")
        if resolved.occupied_material_uuid != sample_uuid:
            gateway.move_material(
                self._mutation(
                    "move_material", f"load:{sample_name}->{resolved.label}"
                ),
                MaterialMove(
                    material_uuid=sample_uuid,
                    destination_site_uuid=resolved.site_uuid,
                ),
            )
        self.logger.info(f"[SampleRack] 样品 {sample_name} 已装载到 {resolved.label}")
        return {
            "success": True,
            "site_label": resolved.label,
            "sample_name": sample_name,
            "sample_uuid": sample_uuid,
        }

    @action(
        display_name="转移样品",
        description="把来源位点上的样品转移到目标位点；来源为空或目标被占用则报错",
        always_free=True,
        feedback_interval=1.0,
    )
    def transfer_sample(
        self, from_label: str = "A1", to_label: str = "B2"
    ) -> Dict[str, Any]:
        gateway = self._gateway()
        rack = self._rack()
        source = self._resolve_site(rack, from_label)
        destination = self._resolve_site(rack, to_label)

        if not source.occupied_material_uuid:
            raise ValueError(f"位点 {from_label} 上没有样品")
        if destination.occupied_material_uuid:
            raise ValueError(f"位点 {to_label} 已被占用")

        sample_uuid = str(source.occupied_material_uuid)
        sample_name = gateway.get_material(sample_uuid).material.name
        gateway.move_material(
            self._mutation(
                "move_material", f"transfer:{from_label}->{to_label}"
            ),
            MaterialMove(
                material_uuid=sample_uuid,
                destination_site_uuid=destination.site_uuid,
            ),
        )
        self.logger.info(
            f"[SampleRack] 样品 {sample_name} 已从 {from_label} 转移到 {to_label}"
        )
        return {
            "success": True,
            "sample_uuid": sample_uuid,
            "sample_name": sample_name,
            "from_label": from_label,
            "to_label": to_label,
        }

    @action(
        display_name="核对位点声明",
        description="把权威中的位点快照与 @device(available_sites=...) 声明逐项对齐（label / index / 坐标 / 类目），不一致则报错",
        always_free=True,
        feedback_interval=1.0,
    )
    def verify_site_definition(self) -> Dict[str, Any]:
        """声明 → 注册表模板 → 权威位点实例 这条链路的可断言证据。"""

        rack = self._rack()
        if len(rack.sites) != len(RACK_SITES):
            raise RuntimeError(f"权威中位点数 {len(rack.sites)} 与声明 {len(RACK_SITES)} 不一致")
        check = self._definition_check(rack)
        if not check["matches"]:
            raise RuntimeError(f"权威位点与声明不一致: expected={check['expected']} actual={check['actual']}")
        return {
            "success": True,
            "rack_uuid": rack.material.material_uuid,
            "backend": str(getattr(self._device_node, "backend_name", "unknown")),
            **check,
        }

    @not_action
    def _definition_check(self, rack: Any) -> Dict[str, Any]:
        """把权威位点快照与装饰器声明逐项对齐（label/index/坐标/类目）。"""

        expected = [
            {
                "label": item.label,
                "site_index": int(item.index),
                "x": item.pose.position.x,
                "y": item.pose.position.y,
                "allowed_resource_categories": list(
                    item.allowed_resource_categories
                ),
            }
            for item in RACK_SITES
        ]
        actual = [
            {
                "label": entry["label"],
                "site_index": entry["site_index"],
                "x": entry["x"],
                "y": entry["y"],
                "allowed_resource_categories": entry[
                    "allowed_resource_categories"
                ],
            }
            for entry in self._site_snapshot(rack)
        ]
        return {"matches": actual == expected, "expected": expected, "actual": actual}
