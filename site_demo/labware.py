"""演示包自带的 PLR 耗材：台面 Deck、24 位枪头盒、12 孔板。

三件套全部通过 ``@resource`` 注册进本包的外部注册表（AST 扫描，
``--external_devices_only`` 下无需内置 YAML）：

- ``demo_bench_deck``   台面 Deck，2x2 四个 ResourceSite（T1-T4），
  占用状态由微后端权威维护（与 PRCXI9300Deck 同款 canonical Site 语义）；
- ``demo_tips_24``      6x4 = 24 位枪头盒，演示「按注册表类名创建」路径
  （materials.create("demo_tips_24", name=...)）；
- ``demo_plate_12``     4x3 = 12 孔板，孔位支持 set_substance / 液量追踪，
  演示草稿实例创建与孔位内容物实时上报。

画布约定：deck 400x320，site 128x86（SBS footprint），2x2 网格与
sample_rack 的 A1-B2 网格呼应；孔板/枪头盒内部布局按 SBS 间距排布。
"""

from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from pylabrobot.resources import (
    Coordinate,
    Deck,
    Plate,
    Resource,
    TipRack,
    Well,
)
from pylabrobot.resources.tip import Tip
from pylabrobot.resources.tip_rack import TipSpot
from pylabrobot.resources.utils import create_ordered_items_2d

from unilabos.registry.decorators import resource
from unilabos.resources.objects.site import ResourceSite
from unilabos.resources.resource_tracker import set_plr_template_name

#: deck 四个位点的画布布局（label -> 左下角坐标），2x2 网格。
BENCH_SITE_LAYOUT: Dict[str, tuple[float, float]] = {
    "T1": (40.0, 40.0),
    "T2": (220.0, 40.0),
    "T3": (40.0, 180.0),
    "T4": (220.0, 180.0),
}

#: SBS footprint 的位点尺寸。
BENCH_SITE_SIZE = {"width": 128.0, "height": 86.0, "depth": 0.0}


@resource(
    id="demo_bench_deck",
    category=["deck"],
    description="演示物料台面：2x2 四位点，占用由微后端权威维护，接受 plate/tip_rack",
    display_name="演示物料台面",
)
class DemoBenchDeck(Deck):
    """只接受 canonical ResourceSite 快照的最小 Deck（PRCXI9300Deck 同款语义）。"""

    def __init__(
        self,
        name: str,
        size_x: float = 400.0,
        size_y: float = 320.0,
        size_z: float = 0.0,
        sites: Optional[List[Union[ResourceSite, Dict[str, Any]]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(size_x, size_y, size_z, name)
        # 运行时实例总是经 build_bench_deck 带 canonical Site 构造；
        # 注册表 config_info 探测（无参默认构造）得到空台面即可。
        self.sites = [
            site.model_copy(deep=True)
            if isinstance(site, ResourceSite)
            else ResourceSite.model_validate(site)
            for site in (sites or [])
        ]
        template_name = type(self).__name__
        if self.sites:
            self.unilabos_uuid = self.sites[0].material_uuid
            template_name = self.sites[0].template_name
        set_plr_template_name(self, template_name)
        # label -> None：外部通过 list(keys()).index(label) 把 label 转 spot。
        import collections

        self._ordering = collections.OrderedDict(
            (site.label, None) for site in self.sites
        )

    def _get_site_location(self, index: int) -> Coordinate:
        position = self.sites[index].pose.position3d
        return Coordinate(position.x, position.y, position.z)

    def _get_site_resource(self, index: int) -> Optional[Resource]:
        location = self._get_site_location(index)
        for child in self.children:
            if child.location == location:
                return child
        return None

    def assign_child_resource(
        self,
        resource: Resource,
        location: Optional[Coordinate] = None,
        reassign: bool = True,
        spot: Optional[int] = None,
    ) -> None:
        index = spot
        if index is None:
            for i, site in enumerate(self.sites):
                if site.label == resource.name or (
                    location is not None and self._get_site_location(i) == location
                ):
                    index = i
                    break
        if index is None:
            for i in range(len(self.sites)):
                if self._get_site_resource(i) is None:
                    index = i
                    break
        if index is None or not 0 <= index < len(self.sites):
            raise ValueError(
                f"Deck {self.name} 没有可用位点放置 {resource.name!r}"
            )
        occupant_uuid = str(getattr(resource, "unilabos_uuid", "") or "")
        if not occupant_uuid:
            raise ValueError(
                f"物料 {resource.name} 缺少微后端分配的 UUID，不能放入位点"
            )
        # 先登记占用再触发 PLR assign：assign 回调（物料快照观察者）会立刻
        # 冻结整棵树，占用滞后写入会让快照携带中间态覆盖权威。失败回滚。
        previous = self.sites[index]
        self.sites[index] = previous.model_copy(
            update={"occupied_material_uuid": occupant_uuid}
        )
        try:
            super().assign_child_resource(
                resource,
                location=self._get_site_location(index),
                reassign=reassign,
            )
        except BaseException:
            self.sites[index] = previous
            raise

    def unassign_child_resource(self, resource: Resource) -> None:
        index = next(
            (
                i
                for i in range(len(self.sites))
                if self._get_site_resource(i) is resource
            ),
            None,
        )
        previous = None
        if index is not None:
            previous = self.sites[index]
            self.sites[index] = previous.model_copy(
                update={"occupied_material_uuid": None}
            )
        try:
            super().unassign_child_resource(resource)
        except BaseException:
            if index is not None and previous is not None:
                self.sites[index] = previous
            raise


def build_bench_deck(name: str, deck_uuid: str) -> DemoBenchDeck:
    """按画布布局构造带四个 canonical Site 的台面草稿（uuid 由调用方固定）。"""

    sites = [
        ResourceSite(
            uuid=str(uuid4()),
            template_name="DemoBenchDeck",
            material_uuid=deck_uuid,
            index=index,
            label=label,
            pose={
                "position": {"x": x, "y": y, "z": 0.0},
                "position3d": {"x": x, "y": y, "z": 0.0},
                "size": dict(BENCH_SITE_SIZE),
            },
            allowed_resource_categories=["plate", "tip_rack"],
            description=f"台面 {label} 位",
        )
        for index, (label, (x, y)) in enumerate(BENCH_SITE_LAYOUT.items(), start=1)
    ]
    deck = DemoBenchDeck(name=name, sites=sites)
    deck.unilabos_uuid = deck_uuid
    return deck


@resource(
    id="demo_tips_24",
    category=["tip_rack"],
    description="24 位演示枪头盒：6x4（300ul），演示按注册表类名创建物料",
    display_name="24 位演示枪头盒",
)
def demo_tips_24(name: str) -> TipRack:
    return TipRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=50.0,
        model="demo_tips_24",
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=6,
            num_items_y=4,
            dx=9.0,
            dy=7.0,
            dz=2.0,
            item_dx=18.0,
            item_dy=18.0,
            size_x=14.0,
            size_y=14.0,
            size_z=0.0,
            make_tip=lambda: Tip(
                has_filter=False,
                maximal_volume=300.0,
                total_tip_length=60.0,
                fitting_depth=51.0,
            ),
        ),
    )


@resource(
    id="demo_plate_12",
    category=["plate"],
    description="12 孔演示板：4x3（2200ul/孔），孔位支持 set_substance 与液量实时上报",
    display_name="12 孔演示板",
)
def demo_plate_12(name: str) -> Plate:
    return Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=44.0,
        lid=None,
        model="demo_plate_12",
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=4,
            num_items_y=3,
            dx=12.0,
            dy=8.0,
            dz=4.0,
            item_dx=27.0,
            item_dy=27.0,
            size_x=20.0,
            size_y=20.0,
            size_z=38.0,
            max_volume=2200.0,
        ),
    )
