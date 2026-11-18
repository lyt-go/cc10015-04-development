"""分群成员明细领域服务（V004）。

职责：
- 读取样本分群的筛选条件并做版本化管理（条件不变复用版本，条件变化升版并保留旧快照）；
- 按既定口径（阈值、上限、范围、条件标准）确定性地计算分群成员明细；
- 筛选条件版本与成员明细在同一事务内落库，越界值不保存，异常整体回滚；
- 计算中断后只补齐未完成成员，恢复时不重复写入同一 user_id；
- 残留数据出现时按成员页旧数据核对并在事务中整体回滚；
- 裁决筛选规则修订与成员明细发布同时到达的版本冲突（两种发布次序）；
- 向指标定义 V004 模块输出可按字段口径校验的成员记录。

当前仓库是内存实现，事务用表快照模拟：提交前任何异常都会把受影响的表
恢复到进入事务时的状态，语义与数据库事务的原子回滚一致。
"""
from __future__ import annotations

import hashlib
import random
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.store import store

MEMBER_TABLE = "sample_group_member"
VERSION_TABLE = "sample_group_filter_version"

# ---- 既定口径规则：阈值、上限、范围、条件标准共同约束 ----
MEMBER_LIMIT = 500          # 成员明细硬上限，超出部分不生成、不保存
VALUE_MIN = 0.0             # 指标值允许范围下界
VALUE_MAX = 1.0             # 指标值允许范围上界
EXPERIMENT_MEAN = 0.62      # 实验组指标阈值（均值口径）
CONTROL_MEAN = 0.55         # 对照组指标阈值（均值口径）
VALUE_SIGMA = 0.08
VALUE_PRECISION = 4         # 指标值保留位数

# 版本状态：当前（成员页展示）、待切换（旧规则优先模式下等待发布）、已归档（留档核对）
VERSION_CURRENT = "当前"
VERSION_PENDING = "待切换"
VERSION_ARCHIVED = "已归档"

# 发布次序裁决模式
PUBLISH_NEW_FIRST = "新成员优先"   # 先发布新成员再归档旧规则
PUBLISH_OLD_FIRST = "旧规则优先"   # 先归档旧规则再发布新成员


class RuleViolation(Exception):
    """口径规则被破坏（阈值/上限/范围/条件标准越界），事务必须回滚。"""


class ResidueError(Exception):
    """检测到残留成员明细，按成员页旧数据核对后整体回滚。"""


def _digest(condition: str) -> str:
    return hashlib.sha256(condition.encode("utf-8")).hexdigest()[:16]


class GroupMemberService:
    # ------------------------------------------------------------------ #
    # 事务：受影响表的进入快照 + 异常整体回滚
    # ------------------------------------------------------------------ #
    @contextmanager
    def _transaction(self, *tables: str) -> Iterator[None]:
        touched = list(dict.fromkeys((MEMBER_TABLE, VERSION_TABLE, *tables)))
        snapshot = {name: [dict(row) for row in store.rows(name)] for name in touched}
        try:
            yield
        except Exception:
            for name, rows in snapshot.items():
                store.rows(name)[:] = [dict(row) for row in rows]
            raise

    # ------------------------------------------------------------------ #
    # 筛选条件版本
    # ------------------------------------------------------------------ #
    def list_versions(self, group_id: int) -> list[dict[str, Any]]:
        return [dict(v) for v in self._version_rows(group_id)]

    def _version_rows(self, group_id: int) -> list[dict[str, Any]]:
        """返回版本表中的真实行（可变），只在服务内部的写入路径使用。"""
        return [v for v in store.rows(VERSION_TABLE) if int(v.get("group_id", 0)) == group_id]

    def current_version(self, group_id: int) -> dict[str, Any] | None:
        for version in self._version_rows(group_id):
            if version.get("status") == VERSION_CURRENT:
                return dict(version)
        return None

    def ensure_version(self, group_id: int, condition: str) -> dict[str, Any]:
        """读取筛选条件并返回对应版本；条件变化时升版，旧版本保留快照。

        条件文本完全一致（digest 相同）时复用同一版本，保证连续两次计算
        同一分群得到相同的成员集合；条件修订则新建版本，旧版本不删除。
        """
        condition = (condition or "").strip()
        if not condition:
            raise RuleViolation("筛选条件为空，不满足条件标准，无法生成成员明细")
        digest = _digest(condition)
        existing = self._version_rows(group_id)
        for version in existing:
            if version.get("condition_digest") == digest:
                return dict(version)
        next_no = max((int(v.get("version_no", 0)) for v in existing), default=0) + 1
        version = {
            "id": store.next_id(VERSION_TABLE),
            "group_id": group_id,
            "version_no": next_no,
            "筛选条件": condition,
            "condition_digest": digest,
            "status": VERSION_CURRENT,
            "created_at": time.time(),
            "published_at": None,
        }
        # 同一分群只允许一个“当前”版本，其余降为归档留档
        for old in existing:
            if old.get("status") == VERSION_CURRENT:
                old["status"] = VERSION_ARCHIVED
        store.rows(VERSION_TABLE).append(version)
        store.bump_version(VERSION_TABLE)
        return dict(version)

    # ------------------------------------------------------------------ #
    # 口径判定
    # ------------------------------------------------------------------ #
    DEFAULT_BASE = 1000  # 样本基数缺失或不可解析时的兜底口径（仍受 MEMBER_LIMIT 上限约束）

    @classmethod
    def _parse_base(cls, group: dict[str, Any]) -> int:
        raw = group.get("样本基数")
        try:
            base = int(float(raw))
        except (TypeError, ValueError):
            # 非数值基数按缺失处理，走兜底口径而不是写入越界成员
            base = cls.DEFAULT_BASE
        if base <= 0:
            raise RuleViolation(f"样本基数 {base} 越界，必须为正整数")
        return min(base, MEMBER_LIMIT)  # 上限口径：超过硬上限的成员不保存

    @staticmethod
    def _metric_value(group_type: str, digest: str, index: int) -> float:
        # 确定性生成：同一筛选条件版本 + 同一序号永远得到同一指标值
        rng = random.Random(f"{digest}:{group_type}:{index}")
        mean = EXPERIMENT_MEAN if group_type == "experiment" else CONTROL_MEAN
        value = round(max(VALUE_MIN, min(VALUE_MAX, rng.gauss(mean, VALUE_SIGMA))), VALUE_PRECISION)
        if not (VALUE_MIN <= value <= VALUE_MAX):
            raise RuleViolation(f"指标值 {value} 超出范围 [{VALUE_MIN}, {VALUE_MAX}]，不得保存")
        return value

    def _build_member(
        self, group_id: int, group_type: str, index: int, version: dict[str, Any], batch_no: int,
    ) -> dict[str, Any]:
        user_id = f"U{group_id}{index:04d}"
        return {
            "id": store.next_id(MEMBER_TABLE),
            "group_id": group_id,
            "user_id": user_id,
            "group_type": group_type,
            "metric_value": self._metric_value(group_type, str(version["condition_digest"]), index),
            # 筛选条件版本与成员明细一起落库：每个用户都能追到唯一版本
            "filter_version_id": int(version["id"]),
            "filter_version_no": int(version["version_no"]),
            "condition_digest": version["condition_digest"],
            "batch_no": batch_no,
            "created_at": time.time(),
        }

    # ------------------------------------------------------------------ #
    # 成员明细读取
    # ------------------------------------------------------------------ #
    def _version_members(self, group_id: int, version_id: int) -> list[dict[str, Any]]:
        return [
            dict(m) for m in store.rows(MEMBER_TABLE)
            if int(m.get("group_id", 0)) == group_id and int(m.get("filter_version_id", 0)) == version_id
        ]

    def list_members(
        self, group_id: int, *, version_no: int | None = None, page: int = 1, size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        group = store.find("sample_group", group_id)
        if group is None:
            return [], 0
        version = self.current_version(group_id)
        if version is None:
            # 兼容历史分群：尚未计算过时按当前筛选条件即时补齐一次
            version = self.ensure_version(group_id, str(group.get("筛选条件", "")))
            self.compute_members(group_id)
            version = self.current_version(group_id)
        if version_no is not None:
            matched = [v for v in self.list_versions(group_id) if int(v.get("version_no", 0)) == version_no]
            if not matched:
                return [], 0
            target = matched[0]
        else:
            target = version
        rows = self._version_members(group_id, int(target["id"]))
        rows.sort(key=lambda m: str(m.get("user_id", "")))
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def member_count(self, group_id: int) -> int:
        version = self.current_version(group_id)
        if version is None:
            return 0
        return len(self._version_members(group_id, int(version["id"])))

    # ------------------------------------------------------------------ #
    # 计算与中断恢复
    # ------------------------------------------------------------------ #
    def compute_members(self, group_id: int) -> dict[str, Any]:
        """按当前筛选条件版本计算并持久化成员明细（事务内整体提交）。"""
        group = store.find("sample_group", group_id)
        if group is None:
            raise RuleViolation(f"样本分群 {group_id} 不存在或已归档")
        with self._transaction("sample_group"):
            version = self.ensure_version(group_id, str(group.get("筛选条件", "")))
            target_count = self._parse_base(group)
            group_type = group.get("group_type", "control")
            batch_no = int(group.get("calc_batch_no", 0) or 0) + 1

            existing = self._version_members(group_id, int(version["id"]))
            done_users = {str(m.get("user_id")) for m in existing}
            # 中断恢复：只补齐未完成成员，已存在的 user_id 绝不重复写入
            members = [dict(m) for m in existing]
            for index in range(target_count):
                user_id = f"U{group_id}{index:04d}"
                if user_id in done_users:
                    continue
                members.append(self._build_member(group_id, group_type, index, version, batch_no))

            self._guard_no_residue(group_id, version, members)
            store.rows(MEMBER_TABLE).extend(
                m for m in members if m["user_id"] not in done_users
            )
            interrupted = len(members) < target_count
            group["filter_version_id"] = int(version["id"])
            group["filter_version_no"] = int(version["version_no"])
            group["condition_digest"] = version["condition_digest"]
            group["member_count"] = len(members)
            group["calc_batch_no"] = batch_no
            group["calc_status"] = "中断" if interrupted else "完整"
            store.bump_version(MEMBER_TABLE)
            store.bump_version("sample_group")
            return {
                "group_id": group_id,
                "filter_version_id": int(version["id"]),
                "version_no": int(version["version_no"]),
                "member_count": len(members),
                "target_count": target_count,
                "resumed": bool(done_users),
                "calc_status": group["calc_status"],
            }

    def resume_members(self, group_id: int) -> dict[str, Any]:
        """计算中断后的恢复入口：仅补齐未完成成员，不重复写同一用户记录。"""
        group = store.find("sample_group", group_id)
        if group is None:
            raise RuleViolation(f"样本分群 {group_id} 不存在或已归档")
        return self.compute_members(group_id)

    def _guard_no_residue(
        self, group_id: int, version: dict[str, Any], pending: list[dict[str, Any]],
    ) -> None:
        """残留核对：同一版本内 user_id 唯一、版本可追溯、指标值不越界。

        发现残留时抛 ResidueError，由事务把成员表与分群表整体回滚到
        成员页正在展示的旧数据，保证旧核对结果不被污染。
        """
        seen: set[str] = set()
        version_ids = {int(v["id"]) for v in self.list_versions(group_id)}
        for member in pending:
            user_id = str(member.get("user_id", ""))
            if not user_id or user_id in seen:
                raise ResidueError(f"检测到重复/空 user_id 残留：{user_id or '<empty>'}")
            seen.add(user_id)
            if int(member.get("filter_version_id", 0)) not in version_ids:
                raise ResidueError(f"成员 {user_id} 追溯不到有效筛选条件版本")
            if member.get("condition_digest") != version["condition_digest"]:
                raise ResidueError(f"成员 {user_id} 的条件摘要与版本不一致")
            value = member.get("metric_value")
            if not isinstance(value, (int, float)) or not (VALUE_MIN <= float(value) <= VALUE_MAX):
                raise ResidueError(f"成员 {user_id} 指标值 {value} 越界，属于残留数据")
        # 同时核对历史批次里是否已存在指向未知版本的残留
        for stale in store.rows(MEMBER_TABLE):
            if int(stale.get("group_id", 0)) != group_id:
                continue
            if int(stale.get("filter_version_id", 0)) not in version_ids:
                raise ResidueError(
                    f"历史批次残留：成员 {stale.get('user_id')} 引用了不存在的版本 "
                    f"{stale.get('filter_version_id')}")

    def reconcile_residue(self, group_id: int) -> dict[str, Any]:
        """显式残留核对：发现残留时事务整体回滚，成员页继续展示旧明细。"""
        group = store.find("sample_group", group_id)
        if group is None:
            raise RuleViolation(f"样本分群 {group_id} 不存在或已归档")
        with self._transaction("sample_group"):
            versions = self.list_versions(group_id)
            version_ids = {int(v["id"]) for v in versions}
            stale = [
                m for m in store.rows(MEMBER_TABLE)
                if int(m.get("group_id", 0)) == group_id
                and int(m.get("filter_version_id", 0)) not in version_ids
            ]
            duplicated: list[str] = []
            seen: set[str] = set()
            for member in store.rows(MEMBER_TABLE):
                if int(member.get("group_id", 0)) != group_id:
                    continue
                key = f"{member.get('filter_version_id')}::{member.get('user_id')}"
                if key in seen:
                    duplicated.append(str(member.get("user_id")))
                seen.add(key)
            if not stale and not duplicated:
                return {"group_id": group_id, "ok": True, "message": "未发现残留，成员明细与版本一致"}
            # 有残留：整体回滚本次核对产生的任何改动（快照恢复），保留旧明细
            raise ResidueError(
                f"发现残留 {len(stale)} 条、重复 {len(duplicated)} 条，事务已整体回滚")

    # ------------------------------------------------------------------ #
    # 版本冲突裁决：规则修订与成员发布同时到达
    # ------------------------------------------------------------------ #
    def publish(self, group_id: int, mode: str = PUBLISH_NEW_FIRST) -> dict[str, Any]:
        """裁决筛选规则修订与成员明细发布同时到达的版本冲突。

        - 新成员优先：先发布新成员再归档旧规则。成员页立即展示新规则命中
          集合，旧规则版本保留快照归档，可随时回查。
        - 旧规则优先：先归档旧规则再发布新成员。成员页保持旧规则历史集合，
          新版本列为“待切换”，需要再次确认后才切到新集合。
        """
        group = store.find("sample_group", group_id)
        if group is None:
            raise RuleViolation(f"样本分群 {group_id} 不存在或已归档")
        if mode not in (PUBLISH_NEW_FIRST, PUBLISH_OLD_FIRST):
            raise RuleViolation(f"发布次序「{mode}」不被支持")
        with self._transaction("sample_group"):
            versions = self._version_rows(group_id)
            if not versions:
                self.ensure_version(group_id, str(group.get("筛选条件", "")))
                versions = self._version_rows(group_id)
            latest = max(versions, key=lambda v: int(v["version_no"]))
            now = time.time()
            if mode == PUBLISH_NEW_FIRST:
                for version in versions:
                    if int(version["id"]) == int(latest["id"]):
                        version["status"] = VERSION_CURRENT
                        version["published_at"] = now
                    else:
                        version["status"] = VERSION_ARCHIVED  # 旧规则快照留档
                self._point_group_at_version(group, latest)
                page_shows = "新规则命中集合（旧规则快照已归档）"
            else:
                for version in versions:
                    if int(version["id"]) == int(latest["id"]):
                        version["status"] = VERSION_PENDING  # 新集合待切换
                    else:
                        version["status"] = VERSION_ARCHIVED
                old_current = max(
                    (v for v in versions if int(v["id"]) != int(latest["id"])),
                    key=lambda v: int(v["version_no"]), default=None,
                )
                if old_current is not None:
                    old_current["status"] = VERSION_CURRENT  # 成员页继续展示旧历史集合
                    self._point_group_at_version(group, old_current)
                page_shows = "旧规则历史集合（新集合待切换）"
            store.bump_version(VERSION_TABLE)
            store.bump_version("sample_group")
            return {
                "group_id": group_id,
                "mode": mode,
                "latest_version_no": int(latest["version_no"]),
                "member_page": page_shows,
                "versions": self.list_versions(group_id),
            }

    def switch_pending_version(self, group_id: int) -> dict[str, Any]:
        """旧规则优先模式下，确认把待切换的新集合切换为成员页当前集合。"""
        group = store.find("sample_group", group_id)
        if group is None:
            raise RuleViolation(f"样本分群 {group_id} 不存在或已归档")
        with self._transaction("sample_group"):
            live_versions = self._version_rows(group_id)
            pending = [v for v in live_versions if v.get("status") == VERSION_PENDING]
            if not pending:
                raise RuleViolation("没有待切换的筛选条件版本")
            target = max(pending, key=lambda v: int(v["version_no"]))
            for version in live_versions:
                if int(version["id"]) == int(target["id"]):
                    version["status"] = VERSION_CURRENT
                    version["published_at"] = time.time()
                elif version.get("status") == VERSION_CURRENT:
                    version["status"] = VERSION_ARCHIVED
            self._point_group_at_version(group, target)
            store.bump_version(VERSION_TABLE)
            store.bump_version("sample_group")
            return {"group_id": group_id, "switched_to_version_no": int(target["version_no"])}

    def _point_group_at_version(self, group: dict[str, Any], version: dict[str, Any]) -> None:
        members = self._version_members(int(group["id"]), int(version["id"]))
        group["filter_version_id"] = int(version["id"])
        group["filter_version_no"] = int(version["version_no"])
        group["condition_digest"] = version["condition_digest"]
        group["member_count"] = len(members)

    # ------------------------------------------------------------------ #
    # V004 跨模块：供指标定义模块按字段口径逐条校验成员记录
    # ------------------------------------------------------------------ #
    def validate_for_metric(
        self, group_id: int, *, precision: int = VALUE_PRECISION,
        value_min: float = VALUE_MIN, value_max: float = VALUE_MAX,
    ) -> dict[str, Any]:
        """指标定义 V004 模块调用：读取成员明细中的指标值并逐条口径校验。"""
        version = self.current_version(group_id)
        if version is None:
            return {"ok": False, "group_id": group_id, "total": 0, "passed": 0,
                    "failed": 0, "issues": ["分群尚未生成成员明细，无法按口径校验"]}
        members = self._version_members(group_id, int(version["id"]))
        issues: list[dict[str, Any]] = []
        version_ids = {int(v["id"]) for v in self.list_versions(group_id)}
        for member in members:
            user_id = str(member.get("user_id", ""))
            value = member.get("metric_value")
            if not isinstance(value, (int, float)):
                issues.append({"user_id": user_id, "reason": "指标值缺失或非数值"})
                continue
            if not (value_min <= float(value) <= value_max):
                issues.append({"user_id": user_id, "reason": f"指标值 {value} 超出口径范围"})
            if round(float(value), precision) != float(value):
                issues.append({"user_id": user_id, "reason": f"指标值精度不符合 {precision} 位口径"})
            if int(member.get("filter_version_id", 0)) not in version_ids:
                issues.append({"user_id": user_id, "reason": "追溯不到唯一筛选条件版本"})
        return {
            "ok": not issues,
            "group_id": group_id,
            "filter_version_no": int(version["version_no"]),
            "total": len(members),
            "passed": len(members) - len(issues),
            "failed": len(issues),
            "issues": issues[:20],
        }
