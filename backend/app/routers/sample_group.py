"""样本分群接口：维护样本分群，并提供成员明细、版本与恢复/发布裁决能力。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.group_member import (
    PUBLISH_NEW_FIRST,
    PUBLISH_OLD_FIRST,
    GroupMemberService,
    ResidueError,
    RuleViolation,
)
from app.services.sample_group import SampleGroupService

router = APIRouter(prefix="/api/sample_group", tags=["样本分群"])

service = SampleGroupService()
member_service = GroupMemberService()

LIST_FIELDS = ["分群编号", "分群名称", "筛选条件", "样本基数", "分群日期", "更新方式", "有效天数", "负责人"]
STATUSES = ["草稿", "计算中", "已就绪", "已过期"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按分群编号检索"),
    status: str | None = Query(default=None, description="草稿、计算中、已就绪、已过期"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、分群编号关键字与状态过滤样本分群列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status", "member_count", "filter_version_no"])
    for item in items:
        row = []
        values = [
            item.get("id"),
            *(item.get(field, "") for field in LIST_FIELDS),
            item.get("status", ""),
            item.get("member_count", 0),
            item.get("filter_version_no", ""),
        ]
        for value in values:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sample_group.csv"'})


@router.get("/{entry_id}/members", response_model=PageResult[dict])
def list_members(
    entry_id: int,
    version_no: int | None = Query(default=None, description="按筛选条件版本号回查成员明细"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=500),
) -> PageResult[dict]:
    """读取分群成员明细（成员页抽屉与导航页共用同一份已落库业务数据）。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"样本分群 {entry_id} 不存在或已归档")
    try:
        items, total = member_service.list_members(
            entry_id, version_no=version_no, page=page, size=size)
    except (RuleViolation, ResidueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}/versions")
def list_versions(entry_id: int) -> dict[str, object]:
    """回查筛选条件版本链：成员数量可追溯到每个版本及其归档状态。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"样本分群 {entry_id} 不存在或已归档")
    versions = member_service.list_versions(entry_id)
    for version in versions:
        version["member_count"] = len(member_service._version_members(entry_id, int(version["id"])))
    return {"items": versions, "total": len(versions)}


@router.post("/{entry_id}/resume", response_model=ActionResult)
def resume_members(entry_id: int) -> ActionResult:
    """计算中断后恢复：只补齐未完成成员，不重复写入同一 user_id。"""
    try:
        result = member_service.resume_members(entry_id)
    except ResidueError as exc:
        return ActionResult(ok=False, message=f"恢复时发现残留，事务已整体回滚：{exc}")
    except RuleViolation as exc:
        return ActionResult(ok=False, message=f"恢复未通过口径判定：{exc}")
    return ActionResult(ok=True, message="中断成员已补齐，未重复写入用户记录", entry=result)


@router.post("/{entry_id}/reconcile", response_model=ActionResult)
def reconcile_residue(entry_id: int) -> ActionResult:
    """残留核对：按成员页旧数据核对，发现残留时事务整体回滚（V004 留档核对）。"""
    try:
        result = member_service.reconcile_residue(entry_id)
    except ResidueError as exc:
        return ActionResult(ok=False, message=str(exc))
    except RuleViolation as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.post("/{entry_id}/publish", response_model=ActionResult)
def publish_members(entry_id: int, payload: ActionPayload) -> ActionResult:
    """版本冲突裁决：新成员优先（先发布新成员再归档旧规则）或旧规则优先。

    动作名通过 payload.action 传入：新成员优先 / 旧规则优先。
    """
    mode = PUBLISH_OLD_FIRST if payload.action.strip() == PUBLISH_OLD_FIRST else PUBLISH_NEW_FIRST
    try:
        result = member_service.publish(entry_id, mode)
    except (RuleViolation, ResidueError) as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"已按「{mode}」裁决发布次序", entry=result)


@router.post("/{entry_id}/switch-version", response_model=ActionResult)
def switch_version(entry_id: int) -> ActionResult:
    """旧规则优先模式下，确认切换到待切换的新成员集合。"""
    try:
        result = member_service.switch_pending_version(entry_id)
    except (RuleViolation, ResidueError) as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="已切换到新筛选条件版本的成员集合", entry=result)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条样本分群明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"样本分群 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条样本分群，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="样本分群已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条样本分群执行开始计算、确认就绪、标记过期；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
