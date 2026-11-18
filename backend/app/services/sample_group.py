"""样本分群业务规则：维护分群定义，并把筛选条件版本与成员明细一起持久化。"""
from __future__ import annotations

import time
from typing import Any

from app.services.group_member import (
    GroupMemberService,
    ResidueError,
    RuleViolation,
)
from app.store import store

MODULE = "sample_group"
LIST_FIELDS = ["分群编号", "分群名称", "筛选条件", "样本基数", "分群日期", "更新方式", "有效天数", "负责人"]
REQUIRED_FIELDS = ["分群编号", "分群名称", "筛选条件"]
STATUS_ORDER = ["草稿", "计算中", "已就绪", "已过期"]
ACTION_RULES = {"开始计算": "计算中", "确认就绪": "已就绪", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []


class SampleGroupService:
    def __init__(self) -> None:
        self.members = GroupMemberService()

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
            rows = [row for row in rows if keyword.casefold() in str(row.get("分群编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 导航页与成员抽屉读取同一业务数据：列表项直接带上已落库的成员数与版本
        decorated = [self._with_member_summary(dict(row)) for row in rows]
        total = len(decorated)
        start = (page - 1) * size
        return decorated[start:start + size], total

    def _with_member_summary(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry["member_count"] = self.members.member_count(int(entry["id"]))
        version = self.members.current_version(int(entry["id"]))
        entry["filter_version_no"] = version["version_no"] if version else None
        entry["filter_version_status"] = version["status"] if version else None
        return entry

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._with_member_summary(dict(entry))

    def get_group_members(self, group_id: int) -> list[dict[str, Any]]:
        """读取已持久化的分群成员明细供 AB 分析消费；未计算过则按当前筛选条件补齐。"""
        if store.find(MODULE, group_id) is None:
            return []
        try:
            members, _ = self.members.list_members(group_id, page=1, size=1000000)
        except (RuleViolation, ResidueError):
            return []
        return members

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["created_at"] = time.time()
        store.rows(MODULE).append(entry)
        store.bump_version(MODULE)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"样本分群 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于样本分群可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 开始计算：筛选条件版本与成员明细在同一事务内一起落库；
        # 口径越界或残留都会整体回滚，状态不会被推进到“计算中”。
        if action == "开始计算":
            try:
                self.members.compute_members(entry_id)
            except ResidueError as exc:
                return None, f"检测到残留成员明细，事务已整体回滚：{exc}"
            except RuleViolation as exc:
                return None, f"成员明细未通过口径判定，已回滚：{exc}"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return self._with_member_summary(dict(entry)), f"样本分群已{action}"
