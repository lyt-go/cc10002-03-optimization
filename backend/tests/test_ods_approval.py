"""贴源审批结论复用与失效规则的回归测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from app.services.dwd import DwdService
from app.services.ods import OdsService
from app.services.ods_credential import (
    DIAGNOSTIC_PERIOD_SECONDS,
    ods_approval,
    parse_moment,
)
from app.store import store


class ApprovalReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ods = OdsService()
        self.dwd = DwdService()
        # 内存仓库是全局单例，每个用例先恢复贴源样例与凭证台账，避免相互污染。
        sales = store.find("ods", 4)
        assert sales is not None
        sales.update({
            "status": "已上线",
            "pending": False,
            "abnormal": False,
            "来源系统": "CRM",
            "增量字段": "updated_at",
            "全量快照策略": "每日全量快照",
            "每日增量行": "230000",
            "最近加载时间": "2026-10-12 10:00",
            "审批结论": "",
            "不可用原因": "",
        })
        dwd = store.find("dwd", 3)
        assert dwd is not None
        dwd.update({
            "status": "已上线",
            "pending": False,
            "abnormal": False,
            "表编号": "DWD-0003",
            "上游贴源表": "ODS_SALES",
            "刷新水位": "明细层样例3",
        })
        dwd.pop("受影响", None)
        dwd.pop("不可用原因", None)
        store.rows("ods_credential").clear()
        store.rows("ods_diagnostic").clear()

    def _sales(self) -> dict:
        entry = store.find("ods", 4)
        assert entry is not None
        return entry

    def test_unchanged_config_reuses_credential(self) -> None:
        entry, review, _ = self.ods.review(4, now="2026-10-12 10:05")
        self.assertEqual(review["复用结论"], "可准入")
        first = review["凭证"]["凭证编号"]

        _, again, _ = self.ods.review(4, now="2026-10-12 11:00")
        self.assertTrue(again["是否复用"])
        self.assertEqual(again["凭证"]["凭证编号"], first)
        self.assertEqual(len(self.ods.credentials(4)[1]), 1)

    def test_any_field_change_invalidates_immediately(self) -> None:
        self.ods.review(4, now="2026-10-12 10:05")
        _, message = self.ods.update_entry(4, {"全量快照策略": "每小时增量快照"})
        self.assertIn("旧审批结论已失效", message)

        _, detail = self.ods.approval_detail(4)
        self.assertEqual(detail["准入状态"], "不可准入")
        self.assertEqual(detail["凭证"]["凭证状态"], "已失效")
        self.assertIn("快照策略", detail["不可用原因"])

    def test_each_fingerprint_field_invalidates(self) -> None:
        for field, new_value in (
            ("来源系统", "ERP"),
            ("增量字段", "created_at"),
            ("全量快照策略", "仅增量"),
            ("最近加载时间", "2026-10-12 12:00"),
        ):
            with self.subTest(field=field):
                store.find("ods", 4)["最近加载时间"] = "2026-10-12 10:00"
                store.find("ods", 4)["全量快照策略"] = "每日全量快照"
                store.find("ods", 4)["增量字段"] = "updated_at"
                store.find("ods", 4)["来源系统"] = "CRM"
                ods_approval.review(self._sales(), now="2026-10-12 10:05")
                self.ods.update_entry(4, {field: new_value})
                _, detail = self.ods.approval_detail(4)
                self.assertEqual(detail["准入状态"], "不可准入")

    def test_approval_detail_and_dwd_read_same_conclusion(self) -> None:
        self.ods.review(4, now="2026-10-12 10:05")
        _, detail = self.ods.approval_detail(4)
        view = self.dwd.with_upstream(store.find("dwd", 3))
        self.assertEqual(view["上游准入状态"], detail["准入状态"])
        self.assertEqual(view["上游凭证编号"], detail["凭证"]["凭证编号"])
        self.assertEqual(view["上游配置指纹"], detail["配置指纹"])

    def test_offline_marks_downstream_unavailable_but_keeps_definition(self) -> None:
        self.ods.review(4, now="2026-10-12 10:05")
        before = dict(store.find("dwd", 3))
        entry, message = self.ods.run_action(4, "下线表")
        self.assertIn("标记为受影响", message)
        self.assertEqual(entry["status"], "已下线")

        row = store.find("dwd", 3)
        view = self.dwd.with_upstream(row)
        self.assertEqual(view["上游准入状态"], "不可准入")
        self.assertIn("已下线", view["不可用原因"])
        # 历史定义保留，只加受影响标记。
        self.assertEqual(row["表编号"], before["表编号"])
        self.assertEqual(row["上游贴源表"], before["上游贴源表"])
        self.assertEqual(row["刷新水位"], before["刷新水位"])
        self.assertTrue(row.get("受影响"))

    def test_downstream_action_blocked_when_upstream_unavailable(self) -> None:
        self.ods.review(4, now="2026-10-12 10:05")
        self.ods.run_action(4, "下线表")
        entry, message = self.dwd.run_action(3, "确认上线")
        self.assertIsNone(entry)
        self.assertIn("上游贴源不可用", message)

    def test_conflicting_configs_resolved_by_ledger_order(self) -> None:
        """同一贴源表出现两张生效凭证时，按审核单落库顺序取最后一版。"""
        first = ods_approval._issue(
            {**self._sales(), "全量快照策略": "每小时增量快照"},
            checked_at="2026-10-12 09:00",
            period_seconds=DIAGNOSTIC_PERIOD_SECONDS,
            operator="审核员甲",
        )
        second = ods_approval._issue(
            {**self._sales(), "全量快照策略": "每日全量快照"},
            checked_at="2026-10-12 10:00",
            period_seconds=DIAGNOSTIC_PERIOD_SECONDS,
            operator="审核员乙",
        )
        self.assertNotEqual(first["id"], second["id"])
        active = ods_approval.active_credential(4)
        self.assertEqual(active["id"], second["id"])

        _, review, _ = self.ods.review(4, now="2026-10-12 10:05")
        self.assertEqual(review["配置指纹"], second["fingerprint"])
        current = [item for item in ods_approval.credentials() if item.get("current")
                   and str(item.get("table_id")) == "4"]
        self.assertEqual(len(current), 1, "并发修改审批结论只允许一个生效")
        self.assertEqual(current[0]["id"], second["id"])

    def test_periodic_duplicate_review_keeps_one_diagnostic(self) -> None:
        before = len(self.ods.diagnostics(4)[1])
        for moment in ("2026-10-12 10:05", "2026-10-12 11:00", "2026-10-12 18:00"):
            self.ods.review(4, now=moment)
        diagnostics = self.ods.diagnostics(4)[1]
        self.assertEqual(len(diagnostics) - before, 1)
        self.assertEqual(diagnostics[-1]["复用次数"], 3)

    def test_new_diagnostic_after_period(self) -> None:
        before = len(self.ods.diagnostics(4)[1])
        self.ods.review(4, now="2026-10-12 10:05")
        self.ods.review(4, now="2026-10-13 10:05")
        self.assertEqual(len(self.ods.diagnostics(4)[1]) - before, 2)

    def test_expired_credential_requires_reissue(self) -> None:
        _, review, _ = self.ods.review(4, now="2026-10-12 10:05", period_seconds=3600)
        first = review["凭证"]["凭证编号"]
        _, detail = self.ods.approval_detail(4, now="2026-10-12 12:00")
        self.assertEqual(detail["准入状态"], "不可准入")
        self.assertIn("有效期", detail["不可用原因"])

        _, reissued, _ = self.ods.review(4, now="2026-10-12 12:05")
        self.assertFalse(reissued["是否复用"])
        self.assertNotEqual(reissued["凭证"]["凭证编号"], first)

    def test_upstream_options_use_same_conclusion(self) -> None:
        self.ods.review(4, now="2026-10-12 10:05")
        options = {item["table_code"]: item for item in self.dwd.upstream_options()}
        _, detail = self.ods.approval_detail(4)
        self.assertEqual(options["ODS_SALES"]["准入状态"], detail["准入状态"])
        self.assertEqual(options["ODS_SALES"]["凭证编号"], detail["凭证"]["凭证编号"])

    def test_review_rejected_for_offline_table(self) -> None:
        store.find("ods", 4)["status"] = "已下线"
        entry, review, message = self.ods.review(4, now="2026-10-12 10:05")
        self.assertIsNone(review)
        self.assertIn("已下线", message)
        self.assertEqual(entry["status"], "已下线")

    def test_config_change_between_reviews_invalidates_before_reissue(self) -> None:
        """配置在两次审批之间被改过：旧凭证必须已失效，新凭证按新指纹签发。"""
        self.ods.review(4, now="2026-10-12 10:05")
        first = self.ods.credentials(4)[1][-1]
        self.ods.update_entry(4, {"增量字段": "created_at"})
        stale = self.ods.credentials(4)[1][-1]
        self.assertEqual(stale["凭证编号"], first["凭证编号"])
        self.assertEqual(stale["凭证状态"], "已失效")

        _, review, _ = self.ods.review(4, now="2026-10-12 12:00")
        self.assertFalse(review["是否复用"])
        self.assertNotEqual(review["凭证"]["凭证编号"], first["凭证编号"])
        self.assertEqual(review["凭证"]["增量字段"], "created_at")
        ledger = self.ods.credentials(4)[1]
        current = [item for item in ledger if item.get("current")]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["凭证编号"], review["凭证"]["凭证编号"])

    def test_dwd_keeps_definition_but_reports_unavailable_on_expiry(self) -> None:
        self.ods.review(4, now="2026-10-12 10:05", period_seconds=3600)
        row = self.dwd.get_entry(3, now="2026-10-12 12:00")
        self.assertEqual(row["上游准入状态"], "不可准入")
        self.assertIn("有效期", row["不可用原因"])
        self.assertEqual(row["表编号"], "DWD-0003")
        self.assertEqual(row["上游贴源表"], "ODS_SALES")

    def test_review_does_not_touch_other_tables_credentials(self) -> None:
        other_before = [c["凭证编号"] for c in ods_approval.credentials() if str(c.get("table_id")) == "3"]
        self.ods.review(4, now="2026-10-12 10:05")
        other_after = [c["凭证编号"] for c in ods_approval.credentials() if str(c.get("table_id")) == "3"]
        self.assertEqual(other_before, other_after)


class MomentParseTest(unittest.TestCase):
    def test_date_only_is_midnight(self) -> None:
        self.assertEqual(parse_moment("2026-09-01"), datetime(2026, 9, 1, 0, 0, 0))
        self.assertEqual(parse_moment("2026-10-12 10:00"), datetime(2026, 10, 12, 10, 0))


if __name__ == "__main__":
    unittest.main()
