"""贴源建表审批结论：准入凭证的签发、复用、失效与争用裁决。

贴源表进入建表审批时，按「来源系统 + 增量字段 + 全量快照策略 + 最近加载时间」签发一张
准入凭证（凭证正文就是这四个字段的配置指纹）。贴源审批详情与明细层选择上游都只读这一份
结论，不再各自重新判断：

- 配置未变且在有效期内：直接复用，不重新计算、不重复签发；
- 任一字段变化：旧凭证据此立即失效，必须按当前配置重新签发；
- 贴源表下线：凭证作废，明细层登记显示不可用原因，但历史定义保留；
- 配置争用：按审核单落库顺序（凭证 id 升序）裁决当前版本，只允许一张凭证生效；
- 同一配置在一个诊断周期内重复审批，只保留一条诊断记录。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store

CREDENTIAL_MODULE = "ods_credential"
DIAGNOSTIC_MODULE = "ods_diagnostic"

# 凭证只认这四项配置；任何一项发生变化都会让旧结论立刻失效。
FINGERPRINT_FIELDS = ["来源系统", "增量字段", "全量快照策略", "最近加载时间"]
FINGERPRINT_LABELS = {
    "来源系统": "来源系统",
    "增量字段": "增量字段",
    "全量快照策略": "快照策略",
    "最近加载时间": "最近加载时间",
}
CREDENTIAL_STATE_VALID = "有效"
CREDENTIAL_STATE_INVALID = "已失效"
READY_STATUS = "已上线"
OFFLINE_STATUS = "已下线"
# 同一配置在一个周期内重复审批时，诊断记录只保留一条。
DIAGNOSTIC_PERIOD_SECONDS = 24 * 3600


def parse_moment(raw: Any) -> datetime | None:
    """统一时刻解析：示例数据只给到日期时按当天 00:00:00 处理。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def moment_key(raw: Any) -> str:
    value = parse_moment(raw)
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else str(raw or "").strip()


class OdsApprovalService:
    """贴源审批结论的唯一读写入口；明细层通过 `OdsApprovalFacade` 读取同一份结论。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._facade = OdsApprovalFacade(self)

    @property
    def facade(self) -> "OdsApprovalFacade":
        return self._facade

    # ---- 台账 -------------------------------------------------------------

    def _credentials(self) -> list[dict[str, Any]]:
        return store.rows(CREDENTIAL_MODULE)

    def _diagnostics(self) -> list[dict[str, Any]]:
        return store.rows(DIAGNOSTIC_MODULE)

    def credentials(self) -> list[dict[str, Any]]:
        return list(self._credentials())

    def diagnostics(self) -> list[dict[str, Any]]:
        return list(self._diagnostics())

    def _next_id(self, deck: list[dict[str, Any]]) -> int:
        return max((int(item.get("id", 0)) for item in deck), default=0) + 1

    @staticmethod
    def fingerprint(values: dict[str, Any]) -> str:
        payload = json.dumps([values.get(field) for field in FINGERPRINT_FIELDS], ensure_ascii=False)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def summary(values: dict[str, Any]) -> str:
        return "、".join(f"{FINGERPRINT_LABELS[field]}={values[field] or '未填写'}" for field in FINGERPRINT_FIELDS)

    @staticmethod
    def _changes(credential: dict[str, Any], values: dict[str, Any]) -> str:
        fields = [FINGERPRINT_LABELS[field] for field in FINGERPRINT_FIELDS
                  if credential.get(field) != values.get(field)]
        return "、".join(fields) or "配置"

    @staticmethod
    def evaluation(values: dict[str, Any], credential: dict[str, Any] | None,
                   table_status: str, checked_at: str, table_code: str = "") -> tuple[bool, str]:
        """准入结论的唯一判定口径：审批详情、明细层上游准入都调用这里。"""
        label = f"贴源表 {table_code}" if table_code else "贴源表"
        if table_status == OFFLINE_STATUS:
            # 下线是终态原因，不再叠加凭证层的失效说明，避免同一句话重复两遍。
            return False, f"{label} 已下线，上游不可用"
        reasons: list[str] = []
        if credential is None:
            reasons.append("尚未签发准入凭证")
        elif credential.get("凭证状态") != CREDENTIAL_STATE_VALID:
            reasons.append(credential.get("失效原因") or "准入凭证已失效")
        elif credential.get("fingerprint") != OdsApprovalService.fingerprint(values):
            reasons.append("准入配置已变化，旧结论失效")
        elif _deadline_reached(credential.get("有效期至"), checked_at):
            reasons.append(f"准入凭证已超出有效期（截至 {credential.get('有效期至')}）")
        elif table_status != READY_STATUS:
            reasons.append(f"{label}状态为{table_status}，尚未完成上线审批")
        return not reasons, "；".join(reasons)

    def active_credential(self, table_id: Any) -> dict[str, Any] | None:
        """当前生效凭证：按审核单落库顺序只认最后一版（id 最大）。"""
        deck = [item for item in self._credentials()
                if str(item.get("table_id")) == str(table_id) and item.get("current")
                and item.get("凭证状态") == CREDENTIAL_STATE_VALID]
        return max(deck, key=lambda item: int(item.get("id", 0)), default=None)

    def latest_credential(self, table_id: Any) -> dict[str, Any] | None:
        """最近一版凭证（含已失效的），用于说明「为什么不可用」。"""
        deck = [item for item in self._credentials() if str(item.get("table_id")) == str(table_id)]
        return max(deck, key=lambda item: int(item.get("id", 0)), default=None)

    def ledger(self, table_id: Any) -> list[dict[str, Any]]:
        """按审核单落库顺序返回某贴源表的凭证台账（含历史失效版本）。"""
        deck = [item for item in self._credentials() if str(item.get("table_id")) == str(table_id)]
        return sorted(deck, key=lambda item: int(item.get("id", 0)))

    def _invalidate(self, credential: dict[str, Any], reason: str, at: str) -> dict[str, Any]:
        credential["凭证状态"] = CREDENTIAL_STATE_INVALID
        credential["准入结论"] = "不可准入"
        credential["state"] = CREDENTIAL_STATE_INVALID
        credential["current"] = False
        credential["失效原因"] = reason
        credential["invalid_reason"] = reason
        credential["失效时间"] = at
        credential["invalidated_at"] = at
        return credential

    def invalidate_for_table(self, table_id: Any, reason: str, *, now: str | None = None) -> list[dict[str, Any]]:
        """立即作废某贴源表的全部当前凭证（配置变更、下线都走这里）。

        返回作废后的快照，保留在台账里作为历史版本。
        """
        with self._lock:
            at = moment_key(now) if now else self._clock()
            revoked: list[dict[str, Any]] = []
            for item in self._credentials():
                if str(item.get("table_id")) != str(table_id) or not item.get("current"):
                    continue
                snapshot = self._invalidate(dict(item), reason, at)
                revoked.append(snapshot)
                item.update({key: value for key, value in snapshot.items() if key in item})
            return revoked

    # ---- 审批 -------------------------------------------------------------

    def _issue(self, entry: dict[str, Any], *, checked_at: str, period_seconds: int,
               operator: str, reason: str = "") -> dict[str, Any]:
        """按当前四项配置落一张新凭证；同一贴源表同时只会有一张 current。"""
        with self._lock:
            values = {field: entry.get(field) for field in FINGERPRINT_FIELDS}
            fingerprint = self.fingerprint(values)
            table_id = str(entry.get("id"))
            expires_at = parse_moment(checked_at) + timedelta(seconds=period_seconds)
            issued_id = self._next_id(self._credentials())
            # 配置争用：后落库的审核单让前一版让位，保证台账里永远只有一张 current。
            for item in self._credentials():
                if str(item.get("table_id")) == table_id and item.get("current"):
                    self._invalidate(item, f"配置争用：审核单 #{issued_id} 落库的新版本优先", checked_at)
            credential = {"id": issued_id}
            credential.update({
                "凭证编号": f"ODSC-{issued_id:06d}",
                "准入结论": "可准入",
                "凭证状态": CREDENTIAL_STATE_VALID,
                "来源系统": values["来源系统"],
                "增量字段": values["增量字段"],
                "全量快照策略": values["全量快照策略"],
                "最近加载时间": values["最近加载时间"],
                "配置指纹": fingerprint,
                "配置摘要": self.summary(values),
                "签发时间": checked_at,
                "有效期至": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                "签发原因": reason or "按当前配置签发准入凭证",
                "失效原因": "",
                "失效时间": "",
                "审核来源": "建表审批",
                "table_id": table_id,
                "table_code": entry.get("表编号"),
                "table_status": entry.get("status"),
                "fingerprint": fingerprint,
                "state": CREDENTIAL_STATE_VALID,
                "current": True,
                "issued_at": checked_at,
                "valid_until": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                "invalid_reason": "",
                "invalidated_at": "",
                "operator": operator,
            })
            self._credentials().append(credential)
            return credential

    def review(
        self,
        entry: dict[str, Any],
        *,
        period_seconds: int = DIAGNOSTIC_PERIOD_SECONDS,
        now: str | None = None,
        operator: str = "值班管理员",
        record_diagnostic: bool = True,
    ) -> dict[str, Any]:
        """申请一张审批凭证：未变且有效则复用；配置变化时旧结论立即失效并重签。"""
        with self._lock:
            checked_at = moment_key(now) if now else self._clock()
            values = {field: entry.get(field) for field in FINGERPRINT_FIELDS}
            fingerprint = self.fingerprint(values)
            table_id = str(entry.get("id"))
            existing = self.active_credential(table_id)

            reason = ""
            reused = False
            if existing is not None and existing.get("fingerprint") == fingerprint:
                if not _deadline_reached(existing.get("valid_until"), checked_at):
                    reused = True
                    reason = "配置未变化且在有效期内，直接复用既有结论"
                else:
                    reason = f"配置未变化但已超出有效期（原有效期至 {existing.get('valid_until')}）"
            elif existing is not None:
                reason = f"配置已变化（{self._changes(existing, values)}），旧结论已失效"

            issued: dict[str, Any] | None = existing if reused else None
            revoked: list[dict[str, Any]] = []
            if reused:
                existing["最近复用时间"] = checked_at
                existing["复用次数"] = int(existing.get("复用次数", 0)) + 1
            else:
                # 需要重签时先作废旧版本，并记录本次失效原因。
                revoked = self.invalidate_for_table(table_id, reason or "按当前配置重新签发准入凭证",
                                                    now=checked_at)
                if entry.get("status") != OFFLINE_STATUS:
                    issued = self._issue(entry, checked_at=checked_at, period_seconds=period_seconds,
                                         operator=operator, reason=reason)
                    reason = reason or "按当前配置签发准入凭证"

            if entry.get("status") == OFFLINE_STATUS:
                reason = f"贴源表已{OFFLINE_STATUS}，不满足准入条件"
                issued = None

            usable, evaluation_reason = self.evaluation(
                values, issued, str(entry.get("status")), checked_at, str(entry.get("表编号") or ""))
            result = "复用" if reused and usable else ("可准入" if usable else "不可准入")
            if not usable:
                reason = evaluation_reason

            payload = {
                "table_id": table_id,
                "table_code": entry.get("表编号"),
                "fingerprint": fingerprint,
                "summary": self.summary(values),
                "credential_id": issued.get("id") if issued else None,
                "reuse_result": result,
                "checked_at": checked_at,
                "message": reason,
            }
            diagnostic = (self._append_diagnostic(payload, period_seconds, operator)
                          if record_diagnostic else None)

            return {
                "复用结论": result,
                "结论文本": reason,
                "是否复用": reused,
                "配置指纹": fingerprint,
                "配置摘要": self.summary(values),
                "凭证": issued,
                "失效凭证": [] if reused else revoked,
                "诊断记录": diagnostic,
            }

    def _append_diagnostic(self, payload: dict[str, Any], period_seconds: int,
                           operator: str) -> dict[str, Any]:
        """周期内同一配置只留一条诊断；重复命中时累加复用次数而不是新增记录。

        周期窗口锚定在首次诊断时间上，因此 10:05 与 11:00 的两次同配置审批命中同一条诊断。
        """
        checked = parse_moment(payload["checked_at"])
        assert checked is not None
        deck = self._diagnostics()
        for item in deck:
            if (str(item.get("table_id")) != payload["table_id"]
                    or item.get("fingerprint") != payload["fingerprint"]):
                continue
            window_start = parse_moment(item.get("诊断周期开始"))
            window_end = parse_moment(item.get("诊断周期结束"))
            if window_start and window_end and window_start <= checked < window_end:
                item["复用次数"] = int(item.get("复用次数", 1)) + 1
                item["诊断说明"] = f"周期内重复审批，复用同一条诊断记录（第 {item['复用次数']} 次）"
                item["诊断结论"] = payload["reuse_result"]
                item["复用结果"] = payload["reuse_result"]
                item["最近诊断时间"] = payload["checked_at"]
                item["credential_id"] = payload["credential_id"]
                return item
        window_end = checked + timedelta(seconds=period_seconds)
        entry = {"id": self._next_id(deck)}
        entry.update({
            "诊断编号": f"ODSD-{entry['id']:06d}",
            "诊断结论": payload["reuse_result"],
            "诊断说明": payload["message"],
            "复用结果": payload["reuse_result"],
            "配置指纹": payload["fingerprint"],
            "配置摘要": payload["summary"],
            "诊断周期开始": checked.strftime("%Y-%m-%d %H:%M:%S"),
            "诊断周期结束": window_end.strftime("%Y-%m-%d %H:%M:%S"),
            "首次诊断时间": payload["checked_at"],
            "最近诊断时间": payload["checked_at"],
            "复用次数": 1,
            "table_id": payload["table_id"],
            "table_code": payload["table_code"],
            "credential_id": payload["credential_id"],
            "fingerprint": payload["fingerprint"],
            "operator": operator,
        })
        deck.append(entry)
        return entry

    # ---- 结论读取 ---------------------------------------------------------

    def approval_detail(self, entry: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        """贴源表审批详情：只读当前凭证，不回写状态，与明细层读的是同一份结论。"""
        checked_at = moment_key(now) if now else self._clock()
        values = {field: entry.get(field) for field in FINGERPRINT_FIELDS}
        fingerprint = self.fingerprint(values)
        credential = self.active_credential(str(entry.get("id")))
        last_credential = credential or self.latest_credential(str(entry.get("id")))
        usable, reason = self.evaluation(values, last_credential, str(entry.get("status")), checked_at, str(entry.get("表编号") or ""))

        return {
            "table_id": entry.get("id"),
            "table_code": entry.get("表编号"),
            "table_status": entry.get("status"),
            "准入状态": "可准入" if usable else "不可准入",
            "可用": usable,
            "不可用原因": reason,
            "配置指纹": fingerprint,
            "当前配置摘要": self.summary(values),
            "凭证": last_credential,
            "审批凭证": last_credential,
            "诊断记录": [item for item in self._diagnostics()
                         if str(item.get("table_id")) == str(entry.get("id"))],
            "判定时间": checked_at,
        }

    # ---- 下游联动 ---------------------------------------------------------

    def on_config_change(self, entry: dict[str, Any], changed_fields: list[str], *,
                         now: str | None = None) -> list[dict[str, Any]]:
        """贴源配置变化：凭证立即失效，并标记受影响的明细登记（不清除历史定义）。"""
        with self._lock:
            at = moment_key(now) if now else self._clock()
            labels = [FINGERPRINT_LABELS.get(field, field) for field in changed_fields]
            reason = f"上游配置已变化（{'、'.join(labels) or '配置'}），准入结论失效"
            self.invalidate_for_table(entry.get("id"), reason, now=at)
            return self.mark_downstream(entry, reason, at)

    def on_table_offline(self, entry: dict[str, Any], *, now: str | None = None) -> list[dict[str, Any]]:
        """贴源表下线：凭证作废，明细层登记显示不可用原因而不是继续引用。"""
        with self._lock:
            at = moment_key(now) if now else self._clock()
            reason = f"上游贴源表 {entry.get('表编号')} 已下线，准入凭证已失效"
            self.invalidate_for_table(entry.get("id"), reason, now=at)
            return self.mark_downstream(entry, reason, at)

    def mark_downstream(self, entry: dict[str, Any], reason: str, at: str) -> list[dict[str, Any]]:
        """只标记受影响，不修改明细表的历史定义字段。"""
        affected: list[dict[str, Any]] = []
        table_id = str(entry.get("id"))
        code = str(entry.get("表编号"))
        for item in store.rows("dwd"):
            if str(item.get("upstream_table_id")) != table_id and str(item.get("上游贴源表")) != code:
                continue
            item.setdefault("upstream_table_id", table_id)
            item["upstream_code"] = code
            item["上游准入状态"] = "不可准入"
            item["不可用原因"] = reason
            item["上游凭证状态"] = CREDENTIAL_STATE_INVALID
            item["受影响"] = True
            item["受影响时间"] = at
            affected.append(item)
        return affected

    def clear_downstream_flag(self, entry: dict[str, Any]) -> None:
        """贴源表重新可准入时，恢复明细登记的可用标记。"""
        table_id = str(entry.get("id"))
        code = str(entry.get("表编号"))
        for item in store.rows("dwd"):
            if str(item.get("upstream_table_id")) != table_id and str(item.get("上游贴源表")) != code:
                continue
            item.setdefault("upstream_table_id", table_id)
            item["upstream_code"] = code
            item["上游准入状态"] = "可准入"
            item["不可用原因"] = ""
            item["上游凭证状态"] = CREDENTIAL_STATE_VALID
            item["受影响"] = False
            item["受影响时间"] = ""

    @staticmethod
    def _clock() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class OdsApprovalFacade:
    """明细层读取贴源审批结论的唯一入口，保证上游准入与审批详情读同一份凭证。"""

    def __init__(self, service: OdsApprovalService) -> None:
        self._service = service

    def approval_detail(self, entry: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        return self._service.approval_detail(entry, now=now)

    def evaluate_upstream(self, entry: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        """明细层使用的上游准入视图；只复用审批结论，不另起一次审批。"""
        detail = self._service.approval_detail(entry, now=now)
        credential = detail.get("审批凭证") or {}
        return {
            "table_id": str(entry.get("id")),
            "table_code": entry.get("表编号"),
            "准入状态": detail["准入状态"],
            "可用": detail["可用"],
            "不可用原因": detail["不可用原因"],
            "配置指纹": detail["配置指纹"],
            "配置摘要": detail["当前配置摘要"],
            "凭证编号": credential.get("凭证编号"),
            "凭证状态": credential.get("凭证状态"),
            "有效期至": credential.get("有效期至"),
            "签发时间": credential.get("签发时间"),
        }


def _deadline_reached(deadline: Any, current: str) -> bool:
    end = parse_moment(deadline)
    now = parse_moment(current)
    return bool(end and now and end <= now)


ods_approval = OdsApprovalService()


def bootstrap_credentials() -> None:
    """服务启动时为已上线但还没有凭证的贴源表补签一张，保证明细层有结论可读。

    这些表在示例数据里已经走完上线审批，补签只是把既有结论登记成凭证，不会额外放宽准入：
    凭证仍然带有效期，四项配置一变就失效。
    """
    for entry in store.rows("ods"):
        if entry.get("status") != READY_STATUS:
            continue
        if ods_approval.latest_credential(entry.get("id")) is not None:
            continue
        ods_approval.review(entry, operator="系统初始化", record_diagnostic=False)


bootstrap_credentials()
