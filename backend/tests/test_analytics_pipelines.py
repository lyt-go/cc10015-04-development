"""多维分析实验平台核心链路测试。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import store
from app.services.experiment import ExperimentService
from app.services.sample_group import SampleGroupService
from app.services.ab_result import AbResultService
from app.services.metric_def import MetricDefService
from app.services.expr_factor import ExprFactorService
from app.services.segment_compare import SegmentCompareService
from app.services.attr_analysis import AttrAnalysisService
from app.services.anomaly_detect import AnomalyDetectService
from app.services.forecast_model import ForecastModelService
from app.services.insight_report import InsightReportService
from app.services.metric_alias import MetricAliasService
from app.services.evaluate_board import EvaluateBoardService
from app.services.data_freshness import DataFreshnessService
from app.services.cohort_track import CohortTrackService
from app.services.data_export import DataExportService
from app.services.drill_down import DrillDownService

def _clear_all():
    for k in list(store._tables.keys()):
        store._tables[k] = []

def test_pipeline_experiment_to_sample_groups():
    """实验创建后自动生成对照和实验组分群。"""
    _clear_all()
    es = ExperimentService()
    entry, ms = es.create_entry({
        "方案编号": "EXPE-001",
        "实验名称": "新首页布局",
        "对照组说明": "旧版首页",
        "实验组说明": "新版首页",
        "样本总量": "10000",
        "分流比例": "5:5",
        "启动日期": "2026-11-01",
        "预期时长": "14",
    })
    assert entry is not None, ms
    sg_rows = store.rows("sample_group")
    assert len(sg_rows) >= 2, f"应创建对照组和实验组，实际{len(sg_rows)}"
    control = [r for r in sg_rows if r.get("group_type") == "control"]
    exp = [r for r in sg_rows if r.get("group_type") == "experiment"]
    assert len(control) >= 1
    assert len(exp) >= 1
    assert int(control[0].get("样本基数", 0)) > 0
    assert int(exp[0].get("样本基数", 0)) > 0

def test_sample_group_members():
    """分群能生成带差异的用户列表。"""
    _clear_all()
    # 先创建实验再用自动分群
    es = ExperimentService()
    es.create_entry({"方案编号":"EXPE-002","实验名称":"测试","对照组说明":"控","实验组说明":"试","样本总量":"200","分流比例":"5:5"})
    sg = SampleGroupService()
    members = sg.get_group_members(1)
    assert len(members) > 0, "应有分群用户"
    assert "user_id" in members[0]
    assert "metric_value" in members[0]

def test_metric_def_creation():
    """指标定义创建和口径版本。"""
    _clear_all()
    md = MetricDefService()
    entry, ms = md.create_entry({
        "指标编号": "M-001",
        "指标名称": "点击率",
        "计算公式": "clicks / impressions",
        "统计粒度": "日",
        "时间窗口": "7",
        "基准取值": "0.10",
        "精度位数": "4",
        "口径版本": "v1",
    })
    assert entry is not None, ms

def test_ab_result_analysis():
    """AB分析生成对照和实验组对比。"""
    _clear_all()
    ab = AbResultService()
    results = ab.run_analysis({"样本范围": "4"})
    assert len(results) == 4
    for r in results:
        assert "指标值" in r
        assert "对比基准" in r
        assert "差异" in r

def test_expr_factor_creation():
    """实验因子互斥规则。"""
    _clear_all()
    ef = ExprFactorService()
    entry, ms = ef.create_entry({
        "因子编号": "F-001",
        "因子名称": "按钮颜色",
        "因子层级": "UI层",
        "取值集合": "红色,蓝色",
        "分配比例": "5:5",
        "互斥规则": "颜色因子互斥",
        "关联实验": "EXPE-001",
        "生效批次": "B001",
    })
    assert entry is not None, ms

def test_anomaly_detect():
    """异常检测计算移动平均。"""
    _clear_all()
    ad = AnomalyDetectService()
    ad.create_entry({
        "规则编号": "AD-001",
        "监控指标": "点击率",
        "检测算法": "moving_average",
        "基线窗口": "7",
        "波动阈值": "0.3",
        "告警级别": "高",
        "静默时段": "0",
        "规则版本": "v1",
    })
    result = ad.run_detection(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.55])
    assert result.get("ok") or "anomaly" in str(result).lower()

def test_forecast_model():
    """预测模型简单移动平均。"""
    _clear_all()
    fc = ForecastModelService()
    fc.create_entry({
        "模型编号": "FC-001",
        "预测对象": "点击率",
        "历史窗口": "7",
        "预测周期": "3",
        "模型类型": "moving_average",
        "参数配置": "window=3",
        "训练批次": "T001",
        "评估误差": "",
    })
    result = fc.train_and_predict(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.16])
    assert result.get("ok"), result

def test_metric_alias_version():
    """口径版本注册。"""
    _clear_all()
    ma = MetricAliasService()
    entry, ms = ma.create_entry({
        "版本编号": "ALIAS-001",
        "指标编号": "M-001",
        "变更说明": "修改去重逻辑",
        "旧口径": "clicks/impressions",
        "新口径": "clicks/valid_impressions",
        "生效时间": "2026-11-16",
        "审批状态": "草稿",
        "变更人": "admin",
    })
    assert entry is not None, ms


def _make_group(base: str = "120", condition: str = "experiment_id=1 AND group='control'"):
    """造一个可计算的样本分群并返回 (service, group_id)。"""
    es = ExperimentService()
    es.create_entry({"方案编号": "EXPE-V004", "实验名称": "V004", "对照组说明": "控",
                     "实验组说明": "试", "样本总量": base, "分流比例": "5:5"})
    sg = SampleGroupService()
    gid = store.rows("sample_group")[0]["id"]
    if condition != "default":
        store.find("sample_group", gid)["筛选条件"] = condition
    return sg, gid


def test_v004_member_persistence_and_stability():
    """筛选条件版本与成员明细一起落库；连续两次计算数量稳定且版本唯一可追溯。"""
    _clear_all()
    from app.services.group_member import GroupMemberService
    sg, gid = _make_group()
    gm = GroupMemberService()
    entry, message = sg.run_action(gid, "开始计算")
    assert entry is not None, message
    assert entry["member_count"] == 60 and entry["filter_version_no"] == 1
    first, total1 = gm.list_members(gid, page=1, size=100000)
    sg.run_action(gid, "开始计算")
    second, total2 = gm.list_members(gid, page=1, size=100000)
    assert total1 == total2 == 60
    assert first == second  # 同版本成员明细稳定
    assert len({m["filter_version_id"] for m in second}) == 1
    assert all(m["condition_digest"] for m in second)
    # 成员数量可回查筛选规则版本
    versions = gm.list_versions(gid)
    assert len(versions) == 1 and versions[0]["status"] == "当前"


def test_v004_filter_revision_creates_versioned_snapshot():
    """筛选条件修订后升版，旧版本成员快照保留并可回查。"""
    _clear_all()
    from app.services.group_member import GroupMemberService
    sg, gid = _make_group()
    gm = GroupMemberService()
    sg.run_action(gid, "开始计算")
    store.find("sample_group", gid)["筛选条件"] += " AND region='north'"
    sg.run_action(gid, "开始计算")
    versions = gm.list_versions(gid)
    assert {v["version_no"] for v in versions} == {1, 2}
    old_rows, old_total = gm.list_members(gid, version_no=1, page=1, size=100000)
    new_rows, new_total = gm.list_members(gid, version_no=2, page=1, size=100000)
    assert old_total == new_total == 60
    assert old_rows[0]["condition_digest"] != new_rows[0]["condition_digest"]


def test_v004_out_of_range_values_rollback():
    """越界样本基数不保存成员，事务整体回滚且状态不推进。"""
    _clear_all()
    from app.services.group_member import GroupMemberService
    sg, _ = _make_group()
    bad, missing = sg.create_entry({"分群编号": "S-BAD", "分群名称": "坏", "筛选条件": "x=1", "样本基数": "0"})
    assert not missing
    entry, message = sg.run_action(bad["id"], "开始计算")
    assert entry is None and "回滚" in message
    assert GroupMemberService().member_count(bad["id"]) == 0
    assert store.find("sample_group", bad["id"])["status"] == "草稿"


def test_v004_resume_only_fills_missing_users():
    """中断恢复只补齐未完成成员，不重复写入同一 user_id。"""
    _clear_all()
    from app.services.group_member import GroupMemberService
    sg, gid = _make_group()
    gm = GroupMemberService()
    sg.run_action(gid, "开始计算")
    table = store.rows("sample_group_member")
    vid = store.find("sample_group", gid)["filter_version_id"]
    owned = [m for m in table if m["group_id"] == gid and m["filter_version_id"] == vid]
    for member in owned[-10:]:
        table.remove(member)
    result = gm.resume_members(gid)
    assert result["resumed"] is True
    rows, total = gm.list_members(gid, page=1, size=100000)
    user_ids = [m["user_id"] for m in rows]
    assert total == 60 and len(user_ids) == len(set(user_ids))


def test_v004_residue_rolls_back_transaction():
    """残留出现时整体回滚，成员页旧明细保持可核对。"""
    _clear_all()
    from app.services.group_member import GroupMemberService, ResidueError
    sg, gid = _make_group()
    gm = GroupMemberService()
    sg.run_action(gid, "开始计算")
    rows, total = gm.list_members(gid, page=1, size=100000)
    stale = dict(rows[0])
    stale["id"] = store.next_id("sample_group_member")
    stale["filter_version_id"] = 999999
    stale["user_id"] = rows[0]["user_id"] + "_stale"
    store.rows("sample_group_member").append(stale)
    try:
        gm.reconcile_residue(gid)
        raise AssertionError("残留核对应当抛 ResidueError 并回滚")
    except ResidueError:
        pass
    _, total_after = gm.list_members(gid, page=1, size=100000)
    assert total_after == total == 60


def test_v004_publish_ordering_conflict_resolution():
    """规则修订与成员发布同时到达时，两种发布次序的裁决符合口径。"""
    _clear_all()
    from app.services.group_member import (
        GroupMemberService, PUBLISH_NEW_FIRST, PUBLISH_OLD_FIRST,
    )
    sg, gid = _make_group()
    gm = GroupMemberService()
    sg.run_action(gid, "开始计算")
    store.find("sample_group", gid)["筛选条件"] += " AND v2=1"
    sg.run_action(gid, "开始计算")
    # 新成员优先：成员页展示新集合，旧规则快照归档
    result = gm.publish(gid, PUBLISH_NEW_FIRST)
    statuses = {v["version_no"]: v["status"] for v in result["versions"]}
    assert statuses[2] == "当前" and statuses[1] == "已归档"
    # 再造 v3，走旧规则优先：成员页保持旧集合，新集合待切换
    store.find("sample_group", gid)["筛选条件"] += " AND v3=1"
    sg.run_action(gid, "开始计算")
    result = gm.publish(gid, PUBLISH_OLD_FIRST)
    statuses = {v["version_no"]: v["status"] for v in result["versions"]}
    assert statuses[3] == "待切换" and statuses[2] == "当前"
    switched = gm.switch_pending_version(gid)
    assert switched["switched_to_version_no"] == 3


def test_v004_metric_def_validates_group_members():
    """指标定义 V004 模块读取成员明细指标值并逐条按字段口径校验。"""
    _clear_all()
    sg, gid = _make_group()
    sg.run_action(gid, "开始计算")
    md = MetricDefService()
    metric, missing = md.create_entry({
        "指标编号": "M-V004", "指标名称": "点击率", "计算公式": "clk/imp",
        "基准取值": "0", "精度位数": "4",
    })
    assert not missing
    report = md.validate_group_members(metric["id"], gid)
    assert report["ok"] and report["total"] == report["passed"] == 60
    metric["基准取值"] = "0.99"
    report = md.validate_group_members(metric["id"], gid)
    assert not report["ok"] and report["failed"] > 0


def test_v004_overview_hides_internal_tables():
    """成员/版本内部表不出现在导航概览，但样本分群成员数可见。"""
    _clear_all()
    sg, gid = _make_group()
    sg.run_action(gid, "开始计算")
    overview = store.overview()
    names = [module["name"] for module in overview["modules"]]
    assert "sample_group_member" not in names
    assert "sample_group_filter_version" not in names
    sample = next(module for module in overview["modules"] if module["name"] == "sample_group")
    assert sample["members"] == 60

