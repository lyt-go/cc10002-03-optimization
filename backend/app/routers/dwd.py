"""明细层接口：维护明细表，覆盖提交建表审批、确认上线、下线表等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dwd import DwdService

router = APIRouter(prefix="/api/dwd", tags=["明细层"])

service = DwdService()

LIST_FIELDS = ["表编号", "表名称", "业务域", "主键列", "度量列", "时间分区列", "上游贴源表", "刷新水位"]
STATUSES = ["草稿", "建表审批中", "已上线", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、建表审批中、已上线、已下线"),
    page: int = 1,
    size: int = 20,
    now: str | None = Query(default=None, description="用于复算上游凭证有效期，默认取服务器当前时间"),
) -> PageResult[dict]:
    """按表编号与状态过滤明细层列表；每行附带上游贴源表的准入结论。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=[service.with_upstream(item, now=now) for item in items], total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出明细层清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dwd", "total": total, "items": [service.with_upstream(item) for item in items]}


@router.get("/upstream-options", response_model=dict)
def list_upstream_options(now: str | None = Query(default=None, description="用于复算上游凭证有效期")) -> dict[str, Any]:
    """明细层选择上游的候选清单：每一项都来自贴源审批结论，选中后与审批详情一致。"""
    items = service.upstream_options(now=now)
    return {"items": items, "total": len(items)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int, now: str | None = Query(default=None, description="用于复算上游凭证有效期")) -> dict:
    """读取单条明细表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id, now=now)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"明细表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条明细表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="明细表已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改明细表定义；仅允许在未上线前修改，历史定义不清除。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"明细表 {entry_id} 不存在或已归档")
    for field, value in payload.values.items():
        if field in ("id", "status"):
            continue
        entry[field] = value
    return ActionResult(ok=True, message="明细表定义已更新", entry=service.with_upstream(entry))


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条明细表执行提交建表审批、确认上线、下线表；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
