"""Post Desk: schedule and publish to many Instagram accounts from one website."""
import csv
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_from_directory, session, url_for)
from jinja2 import ChoiceLoader, FileSystemLoader
from PIL import Image

import instagram as ig

load_dotenv()

APP_ID = os.environ.get("INSTAGRAM_APP_ID", "")
APP_SECRET = os.environ.get("INSTAGRAM_APP_SECRET", "")
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TZ_NAME = os.environ.get("TIMEZONE", "Asia/Kolkata")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:                                # missing timezone data on the server
    TZ_NAME = "UTC"
    TZ = timezone.utc
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
MEDIA_DIR = os.path.join(DATA_DIR, "media")
DB_PATH = os.path.join(DATA_DIR, "postdesk.db")
MAX_ATTEMPTS = 3
os.makedirs(MEDIA_DIR, exist_ok=True)

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
# Pages can live in templates/ or loose next to app.py (both work).
app.jinja_loader = ChoiceLoader([FileSystemLoader(os.path.join(HERE, "templates")),
                                 FileSystemLoader(HERE)])
STATIC_FILES = {"style.css", "sample.csv"}


def serve_static(filename):
    if filename not in STATIC_FILES:
        abort(404)
    folder = os.path.join(HERE, "static")
    if not os.path.exists(os.path.join(folder, filename)):
        folder = HERE
    return send_from_directory(folder, filename)


app.add_url_rule("/static/<path:filename>", endpoint="static", view_func=serve_static)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=1024 * 1024 * 1024,   # 1 GB per request
                  SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=BASE_URL.startswith("https"),
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))


# ================================================================ database
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def q(sql, args=(), one=False):
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with db() as c:
        cur = c.execute(sql, args)
        return cur.rowcount


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY, ig_user_id TEXT UNIQUE, username TEXT,
            page_name TEXT, picture TEXT, access_token TEXT,
            active INTEGER DEFAULT 1, added_at TEXT, token_expires TEXT);
        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY, account_id INTEGER, type TEXT, media TEXT,
            caption TEXT, cover_url TEXT, scheduled_at TEXT, status TEXT,
            attempts INTEGER DEFAULT 0, error TEXT, permalink TEXT,
            media_id TEXT, created_at TEXT, posted_at TEXT);
        CREATE INDEX IF NOT EXISTS posts_due ON posts(status, scheduled_at);
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(accounts)")]
        if "token_expires" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN token_expires TEXT")
        # anything stuck mid-publish when the server stopped
        c.execute("UPDATE posts SET status='failed', error=? WHERE status='publishing'",
                  ("Server restarted while publishing. Check the page on Instagram before retrying.",))


# ================================================================ time helpers
def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return iso(datetime.now(timezone.utc))


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_local(s, fmt="%Y-%m-%dT%H:%M"):
    s = (s or "").strip()
    if not s:
        return datetime.now(timezone.utc)
    return datetime.strptime(s, fmt).replace(tzinfo=TZ)


@app.template_filter("local")
def local_filter(s, fmt="%I:%M %p"):
    return from_iso(s).astimezone(TZ).strftime(fmt).lstrip("0") if s else ""


@app.context_processor
def inject():
    return {"warn_base_url": not BASE_URL.startswith("https://"),
            "tzname": TZ_NAME}


# ================================================================ auth
def login_required(fn):
    @wraps(fn)
    def wrap(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrap


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if ADMIN_PASSWORD and secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.permanent = True
            session["auth"] = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") else "/")
        time.sleep(1)
        flash("That password is incorrect.", "error")
    return render_template("login.html", no_password=not ADMIN_PASSWORD)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ================================================================ media
VIDEO_EXT = {".mp4", ".mov"}
JPEG_EXT = {".jpg", ".jpeg"}
CONVERT_EXT = {".png", ".webp"}


def save_upload(f):
    ext = os.path.splitext(f.filename or "")[1].lower()
    name = uuid.uuid4().hex
    if ext in VIDEO_EXT or ext in JPEG_EXT:
        fname = name + ext
        f.save(os.path.join(MEDIA_DIR, fname))
    elif ext in CONVERT_EXT:                       # Instagram only accepts JPEG
        fname = name + ".jpg"
        Image.open(f.stream).convert("RGB").save(os.path.join(MEDIA_DIR, fname), "JPEG", quality=92)
    else:
        raise ValueError(f"{f.filename}: use MP4/MOV for video or JPG/PNG/WEBP for images")
    return f"{BASE_URL}/media/{fname}"


@app.route("/media/<path:name>")
def media(name):
    # Public on purpose: Instagram downloads files from here.
    return send_from_directory(MEDIA_DIR, name)


# ================================================================ publisher
def post_view(r):
    p = dict(r)
    urls = json.loads(p["media"] or "[]")
    p["urls"] = urls
    p["first"] = urls[0] if urls else ""
    p["first_is_video"] = ig.is_video(p["first"])
    p["media_count"] = len(urls)
    return p


def publish_one(pid):
    r = q("""SELECT p.*, a.ig_user_id, a.access_token FROM posts p
             JOIN accounts a ON a.id = p.account_id WHERE p.id=?""", (pid,), one=True)
    if not r:
        return
    try:
        media_id, link = ig.publish(r["ig_user_id"], r["access_token"], r["type"],
                                    json.loads(r["media"]), r["caption"] or "",
                                    r["cover_url"] or "")
        x("""UPDATE posts SET status='posted', media_id=?, permalink=?, posted_at=?,
             error=NULL WHERE id=?""", (media_id, link, now_iso(), pid))
    except Exception as e:                       # noqa: BLE001
        attempts = (r["attempts"] or 0) + 1
        if attempts < MAX_ATTEMPTS:
            retry_at = iso(datetime.now(timezone.utc) + timedelta(minutes=5 * attempts))
            x("UPDATE posts SET status='scheduled', attempts=?, error=?, scheduled_at=? WHERE id=?",
              (attempts, f"Attempt {attempts} failed, retrying: {e}", retry_at, pid))
        else:
            x("UPDATE posts SET status='failed', attempts=?, error=? WHERE id=?",
              (attempts, str(e), pid))


def process_due(pool):
    due = q("""SELECT p.id FROM posts p JOIN accounts a ON a.id = p.account_id
               WHERE p.status='scheduled' AND p.scheduled_at <= ? AND a.active=1
               ORDER BY p.scheduled_at""", (now_iso(),))
    futures = []
    for r in due:
        if x("UPDATE posts SET status='publishing' WHERE id=? AND status='scheduled'", (r["id"],)):
            futures.append(pool.submit(publish_one, r["id"]))
    for f in futures:
        f.result()


def refresh_tokens():
    """Instagram logins last 60 days; renew any that expire within 20 days."""
    soon = iso(datetime.now(timezone.utc) + timedelta(days=20))
    for a in q("SELECT * FROM accounts WHERE token_expires IS NULL OR token_expires < ?", (soon,)):
        try:
            tok, secs = ig.refresh(a["access_token"])
            x("UPDATE accounts SET access_token=?, token_expires=? WHERE id=?",
              (tok, iso(datetime.now(timezone.utc) + timedelta(seconds=secs)), a["id"]))
        except Exception:                        # noqa: BLE001
            app.logger.warning("could not renew login for @%s", a["username"])


def worker_loop():
    pool = ThreadPoolExecutor(max_workers=4)
    last_refresh = 0.0
    while True:
        try:
            process_due(pool)
            if time.time() - last_refresh > 6 * 3600:
                last_refresh = time.time()
                refresh_tokens()
        except Exception:                        # noqa: BLE001
            app.logger.exception("publisher error")
        time.sleep(20)


_worker_started = False


def start_worker():
    global _worker_started
    if not _worker_started and os.environ.get("DISABLE_WORKER") != "1":
        _worker_started = True
        threading.Thread(target=worker_loop, daemon=True).start()


# ================================================================ pages
@app.route("/")
@login_required
def dashboard():
    rows = q("""SELECT p.*, a.username FROM posts p JOIN accounts a ON a.id=p.account_id
                WHERE p.status IN ('scheduled','publishing') ORDER BY p.scheduled_at LIMIT 300""")
    days, today = [], datetime.now(TZ).date()
    for r in rows:
        p = post_view(r)
        d = from_iso(p["scheduled_at"]).astimezone(TZ).date()
        label = ("Today" if d == today else "Tomorrow" if d == today + timedelta(days=1)
                 else "Overdue" if d < today else d.strftime("%A, %d %B"))
        if not days or days[-1][0] != label:
            days.append((label, []))
        days[-1][1].append(p)
    start_today = iso(datetime.combine(today, datetime.min.time(), TZ))
    stats = {
        "scheduled": len(rows),
        "today": q("SELECT COUNT(*) n FROM posts WHERE status='posted' AND posted_at>=?",
                   (start_today,), one=True)["n"],
        "failed": q("SELECT COUNT(*) n FROM posts WHERE status='failed'", one=True)["n"],
    }
    accounts_count = q("SELECT COUNT(*) n FROM accounts", one=True)["n"]
    return render_template("dashboard.html", days=days, stats=stats,
                           accounts_count=accounts_count)


@app.route("/history")
@login_required
def history():
    status = request.args.get("status", "all")
    where = "" if status == "all" else "WHERE p.status=?"
    args = () if status == "all" else (status,)
    rows = q(f"""SELECT p.*, a.username FROM posts p JOIN accounts a ON a.id=p.account_id
                 {where} ORDER BY COALESCE(p.posted_at, p.scheduled_at) DESC LIMIT 300""", args)
    return render_template("history.html", posts=[post_view(r) for r in rows], status=status)


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_post():
    accounts = q("SELECT * FROM accounts WHERE active=1 ORDER BY username")
    if request.method == "GET":
        return render_template("new_post.html", accounts=accounts)

    ptype = request.form.get("type", "reel")
    valid_ids = {str(a["id"]) for a in accounts}
    created, errors = 0, []
    for i in range(int(request.form.get("variant_count", 0) or 0)):
        acc = request.form.get(f"account_{i}")
        if acc is None:
            continue                                     # removed in the form
        if acc not in valid_ids:
            errors.append(f"Version {i + 1}: choose a page.")
            continue
        try:
            urls = [u.strip() for u in request.form.get(f"urls_{i}", "").splitlines() if u.strip()]
            for f in request.files.getlist(f"files_{i}"):
                if f and f.filename:
                    urls.append(save_upload(f))
            cover = ""
            cf = request.files.get(f"cover_{i}")
            if cf and cf.filename:
                cover = save_upload(cf)
            when = parse_local(request.form.get(f"when_{i}"))
        except ValueError as e:
            errors.append(f"Version {i + 1}: {e}")
            continue
        if not urls:
            errors.append(f"Version {i + 1}: add a file or a media link.")
            continue
        if ptype == "carousel" and not 2 <= len(urls) <= 10:
            errors.append(f"Version {i + 1}: a carousel needs 2 to 10 files.")
            continue
        if ptype == "reel" and not ig.is_video(urls[0]):
            errors.append(f"Version {i + 1}: a reel needs an MP4 or MOV video.")
            continue
        if ptype != "carousel":
            urls = urls[:1]
        x("""INSERT INTO posts(account_id,type,media,caption,cover_url,scheduled_at,status,created_at)
             VALUES(?,?,?,?,?,?, 'scheduled', ?)""",
          (int(acc), ptype, json.dumps(urls), request.form.get(f"caption_{i}", ""),
           cover, iso(when), now_iso()))
        created += 1
    for e in errors:
        flash(e, "error")
    if created:
        flash(f"Scheduled {created} post{'s' if created > 1 else ''}.", "ok")
        return redirect(url_for("dashboard"))
    return render_template("new_post.html", accounts=accounts)


@app.post("/posts/<int:pid>/now")
@login_required
def post_now(pid):
    x("""UPDATE posts SET status='scheduled', scheduled_at=?, attempts=0, error=NULL
         WHERE id=? AND status IN ('scheduled','failed')""", (now_iso(), pid))
    flash("Publishing within the next minute.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/posts/<int:pid>/delete")
@login_required
def post_delete(pid):
    x("DELETE FROM posts WHERE id=? AND status!='publishing'", (pid,))
    flash("Removed from the queue.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


# ================================================================ accounts
def save_account(token, expires_in):
    info = ig.account_info(token)
    expires = iso(datetime.now(timezone.utc) + timedelta(seconds=expires_in))
    x("""INSERT INTO accounts(ig_user_id,username,page_name,picture,access_token,added_at,token_expires)
         VALUES(?,?,?,?,?,?,?)
         ON CONFLICT(ig_user_id) DO UPDATE SET username=excluded.username,
           page_name=excluded.page_name, picture=excluded.picture,
           access_token=excluded.access_token, token_expires=excluded.token_expires, active=1""",
      (info["ig_user_id"], info["username"], (info["account_type"] or "").replace("_", " ").title(),
       info["picture"], token, now_iso(), expires))
    return info


@app.route("/accounts")
@login_required
def accounts():
    rows = [dict(r) for r in q("SELECT * FROM accounts ORDER BY username")]
    now = datetime.now(timezone.utc)
    for a in rows:
        a["days_left"] = (from_iso(a["token_expires"]) - now).days if a.get("token_expires") else None
    checks = {}
    if request.args.get("check"):
        for a in rows:
            try:
                used, total = ig.quota(a["ig_user_id"], a["access_token"])
                checks[a["id"]] = {"ok": True, "msg": f"Working. {used} of {total} posts used in the last 24 hours."}
            except ig.GraphError as e:
                checks[a["id"]] = {"ok": False, "msg": f"Not working: {e}"}
    return render_template("accounts.html", accounts=rows, checks=checks,
                           oauth_ready=bool(APP_ID and APP_SECRET and BASE_URL))


@app.route("/accounts/connect")
@login_required
def connect():
    if not (APP_ID and APP_SECRET and BASE_URL):
        abort(400)
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    return redirect(ig.login_url(APP_ID, BASE_URL + "/oauth/callback", state))


@app.route("/oauth/callback")
@login_required
def oauth_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        flash("The Instagram login expired. Click Connect an Instagram account again.", "error")
        return redirect(url_for("accounts"))
    if request.args.get("error") or not request.args.get("code"):
        flash("Instagram login was cancelled. "
              + request.args.get("error_description", "").replace("+", " "), "error")
        return redirect(url_for("accounts"))
    try:
        short = ig.code_to_token(APP_ID, APP_SECRET, BASE_URL + "/oauth/callback", request.args["code"])
        token, secs = ig.long_lived(APP_SECRET, short)
        info = save_account(token, secs)
        flash(f"Connected @{info['username']}. To add the next account, log out of "
              "instagram.com in this browser, then click Connect again.", "ok")
    except ig.GraphError as e:
        flash(f"Couldn't connect: {e}", "error")
    return redirect(url_for("accounts"))


@app.post("/accounts/token")
@login_required
def connect_token():
    token = request.form.get("token", "").strip()
    secs = 60 * 86400
    try:
        if APP_SECRET:
            try:
                token, secs = ig.long_lived(APP_SECRET, token)
            except ig.GraphError:
                pass                              # already a long-lived token
        info = save_account(token, secs)
        flash(f"Connected @{info['username']}.", "ok")
    except ig.GraphError as e:
        flash(f"Couldn't connect with that token: {e}", "error")
    return redirect(url_for("accounts"))


@app.post("/accounts/<int:aid>/toggle")
@login_required
def account_toggle(aid):
    x("UPDATE accounts SET active = 1 - active WHERE id=?", (aid,))
    return redirect(url_for("accounts"))


@app.post("/accounts/<int:aid>/delete")
@login_required
def account_delete(aid):
    x("DELETE FROM posts WHERE account_id=? AND status IN ('scheduled','failed')", (aid,))
    x("DELETE FROM accounts WHERE id=?", (aid,))
    flash("Page disconnected and its scheduled posts removed.", "ok")
    return redirect(url_for("accounts"))


# ================================================================ CSV import
@app.route("/import", methods=["GET", "POST"])
@login_required
def import_csv():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("import_csv"))
        by_name = {a["username"].lower(): a["id"] for a in q("SELECT id, username FROM accounts")}
        reader = csv.DictReader(io.StringIO(f.read().decode("utf-8-sig")))
        created, errors = 0, []
        for n, row in enumerate(reader, start=2):
            name = (row.get("account") or "").strip().lstrip("@").lower()
            ptype = (row.get("type") or "").strip().lower()
            urls = [u.strip() for u in (row.get("media_urls") or "").split("|") if u.strip()]
            if name not in by_name:
                errors.append(f"Row {n}: page '{name}' isn't connected.")
                continue
            if ptype not in ("reel", "image", "carousel", "story") or not urls:
                errors.append(f"Row {n}: needs a type and at least one media link.")
                continue
            try:
                when = parse_local(row.get("scheduled_at"), "%Y-%m-%d %H:%M")
            except ValueError:
                errors.append(f"Row {n}: write the time as YYYY-MM-DD HH:MM.")
                continue
            x("""INSERT INTO posts(account_id,type,media,caption,cover_url,scheduled_at,status,created_at)
                 VALUES(?,?,?,?,?,?, 'scheduled', ?)""",
              (by_name[name], ptype, json.dumps(urls), row.get("caption") or "",
               (row.get("cover_url") or "").strip(), iso(when), now_iso()))
            created += 1
        for e in errors[:15]:
            flash(e, "error")
        flash(f"Imported {created} post{'s' if created != 1 else ''}.", "ok" if created else "info")
        return redirect(url_for("dashboard") if created and not errors else url_for("import_csv"))
    return render_template("import.html")


@app.route("/health")
def health():
    return {"ok": True}


init_db()
start_worker()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
