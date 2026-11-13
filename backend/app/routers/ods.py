"""贴源层接口：维护贴源表，覆盖提交建表审批、确认上线、下线表等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.ods import OdsService

router = APIRouter(prefix="/api/ods", tags=["贴源层"])

service = OdsService()

LIST_FIELDS = ["表编号", "表名称", "来源系统", "原始字段数", "增量字段", "全量快照策略", "每日增量行", "最近加载时间"]
STATUSES = ["草稿", "建表审批中", "已上线", "已下线"]


def _with_conclusion(items: list[dict[str, Any]], now: str | None = None) -> list[dict[str, Any]]:
    """列表里顺带给出审批结论，前端不必再逐条发起一次判断。"""
    for item in items:
        detail = service.approval_detail(int(item.get("id", 0)), now=now)[1]
        if detail is None:
            continue
        item["审批结论"] = detail["准入状态"]
        item["审批凭证编号"] = (detail["凭证"] or {}).get("凭证编号")
        item["配置指纹"] = detail["配置指纹"]
        item["不可用原因"] = detail["不可用原因"]
        item["凭证有效期至"] = (detail["凭证"] or {}).get("有效期至")
    return items


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、建表审批中、已上线、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表编号与状态过滤贴源层列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=_with_conclusion(items), total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出贴源层清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "ods", "total": total, "items": _with_conclusion(items)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条贴源表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在或已归档")
    return _with_conclusion([entry])[0]


@router.get("/{entry_id}/approval", response_model=dict)
def get_approval_detail(entry_id: int, now: str | None = Query(default=None, description="用于复算有效期，默认取服务器当前时间")) -> dict:
    """贴源表审批详情：返回当前准入凭证、失效原因与诊断记录；明细层读的是同一份结论。"""
    entry, detail = service.approval_detail(entry_id, now=now)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在或已归档")
    return detail or {}


@router.get("/{entry_id}/credentials", response_model=dict)
def list_credentials(entry_id: int) -> dict[str, Any]:
    """贴源表签发过的准入凭证台账（含历史失效版本），按审核单落库顺序返回。"""
    entry, items = service.credentials(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在或已归档")
    return {"table": entry, "items": items, "total": len(items)}


@router.get("/{entry_id}/diagnostics", response_model=dict)
def list_diagnostics(entry_id: int) -> dict[str, Any]:
    """贴源表审批诊断记录：周期内重复审批只保留一条。"""
    entry, items = service.diagnostics(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在或已归档")
    return {"table": entry, "items": items, "total": len(items)}


@router.post("/{entry_id}/review", response_model=ActionResult)
def review_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """申请建表审批凭证：配置未变且有效期内直接复用，任一字段变化立即失效并重签。"""
    action = str(payload.values.get("action") or "提交建表审批").strip()
    now = payload.values.get("now")
    period_seconds = payload.values.get("period_seconds")
    entry, review, message = service.review(
        entry_id,
        action=action,
        now=str(now) if now else None,
        period_seconds=int(period_seconds) if period_seconds else None,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    if review is None:
        # 已下线的贴源表不允许再签发/复用凭证，明确告知原因。
        return ActionResult(ok=False, message=message, entry=entry)
    return ActionResult(ok=True, message=message, entry=review)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条贴源表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="贴源表已登记", entry=_with_conclusion([entry])[0])


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改贴源配置；命中来源系统、增量字段、快照策略或最近加载时间时，旧审批结论立即失效。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=_with_conclusion([entry])[0])


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条贴源表执行提交建表审批、确认上线、下线表；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
