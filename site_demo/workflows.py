"""站点演示的默认子工作流：装载 -> 转移 -> 查看（同一设备三步串行）。"""

from unilabos.registry.workflows import WorkflowBuildContext, workflow

#: smoke/测试按显示名检索上报结果，保持单一出处。
SITE_TOUR_WORKFLOW_NAME = "位点操作演示"


@workflow(
    display_name=SITE_TOUR_WORKFLOW_NAME,
    description="装载样品到 A2 -> 转移到 B1 -> 查看位点占用快照（与闭环用的 A1/B2 互不干扰）",
    tags=["site-demo", "available-sites"],
)
def site_tour(ctx: WorkflowBuildContext) -> None:
    """rack 类在图中只有一个实例：首步 run_template 自动填充 device_id。"""

    ctx.run_template(
        "sample_rack_demo/load_sample",
        {"site_label": "A2", "sample_name": "wf-sample"},
        name="装载样品",
    )
    ctx.run(
        "sample_rack/transfer_sample",
        {"from_label": "A2", "to_label": "B1"},
        name="转移样品",
    )
    ctx.run("sample_rack/inspect_sites", {}, name="查看位点")
