"""贴源层业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.ods_credential import FINGERPRINT_FIELDS, ods_approval
from app.store import store

MODULE = "ods"
REQUIRED_FIELDS = ["表编号", "表名称", "来源系统"]
STATUS_ORDER = ["草稿", "建表审批中", "已上线", "已下线"]
ACTION_RULES = {"提交建表审批": "建表审批中", "确认上线": "已上线", "下线表": "已下线"}
NEGATIVE_ACTIONS = ["下线表"]


class OdsService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("表编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改贴源配置；命中审批口径的字段变化时，旧准入结论立即失效。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"贴源表 {entry_id} 不存在或已归档"
        changed = [field for field in FINGERPRINT_FIELDS
                   if field in values and str(values.get(field) or "") != str(entry.get(field) or "")]
        for field, value in values.items():
            if field not in ("id", "status"):
                entry[field] = value
        if not changed:
            return entry, "贴源配置未变化，审批结论继续有效"
        affected = ods_approval.on_config_change(entry, changed)
        detail = ods_approval.facade.approval_detail(entry)
        message = f"贴源配置已更新，旧审批结论已失效（{'、'.join(changed)}）"
        if affected:
            message += f"；{len(affected)} 条明细登记标记为受影响"
        entry["审批结论"] = detail["准入状态"]
        entry["不可用原因"] = detail["不可用原因"]
        return entry, message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"贴源表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于贴源层可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        previous = entry.get("status")
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        message = f"贴源表已{action}"
        if target == "建表审批中":
            # 进入审批就按四项配置签发/复用准入凭证，后续审批详情与明细层都读这份结论。
            # 状态流转本身不算一次“重复审批诊断”，诊断只由显式 review 记录。
            review = ods_approval.review(entry, record_diagnostic=False)
            detail = ods_approval.facade.approval_detail(entry)
            entry["审批结论"] = detail["准入状态"]
            entry["准入凭证编号"] = (detail["凭证"] or {}).get("凭证编号")
            entry["配置指纹"] = review["配置指纹"]
            entry["不可用原因"] = detail["不可用原因"]
            message = f"已进入建表审批：{review['结论文本']}"
        elif target == "已上线":
            detail = ods_approval.facade.approval_detail(entry)
            entry["审批结论"] = detail["准入状态"]
            entry["准入凭证编号"] = (detail["凭证"] or {}).get("凭证编号")
            entry["不可用原因"] = detail["不可用原因"]
            if detail["可用"]:
                ods_approval.clear_downstream_flag(entry)
                message = f"贴源表已上线，准入凭证 {(detail['凭证'] or {}).get('凭证编号')} 生效"
            else:
                message = f"贴源表已上线，但准入结论不可用：{detail['不可用原因']}"
        elif target == "已下线":
            affected = ods_approval.on_table_offline(entry)
            entry["审批结论"] = "不可准入"
            entry["不可用原因"] = f"贴源表已下线，准入凭证已失效"
            entry["准入凭证编号"] = ""
            message = "贴源表已下线，准入凭证已失效"
            if affected:
                message += f"；{len(affected)} 条明细登记标记为受影响"
        else:
            if previous == "已下线":
                ods_approval.clear_downstream_flag(entry)
        return entry, message

    def approval_detail(self, entry_id: int, *, now: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, None
        return entry, ods_approval.facade.approval_detail(entry, now=now)

    def review(self, entry_id: int, *, action: str | None = None, now: str | None = None,
               period_seconds: int | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """执行一次建表审批（可重复触发）：未变且有效直接复用，变化则立即失效并重签。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, None, f"贴源表 {entry_id} 不存在或已归档"
        if action and action not in ACTION_RULES:
            return None, None, f"动作「{action}」不属于贴源层可执行范围"
        if entry.get("status") == "已下线":
            return entry, None, "贴源表已下线，不能继续申请准入凭证"
        kwargs: dict[str, Any] = {"now": now}
        if period_seconds is not None:
            kwargs["period_seconds"] = period_seconds
        review = ods_approval.review(entry, **kwargs)
        detail = ods_approval.facade.approval_detail(entry, now=now)
        entry["审批结论"] = detail["准入状态"]
        entry["准入凭证编号"] = (detail["凭证"] or {}).get("凭证编号")
        entry["配置指纹"] = review["配置指纹"]
        entry["不可用原因"] = detail["不可用原因"]
        return entry, review, review["结论文本"]

    def credentials(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, []
        return entry, ods_approval.ledger(entry.get("id"))

    def diagnostics(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, []
        table_id = str(entry.get("id"))
        return entry, [item for item in ods_approval.diagnostics() if str(item.get("table_id")) == table_id]
