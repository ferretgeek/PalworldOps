from __future__ import annotations

import hashlib
import http.client
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT_HANDLE = tempfile.TemporaryDirectory(prefix="palworld-ops-tests-")
TEST_ROOT = Path(TEST_ROOT_HANDLE.name)
os.environ.update({
    "PALWORLD_ROOT": str(TEST_ROOT / "runtime"),
    "PALWORLD_MANAGER": str(REPO_ROOT / "palworldctl.py"),
    "PALWORLD_PANEL_STATIC": str(REPO_ROOT / "panel"),
    "PALWORLD_PANEL_SESSION_STORE": str(TEST_ROOT / "sessions.json"),
    "PALWORLD_PANEL_PERFORMANCE_DB": str(TEST_ROOT / "performance.sqlite3"),
    "PALWORLD_PANEL_HOST": "127.0.0.1",
    "PALWORLD_PANEL_SECURE_COOKIE": "false",
})

SPEC = importlib.util.spec_from_file_location("palworld_panel_test", REPO_ROOT / "palworld-panel.py")
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import contract
    raise RuntimeError("cannot load palworld-panel.py")
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)
manager = panel.manager


def write_settings(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(ServerName="Synthetic, World",ServerPlayerMaxNum=16,'
        "bUseAuth=True,RESTAPIPort=8212,DropItemAliveMaxHours=1.000000)\n",
        encoding="utf-8",
    )


def build_backup(path: Path, *, unsafe_name: str | None = None) -> None:
    files = {
        "SaveGames/0/SYNTHETIC_WORLD/Level.sav": b"synthetic-save",
        "Config/LinuxServer/PalWorldSettings.ini": b"synthetic-config",
    }
    if unsafe_name is not None:
        files = {unsafe_name: b"unsafe"}
    records = []
    combined = hashlib.sha256()
    for name, payload in files.items():
        digest = hashlib.sha256(payload).hexdigest()
        records.append({"path": name, "size": len(payload), "sha256": digest})
        combined.update(name.encode("utf-8"))
        combined.update(b"\0")
        combined.update(str(len(payload)).encode("ascii"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
    metadata = {
        "schema": 1,
        "kind": "manual",
        "created_at": "2026-08-10T00:00:00+00:00",
        "source_bytes": sum(len(payload) for payload in files.values()),
        "fingerprint": combined.hexdigest(),
        "files": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        meta_payload = json.dumps(metadata).encode("utf-8")
        meta_info = tarfile.TarInfo("palworld-backup.json")
        meta_info.size = len(meta_payload)
        archive.addfile(meta_info, io.BytesIO(meta_payload))
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="manager-", dir=TEST_ROOT)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_parse_settings_preserves_quoted_commas_and_types(self) -> None:
        config = self.root / "PalWorldSettings.ini"
        write_settings(config)
        _text, _start, _end, pairs = manager.parse_settings(config)
        values = dict(pairs)
        self.assertEqual(values["ServerName"], '"Synthetic, World"')
        self.assertEqual(values["ServerPlayerMaxNum"], "16")
        self.assertEqual(values["bUseAuth"], "True")

    def test_setting_validation_rejects_unsafe_or_out_of_range_values(self) -> None:
        self.assertEqual(manager.validate_setting_value("bUseAuth", "yes", "False"), "True")
        self.assertEqual(manager.validate_setting_value("ServerName", "合成世界", '"old"'), '"合成世界"')
        self.assertEqual(manager.validate_setting_value("RESTAPIPort", "8212", "8212"), "8212")
        with self.assertRaises(manager.ManagerError):
            manager.validate_setting_value("RESTAPIPort", "70000", "8212")
        with self.assertRaises(manager.ManagerError):
            manager.validate_setting_value("ServerName", "line\nbreak", '"old"')
        with self.assertRaises(manager.ManagerError):
            manager.validate_setting_value("List", "(ok;rm)", "()")

    def test_render_setting_changes_is_bounded_and_preserves_previous_values(self) -> None:
        config = manager.CONFIG
        defaults = manager.DEFAULT_CONFIG
        write_settings(config)
        write_settings(defaults)
        updated, canonical, previous = manager.render_setting_changes({"ServerPlayerMaxNum": "20"})
        self.assertIn("ServerPlayerMaxNum=20", updated)
        self.assertEqual(canonical, {"ServerPlayerMaxNum": "20"})
        self.assertEqual(previous, {"ServerPlayerMaxNum": "16"})
        with self.assertRaises(manager.ManagerError):
            manager.render_setting_changes({"Unknown": "1"})
        with self.assertRaises(manager.ManagerError):
            manager.render_setting_changes({"AdminPassword": "placeholder"})

    def test_backup_validation_and_safe_extraction(self) -> None:
        archive = self.root / "valid.tar.gz"
        build_backup(archive)
        result = manager.verify_archive(archive)
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["kind"], "manual")
        destination = self.root / "extracted"
        manager.safe_extract(archive, destination)
        self.assertEqual(
            (destination / "SaveGames/0/SYNTHETIC_WORLD/Level.sav").read_bytes(),
            b"synthetic-save",
        )

    def test_backup_rejects_traversal_and_unknown_roots(self) -> None:
        for name in ("../escape", "Other/file.bin", "/absolute/file.bin"):
            with self.subTest(name=name), self.assertRaises(manager.ManagerError):
                manager.validate_member_name(name)

    def test_atomic_json_round_trip(self) -> None:
        target = self.root / "state" / "health.json"
        manager.atomic_json(target, {"ok": True, "count": 2})
        self.assertEqual(manager.load_json(target, None), {"ok": True, "count": 2})
        self.assertFalse(list(target.parent.glob(".*.tmp")))

    def test_resolve_archive_cannot_escape_managed_root(self) -> None:
        managed = self.root / "managed"
        valid = managed / "manual" / "valid.tar.gz"
        build_backup(valid)
        old_managed = manager.MANAGED
        manager.MANAGED = managed
        try:
            self.assertEqual(manager.resolve_archive("manual/valid.tar.gz"), valid.resolve())
            with self.assertRaises(manager.ManagerError):
                manager.resolve_archive("../outside.tar.gz")
        finally:
            manager.MANAGED = old_managed


class PanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="panel-", dir=TEST_ROOT)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_redaction_and_decoding(self) -> None:
        value = panel.redact("AdminPassword=secret Authorization: Bearer-token sentry_key=abc123", None)
        self.assertNotIn("secret", value)
        self.assertNotIn("Bearer-token", value)
        self.assertNotIn("abc123", value)
        self.assertTrue(panel.decode_setting("True"))
        self.assertEqual(panel.decode_setting('"text"'), "text")
        self.assertEqual(panel.decode_setting("12.5"), 12.5)

    def test_sessions_persist_only_hashes_and_rate_limit_failures(self) -> None:
        state = self.root / "sessions.json"
        sessions = panel.Sessions(state)
        token = sessions.create("192.0.2.10", remember=True)
        self.assertTrue(sessions.valid(token))
        saved = state.read_text(encoding="utf-8")
        self.assertNotIn(token, saved)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), saved)
        for _ in range(8):
            sessions.failed("192.0.2.20")
        self.assertFalse(sessions.login_allowed("192.0.2.20"))
        sessions.remove(token)
        self.assertFalse(sessions.valid(token))

    def test_runtime_security_requires_https_for_non_loopback_and_strong_password(self) -> None:
        secret = self.root / "admin-password"
        secret.write_text("a-strong-synthetic-password\n", encoding="utf-8")
        old_secret, old_host, old_secure = manager.SECRET, panel.HOST, panel.SECURE_COOKIE
        manager.SECRET = secret
        try:
            panel.HOST, panel.SECURE_COOKIE = "127.0.0.1", False
            panel.validate_runtime_security()
            panel.HOST, panel.SECURE_COOKIE = "0.0.0.0", False
            with self.assertRaises(RuntimeError):
                panel.validate_runtime_security()
            panel.SECURE_COOKIE = True
            panel.validate_runtime_security()
            secret.write_text("short\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                panel.validate_runtime_security()
        finally:
            manager.SECRET, panel.HOST, panel.SECURE_COOKIE = old_secret, old_host, old_secure

    def test_selected_backup_and_restore_confirmation_are_scoped(self) -> None:
        managed = self.root / "managed"
        backup = managed / "manual" / "valid.tar.gz"
        build_backup(backup)
        old_managed = manager.MANAGED
        manager.MANAGED = managed
        try:
            self.assertEqual(panel.selected_backup("manual/valid.tar.gz"), backup.resolve())
            with self.assertRaises(manager.ManagerError):
                panel.selected_backup("../valid.tar.gz")
            with self.assertRaises(manager.ManagerError):
                panel.action_callback(
                    "restore-backup",
                    {"backup": "manual/valid.tar.gz", "confirmation": "RESTORE:wrong"},
                )
        finally:
            manager.MANAGED = old_managed

    def test_http_authentication_headers_and_static_assets(self) -> None:
        secret = self.root / "admin-password"
        password = "synthetic-password-0001"
        secret.write_text(password, encoding="utf-8")
        old_secret, old_sessions, old_secure = manager.SECRET, panel.SESSIONS, panel.SECURE_COOKIE
        manager.SECRET = secret
        panel.SESSIONS = panel.Sessions(self.root / "http-sessions.json")
        panel.SECURE_COOKIE = True
        server = panel.PanelServer(("127.0.0.1", 0), panel.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401)

            body = json.dumps({"username": "admin", "password": password, "remember": True})
            connection.request("POST", "/api/login", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            cookie = response.getheader("Set-Cookie")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertIn("Secure", cookie)
            token_cookie = cookie.split(";", 1)[0]

            connection.request(
                "POST",
                "/api/action",
                json.dumps({"action": "unknown"}),
                {"Content-Type": "application/json", "Cookie": token_cookie},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            for asset, content_type in (
                ("/favicon.svg", "image/svg+xml"),
                ("/favicon.ico", "image/x-icon"),
                ("/site.webmanifest", "application/manifest+json"),
            ):
                with self.subTest(asset=asset):
                    connection.request("GET", asset)
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn(content_type, response.getheader("Content-Type"))
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            manager.SECRET, panel.SESSIONS, panel.SECURE_COOKIE = old_secret, old_sessions, old_secure


if __name__ == "__main__":
    unittest.main()
