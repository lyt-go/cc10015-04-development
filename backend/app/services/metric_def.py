"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.group_member import GroupMemberService
from app.store import store

MODULE = "metric_def"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def __init__(self) -> None:
        # V004 跨模块联动：指标定义模块读取分群成员明细做口径校验
        self.group_members = GroupMemberService()
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    # ------------------------------------------------------------------ #
    # V004 跨模块联动：读取分群成员明细中的指标值，按字段口径逐条校验
    # ------------------------------------------------------------------ #
    def validate_group_members(self, entry_id: int, group_id: int) -> dict[str, Any]:
        """按指标定义的字段口径（精度位数、基准取值）校验每个分群成员记录。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"指标定义 {entry_id} 不存在或已归档"}
        try:
            precision = int(float(entry.get("精度位数") or 4))
        except (TypeError, ValueError):
            precision = 4
        precision = max(0, min(precision, 10))
        try:
            baseline = float(entry.get("基准取值") or 0.0)
        except (TypeError, ValueError):
            baseline = None
        report = self.group_members.validate_for_metric(group_id, precision=precision)
        # 在成员明细通用校验之上，叠加指标定义自身的基准取值口径
        version = self.group_members.current_version(group_id)
        if version is not None and baseline is not None:
            members, _ = self.group_members.list_members(group_id, page=1, size=1000000)
            baseline_issues: list[dict[str, Any]] = []
            for member in members:
                value = member.get("metric_value")
                if isinstance(value, (int, float)) and float(value) < baseline:
                    baseline_issues.append({
                        "user_id": member.get("user_id"),
                        "reason": f"指标值 {value} 低于基准取值 {baseline}",
                    })
            report["issues"].extend(baseline_issues)
            report["failed"] = len(report["issues"])
            report["passed"] = max(0, int(report.get("total", 0)) - report["failed"])
        report["ok"] = not report.get("issues")
        report["metric_def_id"] = entry_id
        report["metric_code"] = entry.get("指标编号")
        report["precision"] = precision
        report["baseline"] = baseline
        return report
