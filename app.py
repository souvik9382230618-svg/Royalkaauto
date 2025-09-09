import logging
import threading
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, jsonify
)

# Import configuration from config.py (no .env)
import config

BOT_TOKEN = config.BOT_TOKEN
ADMIN_TELEGRAM_IDS = set(config.ADMIN_TELEGRAM_IDS)
OUTPUT_GROUP_ID = config.OUTPUT_GROUP_ID
PANEL_USERNAME = config.PANEL_USERNAME
PANEL_PASSWORD = config.PANEL_PASSWORD
FLASK_SECRET = config.FLASK_SECRET
LIKE_API = config.LIKE_API
DB_PATH = config.DB_PATH

PORT = int(os.environ.get("PORT", "10000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autolike")

app = Flask(__name__)
app.secret_key = FLASK_SECRET

def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT NOT NULL,
            uid TEXT NOT NULL,
            days INTEGER NOT NULL,
            expiry_utc TEXT NOT NULL,
            added_by TEXT,
            added_at_utc TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
    """)
    conn.commit()
    conn.close()

def db_add_task(region: str, uid: str, days: int, added_by: str = "panel"):
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    now = datetime.now(timezone.utc)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks(region, uid, days, expiry_utc, added_by, added_at_utc, active) VALUES(?,?,?,?,?,?,1)",
        (region.strip(), uid.strip(), int(days), expiry.isoformat(), added_by, now.isoformat())
    )
    conn.commit()
    conn.close()

def db_list_tasks(include_inactive=False):
    conn = db_conn()
    cur = conn.cursor()
    if include_inactive:
        cur.execute("SELECT * FROM tasks ORDER BY id DESC;")
    else:
        cur.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id DESC;")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def db_prune_expired():
    conn = db_conn()
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute("UPDATE tasks SET active=0 WHERE active=1 AND expiry_utc < ?", (now_iso,))
    conn.commit()
    conn.close()

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

def tg_is_admin(user_id: int) -> bool:
    return user_id in ADMIN_TELEGRAM_IDS

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Use /help for commands.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 Commands:\n"
        "/autolike <region> <uid> <days>  — add a task (admin only)\n"
        "/tasks — list active tasks\n"
        "/run — run all active tasks now (admin only)"
    )

async def cmd_autolike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not tg_is_admin(user_id):
        await update.message.reply_text("❌ Only admin can add tasks.")
        return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /autolike <region> <uid> <days>")
        return
    region, uid = context.args[0], context.args[1]
    try:
        days = int(context.args[2])
    except ValueError:
        await update.message.reply_text("Days must be a number.")
        return
    db_add_task(region, uid, days, added_by=f"tg:{user_id}")
    await update.message.reply_text(f"✅ Task added: {region} / {uid} / {days} day(s)")

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_prune_expired()
    tasks = db_list_tasks()
    if not tasks:
        await update.message.reply_text("No active tasks.")
        return
    lines = []
    for t in tasks[:50]:
        lines.append(f"#{t['id']} • {t['region']} • {t['uid']} • exp: {t['expiry_utc']}")
    await update.message.reply_text("\n".join(lines))

def send_group_message(text: str):
    try:
        from telegram import Bot
        bot = Bot(BOT_TOKEN)
        bot.send_message(chat_id=OUTPUT_GROUP_ID, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error("Failed to send group message: %s", e)

def hit_like(region: str, uid: str):
    try:
        resp = requests.get(LIKE_API, params={"uid": uid, "server_name": region}, timeout=25)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        log.warning("Like API error: %s", e)
        return None

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not tg_is_admin(user_id):
        await update.message.reply_text("❌ Only admin can run tasks.")
        return
    total = run_all_tasks_sync()
    await update.message.reply_text(f"✅ Run complete. {total} task result(s) sent to group.")

def run_all_tasks_sync():
    db_prune_expired()
    tasks = db_list_tasks()
    sent = 0
    for t in tasks:
        result = hit_like(t["region"], t["uid"])
        if not result:
            continue
        msg = (
            "✅ *Likes Sent Successfully*\n"
            f"*Player:* {result.get('PlayerName','N/A')}\n"
            f"*UID:* `{t['uid']}`\n"
            f"*Region:* {t['region']}\n"
            f"*Level:* {result.get('Level','N/A')}\n"
            f"*Before:* {result.get('LikesbeforeCommand',0)}\n"
            f"*After:* {result.get('LikesafterCommand',0)}\n"
            f"*Given:* {result.get('LikesGivenByAPI',0)}"
        )
        send_group_message(msg)
        sent += 1
    return sent

def start_telegram_bot_in_thread():
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is not set. Telegram bot will not start.")
        return

    async def _main():
        app_tg = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .build()
        )
        app_tg.add_handler(CommandHandler("start", cmd_start))
        app_tg.add_handler(CommandHandler("help", cmd_help))
        app_tg.add_handler(CommandHandler("autolike", cmd_autolike))
        app_tg.add_handler(CommandHandler("tasks", cmd_tasks))
        app_tg.add_handler(CommandHandler("run", cmd_run))
        log.info("Telegram bot started (polling).")
        await app_tg.run_polling(close_loop=False)

    def runner():
        import asyncio
        try:
            asyncio.run(_main())
        except Exception as e:
            log.exception("Bot crashed: %s", e)

    th = threading.Thread(target=runner, daemon=True)
    th.start()

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == PANEL_USERNAME and password == PANEL_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template("login.html")

@app.route("/dashboard")
@login_required
def dashboard():
    db_prune_expired()
    tasks = db_list_tasks()
    return render_template("dashboard.html", tasks=tasks, total=len(tasks))

@app.route("/add_task", methods=["POST"])
@login_required
def add_task():
    region = request.form.get("region", "").strip()
    uid = request.form.get("uid", "").strip()
    days = int(request.form.get("days", "1"))
    if not region or not uid:
        flash("Region and UID are required.", "warning")
        return redirect(url_for("dashboard"))
    db_add_task(region, uid, days, added_by="panel")
    flash("Task added.", "success")
    return redirect(url_for("dashboard"))

@app.route("/run_tasks", methods=["POST"])
@login_required
def run_tasks():
    count = run_all_tasks_sync()
    flash(f"Run finished. {count} result(s) sent to group.", "success")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/api/tasks", methods=["GET"])
@login_required
def api_tasks():
    db_prune_expired()
    return jsonify(db_list_tasks())

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "ok",
        "time_utc": datetime.utcnow().isoformat(),
        "has_token": bool(BOT_TOKEN),
        "output_group_id": OUTPUT_GROUP_ID
    })

if __name__ == "__main__":
    db_init()
    start_telegram_bot_in_thread()
    log.info("Starting Flask admin on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
