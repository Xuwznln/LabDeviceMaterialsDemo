"""物料演示的工作流模板（随注册表上报到调度权威，前端「编排画布」的模板面板可见）。

每个 @workflow 带 guide（运行前准备 / 预期效果）和每步 description，前端「流程说明」
按"准备 → 执行步骤 → 预期效果"展示；脚本 / smoke 用
``POST /api/v1/workflows/from-template`` 实例化后再创建任务。

设备启动后不自跑任何动作，所有闭环都由这里的 @workflow 触发：

- ``site_loop``      host 侧样品架第一轮：核对位点声明 -> 装载 A1 -> 转移 B2 -> 复查；
- ``material_loop``  slave 侧物料工作台第一轮：准备台面 -> 补给 -> A2 加液 -> 换位到 T3 ->
  废弃枪头盒 -> 报告（六步串行，跨进程分发到 slave 设备执行）；
- ``site_tour``      样品架第二轮：装载 A2 -> 转移 B1 -> 查看（与第一轮的 A1/B2 互不干扰）；
- ``material_flow``  物料工作台第二轮：补给 -> B1 加液 -> 换位到 T4 -> 废弃 -> 报告。

第三阶段「出库装板并加液」不是 @workflow，而是像编排画布一样经 HTTP 上传
（见 ``material_flow_graph.py``）。
"""

from unilabos.registry.workflows import WorkflowBuildContext, WorkflowGuide, workflow

#: smoke/测试按显示名检索上报结果，保持单一出处。
SITE_LOOP_WORKFLOW_NAME = "位点闭环演示"
MATERIAL_LOOP_WORKFLOW_NAME = "物料闭环演示"
SITE_TOUR_WORKFLOW_NAME = "位点操作演示"
MATERIAL_FLOW_WORKFLOW_NAME = "物料流转演示"


@workflow(
    display_name=SITE_LOOP_WORKFLOW_NAME,
    description="核对权威位点与 @device(available_sites=...) 声明一致 -> 装载样品到 A1 -> 转移到 B2 -> 复查位点占用",
    tags=["materials-demo", "available-sites"],
    guide=WorkflowGuide(
        preparation=[
            "「设备」页确认 host 侧样品架 sample_rack 在线（它随 host 图启动，不用单独装）。",
            "「物料」页找到「四位样品架」：A1、B2 两个位点应为空；不为空就先把上面的样品移走或删除。",
            "无需手动出库：样品物料由「装载样品」这一步按 sample_name 自动创建（重跑复用同名物料）。",
        ],
        expected=[
            "任务 succeeded，四步全部成功。",
            "「物料」页里样品架的 B2 位被 proof-sample 占用，A1 空。",
            "「复查位点」的返回值 sites 里 4 个位点与 @device(available_sites=...) 声明一一对应。",
        ],
        notes=["与「位点操作演示」用不同位点（A1/B2 与 A2/B1），两条可以先后各跑一遍互不干扰。"],
    ),
)
def site_loop(ctx: WorkflowBuildContext) -> None:
    """位点链路第一轮：声明 → 注册表模板 → 权威位点实例 → 占用流转。"""

    ctx.run_template(
        "sample_rack_demo/verify_site_definition",
        {},
        name="核对位点声明",
        description="把权威里样品架的位点实例与驱动 @device(available_sites=...) 的声明逐项比对，不一致即报错。",
    )
    ctx.run(
        "sample_rack/load_sample",
        {"site": "A1", "sample_name": "proof-sample"},
        name="装载样品",
        description="创建（或复用）名为 proof-sample 的样品物料并放入 A1；A1 已被占用则报错。",
    )
    ctx.run(
        "sample_rack/transfer_sample",
        {"from_label": "A1", "to_label": "B2"},
        name="转移样品",
        description="把 A1 上的样品移到 B2：权威先落位，再投影回设备。",
    )
    ctx.run(
        "sample_rack/inspect_sites",
        {},
        name="复查位点",
        description="返回 4 个位点的占用快照，此时应只有 B2 被占用。",
    )


@workflow(
    display_name=MATERIAL_LOOP_WORKFLOW_NAME,
    description=(
        "第一轮物料 CRUD：ensure 台面 -> 补给耗材（T1/T2，A1 预置 Water）-> A2 加液 -> "
        "板换位到 T3 -> 废弃枪头盒 -> 台面报告（全部跨进程分发到 slave 侧 bench）"
    ),
    tags=["materials-demo", "materials"],
    guide=WorkflowGuide(
        preparation=[
            "「设备」页确认 slave 侧物料工作台 material_bench 在线（它由 slave 进程接入，先按 README 启动 slave）。",
            "「物料」页里台面 bench_deck 的 T1、T2、T3 位点应为空；有上一轮留下的板 / 枪头盒就先删除。",
            "无需手动出库：枪头盒与 12 孔板由「补给耗材」这一步创建并挂到台面（A1 孔预置 40 µL Water）。",
        ],
        expected=[
            "任务 succeeded，六步全部成功。",
            "「物料」页里台面 T3 上有新板（A1 有 Water 40 µL、A2 有 Buffer 25 µL），T1 的枪头盒已删除。",
            "「台面报告」的返回值列出台面上每块板的孔位内容物。",
        ],
        notes=["跑完后板留在 T3；「物料流转演示」是接着这一轮跑的第二轮（用 T1/T2/T4）。"],
    ),
)
def material_loop(ctx: WorkflowBuildContext) -> None:
    """bench 在 slave 图中，host 上报时不可见其实例：一律 ctx.run 显式指定 device_id。"""

    ctx.run(
        "material_bench/prepare_bench",
        {},
        name="准备台面",
        description="幂等 ensure 固定 uuid 的台面 Deck 并挂到设备自身；已存在则原样复用。",
    )
    ctx.run(
        "material_bench/provision_labware",
        {"tips_site": "T1", "plate_site": "T2", "water_volume": 40.0},
        name="补给耗材",
        description="按注册表类创建枪头盒挂到 T1；本地草稿创建 12 孔板（A1 预置 40 µL Water）挂到 T2。",
    )
    ctx.run(
        "material_bench/hydrate_well",
        {"well": "A2", "substance": "Buffer", "volume": 25.0},
        name="孔位加液",
        description="向当前板 A2 孔加 25 µL Buffer，等权威可见后返回孔位内容物。",
    )
    ctx.run(
        "material_bench/relocate_plate",
        {"to_site": "T3"},
        name="转移板位",
        description="materials.transfer 把当前板从 T2 换到 T3。",
    )
    ctx.run(
        "material_bench/dispose_tips",
        {},
        name="废弃枪头盒",
        description="从权威删除 T1 上的枪头盒并从台面卸载。",
    )
    ctx.run(
        "material_bench/bench_report",
        {},
        name="台面报告",
        description="读权威台面树，汇总每块板每个孔的内容物。",
    )


@workflow(
    display_name=SITE_TOUR_WORKFLOW_NAME,
    description="装载样品到 A2 -> 转移到 B1 -> 查看位点占用快照（与第一轮的 A1/B2 互不干扰）",
    tags=["materials-demo", "available-sites"],
    guide=WorkflowGuide(
        preparation=[
            "「设备」页确认 host 侧样品架 sample_rack 在线。",
            "「物料」页里「四位样品架」的 A2、B1 位点应为空（第一轮用的是 A1/B2，互不影响）。",
            "插入画布时首步的角色是设备类 sample_rack_demo：图里只有一台样品架时会自动选中，多台时手选。",
        ],
        expected=[
            "任务 succeeded，三步全部成功。",
            "「物料」页里样品架的 B1 位被 wf-sample 占用，A2 空。",
            "「查看位点」的返回值 sites 是 4 个位点的占用快照。",
        ],
    ),
)
def site_tour(ctx: WorkflowBuildContext) -> None:
    """rack 类在图中只有一个实例：首步 run_template 自动填充 device_id。"""

    ctx.run_template(
        "sample_rack_demo/load_sample",
        # site 是 SiteSlot：前端提交权威 Site uuid，模板/脚本可用 label 便捷形态
        {"site": "A2", "sample_name": "wf-sample"},
        name="装载样品",
        description="创建（或复用）名为 wf-sample 的样品物料并放入 A2；A2 已被占用则报错。",
    )
    ctx.run(
        "sample_rack/transfer_sample",
        {"from_label": "A2", "to_label": "B1"},
        name="转移样品",
        description="把 A2 上的样品移到 B1。",
    )
    ctx.run(
        "sample_rack/inspect_sites",
        {},
        name="查看位点",
        description="返回 4 个位点的占用快照，此时 B1 被占用、A2 为空。",
    )


@workflow(
    display_name=MATERIAL_FLOW_WORKFLOW_NAME,
    description=(
        "物料 CRUD：补给耗材（T1/T2）-> B1 加液 -> 板换位到 T4 -> "
        "废弃枪头盒 -> 台面报告；可独立运行，也可接续第一轮"
    ),
    tags=["materials-demo", "materials"],
    guide=WorkflowGuide(
        preparation=[
            "默认启动时自动准备台面；若设置 MATERIALS_DEMO_SKIP_AUTO_PREPARE，请先运行「准备台面」或「物料闭环演示」。",
            "「设备」页确认 slave 侧 material_bench 在线；「物料」页里台面的 T1、T2、T4 位点应为空。",
            "无需手动出库：新的枪头盒与 12 孔板由「补给耗材」创建并挂到 T1/T2。",
        ],
        expected=[
            "任务 succeeded，五步全部成功。",
            "T4 有本轮板（B1 有 Dye 15 µL），T1 的枪头盒已删除；已有 T3 板保持不变。",
            "「台面报告」列出现有板及各自孔位内容物。",
        ],
    ),
)
def material_flow(ctx: WorkflowBuildContext) -> None:
    """跨进程设备一律用 ctx.run 显式指定 device_id（run_template 的
    class 单实例自动填充只对 host 图内设备有效）。"""

    ctx.run(
        "material_bench/provision_labware",
        {"tips_site": "T1", "plate_site": "T2", "water_volume": 40.0},
        name="补给耗材",
        description="创建新一轮枪头盒（T1）与 12 孔板（T2，A1 预置 40 µL Water）。",
    )
    ctx.run(
        "material_bench/hydrate_well",
        {"well": "B1", "substance": "Dye", "volume": 15.0},
        name="孔位加液",
        description="向新板 B1 孔加 15 µL Dye。",
    )
    ctx.run(
        "material_bench/relocate_plate",
        {"to_site": "T4"},
        name="转移板位",
        description="把新板从 T2 换到 T4；已有 T3 板保持不变。",
    )
    ctx.run(
        "material_bench/dispose_tips",
        {},
        name="废弃枪头盒",
        description="删除并卸载 T1 上的枪头盒。",
    )
    ctx.run(
        "material_bench/bench_report",
        {},
        name="台面报告",
        description="汇总台面上现有板的孔位内容物。",
    )
