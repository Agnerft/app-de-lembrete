import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from fastapi import HTTPException

import app


class AccessTokenTests(unittest.TestCase):
    def test_token_is_scoped_to_consulted_client(self):
        with patch.object(app, "ACCESS_TOKEN_SECRET", "test-secret"):
            token = app.create_access_token("556993104620", "cliente01")
            app.require_access_token(token, "6993104620", "cliente01")

            with self.assertRaises(HTTPException) as raised:
                app.require_access_token(token, "11999999999", "outro")

        self.assertEqual(raised.exception.status_code, 403)

    def test_expired_token_is_rejected(self):
        with (
            patch.object(app, "ACCESS_TOKEN_SECRET", "test-secret"),
            patch.object(app, "ACCESS_TOKEN_TTL_SECONDS", -1),
        ):
            token = app.create_access_token("6993104620", "cliente01")
            with self.assertRaises(HTTPException) as raised:
                app.require_access_token(token, "6993104620", "cliente01")

        self.assertEqual(raised.exception.status_code, 401)


class ProxyAndRateLimitTests(unittest.TestCase):
    def test_forwarded_ip_is_only_used_for_trusted_proxy(self):
        headers = {"x-forwarded-for": "203.0.113.20", "x-real-ip": "203.0.113.21"}
        trusted = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers=headers)
        untrusted = SimpleNamespace(client=SimpleNamespace(host="198.51.100.10"), headers=headers)

        with patch.object(app, "TRUSTED_PROXY_IPS", {"127.0.0.1"}):
            self.assertEqual(app.client_ip(trusted), "203.0.113.21")
            self.assertEqual(app.client_ip(untrusted), "198.51.100.10")

    def test_rate_limit_hits_are_persisted_in_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "security.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                app.record_rate_limit_hit("ip:test", 2, 60, "limit")
                app.record_rate_limit_hit("ip:test", 2, 60, "limit")

                with self.assertRaises(HTTPException) as raised:
                    app.record_rate_limit_hit("ip:test", 2, 60, "limit")

                with app.db_connect() as connection:
                    total = connection.execute(
                        "SELECT COUNT(*) FROM rate_limit_hits WHERE bucket_key = ?",
                        ("ip:test",),
                    ).fetchone()[0]

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(total, 2)


class AdminAuditTests(unittest.TestCase):
    def test_audit_only_records_while_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                self.assertFalse(app.record_admin_audit_event("login1", "revenda1"))
                app.set_admin_audit(True)
                self.assertTrue(app.record_admin_audit_event("login1", "revenda1"))
                events = app.list_admin_audit_events()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["login"], "login1")
        self.assertEqual(events[0]["reseller"], "revenda1")

    def test_audit_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                app.set_admin_audit(True)
                app.set_admin_audit(False)
                self.assertFalse(app.admin_audit_status()["enabled"])
        self.assertFalse(app.record_admin_audit_event("login1", "revenda1"))


class CommunityStatsTests(unittest.TestCase):
    def test_visitors_and_likes_are_unique_per_device(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "community.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                first = app.record_community_visit("device-0000000001")
                repeated = app.record_community_visit("device-0000000001")
                second = app.record_community_visit("device-0000000002")
                liked = app.set_community_like("device-0000000001", True)
                liked_again = app.set_community_like("device-0000000001", True)

        self.assertEqual(first["users"], 1)
        self.assertEqual(repeated["users"], 1)
        self.assertEqual(second["users"], 2)
        self.assertEqual(liked["likes"], 1)
        self.assertEqual(liked_again["likes"], 1)
        self.assertTrue(liked_again["liked"])

    def test_like_can_be_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "community.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                app.set_community_like("device-0000000001", True)
                result = app.set_community_like("device-0000000001", False)

        self.assertEqual(result["users"], 1)
        self.assertEqual(result["likes"], 0)
        self.assertFalse(result["liked"])


class AdminSupportContactsTests(unittest.TestCase):
    def test_official_and_reseller_contacts_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database = base / "contacts.sqlite3"
            contacts_file = base / "support_contacts.json"
            with patch.object(app, "DB_FILE", database), patch.object(app, "SUPPORT_CONTACTS_FILE", contacts_file):
                app.init_database()
                now = datetime.now().isoformat()
                with app.db_connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO resellers
                            (username, display_name, first_seen_at, last_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("revenda1", "Revenda Um", now, now, now),
                    )

                official = app.save_official_support_whatsapp("69 99999-1111")
                reseller = app.save_reseller_support_whatsapp("revenda1", "69 99999-2222")
                payload = app.list_admin_support_contacts()

        self.assertEqual(official, "5569999991111")
        self.assertEqual(reseller, "5569999992222")
        self.assertEqual(payload["oficial"], official)
        self.assertEqual(payload["revendas"][0]["whatsapp"], reseller)

    def test_only_resellers_from_login_file_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database = base / "contacts.sqlite3"
            logins_file = base / "revendas_logins.json"
            app.write_json_file(logins_file, [{"nome": "Jacques"}, {"nome": "Rogerio"}])
            with patch.object(app, "DB_FILE", database), patch.object(app, "RESELLER_LOGINS_FILE", logins_file):
                app.init_database()
                payload = app.list_admin_support_contacts()

        self.assertEqual([row["nome"] for row in payload["revendas"]], ["Jacques", "Rogerio"])

    def test_invalid_contact_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            app.validate_support_whatsapp("123")
        self.assertEqual(raised.exception.status_code, 400)

    def test_support_message_includes_user_due_date_and_app_origin(self):
        url = app.whatsapp_support_url(
            "5569999999999",
            "usuario_x",
            "20/06/2026",
        )
        message = parse_qs(urlparse(url).query)["text"][0]

        self.assertIn("Usuário: usuario_x", message)
        self.assertIn("Vencimento: 20/06/2026", message)
        self.assertIn("Mensagem enviada pelo Mega App", message)
        self.assertNotIn("Revenda:", message)


class AppSlotsTests(unittest.TestCase):
    def test_three_apps_are_saved_for_three_screens(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "apps.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                for slot, app_name in enumerate(("Clouddy", "HD Player", "Max Player"), start=1):
                    app.save_app_preference_record(
                        app.AppPreferenceRequest(
                            telefone="6993104620",
                            login="cliente01",
                            app_usado=app_name,
                            slot=slot,
                        )
                    )

                preference = app.get_app_preference("556993104620", "cliente01")

        self.assertEqual([item["slot"] for item in preference["apps"]], [1, 2, 3])
        self.assertEqual([item["app_usado"] for item in preference["apps"]], ["Clouddy", "HD Player", "Max Player"])

    def test_clouddy_credentials_use_client_id(self):
        self.assertEqual(
            app.build_clouddy_access("96576852"),
            {"email": "96576852@zt.rpa", "senha": "96576852"},
        )

    def test_removed_slot_is_not_restored_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "migration.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                app.save_app_preference_record(
                    app.AppPreferenceRequest(
                        telefone="6993104620",
                        login="cliente01",
                        app_usado="Clouddy",
                        slot=1,
                    )
                )
                app.save_app_preference_record(
                    app.AppPreferenceRequest(
                        telefone="6993104620",
                        login="cliente01",
                        app_usado="",
                        slot=1,
                    )
                )
                app.init_database()
                preference = app.get_app_preference("6993104620", "cliente01")

        self.assertIsNone(preference)


class PaymentMatchTests(unittest.TestCase):
    def response_with(self, payload):
        return SimpleNamespace(status_code=200, json=lambda: payload)

    def test_unrelated_payment_result_is_rejected(self):
        unrelated = {
            "DT_RowId": "999",
            "telefone": "5511999999999",
            "Link": "https://pagueaqui.top/exemplo",
        }
        with patch.object(app.requests, "post", return_value=self.response_with(unrelated)):
            result = app.search_payment_data("555198801444")

        self.assertIsNone(result)

    def test_matching_payment_result_is_accepted(self):
        matching = {
            "DT_RowId": "123",
            "telefone": "555198801444",
            "Link": "https://pagueaqui.top/exemplo",
        }
        with patch.object(app.requests, "post", return_value=self.response_with(matching)):
            result = app.search_payment_data("55 51 9880-1444")

        self.assertEqual(result, matching)

    def test_line_without_payment_is_returned(self):
        line = {
            "id": 123,
            "user_username": "revenda1",
            "username": "cliente1",
            "password": "senha1",
            "phone": "+5527999999999",
            "is_enabled": True,
            "status": "active",
        }
        with (
            patch.object(app, "enforce_rate_limit"),
            patch.object(app, "search_payment_data", return_value=None),
            patch.object(app, "search_line_data", return_value=line),
            patch.object(app, "support_contact_for_reseller", return_value=None),
            patch.object(app, "get_app_preference", return_value=None),
            patch.object(app, "get_reminder_days_for_client", return_value=[3, 2, 1, 0]),
            patch.object(app, "save_notification_client"),
            patch.object(app, "record_admin_audit_event"),
            patch.object(app, "ACCESS_TOKEN_SECRET", "test-secret"),
        ):
            response = app.consultar_cliente(
                app.PhoneRequest(telefone="27999999999"),
                SimpleNamespace(),
            )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["cliente"]["login"], "cliente1")
        self.assertIsNone(payload["cliente"]["link_pagamento"])


class ReminderPreferenceTests(unittest.TestCase):
    def test_reminder_days_are_normalized(self):
        self.assertEqual(app.normalize_reminder_days([0, 3, 3, 9, "2", 1]), [3, 2, 1, 0])
        self.assertEqual(app.normalize_reminder_days([]), [])
        self.assertEqual(app.normalize_reminder_days(), [3, 2, 1, 0])

    def test_reminders_only_send_on_selected_days(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            clients_file = base / "clients.json"
            subscriptions_file = base / "subscriptions.json"
            sent_file = base / "sent.json"
            phone = "5551999999999"
            due_date = (datetime.now() + timedelta(days=3)).strftime("%d/%m/%Y")
            app.write_json_file(clients_file, {phone: {"telefone": phone, "login": "teste", "vencimento": due_date}})
            app.write_json_file(
                subscriptions_file,
                {phone: {"subscription": {"endpoint": "https://example.test/push"}, "reminder_days": [2, 0]}},
            )
            app.write_json_file(sent_file, {})

            with (
                patch.object(app, "CLIENTS_FILE", clients_file),
                patch.object(app, "SUBSCRIPTIONS_FILE", subscriptions_file),
                patch.object(app, "SENT_REMINDERS_FILE", sent_file),
                patch.object(app, "send_push", return_value=True) as send_push,
            ):
                skipped = app.check_and_send_reminders()
                self.assertEqual(skipped["sent"], 0)
                send_push.assert_not_called()

                app.write_json_file(
                    subscriptions_file,
                    {phone: {"subscription": {"endpoint": "https://example.test/push"}, "reminder_days": [3]}},
                )
                delivered = app.check_and_send_reminders()

        self.assertEqual(delivered["sent"], 1)


if __name__ == "__main__":
    unittest.main()
