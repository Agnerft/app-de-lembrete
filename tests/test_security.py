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


class GestorPlanTests(unittest.TestCase):
    def test_bearer_is_saved_per_reseller_without_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with patch.object(app, "DB_FILE", database):
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
                status = app.save_gestor_bearer("revenda1", "Bearer token-123")
                saved = app.get_reseller_gestor_bearer("Revenda Um")

        self.assertTrue(status["configured"])
        self.assertEqual(saved, "token-123")

    def test_bearer_lookup_matches_reseller_name_without_revenda_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                now = datetime.now().isoformat()
                with app.db_connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO resellers
                            (username, display_name, first_seen_at, last_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("Junior", "Junior", now, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO resellers
                            (username, display_name, first_seen_at, last_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("Revenda Junior", "Revenda Junior", now, now, now),
                    )
                app.save_gestor_bearer("Revenda Junior", "Bearer token-junior")
                saved = app.get_reseller_gestor_bearer("Junior")

        self.assertEqual(saved, "token-junior")

    def test_bearer_lookup_matches_configured_reseller_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                now = datetime.now().isoformat()
                with app.db_connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO resellers
                            (username, display_name, first_seen_at, last_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("MicheliRibeiro", "MicheliRibeiro", now, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO resellers
                            (username, display_name, first_seen_at, last_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("Revenda Michele", "Revenda Michele", now, now, now),
                    )
                app.save_gestor_bearer("Revenda Michele", "Bearer token-michele")
                saved = app.get_reseller_gestor_bearer("MicheliRibeiro")

        self.assertEqual(saved, "token-michele")

    def test_plan_change_sends_patch_with_reseller_bearer(self):
        response = SimpleNamespace(status_code=200)
        response.json = lambda: {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app.requests, "patch", return_value=response) as mocked_patch,
            ):
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
                app.save_gestor_bearer("revenda1", "Bearer gestor-token")
                result = app.change_gestor_client_plan("13", "cliente-456", "Revenda Um")

        self.assertEqual(result["plano"], "Consultoria mensal - R$ 29,90")
        self.assertEqual(result["revenda"], "Revenda Um")
        mocked_patch.assert_called_once()
        _, kwargs = mocked_patch.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer gestor-token")
        self.assertEqual(kwargs["json"], {"plan_id": "13", "external_id": "cliente-456"})

    def test_plan_change_uses_main_bearer_when_reseller_has_no_bearer(self):
        response = SimpleNamespace(status_code=200)
        response.json = lambda: {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app.requests, "patch", return_value=response) as mocked_patch,
            ):
                app.init_database()
                app.save_main_gestor_bearer("Bearer principal-token")
                result = app.change_gestor_client_plan("13", "cliente-456", "tdscr7milgols")

        self.assertEqual(result["plano"], "Consultoria mensal - R$ 29,90")
        self.assertEqual(result["revenda"], "tdscr7milgols")
        mocked_patch.assert_called_once()
        _, kwargs = mocked_patch.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer principal-token")

    def test_plan_change_rejection_uses_gestor_error_message(self):
        response = SimpleNamespace(status_code=422, text="")
        response.json = lambda: {"message": "Plano nao permitido para este cliente."}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app.requests, "patch", return_value=response),
            ):
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
                app.save_gestor_bearer("revenda1", "Bearer gestor-token")
                with self.assertRaises(HTTPException) as raised:
                    app.change_gestor_client_plan("14", "cliente-456", "Revenda Um")

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "Plano nao permitido para este cliente.")

    def test_plan_change_route_rejects_same_plan_before_gestor(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app, "require_access_token"),
                patch.object(
                    app,
                    "search_line_data",
                    return_value={
                        "id": "1713617",
                        "user_username": "Revenda Junior",
                        "plan_name": "Consultoria bimestral - R$ 49,90",
                    },
                ),
                patch.object(app, "search_payment_data", return_value={"Revenda": "Revenda Junior"}),
                patch.object(app.requests, "patch") as mocked_patch,
            ):
                app.init_database()
                with self.assertRaises(HTTPException) as raised:
                    app.trocar_plano_cliente(
                        app.PlanChangeRequest(
                            telefone="51981451949",
                            login="29144975137",
                            external_id="1713617",
                            plan_id="14",
                            access_token="token",
                        )
                    )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Este cliente ja esta nesse plano.")
        mocked_patch.assert_not_called()

    def test_plan_change_updates_cached_line_plan(self):
        response = SimpleNamespace(status_code=200)
        response.json = lambda: {"ok": True}
        line = {
            "id": "line-123",
            "client_id": "cliente-456",
            "plan_name": "Consultoria mensal - R$ 29,90",
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app.requests, "patch", return_value=response),
            ):
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
                    connection.execute(
                        """
                        INSERT INTO client_lines
                            (source_line_id, phone, phone_key, username, username_key, plan_name, payload_json, synced_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "line-123", "5551981451949", "5551981451949", "cliente1", "1",
                            line["plan_name"], json.dumps(line), now,
                        ),
                    )
                app.save_gestor_bearer("revenda1", "Bearer gestor-token")
                app.change_gestor_client_plan("14", "cliente-456", "Revenda Um")
                found = app.search_line_data("51981451949")

        self.assertEqual(found["plan_name"], "Consultoria bimestral - R$ 49,90")

    def test_plan_change_route_prefers_request_external_id(self):
        response = SimpleNamespace(status_code=200)
        response.json = lambda: {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app, "require_access_token"),
                patch.object(
                    app,
                    "search_line_data",
                    return_value={
                        "id": "stale-line-id",
                        "client_id": "stale-client-id",
                        "user_username": "Revenda Um",
                    },
                ),
                patch.object(app.requests, "patch", return_value=response) as mocked_patch,
            ):
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
                app.save_gestor_bearer("revenda1", "Bearer gestor-token")
                app.trocar_plano_cliente(
                    app.PlanChangeRequest(
                        telefone="51981451949",
                        login="cliente1",
                        external_id="cliente-correto",
                        plan_id="14",
                        access_token="token",
                    )
                )

        _, kwargs = mocked_patch.call_args
        self.assertEqual(kwargs["json"], {"plan_id": "14", "external_id": "cliente-correto"})

    def test_plan_change_route_uses_configured_payment_reseller_when_line_reseller_differs(self):
        response = SimpleNamespace(status_code=200)
        response.json = lambda: {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gestor.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app, "require_access_token"),
                patch.object(
                    app,
                    "search_line_data",
                    return_value={
                        "id": "1713617",
                        "user_username": "Williamfarias",
                    },
                ),
                patch.object(app, "search_payment_data", return_value={"Revenda": "Revenda Junior"}),
                patch.object(app.requests, "patch", return_value=response) as mocked_patch,
            ):
                app.init_database()
                now = datetime.now().isoformat()
                with app.db_connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO resellers
                            (username, display_name, first_seen_at, last_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("Revenda Junior", "Revenda Junior", now, now, now),
                    )
                app.save_gestor_bearer("Revenda Junior", "Bearer token-junior")
                result = app.trocar_plano_cliente(
                    app.PlanChangeRequest(
                        telefone="51981451949",
                        login="29144975137",
                        external_id="1713617",
                        plan_id="14",
                        access_token="token",
                    )
                )

        self.assertEqual(result["revenda"], "Revenda Junior")
        _, kwargs = mocked_patch.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-junior")


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
    def test_client_line_is_searched_in_sqlite_before_remote_api(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "lines.sqlite3"
            line = {
                "id": 123,
                "phone": "555181451949",
                "username": "29144975137",
                "user_username": "Williamfarias",
                "status": "expired",
                "is_enabled": True,
            }
            with patch.object(app, "DB_FILE", database):
                app.init_database()
                with app.db_connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO client_lines
                            (source_line_id, phone, phone_key, username, username_key, payload_json, synced_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "123", line["phone"], line["phone"], line["username"], line["username"],
                            json.dumps(line), datetime.now().isoformat(),
                        ),
                    )
                with patch.object(app, "search_line_data_remote") as remote:
                    found = app.search_line_data("555181451949")

        self.assertEqual(found["user_username"], "Williamfarias")
        remote.assert_not_called()

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

    def test_payment_is_retried_with_line_phone_when_login_search_misses(self):
        line = {
            "id": 123,
            "user_username": "GuiMendes",
            "username": "99886446671",
            "password": "senha1",
            "phone": "+555181451949",
            "is_enabled": True,
            "status": "active",
        }
        payment = {
            "DT_RowId": "row-123",
            "nome": "555181451949",
            "telefone": "+5181451949",
            "Link": "https://pagueaqui.top/exemplo",
        }
        with (
            patch.object(app, "enforce_rate_limit"),
            patch.object(app, "search_payment_data", side_effect=[None, payment]) as payment_search,
            patch.object(app, "search_line_data", return_value=line),
            patch.object(app, "support_contact_for_reseller", return_value=None),
            patch.object(app, "get_app_preference", return_value=None),
            patch.object(app, "get_reminder_days_for_client", return_value=[3, 2, 1, 0]),
            patch.object(app, "save_notification_client"),
            patch.object(app, "record_admin_audit_event"),
            patch.object(app, "ACCESS_TOKEN_SECRET", "test-secret"),
        ):
            response = app.consultar_cliente(
                app.PhoneRequest(telefone="99886446671"),
                SimpleNamespace(),
            )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["cliente"]["link_pagamento"], "https://pagueaqui.top/exemplo")
        self.assertEqual(payment_search.call_args_list[0].args[0], "99886446671")
        self.assertEqual(payment_search.call_args_list[1].args[0], "+555181451949")

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

    def test_line_is_returned_when_payment_lookup_fails(self):
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
            patch.object(app, "search_payment_data", side_effect=HTTPException(status_code=503, detail="pagamento fora")),
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

    def test_line_plan_is_preferred_over_stale_payment_plan(self):
        line = {
            "id": 123,
            "user_username": "revenda1",
            "username": "cliente1",
            "password": "senha1",
            "phone": "+5551981451949",
            "plan_name": "Consultoria bimestral - R$ 49,90",
            "is_enabled": True,
            "status": "active",
        }
        payment = {
            "DT_RowId": "row-123",
            "nome": "cliente1",
            "telefone": "51981451949",
            "plano": "Consultoria mensal - R$ 29,90",
            "Link": "https://pagueaqui.top/exemplo",
        }
        with (
            patch.object(app, "enforce_rate_limit"),
            patch.object(app, "search_payment_data", return_value=payment),
            patch.object(app, "search_line_data", return_value=line),
            patch.object(app, "support_contact_for_reseller", return_value=None),
            patch.object(app, "get_app_preference", return_value=None),
            patch.object(app, "get_reminder_days_for_client", return_value=[3, 2, 1, 0]),
            patch.object(app, "save_notification_client"),
            patch.object(app, "record_admin_audit_event"),
            patch.object(app, "ACCESS_TOKEN_SECRET", "test-secret"),
        ):
            response = app.consultar_cliente(
                app.PhoneRequest(telefone="51981451949"),
                SimpleNamespace(),
            )

        payload = json.loads(response.body)
        self.assertEqual(payload["cliente"]["plano"], "Consultoria bimestral - R$ 49,90")

    def test_unavailable_is_returned_when_both_lookups_fail(self):
        with (
            patch.object(app, "enforce_rate_limit"),
            patch.object(app, "search_payment_data", side_effect=HTTPException(status_code=503, detail="pagamento fora")),
            patch.object(app, "search_line_data", side_effect=HTTPException(status_code=503, detail="linhas fora")),
        ):
            with self.assertRaises(HTTPException) as raised:
                app.consultar_cliente(
                    app.PhoneRequest(telefone="27999999999"),
                    SimpleNamespace(),
                )

        self.assertEqual(raised.exception.status_code, 503)

    def test_remote_line_replaces_expired_cached_line(self):
        expired = {
            "id": "old",
            "username": "553499789416",
            "phone": "+553499789416",
            "exp_date": str(int(datetime(2026, 7, 13, tzinfo=app.timezone.utc).timestamp())),
            "status": "expired",
            "is_enabled": True,
        }
        active = {
            "id": "new",
            "username": "553499789416",
            "phone": "+553499789416",
            "exp_date": str(int(datetime(2026, 8, 18, tzinfo=app.timezone.utc).timestamp())),
            "status": "active",
            "is_enabled": True,
        }
        with (
            patch.object(app, "search_line_data_from_database", return_value=expired),
            patch.object(app, "search_line_data_remote", return_value=active),
        ):
            line = app.search_line_data("553499789416")

        self.assertEqual(line["id"], "new")

    def test_gestor_config_uses_the_best_reseller_over_payment_reseller(self):
        line = {
            "id": 985507,
            "user_username": "tdscr7milgols",
            "username": "554799746483",
            "password": "senha1",
            "phone": "+554799746483",
            "is_enabled": True,
            "status": "active",
        }
        payment = {
            "status": "ok",
            "nome": "554799746483",
            "telefone": "554799746483",
            "Revenda": "Gabriel",
            "Link": "https://pagueaqui.top/exemplo",
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "payment.sqlite3"
            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app, "enforce_rate_limit"),
                patch.object(app, "search_payment_data", return_value=payment),
                patch.object(app, "search_line_data", return_value=line),
                patch.object(app, "support_contact_for_reseller", return_value=None),
                patch.object(app, "get_app_preference", return_value=None),
                patch.object(app, "get_reminder_days_for_client", return_value=[3, 2, 1, 0]),
                patch.object(app, "save_notification_client"),
                patch.object(app, "record_admin_audit_event"),
                patch.object(app, "ACCESS_TOKEN_SECRET", "test-secret"),
            ):
                app.init_database()
                now = datetime.now().isoformat()
                with app.db_connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO resellers
                            (username, display_name, first_seen_at, last_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("tdscr7milgols", "tdscr7milgols", now, now, now),
                    )
                app.save_gestor_bearer("tdscr7milgols", "Bearer token-tds")
                response = app.consultar_cliente(
                    app.PhoneRequest(telefone="554799746483"),
                    SimpleNamespace(),
                )

        payload = json.loads(response.body)
        self.assertEqual(payload["cliente"]["revenda"], "Gabriel")
        self.assertEqual(payload["cliente"]["gestor_revenda"], "tdscr7milgols")
        self.assertTrue(payload["cliente"]["gestor_configurado"])


class ReminderPreferenceTests(unittest.TestCase):
    def test_legacy_json_files_are_mirrored_to_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database = base / "mirror.sqlite3"
            subscriptions_file = base / "subscriptions.json"
            clients_file = base / "clients.json"
            sent_file = base / "sent.json"
            contacts_file = base / "contacts.json"
            app.write_json_file(
                subscriptions_file,
                {"5551999999999": {"telefone": "51999999999", "subscription": {"endpoint": "https://push.test"}, "reminder_days": [3, 1]}},
            )
            app.write_json_file(
                clients_file,
                {"5551999999999": {"telefone": "51999999999", "login": "cliente", "vencimento": "30/06/2026"}},
            )
            app.write_json_file(sent_file, {"5551999999999:2026-06-30:3": "2026-06-27T12:00:00+00:00"})
            app.write_json_file(contacts_file, {"default": "5551999999999", "revendas": {"revenda1": "5551888888888"}})

            with (
                patch.object(app, "DB_FILE", database),
                patch.object(app, "SUBSCRIPTIONS_FILE", subscriptions_file),
                patch.object(app, "CLIENTS_FILE", clients_file),
                patch.object(app, "SENT_REMINDERS_FILE", sent_file),
                patch.object(app, "SUPPORT_CONTACTS_FILE", contacts_file),
            ):
                app.init_database()
                with app.db_connect() as connection:
                    counts = {
                        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in ("push_subscriptions", "notification_clients", "sent_reminders", "support_contacts")
                    }

        self.assertEqual(counts["push_subscriptions"], 1)
        self.assertEqual(counts["notification_clients"], 1)
        self.assertEqual(counts["sent_reminders"], 1)
        self.assertEqual(counts["support_contacts"], 2)

    def test_reminder_days_are_normalized(self):
        self.assertEqual(app.normalize_reminder_days([0, 3, 3, 9, "2", 1]), [3, 2, 1, 0])
        self.assertEqual(app.normalize_reminder_days([]), [])
        self.assertEqual(app.normalize_reminder_days(), [3, 2, 1, 0])

    def test_active_reminders_require_a_subscription_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            subscriptions_file = base / "subscriptions.json"
            database = base / "reminders.sqlite3"
            phone = "5551999999999"
            app.write_json_file(
                subscriptions_file,
                {
                    phone: {"subscription": {"endpoint": "https://example.test/push"}},
                    "5551888888888": {"subscription": {}},
                },
            )
            with (
                patch.object(app, "SUBSCRIPTIONS_FILE", subscriptions_file),
                patch.object(app, "DB_FILE", database),
            ):
                app.init_database()
                self.assertTrue(app.has_active_reminders_for_client(phone))
                self.assertFalse(app.has_active_reminders_for_client("5551888888888"))

    def test_reminders_only_send_on_selected_days(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            clients_file = base / "clients.json"
            subscriptions_file = base / "subscriptions.json"
            sent_file = base / "sent.json"
            database = base / "reminders.sqlite3"
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
                patch.object(app, "DB_FILE", database),
                patch.object(app, "send_push", return_value=True) as send_push,
            ):
                app.init_database()
                skipped = app.check_and_send_reminders()
                self.assertEqual(skipped["sent"], 0)
                send_push.assert_not_called()

                with app.db_connect() as connection:
                    connection.execute(
                        "UPDATE push_subscriptions SET reminder_days_json = '[3]' WHERE lookup_key = ?",
                        (phone,),
                    )
                delivered = app.check_and_send_reminders()
                with app.db_connect() as connection:
                    sent_count = connection.execute("SELECT COUNT(*) FROM sent_reminders").fetchone()[0]

        self.assertEqual(delivered["sent"], 1)
        self.assertEqual(sent_count, 1)


if __name__ == "__main__":
    unittest.main()
