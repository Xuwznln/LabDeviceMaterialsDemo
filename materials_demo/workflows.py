"""站点演示的默认子工作流。

- ``site_tour``      host 侧样品架：装载 -> 转移 -> 查看（三步串行）；
- ``material_flow``  slave 侧物料工作台：补给 -> 加液 -> 换位 -> 废弃 ->
  报告（五步串行，跨进程分发到 slave 设备执行）。
"""

from unilabos.registry.workflows import WorkflowBuildContext, workflow

#: smoke/测试按显示名检索上报结果，保持单一出处。
SITE_TOUR_WORKFLOW_NAME = "位点操作演示"
MATERIAL_FLOW_WORKFLOW_NAME = "物料流转演示"


@workflow(
    display_name=SITE_TOUR_WORKFLOW_NAME,
    description="装载样品到 A2 -> 转移到 B1 -> 查看位点占用快照（与闭环用的 A1/B2 互不干扰）",
    tags=["materials-demo", "available-sites"],
)
def site_tour(ctx: WorkflowBuildContext) -> None:
    """rack 类在图中只有一个实例：首步 run_template 自动填充 device_id。"""

    ctx.run_template(
        "sample_rack_demo/load_sample",
        # site 是 SiteSlot：前端提交权威 Site uuid，模板/脚本可用 label 便捷形态
        {"site": "A2", "sample_name": "wf-sample"},
        name="装载样品",
    )
    ctx.run(
        "sample_rack/transfer_sample",
        {"from_label": "A2", "to_label": "B1"},
        name="转移样品",
    )
    ctx.run("sample_rack/inspect_sites", {}, name="查看位点")


@workflow(
    display_name=MATERIAL_FLOW_WORKFLOW_NAME,
    description=(
        "第二轮物料 CRUD：补给耗材（T1/T2）-> B1 加液 -> 板换位到 T4 -> "
        "废弃枪头盒 -> 台面报告（闭环第一轮已把板留在 T3）"
    ),
    tags=["materials-demo", "materials"],
)
def material_flow(ctx: WorkflowBuildContext) -> None:
    """bench 设备在 slave 图中，host 上报时不可见其实例：
    跨进程设备一律用 ctx.run 显式指定 device_id（run_template 的
    class 单实例自动填充只对 host 图内设备有效）。"""

    ctx.run(
        "material_bench/provision_labware",
        {"tips_site": "T1", "plate_site": "T2", "water_volume": 40.0},
        name="补给耗材",
    )
    ctx.run(
        "material_bench/hydrate_well",
        {"well": "B1", "substance": "Dye", "volume": 15.0},
        name="孔位加液",
    )
    ctx.run(
        "material_bench/relocate_plate",
        {"to_site": "T4"},
        name="转移板位",
    )
    ctx.run("material_bench/dispose_tips", {}, name="废弃枪头盒")
    ctx.run("material_bench/bench_report", {}, name="台面报告")
