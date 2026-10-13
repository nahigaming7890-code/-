import os
import re
import json
import time
import shutil
import zipfile
import hashlib
import random
import subprocess
import threading
import py_compile
import io
from datetime import datetime
from collections import defaultdict, deque

from flask import Flask, render_template, render_template_string, request, redirect, url_for, session, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash

# ============================================================
# APP SETUP
# ============================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
VENV_FOLDER = os.path.join(BASE_DIR, "venvs")
LOG_FOLDER = os.path.join(BASE_DIR, "logs")
DB_FILE = os.path.join(BASE_DIR, "database.json")
DB_LOCK = threading.Lock()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(VENV_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

ADMIN_PASS_ENV = os.environ.get("ADMIN_PASS", "112233")
ADMIN_INTERNAL_ID = "__admin__"

ENTRY_FILES_PY = ["main.py", "bot.py", "app.py", "run.py", "start.py"]
ENTRY_FILES_JS = ["index.js", "server.js", "bot.js", "app.js", "main.js"]
ENTRY_FILES_PHP = ["index.php", "bot.php", "main.php", "run.php", "start.php"]

# ============================================================
# ADS / TIME CONFIG (defaults — overridable via admin panel)
# ============================================================
ADS_PER_REWARD = 7           # 7 ads
SECONDS_PER_AD = 10          # each ad = 10 seconds
REWARD_SECONDS = 2 * 3600    # 2 hours reward
LOW_TIME_WARNING = 300       # 5 minutes

# ============================================================
# IN-MEMORY RUNTIME STATE
# ============================================================
processes = {}
proc_meta = {}
request_windows = defaultdict(lambda: deque())
login_attempts = defaultdict(lambda: {"count": 0, "locked_until": 0})
STATE_LOCK = threading.Lock()

# ============================================================
# DATABASE HELPERS
# ============================================================
def default_db():
    return {
        "users": {},
        "admin_device_tokens": {},
        "start_times": {},
        "app_settings": {},
        "public_tokens": {},
        "ad_events": {},          # email -> list of recent ad timestamps
        "config": {
            "max_concurrent_per_user": 3,
            "max_concurrent_vip": 10,
            "max_concurrent_global": 25,
            "sleep_after_hours": 4,
            "auto_wake_after_minutes": 20,
            "request_baseline_per_min": 30,
            "ads_per_reward": ADS_PER_REWARD,
            "seconds_per_ad": SECONDS_PER_AD,
            "reward_seconds": REWARD_SECONDS,
            "monetag_zone_id": "",
            "monetag_postback_secret": "",
        }
    }

def load_db():
    with DB_LOCK:
        if not os.path.exists(DB_FILE):
            data = default_db()
            _write_db(data)
            return data
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = default_db()
            _write_db(data)
            return data

        defaults = default_db()
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        for k, v in defaults["config"].items():
            if k not in data.get("config", {}):
                data["config"][k] = v

        # ensure every user has ad/time fields
        for email, rec in list(data.get("users", {}).items()):
            if not isinstance(rec, dict):
                continue
            rec.setdefault("vip", False)
            rec.setdefault("created", int(time.time() * 1000))
            rec.setdefault("time_balance", 0)
            rec.setdefault("ads_watched_today", 0)
            rec.setdefault("ads_in_current_cycle", 0)
            rec.setdefault("last_ad_day", "")
            rec.setdefault("total_ads_watched", 0)
            rec.setdefault("total_time_earned", 0)
            rec.setdefault("last_time_tick", int(time.time() * 1000))

        migrated_users = {}
        for uname, val in data.get("users", {}).items():
            if isinstance(val, dict):
                migrated_users[uname] = val
            else:
                pw = val
                if not (isinstance(pw, str) and (pw.startswith("pbkdf2:") or pw.startswith("scrypt:"))):
                    pw = generate_password_hash(pw)
                migrated_users[uname] = {
                    "pw_hash": pw, "vip": False, "created": int(time.time() * 1000),
                    "time_balance": 0, "ads_watched_today": 0, "ads_in_current_cycle": 0,
                    "last_ad_day": "", "total_ads_watched": 0, "total_time_earned": 0,
                    "last_time_tick": int(time.time() * 1000)
                }
        data["users"] = migrated_users
        return data

def _write_db(data):
    temp_db = DB_FILE + ".tmp"
    with open(temp_db, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_db, DB_FILE)

def save_db(data):
    with DB_LOCK:
        _write_db(data)

def get_config(db, key):
    return db.get("config", {}).get(key, default_db()["config"].get(key))

def get_user_concurrency_limit(db, email):
    user_rec = db.get("users", {}).get(email, {})
    if user_rec.get("vip"):
        return get_config(db, "max_concurrent_vip")
    return get_config(db, "max_concurrent_per_user")

def today_str():
    return datetime.utcnow().strftime("%Y-%m-%d")

# ============================================================
# SECURITY HELPERS
# ============================================================
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")

def is_safe_component(name):
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if not SAFE_NAME_RE.match(name):
        return False
    return True

def safe_join(base, *parts):
    base = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base, *parts))
    if not (target == base or target.startswith(base + os.sep)):
        raise ValueError("Path traversal blocked")
    return target

def is_locked_out(username):
    rec = login_attempts[username]
    return time.time() < rec["locked_until"]

def register_failed_login(username):
    rec = login_attempts[username]
    rec["count"] += 1
    if rec["count"] >= 5:
        rec["locked_until"] = time.time() + 60
        rec["count"] = 0

def reset_login_attempts(username):
    login_attempts[username] = {"count": 0, "locked_until": 0}

EMAIL_RE = re.compile(r"^[A-Za-z0-9_.+\-]+@[A-Za-z0-9\-]+\.[A-Za-z0-9\-.]+$")

def is_valid_email(email):
    return bool(email) and len(email) <= 120 and bool(EMAIL_RE.match(email))

def user_folder_id(email):
    return hashlib.sha256(email.lower().encode()).hexdigest()[:24]

def generate_secret_token(nbytes=24):
    return os.urandom(nbytes).hex()

# ============================================================
# DEVICE FINGERPRINT POOL
# ============================================================
DEVICE_POOL = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "platform": "Windows"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15", "platform": "Mac"},
    {"ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1", "platform": "iOS"},
    {"ua": "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36", "platform": "Android"},
    {"ua": "Mozilla/5.0 (Linux; Android 13; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36", "platform": "Android"},
    {"ua": "Dalvik/2.1.0 (Linux; U; Android 14; SM-S918B Build/UP1A.231005.007)", "platform": "Android-App"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "platform": "Linux"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0", "platform": "Windows"},
]

def assign_device(seed_key):
    rnd = random.Random(seed_key)
    return rnd.choice(DEVICE_POOL)

def device_env_vars(device):
    return {"BOT_USER_AGENT": device["ua"], "BOT_DEVICE_PLATFORM": device["platform"]}

# ============================================================
# AD / TIME HELPERS
# ============================================================
def ensure_user_time_fields(rec):
    rec.setdefault("time_balance", 0)
    rec.setdefault("ads_watched_today", 0)
    rec.setdefault("ads_in_current_cycle", 0)
    rec.setdefault("last_ad_day", "")
    rec.setdefault("total_ads_watched", 0)
    rec.setdefault("total_time_earned", 0)
    rec.setdefault("last_time_tick", int(time.time() * 1000))

def reset_daily_ad_counter_if_needed(rec):
    today = today_str()
    if rec.get("last_ad_day") != today:
        rec["last_ad_day"] = today
        rec["ads_watched_today"] = 0

def user_has_time(db, user_key):
    """user_key can be email OR __admin__ (admin = unlimited)."""
    if user_key == ADMIN_INTERNAL_ID:
        return True
    rec = db.get("users", {}).get(user_key)
    if not rec:
        return False
    if rec.get("vip"):
        return True
    return rec.get("time_balance", 0) > 0

def user_is_vip(db, user_key):
    if user_key == ADMIN_INTERNAL_ID:
        return True
    rec = db.get("users", {}).get(user_key)
    return bool(rec and rec.get("vip"))

def count_running_for_email(email):
    return sum(1 for (u, _), p in processes.items() if u == user_folder_id(email) and p.poll() is None)

def user_email_from_key(user_key):
    """Reverse-lookup: given a folder_id, find the email."""
    db = load_db()
    for email in db.get("users", {}).keys():
        if user_folder_id(email) == user_key:
            return email
    return None

# ============================================================
# APPS: VALIDATION
# ============================================================
def validate_python_file(path):
    try:
        py_compile.compile(path, doraise=True)
        return {"ok": True}
    except py_compile.PyCompileError as e:
        msg = str(e.exc_value) if hasattr(e, "exc_value") else str(e)
        line = None
        m = re.search(r"line (\d+)", msg)
        if m: line = int(m.group(1))
        return {"ok": False, "line": line, "error": msg.strip()}
    except SyntaxError as e:
        return {"ok": False, "line": e.lineno, "error": f"{e.msg}"}
    except Exception as e:
        return {"ok": False, "line": None, "error": str(e)}

def validate_js_file(path):
    node_bin = shutil.which("node")
    if not node_bin:
        return {"ok": True, "skipped": True, "note": "Node not available"}
    try:
        result = subprocess.run([node_bin, "--check", path], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return {"ok": True}
        err = result.stderr.strip()
        line = None
        first_line = err.splitlines()[0] if err else ""
        m = re.search(re.escape(path) + r":(\d+)", first_line)
        if m: line = int(m.group(1))
        return {"ok": False, "line": line, "error": err}
    except Exception as e:
        return {"ok": False, "line": None, "error": str(e)}

def validate_php_file(path):
    php_bin = shutil.which("php")
    if not php_bin:
        return {"ok": True, "skipped": True, "note": "PHP not available"}
    try:
        result = subprocess.run([php_bin, "-l", path], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return {"ok": True}
        err = result.stderr.strip() or result.stdout.strip()
        line = None
        m = re.search(r"line (\d+)", err)
        if m: line = int(m.group(1))
        return {"ok": False, "line": line, "error": err}
    except Exception as e:
        return {"ok": False, "line": None, "error": str(e)}

def find_entry_file(extract_dir):
    for f in ENTRY_FILES_PY:
        if os.path.exists(os.path.join(extract_dir, f)): return f, "python"
    for f in ENTRY_FILES_JS:
        if os.path.exists(os.path.join(extract_dir, f)): return f, "node"
    for f in ENTRY_FILES_PHP:
        if os.path.exists(os.path.join(extract_dir, f)): return f, "php"
    return None, None

def validate_project(extract_dir):
    entry, kind = find_entry_file(extract_dir)
    if not entry:
        return {"ok": False, "error": f"Entry file not found. Required: {', '.join(ENTRY_FILES_PY + ENTRY_FILES_JS + ENTRY_FILES_PHP)}"}
    problems = []
    for root, dirs, files in os.walk(extract_dir):
        dirs[:] = [d for d in dirs if d not in ("venv", "node_modules", "__pycache__", ".git")]
        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, extract_dir)
            if fname.endswith(".py"):
                res = validate_python_file(fpath)
                if not res["ok"]:
                    problems.append({"file": rel, "line": res.get("line"), "error": res.get("error")})
            elif fname.endswith(".js") and kind == "node":
                res = validate_js_file(fpath)
                if not res.get("ok") and not res.get("skipped"):
                    problems.append({"file": rel, "line": res.get("line"), "error": res.get("error")})
            elif fname.endswith(".php") and kind == "php":
                res = validate_php_file(fpath)
                if not res.get("ok") and not res.get("skipped"):
                    problems.append({"file": rel, "line": res.get("line"), "error": res.get("error")})
    if problems:
        return {"ok": False, "entry": entry, "kind": kind, "problems": problems}
    return {"ok": True, "entry": entry, "kind": kind}

# ============================================================
# SMART INSTALLERS
# ============================================================
def venv_path_for(user, app_name):
    return safe_join(VENV_FOLDER, f"{user}__{app_name}")

def file_hash(path):
    if not os.path.exists(path): return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def get_installed_packages(python_bin):
    try:
        result = subprocess.run([python_bin, "-m", "pip", "list", "--format=freeze"],
                                 capture_output=True, text=True, timeout=30)
        installed = {}
        for line in result.stdout.splitlines():
            if "==" in line:
                name, ver = line.split("==", 1)
                installed[name.lower()] = ver
        return installed
    except Exception:
        return {}

def parse_requirements(req_path):
    reqs = []
    with open(req_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            reqs.append(line)
    return reqs

def ensure_venv(venv_dir):
    if not os.path.exists(os.path.join(venv_dir, "bin", "python")):
        subprocess.run(["python3", "-m", "venv", venv_dir], check=True, timeout=120)
    return os.path.join(venv_dir, "bin", "python")

def smart_install_python(extract_dir, venv_dir, log_callback):
    req_path = os.path.join(extract_dir, "requirements.txt")
    state_path = os.path.join(venv_dir, ".install_state.json")
    if not os.path.exists(req_path):
        log_callback("No requirements.txt found; skipping.\n")
        return {"ok": True, "skipped": True}
    current_hash = file_hash(req_path)
    prev_state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f: prev_state = json.load(f)
        except Exception: prev_state = {}
    if prev_state.get("hash") == current_hash and prev_state.get("last_ok"):
        log_callback("requirements.txt unchanged; skipping install.\n")
        return {"ok": True, "skipped": True}
    try:
        python_bin = ensure_venv(venv_dir)
    except Exception as e:
        log_callback(f"Venv error: {e}\n")
        return {"ok": False, "error": str(e)}
    installed = get_installed_packages(python_bin)
    wanted = parse_requirements(req_path)
    to_install = []
    for spec in wanted:
        pkg_name = re.split(r"[=<>!~\[]", spec, 1)[0].strip().lower()
        if "==" in spec:
            _, ver = spec.split("==", 1)
            if installed.get(pkg_name) == ver.strip(): continue
        else:
            if pkg_name in installed: continue
        to_install.append(spec)
    if not to_install:
        log_callback("All packages already installed.\n")
        with open(state_path, "w") as f:
            json.dump({"hash": current_hash, "last_ok": True}, f)
        return {"ok": True, "skipped": True}
    log_callback(f"Installing: {', '.join(to_install)}\n")
    try:
        result = subprocess.run([python_bin, "-m", "pip", "install", "--disable-pip-version-check"] + to_install,
                                 capture_output=True, text=True, timeout=600)
        log_callback(result.stdout[-3000:] + "\n")
        if result.returncode != 0:
            log_callback("PIP WARN:\n" + result.stderr[-2000:] + "\n")
            with open(state_path, "w") as f:
                json.dump({"hash": current_hash, "last_ok": False}, f)
            return {"ok": False, "partial": True}
        with open(state_path, "w") as f:
            json.dump({"hash": current_hash, "last_ok": True}, f)
        log_callback("Dependencies installed.\n")
        return {"ok": True}
    except subprocess.TimeoutExpired:
        log_callback("Install timeout.\n")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        log_callback(f"Install error: {e}\n")
        return {"ok": False, "error": str(e)}

def smart_install_node(extract_dir, log_callback):
    pkg_path = os.path.join(extract_dir, "package.json")
    if not os.path.exists(pkg_path):
        log_callback("No package.json; skipping npm install.\n")
        return {"ok": True, "skipped": True}
    npm_bin = shutil.which("npm")
    if not npm_bin:
        log_callback("npm not available.\n")
        return {"ok": False, "error": "npm not found"}
    state_path = os.path.join(extract_dir, ".npm_install_state.json")
    current_hash = file_hash(pkg_path)
    prev_state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f: prev_state = json.load(f)
        except Exception: prev_state = {}
    if prev_state.get("hash") == current_hash and prev_state.get("last_ok"):
        log_callback("package.json unchanged; skipping npm install.\n")
        return {"ok": True, "skipped": True}
    log_callback("Running npm install...\n")
    try:
        result = subprocess.run([npm_bin, "install", "--no-audit", "--no-fund"],
                                 cwd=extract_dir, capture_output=True, text=True, timeout=600)
        log_callback(result.stdout[-3000:] + "\n")
        ok = result.returncode == 0
        if not ok: log_callback("NPM WARN:\n" + result.stderr[-2000:] + "\n")
        with open(state_path, "w") as f:
            json.dump({"hash": current_hash, "last_ok": ok}, f)
        return {"ok": ok}
    except subprocess.TimeoutExpired:
        log_callback("npm timeout.\n")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        log_callback(f"npm error: {e}\n")
        return {"ok": False, "error": str(e)}

# ============================================================
# APP SETTINGS
# ============================================================
def app_settings_key(user, name):
    return f"{user}_{name}"

def get_app_settings(db, user, name):
    key = app_settings_key(user, name)
    settings = db.setdefault("app_settings", {}).setdefault(key, {})
    settings.setdefault("auto_on", False)
    settings.setdefault("auto_restart", False)
    settings.setdefault("sleep_until", 0)
    settings.setdefault("offline_at", 0)
    settings.setdefault("device_ua", None)
    settings.setdefault("device_platform", None)
    settings.setdefault("manual_stop", False)
    settings.setdefault("unlimited", False)
    settings.setdefault("public_token", None)
    return settings

def ensure_public_token(db, user, name):
    settings = get_app_settings(db, user, name)
    if not settings.get("public_token"):
        token = generate_secret_token()
        settings["public_token"] = token
        db.setdefault("public_tokens", {})[token] = app_settings_key(user, name)
        save_db(db)
    return settings["public_token"]

def revoke_public_token(db, user, name):
    settings = get_app_settings(db, user, name)
    old = settings.get("public_token")
    if old:
        db.get("public_tokens", {}).pop(old, None)
    settings["public_token"] = None
    save_db(db)

def app_dirs(user, name):
    app_dir = safe_join(UPLOAD_FOLDER, user, name)
    extract_dir = os.path.join(app_dir, "extracted")
    log_path = os.path.join(app_dir, "logs.txt")
    return app_dir, extract_dir, log_path

def append_log(log_path, text):
    try:
        with open(log_path, "a", encoding="utf-8", errors="ignore") as f:
            f.write(text)
    except Exception:
        pass

def trim_log_if_needed(log_path, max_bytes=500_000):
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > max_bytes:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(-max_bytes // 2, os.SEEK_END)
                tail = f.read()
            with open(log_path, "w", encoding="utf-8", errors="ignore") as f:
                f.write("...[truncated]...\n" + tail)
    except Exception:
        pass

def count_running_for_user(user):
    return sum(1 for (u, _), p in processes.items() if u == user and p.poll() is None)

def count_running_global():
    return sum(1 for p in processes.values() if p.poll() is None)

# ============================================================
# APP LAUNCHER
# ============================================================
def start_app_process(user, name, db):
    """user = folder_id (or ADMIN_INTERNAL_ID)."""
    app_dir, extract_dir, log_path = app_dirs(user, name)
    key = (user, name)

    if key in processes and processes[key].poll() is None:
        return {"ok": True, "already_running": True}

    entry, kind = find_entry_file(extract_dir)
    if not entry:
        msg = f"Entry file not found. Required: {', '.join(ENTRY_FILES_PY + ENTRY_FILES_JS + ENTRY_FILES_PHP)}"
        append_log(log_path, f"[{datetime.now()}] START FAILED: {msg}\n")
        return {"ok": False, "error": msg}

    # ---------- TIME GATE ----------
    if user != ADMIN_INTERNAL_ID:
        email = user_email_from_key(user)
        if email:
            rec = db["users"].get(email, {})
            if not rec.get("vip"):
                if rec.get("time_balance", 0) <= 0:
                    return {"ok": False, "error": "No hosting time left. Please watch ads to earn more time."}

    # ---------- CONCURRENCY ----------
    max_global = get_config(db, "max_concurrent_global")
    if user != ADMIN_INTERNAL_ID:
        email = user_email_from_key(user)
        max_user = get_user_concurrency_limit(db, email) if email else 3
        if count_running_for_user(user) >= max_user:
            return {"ok": False, "error": f"You can run a maximum of {max_user} apps at once."}
    if count_running_global() >= max_global:
        return {"ok": False, "error": "Server at maximum capacity."}

    settings = get_app_settings(db, user, name)
    append_log(log_path, f"\n[{datetime.now()}] ===== Deploy start =====\n")

    def log_cb(text):
        append_log(log_path, text)

    if kind == "python":
        venv_dir = venv_path_for(user, name)
        install_res = smart_install_python(extract_dir, venv_dir, log_cb)
        try:
            python_bin = ensure_venv(venv_dir)
        except Exception as e:
            append_log(log_path, f"Venv error: {e}\n")
            return {"ok": False, "error": f"Environment setup fail: {e}"}
        cmd = [python_bin, entry]
    elif kind == "php":
        php_bin = shutil.which("php")
        if not php_bin:
            msg = "PHP is not installed on this server. Add php82 to your nixpacks.toml / mise.toml."
            append_log(log_path, f"[{datetime.now()}] START FAILED: {msg}\n")
            return {"ok": False, "error": msg}
        install_res = {"ok": True, "skipped": True}
        cmd = [php_bin, entry]
    else:
        install_res = smart_install_node(extract_dir, log_cb)
        node_bin = shutil.which("node") or "node"
        cmd = [node_bin, entry]

    seed_key = f"{user}_{name}_{int(time.time())}"
    device = assign_device(seed_key)
    settings["device_ua"] = device["ua"]
    settings["device_platform"] = device["platform"]
    settings["manual_stop"] = False

    env = os.environ.copy()
    env.update(device_env_vars(device))
    env["PYTHONUNBUFFERED"] = "1"

    trim_log_if_needed(log_path)
    log_file = open(log_path, "a", encoding="utf-8", errors="ignore")
    try:
        proc = subprocess.Popen(cmd, cwd=extract_dir, stdout=log_file, stderr=log_file, text=True, env=env)
    except Exception as e:
        append_log(log_path, f"Process start error: {e}\n")
        return {"ok": False, "error": str(e)}

    processes[key] = proc
    proc_meta[key] = {"device": device, "started": time.time(), "restarts": proc_meta.get(key, {}).get("restarts", 0)}

    now_ms = int(time.time() * 1000)
    db["start_times"][app_settings_key(user, name)] = now_ms
    settings["offline_at"] = 0
    settings["sleep_until"] = 0
    save_db(db)

    append_log(log_path, f"[{datetime.now()}] Started entry={entry}, device={device['platform']}\n")
    return {"ok": True, "install": install_res, "device": device["platform"]}

def stop_app_process(user, name, db, manual=True):
    key = (user, name)
    p = processes.get(key)
    if p and p.poll() is None:
        try:
            p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill()
        except Exception: pass
    if key in processes: del processes[key]
    settings = get_app_settings(db, user, name)
    settings["manual_stop"] = manual
    tkey = app_settings_key(user, name)
    if tkey in db["start_times"]:
        del db["start_times"][tkey]
    save_db(db)

def stop_all_user_apps(user_folder):
    db = load_db()
    user_dir = safe_join(UPLOAD_FOLDER, user_folder)
    if not os.path.exists(user_dir): return
    for name in os.listdir(user_dir):
        if os.path.isdir(os.path.join(user_dir, name)):
            stop_app_process(user_folder, name, db, manual=False)

# ============================================================
# TIME CONSUMPTION (runs every second in scheduler)
# ============================================================
def consume_time_for_all_users():
    db = load_db()
    changed = False
    now = time.time()

    # For each user with a time_balance > 0
    for email, rec in list(db.get("users", {}).items()):
        if not isinstance(rec, dict): continue
        if rec.get("vip"): continue
        if rec.get("time_balance", 0) <= 0:
            continue

        folder = user_folder_id(email)
        # Count running apps for this user
        running_count = sum(1 for (u, _), p in processes.items()
                            if u == folder and p.poll() is None)

        if running_count <= 0:
            # No apps running — no time consumed
            rec["last_time_tick"] = int(now * 1000)
            continue

        # Time spent since last tick
        last_ms = rec.get("last_time_tick", int(now * 1000))
        elapsed = (now * 1000 - last_ms) / 1000.0
        if elapsed <= 0:
            rec["last_time_tick"] = int(now * 1000)
            continue

        # Deduct (elapsed * number_of_running_apps)
        deduction = int(elapsed * running_count)
        if deduction <= 0:
            rec["last_time_tick"] = int(now * 1000)
            continue

        rec["time_balance"] = max(0, rec.get("time_balance", 0) - deduction)
        rec["last_time_tick"] = int(now * 1000)
        changed = True

        if rec["time_balance"] <= 0:
            # Time ran out — stop all apps
            print(f"[time] user {email} ran out of time, stopping apps")
            stop_all_user_apps(folder)
            rec["time_balance"] = 0

    if changed:
        save_db(db)

# ============================================================
# SCHEDULER
# ============================================================
def scheduler_loop():
    last_time_tick = 0
    while True:
        try:
            # Every second: consume time
            now = time.time()
            if now - last_time_tick >= 1:
                try:
                    consume_time_for_all_users()
                except Exception as e:
                    print(f"[scheduler] time consume error: {e}")
                last_time_tick = now

            # Every 30 seconds: check sleep/auto-on/crashes
            db = load_db()
            sleep_hours = get_config(db, "sleep_after_hours")
            wake_minutes = get_config(db, "auto_wake_after_minutes")
            changed = False

            for tkey, started_ms in list(db.get("start_times", {}).items()):
                if "_" not in tkey: continue
                user, name = tkey.split("_", 1)
                key = (user, name)
                settings = get_app_settings(db, user, name)
                started_s = started_ms / 1000.0

                p = processes.get(key)
                is_running = bool(p and p.poll() is None)

                if is_running and not settings.get("unlimited") and (now - started_s) >= sleep_hours * 3600:
                    _, _, log_path = app_dirs(user, name)
                    append_log(log_path, f"[{datetime.now()}] Auto-sleep: {sleep_hours}h reached.\n")
                    stop_app_process(user, name, db, manual=False)
                    settings = get_app_settings(db, user, name)
                    settings["offline_at"] = now
                    changed = True

                if p and not is_running and key in processes:
                    del processes[key]
                    if tkey in db["start_times"]:
                        del db["start_times"][tkey]
                    settings["offline_at"] = now
                    changed = True
                    _, _, log_path = app_dirs(user, name)
                    if settings.get("auto_restart") and not settings.get("manual_stop"):
                        append_log(log_path, f"[{datetime.now()}] Crash detected; auto-restarting.\n")
                        start_app_process(user, name, db)

                if not is_running and settings.get("offline_at") and settings.get("auto_on") and not settings.get("manual_stop"):
                    if (now - settings["offline_at"]) >= wake_minutes * 60:
                        _, _, log_path = app_dirs(user, name)
                        append_log(log_path, f"[{datetime.now()}] Auto-On: restarting.\n")
                        start_app_process(user, name, db)
                        changed = True

            if changed:
                save_db(db)
        except Exception as e:
            print(f"[scheduler] error: {e}")

        time.sleep(1)  # tight loop for time consumption

def resume_apps_after_restart():
    db = load_db()
    changed = False
    for tkey in list(db.get("start_times", {}).keys()):
        if "_" not in tkey: continue
        user, name = tkey.split("_", 1)
        key = (user, name)
        if key not in processes or processes[key].poll() is not None:
            del db["start_times"][tkey]
            settings = get_app_settings(db, user, name)
            settings["offline_at"] = time.time()
            changed = True
    if changed:
        save_db(db)

_scheduler_started = False
def ensure_scheduler():
    global _scheduler_started
    if not _scheduler_started:
        _scheduler_started = True
        resume_apps_after_restart()
        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()

# ============================================================
# LOGIN PAGE
# ============================================================
LOGIN_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login | MY HOST</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root { --bg: #f3faf5; --primary: #159957; --sec: #0b713d; --surface: #ffffff; --muted: #6c8275; --line: #dcebe1; }
        body { background: radial-gradient(circle at 15% 0%, #e1f5e8 0, transparent 35%), var(--bg); color: #183326; font-family: 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 18px; }
        .login-card { position: relative; z-index: 10; background: var(--surface); padding: 40px 30px; border-radius: 25px; width: 340px; max-width: 90vw; text-align: center; border: 1px solid var(--line); box-shadow: 0 25px 45px rgba(22,83,46,.12); }
        .lock-container { width: 80px; height: 80px; background: #e8f8ed; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; border: 2px solid #73c795; }
        .lock-icon { font-size: 35px; color: var(--primary); }
        h2 { font-size: 20px; margin-bottom: 10px; letter-spacing: 3px; text-transform: uppercase; color: var(--sec); }
        .sub { font-size: 11px; color: var(--muted); margin-bottom: 15px; }
        .err { background: #fff0f1; border: 1px solid #efabb2; color: #a62e3b; padding: 10px; border-radius: 10px; font-size: 13px; margin-bottom: 10px; }
        input, select { width: 100%; padding: 14px; margin: 8px 0; border-radius: 12px; border: 1px solid var(--line); background: #f8fcf9; color: #183326; outline: none; font-size: 14px; box-sizing: border-box; }
        input:focus, select:focus { border-color: #76c992; box-shadow: 0 0 0 3px #dff3e5; }
        button { width: 100%; padding: 15px; border-radius: 12px; border: none; background: linear-gradient(135deg, var(--sec), var(--primary)); color: #fff; font-weight: bold; font-size: 15px; cursor: pointer; margin-top: 10px; text-transform: uppercase; letter-spacing: 2px; }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="lock-container"><i class="fa-solid fa-user-shield lock-icon"></i></div>
        <h2>MY HOST</h2>
        <div class="sub">Log in with your email or create a new account instantly</div>
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
        <form method="post" action="/login">
            <input type="email" name="email" placeholder="Email address" required maxlength="120">
            <input type="password" name="password" placeholder="Password" required maxlength="100" minlength="4">
            <button type="submit">Login / Register</button>
        </form>
    </div>
</body>
</html>
'''

ADMIN_LOGIN_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin | MY HOST</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root { --bg: #f3faf5; --primary: #159957; --sec: #0b713d; --surface: #ffffff; --muted: #6c8275; --line: #dcebe1; }
        body { background: radial-gradient(circle at 85% 0%, #e1f5e8 0, transparent 35%), var(--bg); color: #183326; font-family: 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 18px; }
        .login-card { background: var(--surface); padding: 40px 30px; border-radius: 25px; width: 320px; max-width: 90vw; text-align: center; border: 1px solid var(--line); box-shadow: 0 25px 45px rgba(22,83,46,.12); }
        .lock-container { width: 80px; height: 80px; background: #e8f8ed; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; border: 2px solid #73c795; }
        .lock-icon { font-size: 35px; color: var(--primary); }
        h2 { font-size: 18px; letter-spacing: 3px; text-transform: uppercase; color: var(--sec); }
        .err { background: #fff0f1; border: 1px solid #efabb2; color: #a62e3b; padding: 10px; border-radius: 10px; font-size: 13px; margin-bottom: 10px; }
        input { width: 100%; padding: 14px; margin: 8px 0; border-radius: 12px; border: 1px solid var(--line); background: #f8fcf9; color: #183326; outline: none; font-size: 14px; box-sizing: border-box; text-align: center; letter-spacing: 4px; }
        input:focus { border-color: #76c992; box-shadow: 0 0 0 3px #dff3e5; }
        button { width: 100%; padding: 15px; border-radius: 12px; border: none; background: linear-gradient(135deg, var(--sec), var(--primary)); color: #fff; font-weight: bold; font-size: 15px; cursor: pointer; margin-top: 10px; text-transform: uppercase; letter-spacing: 2px; }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="lock-container"><i class="fa-solid fa-shield-halved lock-icon"></i></div>
        <h2>Root Access</h2>
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
        <form method="post" action="/admin">
            <input type="password" name="password" placeholder="••••••" required maxlength="50" autofocus>
            <button type="submit">Unlock</button>
        </form>
    </div>
</body>
</html>
'''

# ============================================================
# ADMIN PANEL
# ============================================================
ADMIN_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Root | MY HOST</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root { --bg: #f3faf5; --card: #ffffff; --accent: #159957; --accent-dark:#0b713d; --text: #183326; --muted:#6c8275; --line:#dcebe1; --danger:#d94f5d; --gold:#f0b429; }
        * { box-sizing: border-box; }
        body { background: radial-gradient(circle at 85% 0%, #e1f5e8 0, transparent 32%), var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; margin: 0; padding: 15px; min-height: 100vh; padding-bottom: 60px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding: 12px; background: var(--card); border-radius: 15px; border: 1px solid var(--line); box-shadow:0 10px 30px rgba(22,83,46,.08); }
        .header h2 { font-size: 18px; color: var(--accent-dark); margin: 0; }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 10px; margin-bottom: 20px; }
        .stat-card { background: var(--card); padding: 12px; border-radius: 15px; border: 1px solid var(--line); text-align: center; box-shadow:0 10px 30px rgba(22,83,46,.06); }
        .stat-card p { font-size: 11px; margin: 4px 0; color:var(--muted); }
        .stat-card div.val { font-size: 17px; font-weight: bold; color: var(--accent-dark); }
        .card { background: var(--card); padding: 15px; border-radius: 20px; border: 1px solid var(--line); margin-bottom: 18px; box-shadow:0 10px 30px rgba(22,83,46,.06); }
        h3 { margin-top: 0; font-size: 15px; color: var(--accent-dark); display: flex; align-items: center; gap: 8px; }
        .input-group { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
        input, select { width: 100%; padding: 10px; border-radius: 10px; border: 1px solid var(--line); background: #f8fcf9; color: var(--text); outline: none; font-size: 13px; box-sizing: border-box; }
        input:focus, select:focus { border-color:#76c992; box-shadow:0 0 0 3px #dff3e5; }
        .btn { padding: 10px; border-radius: 10px; border: none; font-weight: bold; cursor: pointer; text-align: center; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; gap: 6px; font-size: 13px; }
        .btn-primary { background: linear-gradient(135deg, var(--accent-dark), var(--accent)); color: #fff; }
        .btn-logout { background: var(--danger); color: white; padding: 8px 14px; }
        .btn-danger { background: var(--danger); color: white; }
        .btn-gold { background: linear-gradient(135deg, #f0b429, #d99a1f); color: #fff; }
        .btn-small { padding: 6px 10px; font-size: 11px; }
        .user-item { background: #f5fbf7; border-radius: 12px; padding: 12px; margin-bottom: 8px; border: 1px solid var(--line); }
        .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
        .username { font-weight: bold; color: var(--accent-dark); font-size: 13px; word-break: break-all; }
        .tag { padding: 3px 8px; border-radius: 6px; font-size: 10px; border: 1px solid; }
        .tag-green { color: #168b4f; border-color: #73c795; background: #e8f8ed; }
        .tag-red { color: #a62e3b; border-color: #efabb2; background: #fff0f1; }
        .tag-gold { color: #b47d0e; border-color: #f0b429; background: #fff8e1; }
        .tag-blue { color: #2563eb; border-color: #93c5fd; background: #eff6ff; }
        .action-row { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
        .toggle-switch { position: relative; width: 44px; height: 24px; flex-shrink: 0; }
        .toggle-switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; inset: 0; background: #b7c7bd; border-radius: 24px; transition: .3s; }
        .slider:before { content: ""; position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: .3s; }
        input:checked + .slider { background: var(--accent); }
        input:checked + .slider:before { transform: translateX(20px); }
        .small-text { font-size: 11px; color:var(--muted); }
        .upload-mini { border: 2px dashed #9bd4af; border-radius: 14px; padding: 15px; text-align: center; background:#fbfefc; }
        .time-badge { font-size: 11px; color: var(--accent-dark); font-weight: bold; }
        .time-badge.low { color: var(--danger); }
    </style>
</head>
<body>
    <div class="header"><h2><i class="fa-solid fa-shield-halved"></i> MY HOST ROOT</h2><a href="/logout" class="btn btn-logout"><i class="fa-solid fa-power-off"></i></a></div>

    <div class="stats-grid">
        <div class="stat-card"><p>Users</p><div class="val">{{ users|length }}</div></div>
        <div class="stat-card"><p>VIP</p><div class="val">{{ vip_count }}</div></div>
        <div class="stat-card"><p>Running</p><div class="val">{{ running_count }}</div></div>
        <div class="stat-card"><p>RAM</p><div class="val">{{ ram_percent }}%</div></div>
    </div>

    <div class="card">
        <h3><i class="fa-solid fa-sliders"></i> Ad / Reward Config</h3>
        <form action="/admin/update_ads_config" method="post" class="input-group">
            <label class="small-text">Ads needed per reward</label>
            <input type="number" name="ads_per_reward" value="{{ ads_config.ads_per_reward }}">
            <label class="small-text">Seconds per ad</label>
            <input type="number" name="seconds_per_ad" value="{{ ads_config.seconds_per_ad }}">
            <label class="small-text">Reward seconds (per reward)</label>
            <input type="number" name="reward_seconds" value="{{ ads_config.reward_seconds }}">
            <label class="small-text">Monetag Zone ID (for HTML integration)</label>
            <input type="text" name="monetag_zone_id" value="{{ ads_config.monetag_zone_id }}" placeholder="e.g. 1234567">
            <label class="small-text">Monetag Postback Secret (server-side verification)</label>
            <input type="text" name="monetag_postback_secret" value="{{ ads_config.monetag_postback_secret }}" placeholder="your-secret">
            <button type="submit" class="btn btn-primary">Save Ads Config</button>
        </form>
    </div>

    <div class="card">
        <h3><i class="fa-solid fa-infinity"></i> Admin Unlimited Apps (no time cost)</h3>
        <div class="upload-mini">
            <input type="file" id="adminFileInput" accept=".zip" style="display:none;">
            <button class="btn btn-primary" onclick="document.getElementById('adminFileInput').click()"><i class="fa-solid fa-upload"></i> Upload Admin App</button>
            <div id="adminUploadStatus" class="small-text" style="margin-top:8px;"></div>
        </div>
        {% for a in admin_apps %}
        <div class="user-item" style="margin-top:10px;">
            <div class="row">
                <span><b>{{ a.name }}</b></span>
                {% if a.running %}<span class="tag tag-green">RUNNING</span>{% else %}<span class="tag tag-red">OFFLINE</span>{% endif %}
            </div>
            <div class="small-text">Device: {{ a.device or "—" }}</div>
            <div class="action-row">
                {% if a.running %}
                <a href="/stop/{{ a.name }}" class="btn btn-danger btn-small">Stop</a>
                {% else %}
                <a href="/run/{{ a.name }}" class="btn btn-primary btn-small">Run</a>
                {% endif %}
                <button class="btn btn-primary btn-small" onclick="getAdminPublicLink('{{ a.name }}')"><i class="fa-solid fa-link"></i> Link</button>
                <a href="/delete/{{ a.name }}" class="btn btn-danger btn-small">Delete</a>
            </div>
        </div>
        {% endfor %}
        {% if not admin_apps %}<p class="small-text">No admin apps yet.</p>{% endif %}
    </div>

    <div class="card">
        <h3><i class="fa-solid fa-sliders"></i> Global Limits</h3>
        <form action="/admin/update_config" method="post" class="input-group">
            <label class="small-text">Max apps per normal user</label>
            <input type="number" name="max_concurrent_per_user" value="{{ config.max_concurrent_per_user }}">
            <label class="small-text">Max apps per VIP</label>
            <input type="number" name="max_concurrent_vip" value="{{ config.max_concurrent_vip }}">
            <label class="small-text">Max apps server-wide</label>
            <input type="number" name="max_concurrent_global" value="{{ config.max_concurrent_global }}">
            <label class="small-text">Auto-sleep after (hours)</label>
            <input type="number" step="0.1" name="sleep_after_hours" value="{{ config.sleep_after_hours }}">
            <label class="small-text">Auto-wake after (minutes)</label>
            <input type="number" name="auto_wake_after_minutes" value="{{ config.auto_wake_after_minutes }}">
            <button type="submit" class="btn btn-primary">Save Config</button>
        </form>
    </div>

    <div class="card">
        <h3><i class="fa-solid fa-user-gear"></i> User Management</h3>
        {% for u in users %}
        <div class="user-item">
            <div class="row">
                <span class="username">
                    <i class="fa-solid fa-circle-user"></i> {{ u.email }}
                    {% if u.vip %}<span class="tag tag-gold" style="margin-left:6px;">VIP</span>{% endif %}
                </span>
                <label class="toggle-switch" title="VIP toggle">
                    <input type="checkbox" {% if u.vip %}checked{% endif %} onchange="toggleVip('{{ u.email }}', this)">
                    <span class="slider"></span>
                </label>
            </div>
            <div class="small-text" style="margin-top:6px;">
                Time: <span class="time-badge {% if u.time_balance < 300 %}low{% endif %}">{{ u.time_human }}</span>
                | Ads today: {{ u.ads_watched_today }}
                | Total ads: {{ u.total_ads_watched }}
            </div>
            <div class="action-row">
                <a href="/admin/login_as/{{ u.email }}" class="btn btn-primary btn-small"><i class="fa-solid fa-sign-in"></i> Login as</a>
            </div>
            <form action="/admin/set_time" method="post" style="display:flex; gap:5px; margin-top:6px;">
                <input type="hidden" name="email" value="{{ u.email }}">
                <select name="mode" style="flex:1;">
                    <option value="add">Add</option>
                    <option value="subtract">Subtract</option>
                    <option value="set">Set</option>
                </select>
                <input type="number" name="seconds" placeholder="seconds" style="flex:2;">
                <button type="submit" class="btn btn-primary btn-small"><i class="fa-solid fa-clock"></i></button>
            </form>
            <form action="/admin/change_pw" method="post" style="display:flex; gap:5px; margin-top:6px;">
                <input type="hidden" name="email" value="{{ u.email }}">
                <input type="text" name="new_pw" placeholder="New password">
                <button type="submit" class="btn btn-primary btn-small"><i class="fa-solid fa-save"></i></button>
            </form>
        </div>
        {% endfor %}
        {% if not users %}<p class="small-text">No users yet.</p>{% endif %}
    </div>

    <div class="card">
        <h3><i class="fa-solid fa-server"></i> All User Apps</h3>
        {% for a in all_apps %}
        <div class="user-item">
            <div class="row">
                <span style="font-size:12px;"><b>{{ a.email }}</b> / {{ a.name }}</span>
                {% if a.running %}<span class="tag tag-green">RUNNING</span>{% else %}<span class="tag tag-red">OFFLINE</span>{% endif %}
            </div>
            <div class="small-text">Device: {{ a.device or "—" }} | Auto-On: {{ "Yes" if a.auto_on else "No" }} | Auto-Restart: {{ "Yes" if a.auto_restart else "No" }}</div>
        </div>
        {% endfor %}
        {% if not all_apps %}<p class="small-text">No user apps yet.</p>{% endif %}
    </div>

<script>
function toggleVip(email, checkbox) {
    const fd = new FormData();
    fd.append('email', email);
    fetch('/admin/toggle_vip', { method: 'POST', body: fd })
        .then(r => r.json()).then(res => {
            if (!res.ok) { checkbox.checked = !checkbox.checked; }
            else { setTimeout(() => location.reload(), 300); }
        });
}

document.getElementById('adminFileInput').addEventListener('change', function(e) {
    const file = e.target.files[0];
    if (!file) return;
    const statusDiv = document.getElementById('adminUploadStatus');
    statusDiv.textContent = 'Uploading...';
    const fd = new FormData();
    fd.append('file', file, file.name);
    fetch('/admin/upload_admin_app', { method: 'POST', body: fd })
        .then(r => r.json()).then(res => {
            if (res.ok) { statusDiv.textContent = 'Deployed!'; setTimeout(() => location.reload(), 700); }
            else { statusDiv.textContent = ''; Swal.fire('Error', res.error || 'Upload fail', 'error'); }
        });
});

function getAdminPublicLink(name) {
    fetch('/admin/get_public_link/' + encodeURIComponent(name), { method: 'POST' })
        .then(r => r.json()).then(res => {
            if (res.ok) {
                Swal.fire({ title: 'Public Link', html: '<input readonly style="width:100%;padding:8px;" value="' + res.url + '" onclick="this.select()">', confirmButtonText: 'Close' });
            } else { Swal.fire('Error', res.error || 'Unable', 'error'); }
        });
}
</script>
</body>
</html>
'''

# ============================================================
# PUBLIC PAGE
# ============================================================
PUBLIC_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ name or "Not found" }} | MY HOST Public</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root { --bg: #0a0f1e; --primary: #2563eb; --card: rgba(22,27,34,0.85); --text: #e6edf3; }
        * { box-sizing: border-box; }
        body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; margin: 0; padding: 15px; }
        .card { background: var(--card); border-radius: 18px; padding: 20px; border: 1px solid rgba(255,255,255,0.08); max-width: 500px; margin: 20px auto; }
        h2 { color: var(--primary); font-size: 18px; margin-top: 0; }
        .status-pill { font-size: 11px; padding: 5px 12px; border-radius: 20px; border: 1px solid; }
        .status-run { color: #2ecc71; border-color: #2ecc71; background: rgba(46,204,113,0.1); }
        .status-off { color: #ef4444; border-color: #ef4444; background: rgba(239,68,68,0.1); }
        .btn { padding: 12px; border-radius: 10px; border: none; font-weight: bold; cursor: pointer; font-size: 14px; width: 100%; margin-top: 10px; color: #fff; }
        .btn-primary { background: linear-gradient(45deg, #2563eb, #1d4ed8); color: #fff; }
        .btn-danger { background: #ef4444; }
        #logBox { background: #000; color: #0f0; font-family: monospace; font-size: 11px; padding: 10px; border-radius: 8px; max-height: 300px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin-top: 15px; }
        .small-text { font-size: 11px; opacity: 0.6; margin-top: 10px; }
    </style>
</head>
<body>
    {% if valid %}
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <h2><i class="fa-solid fa-server"></i> {{ name }}</h2>
            <span class="status-pill {% if running %}status-run{% else %}status-off{% endif %}" id="statusPill">{% if running %}RUNNING{% else %}OFFLINE{% endif %}</span>
        </div>
        <p class="small-text">Public control link — start, stop, and view logs.</p>
        <button class="btn btn-primary" id="startBtn" onclick="doAction('start')" {% if running %}style="display:none;"{% endif %}><i class="fa-solid fa-play"></i> Start</button>
        <button class="btn btn-danger" id="stopBtn" onclick="doAction('stop')" {% if not running %}style="display:none;"{% endif %}><i class="fa-solid fa-stop"></i> Stop</button>
        <div id="logBox">Loading logs...</div>
    </div>
    <script>
        const token = "{{ token }}";
        function refresh() {
            fetch('/public/' + token + '/status').then(r => r.json()).then(res => {
                if (!res.ok) return;
                document.getElementById('logBox').textContent = res.log || '(No logs)';
                const pill = document.getElementById('statusPill');
                pill.textContent = res.running ? 'RUNNING' : 'OFFLINE';
                pill.className = 'status-pill ' + (res.running ? 'status-run' : 'status-off');
                document.getElementById('startBtn').style.display = res.running ? 'none' : 'block';
                document.getElementById('stopBtn').style.display = res.running ? 'block' : 'none';
            });
        }
        function doAction(action) {
            fetch('/public/' + token + '/' + action, { method: 'POST' }).then(r => r.json()).then(res => {
                if (!res.ok) Swal.fire('Error', res.error || 'Action failed', 'error');
                setTimeout(refresh, 500);
            });
        }
        refresh();
        setInterval(refresh, 4000);
    </script>
    {% else %}
    <div class="card">
        <h2><i class="fa-solid fa-triangle-exclamation"></i> Link Invalid</h2>
        <p class="small-text">This link has expired or was never valid.</p>
    </div>
    {% endif %}
</body>
</html>
'''

# ============================================================
# ROUTES — AUTH
# ============================================================
@app.before_request
def _boot():
    ensure_scheduler()

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "").strip()
        if not is_valid_email(email):
            return render_template_string(LOGIN_HTML, error="Enter a valid email.")
        if not pw or len(pw) < 4:
            return render_template_string(LOGIN_HTML, error="Password must be at least 4 characters.")
        if is_locked_out(email):
            return render_template_string(LOGIN_HTML, error="Too many failed attempts. Try again in 1 minute.")
        db = load_db()
        folder_id = user_folder_id(email)
        if email not in db["users"]:
            db["users"][email] = {
                "pw_hash": generate_password_hash(pw), "vip": False,
                "created": int(time.time() * 1000),
                "time_balance": 600,   # 10 minutes free on signup (adjust as you like)
                "ads_watched_today": 0, "ads_in_current_cycle": 0,
                "last_ad_day": "", "total_ads_watched": 0,
                "total_time_earned": 600, "last_time_tick": int(time.time() * 1000)
            }
            save_db(db)
            session['is_admin'], session['username'], session['user_folder'] = False, email, folder_id
            reset_login_attempts(email)
            return redirect(url_for("index"))
        if check_password_hash(db["users"][email]["pw_hash"], pw):
            session['is_admin'], session['username'], session['user_folder'] = False, email, folder_id
            reset_login_attempts(email)
            return redirect(url_for("index"))
        register_failed_login(email)
        error = "Incorrect password."
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if session.get('is_admin'):
        return redirect(url_for("admin_panel"))
    db = load_db()
    trusted_cookie = request.cookies.get("admin_trust")
    if trusted_cookie and trusted_cookie in db.get("admin_device_tokens", {}):
        session['is_admin'], session['username'] = True, ADMIN_INTERNAL_ID
        return redirect(url_for("admin_panel"))
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "").strip()
        if is_locked_out("__admin_pw__"):
            error = "Too many attempts."
        elif pw == ADMIN_PASS_ENV:
            reset_login_attempts("__admin_pw__")
            session['is_admin'], session['username'] = True, ADMIN_INTERNAL_ID
            token = generate_secret_token()
            db.setdefault("admin_device_tokens", {})[token] = True
            save_db(db)
            resp = redirect(url_for("admin_panel"))
            resp.set_cookie("admin_trust", token, max_age=60*60*24*365, httponly=True, samesite="Lax")
            return resp
        else:
            register_failed_login("__admin_pw__")
            error = "Incorrect password."
    return render_template_string(ADMIN_LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    was_admin = session.get('is_admin', False)
    session.clear()
    if was_admin:
        resp = redirect(url_for("login"))
        resp.delete_cookie("admin_trust")
        return resp
    return redirect(url_for("login"))

def require_login():
    return 'username' in session

def require_admin():
    return session.get('is_admin', False)

def current_user_key():
    if session.get('is_admin'):
        return ADMIN_INTERNAL_ID
    return session.get('user_folder') or user_folder_id(session.get('username', ''))

# ============================================================
# ROUTES — DASHBOARD
# ============================================================
@app.route("/")
def index():
    if not require_login():
        return redirect(url_for("login"))
    user_name = current_user_key()
    user_dir = safe_join(UPLOAD_FOLDER, user_name)
    os.makedirs(user_dir, exist_ok=True)
    db = load_db()
    email = session.get('username') if not session.get('is_admin') else None
    is_vip = False
    if email and email in db["users"]:
        is_vip = bool(db["users"][email].get("vip"))
    if session.get('is_admin'):
        is_vip = True

    apps_list = []
    for name in sorted(os.listdir(user_dir)):
        if not os.path.isdir(os.path.join(user_dir, name)): continue
        p = processes.get((user_name, name))
        running = bool(p and p.poll() is None)
        settings = get_app_settings(db, user_name, name)
        start_ms = db["start_times"].get(app_settings_key(user_name, name), 0)
        elapsed = (time.time() - start_ms / 1000.0) if start_ms else 0
        sleep_hours = get_config(db, "sleep_after_hours")
        wake_minutes = get_config(db, "auto_wake_after_minutes")
        remaining_run = max(0, sleep_hours * 3600 - elapsed) if running else 0
        remaining_wake = 0
        if not running and settings.get("offline_at") and settings.get("auto_on") and not settings.get("manual_stop"):
            remaining_wake = max(0, wake_minutes * 60 - (time.time() - settings["offline_at"]))
        apps_list.append({
            "name": name, "running": running,
            "auto_on": settings.get("auto_on", False),
            "auto_restart": settings.get("auto_restart", False),
            "device": settings.get("device_platform"),
            "remaining_run_min": round(remaining_run / 60, 1),
            "remaining_wake_min": round(remaining_wake / 60, 1),
        })
    display_name = "Admin" if session.get('is_admin') else session.get('username', user_name)
    return render_template("index.html", apps=apps_list, username=display_name, is_vip=is_vip)

# ============================================================
# ROUTES — ADS / TIME
# ============================================================
@app.route("/ads/status")
def ads_status():
    if not require_login():
        return jsonify({"ok": False}), 401
    db = load_db()
    if session.get('is_admin'):
        return jsonify({"ok": True, "is_vip": True, "time_balance": -1, "ads_watched_today": 0})
    email = session.get('username')
    rec = db["users"].get(email)
    if not rec:
        return jsonify({"ok": False}), 404
    ensure_user_time_fields(rec)
    reset_daily_ad_counter_if_needed(rec)
    save_db(db)
    return jsonify({
        "ok": True,
        "is_vip": bool(rec.get("vip")),
        "time_balance": rec.get("time_balance", 0),
        "ads_watched_today": rec.get("ads_watched_today", 0),
        "ads_in_current_cycle": rec.get("ads_in_current_cycle", 0),
        "total_ads_watched": rec.get("total_ads_watched", 0),
        "ads_per_reward": get_config(db, "ads_per_reward"),
        "seconds_per_ad": get_config(db, "seconds_per_ad"),
        "reward_seconds": get_config(db, "reward_seconds"),
    })

@app.route("/ads/watched", methods=["POST"])
def ads_watched():
    if not require_login():
        return jsonify({"ok": False}), 401
    if session.get('is_admin'):
        return jsonify({"ok": True, "ads_watched_today": 0})
    email = session.get('username')
    db = load_db()
    rec = db["users"].get(email)
    if not rec:
        return jsonify({"ok": False, "error": "user not found"}), 404
    ensure_user_time_fields(rec)
    reset_daily_ad_counter_if_needed(rec)

    # Only increment if we're within the ad cycle
    ads_per = get_config(db, "ads_per_reward")
    if rec.get("ads_in_current_cycle", 0) < ads_per:
        rec["ads_in_current_cycle"] = rec.get("ads_in_current_cycle", 0) + 1
        rec["ads_watched_today"] = rec.get("ads_watched_today", 0) + 1
        rec["total_ads_watched"] = rec.get("total_ads_watched", 0) + 1
        save_db(db)
    return jsonify({
        "ok": True,
        "ads_in_current_cycle": rec["ads_in_current_cycle"],
        "ads_watched_today": rec["ads_watched_today"]
    })

@app.route("/ads/claim", methods=["POST"])
def ads_claim():
    if not require_login():
        return jsonify({"ok": False}), 401
    if session.get('is_admin'):
        return jsonify({"ok": True, "time_balance": -1})
    email = session.get('username')
    db = load_db()
    rec = db["users"].get(email)
    if not rec:
        return jsonify({"ok": False, "error": "user not found"}), 404
    ensure_user_time_fields(rec)
    ads_per = get_config(db, "ads_per_reward")
    reward = get_config(db, "reward_seconds")
    if rec.get("ads_in_current_cycle", 0) < ads_per:
        return jsonify({"ok": False, "error": f"Not enough ads watched. Need {ads_per}."}), 400
    rec["time_balance"] = rec.get("time_balance", 0) + reward
    rec["total_time_earned"] = rec.get("total_time_earned", 0) + reward
    rec["ads_in_current_cycle"] = 0
    rec["last_time_tick"] = int(time.time() * 1000)
    save_db(db)
    return jsonify({"ok": True, "time_balance": rec["time_balance"]})

# ============================================================
# ROUTES — MONETAG POSTBACK
# ============================================================
@app.route("/monetag/postback")
def monetag_postback():
    """
    Monetag calls this URL after the user completes an ad.
    Configure this URL in your Monetag dashboard:
       https://your-app.up.railway.app/monetag/postback?email=USER_EMAIL&secret=YOUR_SECRET
    Monetag replaces USER_EMAIL with its own macro — see Monetag docs.
    """
    db = load_db()
    cfg_secret = get_config(db, "monetag_postback_secret") or ""
    provided_secret = request.args.get("secret", "")
    if cfg_secret and provided_secret != cfg_secret:
        return "Invalid secret", 403

    email = request.args.get("email", "").strip().lower()
    if not email or email not in db.get("users", {}):
        return "Unknown user", 404

    rec = db["users"][email]
    ensure_user_time_fields(rec)
    reset_daily_ad_counter_if_needed(rec)
    ads_per = get_config(db, "ads_per_reward")

    rec["ads_in_current_cycle"] = rec.get("ads_in_current_cycle", 0) + 1
    rec["ads_watched_today"] = rec.get("ads_watched_today", 0) + 1
    rec["total_ads_watched"] = rec.get("total_ads_watched", 0) + 1

    # Auto-credit reward if cycle complete
    if rec["ads_in_current_cycle"] >= ads_per:
        rec["time_balance"] = rec.get("time_balance", 0) + get_config(db, "reward_seconds")
        rec["total_time_earned"] = rec.get("total_time_earned", 0) + get_config(db, "reward_seconds")
        rec["ads_in_current_cycle"] = 0
        rec["last_time_tick"] = int(time.time() * 1000)

    save_db(db)
    return "OK", 200

# ============================================================
# ROUTES — UPLOAD / RUN
# ============================================================
def _do_upload(user_name, mark_unlimited=False):
    file = request.files.get("file")
    overwrite = request.form.get("overwrite") == "true"
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No file."}), 400
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"ok": False, "error": "Only .zip allowed."}), 400
    app_name = re.sub(r"[^A-Za-z0-9_\-]", "_", file.filename.rsplit('.', 1)[0])[:50]
    if not app_name:
        return jsonify({"ok": False, "error": "Invalid name."}), 400
    user_dir = safe_join(UPLOAD_FOLDER, user_name, app_name)
    already_exists = os.path.exists(user_dir)
    if already_exists and not overwrite:
        return jsonify({"ok": False, "needs_confirm": True, "error": f"'{app_name}' exists. Overwrite?"}), 200
    tmp_zip = os.path.join(BASE_DIR, f"_tmp_{user_name}_{app_name}_{int(time.time())}.zip")
    file.save(tmp_zip)
    if not zipfile.is_zipfile(tmp_zip):
        os.remove(tmp_zip)
        return jsonify({"ok": False, "error": "Corrupt ZIP."}), 400
    db = load_db()
    key = (user_name, app_name)
    if key in processes and processes[key].poll() is None:
        stop_app_process(user_name, app_name, db, manual=True)
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir, ignore_errors=True)
    os.makedirs(user_dir, exist_ok=True)
    extract_dir = os.path.join(user_dir, "extracted")
    try:
        with zipfile.ZipFile(tmp_zip, 'r') as zip_ref:
            bad = zip_ref.testzip()
            if bad: raise zipfile.BadZipFile(f"Corrupt entry: {bad}")
            for member in zip_ref.namelist():
                mp = os.path.abspath(os.path.join(extract_dir, member))
                if not mp.startswith(os.path.abspath(extract_dir)):
                    raise ValueError("Path traversal detected.")
            zip_ref.extractall(extract_dir)
    except (zipfile.BadZipFile, ValueError) as e:
        shutil.rmtree(user_dir, ignore_errors=True)
        if os.path.exists(tmp_zip): os.remove(tmp_zip)
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        shutil.rmtree(user_dir, ignore_errors=True)
        if os.path.exists(tmp_zip): os.remove(tmp_zip)
        return jsonify({"ok": False, "error": f"Extract error: {e}"}), 400
    finally:
        if os.path.exists(tmp_zip): os.remove(tmp_zip)
    if not os.path.exists(extract_dir) or not os.listdir(extract_dir):
        shutil.rmtree(user_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "ZIP empty."}), 400
    if mark_unlimited:
        settings = get_app_settings(db, user_name, app_name)
        settings["unlimited"] = True
        save_db(db)
    entry, kind = find_entry_file(extract_dir)
    if not entry:
        return jsonify({
            "ok": True,
            "warning": f"Uploaded, but no entry file found. Required: {', '.join(ENTRY_FILES_PY + ENTRY_FILES_JS + ENTRY_FILES_PHP)}",
            "app_name": app_name
        })
    return jsonify({"ok": True, "app_name": app_name, "entry": entry, "kind": kind})

@app.route("/upload", methods=["POST"])
def upload():
    if not require_login():
        return jsonify({"ok": False, "error": "Login required"}), 401
    return _do_upload(current_user_key())

@app.route("/validate/<name>")
def validate_app(name):
    if not require_login():
        return jsonify({"ok": False}), 401
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    try:
        _, extract_dir, _ = app_dirs(user_name, name)
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid path"}), 400
    if not os.path.exists(extract_dir):
        return jsonify({"ok": False, "error": "App not found."}), 404
    return jsonify(validate_project(extract_dir))

@app.route("/run/<name>")
def run_app_route(name):
    if not require_login():
        return redirect(url_for("login"))
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    db = load_db()
    result = start_app_process(user_name, name, db)
    if request.args.get("json") == "1":
        return jsonify(result)
    return redirect(url_for("index"))

@app.route("/stop/<name>")
def stop_app_route(name):
    if not require_login():
        return redirect(url_for("login"))
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    db = load_db()
    stop_app_process(user_name, name, db, manual=True)
    if request.args.get("json") == "1":
        return jsonify({"ok": True})
    return redirect(url_for("index"))

@app.route("/restart/<name>")
def restart_app_route(name):
    if not require_login():
        return redirect(url_for("login"))
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    db = load_db()
    stop_app_process(user_name, name, db, manual=False)
    time.sleep(0.5)
    result = start_app_process(user_name, name, db)
    if request.args.get("json") == "1":
        return jsonify(result)
    return redirect(url_for("index"))

@app.route("/delete/<name>")
def delete_app_route(name):
    if not require_login():
        return redirect(url_for("login"))
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    db = load_db()
    stop_app_process(user_name, name, db, manual=True)
    app_dir, _, _ = app_dirs(user_name, name)
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir, ignore_errors=True)
    venv_dir = venv_path_for(user_name, name)
    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir, ignore_errors=True)
    key = app_settings_key(user_name, name)
    db.get("app_settings", {}).pop(key, None)
    db.get("start_times", {}).pop(key, None)
    save_db(db)
    return redirect(url_for("index"))

@app.route("/toggle_auto_on/<name>", methods=["POST"])
def toggle_auto_on(name):
    if not require_login():
        return jsonify({"ok": False}), 401
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"ok": False}), 400
    db = load_db()
    settings = get_app_settings(db, user_name, name)
    settings["auto_on"] = not settings.get("auto_on", False)
    save_db(db)
    return jsonify({"ok": True, "auto_on": settings["auto_on"]})

@app.route("/toggle_auto_restart/<name>", methods=["POST"])
def toggle_auto_restart(name):
    if not require_login():
        return jsonify({"ok": False}), 401
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"ok": False}), 400
    db = load_db()
    settings = get_app_settings(db, user_name, name)
    settings["auto_restart"] = not settings.get("auto_restart", False)
    save_db(db)
    return jsonify({"ok": True, "auto_restart": settings["auto_restart"]})

@app.route("/get_log/<name>")
def get_log(name):
    if not require_login():
        return jsonify({"log": "", "status": "OFFLINE"}), 401
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"log": "", "status": "OFFLINE"}), 400
    _, _, log_path = app_dirs(user_name, name)
    log_content = ""
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            log_content = f.read()[-4000:]
    p = processes.get((user_name, name))
    db = load_db()
    is_running = bool(p and p.poll() is None)
    settings = get_app_settings(db, user_name, name)
    return jsonify({
        "log": log_content,
        "status": "RUNNING" if is_running else "OFFLINE",
        "start_time": db["start_times"].get(app_settings_key(user_name, name), 0),
        "device": settings.get("device_platform")
    })

@app.route("/download/<name>")
def download_app(name):
    if not require_login():
        return redirect(url_for("login"))
    user_name = current_user_key()
    if not is_safe_component(name):
        return "Invalid name", 400
    _, extract_dir, _ = app_dirs(user_name, name)
    if not os.path.exists(extract_dir):
        return "Not found", 404
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(extract_dir):
            dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", "node_modules")]
            for f in files:
                fpath = os.path.join(root, f)
                zf.write(fpath, os.path.relpath(fpath, extract_dir))
    memory_file.seek(0)
    return send_file(memory_file, download_name=f"{name}.zip", as_attachment=True)

# ============================================================
# PUBLIC SHARE
# ============================================================
@app.route("/get_public_link/<name>", methods=["POST"])
def get_public_link(name):
    if not require_login():
        return jsonify({"ok": False}), 401
    if not is_safe_component(name):
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    user_name = current_user_key()
    _, extract_dir, _ = app_dirs(user_name, name)
    if not os.path.exists(extract_dir):
        return jsonify({"ok": False, "error": "App not found"}), 404
    db = load_db()
    token = ensure_public_token(db, user_name, name)
    return jsonify({"ok": True, "token": token, "url": url_for("public_view", token=token, _external=True)})

def _lookup_public_token(token):
    db = load_db()
    tkey = db.get("public_tokens", {}).get(token)
    if not tkey or "_" not in tkey:
        return None, None, None
    user, name = tkey.split("_", 1)
    return db, user, name

@app.route("/public/<token>")
def public_view(token):
    db, user, name = _lookup_public_token(token)
    if not user:
        return render_template_string(PUBLIC_HTML, valid=False, name=None, running=False, token=token)
    p = processes.get((user, name))
    running = bool(p and p.poll() is None)
    return render_template_string(PUBLIC_HTML, valid=True, name=name, running=running, token=token)

@app.route("/public/<token>/status")
def public_status(token):
    db, user, name = _lookup_public_token(token)
    if not user:
        return jsonify({"ok": False}), 404
    p = processes.get((user, name))
    running = bool(p and p.poll() is None)
    _, _, log_path = app_dirs(user, name)
    log_content = ""
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            log_content = f.read()[-3000:]
    return jsonify({"ok": True, "running": running, "log": log_content, "name": name})

@app.route("/public/<token>/start", methods=["POST"])
def public_start(token):
    db, user, name = _lookup_public_token(token)
    if not user:
        return jsonify({"ok": False}), 404
    return jsonify(start_app_process(user, name, db))

@app.route("/public/<token>/stop", methods=["POST"])
def public_stop(token):
    db, user, name = _lookup_public_token(token)
    if not user:
        return jsonify({"ok": False}), 404
    stop_app_process(user, name, db, manual=True)
    return jsonify({"ok": True})

# ============================================================
# FILE MANAGER
# ============================================================
@app.route("/list_files/<name>")
def list_files(name):
    if not require_login():
        return jsonify({"files": []}), 401
    user_name = current_user_key()
    if not is_safe_component(name):
        return jsonify({"files": []}), 400
    _, extract_dir, _ = app_dirs(user_name, name)
    files = []
    if os.path.exists(extract_dir):
        for root, dirs, filenames in os.walk(extract_dir):
            dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", "node_modules", ".git")]
            for f in filenames:
                files.append(os.path.relpath(os.path.join(root, f), extract_dir))
    return jsonify({"files": sorted(files)})

def _resolve_project_file(user_name, project, filename):
    if not is_safe_component(project):
        raise ValueError("Invalid project name")
    _, extract_dir, _ = app_dirs(user_name, project)
    target = os.path.abspath(os.path.join(extract_dir, filename))
    if not target.startswith(os.path.abspath(extract_dir) + os.sep) and target != os.path.abspath(extract_dir):
        raise ValueError("Path traversal blocked")
    return target

@app.route("/read_file", methods=["POST"])
def read_content():
    if not require_login():
        return jsonify({"content": ""}), 401
    data = request.json or {}
    try:
        path = _resolve_project_file(current_user_key(), data.get('project', ''), data.get('filename', ''))
    except ValueError as e:
        return jsonify({"content": "", "error": str(e)}), 400
    if os.path.exists(path) and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return jsonify({"content": f.read()})
        except Exception as e:
            return jsonify({"content": "", "error": str(e)}), 500
    return jsonify({"content": "", "error": "File not found"}), 404

@app.route("/save_file", methods=["POST"])
def save_content():
    if not require_login():
        return jsonify({"status": "error"}), 401
    data = request.json or {}
    try:
        path = _resolve_project_file(current_user_key(), data.get('project', ''), data.get('filename', ''))
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data.get('content', ''))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/delete_file", methods=["POST"])
def delete_file_api():
    if not require_login():
        return jsonify({"status": "error"}), 401
    data = request.json or {}
    try:
        path = _resolve_project_file(current_user_key(), data.get('project', ''), data.get('filename', ''))
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    if os.path.exists(path):
        os.remove(path)
        return jsonify({"status": "deleted"})
    return jsonify({"status": "error", "error": "Not found"}), 404

# ============================================================
# ADMIN ROUTES
# ============================================================
@app.route("/admin/panel")
def admin_panel():
    if not require_admin():
        return redirect(url_for("admin_login"))
    db = load_db()
    running_count = count_running_global()
    try:
        import psutil
        ram_percent = psutil.virtual_memory().percent
    except Exception:
        ram_percent = "N/A"

    all_apps = []
    admin_apps = []
    if os.path.exists(UPLOAD_FOLDER):
        for user in sorted(os.listdir(UPLOAD_FOLDER)):
            user_dir = os.path.join(UPLOAD_FOLDER, user)
            if not os.path.isdir(user_dir): continue
            for name in sorted(os.listdir(user_dir)):
                if not os.path.isdir(os.path.join(user_dir, name)): continue
                p = processes.get((user, name))
                running = bool(p and p.poll() is None)
                settings = get_app_settings(db, user, name)
                # reverse-lookup email
                email = user if user == ADMIN_INTERNAL_ID else (user_email_from_key(user) or user)
                entry = {
                    "user": user, "email": email, "name": name, "running": running,
                    "device": settings.get("device_platform"),
                    "auto_on": settings.get("auto_on"),
                    "auto_restart": settings.get("auto_restart"),
                }
                if user == ADMIN_INTERNAL_ID:
                    admin_apps.append(entry)
                else:
                    all_apps.append(entry)

    users_list = []
    vip_count = 0
    for email, rec in db.get("users", {}).items():
        if not isinstance(rec, dict): continue
        ensure_user_time_fields(rec)
        tb = rec.get("time_balance", 0)
        h = tb // 3600
        m = (tb % 3600) // 60
        s = tb % 60
        human = f"{h:02d}:{m:02d}:{s:02d}"
        users_list.append({
            "email": email,
            "vip": bool(rec.get("vip")),
            "time_balance": tb,
            "time_human": human,
            "ads_watched_today": rec.get("ads_watched_today", 0),
            "total_ads_watched": rec.get("total_ads_watched", 0),
        })
        if rec.get("vip"):
            vip_count += 1
    users_list.sort(key=lambda x: x["email"])

    ads_config = {
        "ads_per_reward": get_config(db, "ads_per_reward"),
        "seconds_per_ad": get_config(db, "seconds_per_ad"),
        "reward_seconds": get_config(db, "reward_seconds"),
        "monetag_zone_id": get_config(db, "monetag_zone_id") or "",
        "monetag_postback_secret": get_config(db, "monetag_postback_secret") or "",
    }

    return render_template_string(ADMIN_HTML, users=users_list, vip_count=vip_count,
                                   config=db.get("config", default_db()["config"]),
                                   ads_config=ads_config,
                                   running_count=running_count, ram_percent=ram_percent,
                                   all_apps=all_apps, admin_apps=admin_apps)

@app.route("/admin/update_config", methods=["POST"])
def admin_update_config():
    if not require_admin():
        return redirect(url_for("admin_login"))
    db = load_db()
    cfg = db.setdefault("config", {})
    for field, cast in [("max_concurrent_per_user", int), ("max_concurrent_vip", int),
                         ("max_concurrent_global", int),
                         ("sleep_after_hours", float), ("auto_wake_after_minutes", int)]:
        val = request.form.get(field)
        if val is not None:
            try: cfg[field] = cast(val)
            except ValueError: pass
    save_db(db)
    return redirect(url_for("admin_panel"))

@app.route("/admin/update_ads_config", methods=["POST"])
def admin_update_ads_config():
    if not require_admin():
        return redirect(url_for("admin_login"))
    db = load_db()
    cfg = db.setdefault("config", {})
    for field, cast in [("ads_per_reward", int), ("seconds_per_ad", int), ("reward_seconds", int)]:
        val = request.form.get(field)
        if val is not None:
            try: cfg[field] = cast(val)
            except ValueError: pass
    for field in ("monetag_zone_id", "monetag_postback_secret"):
        val = request.form.get(field)
        if val is not None:
            cfg[field] = val.strip()
    save_db(db)
    return redirect(url_for("admin_panel"))

@app.route("/admin/toggle_vip", methods=["POST"])
def admin_toggle_vip():
    if not require_admin():
        return jsonify({"ok": False}), 401
    email = request.form.get("email", "").strip().lower()
    db = load_db()
    if email in db["users"]:
        db["users"][email]["vip"] = not db["users"][email].get("vip", False)
        save_db(db)
        return jsonify({"ok": True, "vip": db["users"][email]["vip"]})
    return jsonify({"ok": False, "error": "User not found"}), 404

@app.route("/admin/set_time", methods=["POST"])
def admin_set_time():
    if not require_admin():
        return redirect(url_for("admin_login"))
    email = request.form.get("email", "").strip().lower()
    mode = request.form.get("mode", "add")
    try:
        seconds = int(request.form.get("seconds", 0))
    except ValueError:
        return redirect(url_for("admin_panel"))
    db = load_db()
    if email not in db["users"]:
        return redirect(url_for("admin_panel"))
    rec = db["users"][email]
    ensure_user_time_fields(rec)
    if mode == "add":
        rec["time_balance"] = rec.get("time_balance", 0) + seconds
    elif mode == "subtract":
        rec["time_balance"] = max(0, rec.get("time_balance", 0) - seconds)
    elif mode == "set":
        rec["time_balance"] = max(0, seconds)
    rec["last_time_tick"] = int(time.time() * 1000)
    save_db(db)
    return redirect(url_for("admin_panel"))

@app.route("/admin/change_pw", methods=["POST"])
def change_pw():
    if not require_admin():
        return redirect(url_for("admin_login"))
    email, new_pw = request.form.get("email", "").strip().lower(), request.form.get("new_pw", "").strip()
    db = load_db()
    if email in db["users"] and new_pw:
        db["users"][email]["pw_hash"] = generate_password_hash(new_pw)
        save_db(db)
    return redirect(url_for("admin_panel"))

@app.route("/admin/login_as/<path:email>")
def login_as(email):
    if not require_admin():
        return redirect(url_for("admin_login"))
    db = load_db()
    email = email.strip().lower()
    if email not in db["users"]:
        return redirect(url_for("admin_panel"))
    session['username'], session['is_admin'] = email, False
    session['user_folder'] = user_folder_id(email)
    return redirect(url_for("index"))

@app.route("/admin/upload_admin_app", methods=["POST"])
def admin_upload_app():
    if not require_admin():
        return jsonify({"ok": False}), 401
    return _do_upload(ADMIN_INTERNAL_ID, mark_unlimited=True)

@app.route("/admin/get_public_link/<name>", methods=["POST"])
def admin_get_public_link(name):
    if not require_admin():
        return jsonify({"ok": False}), 401
    if not is_safe_component(name):
        return jsonify({"ok": False, "error": "Invalid name"}), 400
    db = load_db()
    token = ensure_public_token(db, ADMIN_INTERNAL_ID, name)
    return jsonify({"ok": True, "token": token, "url": url_for("public_view", token=token, _external=True)})

# ============================================================
# ENTRY
# ============================================================
if __name__ == "__main__":
    ensure_scheduler()
    port = int(os.environ.get("PORT", 3522))
    app.run(host="0.0.0.0", port=port, debug=False)