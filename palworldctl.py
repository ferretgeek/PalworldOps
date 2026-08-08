#!/usr/bin/env python3
"""Small, dependency-free Palworld dedicated-server manager."""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
from decimal import Decimal, InvalidOperation
import getpass
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows syntax checks only
    fcntl = None

try:
    import pwd
except ImportError:  # pragma: no cover - Windows syntax checks only
    pwd = None  # type: ignore[assignment]


APP_ID = 2394010
SERVICE = os.environ.get("PALWORLD_SERVICE", "palworld.service")
BASE = Path(os.environ.get("PALWORLD_ROOT", "/opt/palworld"))
SERVER = BASE / "server"
SAVED = SERVER / "Pal" / "Saved"
CONFIG = SAVED / "Config" / "LinuxServer" / "PalWorldSettings.ini"
DEFAULT_CONFIG = SERVER / "DefaultPalWorldSettings.ini"
STEAMCMD = BASE / "steamcmd" / "steamcmd.sh"
MANIFEST = SERVER / "steamapps" / f"appmanifest_{APP_ID}.acf"
SECRET = BASE / "secrets" / "admin-password"
BACKUPS = BASE / "backups"
MANAGED = BACKUPS / "managed"
CONFIG_HISTORY = BACKUPS / "config-history"
STATE = BASE / "state"
LOCK_FILE = STATE / "maintenance.lock"
MANUAL_STOP = STATE / "manual-stop.json"
PERFORMANCE_DB = STATE / "performance-history.sqlite3"
API_USER = "admin"
MIN_FREE_BYTES = 50 * 1024**3
BACKUP_CAP_BYTES = 15 * 1024**3
MEMORY_RESTART_BYTES = 8 * 1024**3
MEMORY_RESTART_COOLDOWN_SECONDS = 6 * 3600
UPDATE_DIRECT_CHECK_SECONDS = 6 * 3600
RETENTION = {
    "daily": 14,
    "weekly": 8,
    "monthly": 3,
    "event": 10,
    "update": 5,
    "manual": 10,
}
SENSITIVE_KEYS = {"AdminPassword", "ServerPassword"}
RESTORE_PRESERVE_KEYS = {
    "AdminPassword", "ServerPassword", "PublicPort", "PublicIP", "RCONEnabled", "RCONPort",
    "RESTAPIEnabled", "RESTAPIPort", "bUseAuth", "BanListURL", "LogFormatType", "bIsMultiplay",
}


class ManagerError(RuntimeError):
    pass


class LockBusy(ManagerError):
    pass


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def human_size(value: int | None) -> str:
    if value is None:
        return "未知"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise ManagerError("此操作需要 root：请在命令前加 sudo")


def palworld_ids() -> tuple[int, int] | None:
    if pwd is None:
        return None
    try:
        entry = pwd.getpwnam("palworld")
        return entry.pw_uid, entry.pw_gid
    except KeyError:
        return None


def give_to_palworld(path: Path) -> None:
    ids = palworld_ids()
    if ids is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(path, *ids)


def ensure_dir(path: Path, mode: int = 0o750) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
        give_to_palworld(path)
    except PermissionError:
        pass


def atomic_json(path: Path, value: Any) -> None:
    ensure_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        give_to_palworld(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        return default


def record_performance_event(
    kind: str,
    title: str,
    detail: str = "",
    *,
    metadata: Mapping[str, Any] | None = None,
    dedupe_key: str | None = None,
    timestamp: int | None = None,
    end_timestamp: int | None = None,
) -> None:
    """Best-effort event logging shared with the performance timeline.

    Management actions must never fail solely because the optional history
    database is temporarily busy or unavailable.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kind):
        return
    try:
        ensure_dir(STATE, 0o700)
        with sqlite3.connect(PERFORMANCE_DB, timeout=3) as connection:
            connection.execute("PRAGMA busy_timeout=3000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    ts INTEGER NOT NULL,
                    end_ts INTEGER,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manager',
                    title TEXT NOT NULL,
                    detail TEXT,
                    metadata_json TEXT,
                    dedupe_key TEXT UNIQUE
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts)")
            connection.execute(
                """
                INSERT OR IGNORE INTO events
                    (ts, end_ts, kind, source, title, detail, metadata_json, dedupe_key)
                VALUES (?, ?, ?, 'manager', ?, ?, ?, ?)
                """,
                (
                    int(timestamp or time.time()),
                    int(end_timestamp) if end_timestamp is not None else None,
                    kind,
                    title[:160],
                    detail[:1000] or None,
                    json.dumps(dict(metadata or {}), ensure_ascii=False, separators=(",", ":")),
                    dedupe_key[:240] if dedupe_key else None,
                ),
            )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"警告：性能事件记录失败（{type(exc).__name__}）", file=sys.stderr)


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: int | None = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagerError(f"命令执行失败：{args[0]}（{exc}）") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f"：{detail[-1]}" if detail else ""
        raise ManagerError(f"命令失败：{args[0]}，退出码 {result.returncode}{suffix}")
    return result


@contextlib.contextmanager
def maintenance_lock(nonblocking: bool = False) -> Iterator[None]:
    ensure_dir(STATE)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o660)
    try:
        give_to_palworld(LOCK_FILE)
        if fcntl is not None:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as exc:
                raise LockBusy("已有备份、更新、恢复或维护任务在运行") from exc
        yield
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def service_active() -> bool:
    return run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0


def start_service(
    *,
    restart: bool = False,
    check: bool = True,
    capture: bool = False,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Start intentionally without inheriting an exhausted crash-loop budget.

    systemd counts successful manual starts toward StartLimitBurst as well as
    failed crash-loop restarts.  A verified maintenance workflow can therefore
    hit ``start-limit-hit`` after several deliberate start/stop cycles.  Reset
    only the accumulated failure/start counter immediately before a requested
    start; the unit's runtime Restart=on-failure limit remains unchanged.
    """
    run(["systemctl", "reset-failed", SERVICE], check=False)
    action = "restart" if restart else "start"
    return run(
        ["systemctl", action, SERVICE],
        check=check,
        capture=capture,
        timeout=timeout,
    )


def service_value(name: str) -> str:
    result = run(["systemctl", "show", SERVICE, "--property", name, "--value"], check=False)
    return (result.stdout or "").strip()


def service_pids() -> list[int]:
    """Return every live PID in the service cgroup, including wrapper children."""
    control_group = service_value("ControlGroup")
    if control_group:
        try:
            values = (Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs").read_text().split()
            return [int(value) for value in values if value.isdigit() and int(value) > 0]
        except OSError:
            pass
    try:
        pid = int(service_value("MainPID"))
        return [pid] if pid > 0 else []
    except ValueError:
        return []


def game_pid() -> int | None:
    """Find the Unreal game process instead of PalServer.sh's shell wrapper."""
    fallback: int | None = None
    for pid in service_pids():
        fallback = fallback or pid
        try:
            command = (Path("/proc") / str(pid) / "comm").read_text().strip()
        except OSError:
            continue
        if command.startswith("PalServer-Linux"):
            return pid
    return fallback


def process_age(pid: int | None = None) -> float | None:
    try:
        selected = pid or game_pid()
        if selected is None or selected <= 0:
            return None
        # The second field is parenthesized and may contain spaces. Split after it
        # before reading field 22 (starttime), which is index 19 in the tail.
        tail = (Path("/proc") / str(selected) / "stat").read_text().rsplit(") ", 1)[1].split()
        ticks = int(tail[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return max(0.0, uptime - ticks / os.sysconf("SC_CLK_TCK"))
    except (ValueError, IndexError, OSError):
        return None


def manual_stop_active() -> bool:
    return MANUAL_STOP.is_file()


def udp_listening() -> bool:
    result = run(["ss", "-H", "-lun"], check=False)
    return any(re.search(r"(?:^|[\[\]:.])8211(?:\s|$)", line) for line in (result.stdout or "").splitlines())


def parse_settings(path: Path = CONFIG) -> tuple[str, int, int, list[tuple[str, str]]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ManagerError(f"无法读取配置：{path}") from exc
    marker = "OptionSettings=("
    marker_pos = text.find(marker)
    if marker_pos < 0:
        raise ManagerError("配置中缺少 OptionSettings")
    open_pos = marker_pos + len(marker) - 1
    depth = 1
    quoted = False
    escaped = False
    close_pos = -1
    for index in range(open_pos + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    close_pos = index
                    break
    if close_pos < 0:
        raise ManagerError("OptionSettings 括号不完整")
    content = text[open_pos + 1 : close_pos]
    items: list[str] = []
    start = 0
    depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(content):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                items.append(content[start:index])
                start = index + 1
    items.append(content[start:])
    pairs: list[tuple[str, str]] = []
    for item in items:
        if not item.strip():
            continue
        if "=" not in item:
            raise ManagerError(f"无法解析配置项：{item[:40]}")
        key, value = item.split("=", 1)
        pairs.append((key.strip(), value.strip()))
    return text, open_pos + 1, close_pos, pairs


def settings_map(path: Path = CONFIG) -> dict[str, str]:
    return dict(parse_settings(path)[3])


def api_port() -> int:
    try:
        return int(settings_map().get("RESTAPIPort", "8212"))
    except (ManagerError, ValueError):
        return 8212


def api_request(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    try:
        password = SECRET.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ManagerError("REST API 管理密码文件不可读") from exc
    if not password:
        raise ManagerError("REST API 管理密码为空")
    token = base64.b64encode(f"{API_USER}:{password}".encode()).decode("ascii")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{api_port()}/v1/api/{path.lstrip('/')}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = response.read()
    except HTTPError as exc:
        raise ManagerError(f"REST API 返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ManagerError("REST API 当前不可用") from exc
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload.decode("utf-8", errors="replace")


def metrics_or_none() -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = api_request("metrics")
        return (value if isinstance(value, dict) else {}), None
    except ManagerError as exc:
        return None, str(exc)


def request_world_save(wait_seconds: int = 3) -> bool:
    if not service_active():
        return False
    try:
        api_request("save", method="POST")
        time.sleep(wait_seconds)
        record_performance_event(
            "save",
            "世界存档已保存",
            "通过 Palworld 管理接口完成一致性保存",
            dedupe_key=f"save:{int(time.time())}:{os.getpid()}",
        )
        return True
    except ManagerError as exc:
        print(f"警告：{exc}；继续创建尽力而为的备份", file=sys.stderr)
        return False


def collect_save_files() -> list[Path]:
    savegames = SAVED / "SaveGames"
    if not savegames.is_dir():
        raise ManagerError(f"活动存档目录不存在：{savegames}")
    files: list[Path] = []
    for top_name in ("SaveGames", "Config"):
        top = SAVED / top_name
        if not top.is_dir():
            continue
        for path in sorted(top.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(SAVED)
            lowered = {part.lower() for part in relative.parts}
            # Palworld 1.0 rotates both the long-lived ``backup`` tree and a
            # short-lived ``world_save_bak`` snapshot while saving.  Neither
            # belongs in a managed backup; the latter can disappear between
            # enumeration and hashing/archiving and used to fail the daily job.
            if top_name == "SaveGames" and lowered.intersection(
                {"backup", "world_save_bak"}
            ):
                continue
            if "logs" in lowered or "crashes" in lowered:
                continue
            files.append(path)
    if not any(path.name == "Level.sav" for path in files):
        raise ManagerError("活动存档中未找到 Level.sav，拒绝创建无效备份")
    return files


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_files(files: list[Path]) -> tuple[list[dict[str, Any]], str, int]:
    records: list[dict[str, Any]] = []
    combined = hashlib.sha256()
    total = 0
    for path in files:
        stat_result = path.stat()
        relative = path.relative_to(SAVED).as_posix()
        digest = file_digest(path)
        record = {"path": relative, "size": stat_result.st_size, "sha256": digest}
        records.append(record)
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(str(stat_result.st_size).encode("ascii"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        total += stat_result.st_size
    return records, combined.hexdigest(), total


def managed_archives() -> list[Path]:
    if not MANAGED.exists():
        return []
    found: list[tuple[float, Path]] = []
    for path in MANAGED.glob("*/*.tar.gz"):
        try:
            if path.is_file():
                found.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            # Retention may remove a file while another request is listing it.
            continue
    return [path for _, path in sorted(found, key=lambda item: item[0])]


def prune_managed(apply: bool = True) -> dict[str, int]:
    archives = managed_archives()
    to_delete: set[Path] = set()
    by_kind: dict[str, list[Path]] = {}
    for archive in archives:
        by_kind.setdefault(archive.parent.name, []).append(archive)
    for kind, paths in by_kind.items():
        keep = RETENTION.get(kind, 5)
        to_delete.update(paths[:-keep] if keep > 0 else paths)
    remaining = [path for path in archives if path not in to_delete]
    newest_by_kind = {paths[-1] for paths in by_kind.values() if paths}

    def remaining_size() -> int:
        return sum(path.stat().st_size for path in remaining if path not in to_delete and path.exists())

    def projected_free() -> int:
        return shutil.disk_usage(BASE).free + sum(
            path.stat().st_size for path in to_delete if path.exists()
        )

    while remaining_size() > BACKUP_CAP_BYTES or projected_free() < MIN_FREE_BYTES:
        candidates = [
            path for path in remaining if path not in to_delete and path not in newest_by_kind and path.exists()
        ]
        if len([path for path in remaining if path not in to_delete]) <= 3 or not candidates:
            break
        to_delete.add(min(candidates, key=lambda p: p.stat().st_mtime))
    deleted_bytes = sum(path.stat().st_size for path in to_delete if path.exists())
    if apply:
        for path in sorted(to_delete):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        directories = MANAGED.glob("*") if MANAGED.exists() else []
        for directory in directories:
            with contextlib.suppress(OSError):
                directory.rmdir()
    return {"files": len(to_delete), "bytes": deleted_bytes}


def validate_member_name(name: str) -> None:
    posix = PurePosixPath(name)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise ManagerError(f"备份包含不安全路径：{name}")
    if posix.parts[0] not in {"palworld-backup.json", "SaveGames", "Config"}:
        raise ManagerError(f"备份包含未知顶层路径：{name}")


def verify_archive(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ManagerError(f"备份不存在：{path}")
    unpacked = 0
    has_level = False
    has_config = False
    metadata: dict[str, Any] | None = None
    actual: dict[str, dict[str, Any]] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                validate_member_name(member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise ManagerError(f"备份包含不允许的链接或设备：{member.name}")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ManagerError(f"备份包含未知条目类型：{member.name}")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ManagerError(f"无法读取备份条目：{member.name}")
                if member.name == "palworld-backup.json":
                    if metadata is not None:
                        raise ManagerError("备份包含重复元数据")
                    if member.size > 16 * 1024**2:
                        raise ManagerError("备份元数据异常过大")
                    try:
                        metadata = json.loads(extracted.read())
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ManagerError("备份元数据损坏") from exc
                else:
                    if member.name in actual:
                        raise ManagerError(f"备份包含重复文件：{member.name}")
                    digest = hashlib.sha256()
                    read_bytes = 0
                    for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                        digest.update(chunk)
                        read_bytes += len(chunk)
                    if read_bytes != member.size:
                        raise ManagerError(f"备份条目大小不一致：{member.name}")
                    actual[member.name] = {"size": member.size, "sha256": digest.hexdigest()}
                    unpacked += member.size
                    has_level = has_level or member.name.endswith("/Level.sav")
                    has_config = has_config or member.name.startswith("Config/")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ManagerError(f"备份校验失败：{path.name}") from exc
    if not isinstance(metadata, dict) or metadata.get("schema") != 1:
        raise ManagerError("备份缺少受支持的元数据")
    if not has_level or not has_config:
        raise ManagerError("备份缺少 Level.sav 或服务器配置")
    records = metadata.get("files")
    if not isinstance(records, list):
        raise ManagerError("备份元数据缺少文件清单")
    expected: dict[str, dict[str, Any]] = {}
    combined = hashlib.sha256()
    expected_source_bytes = 0
    for record in records:
        if not isinstance(record, dict):
            raise ManagerError("备份文件清单格式无效")
        name = record.get("path")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(name, str) or not isinstance(size, int) or size < 0:
            raise ManagerError("备份文件清单条目无效")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManagerError("备份文件清单哈希无效")
        validate_member_name(name)
        if name in expected:
            raise ManagerError(f"备份文件清单包含重复项：{name}")
        expected[name] = {"size": size, "sha256": digest}
        combined.update(name.encode("utf-8"))
        combined.update(b"\0")
        combined.update(str(size).encode("ascii"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        expected_source_bytes += size
    if set(expected) != set(actual):
        raise ManagerError("备份内容与文件清单不一致")
    for name, record in expected.items():
        if actual[name] != record:
            raise ManagerError(f"备份文件哈希或大小不一致：{name}")
    if metadata.get("fingerprint") != combined.hexdigest():
        raise ManagerError("备份整体指纹不一致")
    if metadata.get("source_bytes") != expected_source_bytes:
        raise ManagerError("备份源大小记录不一致")
    return {
        "path": str(path),
        "kind": metadata.get("kind"),
        "created_at": metadata.get("created_at"),
        "fingerprint": metadata.get("fingerprint"),
        "files": len(actual),
        "unpacked_bytes": unpacked,
        "archive_bytes": path.stat().st_size,
    }


def create_backup(kind: str, if_changed: bool = False, request_save: bool = True) -> Path | None:
    if kind not in RETENTION:
        raise ManagerError(f"不支持的备份类别：{kind}")
    save_requested = request_world_save() if request_save else False
    files = collect_save_files()
    records, fingerprint, source_size = describe_files(files)
    last = load_json(STATE / "last-backup.json", {})
    if if_changed and last.get("fingerprint") == fingerprint:
        print("存档未变化，跳过重复备份")
        return None
    prune_managed(apply=True)
    free = shutil.disk_usage(BASE).free
    required_headroom = source_size * 2 + 64 * 1024**2
    if free - required_headroom < MIN_FREE_BYTES:
        raise ManagerError(
            f"可用空间不足以安全备份（可用 {human_size(free)}，必须保留 {human_size(MIN_FREE_BYTES)}）"
        )
    directory = MANAGED / kind
    ensure_dir(directory)
    target = directory / f"palworld-{kind}-{stamp()}-{fingerprint[:10]}.tar.gz"
    partial = target.with_name(f".{target.name}.partial")
    metadata = {
        "schema": 1,
        "created_at": iso_now(),
        "kind": kind,
        "fingerprint": fingerprint,
        "source_bytes": source_size,
        "consistent_save_requested": save_requested,
        "files": records,
    }
    payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        with tarfile.open(partial, "w:gz", compresslevel=6) as archive:
            info = tarfile.TarInfo("palworld-backup.json")
            info.size = len(payload)
            info.mode = 0o600
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(payload))
            for path in files:
                archive.add(path, arcname=path.relative_to(SAVED).as_posix(), recursive=False)
        os.chmod(partial, 0o600)
        os.replace(partial, target)
        give_to_palworld(target)
        verified = verify_archive(target)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        raise
    state = {
        "created_at": metadata["created_at"],
        "kind": kind,
        "fingerprint": fingerprint,
        "path": str(target),
        "archive_bytes": verified["archive_bytes"],
        "verified": True,
    }
    atomic_json(STATE / "last-backup.json", state)
    prune_managed(apply=True)
    record_performance_event(
        "backup",
        f"{kind} 备份完成",
        f"{target.name} · {human_size(verified['archive_bytes'])} · 已完整校验",
        metadata={
            "kind": kind,
            "path": str(target),
            "archive_bytes": verified["archive_bytes"],
            "fingerprint": fingerprint,
        },
        dedupe_key=f"backup:{target.name}",
    )
    print(f"备份完成并校验通过：{target}（{human_size(target.stat().st_size)}）")
    return target


def backup_config_file() -> Path:
    ensure_dir(CONFIG_HISTORY)
    target = CONFIG_HISTORY / f"PalWorldSettings-{stamp()}.ini"
    shutil.copy2(CONFIG, target)
    target.chmod(0o600)
    give_to_palworld(target)
    paths = sorted(CONFIG_HISTORY.glob("PalWorldSettings-*.ini"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in paths[20:]:
        old.unlink()
    return target


def installed_build() -> int:
    try:
        text = MANIFEST.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ManagerError(f"Steam 构建清单不可读：{MANIFEST}") from exc
    match = re.search(r'"buildid"\s+"(\d+)"', text)
    if not match:
        raise ManagerError("Steam 构建清单中未找到 buildid")
    return int(match.group(1))


def query_update_web(local: int) -> dict[str, Any]:
    url = "https://api.steampowered.com/ISteamApps/UpToDateCheck/v1/?" + urlencode(
        {"appid": APP_ID, "version": local}
    )
    try:
        with urlopen(url, timeout=20) as response:
            payload = json.load(response)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ManagerError("无法查询 Steam 最新构建") from exc
    data = payload.get("response", {})
    if data.get("success") is not True or "up_to_date" not in data:
        raise ManagerError("Steam 更新查询返回了无法识别的数据")
    return {
        "checked_at": iso_now(),
        "installed_build": local,
        "up_to_date": bool(data["up_to_date"]),
        "required_build": data.get("required_version"),
        "query_source": "steam_web_api",
    }


def query_update_steamcmd(local: int) -> dict[str, Any]:
    command = [
        str(STEAMCMD), "+login", "anonymous", "+app_info_update", "1",
        "+app_info_print", str(APP_ID), "+quit",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command = ["runuser", "-u", "palworld", "--", "timeout", "120", *command]
    result = run(command, timeout=135)
    match = re.search(
        r'"branches"\s*\{\s*"public"\s*\{.*?"buildid"\s*"(\d+)"',
        result.stdout or "",
        re.DOTALL,
    )
    if not match:
        raise ManagerError("SteamCMD 未返回 public 分支构建号")
    required = int(match.group(1))
    return {
        "checked_at": iso_now(),
        "installed_build": local,
        "up_to_date": local >= required,
        "required_build": required,
        "query_source": "steamcmd_public_branch",
    }


def query_update(*, automatic: bool = False) -> dict[str, Any]:
    """Query Steam with a cheap API path plus a periodic authoritative fallback."""
    local = installed_build()
    web_info: dict[str, Any] | None = None
    web_error: ManagerError | None = None
    try:
        web_info = query_update_web(local)
    except ManagerError as exc:
        web_error = exc

    direct_state = load_json(STATE / "update-query.json", {})
    try:
        last_direct = float(direct_state.get("last_direct_epoch", 0))
    except (TypeError, ValueError):
        last_direct = 0
    direct_due = time.time() - last_direct >= UPDATE_DIRECT_CHECK_SECONDS
    direct_needed = not automatic or web_info is None or not web_info["up_to_date"] or direct_due
    if direct_needed:
        try:
            direct = query_update_steamcmd(local)
            with contextlib.suppress(OSError, PermissionError):
                atomic_json(STATE / "update-query.json", {
                    "last_direct_epoch": time.time(),
                    "last_direct_at": direct["checked_at"],
                    "required_build": direct["required_build"],
                    "result": "ok",
                })
            if web_error:
                direct["query_warning"] = str(web_error)
            return direct
        except ManagerError as direct_error:
            if web_info is None:
                raise ManagerError(
                    f"无法查询 Steam 最新构建（Web API 与 SteamCMD 均失败：{direct_error}）"
                ) from direct_error
            web_info["query_warning"] = str(direct_error)
            return web_info
    if web_info is None:
        raise web_error or ManagerError("无法查询 Steam 最新构建")
    return web_info


def wait_for_server(timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_active() and udp_listening():
            metrics, _ = metrics_or_none()
            if metrics is not None:
                try:
                    if float(metrics.get("serverfps", 0)) > 0:
                        return True
                except (TypeError, ValueError):
                    pass
        time.sleep(3)
    return False


def maintenance_players(force: bool = False) -> int | None:
    """Return the online count and refuse disruptive work when it is unknown/non-zero."""
    if not service_active():
        return 0
    metrics, error = metrics_or_none()
    if metrics is None:
        if not force:
            raise ManagerError(f"无法确认在线人数（{error}）；如确需维护，请加 --force")
        return None
    players = int(metrics.get("currentplayernum", 0))
    if players > 0 and not force:
        raise ManagerError(f"有 {players} 名玩家在线；如确需维护，请加 --force")
    return players


def service_control(action: str, force: bool = False) -> dict[str, Any]:
    """Start/stop/restart while preserving a deliberate stop across health checks."""
    if action not in {"start", "stop", "restart"}:
        raise ManagerError(f"未知服务操作：{action}")
    require_root()
    with maintenance_lock():
        active = service_active()
        if action == "start":
            with contextlib.suppress(FileNotFoundError):
                MANUAL_STOP.unlink()
            if not active:
                start_service()
                if not wait_for_server():
                    raise ManagerError("服务启动后未在 180 秒内通过健康检查")
                record_performance_event("restart", "游戏服务已启动", "通过安全控制流程启动并通过健康检查")
            print("服务已启动并通过健康检查" if not active else "服务原本就在运行")
            return {"action": action, "service_active": True}

        if not active and action == "stop":
            atomic_json(MANUAL_STOP, {"stopped_at": iso_now(), "reason": "manual"})
            print("服务原本就是手动停止状态")
            return {"action": action, "service_active": False}

        if active:
            maintenance_players(force)
            request_world_save()
            # ExecStopPost cannot acquire the same lock held by this operation,
            # so make the verified event backup explicitly before stopping.
            create_backup("event", if_changed=True, request_save=False)

        if action == "stop":
            atomic_json(MANUAL_STOP, {"stopped_at": iso_now(), "reason": "manual"})
            try:
                run(["systemctl", "stop", SERVICE], capture=False, timeout=180)
            except Exception:
                if service_active():
                    with contextlib.suppress(FileNotFoundError):
                        MANUAL_STOP.unlink()
                raise
            print("服务已安全停止；健康检查会保持该状态")
            record_performance_event("stop", "游戏服务已停止", "已先保存世界并创建校验备份")
            return {"action": action, "service_active": False}

        with contextlib.suppress(FileNotFoundError):
            MANUAL_STOP.unlink()
        start_service(restart=active, timeout=180)
        if not wait_for_server():
            raise ManagerError("服务重启后未在 180 秒内通过健康检查")
        record_performance_event(
            "restart",
            "游戏服务已重启" if active else "游戏服务已启动",
            "安全控制流程已完成，端口和管理接口通过检查",
        )
        print("服务已重启并通过健康检查" if active else "服务已启动并通过健康检查")
        return {"action": action, "service_active": True}


def update_server(apply: bool, automatic: bool, force: bool) -> int:
    try:
        with maintenance_lock(nonblocking=automatic):
            try:
                info = query_update(automatic=automatic)
            except ManagerError as exc:
                if not automatic:
                    raise
                previous_failure = load_json(STATE / "update-query-failures.json", {})
                failures = int(previous_failure.get("consecutive_failures", 0)) + 1
                failure = {
                    "checked_at": iso_now(),
                    "installed_build": installed_build(),
                    "result": "deferred_query_unavailable",
                    "consecutive_failures": failures,
                    "error": str(exc),
                }
                atomic_json(STATE / "update-query-failures.json", failure)
                atomic_json(STATE / "update.json", failure)
                print(f"Steam 更新查询暂时不可用，已安全延后（连续 {failures} 次）：{exc}")
                return 0
            atomic_json(STATE / "update-query-failures.json", {
                "checked_at": info["checked_at"], "consecutive_failures": 0, "result": "ok",
            })
            atomic_json(STATE / "update.json", {**info, "result": "checked"})
            local = info["installed_build"]
            required = info.get("required_build")
            if info["up_to_date"]:
                print(f"已是最新 Steam 构建：{local}")
                return 0
            print(f"检测到新构建：{local} -> {required or '未知'}")
            if not apply and not automatic:
                print("仅完成检查；使用 --apply 执行更新")
                return 0
            require_root()
            was_active = service_active()
            metrics, api_error = metrics_or_none() if was_active else ({"currentplayernum": 0}, None)
            if metrics is None:
                if automatic:
                    atomic_json(STATE / "update.json", {**info, "result": "deferred_api_unavailable"})
                    print(f"自动更新已延后：{api_error}")
                    return 0
                if not force:
                    raise ManagerError("无法确认在线人数；如确需继续，请加 --force")
                players = None
            else:
                players = int(metrics.get("currentplayernum", 0))
            if players and players > 0:
                if automatic:
                    atomic_json(STATE / "update.json", {**info, "result": "deferred_players_online", "players": players})
                    print(f"有 {players} 名玩家在线，自动更新已延后")
                    return 0
                if not force:
                    raise ManagerError(f"有 {players} 名玩家在线；如确需维护，请加 --force")
                with contextlib.suppress(ManagerError):
                    api_request("announce", method="POST", body={"message": "Server update in 30 seconds."})
                time.sleep(30)
            create_backup("update", request_save=was_active)
            update_started = int(time.time())
            record_performance_event(
                "update_start",
                "Palworld 更新开始",
                f"Steam 构建 {local} → {required or '待确认'}",
                metadata={"old_build": local, "required_build": required},
                dedupe_key=f"update-start:{local}:{required}:{update_started}",
                timestamp=update_started,
            )
            if was_active:
                run(["systemctl", "stop", SERVICE], capture=False, timeout=180)
            update_args = [
                "runuser", "-u", "palworld", "--", "timeout", "1200",
                str(STEAMCMD), "+force_install_dir", str(SERVER), "+login", "anonymous",
                "+app_update", str(APP_ID), "+quit",
            ]
            try:
                run(update_args, capture=False, timeout=1250)
            except Exception as exc:
                record_performance_event(
                    "update_failed",
                    "Palworld 更新失败",
                    f"原构建 {local} · {type(exc).__name__}",
                    metadata={"old_build": local, "required_build": required},
                )
                raise
            finally:
                if was_active:
                    start_service(check=False)
            if was_active and not wait_for_server():
                raise ManagerError("更新后服务未在 180 秒内通过健康检查；更新前备份仍完整保留")
            new_build = installed_build()
            result = {**info, "completed_at": iso_now(), "result": "updated", "new_build": new_build}
            atomic_json(STATE / "update.json", result)
            record_performance_event(
                "update",
                "Palworld 更新完成",
                f"Steam 构建 {local} → {new_build}",
                metadata={"old_build": local, "new_build": new_build},
                dedupe_key=f"update:{new_build}",
                timestamp=int(dt.datetime.fromisoformat(result["completed_at"]).timestamp()),
            )
            print(f"更新完成，当前 Steam 构建：{new_build}")
            return 0
    except LockBusy as exc:
        if automatic:
            print(f"自动更新跳过：{exc}")
            return 0
        raise


def health_snapshot() -> dict[str, Any]:
    active = service_active()
    pid = game_pid() if active else None
    udp = udp_listening() if active else False
    metrics, api_error = metrics_or_none() if active else (None, "服务未运行")
    try:
        memory = int(service_value("MemoryCurrent")) if active else 0
    except ValueError:
        memory = None
    disk = shutil.disk_usage(BASE)
    return {
        "checked_at": iso_now(),
        "service_active": active,
        "manual_stop": manual_stop_active(),
        "game_pid": pid,
        "udp_8211_listening": udp,
        "process_age_seconds": process_age(pid) if active else None,
        "memory_bytes": memory,
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "metrics": metrics,
        "api_error": api_error,
    }


def health_command(automatic: bool, as_json: bool) -> int:
    if not automatic:
        snapshot = health_snapshot()
        expected_stop = bool(snapshot["manual_stop"] and not snapshot["service_active"])
        healthy = bool(expected_stop or (
            snapshot["service_active"] and snapshot["udp_8211_listening"] and snapshot["metrics"] is not None
        ))
        if as_json:
            print(json.dumps({**snapshot, "healthy": healthy}, ensure_ascii=False, indent=2))
        else:
            metrics = snapshot["metrics"] or {}
            print(f"服务：{'正常' if snapshot['service_active'] else '停止'}")
            print(f"游戏端口：{'正常' if snapshot['udp_8211_listening'] else '异常'}")
            print(f"REST API：{'正常' if snapshot['metrics'] is not None else snapshot['api_error']}")
            if metrics:
                print(
                    f"服务器 FPS：{metrics.get('serverfps', '未知')}；在线：{metrics.get('currentplayernum', '未知')}；"
                    f"帧时间：{metrics.get('serverframetime', '未知')} ms"
                )
            print(f"内存：{human_size(snapshot['memory_bytes'])}；磁盘可用：{human_size(snapshot['disk_free_bytes'])}")
        return 0 if healthy else 2
    try:
        with maintenance_lock(nonblocking=True):
            snapshot = health_snapshot()
            previous = load_json(STATE / "health.json", {})
            expected_stop = bool(snapshot["manual_stop"] and not snapshot["service_active"])
            age = snapshot.get("process_age_seconds") or 0
            metrics = snapshot.get("metrics")
            players = int(metrics.get("currentplayernum", 0)) if isinstance(metrics, dict) else None
            fps = float(metrics.get("serverfps", 0)) if isinstance(metrics, dict) else None
            critical_reasons: list[str] = []
            if not snapshot["service_active"] and not expected_stop:
                critical_reasons.append("service_inactive")
            elif age > 180 and not snapshot["udp_8211_listening"]:
                critical_reasons.append("game_port_missing")
            if fps is not None and fps <= 5 and players == 0 and age > 180:
                critical_reasons.append("idle_fps_critical")
            memory = snapshot.get("memory_bytes")
            if isinstance(memory, int) and memory >= MEMORY_RESTART_BYTES and players == 0:
                critical_reasons.append("idle_memory_high")
            previous_counts = previous.get("reason_counts") if isinstance(previous.get("reason_counts"), dict) else {}
            reason_counts = {
                reason: (int(previous_counts.get(reason, 0)) + 1 if reason in critical_reasons else 0)
                for reason in {"service_inactive", "game_port_missing", "idle_fps_critical", "idle_memory_high"}
            }
            consecutive = max(reason_counts.values(), default=0)
            if snapshot["disk_free_bytes"] < MIN_FREE_BYTES:
                prune_managed(apply=True)
            last_recovery = float(previous.get("last_recovery_epoch", 0))
            previous_by_reason = (
                previous.get("last_recovery_by_reason")
                if isinstance(previous.get("last_recovery_by_reason"), dict)
                else {}
            )
            last_recovery_by_reason = {
                str(key): float(value)
                for key, value in previous_by_reason.items()
                if isinstance(value, (int, float))
            }
            now_epoch = time.time()
            trigger_reasons = []
            for reason in critical_reasons:
                cooldown = MEMORY_RESTART_COOLDOWN_SECONDS if reason == "idle_memory_high" else 3600
                reason_last = float(last_recovery_by_reason.get(reason, last_recovery))
                if reason_counts.get(reason, 0) >= 3 and now_epoch - reason_last >= cooldown:
                    trigger_reasons.append(reason)
            recovered = False
            if trigger_reasons:
                require_root()
                if snapshot["service_active"] and snapshot["metrics"] is not None:
                    create_backup("event", if_changed=True, request_save=True)
                start_service(restart=bool(snapshot["service_active"]), timeout=180)
                recovered = wait_for_server()
                last_recovery = time.time()
                for reason in trigger_reasons:
                    last_recovery_by_reason[reason] = last_recovery
                if recovered:
                    reason_counts = {reason: 0 for reason in reason_counts}
                    consecutive = 0
                memory_detail = human_size(snapshot.get("memory_bytes"))
                memory_restart = "idle_memory_high" in trigger_reasons
                record_performance_event(
                    "restart" if recovered else "restart_failed",
                    (
                        "无人在线内存保护重启"
                        if memory_restart and recovered
                        else ("健康检查自动恢复" if recovered else "健康检查自动恢复失败")
                    ),
                    f"原因 {', '.join(trigger_reasons)} · 重启前游戏内存 {memory_detail}",
                    metadata={
                        "reasons": trigger_reasons,
                        "memory_bytes": snapshot.get("memory_bytes"),
                        "players": players,
                        "recovered": recovered,
                    },
                    dedupe_key=f"health-recovery:{int(last_recovery)}",
                )
            state = {
                **snapshot,
                "critical_reasons": critical_reasons,
                "reason_counts": reason_counts,
                "consecutive_critical": consecutive,
                "last_recovery_epoch": last_recovery,
                "last_recovery_by_reason": last_recovery_by_reason,
                "recovered_this_run": recovered,
                "trigger_reasons": trigger_reasons,
            }
            atomic_json(STATE / "health.json", state)
            if expected_stop:
                condition = "已手动停止"
            else:
                condition = "正常" if not critical_reasons and snapshot["metrics"] is not None else "降级"
            metric_text = (
                f"FPS={metrics.get('serverfps', '未知')}，在线={metrics.get('currentplayernum', '未知')}，"
                f"帧时间={metrics.get('serverframetime', '未知')}ms"
                if isinstance(metrics, dict)
                else f"REST={snapshot.get('api_error', '不可用')}"
            )
            print(
                f"健康检查：{condition}；{metric_text}；内存={human_size(snapshot.get('memory_bytes'))}；"
                f"连续严重异常={consecutive}；自动恢复={'成功' if recovered else '未触发'}"
            )
            return 0
    except LockBusy as exc:
        print(f"健康检查跳过：{exc}")
        return 0


def validate_setting_value(key: str, raw: str, template: str) -> str:
    if "\n" in raw or "\r" in raw or len(raw) > 2048:
        raise ManagerError("设置值包含换行或过长")
    if template in {"True", "False"}:
        lowered = raw.lower()
        if lowered not in {"true", "false", "1", "0", "yes", "no"}:
            raise ManagerError(f"{key} 需要布尔值 True/False")
        return "True" if lowered in {"true", "1", "yes"} else "False"
    if template.startswith('"') and template.endswith('"'):
        return json.dumps(raw, ensure_ascii=False)
    if template.startswith("(") and template.endswith(")"):
        value = raw if raw.startswith("(") and raw.endswith(")") else f"({raw})"
        if not re.fullmatch(r"\([A-Za-z0-9_,.-]*\)", value):
            raise ManagerError(f"{key} 列表格式无效")
        return value
    if re.fullmatch(r"-?\d+", template):
        try:
            value = int(raw)
        except ValueError as exc:
            raise ManagerError(f"{key} 需要整数") from exc
        if not -(2**31) <= value < 2**31:
            raise ManagerError(f"{key} 超出整数范围")
        if key.endswith("Port") and not 1 <= value <= 65535:
            raise ManagerError(f"{key} 端口必须为 1–65535")
        limits = {
            "ServerPlayerMaxNum": (1, 32),
            "CoopPlayerMaxNum": (1, 32),
            "BaseCampMaxNum": (1, 128),
            "BaseCampWorkerMaxNum": (1, 50),
            "BaseCampMaxNumInGuild": (1, 10),
            "GuildPlayerMaxNum": (1, 100),
            "DropItemMaxNum": (0, 10000),
            "MaxBuildingLimitNum": (0, 1000000),
            "ChatPostLimitPerMinute": (1, 1000),
        }
        if key in limits and not limits[key][0] <= value <= limits[key][1]:
            raise ManagerError(f"{key} 必须为 {limits[key][0]}–{limits[key][1]}")
        return str(value)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", template):
        try:
            value = float(raw)
        except ValueError as exc:
            raise ManagerError(f"{key} 需要数字") from exc
        limits = {
            "ServerReplicatePawnCullDistance": (5000.0, 15000.0),
            "VoiceChatMaxVolumeDistance": (0.0, 100000.0),
            "VoiceChatZeroVolumeDistance": (0.0, 100000.0),
            "AutoTransferMasterCheckIntervalSeconds": (1.0, 86400.0),
            "AutoResetGuildTimeNoOnlinePlayers": (0.0, 8760.0),
            "AutoSaveSpan": (5.0, 3600.0),
            "DropItemAliveMaxHours": (0.0, 168.0),
            "PalEggDefaultHatchingTime": (0.0, 240.0),
            "BlockRespawnTime": (0.0, 3600.0),
            "BuildingNameDisplayCacheTTLSeconds": (0.0, 86400.0),
        }
        minimum, maximum = limits.get(key, (0.0, 1000.0))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ManagerError(f"{key} 数值必须在 {minimum:g}–{maximum:g} 内")
        return format(value, ".10g")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", raw):
        raise ManagerError(f"{key} 值格式无效")
    return raw


def render_setting_changes(
    changes: Mapping[str, str],
) -> tuple[str, dict[str, str], dict[str, str]]:
    if not changes:
        raise ManagerError("没有需要修改的设置")
    if len(changes) > 128:
        raise ManagerError("单次设置项过多")
    text, start, end, pairs = parse_settings()
    current = dict(pairs)
    try:
        defaults = settings_map(DEFAULT_CONFIG)
    except ManagerError:
        defaults = {}
    canonical: dict[str, str] = {}
    previous: dict[str, str] = {}
    for key, value in changes.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ManagerError("设置名称和值必须是字符串")
        if key not in current:
            raise ManagerError(f"未知设置项：{key}")
        if key in SENSITIVE_KEYS:
            raise ManagerError("密码设置请使用 settings set-secret，避免进入命令历史")
        template = defaults.get(key, current[key])
        encoded = validate_setting_value(key, value, template)
        if not setting_values_equal(encoded, current[key]):
            canonical[key] = encoded
            previous[key] = current[key]
    replaced = [(name, canonical.get(name, old)) for name, old in pairs]
    updated = text[:start] + ",".join(f"{name}={raw}" for name, raw in replaced) + text[end:]
    return updated, canonical, previous


def setting_values_equal(left: str, right: str) -> bool:
    """Treat equivalent numeric spellings (1, 1.0, 1.000000) as unchanged."""
    if left == right:
        return True
    numeric = r"-?\d+(?:\.\d+)?"
    if re.fullmatch(numeric, left) and re.fullmatch(numeric, right):
        try:
            return Decimal(left) == Decimal(right)
        except InvalidOperation:
            return False
    return False


def render_setting_change(key: str, value: str) -> tuple[str, str]:
    updated, canonical, _ = render_setting_changes({key: value})
    return updated, canonical.get(key, settings_map()[key])


def atomic_config_write(text: str) -> None:
    ensure_dir(CONFIG.parent)
    fd, name = tempfile.mkstemp(prefix=".PalWorldSettings.", dir=CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, CONFIG)
        give_to_palworld(CONFIG)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def restore_preserved_settings(values: Mapping[str, str]) -> None:
    """Reapply exact infrastructure values after unpacking an older backup."""
    if not values:
        return
    text, start, end, pairs = parse_settings()
    current = dict(pairs)
    replacements = {
        key: value
        for key, value in values.items()
        if key in current and current[key] != value
    }
    if not replacements:
        return
    replaced = [(name, replacements.get(name, old)) for name, old in pairs]
    updated = text[:start] + ",".join(f"{name}={raw}" for name, raw in replaced) + text[end:]
    atomic_config_write(updated)
    written = settings_map()
    if any(written.get(key) != value for key, value in replacements.items()):
        raise ManagerError("恢复后重新应用面板/API 关键配置失败")


def set_settings(
    changes: Mapping[str, str],
    apply: bool,
    restart: bool,
    force: bool,
    progress: Callable[[str, str, int], None] | None = None,
) -> dict[str, Any]:
    current_phase = "validating"

    def report(phase: str, message: str, percent: int) -> None:
        nonlocal current_phase
        current_phase = phase
        if progress is not None:
            progress(phase, message, percent)

    report("validating", "正在校验设置值和当前配置…", 8)
    updated, canonical, previous = render_setting_changes(changes)
    for key in canonical:
        print(f"{key}: {previous[key]} -> {canonical[key]}")
    if not canonical:
        print("所有设置均已是目标值，无需写入或重启")
        report("complete", "设置已经是目标值，无需重复写入或重启", 100)
        return {"changed": 0, "keys": [], "restarted": False}
    if not apply:
        print("预览完成；使用 --apply 写入")
        report("complete", "设置预览已完成，尚未写入", 100)
        return {"changed": len(canonical), "keys": list(canonical), "restarted": False}
    require_root()
    restarted = False
    with maintenance_lock():
        report("validating", "已取得维护锁，正在复核本次改动…", 14)
        # Re-read inside the lock so a concurrent maintenance task cannot make
        # the preview stale between validation and the atomic write.
        updated, canonical, previous = render_setting_changes(changes)
        if not canonical:
            print("设置已由其他任务写入，无需重复操作")
            report("complete", "设置已经是目标值，无需重复写入或重启", 100)
            return {"changed": 0, "keys": [], "restarted": False}
        was_active = service_active()
        if was_active and not restart:
            raise ManagerError("服务运行中必须同时使用 --restart；否则关服时旧设置会覆盖新值")
        if restart and was_active:
            report("saving", "正在确认无人在线并保存当前世界…", 22)
            maintenance_players(force)
            request_world_save()
            report("backup", "世界已保存，正在创建并校验应用前备份…", 36)
            create_backup("event", if_changed=True, request_save=False)
        history = backup_config_file()
        try:
            if restart and was_active:
                report("stopping", "备份已完成，正在安全停止游戏服务…", 50)
                run(["systemctl", "stop", SERVICE], capture=False, timeout=180)
            report("writing", "正在原子写入并复核世界设置…", 65)
            atomic_config_write(updated)
            written = settings_map()
            if any(written.get(key) != value for key, value in canonical.items()):
                raise ManagerError("写入后的配置复核不一致")
            if restart and was_active:
                report("starting", "设置写入已复核，正在启动游戏服务…", 78)
                start_service()
                report("health", "服务已启动，正在等待端口和管理接口恢复…", 90)
                if not wait_for_server():
                    raise ManagerError("启动后未在 180 秒内通过健康检查")
                restarted = True
        except Exception as exc:
            failed_phase = current_phase
            report(f"rollback-{failed_phase}", "应用未完成，正在恢复原配置和服务状态…", 95)
            if restart and was_active:
                run(["systemctl", "stop", SERVICE], check=False, capture=False, timeout=180)
            shutil.copy2(history, CONFIG)
            give_to_palworld(CONFIG)
            recovered = True
            if restart and was_active:
                start_service(check=False)
                recovered = wait_for_server()
            result = "已恢复原配置" if recovered else "原配置已恢复，但服务仍未通过检查"
            raise ManagerError(f"设置应用失败，{result}：{exc}") from exc
    state = "并通过重启生效" if restarted else "；将在下次启动时生效"
    print(f"已写入 {len(canonical)} 项设置{state}")
    report(
        "complete",
        f"已应用并复核 {len(canonical)} 项设置，游戏服务已恢复运行"
        if restarted else f"已写入并复核 {len(canonical)} 项设置，将在下次启动时生效",
        100,
    )
    return {"changed": len(canonical), "keys": list(canonical), "restarted": restarted}


def set_setting(key: str, value: str, apply: bool, restart: bool, force: bool) -> int:
    set_settings({key: value}, apply, restart, force)
    return 0


def set_secret(key: str, apply: bool, restart: bool, force: bool) -> int:
    if key not in SENSITIVE_KEYS:
        raise ManagerError("set-secret 仅支持 AdminPassword 或 ServerPassword")
    value = getpass.getpass(f"输入 {key}：") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    if not 8 <= len(value) <= 128 or any(char in value for char in ('"', "\n", "\r")):
        raise ManagerError("密码长度需为 8–128，且不能包含双引号或换行")
    print(f"{key}: <已隐藏> -> <新值已校验>")
    if not apply:
        print("预览完成；使用 --apply 写入")
        return 0
    require_root()
    with maintenance_lock():
        text, start, end, pairs = parse_settings()
        encoded = json.dumps(value, ensure_ascii=False)
        replaced = [(name, encoded if name == key else old) for name, old in pairs]
        updated = text[:start] + ",".join(f"{name}={raw}" for name, raw in replaced) + text[end:]
        was_active = service_active()
        if was_active and not restart:
            raise ManagerError("服务运行中必须同时使用 --restart；否则关服时旧设置会覆盖新值")
        if restart and was_active:
            maintenance_players(force)
            request_world_save()
            create_backup("event", if_changed=True, request_save=False)
        history = backup_config_file()
        old_secret = SECRET.read_text(encoding="utf-8") if key == "AdminPassword" and SECRET.exists() else None
        try:
            if restart and was_active:
                run(["systemctl", "stop", SERVICE], capture=False, timeout=180)
            atomic_config_write(updated)
            if settings_map().get(key) != encoded:
                raise ManagerError("写入后的密码配置复核不一致")
            if key == "AdminPassword":
                SECRET.write_text(value + "\n", encoding="utf-8")
                SECRET.chmod(0o600)
                give_to_palworld(SECRET)
            if restart and was_active:
                start_service()
                if not wait_for_server():
                    raise ManagerError("启动后未在 180 秒内通过健康检查")
        except Exception as exc:
            if restart and was_active:
                run(["systemctl", "stop", SERVICE], check=False, capture=False, timeout=180)
            shutil.copy2(history, CONFIG)
            give_to_palworld(CONFIG)
            if key == "AdminPassword" and old_secret is not None:
                SECRET.write_text(old_secret, encoding="utf-8")
                SECRET.chmod(0o600)
                give_to_palworld(SECRET)
            elif key == "AdminPassword":
                with contextlib.suppress(FileNotFoundError):
                    SECRET.unlink()
            recovered = True
            if restart and was_active:
                start_service(check=False)
                recovered = wait_for_server()
            result = "已恢复原密码和配置" if recovered else "原密码和配置已恢复，但服务仍未通过检查"
            raise ManagerError(f"密码应用失败，{result}：{exc}") from exc
    print(f"密码已写入{'并通过重启生效' if restart and was_active else '；下次启动生效'}")
    return 0


def resolve_archive(value: str) -> Path:
    if value == "latest":
        archives = managed_archives()
        if not archives:
            raise ManagerError("没有可恢复的受管备份")
        return archives[-1]
    path = Path(value).expanduser()
    if not path.is_absolute():
        managed_root = MANAGED.resolve()
        path = (managed_root / path).resolve()
        try:
            path.relative_to(managed_root)
        except ValueError as exc:
            raise ManagerError("备份路径超出受管目录") from exc
    return path


def safe_extract(archive_path: Path, destination: Path) -> None:
    ensure_dir(destination, 0o700)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            validate_member_name(member.name)
            if member.name == "palworld-backup.json" or member.isdir():
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise ManagerError(f"拒绝恢复未知条目：{member.name}")
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ManagerError(f"无法读取：{member.name}")
            with target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o600)


def restore_backup(value: str, apply: bool) -> int:
    archive = resolve_archive(value)
    info = verify_archive(archive)
    print(
        f"备份有效：{archive}；类别={info['kind']}；时间={info['created_at']}；"
        f"文件={info['files']}；解包大小={human_size(info['unpacked_bytes'])}"
    )
    if not apply:
        print("仅完成恢复演练；使用 --apply 才会停服并恢复")
        return 0
    require_root()
    with maintenance_lock():
        was_active = service_active()
        if was_active:
            maintenance_players(False)
        preserved = {
            key: value
            for key, value in settings_map().items()
            if key in RESTORE_PRESERVE_KEYS
        }
        previous_marker = load_json(MANUAL_STOP, {}) if manual_stop_active() else {}

        def preserve_stopped_state(reason: str) -> None:
            marker = previous_marker if isinstance(previous_marker, dict) and previous_marker else {
                "stopped_at": iso_now(),
                "reason": reason,
            }
            # Set the marker before stopping so the health timer cannot race us.
            atomic_json(MANUAL_STOP, marker)
            run(["systemctl", "stop", SERVICE], capture=False, timeout=180)
            if service_active():
                raise ManagerError("服务未能恢复为原先的停止状态")

        create_backup("manual", request_save=was_active)
        if was_active:
            run(["systemctl", "stop", SERVICE], capture=False, timeout=180)
        token = stamp()
        staging = STATE / f"restore-staging-{token}"
        rollback = STATE / f"restore-rollback-{token}"
        ensure_dir(rollback, 0o700)
        moved: list[str] = []
        restore_succeeded = False
        rollback_succeeded = False
        try:
            safe_extract(archive, staging)
            for name in ("SaveGames", "Config"):
                source = staging / name
                if not source.is_dir():
                    raise ManagerError(f"备份缺少 {name}")
                current = SAVED / name
                if current.exists():
                    shutil.move(str(current), str(rollback / name))
                moved.append(name)
                shutil.move(str(source), str(current))
            restore_preserved_settings(preserved)
            ids = palworld_ids()
            if ids is not None:
                for root, directories, files in os.walk(SAVED):
                    os.chown(root, *ids)
                    for name in directories + files:
                        os.chown(Path(root) / name, *ids)
            start_service()
            if not wait_for_server():
                raise ManagerError("恢复后的服务未通过健康检查")
            if not was_active:
                preserve_stopped_state("restore-preserved-stop")
            restore_succeeded = True
        except Exception as exc:
            run(["systemctl", "stop", SERVICE], check=False, capture=False, timeout=180)
            try:
                for name in moved:
                    current = SAVED / name
                    if current.exists():
                        shutil.rmtree(current)
                    old = rollback / name
                    if old.exists():
                        shutil.move(str(old), str(current))
                start_service(check=False)
                rollback_succeeded = wait_for_server(180)
                if rollback_succeeded and not was_active:
                    preserve_stopped_state("restore-rollback-preserved-stop")
            except Exception as rollback_exc:
                raise ManagerError(
                    f"恢复失败且自动回滚未完成；回滚目录已保留在 {rollback}：{rollback_exc}"
                ) from exc
            if not rollback_succeeded:
                raise ManagerError(f"恢复失败；原文件已放回，但服务未通过检查：{exc}") from exc
            raise ManagerError(f"恢复失败，已回滚到恢复前状态：{exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if restore_succeeded or rollback_succeeded:
                shutil.rmtree(rollback, ignore_errors=True)
    if was_active:
        print("恢复完成，服务已重新启动并通过健康检查")
    else:
        print("恢复完成，校验启动通过，并已恢复为原先的停止状态")
    return 0


def old_log_files() -> list[Path]:
    now = time.time()
    roots = [
        (SAVED / "Logs", 14),
        (SAVED / "Crashes", 14),
        (BASE / "steamcmd" / "logs", 30),
    ]
    result: list[Path] = []
    for root, days in roots:
        if not root.exists():
            continue
        cutoff = now - days * 86400
        for path in root.rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    result.append(path)
    return result


def smart_health() -> list[dict[str, Any]]:
    if shutil.which("smartctl") is None:
        return [{"device": None, "status": "smartctl_not_installed"}]
    disks = []
    result = run(["lsblk", "-dn", "-o", "NAME,TYPE"], check=False)
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk":
            disks.append(f"/dev/{parts[0]}")
    reports: list[dict[str, Any]] = []
    for device in disks:
        check = run(["smartctl", "-H", device], check=False)
        output = f"{check.stdout or ''}\n{check.stderr or ''}"
        if re.search(r"(PASSED|OK)", output, re.IGNORECASE):
            health = "passed"
        elif re.search(r"(FAILED|FAILING)", output, re.IGNORECASE):
            health = "failed"
        else:
            health = "unknown"
        reports.append({"device": device, "status": health, "exit_code": check.returncode})
    return reports


def maintenance(apply: bool) -> int:
    if apply:
        require_root()
    with maintenance_lock():
        logs = old_log_files()
        prune = prune_managed(apply=apply)
        removed_bytes = sum(path.stat().st_size for path in logs if path.exists())
        if apply:
            for path in logs:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            for root in (SAVED / "Logs", SAVED / "Crashes", BASE / "steamcmd" / "logs"):
                if root.exists():
                    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
                        with contextlib.suppress(OSError):
                            directory.rmdir()
            run(["journalctl", "--vacuum-time=14d", "--vacuum-size=1G"], check=False, capture=False, timeout=120)
        smart = smart_health()
        result = {
            "checked_at": iso_now(),
            "applied": apply,
            "old_log_files": len(logs),
            "old_log_bytes": removed_bytes,
            "managed_backup_prune": prune,
            "disk_free_bytes": shutil.disk_usage(BASE).free,
            "smart": smart,
        }
        if apply:
            atomic_json(STATE / "maintenance.json", result)
        action = "已清理" if apply else "可清理"
        print(
            f"维护检查：{action}日志 {len(logs)} 个/{human_size(removed_bytes)}；"
            f"备份 {prune['files']} 个/{human_size(prune['bytes'])}；SMART={smart}"
        )
        return 0


def status(as_json: bool) -> int:
    snapshot = health_snapshot()
    archives = managed_archives()
    latest = archives[-1] if archives else None
    value = {
        **snapshot,
        "installed_build": installed_build(),
        "managed_backup_count": len(archives),
        "managed_backup_bytes": sum(path.stat().st_size for path in archives),
        "latest_backup": str(latest) if latest else None,
        "last_update": load_json(STATE / "update.json", None),
        "last_maintenance": load_json(STATE / "maintenance.json", None),
    }
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        metrics = snapshot["metrics"] or {}
        print(f"Palworld：{'运行中' if snapshot['service_active'] else '已停止'}；Steam 构建 {value['installed_build']}")
        print(
            f"端口：UDP 8211 {'正常' if snapshot['udp_8211_listening'] else '异常'}；"
            f"REST {api_port()} {'正常' if snapshot['metrics'] is not None else '异常'}"
        )
        print(
            f"运行指标：FPS {metrics.get('serverfps', '未知')}；在线 {metrics.get('currentplayernum', '未知')}；"
            f"帧时间 {metrics.get('serverframetime', '未知')} ms；内存 {human_size(snapshot['memory_bytes'])}"
        )
        print(
            f"磁盘可用：{human_size(snapshot['disk_free_bytes'])}；受管备份：{len(archives)} 个/"
            f"{human_size(value['managed_backup_bytes'])}；最新：{latest or '无'}"
        )
    return 0


def show_settings(key: str | None, as_json: bool) -> int:
    values = settings_map()
    safe = {name: ("<已隐藏>" if name in SENSITIVE_KEYS else raw) for name, raw in values.items()}
    if key:
        if key not in safe:
            raise ManagerError(f"未知设置项：{key}")
        print(f"{key}={safe[key]}")
    elif as_json:
        print(json.dumps(safe, ensure_ascii=False, indent=2))
    else:
        for name, raw in safe.items():
            print(f"{name}={raw}")
    return 0


def api_cli(operation: str, message: str | None) -> int:
    if operation in {"metrics", "players", "settings", "info"}:
        value = api_request(operation)
    elif operation == "save":
        value = api_request("save", method="POST")
    elif operation == "announce":
        if not message:
            raise ManagerError("announce 需要 --message")
        value = api_request("announce", method="POST", body={"message": message})
    else:
        raise ManagerError(f"未知 API 操作：{operation}")
    if isinstance(value, dict):
        for key in list(value):
            if "password" in key.lower():
                value[key] = "<已隐藏>"
    print(json.dumps(value, ensure_ascii=False, indent=2) if value is not None else "操作成功")
    return 0


def event_cli(kind: str, title: str, detail: str) -> int:
    require_root()
    record_performance_event(kind, title, detail, dedupe_key=f"manual-event:{kind}:{int(time.time())}")
    print(f"性能事件已记录：{title}")
    return 0


def backup_command(args: argparse.Namespace) -> int:
    if args.backup_action == "create":
        try:
            with maintenance_lock(nonblocking=args.nonblocking):
                create_backup(args.kind, if_changed=args.if_changed)
        except LockBusy as exc:
            if args.nonblocking:
                print(f"停止事件备份跳过：{exc}")
                return 0
            raise
        return 0
    if args.backup_action == "list":
        archives = sorted(managed_archives(), reverse=True)
        if not archives:
            print("暂无受管备份")
        for path in archives:
            print(f"{dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')}  {human_size(path.stat().st_size):>10}  {path}")
        return 0
    archive = resolve_archive(args.archive)
    print(json.dumps(verify_archive(archive), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="palworldctl", description="Palworld 轻量级服务器管理")
    sub = parser.add_subparsers(dest="command", required=True)

    status_parser = sub.add_parser("status", help="汇总状态")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=lambda a: status(a.json))

    health_parser = sub.add_parser("health", help="游戏级健康检查")
    health_parser.add_argument("--automatic", action="store_true")
    health_parser.add_argument("--json", action="store_true")
    health_parser.set_defaults(func=lambda a: health_command(a.automatic, a.json))

    service_parser = sub.add_parser("service", help="安全启动、停止或重启")
    service_parser.add_argument("action", choices=["start", "stop", "restart"])
    service_parser.add_argument("--force", action="store_true")
    service_parser.set_defaults(func=lambda a: service_control(a.action, a.force) and 0)

    backup_parser = sub.add_parser("backup", help="备份管理")
    backup_sub = backup_parser.add_subparsers(dest="backup_action", required=True)
    create = backup_sub.add_parser("create", help="创建并校验备份")
    create.add_argument("--kind", choices=sorted(RETENTION), default="manual")
    create.add_argument("--if-changed", action="store_true")
    create.add_argument("--nonblocking", action="store_true")
    backup_sub.add_parser("list", help="列出备份")
    verify = backup_sub.add_parser("verify", help="完整校验备份")
    verify.add_argument("archive", nargs="?", default="latest")
    backup_parser.set_defaults(func=backup_command)

    update = sub.add_parser("update", help="检查或执行 Steam 更新")
    update.add_argument("--apply", action="store_true")
    update.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    update.add_argument("--force", action="store_true")
    update.set_defaults(func=lambda a: update_server(a.apply, a.automatic, a.force))

    settings = sub.add_parser("settings", help="安全查看和修改配置")
    settings_sub = settings.add_subparsers(dest="settings_action", required=True)
    show = settings_sub.add_parser("show")
    show.add_argument("key", nargs="?")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=lambda a: show_settings(a.key, a.json))
    setting = settings_sub.add_parser("set")
    setting.add_argument("key")
    setting.add_argument("value")
    setting.add_argument("--apply", action="store_true")
    setting.add_argument("--restart", action="store_true")
    setting.add_argument("--force", action="store_true")
    setting.set_defaults(func=lambda a: set_setting(a.key, a.value, a.apply, a.restart, a.force))
    secret = settings_sub.add_parser("set-secret")
    secret.add_argument("key", choices=sorted(SENSITIVE_KEYS))
    secret.add_argument("--apply", action="store_true")
    secret.add_argument("--restart", action="store_true")
    secret.add_argument("--force", action="store_true")
    secret.set_defaults(func=lambda a: set_secret(a.key, a.apply, a.restart, a.force))

    restore = sub.add_parser("restore", help="校验或恢复备份")
    restore.add_argument("archive", nargs="?", default="latest")
    restore.add_argument("--apply", action="store_true")
    restore.set_defaults(func=lambda a: restore_backup(a.archive, a.apply))

    maintenance_parser = sub.add_parser("maintenance", help="备份/日志/磁盘维护")
    maintenance_parser.add_argument("--apply", action="store_true")
    maintenance_parser.set_defaults(func=lambda a: maintenance(a.apply))

    api = sub.add_parser("api", help="调用 Palworld REST API")
    api.add_argument("operation", choices=["info", "metrics", "players", "settings", "save", "announce"])
    api.add_argument("--message")
    api.set_defaults(func=lambda a: api_cli(a.operation, a.message))

    event = sub.add_parser("event", help=argparse.SUPPRESS)
    event.add_argument("--kind", choices=["backup", "restart", "save", "stop", "update"], required=True)
    event.add_argument("--title", required=True)
    event.add_argument("--detail", default="")
    event.set_defaults(func=lambda a: event_cli(a.kind, a.title, a.detail))
    return parser


def main() -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("操作已取消", file=sys.stderr)
        return 130
    except ManagerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"未预期错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
