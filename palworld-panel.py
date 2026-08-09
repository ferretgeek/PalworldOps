#!/usr/bin/env python3
"""Dependency-free LAN control panel for the managed Palworld server."""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import hmac
import io
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import sqlite3
import threading
import time
import zipfile
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit


PANEL_VERSION = "1.5.0"
HOST = os.environ.get("PALWORLD_PANEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("PALWORLD_PANEL_PORT", "8213"))
MANAGER_PATH = Path(os.environ.get("PALWORLD_MANAGER", Path(__file__).with_name("palworldctl.py")))
STATIC_ROOT = Path(os.environ.get("PALWORLD_PANEL_STATIC", "/opt/palworld/panel"))
if not STATIC_ROOT.is_dir():
    STATIC_ROOT = Path(__file__).with_name("panel")
BREED_ROOT = Path(os.environ.get("PALWORLD_BREED_ROOT", "/opt/palworld/breed-helper/public"))
BREED_STATUS_PATH = BREED_ROOT / "server-status.json"


def discover_breed_save() -> Path:
    configured = os.environ.get("PALWORLD_BREED_SAVE", "").strip()
    if configured:
        return Path(configured)
    worlds_root = Path("/opt/palworld/server/Pal/Saved/SaveGames/0")
    try:
        candidates = [path for path in worlds_root.glob("*/Level.sav") if path.is_file()]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
    except OSError:
        pass
    return worlds_root / "WORLD_ID" / "Level.sav"


BREED_SAVE_PATH = discover_breed_save()
BREED_REFRESH_SERVICE = os.environ.get("PALWORLD_BREED_REFRESH_SERVICE", "palworld-breed-refresh.service")

loader = SourceFileLoader("palworldctl", str(MANAGER_PATH))
spec = importlib.util.spec_from_loader("palworldctl", loader)
if spec is None:
    raise RuntimeError(f"cannot load manager: {MANAGER_PATH}")
manager = importlib.util.module_from_spec(spec)
loader.exec_module(manager)

SESSION_COOKIE = "palworld_panel_session"
SESSION_TTL_SECONDS = 12 * 3600
REMEMBER_SESSION_TTL_SECONDS = 30 * 24 * 3600
SESSION_STORE_PATH = Path(os.environ.get("PALWORLD_PANEL_SESSION_STORE", "/opt/palworld/state/panel-sessions.json"))
MAX_ACTIVE_SESSIONS = 64
MAX_JSON_BYTES = 64 * 1024
PERFORMANCE_DB_PATH = Path(os.environ.get("PALWORLD_PANEL_PERFORMANCE_DB", "/opt/palworld/state/performance-history.sqlite3"))
PERFORMANCE_SAMPLE_SECONDS = max(10, int(os.environ.get("PALWORLD_PANEL_PERFORMANCE_INTERVAL", "30")))
PERFORMANCE_RETENTION_DAYS = 365
PERFORMANCE_QUERY_DAYS = 90
PERFORMANCE_MAX_ROWS = 1_200_000
PERFORMANCE_CLIENT_MAX_ROWS = 1_200_000
PERFORMANCE_MAX_POINTS = 480
PERFORMANCE_CAPACITY_MIN_SAMPLES = max(10, 600 // PERFORMANCE_SAMPLE_SECONDS)
PERFORMANCE_DB_HARD_LIMIT_BYTES = int(1.8 * 1024**3)
PERFORMANCE_DB_MAIN_LIMIT_BYTES = int(1.70 * 1024**3)
PERFORMANCE_DB_SOFT_LIMIT_BYTES = int(1.55 * 1024**3)
PERFORMANCE_WAL_LIMIT_BYTES = 16 * 1024**2
CLIENT_GAME_SAMPLE_SECONDS = 10
CLIENT_IDLE_SAMPLE_SECONDS = 60
SSD_TEMPERATURE_SAMPLE_SECONDS = 300
SSD_TEMPERATURE_RETRY_SECONDS = 30


CATEGORY_ORDER = ["服务器", "时间与成长", "战斗与生存", "资源与掉落", "建造与据点", "联机规则", "性能与存档", "高级"]

SETTING_META: dict[str, dict[str, Any]] = {
    "ServerName": {"label": "服务器名称", "category": "服务器", "help": "支持中文；保存后面板会先备份存档，再重启游戏服务使其生效。"},
    "ServerDescription": {"label": "服务器描述", "category": "服务器", "help": "支持中文，用于说明这个服务器的用途。"},
    "ServerPlayerMaxNum": {"label": "最大玩家数", "category": "服务器", "min": 1, "max": 32, "impact": "medium"},
    "Difficulty": {
        "label": "世界难度预设", "category": "服务器", "choices": ["None", "Normal", "Difficult"],
        "choice_labels": {"None": "自定义参数", "Normal": "普通", "Difficult": "困难"},
        "help": "选择“自定义参数”时，下面各项倍率会按当前存档设置生效。",
    },
    "RandomizerType": {
        "label": "帕鲁生成随机化模式", "category": "服务器", "choices": ["None", "Region", "All"],
        "choice_labels": {"None": "不随机", "Region": "按区域随机", "All": "完全随机"},
        "help": "控制野生帕鲁种类的随机方式。",
    },
    "RandomizerSeed": {"label": "随机化种子", "category": "服务器", "help": "仅在启用帕鲁生成随机化时使用。"},
    "bIsRandomizerPalLevelRandom": {"label": "完全随机野生帕鲁等级", "category": "服务器", "help": "关闭时，等级会在各区域适合的范围内随机。"},
    "DayTimeSpeedRate": {"label": "白天速度倍率", "category": "时间与成长", "min": 0.1, "max": 5, "step": 0.1},
    "NightTimeSpeedRate": {"label": "夜晚速度倍率", "category": "时间与成长", "min": 0.1, "max": 5, "step": 0.1},
    "ExpRate": {"label": "经验倍率", "category": "时间与成长", "min": 0.1, "max": 20, "step": 0.1},
    "PalCaptureRate": {"label": "捕获概率倍率", "category": "时间与成长", "min": 0.1, "max": 10, "step": 0.1},
    "PalSpawnNumRate": {"label": "帕鲁生成数量倍率", "category": "时间与成长", "min": 0.1, "max": 5, "step": 0.1, "impact": "high", "help": "高于 1 会直接增加服务器计算量；追求稳定帧时间建议保持 1。"},
    "PalEggDefaultHatchingTime": {"label": "巨大蛋孵化小时数", "category": "时间与成长", "min": 0, "max": 240, "step": 0.5},
    "WorkSpeedRate": {"label": "工作速度倍率", "category": "时间与成长", "min": 0.1, "max": 20, "step": 0.1},
    "MonsterFarmActionSpeedRate": {"label": "牧场产出速度倍率", "category": "时间与成长", "min": 0.1, "max": 20, "step": 0.1},
    "PalDamageRateAttack": {"label": "帕鲁造成伤害倍率", "category": "战斗与生存"},
    "PalDamageRateDefense": {"label": "帕鲁受到伤害倍率", "category": "战斗与生存"},
    "PlayerDamageRateAttack": {"label": "玩家造成伤害倍率", "category": "战斗与生存"},
    "PlayerDamageRateDefense": {"label": "玩家受到伤害倍率", "category": "战斗与生存"},
    "PlayerStomachDecreaceRate": {"label": "玩家饱食度消耗倍率", "category": "战斗与生存"},
    "PlayerStaminaDecreaceRate": {"label": "玩家耐力消耗倍率", "category": "战斗与生存"},
    "PlayerAutoHPRegeneRate": {"label": "玩家生命恢复倍率", "category": "战斗与生存"},
    "PlayerAutoHpRegeneRateInSleep": {"label": "玩家睡眠恢复倍率", "category": "战斗与生存"},
    "PalStomachDecreaceRate": {"label": "帕鲁饱食度消耗倍率", "category": "战斗与生存"},
    "PalStaminaDecreaceRate": {"label": "帕鲁耐力消耗倍率", "category": "战斗与生存"},
    "PalAutoHPRegeneRate": {"label": "帕鲁生命恢复倍率", "category": "战斗与生存"},
    "PalAutoHpRegeneRateInSleep": {"label": "帕鲁睡眠恢复倍率", "category": "战斗与生存"},
    "ItemWeightRate": {"label": "物品重量倍率", "category": "战斗与生存"},
    "EquipmentDurabilityDamageRate": {"label": "装备耐久消耗倍率", "category": "战斗与生存"},
    "ItemCorruptionMultiplier": {"label": "物品腐败速度倍率", "category": "战斗与生存"},
    "DeathPenalty": {
        "label": "死亡掉落规则", "category": "战斗与生存", "choices": ["None", "Item", "ItemAndEquipment", "All"],
        "choice_labels": {
            "None": "不掉落", "Item": "仅掉落背包物品", "ItemAndEquipment": "掉落物品和装备", "All": "掉落物品、装备和队伍帕鲁",
        },
    },
    "bHardcore": {"label": "硬核模式", "category": "战斗与生存"},
    "bPalLost": {"label": "硬核模式丢失帕鲁", "category": "战斗与生存"},
    "CollectionDropRate": {"label": "采集物掉落倍率", "category": "资源与掉落"},
    "CollectionObjectHpRate": {"label": "采集物耐久倍率", "category": "资源与掉落"},
    "CollectionObjectRespawnSpeedRate": {"label": "采集物重生速度倍率", "category": "资源与掉落"},
    "EnemyDropItemRate": {"label": "敌人掉落倍率", "category": "资源与掉落"},
    "SupplyDropSpan": {"label": "补给投放间隔（分钟）", "category": "资源与掉落"},
    "EnablePredatorBossPal": {"label": "启用掠食者 Boss", "category": "资源与掉落"},
    "BuildObjectHpRate": {"label": "建筑生命倍率", "category": "建造与据点"},
    "BuildObjectDamageRate": {"label": "建筑受到伤害倍率", "category": "建造与据点"},
    "BuildObjectDeteriorationDamageRate": {"label": "建筑自然劣化倍率", "category": "建造与据点"},
    "BaseCampMaxNum": {"label": "全世界据点上限", "category": "建造与据点", "min": 1, "max": 128, "impact": "high", "help": "只是上限；实际据点和建筑越多，持续计算量越高。"},
    "BaseCampMaxNumInGuild": {"label": "每公会据点上限", "category": "建造与据点", "min": 1, "max": 10, "impact": "medium"},
    "BaseCampWorkerMaxNum": {"label": "每据点工作帕鲁上限", "category": "建造与据点", "min": 1, "max": 50, "impact": "high", "help": "工作帕鲁数量会显著影响基地 AI 计算；当前 15 是偏稳妥的值。"},
    "MaxBuildingLimitNum": {"label": "建筑总数上限", "category": "建造与据点", "min": 0, "max": 1000000, "impact": "high", "help": "0 表示不限。单人世界可保留，但超大型基地最终会增加存档和模拟负担。"},
    "bBuildAreaLimit": {"label": "启用建筑区域限制", "category": "建造与据点"},
    "bEnablePlayerToPlayerDamage": {"label": "玩家互相伤害", "category": "联机规则"},
    "bEnableFriendlyFire": {"label": "友军伤害", "category": "联机规则"},
    "bEnableInvaderEnemy": {"label": "启用袭击事件", "category": "联机规则"},
    "bIsPvP": {"label": "PvP 模式", "category": "联机规则"},
    "bEnableFastTravel": {"label": "允许快速传送", "category": "联机规则"},
    "bEnableFastTravelOnlyBaseCamp": {"label": "仅据点允许快速传送", "category": "联机规则"},
    "bExistPlayerAfterLogout": {"label": "退出后保留玩家身体", "category": "联机规则"},
    "bCanPickupOtherGuildDeathPenaltyDrop": {"label": "可拾取其他公会死亡掉落", "category": "联机规则"},
    "GuildPlayerMaxNum": {"label": "公会人数上限", "category": "联机规则"},
    "GuildRejoinCooldownMinutes": {"label": "重新加入公会冷却（分钟）", "category": "联机规则"},
    "bIsShowJoinLeftMessage": {"label": "显示加入/离开消息", "category": "联机规则"},
    "bEnableVoiceChat": {"label": "语音聊天", "category": "联机规则"},
    "bAllowClientMod": {"label": "允许客户端模组", "category": "联机规则"},
    "AutoSaveSpan": {"label": "自动保存间隔（秒）", "category": "性能与存档", "min": 5, "max": 3600, "impact": "medium", "help": "过短会增加磁盘写入；当前 30 秒兼顾数据安全和负载。"},
    "DropItemMaxNum": {"label": "世界掉落物上限", "category": "性能与存档", "min": 0, "max": 10000, "impact": "high", "help": "提高会增加复制、物理和存档开销；当前 3000 已足够宽裕。"},
    "PhysicsActiveDropItemMaxNum": {"label": "启用物理的掉落物上限", "category": "性能与存档", "impact": "high"},
    "DropItemAliveMaxHours": {"label": "掉落物保留小时数", "category": "性能与存档", "min": 0, "max": 168, "impact": "medium"},
    "ServerReplicatePawnCullDistance": {"label": "角色网络同步距离", "category": "性能与存档", "min": 5000, "max": 15000, "step": 100, "impact": "high", "help": "越高视野同步越完整，但网络和 CPU 负载越高。当前使用官方允许的最高值。"},
    "ItemContainerForceMarkDirtyInterval": {"label": "容器强制同步间隔（秒）", "category": "性能与存档", "min": 0, "max": 1000, "impact": "medium", "help": "值越低同步越及时、开销越高；不建议低于 1。"},
    "PlayerDataPalStorageUpdateCheckTickInterval": {"label": "帕鲁存储更新检查间隔", "category": "性能与存档", "impact": "medium"},
    "MaxGuildsPerFrame": {"label": "每帧处理公会数", "category": "性能与存档", "impact": "medium"},
    "bIsUseBackupSaveData": {"label": "游戏内建备份", "category": "性能与存档", "help": "建议保留；它与面板的独立受管备份互为补充。"},

    # 新版及基础设施参数。官方标为预留或废弃的项目仍给出中文说明，避免直接暴露难懂键名。
    "bActiveUNKO": {"label": "排泄物系统（预留项）", "category": "高级", "help": "游戏预留参数；官方未公布稳定用途，不建议修改。"},
    "DropItemMaxNum_UNKO": {"label": "排泄物掉落上限（预留项）", "category": "高级", "help": "游戏预留参数；官方未公布稳定用途，不建议修改。"},
    "bEnableAimAssistPad": {"label": "手柄瞄准辅助", "category": "战斗与生存"},
    "bEnableAimAssistKeyboard": {"label": "键鼠瞄准辅助", "category": "战斗与生存", "help": "开启后，键盘鼠标玩家会使用游戏提供的瞄准辅助；关闭则完全按鼠标输入。它对服务器性能几乎没有影响。"},
    "bAutoResetGuildNoOnlinePlayers": {"label": "自动清理长期无人公会", "category": "联机规则", "help": "启用后，达到离线时限的公会建筑和据点帕鲁会被删除。"},
    "AutoResetGuildTimeNoOnlinePlayers": {"label": "无人公会清理时限（小时）", "category": "联机规则", "min": 1, "max": 8760, "step": 1, "help": "仅在开启自动清理长期无人公会时生效。"},
    "bIsMultiplay": {"label": "多人服务器模式", "category": "服务器", "help": "专用服务器基础参数，由部署系统锁定。"},
    "bCharacterRecreateInHardcore": {"label": "硬核死亡后允许重建角色", "category": "战斗与生存"},
    "bEnableNonLoginPenalty": {"label": "启用长期未登录惩罚（预留项）", "category": "高级", "help": "官方未公布稳定用途，不建议修改。"},
    "bIsStartLocationSelectByMap": {"label": "允许在地图选择出生点", "category": "联机规则"},
    "bEnableDefenseOtherGuildPlayer": {"label": "据点帕鲁防御其他公会玩家", "category": "联机规则", "help": "主要用于 PvP；启用后据点帕鲁会攻击入侵的其他公会玩家。"},
    "bInvisibleOtherGuildBaseCampAreaFX": {"label": "隐藏其他公会据点范围特效", "category": "联机规则"},
    "CoopPlayerMaxNum": {"label": "合作模式人数上限", "category": "联机规则", "min": 1, "max": 32, "help": "专用服务器实际人数上限主要由“最大玩家数”控制。"},
    "PublicPort": {"label": "对外公布端口", "category": "服务器", "help": "仅用于社区服务器列表，不改变实际监听端口。"},
    "PublicIP": {"label": "对外公布 IP", "category": "服务器", "help": "仅用于社区服务器；当前局域网部署无需填写。"},
    "RCONEnabled": {"label": "启用 RCON 远程控制", "category": "服务器", "help": "由服务器基础设施锁定。"},
    "RCONPort": {"label": "RCON 端口", "category": "服务器", "help": "由服务器基础设施锁定。"},
    "Region": {"label": "服务器区域标识", "category": "服务器", "help": "可留空；主要用于社区服务器列表分类。"},
    "bUseAuth": {"label": "管理接口身份验证", "category": "服务器", "help": "保护管理接口所必需，由部署系统锁定。"},
    "BanListURL": {"label": "官方封禁名单地址", "category": "服务器", "help": "由部署系统锁定。"},
    "RESTAPIEnabled": {"label": "启用管理接口", "category": "服务器", "help": "面板读取状态和执行安全操作所必需。"},
    "RESTAPIPort": {"label": "管理接口端口", "category": "服务器", "help": "由部署系统锁定。"},
    "bShowPlayerList": {"label": "在 ESC 菜单显示玩家列表", "category": "联机规则"},
    "ChatPostLimitPerMinute": {"label": "每分钟聊天消息上限", "category": "联机规则", "min": 1, "max": 600},
    "CrossplayPlatforms": {"label": "允许连接的平台", "category": "联机规则", "help": "平台专名保留原文；当前允许 Steam、Xbox、PS5 和 Mac。"},
    "LogFormatType": {
        "label": "服务器日志格式", "category": "高级", "choices": ["Text", "Json"],
        "choice_labels": {"Text": "纯文本", "Json": "结构化 JSON"}, "help": "由日志收集流程锁定。",
    },
    "bAllowGlobalPalboxExport": {"label": "允许存入全局帕鲁终端", "category": "联机规则"},
    "bAllowGlobalPalboxImport": {"label": "允许从全局帕鲁终端取回", "category": "联机规则"},
    "DenyTechnologyList": {"label": "禁用科技项目列表", "category": "高级", "help": "填写科技项目 ID；由基础设施锁定以避免格式错误。"},
    "AutoTransferMasterCheckIntervalSeconds": {"label": "主存档自动转移检查间隔（秒）", "category": "性能与存档", "help": "新版存档维护参数；通常保持默认值。"},
    "AutoTransferMasterThresholdDays": {"label": "主存档自动转移阈值（天）", "category": "性能与存档", "help": "新版存档维护参数；通常保持默认值。"},
    "BlockRespawnTime": {"label": "死亡后复活等待时间（秒）", "category": "战斗与生存", "min": 0, "max": 3600, "step": 0.5},
    "RespawnPenaltyDurationThreshold": {"label": "再次死亡惩罚判定时限（秒）", "category": "战斗与生存", "min": 0, "max": 86400, "step": 1, "help": "存活时间低于此阈值后再次死亡，会应用复活等待倍率。0 表示关闭该判定。"},
    "RespawnPenaltyTimeScale": {"label": "再次死亡复活等待倍率", "category": "战斗与生存", "min": 0, "max": 100, "step": 0.1},
    "bDisplayPvPItemNumOnWorldMap_BaseCamp": {"label": "地图显示据点 PvP 专属物品数", "category": "联机规则"},
    "bDisplayPvPItemNumOnWorldMap_Player": {"label": "地图显示玩家位置和 PvP 专属物品数", "category": "联机规则"},
    "AdditionalDropItemWhenPlayerKillingInPvPMode": {"label": "PvP 击杀玩家时额外掉落物品 ID", "category": "资源与掉落"},
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode": {"label": "PvP 击杀额外掉落数量", "category": "资源与掉落", "min": 0, "max": 999},
    "bAdditionalDropItemWhenPlayerKillingInPvPMode": {"label": "PvP 击杀玩家时启用额外掉落", "category": "资源与掉落"},
    "VoiceChatMaxVolumeDistance": {"label": "语音保持最大音量距离", "category": "联机规则", "min": 0, "max": 100000, "step": 100},
    "VoiceChatZeroVolumeDistance": {"label": "语音完全听不见距离", "category": "联机规则", "min": 0, "max": 100000, "step": 100},
    "bAllowEnhanceStat_Health": {"label": "允许强化生命值", "category": "时间与成长"},
    "bAllowEnhanceStat_Attack": {"label": "允许强化攻击力", "category": "时间与成长"},
    "bAllowEnhanceStat_Stamina": {"label": "允许强化耐力", "category": "时间与成长"},
    "bAllowEnhanceStat_Weight": {"label": "允许强化负重", "category": "时间与成长"},
    "bAllowEnhanceStat_WorkSpeed": {"label": "允许强化工作速度", "category": "时间与成长"},
    "bEnableBuildingPlayerUIdDisplay": {"label": "建筑显示建造者玩家 ID", "category": "建造与据点"},
    "BuildingNameDisplayCacheTTLSeconds": {"label": "建筑名称显示缓存时间（秒）", "category": "建造与据点", "min": 0, "max": 3600, "step": 1},
}

LOCKED_SETTINGS = {
    "bIsMultiplay", "PublicPort", "PublicIP", "RCONEnabled", "RCONPort", "RESTAPIEnabled",
    "RESTAPIPort", "bUseAuth", "BanListURL", "LogFormatType", "DenyTechnologyList",
}

TIMER_LABELS = {
    "palworld-backup-daily.timer": "每日备份",
    "palworld-backup-weekly.timer": "每周备份",
    "palworld-backup-monthly.timer": "每月备份",
    "palworld-update.timer": "自动更新",
    "palworld-health.timer": "健康检查",
    "palworld-maintenance.timer": "磁盘维护",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def redact(text: str, limit: int | None = 12000) -> str:
    value = re.sub(r"(?i)(AdminPassword|ServerPassword)(\s*[=:]\s*)([^,\s]+)", r"\1\2<已隐藏>", text)
    value = re.sub(r"(?i)(sentry_key=)[A-Za-z0-9]+", r"\1<已隐藏>", value)
    value = re.sub(r"(?i)(Authorization:\s*)(\S+)", r"\1<已隐藏>", value)
    return value[-limit:] if limit is not None else value


def decode_setting(raw: str) -> Any:
    if raw in {"True", "False"}:
        return raw == "True"
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw[1:-1]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return float(raw)
    return raw


def setting_category(key: str) -> str:
    if key in SETTING_META:
        return str(SETTING_META[key].get("category", "高级"))
    if any(word in key for word in ("Damage", "Stomach", "Stamina", "Regene", "Penalty", "Durability")):
        return "战斗与生存"
    if any(word in key for word in ("Drop", "Collection", "Supply", "Farm")):
        return "资源与掉落"
    if any(word in key for word in ("Build", "BaseCamp", "Worker")):
        return "建造与据点"
    if any(word in key for word in ("Guild", "PvP", "Voice", "Player")):
        return "联机规则"
    return "高级"


def settings_payload() -> dict[str, Any]:
    raw_values = manager.settings_map()
    values: list[dict[str, Any]] = []
    for key, raw in raw_values.items():
        if key in manager.SENSITIVE_KEYS:
            continue
        meta = dict(SETTING_META.get(key, {}))
        value = decode_setting(raw)
        if "choices" in meta:
            kind = "select"
        elif isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, int) and not isinstance(value, bool):
            kind = "integer"
        elif isinstance(value, float):
            kind = "number"
        else:
            kind = "text"
        values.append({
            "key": key,
            "label": meta.get("label", key),
            "category": meta.get("category", setting_category(key)),
            "value": value,
            "type": kind,
            "choices": meta.get("choices"),
            "choice_labels": meta.get("choice_labels", {}),
            "min": meta.get("min"),
            "max": meta.get("max"),
            "step": meta.get("step", 0.1 if kind == "number" else 1),
            "impact": meta.get("impact"),
            "help": meta.get("help"),
            "editable": key not in LOCKED_SETTINGS,
        })
    order = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    values.sort(key=lambda item: (order.get(item["category"], 999), item["label"].lower(), item["key"]))
    return {"categories": CATEGORY_ORDER, "settings": values}


def backup_payload() -> dict[str, Any]:
    items = []
    total = 0
    # A retention pass may remove an archive between listing and statting it.
    # Skipping that one entry keeps the dashboard refresh reliable.
    for path in reversed(manager.managed_archives()):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        total += stat.st_size
        items.append({
            "id": path.relative_to(manager.MANAGED).as_posix(),
            "name": path.name,
            "kind": path.parent.name,
            "size_bytes": stat.st_size,
            "created_at": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
        })
    return {"items": items, "count": len(items), "total_bytes": total}


def selected_backup(value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2:
        raise manager.ManagerError("备份标识无效")
    candidate = (manager.MANAGED / Path(*relative.parts)).resolve()
    root = manager.MANAGED.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise manager.ManagerError("备份标识超出受管目录") from exc
    if not candidate.is_file() or candidate.suffixes[-2:] != [".tar", ".gz"]:
        raise manager.ManagerError("所选备份不存在")
    return candidate


class HostSampler:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.previous: dict[str, Any] | None = None
        self.cpu_model = self._cpu_model()
        self.interface = self._interface()
        self.disk_devices = self._disk_devices()
        self.ssd_temperature_c: float | None = None
        self.ssd_temperature_checked_at = 0.0

    @staticmethod
    def _cpu_model() -> str:
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return "Linux host"

    @staticmethod
    def _interface() -> str | None:
        try:
            for line in Path("/proc/net/route").read_text().splitlines()[1:]:
                parts = line.split()
                if len(parts) > 1 and parts[1] == "00000000":
                    return parts[0]
        except OSError:
            pass
        for path in Path("/sys/class/net").glob("*"):
            if path.name != "lo":
                return path.name
        return None

    @staticmethod
    def _disk_devices() -> list[str]:
        result = manager.run(["lsblk", "-dn", "-o", "NAME,TYPE"], check=False, timeout=10)
        devices = []
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "disk":
                devices.append(f"/dev/{parts[0]}")
        return devices

    def _ssd_temperature(self, now: float) -> float | None:
        interval = (
            SSD_TEMPERATURE_SAMPLE_SECONDS
            if self.ssd_temperature_c is not None
            else SSD_TEMPERATURE_RETRY_SECONDS
        )
        if now - self.ssd_temperature_checked_at < interval:
            return self.ssd_temperature_c
        refreshed_devices = self._disk_devices()
        if refreshed_devices:
            self.disk_devices = refreshed_devices
        temperatures: list[float] = []
        for device in self.disk_devices:
            try:
                result = manager.run(["smartctl", "-Aj", device], check=False, timeout=15)
            except manager.ManagerError:
                continue
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                continue
            temperature = payload.get("temperature") if isinstance(payload, dict) else None
            value = temperature.get("current") if isinstance(temperature, dict) else None
            if not isinstance(value, (int, float)):
                table = payload.get("ata_smart_attributes", {}).get("table", []) if isinstance(payload, dict) else []
                for attribute in table if isinstance(table, list) else []:
                    if isinstance(attribute, dict) and attribute.get("id") == 194:
                        raw = attribute.get("raw")
                        value = raw.get("value") if isinstance(raw, dict) else None
                        break
            if isinstance(value, (int, float)) and 0 < float(value) < 120:
                temperatures.append(float(value))
        self.ssd_temperature_checked_at = now
        if temperatures:
            self.ssd_temperature_c = max(temperatures)
        return self.ssd_temperature_c

    @staticmethod
    def _cpu_ticks() -> tuple[int, int, dict[str, tuple[int, int]]]:
        rows = Path("/proc/stat").read_text().splitlines()

        def parse(line: str) -> tuple[int, int]:
            numbers = [int(value) for value in line.split()[1:]]
            total = sum(numbers)
            idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
            return total, idle

        total, idle = parse(rows[0])
        cores = {line.split()[0]: parse(line) for line in rows[1:] if re.match(r"^cpu\d+\s", line)}
        return total, idle, cores

    @staticmethod
    def _memory() -> dict[str, int]:
        result: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            result[key] = int(value.strip().split()[0]) * 1024
        return result

    @staticmethod
    def _temperature() -> float | None:
        values: list[float] = []
        for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
            try:
                name = (hwmon / "name").read_text().strip()
            except OSError:
                continue
            if name not in {"coretemp", "k10temp", "zenpower"}:
                continue
            for path in hwmon.glob("temp*_input"):
                with contextlib.suppress(OSError, ValueError):
                    value = int(path.read_text().strip()) / 1000
                    if 0 < value < 120:
                        values.append(value)
        return max(values) if values else None

    @staticmethod
    def _cgroup_usage() -> int | None:
        control_group = manager.service_value("ControlGroup")
        if not control_group:
            return None
        try:
            for line in (Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec "):
                    return int(line.split()[1])
        except (OSError, ValueError):
            pass
        return None

    @staticmethod
    def _network_counters(interface: str | None) -> dict[str, int]:
        names = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped")
        counters = {name: 0 for name in names}
        if not interface:
            return counters
        base = Path("/sys/class/net") / interface / "statistics"
        for name in names:
            with contextlib.suppress(OSError, ValueError):
                counters[name] = int((base / name).read_text())
        return counters

    @staticmethod
    def _disk_counters() -> dict[str, int]:
        counters = {
            "read_ios": 0, "read_sectors": 0, "write_ios": 0, "write_sectors": 0,
            "in_flight": 0, "io_ms": 0, "weighted_io_ms": 0,
        }
        try:
            device = os.stat(manager.BASE).st_dev
            fields = (Path("/sys/dev/block") / f"{os.major(device)}:{os.minor(device)}" / "stat").read_text().split()
            if len(fields) >= 11:
                values = [int(value) for value in fields]
                counters.update({
                    "read_ios": values[0],
                    "read_sectors": values[2],
                    "write_ios": values[4],
                    "write_sectors": values[6],
                    "in_flight": values[8],
                    "io_ms": values[9],
                    "weighted_io_ms": values[10],
                })
        except (OSError, ValueError):
            pass
        return counters

    @staticmethod
    def _cpu_frequency() -> float | None:
        values: list[float] = []
        for path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
            with contextlib.suppress(OSError, ValueError):
                value = float(path.read_text().strip()) / 1000
                if value > 0:
                    values.append(value)
        return sum(values) / len(values) if values else None

    @staticmethod
    def _process_stats(pid: int | None) -> dict[str, int]:
        result = {"pid": int(pid or 0), "threads": 0, "fds": 0, "read_bytes": 0, "write_bytes": 0, "context_switches": 0}
        if not pid:
            return result
        try:
            for line in (Path("/proc") / str(pid) / "status").read_text(errors="replace").splitlines():
                key, _, value = line.partition(":")
                if key == "Threads":
                    result["threads"] = int(value.strip())
                elif key in {"voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"}:
                    result["context_switches"] += int(value.strip())
        except (OSError, ValueError):
            pass
        try:
            for line in (Path("/proc") / str(pid) / "io").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in {"read_bytes", "write_bytes"}:
                    result[key] = int(value.strip())
        except (OSError, ValueError):
            pass
        with contextlib.suppress(OSError):
            result["fds"] = sum(1 for _ in (Path("/proc") / str(pid) / "fd").iterdir())
        return result

    def sample(self, game_pid: int | None = None) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            total_ticks, idle_ticks, core_ticks = self._cpu_ticks()
            cgroup_usage = self._cgroup_usage()
            network = self._network_counters(self.interface)
            disk_io = self._disk_counters()
            process = self._process_stats(game_pid)
            current = {
                "time": now, "total": total_ticks, "idle": idle_ticks, "cores": core_ticks,
                "cgroup": cgroup_usage, "network": network, "disk": disk_io, "process": process,
            }
            cpu_percent = peak_core_percent = game_host_percent = game_core_percent = None
            network_rates: dict[str, float | None] = {name: None for name in network}
            disk_rates: dict[str, float | None] = {
                "read_bps": None, "write_bps": None, "read_iops": None, "write_iops": None,
                "busy_percent": None, "queue_depth": None,
            }
            process_rates: dict[str, float | None] = {"read_bps": None, "write_bps": None, "context_switches_per_second": None}
            if self.previous and now > self.previous["time"]:
                elapsed = now - self.previous["time"]
                total_delta = total_ticks - self.previous["total"]
                idle_delta = idle_ticks - self.previous["idle"]
                if total_delta > 0:
                    cpu_percent = max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))
                core_values: list[float] = []
                previous_cores = self.previous.get("cores") or {}
                for name, (core_total, core_idle) in core_ticks.items():
                    old = previous_cores.get(name)
                    if not old:
                        continue
                    core_delta = core_total - old[0]
                    core_idle_delta = core_idle - old[1]
                    if core_delta > 0:
                        core_values.append(max(0.0, min(100.0, (1 - core_idle_delta / core_delta) * 100)))
                peak_core_percent = max(core_values) if core_values else None
                old_usage = self.previous.get("cgroup")
                if cgroup_usage is not None and old_usage is not None and cgroup_usage >= old_usage:
                    game_core_percent = (cgroup_usage - old_usage) / 1_000_000 / elapsed * 100
                    game_host_percent = game_core_percent / max(1, os.cpu_count() or 1)
                old_network = self.previous.get("network") or {}
                for name, value in network.items():
                    old_value = old_network.get(name)
                    if isinstance(old_value, int):
                        network_rates[name] = max(0.0, (value - old_value) / elapsed)
                old_disk = self.previous.get("disk") or {}
                if old_disk:
                    disk_rates = {
                        "read_bps": max(0.0, (disk_io["read_sectors"] - old_disk.get("read_sectors", 0)) * 512 / elapsed),
                        "write_bps": max(0.0, (disk_io["write_sectors"] - old_disk.get("write_sectors", 0)) * 512 / elapsed),
                        "read_iops": max(0.0, (disk_io["read_ios"] - old_disk.get("read_ios", 0)) / elapsed),
                        "write_iops": max(0.0, (disk_io["write_ios"] - old_disk.get("write_ios", 0)) / elapsed),
                        "busy_percent": max(0.0, min(100.0, (disk_io["io_ms"] - old_disk.get("io_ms", 0)) / elapsed / 10)),
                        "queue_depth": max(0.0, (disk_io["weighted_io_ms"] - old_disk.get("weighted_io_ms", 0)) / elapsed / 1000),
                    }
                old_process = self.previous.get("process") or {}
                if process["pid"] and process["pid"] == old_process.get("pid"):
                    process_rates = {
                        "read_bps": max(0.0, (process["read_bytes"] - old_process.get("read_bytes", 0)) / elapsed),
                        "write_bps": max(0.0, (process["write_bytes"] - old_process.get("write_bytes", 0)) / elapsed),
                        "context_switches_per_second": max(
                            0.0, (process["context_switches"] - old_process.get("context_switches", 0)) / elapsed
                        ),
                    }
            self.previous = current

            memory = self._memory()
            total_memory = memory.get("MemTotal", 0)
            available_memory = memory.get("MemAvailable", 0)
            used_memory = max(0, total_memory - available_memory)
            disk = shutil.disk_usage(manager.BASE)
            try:
                load = list(os.getloadavg())
                uptime = float(Path("/proc/uptime").read_text().split()[0])
            except (OSError, ValueError):
                load, uptime = [0.0, 0.0, 0.0], 0.0
            return {
                "cpu_model": self.cpu_model,
                "logical_cpus": os.cpu_count(),
                "cpu_percent": cpu_percent,
                "peak_core_percent": peak_core_percent,
                "cpu_frequency_mhz": self._cpu_frequency(),
                "memory_total_bytes": total_memory,
                "memory_used_bytes": used_memory,
                "memory_percent": used_memory / total_memory * 100 if total_memory else None,
                "swap_total_bytes": memory.get("SwapTotal", 0),
                "swap_used_bytes": max(0, memory.get("SwapTotal", 0) - memory.get("SwapFree", 0)),
                "disk_total_bytes": disk.total,
                "disk_used_bytes": disk.used,
                "disk_free_bytes": disk.free,
                "disk_percent": disk.used / disk.total * 100 if disk.total else None,
                "temperature_c": self._temperature(),
                "ssd_temperature_c": self._ssd_temperature(now),
                "load_average": load,
                "uptime_seconds": uptime,
                "network_interface": self.interface,
                "network_rx_bytes_per_second": network_rates["rx_bytes"],
                "network_tx_bytes_per_second": network_rates["tx_bytes"],
                "network_rx_packets_per_second": network_rates["rx_packets"],
                "network_tx_packets_per_second": network_rates["tx_packets"],
                "network_rx_errors_per_second": network_rates["rx_errors"],
                "network_tx_errors_per_second": network_rates["tx_errors"],
                "network_rx_dropped_per_second": network_rates["rx_dropped"],
                "network_tx_dropped_per_second": network_rates["tx_dropped"],
                "disk_read_bytes_per_second": disk_rates["read_bps"],
                "disk_write_bytes_per_second": disk_rates["write_bps"],
                "disk_read_iops": disk_rates["read_iops"],
                "disk_write_iops": disk_rates["write_iops"],
                "disk_busy_percent": disk_rates["busy_percent"],
                "disk_queue_depth": disk_rates["queue_depth"],
                "game_cpu_host_percent": game_host_percent,
                "game_cpu_one_core_percent": game_core_percent,
                "game_threads": process["threads"] or None,
                "game_fds": process["fds"] or None,
                "game_read_bytes_per_second": process_rates["read_bps"],
                "game_write_bytes_per_second": process_rates["write_bps"],
                "game_context_switches_per_second": process_rates["context_switches_per_second"],
            }


class PerformanceHistory:
    """Persist bounded server and Windows-client metrics in one low-priority SQLite database."""

    COLUMNS = (
        "ts", "service_active", "player_count", "max_players", "server_fps", "frame_time_ms",
        "host_cpu_percent", "game_cpu_host_percent", "game_cpu_one_core_percent", "memory_percent",
        "memory_used_bytes", "game_memory_bytes", "swap_used_bytes", "temperature_c", "ssd_temperature_c", "load_1",
        "load_5", "load_15", "network_rx_bps", "network_tx_bps", "base_camp_count", "world_days",
        "host_peak_core_percent", "cpu_frequency_mhz", "disk_read_bps", "disk_write_bps",
        "disk_read_iops", "disk_write_iops", "disk_busy_percent", "disk_queue_depth",
        "network_rx_pps", "network_tx_pps", "network_rx_errors_ps", "network_tx_errors_ps",
        "network_rx_dropped_ps", "network_tx_dropped_ps", "game_threads", "game_fds",
        "game_read_bps", "game_write_bps", "game_context_switches_ps",
    )
    SERVER_EXTRA_COLUMNS = {
        "host_peak_core_percent": "REAL", "cpu_frequency_mhz": "REAL",
        "disk_read_bps": "REAL", "disk_write_bps": "REAL", "disk_read_iops": "REAL",
        "disk_write_iops": "REAL", "disk_busy_percent": "REAL", "disk_queue_depth": "REAL",
        "network_rx_pps": "REAL", "network_tx_pps": "REAL", "network_rx_errors_ps": "REAL",
        "network_tx_errors_ps": "REAL", "network_rx_dropped_ps": "REAL", "network_tx_dropped_ps": "REAL",
        "game_threads": "INTEGER", "game_fds": "INTEGER", "game_read_bps": "REAL",
        "game_write_bps": "REAL", "game_context_switches_ps": "REAL",
        "ssd_temperature_c": "REAL",
    }
    QUERY_COLUMNS = (
        "ts", "service_active", "player_count", "server_fps", "frame_time_ms", "host_cpu_percent",
        "game_cpu_one_core_percent", "memory_percent", "game_memory_bytes", "temperature_c", "ssd_temperature_c",
    )
    CLIENT_COLUMNS = (
        "ts", "received_ts", "client_ts", "host_id", "host_label", "collector_version", "session_id", "game_running",
        "game_pid", "game_executable_name", "game_process_role", "game_start_ts", "sample_interval_seconds", "cpu_percent", "cpu_peak_core_percent",
        "cpu_frequency_mhz", "cpu_per_core_json", "memory_percent", "memory_used_bytes",
        "memory_available_bytes", "disk_read_bps", "disk_write_bps", "disk_read_iops",
        "disk_write_iops", "disk_busy_percent", "disk_percent", "disk_free_bytes", "game_disk_percent",
        "game_disk_free_bytes", "game_disk_path", "network_rx_bps", "network_tx_bps", "network_rx_pps",
        "network_tx_pps", "network_rx_errors_ps", "network_tx_errors_ps", "network_rx_dropped_ps",
        "network_tx_dropped_ps", "gpu_name", "gpu_util_percent", "gpu_memory_util_percent",
        "gpu_memory_used_bytes", "gpu_memory_total_bytes", "gpu_temperature_c", "gpu_power_w",
        "gpu_power_limit_w", "gpu_clock_mhz", "gpu_memory_clock_mhz", "game_cpu_process_percent",
        "game_cpu_host_percent", "game_memory_rss_bytes", "game_memory_private_bytes", "game_read_bps",
        "game_write_bps", "game_threads", "game_handles", "game_uptime_seconds", "upload_latency_ms",
        "upload_failures", "queue_depth",
    )
    CLIENT_QUERY_COLUMNS = (
        "ts", "client_ts", "session_id", "game_running", "game_executable_name", "game_process_role", "sample_interval_seconds", "cpu_percent",
        "cpu_peak_core_percent", "memory_percent", "disk_busy_percent", "disk_percent", "game_disk_percent", "gpu_util_percent",
        "gpu_memory_util_percent", "gpu_memory_used_bytes", "gpu_memory_total_bytes",
        "gpu_temperature_c", "gpu_power_w", "game_cpu_process_percent", "game_cpu_host_percent",
        "game_memory_rss_bytes", "game_memory_private_bytes", "game_read_bps", "game_write_bps",
        "upload_latency_ms", "upload_failures", "queue_depth",
    )
    CLIENT_EXTRA_COLUMNS = {
        "game_executable_name": "TEXT",
        "game_process_role": "TEXT",
    }

    def __init__(self, path: Path, sampler: HostSampler) -> None:
        self.path = path
        self.sampler = sampler
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state_lock = threading.Lock()
        self.initialize_lock = threading.Lock()
        self.last_error: str | None = None
        self.samples_written = 0
        self.client_samples_written = 0
        self.initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA wal_autocheckpoint=512")
        connection.execute(f"PRAGMA journal_size_limit={PERFORMANCE_WAL_LIMIT_BYTES}")
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        maximum_pages = max(1024, PERFORMANCE_DB_MAIN_LIMIT_BYTES // max(1, page_size))
        connection.execute(f"PRAGMA max_page_count={maximum_pages}")
        return connection

    @contextlib.contextmanager
    def _connection(self) -> Any:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _backfill_events(connection: sqlite3.Connection) -> None:
        """Seed existing verified backups and the installed build exactly once."""
        for path in manager.managed_archives():
            with contextlib.suppress(OSError):
                stat_result = path.stat()
                kind = path.parent.name
                connection.execute(
                    """
                    INSERT OR IGNORE INTO events
                        (ts, kind, source, title, detail, metadata_json, dedupe_key)
                    VALUES (?, 'backup', 'backfill', ?, ?, ?, ?)
                    """,
                    (
                        int(stat_result.st_mtime),
                        f"{kind} 备份完成",
                        f"{path.name} · {manager.human_size(stat_result.st_size)} · 受管归档",
                        json.dumps({"kind": kind, "path": str(path), "archive_bytes": stat_result.st_size}, ensure_ascii=False),
                        f"backup:{path.name}",
                    ),
                )
        with contextlib.suppress(OSError, manager.ManagerError):
            manifest_time = int(manager.MANIFEST.stat().st_mtime)
            build = manager.installed_build()
            connection.execute(
                """
                INSERT OR IGNORE INTO events
                    (ts, kind, source, title, detail, metadata_json, dedupe_key)
                VALUES (?, 'update', 'backfill', 'Palworld 构建已部署', ?, ?, ?)
                """,
                (
                    manifest_time,
                    f"Steam 构建 {build}",
                    json.dumps({"new_build": build}, ensure_ascii=False),
                    f"manifest-build:{build}:{manifest_time}",
                ),
            )

    def initialize(self) -> None:
        if self.initialized:
            return
        with self.initialize_lock:
            if self.initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                connection.execute(f"PRAGMA journal_size_limit={PERFORMANCE_WAL_LIMIT_BYTES}")
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
                maximum_pages = max(1024, PERFORMANCE_DB_MAIN_LIMIT_BYTES // max(1, page_size))
                connection.execute(f"PRAGMA max_page_count={maximum_pages}")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS samples (
                        id INTEGER PRIMARY KEY,
                        ts INTEGER NOT NULL,
                        service_active INTEGER NOT NULL,
                        player_count INTEGER,
                        max_players INTEGER,
                        server_fps REAL,
                        frame_time_ms REAL,
                        host_cpu_percent REAL,
                        game_cpu_host_percent REAL,
                        game_cpu_one_core_percent REAL,
                        memory_percent REAL,
                        memory_used_bytes INTEGER,
                        game_memory_bytes INTEGER,
                        swap_used_bytes INTEGER,
                        temperature_c REAL,
                        ssd_temperature_c REAL,
                        load_1 REAL,
                        load_5 REAL,
                        load_15 REAL,
                        network_rx_bps REAL,
                        network_tx_bps REAL,
                        base_camp_count INTEGER,
                        world_days INTEGER,
                        host_peak_core_percent REAL,
                        cpu_frequency_mhz REAL,
                        disk_read_bps REAL,
                        disk_write_bps REAL,
                        disk_read_iops REAL,
                        disk_write_iops REAL,
                        disk_busy_percent REAL,
                        disk_queue_depth REAL,
                        network_rx_pps REAL,
                        network_tx_pps REAL,
                        network_rx_errors_ps REAL,
                        network_tx_errors_ps REAL,
                        network_rx_dropped_ps REAL,
                        network_tx_dropped_ps REAL,
                        game_threads INTEGER,
                        game_fds INTEGER,
                        game_read_bps REAL,
                        game_write_bps REAL,
                        game_context_switches_ps REAL
                    )
                    """
                )
                existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(samples)")}
                for name, column_type in self.SERVER_EXTRA_COLUMNS.items():
                    if name not in existing:
                        connection.execute(f"ALTER TABLE samples ADD COLUMN {name} {column_type}")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS client_samples (
                        id INTEGER PRIMARY KEY,
                        ts INTEGER NOT NULL,
                        received_ts INTEGER NOT NULL,
                        client_ts INTEGER NOT NULL,
                        host_id TEXT NOT NULL,
                        host_label TEXT NOT NULL,
                        collector_version TEXT NOT NULL,
                        session_id TEXT,
                        game_running INTEGER NOT NULL,
                        game_pid INTEGER,
                        game_executable_name TEXT,
                        game_process_role TEXT,
                        game_start_ts INTEGER,
                        sample_interval_seconds INTEGER NOT NULL,
                        cpu_percent REAL,
                        cpu_peak_core_percent REAL,
                        cpu_frequency_mhz REAL,
                        cpu_per_core_json TEXT,
                        memory_percent REAL,
                        memory_used_bytes INTEGER,
                        memory_available_bytes INTEGER,
                        disk_read_bps REAL,
                        disk_write_bps REAL,
                        disk_read_iops REAL,
                        disk_write_iops REAL,
                        disk_busy_percent REAL,
                        disk_percent REAL,
                        disk_free_bytes INTEGER,
                        game_disk_percent REAL,
                        game_disk_free_bytes INTEGER,
                        game_disk_path TEXT,
                        network_rx_bps REAL,
                        network_tx_bps REAL,
                        network_rx_pps REAL,
                        network_tx_pps REAL,
                        network_rx_errors_ps REAL,
                        network_tx_errors_ps REAL,
                        network_rx_dropped_ps REAL,
                        network_tx_dropped_ps REAL,
                        gpu_name TEXT,
                        gpu_util_percent REAL,
                        gpu_memory_util_percent REAL,
                        gpu_memory_used_bytes INTEGER,
                        gpu_memory_total_bytes INTEGER,
                        gpu_temperature_c REAL,
                        gpu_power_w REAL,
                        gpu_power_limit_w REAL,
                        gpu_clock_mhz REAL,
                        gpu_memory_clock_mhz REAL,
                        game_cpu_process_percent REAL,
                        game_cpu_host_percent REAL,
                        game_memory_rss_bytes INTEGER,
                        game_memory_private_bytes INTEGER,
                        game_read_bps REAL,
                        game_write_bps REAL,
                        game_threads INTEGER,
                        game_handles INTEGER,
                        game_uptime_seconds REAL,
                        upload_latency_ms REAL,
                        upload_failures INTEGER,
                        queue_depth INTEGER
                    )
                    """
                )
                existing_client = {str(row["name"]) for row in connection.execute("PRAGMA table_info(client_samples)")}
                for name, column_type in self.CLIENT_EXTRA_COLUMNS.items():
                    if name not in existing_client:
                        connection.execute(f"ALTER TABLE client_samples ADD COLUMN {name} {column_type}")
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
                connection.execute("CREATE INDEX IF NOT EXISTS samples_ts_idx ON samples(ts)")
                connection.execute("CREATE INDEX IF NOT EXISTS samples_players_ts_idx ON samples(player_count, ts)")
                connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS client_samples_host_time_idx ON client_samples(host_id, client_ts)")
                connection.execute("CREATE INDEX IF NOT EXISTS client_samples_ts_idx ON client_samples(ts)")
                connection.execute("CREATE INDEX IF NOT EXISTS client_samples_game_ts_idx ON client_samples(game_running, ts)")
                connection.execute("CREATE INDEX IF NOT EXISTS client_samples_session_idx ON client_samples(session_id, ts)")
                connection.execute("CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts)")
                self._backfill_events(connection)
            # Backup/update jobs run as the dedicated ``palworld`` account and
            # write timeline markers into the same WAL database.  Keep the
            # database private to root + that service account while allowing
            # both writers to open the main file and SQLite sidecars.
            with contextlib.suppress(OSError):
                manager.give_to_palworld(self.path)
                self.path.chmod(0o660)
            self.initialized = True

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number and abs(number) != float("inf") else None

    @classmethod
    def _integer(cls, value: Any) -> int | None:
        number = cls._number(value)
        return int(number) if number is not None else None

    @staticmethod
    def _database_paths(path: Path) -> tuple[Path, Path, Path]:
        return path, Path(f"{path}-wal"), Path(f"{path}-shm")

    def database_size(self) -> int:
        return sum(path.stat().st_size for path in self._database_paths(self.path) if path.exists())

    def database_used_size(self, connection: sqlite3.Connection) -> int:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        auxiliary = sum(
            path.stat().st_size for path in self._database_paths(self.path)[1:] if path.exists()
        )
        return max(0, page_count - free_pages) * page_size + auxiliary

    def capture(self) -> dict[str, Any]:
        snapshot = manager.health_snapshot()
        metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
        host = self.sampler.sample(self._integer(snapshot.get("game_pid")))
        loads = host.get("load_average") if isinstance(host.get("load_average"), list) else []
        return {
            "ts": int(time.time()),
            "service_active": 1 if snapshot.get("service_active") else 0,
            "player_count": self._integer(metrics.get("currentplayernum")),
            "max_players": self._integer(metrics.get("maxplayernum")),
            "server_fps": self._number(metrics.get("serverfpsaverage", metrics.get("serverfps"))),
            "frame_time_ms": self._number(metrics.get("serverframetime")),
            "host_cpu_percent": self._number(host.get("cpu_percent")),
            "game_cpu_host_percent": self._number(host.get("game_cpu_host_percent")),
            "game_cpu_one_core_percent": self._number(host.get("game_cpu_one_core_percent")),
            "memory_percent": self._number(host.get("memory_percent")),
            "memory_used_bytes": self._integer(host.get("memory_used_bytes")),
            "game_memory_bytes": self._integer(snapshot.get("memory_bytes")),
            "swap_used_bytes": self._integer(host.get("swap_used_bytes")),
            "temperature_c": self._number(host.get("temperature_c")),
            "ssd_temperature_c": self._number(host.get("ssd_temperature_c")),
            "load_1": self._number(loads[0] if len(loads) > 0 else None),
            "load_5": self._number(loads[1] if len(loads) > 1 else None),
            "load_15": self._number(loads[2] if len(loads) > 2 else None),
            "network_rx_bps": self._number(host.get("network_rx_bytes_per_second")),
            "network_tx_bps": self._number(host.get("network_tx_bytes_per_second")),
            "base_camp_count": self._integer(metrics.get("basecampnum")),
            "world_days": self._integer(metrics.get("days")),
            "host_peak_core_percent": self._number(host.get("peak_core_percent")),
            "cpu_frequency_mhz": self._number(host.get("cpu_frequency_mhz")),
            "disk_read_bps": self._number(host.get("disk_read_bytes_per_second")),
            "disk_write_bps": self._number(host.get("disk_write_bytes_per_second")),
            "disk_read_iops": self._number(host.get("disk_read_iops")),
            "disk_write_iops": self._number(host.get("disk_write_iops")),
            "disk_busy_percent": self._number(host.get("disk_busy_percent")),
            "disk_queue_depth": self._number(host.get("disk_queue_depth")),
            "network_rx_pps": self._number(host.get("network_rx_packets_per_second")),
            "network_tx_pps": self._number(host.get("network_tx_packets_per_second")),
            "network_rx_errors_ps": self._number(host.get("network_rx_errors_per_second")),
            "network_tx_errors_ps": self._number(host.get("network_tx_errors_per_second")),
            "network_rx_dropped_ps": self._number(host.get("network_rx_dropped_per_second")),
            "network_tx_dropped_ps": self._number(host.get("network_tx_dropped_per_second")),
            "game_threads": self._integer(host.get("game_threads")),
            "game_fds": self._integer(host.get("game_fds")),
            "game_read_bps": self._number(host.get("game_read_bytes_per_second")),
            "game_write_bps": self._number(host.get("game_write_bytes_per_second")),
            "game_context_switches_ps": self._number(host.get("game_context_switches_per_second")),
        }

    def _maintenance(self, connection: sqlite3.Connection) -> None:
        cutoff = int(time.time()) - PERFORMANCE_RETENTION_DAYS * 86400
        connection.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        connection.execute("DELETE FROM client_samples WHERE ts < ?", (cutoff,))
        connection.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        for table, maximum in (("samples", PERFORMANCE_MAX_ROWS), ("client_samples", PERFORMANCE_CLIENT_MAX_ROWS)):
            boundary = connection.execute(
                f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?", (maximum,)
            ).fetchone()
            if boundary:
                connection.execute(f"DELETE FROM {table} WHERE id <= ?", (int(boundary[0]),))
        connection.commit()
        if self.database_used_size(connection) >= PERFORMANCE_DB_SOFT_LIMIT_BYTES:
            for _ in range(4):
                oldest = []
                for table in ("samples", "client_samples"):
                    row = connection.execute(f"SELECT MIN(ts) FROM {table}").fetchone()
                    if row and row[0] is not None:
                        oldest.append((int(row[0]), table))
                if not oldest:
                    break
                table = min(oldest)[1]
                connection.execute(
                    f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY ts, id LIMIT 25000)"
                )
                connection.commit()
                connection.execute("PRAGMA incremental_vacuum(4096)")
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                if self.database_used_size(connection) < PERFORMANCE_DB_SOFT_LIMIT_BYTES:
                    break
        connection.execute("PRAGMA incremental_vacuum(256)")
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        connection.execute("PRAGMA optimize")

    def insert(self, sample: dict[str, Any]) -> None:
        self.initialize()
        placeholders = ", ".join("?" for _ in self.COLUMNS)
        columns = ", ".join(self.COLUMNS)
        with self._connection() as connection:
            if self.database_used_size(connection) >= PERFORMANCE_DB_SOFT_LIMIT_BYTES:
                self._maintenance(connection)
            connection.execute(
                f"INSERT INTO samples ({columns}) VALUES ({placeholders})",
                tuple(sample.get(column) for column in self.COLUMNS),
            )
            self.samples_written += 1
            if self.samples_written % 120 == 0:
                self._maintenance(connection)

    @staticmethod
    def _bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise manager.ManagerError(f"本机性能字段无效：{name}") from exc
        if number != number or abs(number) == float("inf") or not minimum <= number <= maximum:
            raise manager.ManagerError(f"本机性能字段超出范围：{name}")
        return number

    @classmethod
    def _bounded_integer(cls, value: Any, name: str, minimum: int, maximum: int) -> int | None:
        number = cls._bounded_number(value, name, minimum, maximum)
        return int(number) if number is not None else None

    @staticmethod
    def _bounded_text(value: Any, name: str, maximum: int, required: bool = False) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise manager.ManagerError(f"本机性能字段无效：{name}")
        text = value.strip()
        if (required and not text) or len(text) > maximum:
            raise manager.ManagerError(f"本机性能字段无效：{name}")
        return text or None

    @classmethod
    def _validate_client_sample(cls, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        client_ts = cls._bounded_integer(payload.get("timestamp"), "timestamp", now - 31 * 86400, now + 300)
        if client_ts is None:
            raise manager.ManagerError("本机性能字段缺失：timestamp")
        host_id = cls._bounded_text(payload.get("host_id"), "host_id", 64, required=True)
        if not host_id or not re.fullmatch(r"[A-Za-z0-9._-]+", host_id):
            raise manager.ManagerError("本机性能字段无效：host_id")
        session_id = cls._bounded_text(payload.get("session_id"), "session_id", 64)
        if session_id and not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
            raise manager.ManagerError("本机性能字段无效：session_id")
        game_running = payload.get("game_running")
        if not isinstance(game_running, bool):
            raise manager.ManagerError("本机性能字段无效：game_running")
        process_role = cls._bounded_text(payload.get("game_process_role"), "game_process_role", 16)
        if process_role not in {None, "shipping", "launcher"}:
            raise manager.ManagerError("本机性能字段无效：game_process_role")
        cores = payload.get("cpu_per_core_percent")
        if cores is not None:
            if not isinstance(cores, list) or len(cores) > 256:
                raise manager.ManagerError("本机性能字段无效：cpu_per_core_percent")
            normalized_cores = [cls._bounded_number(value, "cpu_per_core_percent", 0, 100) for value in cores]
            core_json = json.dumps(normalized_cores, ensure_ascii=False, separators=(",", ":"))
        else:
            core_json = None

        number_fields = {
            "cpu_percent": (0, 100), "cpu_peak_core_percent": (0, 100), "cpu_frequency_mhz": (0, 10000),
            "memory_percent": (0, 100), "disk_read_bps": (0, 10**12), "disk_write_bps": (0, 10**12),
            "disk_read_iops": (0, 10**8), "disk_write_iops": (0, 10**8), "disk_busy_percent": (0, 100),
            "disk_percent": (0, 100), "game_disk_percent": (0, 100),
            "network_rx_bps": (0, 10**12), "network_tx_bps": (0, 10**12), "network_rx_pps": (0, 10**8),
            "network_tx_pps": (0, 10**8), "network_rx_errors_ps": (0, 10**8),
            "network_tx_errors_ps": (0, 10**8), "network_rx_dropped_ps": (0, 10**8),
            "network_tx_dropped_ps": (0, 10**8), "gpu_util_percent": (0, 100),
            "gpu_memory_util_percent": (0, 100), "gpu_temperature_c": (-20, 150), "gpu_power_w": (0, 2000),
            "gpu_power_limit_w": (0, 2000), "gpu_clock_mhz": (0, 10000), "gpu_memory_clock_mhz": (0, 50000),
            "game_cpu_process_percent": (0, 10000), "game_cpu_host_percent": (0, 100),
            "game_read_bps": (0, 10**12), "game_write_bps": (0, 10**12),
            "game_uptime_seconds": (0, 10 * 365 * 86400), "upload_latency_ms": (0, 120000),
        }
        integer_fields = {
            "game_pid": (0, 2**31 - 1), "game_start_ts": (now - 31 * 86400, now + 300),
            "sample_interval_seconds": (5, 300), "memory_used_bytes": (0, 2**60),
            "memory_available_bytes": (0, 2**60), "disk_free_bytes": (0, 2**60),
            "game_disk_free_bytes": (0, 2**60), "gpu_memory_used_bytes": (0, 2**60),
            "gpu_memory_total_bytes": (0, 2**60), "game_memory_rss_bytes": (0, 2**60),
            "game_memory_private_bytes": (0, 2**60), "game_threads": (0, 100000),
            "game_handles": (0, 10**7), "upload_failures": (0, 10**7), "queue_depth": (0, 10**7),
        }
        result: dict[str, Any] = {
            "ts": client_ts,
            "received_ts": now,
            "client_ts": client_ts,
            "host_id": host_id,
            "host_label": cls._bounded_text(payload.get("host_label"), "host_label", 64, required=True),
            "collector_version": cls._bounded_text(payload.get("collector_version"), "collector_version", 32, required=True),
            "session_id": session_id,
            "game_running": 1 if game_running else 0,
            "game_executable_name": cls._bounded_text(payload.get("game_executable_name"), "game_executable_name", 128),
            "game_process_role": process_role,
            "cpu_per_core_json": core_json,
            "gpu_name": cls._bounded_text(payload.get("gpu_name"), "gpu_name", 128),
            "game_disk_path": cls._bounded_text(payload.get("game_disk_path"), "game_disk_path", 16),
        }
        for name, bounds in number_fields.items():
            result[name] = cls._bounded_number(payload.get(name), name, *bounds)
        for name, bounds in integer_fields.items():
            result[name] = cls._bounded_integer(payload.get(name), name, *bounds)
        if result["sample_interval_seconds"] is None:
            raise manager.ManagerError("本机性能字段缺失：sample_interval_seconds")
        return result

    def ingest_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        sample = self._validate_client_sample(payload)
        placeholders = ", ".join("?" for _ in self.CLIENT_COLUMNS)
        columns = ", ".join(self.CLIENT_COLUMNS)
        with self._connection() as connection:
            if self.database_used_size(connection) >= PERFORMANCE_DB_SOFT_LIMIT_BYTES:
                self._maintenance(connection)
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO client_samples ({columns}) VALUES ({placeholders})",
                tuple(sample.get(column) for column in self.CLIENT_COLUMNS),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                self.client_samples_written += 1
                if self.client_samples_written % 360 == 0:
                    self._maintenance(connection)
        return {"accepted": True, "duplicate": not inserted, "server_timestamp": int(time.time())}

    def _loop(self) -> None:
        logged_error: str | None = None
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.insert(self.capture())
                with self.state_lock:
                    self.last_error = None
                logged_error = None
            except Exception as exc:
                message = f"{type(exc).__name__}: {redact(str(exc))}"
                with self.state_lock:
                    self.last_error = message
                if message != logged_error:
                    print(f"Performance recorder error: {message}")
                    logged_error = message
            remaining = max(1.0, PERFORMANCE_SAMPLE_SECONDS - (time.monotonic() - started))
            self.stop_event.wait(remaining)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        try:
            self.initialize()
        except Exception as exc:
            with self.state_lock:
                self.last_error = f"{type(exc).__name__}: {redact(str(exc))}"
            return
        self.thread = threading.Thread(target=self._loop, name="performance-recorder", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * percentile
        lower = int(position)
        upper = min(len(ordered) - 1, lower + 1)
        fraction = position - lower
        return ordered[lower] * (1 - fraction) + ordered[upper] * fraction

    @staticmethod
    def _average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    @classmethod
    def _values(cls, rows: list[sqlite3.Row], key: str) -> list[float]:
        return [float(row[key]) for row in rows if row[key] is not None]

    @staticmethod
    def _range(range_name: str) -> tuple[int, bool, str]:
        ranges = {
            "playing": (PERFORMANCE_QUERY_DAYS * 86400, True, f"最近 {PERFORMANCE_QUERY_DAYS} 天游玩时"),
            "24h": (86400, False, "最近 24 小时"),
            "7d": (7 * 86400, False, "最近 7 天"),
            "30d": (30 * 86400, False, "最近 30 天"),
            "90d": (PERFORMANCE_QUERY_DAYS * 86400, False, f"最近 {PERFORMANCE_QUERY_DAYS} 天"),
        }
        if range_name not in ranges:
            raise manager.ManagerError("性能记录时间范围无效")
        return ranges[range_name]

    def _rows(self, range_name: str, source: str = "server") -> tuple[list[sqlite3.Row], int, bool, str]:
        self.initialize()
        seconds, online_only, label = self._range(range_name)
        cutoff = int(time.time()) - seconds
        if source == "server":
            table = "samples"
            columns = self.QUERY_COLUMNS
            where = "ts >= ?" + (" AND player_count > 0" if online_only else "")
        elif source == "client":
            table = "client_samples"
            columns = self.CLIENT_QUERY_COLUMNS
            where = "ts >= ?" + (" AND game_running = 1" if online_only else "")
        else:
            raise manager.ManagerError("性能记录来源无效")
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE {where} ORDER BY ts", (cutoff,)
            ).fetchall()
        return rows, cutoff, online_only, label

    @classmethod
    def _point(cls, rows: list[sqlite3.Row]) -> dict[str, Any]:
        def average(key: str) -> float | None:
            return cls._average(cls._values(rows, key))

        def maximum(key: str) -> float | None:
            values = cls._values(rows, key)
            return max(values) if values else None

        fps = cls._values(rows, "server_fps")
        return {
            "timestamp": dt.datetime.fromtimestamp(int(rows[-1]["ts"]), dt.timezone.utc).isoformat(timespec="seconds"),
            "player_count": int(maximum("player_count") or 0),
            "server_fps": average("server_fps"),
            "min_server_fps": min(fps) if fps else None,
            "frame_time_ms": maximum("frame_time_ms"),
            "host_cpu_percent": maximum("host_cpu_percent"),
            "game_cpu_one_core_percent": maximum("game_cpu_one_core_percent"),
            "memory_percent": maximum("memory_percent"),
            "ssd_temperature_c": maximum("ssd_temperature_c"),
        }

    @classmethod
    def _downsample(cls, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        if not rows:
            return []
        chunk_size = max(1, (len(rows) + PERFORMANCE_MAX_POINTS - 1) // PERFORMANCE_MAX_POINTS)
        return [cls._point(rows[index:index + chunk_size]) for index in range(0, len(rows), chunk_size)]

    @classmethod
    def _client_point(cls, rows: list[sqlite3.Row]) -> dict[str, Any]:
        def average(key: str) -> float | None:
            return cls._average(cls._values(rows, key))

        def maximum(key: str) -> float | None:
            values = cls._values(rows, key)
            return max(values) if values else None

        return {
            "timestamp": dt.datetime.fromtimestamp(int(rows[-1]["ts"]), dt.timezone.utc).isoformat(timespec="seconds"),
            "client_cpu_percent": maximum("cpu_percent"),
            "cpu_peak_core_percent": maximum("cpu_peak_core_percent"),
            "gpu_util_percent": average("gpu_util_percent"),
            "memory_percent": maximum("memory_percent"),
            "game_cpu_host_percent": maximum("game_cpu_host_percent"),
            "gpu_temperature_c": maximum("gpu_temperature_c"),
        }

    @classmethod
    def _client_downsample(cls, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        if not rows:
            return []
        chunk_size = max(1, (len(rows) + PERFORMANCE_MAX_POINTS - 1) // PERFORMANCE_MAX_POINTS)
        return [cls._client_point(rows[index:index + chunk_size]) for index in range(0, len(rows), chunk_size)]

    @classmethod
    def _summary(cls, rows: list[sqlite3.Row]) -> dict[str, Any]:
        def values(key: str) -> list[float]:
            return cls._values(rows, key)

        fps = values("server_fps")
        frame_time = values("frame_time_ms")
        host_cpu = values("host_cpu_percent")
        game_cpu = values("game_cpu_one_core_percent")
        memory = values("memory_percent")
        game_memory = values("game_memory_bytes")
        temperature = values("temperature_c")
        ssd_temperature = values("ssd_temperature_c")
        players = values("player_count")
        return {
            "sample_count": len(rows),
            "online_sample_count": sum(1 for row in rows if (row["player_count"] or 0) > 0),
            "sampled_seconds": len(rows) * PERFORMANCE_SAMPLE_SECONDS,
            "first_sample_at": dt.datetime.fromtimestamp(int(rows[0]["ts"]), dt.timezone.utc).isoformat(timespec="seconds") if rows else None,
            "last_sample_at": dt.datetime.fromtimestamp(int(rows[-1]["ts"]), dt.timezone.utc).isoformat(timespec="seconds") if rows else None,
            "max_players_observed": int(max(players)) if players else 0,
            "average_fps": cls._average(fps),
            "minimum_fps": min(fps) if fps else None,
            "p10_fps": cls._percentile(fps, 0.10),
            "average_frame_time_ms": cls._average(frame_time),
            "p95_frame_time_ms": cls._percentile(frame_time, 0.95),
            "average_host_cpu_percent": cls._average(host_cpu),
            "p95_host_cpu_percent": cls._percentile(host_cpu, 0.95),
            "average_game_cpu_one_core_percent": cls._average(game_cpu),
            "p95_game_cpu_one_core_percent": cls._percentile(game_cpu, 0.95),
            "average_memory_percent": cls._average(memory),
            "p95_memory_percent": cls._percentile(memory, 0.95),
            "peak_game_memory_bytes": int(max(game_memory)) if game_memory else None,
            "peak_temperature_c": max(temperature) if temperature else None,
            "average_ssd_temperature_c": cls._average(ssd_temperature),
            "peak_ssd_temperature_c": max(ssd_temperature) if ssd_temperature else None,
        }

    @classmethod
    def _client_summary(cls, rows: list[sqlite3.Row]) -> dict[str, Any]:
        def values(key: str) -> list[float]:
            return cls._values(rows, key)

        cpu = values("cpu_percent")
        peak_core = values("cpu_peak_core_percent")
        memory = values("memory_percent")
        gpu = values("gpu_util_percent")
        gpu_memory = values("gpu_memory_util_percent")
        gpu_temp = values("gpu_temperature_c")
        gpu_power = values("gpu_power_w")
        upload_latency = values("upload_latency_ms")
        game_rows = [row for row in rows if row["game_running"]]
        shipping_rows = [row for row in game_rows if row["game_process_role"] == "shipping"]
        launcher_rows = [row for row in game_rows if row["game_process_role"] == "launcher"]
        game_cpu = [float(row["game_cpu_host_percent"]) for row in shipping_rows if row["game_cpu_host_percent"] is not None]
        game_memory = [
            float(row["game_memory_private_bytes"] if row["game_memory_private_bytes"] is not None else row["game_memory_rss_bytes"])
            for row in shipping_rows
            if row["game_memory_private_bytes"] is not None or row["game_memory_rss_bytes"] is not None
        ]
        return {
            "sample_count": len(rows),
            "game_sample_count": len(game_rows),
            "sampled_seconds": int(sum(int(row["sample_interval_seconds"] or 0) for row in rows)),
            "play_seconds": int(sum(int(row["sample_interval_seconds"] or 0) for row in game_rows)),
            "first_sample_at": dt.datetime.fromtimestamp(int(rows[0]["ts"]), dt.timezone.utc).isoformat(timespec="seconds") if rows else None,
            "last_sample_at": dt.datetime.fromtimestamp(int(rows[-1]["ts"]), dt.timezone.utc).isoformat(timespec="seconds") if rows else None,
            "average_cpu_percent": cls._average(cpu),
            "p95_cpu_percent": cls._percentile(cpu, 0.95),
            "p95_peak_core_percent": cls._percentile(peak_core, 0.95),
            "average_memory_percent": cls._average(memory),
            "p95_memory_percent": cls._percentile(memory, 0.95),
            "average_gpu_percent": cls._average(gpu),
            "p95_gpu_percent": cls._percentile(gpu, 0.95),
            "p95_gpu_memory_percent": cls._percentile(gpu_memory, 0.95),
            "p95_game_cpu_host_percent": cls._percentile(game_cpu, 0.95),
            "peak_game_memory_bytes": int(max(game_memory)) if game_memory else None,
            "peak_gpu_temperature_c": max(gpu_temp) if gpu_temp else None,
            "peak_gpu_power_w": max(gpu_power) if gpu_power else None,
            "p95_upload_latency_ms": cls._percentile(upload_latency, 0.95),
            "upload_failures": int(max(values("upload_failures"), default=0)),
            "maximum_queue_depth": int(max(values("queue_depth"), default=0)),
            "shipping_game_sample_count": len(shipping_rows),
            "launcher_game_sample_count": len(launcher_rows),
        }

    @classmethod
    def _capacity(cls, rows: list[sqlite3.Row]) -> dict[str, Any]:
        groups: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            count = int(row["player_count"] or 0)
            if count > 0:
                groups.setdefault(count, []).append(row)
        buckets: list[dict[str, Any]] = []
        for player_count, group in sorted(groups.items()):
            summary = cls._summary(group)
            buckets.append({
                "player_count": player_count,
                "sample_count": len(group),
                "sampled_seconds": len(group) * PERFORMANCE_SAMPLE_SECONDS,
                "average_fps": summary["average_fps"],
                "minimum_fps": summary["minimum_fps"],
                "p10_fps": summary["p10_fps"],
                "p95_frame_time_ms": summary["p95_frame_time_ms"],
                "p95_host_cpu_percent": summary["p95_host_cpu_percent"],
                "p95_game_cpu_one_core_percent": summary["p95_game_cpu_one_core_percent"],
                "p95_memory_percent": summary["p95_memory_percent"],
                "peak_game_memory_bytes": summary["peak_game_memory_bytes"],
            })
        qualified = [item for item in buckets if item["sample_count"] >= PERFORMANCE_CAPACITY_MIN_SAMPLES]
        stable = [
            item for item in qualified
            if (item["p10_fps"] is not None and item["p10_fps"] >= 55)
            and (item["p95_memory_percent"] is None or item["p95_memory_percent"] < 85)
        ]
        observed_stable = max((item["player_count"] for item in stable), default=None)
        if not qualified:
            message = "尚未形成有效人数档；每种在线人数至少连续记录 10 分钟。"
            readiness = "collecting"
        elif len(qualified) == 1:
            message = f"已形成 {qualified[0]['player_count']} 人样本；再采集其他人数档才能判断扩容趋势。"
            readiness = "collecting"
        else:
            message = f"已有 {len(qualified)} 个人数档可比较；最高实测稳定人数为 {observed_stable or 0} 人。"
            readiness = "ready"
        return {
            "readiness": readiness,
            "message": message,
            "minimum_samples_per_player_count": PERFORMANCE_CAPACITY_MIN_SAMPLES,
            "observed_stable_players": observed_stable,
            "buckets": buckets,
        }

    def status(self) -> dict[str, Any]:
        try:
            self.initialize()
            with self._connection() as connection:
                row = connection.execute("SELECT COUNT(*) AS count, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM samples").fetchone()
                used_size = self.database_used_size(connection)
            size = self.database_size()
            last_ts = int(row["last_ts"]) if row and row["last_ts"] is not None else None
            with self.state_lock:
                error = self.last_error
            return {
                "running": bool(self.thread and self.thread.is_alive()),
                "error": error,
                "sample_interval_seconds": PERFORMANCE_SAMPLE_SECONDS,
                "retention_days": PERFORMANCE_RETENTION_DAYS,
                "maximum_rows": PERFORMANCE_MAX_ROWS,
                "row_count": int(row["count"] or 0) if row else 0,
                "database_size_bytes": size,
                "database_used_bytes": used_size,
                "database_hard_limit_bytes": PERFORMANCE_DB_HARD_LIMIT_BYTES,
                "first_sample_at": dt.datetime.fromtimestamp(int(row["first_ts"]), dt.timezone.utc).isoformat(timespec="seconds") if row and row["first_ts"] is not None else None,
                "last_sample_at": dt.datetime.fromtimestamp(last_ts, dt.timezone.utc).isoformat(timespec="seconds") if last_ts is not None else None,
                "last_sample_age_seconds": max(0, int(time.time()) - last_ts) if last_ts is not None else None,
            }
        except Exception as exc:
            return {
                "running": bool(self.thread and self.thread.is_alive()),
                "error": f"{type(exc).__name__}: {redact(str(exc))}",
                "sample_interval_seconds": PERFORMANCE_SAMPLE_SECONDS,
                "retention_days": PERFORMANCE_RETENTION_DAYS,
                "maximum_rows": PERFORMANCE_MAX_ROWS,
                "row_count": 0,
                "database_size_bytes": 0,
                "database_used_bytes": 0,
                "database_hard_limit_bytes": PERFORMANCE_DB_HARD_LIMIT_BYTES,
                "first_sample_at": None,
                "last_sample_at": None,
                "last_sample_age_seconds": None,
            }

    def client_status(self) -> dict[str, Any]:
        try:
            self.initialize()
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS count, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM client_samples"
                ).fetchone()
                latest = connection.execute(
                    """
                    SELECT ts, client_ts, host_label, collector_version, game_running, session_id,
                           gpu_name, cpu_percent, memory_percent, gpu_util_percent, gpu_temperature_c,
                           queue_depth, upload_failures, game_executable_name, game_process_role
                    FROM client_samples ORDER BY ts DESC, id DESC LIMIT 1
                    """
                ).fetchone()
                latest_game = connection.execute(
                    """
                    SELECT ts, collector_version, game_executable_name, game_process_role
                    FROM client_samples WHERE game_running = 1 ORDER BY ts DESC, id DESC LIMIT 1
                    """
                ).fetchone()
                used_size = self.database_used_size(connection)
            last_ts = int(row["last_ts"]) if row and row["last_ts"] is not None else None
            age = max(0, int(time.time()) - last_ts) if last_ts is not None else None
            fresh = isinstance(age, int) and age <= CLIENT_IDLE_SAMPLE_SECONDS * 3
            latest_version = str(latest["collector_version"] or "") if latest else ""
            version_numbers = tuple(int(value) for value in re.findall(r"\d+", latest_version)[:3])
            while len(version_numbers) < 3:
                version_numbers += (0,)
            if version_numbers < (1, 0, 1):
                target_status = "upgrade_required"
            elif not latest_game or str(latest_game["collector_version"] or "") != latest_version:
                target_status = "awaiting_game"
            elif latest_game["game_process_role"] == "shipping":
                target_status = "verified"
            elif latest_game["game_process_role"] == "launcher":
                target_status = "invalid"
            else:
                target_status = "awaiting_game"
            return {
                "running": fresh,
                "error": None,
                "sample_interval_seconds": CLIENT_GAME_SAMPLE_SECONDS,
                "idle_sample_interval_seconds": CLIENT_IDLE_SAMPLE_SECONDS,
                "retention_days": PERFORMANCE_RETENTION_DAYS,
                "maximum_rows": PERFORMANCE_CLIENT_MAX_ROWS,
                "row_count": int(row["count"] or 0) if row else 0,
                "database_size_bytes": self.database_size(),
                "database_used_bytes": used_size,
                "database_hard_limit_bytes": PERFORMANCE_DB_HARD_LIMIT_BYTES,
                "first_sample_at": dt.datetime.fromtimestamp(int(row["first_ts"]), dt.timezone.utc).isoformat(timespec="seconds") if row and row["first_ts"] is not None else None,
                "last_sample_at": dt.datetime.fromtimestamp(last_ts, dt.timezone.utc).isoformat(timespec="seconds") if last_ts is not None else None,
                "last_sample_age_seconds": age,
                "game_running": bool(latest["game_running"]) if latest else False,
                "host_label": latest["host_label"] if latest else None,
                "collector_version": latest["collector_version"] if latest else None,
                "gpu_name": latest["gpu_name"] if latest else None,
                "game_target_status": target_status,
                "last_game_executable_name": latest_game["game_executable_name"] if latest_game else None,
                "last_game_process_role": latest_game["game_process_role"] if latest_game else None,
                "last_game_sample_at": self._event_timestamp(int(latest_game["ts"])) if latest_game else None,
                "latest": dict(latest) if latest else None,
            }
        except Exception as exc:
            return {
                "running": False, "error": f"{type(exc).__name__}: {redact(str(exc))}",
                "sample_interval_seconds": CLIENT_GAME_SAMPLE_SECONDS,
                "idle_sample_interval_seconds": CLIENT_IDLE_SAMPLE_SECONDS,
                "retention_days": PERFORMANCE_RETENTION_DAYS, "maximum_rows": PERFORMANCE_CLIENT_MAX_ROWS,
                "row_count": 0, "database_size_bytes": self.database_size(),
                "database_used_bytes": 0,
                "database_hard_limit_bytes": PERFORMANCE_DB_HARD_LIMIT_BYTES,
                "first_sample_at": None, "last_sample_at": None, "last_sample_age_seconds": None,
                "game_running": False, "game_target_status": "unknown", "latest": None,
            }

    def _client_sessions(self, cutoff: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, MIN(ts) AS started, MAX(ts) AS ended, COUNT(*) AS sample_count,
                       SUM(sample_interval_seconds) AS sampled_seconds,
                       AVG(cpu_percent) AS average_cpu_percent,
                       AVG(gpu_util_percent) AS average_gpu_percent,
                       MAX(gpu_temperature_c) AS peak_gpu_temperature_c,
                       MAX(COALESCE(game_memory_private_bytes, game_memory_rss_bytes)) AS peak_game_memory_bytes,
                       AVG(upload_latency_ms) AS average_upload_latency_ms
                FROM client_samples
                WHERE ts >= ? AND game_running = 1 AND game_process_role = 'shipping' AND session_id IS NOT NULL
                GROUP BY session_id
                ORDER BY ended DESC
                LIMIT 20
                """,
                (cutoff,),
            ).fetchall()
        return [{
            "session_id": row["session_id"],
            "started_at": dt.datetime.fromtimestamp(int(row["started"]), dt.timezone.utc).isoformat(timespec="seconds"),
            "ended_at": dt.datetime.fromtimestamp(int(row["ended"]), dt.timezone.utc).isoformat(timespec="seconds"),
            "sample_count": int(row["sample_count"] or 0),
            "sampled_seconds": int(row["sampled_seconds"] or 0),
            "average_cpu_percent": row["average_cpu_percent"],
            "average_gpu_percent": row["average_gpu_percent"],
            "peak_gpu_temperature_c": row["peak_gpu_temperature_c"],
            "peak_game_memory_bytes": row["peak_game_memory_bytes"],
            "average_upload_latency_ms": row["average_upload_latency_ms"],
        } for row in rows]

    @staticmethod
    def _event_timestamp(value: int) -> str:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds")

    def _timeline_events(
        self,
        cutoff: int,
        online_only: bool,
        visible_rows: list[sqlite3.Row],
    ) -> dict[str, Any]:
        if not visible_rows:
            return {"start_at": None, "end_at": None, "events": [], "counts": {}, "truncated": False}
        start_ts = int(visible_rows[0]["ts"])
        end_ts = int(visible_rows[-1]["ts"])
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT player_count FROM samples WHERE ts < ? ORDER BY ts DESC, id DESC LIMIT 1",
                (start_ts,),
            ).fetchone()
            samples = connection.execute(
                """
                SELECT ts, service_active, player_count, server_fps
                FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts, id
                """,
                (max(cutoff, start_ts), end_ts),
            ).fetchall()
            stored = connection.execute(
                """
                SELECT ts, end_ts, kind, source, title, detail
                FROM events WHERE ts BETWEEN ? AND ? ORDER BY ts, id
                """,
                (start_ts, end_ts),
            ).fetchall()

        events: list[dict[str, Any]] = []
        previous_players = int(previous["player_count"] or 0) if previous else None
        for row in samples:
            players = int(row["player_count"] or 0)
            if previous_players is not None and players != previous_players:
                if not online_only or players > 0 or previous_players > 0:
                    direction = "玩家加入" if players > previous_players else "玩家离开"
                    events.append({
                        "timestamp": self._event_timestamp(int(row["ts"])),
                        "end_timestamp": None,
                        "kind": "players",
                        "title": f"在线人数 {previous_players} → {players}",
                        "detail": direction,
                        "severity": "info",
                        "source": "derived",
                    })
            previous_players = players

        drop: dict[str, Any] | None = None

        def finish_drop() -> None:
            nonlocal drop
            if not drop:
                return
            duration = max(PERFORMANCE_SAMPLE_SECONDS, int(drop["end_ts"]) - int(drop["start_ts"]) + PERFORMANCE_SAMPLE_SECONDS)
            events.append({
                "timestamp": self._event_timestamp(int(drop["start_ts"])),
                "end_timestamp": self._event_timestamp(int(drop["end_ts"])),
                "kind": "fps_drop",
                "title": f"掉帧至 {float(drop['minimum']):.1f} FPS",
                "detail": f"持续约 {duration} 秒 · 在线 {int(drop['players'])} 人 · {int(drop['samples'])} 个样本",
                "severity": "critical" if float(drop["minimum"]) < 30 else "warning",
                "source": "derived",
            })
            drop = None

        for row in samples:
            players = int(row["player_count"] or 0)
            fps = self._number(row["server_fps"])
            ts = int(row["ts"])
            qualifies = bool(row["service_active"]) and fps is not None and fps < 55 and (not online_only or players > 0)
            contiguous = bool(drop and ts - int(drop["end_ts"]) <= PERFORMANCE_SAMPLE_SECONDS * 3)
            if qualifies:
                if not drop or not contiguous:
                    finish_drop()
                    drop = {"start_ts": ts, "end_ts": ts, "minimum": fps, "players": players, "samples": 1}
                else:
                    drop["end_ts"] = ts
                    drop["minimum"] = min(float(drop["minimum"]), fps)
                    drop["players"] = max(int(drop["players"]), players)
                    drop["samples"] = int(drop["samples"]) + 1
            else:
                finish_drop()
        finish_drop()

        for row in stored:
            kind = str(row["kind"] or "system")
            events.append({
                "timestamp": self._event_timestamp(int(row["ts"])),
                "end_timestamp": self._event_timestamp(int(row["end_ts"])) if row["end_ts"] is not None else None,
                "kind": kind,
                "title": str(row["title"] or kind),
                "detail": str(row["detail"] or ""),
                "severity": "critical" if kind.endswith("failed") else "info",
                "source": str(row["source"] or "manager"),
            })
        events.sort(key=lambda item: (item["timestamp"], item["kind"], item["title"]))
        truncated = len(events) > 800
        if truncated:
            events = events[-800:]
        counts: dict[str, int] = {}
        for event in events:
            kind = str(event["kind"])
            category = "update" if kind.startswith("update") else ("restart" if kind.startswith("restart") else kind)
            counts[category] = counts.get(category, 0) + 1
        return {
            "start_at": self._event_timestamp(start_ts),
            "end_at": self._event_timestamp(end_ts),
            "events": events,
            "counts": counts,
            "truncated": truncated,
        }

    def query(self, range_name: str, source: str = "server") -> dict[str, Any]:
        rows, cutoff, online_only, label = self._rows(range_name, source)
        if source == "client":
            return {
                "source": "client", "range": range_name, "range_label": label,
                "online_only": online_only, "recorder": self.client_status(),
                "summary": self._client_summary(rows), "sessions": self._client_sessions(cutoff),
                "points": self._client_downsample(rows),
            }
        return {
            "source": "server",
            "range": range_name,
            "range_label": label,
            "online_only": online_only,
            "recorder": self.status(),
            "summary": self._summary(rows),
            "capacity": self._capacity(rows),
            "timeline": self._timeline_events(cutoff, online_only, rows),
            "points": self._downsample(rows),
        }

    def diagnostic_summary(self) -> dict[str, Any]:
        rows, _cutoff, online_only, label = self._rows("30d", "server")
        client_rows, _client_cutoff, client_online_only, client_label = self._rows("30d", "client")
        return {
            "server": {
                "range": "30d", "range_label": label, "online_only": online_only,
                "recorder": self.status(), "summary": self._summary(rows), "capacity": self._capacity(rows),
            },
            "client": {
                "range": "30d", "range_label": client_label, "online_only": client_online_only,
                "recorder": self.client_status(), "summary": self._client_summary(client_rows),
            },
        }

    def csv_export(self, range_name: str, source: str = "server") -> tuple[str, bytes]:
        self.initialize()
        seconds, online_only, _label = self._range(range_name)
        cutoff = int(time.time()) - seconds
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        with self._connection() as connection:
            if source == "server":
                where = "ts >= ?" + (" AND player_count > 0" if online_only else "")
                writer.writerow(("timestamp_utc",) + self.COLUMNS[1:])
                rows = connection.execute(
                    f"SELECT {', '.join(self.COLUMNS)} FROM samples WHERE {where} ORDER BY ts", (cutoff,)
                )
                for row in rows:
                    timestamp = dt.datetime.fromtimestamp(int(row["ts"]), dt.timezone.utc).isoformat(timespec="seconds")
                    writer.writerow((timestamp,) + tuple(row[column] for column in self.COLUMNS[1:]))
            elif source == "client":
                where = "ts >= ?" + (" AND game_running = 1" if online_only else "")
                writer.writerow(("received_timestamp_utc", "client_timestamp_utc") + self.CLIENT_COLUMNS[3:])
                rows = connection.execute(
                    f"SELECT {', '.join(self.CLIENT_COLUMNS)} FROM client_samples WHERE {where} ORDER BY ts", (cutoff,)
                )
                for row in rows:
                    received = dt.datetime.fromtimestamp(int(row["received_ts"]), dt.timezone.utc).isoformat(timespec="seconds")
                    client = dt.datetime.fromtimestamp(int(row["client_ts"]), dt.timezone.utc).isoformat(timespec="seconds")
                    writer.writerow((received, client) + tuple(row[column] for column in self.CLIENT_COLUMNS[3:]))
            else:
                raise manager.ManagerError("性能记录来源无效")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"palworld-performance-{source}-{range_name}-{stamp}.csv", output.getvalue().encode("utf-8-sig")


class Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float, loader: Callable[[], Any], fallback: Any = None) -> Any:
        with self.lock:
            saved = self.values.get(key)
            if saved and time.monotonic() - saved[0] < ttl:
                return saved[1]
        try:
            value = loader()
        except Exception:
            if saved:
                return saved[1]
            return fallback
        with self.lock:
            self.values[key] = (time.monotonic(), value)
        return value


def timer_payload() -> list[dict[str, Any]]:
    units = list(TIMER_LABELS)
    result = manager.run([
        "systemctl", "show", *units, "--no-pager",
        "--property=Id,ActiveState,UnitFileState,NextElapseUSecRealtime",
    ], check=False)
    reports: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for line in (result.stdout or "").splitlines() + [""]:
        if line and "=" in line:
            key, value = line.split("=", 1)
            current[key] = value
        elif current:
            unit = current.get("Id", "")
            if unit in TIMER_LABELS:
                reports.append({
                    "unit": unit,
                    "label": TIMER_LABELS[unit],
                    "active": current.get("ActiveState") == "active",
                    "enabled": current.get("UnitFileState") == "enabled",
                    "next": current.get("NextElapseUSecRealtime") or None,
                })
            current = {}
    return reports


def server_info() -> dict[str, Any] | None:
    if not manager.service_active():
        return None
    value = manager.api_request("info")
    return value if isinstance(value, dict) else None


SAMPLER = HostSampler()
PERFORMANCE = PerformanceHistory(PERFORMANCE_DB_PATH, SAMPLER)
CACHE = Cache()


class Sessions:
    def __init__(self, state_path: Path = SESSION_STORE_PATH) -> None:
        self.lock = threading.Lock()
        self.state_path = state_path
        self.tokens: dict[str, tuple[float, bool]] = {}
        self.failures: dict[str, list[float]] = {}
        self._load()

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        values = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            return
        now = time.time()
        for key, expiry in values.items():
            if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
                continue
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or expiry <= now:
                continue
            self.tokens[key] = (float(expiry), True)

    def _prune_locked(self) -> bool:
        now = time.time()
        removed_persistent = False
        for key, (expiry, remembered) in list(self.tokens.items()):
            if expiry <= now:
                self.tokens.pop(key, None)
                removed_persistent = removed_persistent or remembered
        if len(self.tokens) > MAX_ACTIVE_SESSIONS:
            overflow = len(self.tokens) - MAX_ACTIVE_SESSIONS
            for key, (_, remembered) in sorted(self.tokens.items(), key=lambda item: item[1][0])[:overflow]:
                self.tokens.pop(key, None)
                removed_persistent = removed_persistent or remembered
        return removed_persistent

    def _persist_locked(self) -> None:
        remembered = {
            key: expiry
            for key, (expiry, persistent) in self.tokens.items()
            if persistent and expiry > time.time()
        }
        try:
            if not remembered:
                with contextlib.suppress(FileNotFoundError):
                    self.state_path.unlink()
                return
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"version": 1, "sessions": remembered}, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        except OSError as exc:
            with contextlib.suppress(OSError, UnboundLocalError):
                temporary.unlink()
            raise manager.ManagerError("无法保存 30 天自动登录状态") from exc

    def login_allowed(self, address: str) -> bool:
        now = time.time()
        with self.lock:
            values = [value for value in self.failures.get(address, []) if now - value < 300]
            self.failures[address] = values
            return len(values) < 8

    def failed(self, address: str) -> None:
        with self.lock:
            self.failures.setdefault(address, []).append(time.time())

    def create(self, address: str, remember: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        key = self._token_key(token)
        ttl = REMEMBER_SESSION_TTL_SECONDS if remember else SESSION_TTL_SECONDS
        with self.lock:
            changed = self._prune_locked()
            self.tokens[key] = (time.time() + ttl, remember)
            changed = self._prune_locked() or changed
            if remember or changed:
                try:
                    self._persist_locked()
                except manager.ManagerError:
                    self.tokens.pop(key, None)
                    raise
            self.failures.pop(address, None)
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        key = self._token_key(token)
        with self.lock:
            record = self.tokens.get(key)
            if record is None or record[0] < now:
                self.tokens.pop(key, None)
                return False
            return True

    def remove(self, token: str | None) -> None:
        if token:
            with self.lock:
                record = self.tokens.pop(self._token_key(token), None)
                if record and record[1]:
                    self._persist_locked()


class Operations:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: dict[str, Any] | None = None

    def public(self) -> dict[str, Any] | None:
        with self.lock:
            return dict(self.current) if self.current else None

    def start(
        self,
        name: str,
        label: str,
        callback: Callable[..., Any],
        *,
        with_progress: bool = False,
        metadata: dict[str, Any] | None = None,
        success_message: Callable[[Any], str] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if self.current and self.current.get("state") == "running":
                raise manager.ManagerError(f"{self.current.get('label', '另一项操作')}正在执行，请稍候")
            operation = {
                "id": secrets.token_hex(8), "name": name, "label": label, "state": "running",
                "started_at": utc_now(), "updated_at": utc_now(), "finished_at": None,
                "phase": "queued", "progress": 4, "message": "操作已提交，正在准备…", "result": None,
            }
            if metadata:
                operation.update(metadata)
            self.current = operation

        def report(phase: str, message: str, percent: int) -> None:
            with self.lock:
                if self.current and self.current.get("id") == operation["id"] and self.current.get("state") == "running":
                    self.current.update({
                        "phase": str(phase)[:40],
                        "progress": max(0, min(100, int(percent))),
                        "message": redact(str(message)),
                        "updated_at": utc_now(),
                    })

        def worker() -> None:
            try:
                result = callback(report) if with_progress else callback()
                state = "success"
                message = success_message(result) if success_message else f"{label}已完成"
                error_type = None
            except Exception as exc:  # Keep the HTTP process alive and expose only the safe message.
                state = "error"
                message = str(exc) if isinstance(exc, manager.ManagerError) else f"{type(exc).__name__}：操作未完成"
                result = None
                error_type = type(exc).__name__
            with self.lock:
                if self.current and self.current.get("id") == operation["id"]:
                    failed_phase = self.current.get("phase") if state == "error" else None
                    self.current.update({
                        "state": state, "finished_at": utc_now(), "message": redact(message),
                        "updated_at": utc_now(), "phase": "complete" if state == "success" else "error",
                        "progress": 100 if state == "success" else self.current.get("progress", 0),
                        "failed_phase": failed_phase, "result": result, "error_type": error_type,
                    })

        threading.Thread(target=worker, name=f"panel-{name}", daemon=True).start()
        return dict(operation)


SESSIONS = Sessions()
OPERATIONS = Operations()


def breed_status_payload() -> dict[str, Any]:
    try:
        value = json.loads(BREED_STATUS_PATH.read_text(encoding="utf-8"))
        status = value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        status = {}
    try:
        source_stat = BREED_SAVE_PATH.stat()
        source_signature = f"{source_stat.st_mtime_ns}:{source_stat.st_size}"
        source_modified_at = dt.datetime.fromtimestamp(
            source_stat.st_mtime, dt.timezone.utc
        ).isoformat(timespec="seconds")
        source_size = source_stat.st_size
        source_error = ""
    except OSError:
        source_signature = ""
        source_modified_at = None
        source_size = None
        source_error = "服务器最新 Level.sav 不可读"
    service = manager.run(
        ["systemctl", "is-active", BREED_REFRESH_SERVICE],
        check=False,
        timeout=10,
    ).stdout.strip()
    busy = service in {"active", "activating", "reloading"}
    published_signature = str(status.get("publishedSignature") or "")
    last_error = str(status.get("lastError") or source_error)
    if status.get("busy") and not busy and not last_error:
        last_error = "上次存档读取未正常完成，请重试"
    return {
        "available": (BREED_ROOT / "index.html").is_file(),
        "busy": busy,
        "fresh": bool(source_signature and published_signature == source_signature),
        "serviceState": service or "unknown",
        "sourceSignature": source_signature,
        "sourceModifiedAt": source_modified_at,
        "sourceSize": source_size,
        "publishedSignature": published_signature,
        "publishedSaveModifiedAt": status.get("publishedSaveModifiedAt"),
        "analyzedAt": status.get("analyzedAt"),
        "durationSeconds": status.get("durationSeconds"),
        "speciesCount": int(status.get("speciesCount") or 0),
        "palCount": int(status.get("palCount") or 0),
        "crossWorldGeneCount": int(status.get("crossWorldGeneCount") or 0),
        "globalStorageStatus": str(status.get("globalStorageStatus") or "missing"),
        "assistantVersion": str(status.get("assistantVersion") or ""),
        "lastError": redact(last_error, 1000),
    }


def status_payload() -> dict[str, Any]:
    # Freeze the operation state before collecting slower REST/system metrics.
    # Otherwise a finishing worker can be paired with a pre-finish health sample.
    operation = OPERATIONS.public()
    snapshot = manager.health_snapshot()
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    host = SAMPLER.sample(snapshot.get("game_pid"))
    backups = backup_payload()
    info = CACHE.get("server-info", 60, server_info, None)
    players: Any = []
    if snapshot["service_active"]:
        try:
            response = manager.api_request("players")
            players = response.get("players", []) if isinstance(response, dict) else (response or [])
        except manager.ManagerError:
            players = []
    update_state = manager.load_json(manager.STATE / "update.json", None)
    return {
        "panel_version": PANEL_VERSION,
        "checked_at": utc_now(),
        "healthy": bool(snapshot["service_active"] and snapshot["udp_8211_listening"] and snapshot["metrics"] is not None),
        "service": {
            "active": snapshot["service_active"],
            "manual_stop": snapshot.get("manual_stop", False),
            "udp_ready": snapshot["udp_8211_listening"],
            "api_ready": snapshot["metrics"] is not None,
            "api_error": snapshot.get("api_error"),
            "pid": snapshot.get("game_pid"),
            "uptime_seconds": snapshot.get("process_age_seconds"),
            "memory_bytes": snapshot.get("memory_bytes"),
            "installed_build": manager.installed_build(),
            "game_version": (info or {}).get("version"),
            "server_name": (info or {}).get("servername") or (info or {}).get("serverName"),
            "frame_rate_limit": 0,
        },
        "metrics": metrics,
        "players": players,
        "host": host,
        "game_cpu_host_percent": host.pop("game_cpu_host_percent", None),
        "game_cpu_one_core_percent": host.pop("game_cpu_one_core_percent", None),
        "backups": {"count": backups["count"], "total_bytes": backups["total_bytes"], "latest": backups["items"][0] if backups["items"] else None},
        "timers": CACHE.get("timers", 60, timer_payload, []),
        "last_update": update_state,
        "operation": operation,
    }


def check_item(identifier: str, label: str, status: str, value: str, detail: str = "") -> dict[str, str]:
    return {"id": identifier, "label": label, "status": status, "value": value, "detail": detail}


def checks_payload() -> dict[str, Any]:
    """Run a read-only, low-cost audit of the game, host, automation and data safety."""
    groups: list[dict[str, Any]] = []
    snapshot = manager.health_snapshot()
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    host = SAMPLER.sample(snapshot.get("game_pid"))
    expected_stop = bool(snapshot.get("manual_stop") and not snapshot.get("service_active"))
    active = bool(snapshot.get("service_active"))

    service_checks: list[dict[str, str]] = []
    if active:
        service_checks.append(check_item("game-service", "游戏服务", "pass", "正在运行", f"进程号 {snapshot.get('game_pid') or '未知'}"))
    elif expected_stop:
        service_checks.append(check_item("game-service", "游戏服务", "info", "已按要求停止", "健康检查会尊重手动停止状态"))
    else:
        service_checks.append(check_item("game-service", "游戏服务", "fail", "意外停止", "未检测到手动停止标记"))

    for identifier, label, ready, success_text in (
        ("game-port", "游戏连接端口", snapshot.get("udp_8211_listening"), "UDP 8211 正常监听"),
        ("rest-api", "游戏管理接口", snapshot.get("metrics") is not None, "REST API 正常响应"),
    ):
        if ready:
            service_checks.append(check_item(identifier, label, "pass", "正常", success_text))
        elif expected_stop:
            service_checks.append(check_item(identifier, label, "info", "随游戏停止", "属于预期状态"))
        else:
            service_checks.append(check_item(identifier, label, "fail", "不可用", str(snapshot.get("api_error") or "未收到响应")))

    fps = metrics.get("serverfpsaverage", metrics.get("serverfps"))
    try:
        fps_number = float(fps)
    except (TypeError, ValueError):
        fps_number = None
    if fps_number is not None:
        fps_status = "pass" if fps_number >= 55 else ("warning" if fps_number >= 30 else "fail")
        service_checks.append(check_item("server-fps", "服务器模拟帧率", fps_status, f"{fps_number:.1f} FPS", "目标为稳定接近 60 FPS"))
    elif expected_stop:
        service_checks.append(check_item("server-fps", "服务器模拟帧率", "info", "游戏已停止", "启动后自动读取"))
    else:
        service_checks.append(check_item("server-fps", "服务器模拟帧率", "warning", "暂无数据", "管理接口暂未返回帧率"))

    game_user_settings = manager.SAVED / "Config" / "LinuxServer" / "GameUserSettings.ini"
    try:
        text = game_user_settings.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?im)^\s*FrameRateLimit\s*=\s*([0-9.]+)", text)
        frame_limit = float(match.group(1)) if match else None
        if frame_limit == 0:
            service_checks.append(check_item("frame-limit", "客户端帧率限制", "pass", "不限制", "FrameRateLimit = 0"))
        elif frame_limit is None:
            service_checks.append(check_item("frame-limit", "客户端帧率限制", "warning", "未找到设置", "请检查 GameUserSettings.ini"))
        else:
            service_checks.append(check_item("frame-limit", "客户端帧率限制", "fail", f"限制为 {frame_limit:g} FPS", "应设置为 0"))
    except OSError:
        service_checks.append(check_item("frame-limit", "客户端帧率限制", "warning", "配置不可读", "无法核对 FrameRateLimit"))
    groups.append({"id": "game", "title": "游戏运行", "checks": service_checks})

    host_checks: list[dict[str, str]] = []
    cpu = host.get("cpu_percent")
    if isinstance(cpu, (int, float)):
        host_checks.append(check_item("host-cpu", "主机 CPU", "pass" if cpu < 85 else "warning", f"{cpu:.1f}%", f"{host.get('logical_cpus') or '—'} 个逻辑线程"))
    else:
        host_checks.append(check_item("host-cpu", "主机 CPU", "info", "正在采样", "下一次刷新会显示百分比"))
    memory = host.get("memory_percent")
    memory_status = "pass" if isinstance(memory, (int, float)) and memory < 82 else ("warning" if isinstance(memory, (int, float)) and memory < 94 else "fail")
    host_checks.append(check_item("host-memory", "内存占用", memory_status, f"{memory:.1f}%" if isinstance(memory, (int, float)) else "未知", f"游戏进程 {format_bytes(snapshot.get('memory_bytes'))}"))
    disk = host.get("disk_percent")
    disk_status = "pass" if isinstance(disk, (int, float)) and disk < 85 else ("warning" if isinstance(disk, (int, float)) and disk < 95 else "fail")
    host_checks.append(check_item("host-disk", "系统盘占用", disk_status, f"{disk:.1f}%" if isinstance(disk, (int, float)) else "未知", f"可用 {format_bytes(host.get('disk_free_bytes'))}"))
    temperature = host.get("temperature_c")
    if isinstance(temperature, (int, float)):
        temperature_status = "pass" if temperature < 80 else ("warning" if temperature < 90 else "fail")
        host_checks.append(check_item("host-temperature", "处理器温度", temperature_status, f"{temperature:.0f}°C", "低于 80°C 为正常范围"))
    else:
        host_checks.append(check_item("host-temperature", "处理器温度", "info", "传感器未提供", "不影响其他监控"))
    ssd_temperature = host.get("ssd_temperature_c")
    if isinstance(ssd_temperature, (int, float)):
        if ssd_temperature < 60:
            ssd_status, ssd_value = "pass", f"{ssd_temperature:.0f}°C"
        elif ssd_temperature < 65:
            ssd_status, ssd_value = "info", f"{ssd_temperature:.0f}°C · 偏热提醒"
        elif ssd_temperature < 70:
            ssd_status, ssd_value = "warning", f"{ssd_temperature:.0f}°C · 温度警告"
        else:
            ssd_status, ssd_value = "fail", f"{ssd_temperature:.0f}°C · 超出规格"
        host_checks.append(check_item(
            "ssd-temperature", "SSD 温度", ssd_status, ssd_value,
            "60°C 提醒 · 65°C 警告 · 70°C 临界；每 5 分钟读取 SMART 并写入历史",
        ))
    else:
        host_checks.append(check_item("ssd-temperature", "SSD 温度", "warning", "暂未读到", "SMART 温度采样会自动重试"))
    try:
        smart = manager.smart_health()
        smart_statuses = {str(item.get("status")) for item in smart}
        if "failed" in smart_statuses:
            smart_status, smart_value = "fail", "硬盘健康异常"
        elif smart_statuses and smart_statuses <= {"passed"}:
            smart_status, smart_value = "pass", "SMART 正常"
        elif "smartctl_not_installed" in smart_statuses:
            smart_status, smart_value = "info", "未安装 SMART 工具"
        else:
            smart_status, smart_value = "warning", "SMART 状态未知"
        host_checks.append(check_item("smart", "硬盘健康", smart_status, smart_value, f"检测到 {len(smart)} 个磁盘报告"))
    except Exception:
        host_checks.append(check_item("smart", "硬盘健康", "warning", "检查失败", "无法读取 SMART 状态"))
    groups.append({"id": "host", "title": "Linux 主机", "checks": host_checks})

    backups = backup_payload()
    backup_checks: list[dict[str, str]] = []
    latest = backups["items"][0] if backups["items"] else None
    if latest:
        backup_checks.append(check_item("backup-present", "可恢复备份", "pass", f"共 {backups['count']} 份", f"最新：{latest['name']}"))
        try:
            created = dt.datetime.fromisoformat(str(latest["created_at"]))
            age_hours = max(0.0, (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600)
            backup_checks.append(check_item("backup-age", "最新备份时间", "pass" if age_hours <= 36 else "warning", f"{age_hours:.1f} 小时前", "超过 36 小时会提醒"))
        except (TypeError, ValueError):
            backup_checks.append(check_item("backup-age", "最新备份时间", "warning", "时间无法解析"))
        try:
            latest_path = selected_backup(str(latest["id"]))
            with latest_path.open("rb") as handle:
                gzip_header = handle.read(2)
            readable = latest_path.stat().st_size > 0 and gzip_header == b"\x1f\x8b"
            backup_checks.append(check_item(
                "backup-integrity", "最新备份归档", "pass" if readable else "fail",
                "可读取" if readable else "文件格式异常", "创建时已完成全量校验；此处只做轻量检查，避免影响游戏帧时间",
            ))
        except Exception as exc:
            backup_checks.append(check_item("backup-integrity", "最新备份归档", "fail", "读取失败", redact(str(exc))))
    else:
        backup_checks.append(check_item("backup-present", "可恢复备份", "fail", "没有备份", "请立即创建一份手动备份"))
    cap = getattr(manager, "BACKUP_CAP_BYTES", 15 * 1024**3)
    backup_checks.append(check_item("backup-cap", "备份容量保护", "pass" if backups["total_bytes"] <= cap else "fail", f"{format_bytes(backups['total_bytes'])} / {format_bytes(cap)}", "超过上限会按保留策略轮换"))
    free = host.get("disk_free_bytes")
    minimum_free = getattr(manager, "MIN_FREE_BYTES", 50 * 1024**3)
    free_status = "pass" if isinstance(free, int) and free >= minimum_free else "warning"
    backup_checks.append(check_item("free-space", "磁盘保留空间", free_status, format_bytes(free), f"保护线 {format_bytes(minimum_free)}"))
    groups.append({"id": "backup", "title": "存档与备份", "checks": backup_checks})

    automation_checks: list[dict[str, str]] = []
    timers = timer_payload()
    for timer in timers:
        ready = bool(timer.get("active") and timer.get("enabled"))
        automation_checks.append(check_item(
            f"timer-{timer.get('unit')}", str(timer.get("label") or timer.get("unit")),
            "pass" if ready else "fail", "已启用" if ready else "未完整启用", str(timer.get("next") or "等待系统排期"),
        ))
    if not timers:
        automation_checks.append(check_item("timers", "自动任务", "fail", "未读取到定时器", "备份、更新和健康检查可能不会自动运行"))
    failed_units = manager.run(["systemctl", "--failed", "--no-legend", "--plain"], check=False, timeout=10)
    failed_lines = [line for line in (failed_units.stdout or "").splitlines() if line.strip()]
    automation_checks.append(check_item("failed-units", "系统失败服务", "pass" if not failed_lines else "fail", "0 个" if not failed_lines else f"{len(failed_lines)} 个", "systemd 运行状态"))
    game_enabled = manager.run(["systemctl", "is-enabled", manager.SERVICE], check=False, timeout=10).stdout.strip() == "enabled"
    automation_checks.append(check_item("game-enabled", "游戏服务开机自启", "pass" if game_enabled else "fail", "已启用" if game_enabled else "未启用", manager.SERVICE))
    panel_active = manager.run(["systemctl", "is-active", "palworld-panel.service"], check=False, timeout=10).stdout.strip() == "active"
    panel_enabled = manager.run(["systemctl", "is-enabled", "palworld-panel.service"], check=False, timeout=10).stdout.strip() == "enabled"
    automation_checks.append(check_item("panel-service", "管理面板服务", "pass" if panel_active and panel_enabled else "fail", "运行并开机自启" if panel_active and panel_enabled else "状态异常", "palworld-panel.service"))
    performance = PERFORMANCE.status()
    performance_age = performance.get("last_sample_age_seconds")
    recorder_fresh = isinstance(performance_age, int) and performance_age <= PERFORMANCE_SAMPLE_SECONDS * 3
    if performance.get("error"):
        recorder_status, recorder_value = "fail", "记录异常"
        recorder_detail = str(performance["error"])
    elif performance.get("running") and (recorder_fresh or performance.get("row_count") == 0):
        recorder_status = "pass" if recorder_fresh else "info"
        recorder_value = f"已记录 {performance.get('row_count', 0)} 条"
        recorder_detail = (
            f"每 {PERFORMANCE_SAMPLE_SECONDS} 秒采样 · 保留 {PERFORMANCE_RETENTION_DAYS} 天 / 最多 {PERFORMANCE_MAX_ROWS} 条"
            f" · 当前 {format_bytes(performance.get('database_size_bytes'))}"
            f" / 硬限制 {format_bytes(PERFORMANCE_DB_HARD_LIMIT_BYTES)}"
        )
    else:
        recorder_status, recorder_value = "fail", "没有持续记录"
        recorder_detail = "后台记录线程未运行或最后一次采样已超时"
    automation_checks.append(check_item("performance-history", "后台性能记录", recorder_status, recorder_value, recorder_detail))
    groups.append({"id": "automation", "title": "自动化与服务", "checks": automation_checks})

    config_checks: list[dict[str, str]] = []
    try:
        settings = manager.settings_map()
        config_checks.append(check_item("settings-parse", "世界设置文件", "pass", f"已读取 {len(settings)} 项", "配置结构可正常解析"))
        try:
            secret = manager.SECRET.read_text(encoding="utf-8").strip()
            api_secret_matches = decode_setting(settings.get("AdminPassword", "")) == secret
            config_checks.append(check_item("admin-secret", "管理密码同步", "pass" if api_secret_matches else "fail", "一致" if api_secret_matches else "不一致", "不会显示密码内容"))
        except OSError:
            config_checks.append(check_item("admin-secret", "管理密码同步", "fail", "密码文件不可读", "不会显示密码内容"))
        builtin_backup = decode_setting(settings.get("bIsUseBackupSaveData", "False")) is True
        config_checks.append(check_item("builtin-backup", "游戏内建备份", "pass" if builtin_backup else "warning", "已开启" if builtin_backup else "未开启", "与面板受管备份互为补充"))
    except Exception:
        config_checks.append(check_item("settings-parse", "世界设置文件", "fail", "解析失败", "面板无法可靠修改设置"))
    quota = manager.service_value("CPUQuotaPerSecUSec")
    quota_unlimited = quota in {"", "infinity", "max"}
    config_checks.append(check_item("cpu-quota", "游戏 CPU 上限", "pass" if quota_unlimited else "warning", "不限制" if quota_unlimited else quota, "服务不设置 60 FPS 相关 CPU 硬上限"))
    recent_errors = 0
    for unit in (manager.SERVICE, "palworld-panel.service"):
        result = manager.run(["journalctl", "-q", "-u", unit, "--since", "1 hour ago", "-p", "err", "-n", "50", "--no-pager", "-o", "cat"], check=False, timeout=10)
        recent_errors += len([line for line in (result.stdout or "").splitlines() if line.strip()])
    config_checks.append(check_item("recent-errors", "最近一小时错误日志", "pass" if recent_errors == 0 else "warning", f"{recent_errors} 条", "统计游戏服务和管理面板的 error 级别日志"))
    groups.append({"id": "config", "title": "配置与日志", "checks": config_checks})

    all_checks = [item for group in groups for item in group["checks"]]
    counts = {name: sum(item["status"] == name for item in all_checks) for name in ("pass", "warning", "fail", "info")}
    overall = "error" if counts["fail"] else ("warning" if counts["warning"] else "healthy")
    for group in groups:
        group_failures = sum(item["status"] == "fail" for item in group["checks"])
        group_warnings = sum(item["status"] == "warning" for item in group["checks"])
        group["status"] = "fail" if group_failures else ("warning" if group_warnings else "pass")
        group["summary"] = f"{len(group['checks'])} 项 · {group_failures} 异常 · {group_warnings} 提醒"
    return {"checked_at": utc_now(), "overall": overall, "counts": {**counts, "total": len(all_checks)}, "groups": groups}


def format_bytes(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "未知"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    return f"{amount:.0f} {units[index]}" if index == 0 else f"{amount:.1f} {units[index]}"


def diagnostics_archive() -> tuple[str, bytes]:
    """Build a redacted support bundle entirely in memory."""
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        readme = (
            "幻兽帕鲁服务器诊断包\n"
            f"生成时间：{utc_now()}\n\n"
            "内容包括只读状态、完整检查、性能历史摘要、非敏感世界设置和最近日志。\n"
            "管理密码与服务器密码不会写入此诊断包。\n"
        )
        archive.writestr("说明.txt", readme)
        try:
            archive.writestr("服务器状态.json", redact(json.dumps(status_payload(), ensure_ascii=False, indent=2, default=str), None))
        except Exception as exc:
            archive.writestr("服务器状态-读取失败.txt", redact(str(exc)))
        try:
            archive.writestr("完整检查.json", redact(json.dumps(checks_payload(), ensure_ascii=False, indent=2, default=str), None))
        except Exception as exc:
            archive.writestr("完整检查-读取失败.txt", redact(str(exc)))
        try:
            performance = PERFORMANCE.diagnostic_summary()
            archive.writestr("性能历史摘要.json", json.dumps(performance, ensure_ascii=False, indent=2, default=str))
        except Exception as exc:
            archive.writestr("性能历史摘要-读取失败.txt", redact(str(exc)))
        try:
            archive.writestr("世界设置-已脱敏.json", redact(json.dumps(settings_payload(), ensure_ascii=False, indent=2, default=str), None))
        except Exception as exc:
            archive.writestr("世界设置-读取失败.txt", redact(str(exc)))
        for filename, unit, lines in (
            ("游戏服务.log", manager.SERVICE, "500"),
            ("管理面板.log", "palworld-panel.service", "300"),
            ("自动更新.log", "palworld-update.service", "200"),
            ("自动备份.log", "palworld-backup-daily.service", "200"),
        ):
            result = manager.run(["journalctl", "-q", "-u", unit, "-n", lines, "--no-pager", "-o", "short-iso"], check=False, timeout=15)
            archive.writestr(filename, redact(f"{result.stdout or ''}\n{result.stderr or ''}"))
        system_status = manager.run([
            "systemctl", "status", manager.SERVICE, "palworld-panel.service", "palworld-health.timer",
            "palworld-update.timer", "palworld-maintenance.timer", "--no-pager", "--full",
        ], check=False, timeout=15)
        archive.writestr("系统服务状态.txt", redact(f"{system_status.stdout or ''}\n{system_status.stderr or ''}"))
        timers = manager.run(["systemctl", "list-timers", "palworld-*", "--all", "--no-pager"], check=False, timeout=15)
        archive.writestr("定时任务.txt", redact(f"{timers.stdout or ''}\n{timers.stderr or ''}"))
    return f"palworld-diagnostics-{timestamp}.zip", buffer.getvalue()


def action_callback(action: str, payload: dict[str, Any]) -> tuple[str, Callable[[], Any]]:
    if action == "save":
        if not manager.service_active():
            raise manager.ManagerError("服务未运行，无法请求世界保存")
        return "保存世界", lambda: manager.api_request("save", method="POST")
    if action == "backup":
        def backup() -> Any:
            with manager.maintenance_lock():
                path = manager.create_backup("manual")
            return {"path": path.name if path else None}
        return "创建并校验备份", backup
    if action == "update-check":
        def update_check() -> Any:
            value = manager.query_update()
            manager.atomic_json(manager.STATE / "update.json", {**value, "result": "checked"})
            print("已是最新版本" if value["up_to_date"] else "检测到可用更新")
            return value
        return "检查 Steam 更新", update_check
    if action == "update-apply":
        def update_apply() -> Any:
            # SteamCMD can be memory/CPU intensive. Run the real update in a
            # transient unit so the always-on panel keeps its strict 50% quota,
            # while the updater receives its own low-priority resource budget.
            unit = f"palworld-panel-update-{int(time.time())}-{secrets.token_hex(2)}"
            manager.run([
                "systemd-run", "--quiet", "--wait", "--collect", f"--unit={unit}",
                "--property=Nice=10", "--property=CPUWeight=20", "--property=IOWeight=20",
                "--property=IOSchedulingClass=best-effort", "--property=IOSchedulingPriority=7",
                "--property=MemoryMax=3G", str(Path("/opt/palworld/bin/palworldctl")),
                "update", "--apply",
            ], capture=False, timeout=1300)
            return manager.load_json(manager.STATE / "update.json", None)
        return "更新服务器", update_apply
    if action in {"start", "stop", "restart"}:
        labels = {"start": "启动服务器", "stop": "安全停止服务器", "restart": "安全重启服务器"}
        return labels[action], lambda: manager.service_control(action, False)
    if action == "verify-backup":
        path = selected_backup(str(payload.get("backup", "")))
        return "完整校验备份", lambda: manager.verify_archive(path)
    if action == "restore-backup":
        backup_id = str(payload.get("backup", ""))
        path = selected_backup(backup_id)
        if payload.get("confirmation") != f"RESTORE:{backup_id}":
            raise manager.ManagerError("恢复确认不匹配")
        return "恢复备份", lambda: manager.restore_backup(str(path), True)
    if action == "maintenance":
        return "清理日志与过期备份", lambda: manager.maintenance(True)
    if action == "announce":
        message = str(payload.get("message", "")).strip()
        if not message or len(message) > 300 or any(char in message for char in "\r\n"):
            raise manager.ManagerError("公告需为 1–300 个字符且不能换行")
        return "发送游戏内公告", lambda: manager.api_request("announce", method="POST", body={"message": message})
    raise manager.ManagerError("未知操作")


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "PalworldPanel/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        print(f"{self.client_address[0]} {redact(message)}")

    def _cookie_token(self) -> str | None:
        cookie = SimpleCookie()
        with contextlib.suppress(Exception):
            cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _authenticated(self) -> bool:
        return SESSIONS.valid(self._cookie_token())

    def _send_headers(
        self,
        status: int,
        content_type: str,
        length: int,
        *,
        content_security_policy: str | None = None,
        cache_control: str = "no-store",
        location: str | None = None,
        download_filename: str | None = None,
        session_token: str | None = None,
        remember_session: bool = False,
        clear_session: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            content_security_policy
            or "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if location in {"/", "/breed/"}:
            self.send_header("Location", location)
        if download_filename is not None:
            self.send_header("Content-Disposition", self._download_header(download_filename))
        if clear_session:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            )
        elif session_token is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session_token):
                raise manager.ManagerError("会话令牌格式无效")
            cookie = f"{SESSION_COOKIE}={session_token}; Path=/; HttpOnly; SameSite=Strict"
            if remember_session:
                cookie += f"; Max-Age={REMEMBER_SESSION_TTL_SECONDS}"
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _json(
        self,
        status: int,
        value: Any,
        *,
        session_token: str | None = None,
        remember_session: bool = False,
        clear_session: bool = False,
    ) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_headers(
            status,
            "application/json; charset=utf-8",
            len(payload),
            session_token=session_token,
            remember_session=remember_session,
            clear_session=clear_session,
        )
        if self.command != "HEAD":
            self.wfile.write(payload)

    @staticmethod
    def _download_header(filename: str) -> str:
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download.bin"
        encoded_name = quote(filename.replace("\r", "").replace("\n", ""), safe="")
        return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"

    def _download_bytes(self, filename: str, payload: bytes, content_type: str) -> None:
        self._send_headers(HTTPStatus.OK, content_type, len(payload), download_filename=filename)
        if self.command != "HEAD":
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

    def _download_file(self, path: Path, content_type: str) -> None:
        try:
            handle = path.open("rb")
            size = path.stat().st_size
        except OSError as exc:
            raise manager.ManagerError("备份文件不可读") from exc
        with handle:
            self._send_headers(HTTPStatus.OK, content_type, size, download_filename=path.name)
            if self.command == "HEAD":
                return
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": redact(message)})

    def _read_json(self, maximum_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise manager.ManagerError("请求长度无效") from exc
        if length <= 0 or length > maximum_bytes:
            raise manager.ManagerError("请求内容为空或过大")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise manager.ManagerError("JSON 请求无效") from exc
        if not isinstance(value, dict):
            raise manager.ManagerError("请求必须是 JSON 对象")
        return value

    def _require_post_auth(self) -> bool:
        if not self._authenticated():
            self._error(HTTPStatus.UNAUTHORIZED, "登录已失效")
            return False
        if self.headers.get("X-Palworld-Panel") != "1":
            self._error(HTTPStatus.FORBIDDEN, "请求来源校验失败")
            return False
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self._error(HTTPStatus.FORBIDDEN, "请求来源不匹配")
            return False
        return True

    def _static(self, path: str) -> None:
        if path == "/breed":
            self._send_headers(
                HTTPStatus.FOUND,
                "text/plain; charset=utf-8",
                0,
                location="/breed/",
            )
            return
        if path.startswith("/breed/"):
            self._breed_static(path)
            return
        routes = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/panel.css": ("panel.css", "text/css; charset=utf-8"),
            "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        if path not in routes:
            self._error(HTTPStatus.NOT_FOUND, "页面不存在")
            return
        filename, content_type = routes[path]
        try:
            payload = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "面板静态文件缺失")
            return
        self._send_headers(HTTPStatus.OK, content_type, len(payload))
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _breed_static(self, path: str) -> None:
        if not self._authenticated():
            self._send_headers(
                HTTPStatus.FOUND,
                "text/plain; charset=utf-8",
                0,
                location="/",
            )
            return
        raw_relative = unquote(path.removeprefix("/breed/"))
        relative = PurePosixPath(raw_relative or "index.html")
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            self._error(HTTPStatus.NOT_FOUND, "配种助手页面不存在")
            return
        root = BREED_ROOT.resolve()
        target = (root / Path(*relative.parts)).resolve()
        if target != root and root not in target.parents:
            self._error(HTTPStatus.NOT_FOUND, "配种助手页面不存在")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE
                if relative == PurePosixPath("index.html")
                else HTTPStatus.NOT_FOUND,
                "配种助手尚未生成" if relative == PurePosixPath("index.html") else "配种助手资源不存在",
            )
            return
        try:
            payload = target.read_bytes()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "配种助手资源不可读")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() == ".js":
            content_type = "text/javascript"
        if content_type.startswith("text/") or target.suffix.lower() in {".json", ".webmanifest"}:
            content_type += "; charset=utf-8"
        csp = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        cache = "no-store" if target.suffix.lower() in {".html", ".json"} else "private, max-age=86400"
        self._send_headers(
            HTTPStatus.OK,
            content_type,
            len(payload),
            content_security_policy=csp,
            cache_control=cache,
        )
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            self._static(parsed.path)
            return
        if parsed.path == "/api/session":
            self._json(HTTPStatus.OK, {"ok": True, "authenticated": self._authenticated()})
            return
        if not self._authenticated():
            self._error(HTTPStatus.UNAUTHORIZED, "请先登录")
            return
        try:
            if parsed.path == "/api/status":
                value = status_payload()
            elif parsed.path == "/api/operation":
                value = {"operation": OPERATIONS.public()}
            elif parsed.path == "/api/checks":
                value = checks_payload()
            elif parsed.path == "/api/performance-history":
                query = parse_qs(parsed.query)
                range_name = str(query.get("range", ["playing"])[0])
                source = str(query.get("source", ["server"])[0])
                value = PERFORMANCE.query(range_name, source)
            elif parsed.path == "/api/settings":
                value = settings_payload()
            elif parsed.path == "/api/backups":
                value = backup_payload()
            elif parsed.path == "/api/download/backup":
                query = parse_qs(parsed.query)
                backup_id = str(query.get("id", [""])[0])
                self._download_file(selected_backup(backup_id), "application/gzip")
                return
            elif parsed.path == "/api/download/diagnostics":
                filename, payload = diagnostics_archive()
                self._download_bytes(filename, payload, "application/zip")
                return
            elif parsed.path == "/api/download/performance-history":
                query = parse_qs(parsed.query)
                range_name = str(query.get("range", ["playing"])[0])
                source = str(query.get("source", ["server"])[0])
                filename, payload = PERFORMANCE.csv_export(range_name, source)
                self._download_bytes(filename, payload, "text/csv; charset=utf-8")
                return
            elif parsed.path == "/api/logs":
                query = parse_qs(parsed.query)
                lines = min(300, max(20, int(query.get("lines", ["120"])[0])))
                game = manager.run(["journalctl", "-u", manager.SERVICE, "-n", str(lines), "--no-pager", "-o", "short-iso"], check=False, timeout=10)
                panel = manager.run(["journalctl", "-u", "palworld-panel.service", "-n", "60", "--no-pager", "-o", "short-iso"], check=False, timeout=10)
                value = {"game": redact(game.stdout or ""), "panel": redact(panel.stdout or "")}
            elif parsed.path == "/api/breed/status":
                value = breed_status_payload()
            else:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            self._json(HTTPStatus.OK, {"ok": True, "data": value})
        except manager.ManagerError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "读取状态失败")

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            payload = self._read_json(MAX_JSON_BYTES)
            if path == "/api/login":
                address = self.client_address[0]
                if not SESSIONS.login_allowed(address):
                    self._error(HTTPStatus.TOO_MANY_REQUESTS, "尝试次数过多，请稍后再试")
                    return
                username = str(payload.get("username", ""))
                password = str(payload.get("password", ""))
                try:
                    expected = manager.SECRET.read_text(encoding="utf-8").strip()
                except OSError:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "管理密码文件不可读")
                    return
                valid = hmac.compare_digest(username.encode(), manager.API_USER.encode()) and hmac.compare_digest(
                    password.encode(), expected.encode()
                )
                if not valid:
                    SESSIONS.failed(address)
                    time.sleep(0.25)
                    self._error(HTTPStatus.UNAUTHORIZED, "用户名或密码不正确")
                    return
                remember = payload.get("remember") is True
                token = SESSIONS.create(address, remember=remember)
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "remembered": remember},
                    session_token=token,
                    remember_session=remember,
                )
                return
            if not self._require_post_auth():
                return
            if path == "/api/logout":
                with contextlib.suppress(manager.ManagerError):
                    SESSIONS.remove(self._cookie_token())
                self._json(HTTPStatus.OK, {"ok": True}, clear_session=True)
                return
            if path == "/api/breed/refresh":
                manager.run(
                    ["systemctl", "start", "--no-block", BREED_REFRESH_SERVICE],
                    timeout=10,
                )
                self._json(
                    HTTPStatus.ACCEPTED,
                    {"ok": True, "data": breed_status_payload()},
                )
                return
            if path == "/api/action":
                action = str(payload.get("action", ""))
                label, callback = action_callback(action, payload)
                operation = OPERATIONS.start(action, label, callback)
                self._json(HTTPStatus.ACCEPTED, {"ok": True, "operation": operation})
                return
            if path == "/api/settings":
                changes = payload.get("changes")
                if not isinstance(changes, dict) or not changes or len(changes) > 128:
                    raise manager.ManagerError("没有有效的设置改动")
                normalized: dict[str, str] = {}
                for key, value in changes.items():
                    if not isinstance(key, str) or key in manager.SENSITIVE_KEYS or key in LOCKED_SETTINGS:
                        raise manager.ManagerError(f"设置项不可由面板修改：{key}")
                    if isinstance(value, bool):
                        normalized[key] = "true" if value else "false"
                    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                        normalized[key] = str(value)
                    else:
                        raise manager.ManagerError(f"设置值无效：{key}")
                operation = OPERATIONS.start(
                    "settings", f"应用 {len(normalized)} 项世界设置",
                    lambda report: manager.set_settings(normalized, True, True, False, progress=report),
                    with_progress=True,
                    metadata={"change_count": len(normalized)},
                    success_message=lambda result: (
                        "设置已经是目标值，无需重复写入或重启"
                        if not isinstance(result, dict) or not result.get("changed")
                        else f"已应用并复核 {result['changed']} 项设置，游戏服务已恢复运行"
                        if result.get("restarted")
                        else f"已写入并复核 {result['changed']} 项设置，将在下次启动时生效"
                    ),
                )
                self._json(HTTPStatus.ACCEPTED, {"ok": True, "operation": operation})
                return
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
        except manager.ManagerError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "请求处理失败")


def main() -> int:
    os.umask(0o077)
    if not STATIC_ROOT.is_dir():
        raise RuntimeError(f"static directory missing: {STATIC_ROOT}")
    PERFORMANCE.start()
    server = PanelServer((HOST, PORT), Handler)

    def stop_server(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"Palworld panel {PANEL_VERSION} listening on {HOST}:{PORT}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        PERFORMANCE.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
