from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

from imagegen.extensions import db
from imagegen.models import (
    User,
    WalletLedger,
    utcnow,
)
from imagegen.services import ServiceError
from imagegen.services.conversations.prompts import CHAT_SYSTEM_PROMPT
from tests.support.platform import (
    PlatformTestCase,
)


class TestAuthAndWorkspaces(PlatformTestCase):
    def test_logout_clears_remember_cookie_and_requires_login(self):
        client = self.app.test_client()
        response = client.post(
            "/login",
            data={
                "username": "artist",
                "password": "StrongPass123!",
                "remember": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(client.get_cookie("remember_token"))

        response = client.post("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))
        self.assertIsNone(client.get_cookie("remember_token"))
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_password_reset_revokes_old_remember_cookie(self):
        client = self.app.test_client()
        response = client.post(
            "/login",
            data={
                "username": "artist",
                "password": "StrongPass123!",
                "remember": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(client.get_cookie("remember_token"))

        self.services.users.reset_password(
            self.user.id,
            "ReplacementPass123!",
            self.admin.id,
        )
        client.delete_cookie(self.app.config.get("SESSION_COOKIE_NAME", "session"))

        self.context.pop()
        try:
            response = client.get("/")
        finally:
            self.context.push()

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_changing_own_password_refreshes_current_remember_identity(self):
        client = self.app.test_client()
        client.post(
            "/login",
            data={
                "username": "artist",
                "password": "StrongPass123!",
                "remember": "1",
            },
        )
        old_token = client.get_cookie("remember_token").value

        response = client.post(
            "/account/password",
            json={
                "current_password": "StrongPass123!",
                "new_password": "ReplacementPass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertNotEqual(client.get_cookie("remember_token").value, old_token)

    def test_password_requires_at_least_six_characters(self):
        self.services.auth.set_password(self.user, "123456")
        self.assertTrue(self.services.auth.verify_password(self.user, "123456"))

        with self.assertRaisesRegex(ServiceError, "密码不能为空"):
            self.services.auth.set_password(self.user, "")
        with self.assertRaisesRegex(ServiceError, "至少需要 6"):
            self.services.auth.set_password(self.user, "12345")

    def test_login_rate_limit_uses_trusted_forwarded_client(self):
        client = self.app.test_client()
        request_options = {
            "data": {"username": "artist", "password": "wrong-password"},
            "headers": {"X-Forwarded-For": "198.51.100.20"},
            "environ_base": {"REMOTE_ADDR": "192.0.2.10"},
        }

        for _attempt in range(5):
            response = client.post("/login", **request_options)
            self.assertEqual(response.status_code, 200)

        blocked = client.post("/login", **request_options)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("登录失败次数过多", blocked.get_data(as_text=True))

        allowed = client.post(
            "/login",
            data={"username": "artist", "password": "StrongPass123!"},
            headers={"X-Forwarded-For": "198.51.100.21"},
            environ_base={"REMOTE_ADDR": "192.0.2.10"},
        )
        self.assertEqual(allowed.status_code, 302)

    def test_chat_system_prompt_uses_a_natural_visual_partner_identity(self):
        self.assertIn("AI 视觉创作搭档", CHAT_SYSTEM_PROMPT)
        self.assertIn("不要像客服、产品说明书或信息收集表", CHAT_SYSTEM_PROMPT)
        self.assertIn("需求访谈", CHAT_SYSTEM_PROMPT)
        self.assertIn("在同一条回复中一次性问完", CHAT_SYSTEM_PROMPT)
        self.assertIn("问题宁少勿多，最多四个", CHAT_SYSTEM_PROMPT)
        self.assertIn("不得把已经能识别的问题留到后续轮次", CHAT_SYSTEM_PROMPT)
        self.assertIn("1A 2C 3B", CHAT_SYSTEM_PROMPT)
        self.assertIn("其他（请自定义）", CHAT_SYSTEM_PROMPT)
        self.assertIn("确认后在同一次回复中直接整理最终提示词", CHAT_SYSTEM_PROMPT)
        self.assertNotIn("公司内部 AI 视觉创作工作台的需求顾问", CHAT_SYSTEM_PROMPT)

    def test_image_workspace_chat_uses_static_image_guidance(self):
        workspace = self.create_workspace("单图讨论")

        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="设计一张电影感人物海报",
        )

        system = self.chat_client.calls[-1]["system"]
        self.assertIn("当前是静态图片工作站", system)
        self.assertIn("以下情况必须先澄清", system)
        self.assertIn("一张完整画面", system)

    def test_unsupported_workspace_kind_is_rejected(self):
        response = self.user_client().post(
            "/api/workspaces",
            json={"name": "未知类型", "kind": "unsupported"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "工作站类型无效")

    def test_admin_creates_user_and_balance_ledger_is_immutable_history(self):
        self.services.billing.adjust(
            user_id=self.user.id,
            actor_user_id=self.admin.id,
            amount="5.25",
            operation="add",
            note="季度额度",
        )
        user = db.session.get(User, self.user.id)
        self.assertEqual(user.balance_rmb, Decimal("25.2500"))
        entries = list(
            db.session.scalars(
                select(WalletLedger)
                .where(WalletLedger.user_id == self.user.id)
                .order_by(WalletLedger.id)
            )
        )
        self.assertEqual([entry.entry_type for entry in entries], ["initial_balance", "admin_add"])
        self.assertEqual(entries[-1].amount_rmb, Decimal("5.2500"))

    def test_spending_summary_uses_shanghai_day_and_only_generation_charges(self):
        now = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
        db.session.add_all(
            [
                WalletLedger(
                    user_id=self.user.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-1.2500"),
                    balance_after_rmb=Decimal("18.7500"),
                    note="昨日生图",
                    created_at=datetime(2026, 7, 13, 15, 59, tzinfo=timezone.utc),
                ),
                WalletLedger(
                    user_id=self.user.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-2.5000"),
                    balance_after_rmb=Decimal("16.2500"),
                    note="今日生图",
                    created_at=datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc),
                ),
                WalletLedger(
                    user_id=self.user.id,
                    actor_user_id=self.admin.id,
                    entry_type="admin_subtract",
                    amount_rmb=Decimal("-9.0000"),
                    balance_after_rmb=Decimal("7.2500"),
                    note="余额调整不计消费",
                    created_at=datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        db.session.commit()

        summary = self.services.billing.spending_summary(self.user.id, now=now)

        self.assertEqual(summary.total_rmb, Decimal("3.7500"))
        self.assertEqual(summary.today_rmb, Decimal("2.5000"))

    def test_user_and_admin_apis_include_spending_summaries(self):
        db.session.add_all(
            [
                WalletLedger(
                    user_id=self.user.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-1.2500"),
                    balance_after_rmb=Decimal("18.7500"),
                    note="用户生图",
                    created_at=utcnow(),
                ),
                WalletLedger(
                    user_id=self.admin.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-0.7500"),
                    balance_after_rmb=Decimal("0.0000"),
                    note="管理员生图",
                    created_at=utcnow(),
                ),
            ]
        )
        db.session.commit()

        user_client = self.user_client()
        me = user_client.get("/api/me").json
        self.assertEqual(me["spending"], {"today_rmb": "1.2500", "total_rmb": "1.2500"})
        user_client.post("/logout")

        admin_data = self.admin_client().get("/api/admin/users").json
        self.assertEqual(admin_data["spending"], {"today_rmb": "2.0000", "total_rmb": "2.0000"})
        users = {user["id"]: user for user in admin_data["users"]}
        self.assertEqual(
            users[self.user.id]["spending"],
            {"today_rmb": "1.2500", "total_rmb": "1.2500"},
        )

    def test_me_can_skip_ledger_for_polling(self):
        client = self.user_client()

        full = client.get("/api/me").json
        polling = client.get("/api/me?ledger=0").json

        self.assertIn("ledger", full)
        self.assertNotIn("ledger", polling)
        self.assertEqual(polling["user"], full["user"])
        self.assertEqual(polling["spending"], full["spending"])

    def test_admin_balance_adjustment_note_is_optional(self):
        self.services.billing.adjust(
            user_id=self.user.id,
            actor_user_id=self.admin.id,
            amount="1.00",
            operation="add",
            note="",
        )
        entry = db.session.scalar(
            select(WalletLedger)
            .where(WalletLedger.user_id == self.user.id)
            .order_by(WalletLedger.id.desc())
        )
        self.assertEqual(entry.entry_type, "admin_add")
        self.assertEqual(entry.note, "")

    def test_at_most_ten_workspaces_per_user(self):
        for index in range(10):
            self.create_workspace(f"工作站 {index + 1}")
        with self.assertRaisesRegex(ServiceError, "最多创建 10 个"):
            self.create_workspace("第十一个")

        response = self.user_client().get("/api/workspaces")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["max_count"], 10)

    def test_workspace_order_can_be_rearranged_and_persists(self):
        first = self.create_workspace("第一站")
        second = self.create_workspace("第二站")
        third = self.create_workspace("第三站")
        requested = [first.id, third.id, second.id]
        client = self.user_client()

        response = client.put(
            "/api/workspaces/order",
            json={"workspace_ids": requested},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        db.session.expire_all()
        self.assertEqual(
            [workspace.id for workspace in self.services.workspaces.list(self.user.id)],
            requested,
        )
        listed = client.get("/api/workspaces").json["workspaces"]
        self.assertEqual([workspace["id"] for workspace in listed], requested)

    def test_workspace_rename_rejects_an_existing_name(self):
        first = self.create_workspace("同名工作站")
        second = self.create_workspace("待重命名工作站")

        response = self.user_client().patch(
            f"/api/workspaces/{second.id}",
            json={"name": first.name},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "workspace_name_exists")
        db.session.refresh(second)
        self.assertEqual(second.name, "待重命名工作站")

    def test_blank_workspace_names_use_dated_defaults(self):
        first = self.create_workspace("")
        second = self.create_workspace("")

        self.assertRegex(first.name, r"^工作站-\d{4}-\d{2}-\d{2}$")
        self.assertEqual(second.name, f"{first.name} 2")
        self.assertNotIn("auto_title", first.settings)

    def test_admin_created_user_receives_starter_workspaces(self):
        created = self.admin_client().post(
            "/api/admin/users",
            json={"username": "starter-user", "password": "StarterPass123!"},
        )

        self.assertEqual(created.status_code, 201)
        workspaces = self.services.workspaces.list(created.json["user"]["id"])
        self.assertEqual(
            [workspace.name for workspace in workspaces],
            ["海风与远方", "参考图再创作"],
        )

    def test_admin_user_creation_rolls_back_when_starter_workspaces_fail(self):
        client = self.admin_client()
        with patch.object(
            self.services.workspaces,
            "ensure_starter_workspaces",
            side_effect=RuntimeError("starter workspace failure"),
        ):
            response = client.post(
                "/api/admin/users",
                json={"username": "failed-starter", "password": "StarterPass123!"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertIsNone(db.session.scalar(select(User).where(User.username == "failed-starter")))

    def test_starter_workspaces_are_ready_to_use(self):
        self.services.workspaces.ensure_starter_workspaces(self.user.id)
        client = self.user_client()

        workspaces = self.services.workspaces.list(self.user.id)
        self.assertEqual(
            [workspace.name for workspace in workspaces],
            ["海风与远方", "参考图再创作"],
        )
        by_name = {workspace.name: workspace for workspace in workspaces}

        text_messages = client.get(f"/api/workspaces/{by_name['海风与远方'].id}/messages").json[
            "messages"
        ]
        self.assertEqual(len(text_messages), 1)
        self.assertEqual(text_messages[0]["kind"], "prompt_draft")
        self.assertEqual(text_messages[0]["payload"]["reference_ids"], [])
        self.assertIn("海洋", text_messages[0]["payload"]["prompt"])
        self.assertIn("天空", text_messages[0]["payload"]["prompt"])

        reference_messages = client.get(
            f"/api/workspaces/{by_name['参考图再创作'].id}/messages"
        ).json["messages"]
        self.assertEqual(len(reference_messages), 1)
        reference_draft = reference_messages[0]
        self.assertEqual(reference_draft["kind"], "prompt_draft")
        self.assertEqual(len(reference_draft["attachments"]), 1)
        self.assertEqual(
            reference_draft["payload"]["reference_ids"],
            [reference_draft["attachments"][0]["id"]],
        )
        image = client.get(reference_draft["attachments"][0]["url"])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.mimetype, "image/png")
        image.close()

    def test_studio_does_not_recreate_deleted_workspaces(self):
        workspace = self.create_workspace("最后一个工作站")
        self.services.workspaces.delete(workspace)

        response = self.user_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.services.workspaces.list(self.user.id), [])

    def test_workspace_settings_persist(self):
        workspace = self.create_workspace()
        client = self.user_client()
        reference_ids = ["a" * 32, "b" * 32]

        response = client.patch(
            f"/api/workspaces/{workspace.id}",
            json={
                "settings": {
                    "size": "1280x720",
                    "mode": "img2img",
                    "reference_ids": reference_ids,
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["workspace"]["settings"]["size"], "1280x720")
        self.assertEqual(response.json["workspace"]["settings"]["reference_ids"], reference_ids)
        workspaces = client.get("/api/workspaces").json["workspaces"]
        restored = next(item for item in workspaces if item["id"] == workspace.id)
        self.assertEqual(restored["settings"]["size"], "1280x720")
        self.assertEqual(restored["settings"]["reference_ids"], reference_ids)
