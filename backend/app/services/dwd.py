"""明细层业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.ods_credential import ods_approval
from app.store import store

MODULE = "dwd"
REQUIRED_FIELDS = ["表编号", "表名称", "业务域"]
STATUS_ORDER = ["草稿", "建表审批中", "已上线", "已下线"]
ACTION_RULES = {"提交建表审批": "建表审批中", "确认上线": "已上线", "下线表": "已下线"}
NEGATIVE_ACTIONS = ["下线表"]


class DwdService:
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

    def get_entry(self, entry_id: int, *, now: str | None = None) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self.with_upstream(entry, now=now)

    def _upstream_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """按「上游贴源表」登记号定位贴源表；兼容直接引用贴源表 id 的写法。"""
        reference = str(entry.get("上游贴源表") or "").strip()
        table_id = str(entry.get("upstream_table_id") or "").strip()
        if table_id:
            found = store.find("ods", int(table_id)) if table_id.isdigit() else None
            if found is not None:
                return found
        if not reference:
            return None
        for row in store.rows("ods"):
            if reference in (str(row.get("表编号")), str(row.get("表名称"))) or reference == str(row.get("id")):
                return row
        return None

    def with_upstream(self, entry: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        """明细登记只读贴源审批结论：贴源下线或凭证失效时显示不可用原因，历史定义原样保留。"""
        upstream = self._upstream_entry(entry)
        if upstream is None:
            entry["上游准入状态"] = "不可准入"
            entry["不可用原因"] = "上游贴源表未找到对应审批结论" if entry.get("上游贴源表") else "尚未选择上游贴源表"
            entry["可用"] = False
            return entry
        view = ods_approval.facade.evaluate_upstream(upstream, now=now)
        entry["upstream_table_id"] = view["table_id"]
        entry["upstream_code"] = view["table_code"]
        entry["上游贴源表"] = view["table_code"] or entry.get("上游贴源表")
        entry["上游准入状态"] = view["准入状态"]
        entry["不可用原因"] = view["不可用原因"]
        entry["上游凭证编号"] = view["凭证编号"]
        entry["上游凭证状态"] = view["凭证状态"]
        entry["上游凭证有效期至"] = view["有效期至"]
        entry["上游配置指纹"] = view["配置指纹"]
        entry["上游配置摘要"] = view["配置摘要"]
        entry["可用"] = view["可用"]
        if not view["可用"]:
            entry["受影响"] = True
        else:
            # 贴源恢复准入后，明细登记自动恢复可用标记，历史定义始终保留。
            entry["受影响"] = False
            entry["受影响时间"] = ""
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["上游贴源表"] = values.get("上游贴源表") or entry.get("上游贴源表") or ""
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self.with_upstream(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"明细表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于明细层可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        view = self.with_upstream(entry)
        if target != "已下线" and not view["可用"]:
            # 上游审批结论不可准入时不允许明细表继续审批/上线，但历史定义不做删除。
            return None, f"明细表 {entry.get('表编号')} 上游贴源不可用：{view['不可用原因'] or '上游审批结论不可准入'}"

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self.with_upstream(entry), f"明细表已{action}"

    def upstream_options(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """明细层选择上游时的候选清单：每一项都带贴源审批结论，选中即可直接使用。"""
        options: list[dict[str, Any]] = []
        for row in store.rows("ods"):
            view = ods_approval.facade.evaluate_upstream(row, now=now)
            options.append({
                **view,
                "表名称": row.get("表名称"),
                "来源系统": row.get("来源系统"),
                "增量字段": row.get("增量字段"),
                "全量快照策略": row.get("全量快照策略"),
                "最近加载时间": row.get("最近加载时间"),
                "贴源状态": row.get("status"),
            })
        return options
