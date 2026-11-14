"""TeamOnline — курсы и личные кабинеты (администратор, куратор, ученик). Данные: MySQL (курсы, пользователи, задания) + MongoDB (лента, журнал, входы, достижения)."""
import calendar
import html
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import db
import gamify
import mongo
import stats
from stats import PASS_PCT

app = Flask(__name__)


def _secret_key():
    """Ключ подписи сессий: из SECRET_KEY или из файла .secret_key (создаётся при первом запуске)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(db.BASE, ".secret_key")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key


app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",   # включите за HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
          "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_LOW = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

STATUS_LABELS = {"enrolled": "Записан", "learning": "Учится", "finished": "Завершил",
                 "behind": "Отстаёт", "failed": "Не завершил"}
COURSE_PILL = {"active": "Активен", "upcoming": "Скоро начнётся", "finished": "Завершён"}
EVENT_ICONS = {"join": ("green", "+"), "added": ("purple", "+"), "curator": ("yellow", "⇄"),
               "created": ("blue", "★"), "leave": ("red", "−"), "status": ("green", "✓")}
ROLE_LABELS = {"admin": "Администратор", "curator": "Куратор", "student": "Ученик"}


# ───────────────────────── утилиты ─────────────────────────
def today():
    iso = os.environ.get("TODAY")  # для демо: TODAY=2026-11-16 python app.py
    return date.fromisoformat(iso) if iso else date.today()


def plural(n, forms):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def days_word(n):
    return plural(n, ("день", "дня", "дней"))


def fmtdate(iso):
    return date.fromisoformat(iso[:10]).strftime("%d.%m.%Y")


def parse_date(value):
    """Принимает ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."""
    value = (value or "").strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        d, m, y = value.split(".")
        return date(int(y), int(m), int(d))
    return date.fromisoformat(value)


def short_name(full):
    parts = (full or "").split()
    if len(parts) < 2:
        return full or ""
    return f"{parts[0]} {parts[1] if '.' in parts[1] else parts[1][0].upper() + '.'}"


def workdays(start, end):
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def rel_time(stamp):
    created = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    sec = max(int((datetime.now() - created).total_seconds()), 0)
    if sec < 60:
        return "только что"
    if sec < 3600:
        return f"{sec // 60} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    if created.date() == datetime.now().date() - timedelta(days=1):
        return f"вчера, {created:%H:%M}"
    d = sec // 86400
    return f"{d} {days_word(d)} назад"


def bold(text):
    """Экранирует текст и превращает **слово** в жирное."""
    return Markup(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(text or "")))


def module_sub(m):
    return (f"{m['lessons']} {plural(m['lessons'], ('урок', 'урока', 'уроков'))} / "
            f"{m['tasks']} {plural(m['tasks'], ('задание', 'задания', 'заданий'))} / "
            f"{m['video_hours']} ч видео")


def fmtdt(stamp):
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M") if stamp else ""


app.jinja_env.filters.update(fmtdate=fmtdate, short=short_name, rel=rel_time, bold=bold,
                             module_sub=module_sub, fmtdt=fmtdt)
app.jinja_env.globals.update(STATUS_LABELS=STATUS_LABELS, COURSE_PILL=COURSE_PILL,
                             EVENT_ICONS=EVENT_ICONS, days_word=days_word, plural=plural,
                             PASS_PCT=PASS_PCT)


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def log_event(course_id, kind, text, feed_text=None):
    """Пишет событие в журнал курса и (если задан feed_text) в общую ленту."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mongo.add_event(course_id, kind, text, now)                 # MongoDB
    if feed_text:
        mongo.add_feed(course_id, kind, feed_text, now)


def curator_short(r):
    pat = f"{r['patronymic'][0]}." if r["patronymic"] else ""
    return f"{r['last_name']} {r['first_name'][0]}.{pat}"


COURSE_SQL = """
SELECT c.*, cu.last_name, cu.first_name, cu.patronymic,
       (SELECT COUNT(*) FROM enrollments e WHERE e.course_id = c.id) AS enrolled,
       (SELECT COUNT(*) FROM enrollments e WHERE e.course_id = c.id AND e.status = 'finished') AS done
FROM courses c JOIN curators cu ON cu.id = c.curator_id
"""


def course_view(row):
    c = dict(row)
    s, e, t = date.fromisoformat(c["start_date"]), date.fromisoformat(c["end_date"]), today()
    total = workdays(s, e)
    if t < s:
        status, day = "upcoming", 0
    elif t > e:
        status, day = "finished", total
    else:
        status, day = "active", workdays(s, t)
    until = (s - t).days
    c.update(
        status=status, day=day, total=total, pill=COURSE_PILL[status],
        span=f"{fmtdate(c['start_date'])} - {fmtdate(c['end_date'])} / {total} {days_word(total)}",
        label={"active": f"День {day} из {total}",
               "upcoming": f"Старт через {until} {days_word(until)}",
               "finished": "Курс завершён"}[status],
        pct=round(day / total * 100) if total else 0,
        left=total - day,
        # синяя полоска «заполненности» есть только у идущего курса — как на макете
        fill=round(min(c["enrolled"] / c["capacity"], 1) * 100, 1) if status == "active" else 0,
        curator=curator_short(c),
        curator_full=f"{c['last_name']} {c['first_name']} {c['patronymic']}".strip(),
        start_ru=fmtdate(c["start_date"]), end_ru=fmtdate(c["end_date"]),
    )
    return c




# ───────────────────────── вход, сессии, права ─────────────────────────
USER_SQL = """
SELECT u.*, cu.last_name, cu.first_name, cu.patronymic, st.full_name AS student_name
FROM users u LEFT JOIN curators cu ON cu.id = u.curator_id LEFT JOIN students st ON st.id = u.student_id
"""
DUMMY_HASH = generate_password_hash("not-a-real-password")   # чтобы вход не выдавал, существует ли логин
FAILS = {}                                                    # ключ -> времена неудачных попыток
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


def user_view(r):
    role = r["role"]
    if role == "curator":
        full = f"{r['last_name']} {r['first_name']} {r['patronymic']}".strip()
        name, first, ini = curator_short(r), r["first_name"], r["first_name"][0] + r["last_name"][0]
    elif role == "student":
        full = r["student_name"]
        parts = full.split()
        name, first = short_name(full), parts[1] if len(parts) > 1 else parts[0]
        ini = (parts[1][0] + parts[0][0]) if len(parts) > 1 else parts[0][:2]
    else:
        full = name = first = r["full_name"] or "Администратор"
        ini = name[:2]
    return dict(id=r["id"], role=role, role_label=ROLE_LABELS[role], login=r["login"], full=full, name=name,
                first_name=first, initials=ini.upper(), curator_id=r["curator_id"], student_id=r["student_id"])


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def csrf_ok():
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
    return secrets.compare_digest(sent.encode(), session.get("csrf", "").encode())


def safe_next(value):
    """Возвращаем пользователя только на страницы этого же сайта."""
    u = urlparse(value or "")
    if value and not u.scheme and not u.netloc and value.startswith("/") and not value.startswith("//") \
            and "\\" not in value and not value.startswith("/login"):
        return value
    return ""


def home_for(user):
    return "/me" if user["role"] == "student" else "/"


@app.before_request
def guard():
    g.user = None
    uid = session.get("uid")
    if uid:
        r = get_db().execute(USER_SQL + " WHERE u.id = ?", (uid,)).fetchone()
        # после смены пароля старые сессии перестают действовать
        if r and secrets.compare_digest(session.get("ph", ""), r["password_hash"][-16:]):
            g.user = user_view(r)
        else:
            session.clear()
    if request.method not in ("GET", "HEAD", "OPTIONS") and not csrf_ok():
        abort(400, "Сессия устарела. Обновите страницу и повторите действие.")
    if request.endpoint in ("login", "static"):
        return None
    if not g.user:
        if request.path.startswith("/api/"):
            return jsonify(error="Войдите в личный кабинет"), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    # ученик: любой заход на страницу кабинета засчитывается как вход за сегодня
    if g.user["role"] == "student" and request.method == "GET" and request.endpoint != "logout":
        day = today().isoformat()
        if session.get("seen_day") != day:
            conn = get_db()
            if gamify.record_login(g.user["id"], today()):
                gamify.award(conn, g.user["id"], g.user["student_id"], today())
                session["checkin"] = True
            conn.commit()
            session["seen_day"] = day
    return None


def roles(*allowed):
    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            if g.user["role"] not in allowed:
                abort(403)
            return fn(*a, **kw)
        return inner
    return deco


staff = roles("admin", "curator")
admin_only = roles("admin")
student_only = roles("student")


def nav_items(u):
    if not u:
        return []
    p = request.path
    if u["role"] == "student":
        items = [("/me", "Кабинет", p == "/me"), ("/me/tasks", "Задания", p.startswith("/me/tasks")),
                 ("/me/achievements", "Достижения", p.startswith("/me/achievements"))]
    else:
        items = [("/", "Курсы" if u["role"] == "admin" else "Мои курсы",
                  p == "/" or p.startswith(("/course", "/assignment")))]
        if u["role"] == "admin":
            items.append(("/admin/users", "Пользователи", p.startswith("/admin/users")))
    return items


@app.context_processor
def inject_globals():
    u = g.get("user")
    return dict(me=u, nav=nav_items(u), is_admin=bool(u and u["role"] == "admin"), csrf_token=csrf_token)


ERRORS = {400: "Некорректный запрос", 401: "Нужно войти", 403: "Нет доступа", 404: "Не найдено",
          405: "Метод не поддерживается", 429: "Слишком много запросов"}


@app.errorhandler(HTTPException)
def http_error(e):
    custom = e.description != type(e).description
    msg = e.description if custom else ERRORS.get(e.code, "Что-то пошло не так")
    if request.path.startswith("/api/"):
        return jsonify(error=msg), e.code
    return render_template("error.html", code=e.code, msg=msg), e.code


def _throttled(keys):
    now = time.time()
    for k in keys:
        FAILS[k[0]] = [t for t in FAILS.get(k[0], []) if now - t < 300]
    return any(len(FAILS[k[0]]) >= k[1] for k in keys)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(home_for(g.user))
    nxt = safe_next(request.values.get("next"))
    if request.method == "GET":
        return render_template("login.html", error=None, login="", next=nxt)
    name = (request.form.get("login") or "").strip()
    pwd = request.form.get("password") or ""
    ip = request.remote_addr or "?"
    keys = [(f"ip:{ip}", 40), (f"user:{name.lower()}", 8)]
    if _throttled(keys):
        return render_template("login.html", error="Слишком много попыток. Подождите пять минут.",
                               login=name, next=nxt), 429
    row = get_db().execute(USER_SQL + " WHERE u.login = ?", (name,)).fetchone()
    ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, pwd) and row is not None
    if not ok:
        for k in keys:
            FAILS.setdefault(k[0], []).append(time.time())
        return render_template("login.html", error="Неверный логин или пароль", login=name, next=nxt), 401
    for k in keys:
        FAILS.pop(k[0], None)
    session.clear()                                  # новая сессия — защита от подмены
    session["uid"] = row["id"]
    session["ph"] = row["password_hash"][-16:]
    session.permanent = request.form.get("remember") == "1"
    conn = get_db()
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
    conn.commit()
    return redirect(nxt or home_for(user_view(row)))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ───────────────────────── доступ к курсам ─────────────────────────
def scope_sql(alias="c"):
    """Куратор видит только свои курсы, администратор — все."""
    if g.user["role"] == "curator":
        return f" AND {alias}.curator_id = ?", [g.user["curator_id"]]
    return "", []


def course_row(cid):
    extra, params = scope_sql()
    row = get_db().execute(COURSE_SQL + " WHERE c.id = ?" + extra, [cid, *params]).fetchone()
    if not row:
        abort(404, "Курс не найден")
    return row


def course_id_of(table, rid):
    """Курс, к которому относится запись (module / assignment / enrollment); 404, если записи нет."""
    r = get_db().execute(f"SELECT course_id FROM {table} WHERE id = ?", (rid,)).fetchone()
    if not r:
        abort(404)
    course_row(r["course_id"])          # заодно проверяем, что курс доступен вошедшему
    return r["course_id"]


def list_courses(q="", status="", curator=0, sort="new"):
    extra, params = scope_sql()
    rows = [course_view(r) for r in get_db().execute(COURSE_SQL + " WHERE c.archived = 0" + extra, params)]
    q = q.strip().lower()
    if q:
        rows = [c for c in rows if q in c["title"].lower()]
    if status in COURSE_PILL:
        rows = [c for c in rows if c["status"] == status]
    if curator:
        rows = [c for c in rows if c["curator_id"] == curator]
    if sort == "title":
        rows.sort(key=lambda c: c["title"].lower())
    else:
        rows.sort(key=lambda c: (c["created_at"], c["id"]), reverse=(sort != "old"))
    return rows


# ───────────────────────── календарь ─────────────────────────
def build_calendar(year, month):
    extra, params = scope_sql()
    courses = [course_view(r) for r in get_db().execute(COURSE_SQL + " WHERE c.archived = 0" + extra, params)]
    t = today()
    cls = {"active": "act", "upcoming": "up", "finished": "fin"}
    days = []
    for week in calendar.Calendar(0).monthdatescalendar(year, month):
        for d in week:
            if d.month != month:
                days.append(None)
                continue
            hit = [c for c in courses
                   if c["start_date"] <= d.isoformat() <= c["end_date"] and d.weekday() < 5]
            days.append(dict(num=d.day, weekend=d.weekday() >= 5, today=d == t,
                             cls=cls[hit[0]["status"]] if hit else "",
                             title=", ".join(c["title"] for c in hit)))
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return dict(year=year, month=month, name=MONTHS[month - 1], show_year=year != t.year,
                days=days, weekdays=WEEKDAYS_SHORT, prev=(py, pm), next=(ny, nm))


@app.get("/calendar")
@staff
def calendar_partial():
    t = today()
    try:
        y, m = int(request.args.get("y", t.year)), int(request.args.get("m", t.month))
        date(y, m, 1)
    except ValueError:
        abort(400)
    return render_template("_calendar.html", cal=build_calendar(y, m))


# ───────────────────────── администратор и куратор: курсы ─────────────────────────
@app.get("/")
def index():
    if g.user["role"] == "student":
        return redirect("/me")
    q, status = request.args.get("q", ""), request.args.get("status", "")
    sort = request.args.get("sort", "new")
    try:
        cur = int(request.args.get("curator", 0))
    except ValueError:
        cur = 0
    conn = get_db()
    courses = list_courses(q, status, cur, sort)
    extra, params = scope_sql()
    if g.user["role"] == "curator":      # куратору — только лента по его курсам
        own = [r[0] for r in conn.execute("SELECT id FROM courses WHERE curator_id = ?", params).fetchall()]
        feed = mongo.feed_list(own, 6)
    else:
        feed = mongo.feed_list(None, 6)
    rating = [dict(r) for r in conn.execute(
        "SELECT c.id, c.title, c.rating, c.reviews FROM courses c WHERE c.archived = 0 AND c.rating IS NOT NULL"
        + extra + " ORDER BY c.rating DESC, c.reviews DESC LIMIT 3", params)]
    curators = conn.execute("SELECT * FROM curators ORDER BY last_name").fetchall()
    t = today()
    return render_template(
        "courses.html", courses=courses, q=q, status=status, cur=cur, sort=sort, feed=feed,
        rating=rating, cal=build_calendar(t.year, t.month),
        curators=[dict(id=r["id"], name=curator_short(r)) for r in curators])


def students_query(course_id, q="", show_all=False):
    conn = get_db()
    where, params = "e.course_id = ?", [course_id]
    if q.strip():
        where += " AND INSTR(LOWER(s.full_name), ?) > 0"
        params.append(q.strip().lower())
    total = conn.execute(f"SELECT COUNT(*) FROM enrollments e JOIN students s ON s.id = e.student_id "
                         f"WHERE {where}", params).fetchone()[0]
    sql = (f"SELECT e.id, e.enrolled_at, e.progress, e.status, s.full_name, u.id AS user_id, u.login "
           f"FROM enrollments e JOIN students s ON s.id = e.student_id "
           f"LEFT JOIN users u ON u.student_id = s.id WHERE {where} "
           f"ORDER BY e.enrolled_at DESC, e.id ASC")
    if not show_all:
        sql += " LIMIT 5"
    return conn.execute(sql, params).fetchall(), total


def student_candidates(cid):
    """Кого можно записать на курс: ещё не записанные на него. Куратору — только свободные или свои."""
    sql = ("SELECT s.id, s.full_name FROM students s "
           "WHERE s.id NOT IN (SELECT student_id FROM enrollments WHERE course_id = ?)")
    params = [cid]
    if g.user["role"] == "curator":
        sql += (" AND (s.id NOT IN (SELECT student_id FROM enrollments) OR s.id IN ("
                "SELECT e.student_id FROM enrollments e JOIN courses c ON c.id = e.course_id WHERE c.curator_id = ?))")
        params.append(g.user["curator_id"])
    return get_db().execute(sql + " ORDER BY s.full_name", params).fetchall()


@app.get("/course/<int:cid>")
@staff
def course_page(cid):
    conn = get_db()
    c = course_view(course_row(cid))

    modules = [dict(r, sub=module_sub(r)) for r in
               conn.execute("SELECT * FROM modules WHERE course_id = ? ORDER BY position, id", (cid,))]

    # столбики — реальная активность за последние 7 дней: сколько учеников заходили или сдавали работы
    t = today()
    week = stats.activity_days(conn, cid, t - timedelta(days=6), t)
    peak = max((d["active"] for d in week), default=0)
    bars = [dict(label=WEEKDAYS_LOW[d["date"].weekday()], title=f"{d['date']:%d.%m}: {d['active']}",
                 h=round(d["active"] / peak * 80) if peak else 0, weekend=d["date"].weekday() >= 5)
            for d in week]

    all_events = request.args.get("events") == "all"
    events = mongo.course_events(cid, None if all_events else 5)          # журнал курса — в MongoDB
    ev_total = mongo.count_events(cid)

    cur = conn.execute("SELECT * FROM curators WHERE id = ?", (c["curator_id"],)).fetchone()
    cur_stats = conn.execute(
        "SELECT COUNT(DISTINCT c.id) AS courses, COUNT(e.id) AS students FROM courses c "
        "LEFT JOIN enrollments e ON e.course_id = c.id WHERE c.curator_id = ?", (cur["id"],)).fetchone()
    curators = [dict(id=r["id"], name=curator_short(r))
                for r in conn.execute("SELECT * FROM curators ORDER BY last_name")]
    rows, total = students_query(cid)
    return render_template(
        "course.html", c=c, tab="about", modules=modules, bars=bars, events=events, all_events=all_events,
        ev_total=ev_total, curator=cur, initials=(cur["first_name"][0] + cur["last_name"][0]),
        cur_stats=cur_stats, curators=curators, rows=rows, total=total, show_all=False, q="",
        course_id=cid, candidates=student_candidates(cid))


@app.get("/course/<int:cid>/students")
@staff
def students_partial(cid):
    course_row(cid)
    rows, total = students_query(cid, request.args.get("q", ""), request.args.get("all") == "1")
    return render_template("_students.html", rows=rows, total=total, course_id=cid,
                           show_all=request.args.get("all") == "1", q=request.args.get("q", ""))


# ───────────────────────── задания, оценки, статистика (куратор / администратор) ─────────────────────────
def _asg_view(a):
    a["due_ru"] = fmtdate(a["due_date"]) if a["due_date"] else ""
    a["edit"] = dict(title=a["title"], module_id=a["module_id"], due=a["due_ru"], description=a["description"],
                     max_score=a["max_score"], answer_key=a["answer_key"])
    return a


@app.get("/course/<int:cid>/tasks")
@staff
def course_tasks(cid):
    conn = get_db()
    c = course_view(course_row(cid))
    items = [_asg_view(a) for a in stats.assignment_stats(conn, cid)]
    modules = conn.execute("SELECT id, title FROM modules WHERE course_id = ? ORDER BY position, id", (cid,)).fetchall()
    return render_template("course_tasks.html", c=c, tab="tasks", items=items, modules=modules,
                           pending=sum(a["pending"] for a in items))


@app.get("/course/<int:cid>/stats")
@staff
def course_stats(cid):
    conn = get_db()
    c = course_view(course_row(cid))
    t = today()
    students = stats.student_stats(conn, cid, t)
    asg = stats.assignment_stats(conn, cid)
    days = stats.activity_days(conn, cid, t - timedelta(days=13), t)
    top = max([d["active"] for d in days] + [d["subs"] for d in days] + [1])
    for d in days:
        d["h_active"], d["h_subs"] = round(d["active"] / top * 100), round(d["subs"] / top * 100)
    avg = conn.execute(
        "SELECT AVG(s.score * 100.0 / a.max_score) FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
        "JOIN enrollments e ON e.student_id = s.student_id AND e.course_id = a.course_id "
        "WHERE a.course_id = ? AND s.status = 'graded'", (cid,)).fetchone()[0]
    possible = len(asg) * len(students)
    summary = dict(
        students=len(students),
        avg_progress=round(sum(s["progress"] for s in students) / len(students)) if students else 0,
        avg_score=round(avg) if avg is not None else None,
        completion=round(sum(a["passed"] for a in asg) * 100 / possible) if possible else 0,
        pending=sum(a["pending"] for a in asg))
    ranked = sorted(asg, key=lambda a: (a["index"] is None, -(a["index"] or 0), a["position"]))
    return render_template("course_stats.html", c=c, tab="stats", summary=summary, days=days,
                           students=students, ranked=ranked)


def sub_state(r):
    st = r["status"] if "status" in r.keys() else r["sub_status"]
    if st is None:
        return "none"
    if st == "submitted":
        return "pending"
    if st == "returned":
        return "returned"
    return "done" if r["score"] * 100 >= PASS_PCT * r["max_score"] else "failed"


@app.get("/assignment/<int:aid>")
@staff
def assignment_page(aid):
    conn = get_db()
    a = conn.execute("SELECT * FROM assignments WHERE id = ?", (aid,)).fetchone()
    if not a:
        abort(404)
    c = course_view(course_row(a["course_id"]))
    st = next(x for x in stats.assignment_stats(conn, a["course_id"]) if x["id"] == aid)
    module = conn.execute("SELECT * FROM modules WHERE id = ?", (a["module_id"],)).fetchone() if a["module_id"] else None
    rows = []
    for r in conn.execute(
            "SELECT s.id AS student_id, s.full_name, sub.id AS sub_id, sub.status, sub.score, sub.attempts, sub.answer, "
            "sub.feedback, sub.submitted_at, ? AS max_score FROM enrollments e JOIN students s ON s.id = e.student_id "
            "LEFT JOIN submissions sub ON sub.assignment_id = ? AND sub.student_id = s.id WHERE e.course_id = ?",
            (a["max_score"], aid, a["course_id"])):
        state = sub_state(r)
        rows.append(dict(r, state=state, review=dict(name=r["full_name"], answer=r["answer"], attempts=r["attempts"],
                                                     score=r["score"], feedback=r["feedback"])))
    order = {"pending": 0, "returned": 1, "failed": 2, "done": 3, "none": 4}
    rows.sort(key=lambda r: (order[r["state"]], r["full_name"]))
    return render_template("assignment.html", a=a, c=c, tab="tasks", st=st, module=module, rows=rows)


# ───────────────────────── администратор: пользователи ─────────────────────────
@app.get("/admin/users")
@admin_only
def admin_users():
    conn = get_db()
    titles = {}
    for r in conn.execute("SELECT c.curator_id, c.title FROM courses c WHERE c.archived = 0 ORDER BY c.title"):
        titles.setdefault(("curator", r["curator_id"]), []).append(r["title"])
    for r in conn.execute("SELECT e.student_id, c.title FROM enrollments e JOIN courses c ON c.id = e.course_id "
                          "WHERE c.archived = 0 ORDER BY c.title"):
        titles.setdefault(("student", r["student_id"]), []).append(r["title"])
    groups = {"admin": [], "curator": [], "student": []}
    for r in conn.execute(USER_SQL + " ORDER BY u.id"):
        v = user_view(r)
        key = (v["role"], r["curator_id"] if v["role"] == "curator" else r["student_id"])
        info = ", ".join(titles.get(key, [])) or ("нет курсов" if v["role"] == "curator" else "не записан")
        groups[v["role"]].append(dict(id=v["id"], role=v["role"], name=v["full"], login=v["login"],
                                      info="" if v["role"] == "admin" else info, last_login=r["last_login"]))
    for lst in groups.values():
        lst.sort(key=lambda u: u["name"].lower())
    return render_template("admin_users.html", groups=[
        ("admin", "Администраторы", groups["admin"]), ("curator", "Кураторы", groups["curator"]),
        ("student", "Ученики", groups["student"])])


# ───────────────────────── ученик: личный кабинет ─────────────────────────
STATE_LABELS = {"new": "К выполнению", "pending": "На проверке", "returned": "Доработать",
                "failed": "Не засчитано", "done": "Выполнено"}


def student_tasks(conn, student_id):
    """Все задания всех курсов ученика с состоянием его работы."""
    t = today().isoformat()
    out = []
    for r in conn.execute(
            "SELECT a.id, a.course_id, a.title, a.description, a.max_score, a.due_date, a.answer_key, "
            "c.title AS course_title, c.start_date, c.end_date, m.title AS module_title, "
            "sub.status AS sub_status, sub.score, sub.attempts, sub.feedback, sub.answer, sub.submitted_at "
            "FROM enrollments e JOIN courses c ON c.id = e.course_id AND c.archived = 0 "
            "JOIN assignments a ON a.course_id = c.id LEFT JOIN modules m ON m.id = a.module_id "
            "LEFT JOIN submissions sub ON sub.assignment_id = a.id AND sub.student_id = e.student_id "
            "WHERE e.student_id = ? ORDER BY c.start_date, c.id, COALESCE(m.position, 9999), a.position, a.id",
            (student_id,)):
        d = dict(r)
        state = sub_state(dict(r, max_score=r["max_score"]))
        state = "new" if state == "none" else state
        d.update(state=state, state_label=STATE_LABELS[state], auto=bool(r["answer_key"]),
                 due_ru=fmtdate(r["due_date"]) if r["due_date"] else "",
                 overdue=bool(r["due_date"] and r["due_date"] < t and state in ("new", "returned", "failed")),
                 locked=r["start_date"] > t)
        del d["answer_key"]                                   # ответ ученику не показываем
        out.append(d)
    return out


@app.get("/me")
@student_only
def me_dashboard():
    conn = get_db()
    uid, sid, t = g.user["id"], g.user["student_id"], today()
    ach, m = gamify.overview(conn, uid, sid, t)
    tasks = student_tasks(conn, sid)
    open_tasks = [x for x in tasks if x["state"] in ("new", "returned", "failed") and not x["locked"]]
    open_tasks.sort(key=lambda x: (x["due_date"] is None, x["due_date"] or "", x["id"]))
    courses = []
    for e in conn.execute("SELECT course_id, progress FROM enrollments WHERE student_id = ?", (sid,)).fetchall():
        row = conn.execute(COURSE_SQL + " WHERE c.id = ? AND c.archived = 0", (e["course_id"],)).fetchone()
        if not row:
            continue
        own = [x for x in tasks if x["course_id"] == e["course_id"]]
        courses.append(dict(course_view(row), progress=e["progress"], n_tasks=len(own),
                            n_done=sum(x["state"] == "done" for x in own),
                            n_fix=sum(x["state"] in ("returned", "failed") for x in own)))
    nxt = next((a for a in ach if a["kind"] == "streak" and not a["earned"]), None)
    return render_template(
        "me_dashboard.html", checkin=session.pop("checkin", False), m=m, ach=ach, new=[a for a in ach if a["new"]],
        strip=gamify.week_strip(m["days"], t), nxt=nxt, courses=courses, todo=open_tasks[:5],
        todo_total=len(open_tasks), done=sum(x["state"] == "done" for x in tasks), total=len(tasks),
        pending=sum(x["state"] == "pending" for x in tasks), earned=sum(a["earned"] for a in ach))


@app.get("/me/tasks")
@student_only
def me_tasks():
    tasks = student_tasks(get_db(), g.user["student_id"])
    f = request.args.get("f", "all")
    keys = {"todo": ("new",), "fix": ("returned", "failed"), "pending": ("pending",), "done": ("done",)}
    counts = {"all": len(tasks), **{k: sum(x["state"] in v for x in tasks) for k, v in keys.items()}}
    shown = [x for x in tasks if f not in keys or x["state"] in keys[f]]
    groups = []
    for x in shown:
        if not groups or groups[-1]["course_id"] != x["course_id"]:
            groups.append(dict(course_id=x["course_id"], title=x["course_title"], items=[]))
        groups[-1]["items"].append(x)
    return render_template("me_tasks.html", groups=groups, counts=counts, f=f if f in keys else "all")


@app.get("/me/tasks/<int:aid>")
@student_only
def me_task(aid):
    t = next((x for x in student_tasks(get_db(), g.user["student_id"]) if x["id"] == aid), None)
    if not t:
        abort(404)
    return render_template("me_task.html", t=t)


@app.get("/me/achievements")
@student_only
def me_achievements():
    conn = get_db()
    ach, m = gamify.overview(conn, g.user["id"], g.user["student_id"], today())
    page = render_template("me_achievements.html", ach=ach, m=m, strip=gamify.week_strip(m["days"], today()),
                           earned=sum(a["earned"] for a in ach))
    mongo.mark_seen(g.user["id"])                                          # «Новое» показали
    return page


def norm_answer(text):
    return " ".join((text or "").split()).casefold()


@app.post("/api/assignments/<int:aid>/submit")
@student_only
def api_submit(aid):
    conn, sid = get_db(), g.user["student_id"]
    answer = ((request.get_json(silent=True) or {}).get("answer") or "").strip()
    if not answer:
        return bad("Введите ответ")
    if len(answer) > 5000:
        return bad("Ответ слишком длинный — не больше 5000 символов")
    a = conn.execute(
        "SELECT a.*, c.start_date, c.end_date FROM assignments a JOIN courses c ON c.id = a.course_id "
        "JOIN enrollments e ON e.course_id = c.id AND e.student_id = ? WHERE a.id = ? AND c.archived = 0",
        (sid, aid)).fetchone()
    if not a:
        return bad("Задание не найдено", 404)
    t = today().isoformat()
    if t < a["start_date"]:
        return bad("Курс ещё не начался")
    if t > a["end_date"]:
        return bad("Курс завершён — приём ответов закрыт")
    sub = conn.execute("SELECT * FROM submissions WHERE assignment_id = ? AND student_id = ?", (aid, sid)).fetchone()
    if sub and sub["status"] == "submitted":
        return bad("Работа уже на проверке")
    if sub and sub["status"] == "graded" and sub["score"] * 100 >= PASS_PCT * a["max_score"]:
        return bad("Задание уже выполнено")
    attempts = (sub["attempts"] if sub else 0) + 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if a["answer_key"]:                                          # автопроверка
        right = norm_answer(answer) == norm_answer(a["answer_key"])
        status, score, graded_at = "graded", a["max_score"] if right else 0, now
        result = ("done", "Верно! Задание выполнено.") if right else ("returned", "Неверно. Попробуйте ещё раз.")
    else:
        status, score, graded_at = "submitted", None, None
        result = ("pending", "Работа отправлена куратору на проверку.")
    if sub:
        conn.execute("UPDATE submissions SET answer=?, status=?, score=?, attempts=?, feedback='', submitted_at=?, "
                     "graded_at=? WHERE id=?", (answer, status, score, attempts, now, graded_at, sub["id"]))
    else:
        conn.execute("INSERT INTO submissions(assignment_id, student_id, answer, status, score, attempts, "
                     "submitted_at, graded_at) VALUES (?,?,?,?,?,?,?,?)",
                     (aid, sid, answer, status, score, attempts, now, graded_at))
    stats.recalc_progress(conn, a["course_id"], sid)
    new = gamify.award(conn, g.user["id"], sid, today())
    conn.commit()
    return jsonify(ok=True, status=result[0], message=result[1], new_achievements=[x["title"] for x in new])


@app.post("/api/submissions/<int:sub_id>/grade")
@staff
def api_grade(sub_id):
    conn = get_db()
    r = conn.execute("SELECT s.*, a.course_id, a.max_score FROM submissions s "
                     "JOIN assignments a ON a.id = s.assignment_id WHERE s.id = ?", (sub_id,)).fetchone()
    if not r:
        return bad("Работа не найдена", 404)
    course_row(r["course_id"])
    d = request.get_json(silent=True) or {}
    feedback = (d.get("feedback") or "").strip()[:2000]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if d.get("action") == "return":
        conn.execute("UPDATE submissions SET status='returned', score=NULL, feedback=?, graded_at=? WHERE id=?",
                     (feedback, now, sub_id))
    elif d.get("action") == "grade":
        try:
            score = int(str(d.get("score")).strip())
        except ValueError:
            return bad("Введите оценку числом")
        if not 0 <= score <= r["max_score"]:
            return bad(f"Оценка — от 0 до {r['max_score']}")
        conn.execute("UPDATE submissions SET status='graded', score=?, feedback=?, graded_at=? WHERE id=?",
                     (score, feedback, now, sub_id))
    else:
        return bad("Неизвестное действие")
    stats.recalc_progress(conn, r["course_id"], r["student_id"])
    u = conn.execute("SELECT id FROM users WHERE student_id = ?", (r["student_id"],)).fetchone()
    if u:
        gamify.award(conn, u["id"], r["student_id"], today())
    conn.commit()
    return jsonify(ok=True)


# ───────────────────────── API: курсы ─────────────────────────
def bad(msg, code=400):
    return jsonify(error=msg), code


def _course_payload(d):
    """Проверяет поля формы курса. Возвращает (значения, ошибка)."""
    title = (d.get("title") or "").strip()
    try:
        s, e = parse_date(d.get("start_date")), parse_date(d.get("end_date"))
        cap = int(d.get("capacity"))
    except (ValueError, TypeError):
        return None, "Проверьте даты (ДД.ММ.ГГГГ) и вместимость"
    if not title:
        return None, "Укажите название курса"
    if e < s:
        return None, "Дата окончания раньше даты начала"
    if not 1 <= cap <= 500:
        return None, "Вместимость — от 1 до 500"
    return dict(title=title[:120], direction=(d.get("direction") or "").strip()[:80],
                description=(d.get("description") or "").strip()[:1000], start=s.isoformat(), end=e.isoformat(), cap=cap), None


@app.post("/api/courses")
@admin_only
def api_create_course():
    d = request.get_json(silent=True) or {}
    v, err = _course_payload(d)
    if err:
        return bad(err)
    conn = get_db()
    try:
        cur = int(d.get("curator_id"))
    except (TypeError, ValueError):
        return bad("Назначьте куратора")
    new = conn.execute("SELECT * FROM curators WHERE id = ?", (cur,)).fetchone()
    if not new:
        return bad("Куратор не найден")
    cid = conn.execute(
        "INSERT INTO courses(title, direction, description, start_date, end_date, capacity, curator_id) "
        "VALUES (?,?,?,?,?,?,?)", (v["title"], v["direction"], v["description"], v["start"], v["end"], v["cap"], cur)).lastrowid
    log_event(cid, "created", "Курс создан", f"Создан курс «{v['title']}», куратор — **{curator_short(new)}**")
    conn.commit()
    return jsonify(ok=True, redirect=f"/course/{cid}"), 201


@app.patch("/api/courses/<int:cid>")
@staff
def api_update_course(cid):
    row = course_row(cid)
    d = request.get_json(silent=True) or {}
    v, err = _course_payload(d)
    if err:
        return bad(err)
    conn = get_db()
    if v["cap"] < row["enrolled"]:
        return bad(f"На курсе уже {row['enrolled']} студентов — вместимость не может быть меньше")
    cur = row["curator_id"]                         # куратора назначает только администратор
    if g.user["role"] == "admin":
        try:
            cur = int(d.get("curator_id"))
        except (TypeError, ValueError):
            return bad("Выберите куратора")
        if not conn.execute("SELECT 1 FROM curators WHERE id = ?", (cur,)).fetchone():
            return bad("Куратор не найден")
    conn.execute("UPDATE courses SET title=?, direction=?, description=?, start_date=?, end_date=?, "
                 "capacity=?, curator_id=? WHERE id=?",
                 (v["title"], v["direction"], v["description"], v["start"], v["end"], v["cap"], cur, cid))
    if cur != row["curator_id"]:
        new = conn.execute("SELECT * FROM curators WHERE id = ?", (cur,)).fetchone()
        log_event(cid, "curator", f"Куратор изменён: {curator_short(new)}",
                  f"По курсу «{v['title']}» назначен новый куратор - **{curator_short(new)}**")
    conn.commit()
    return jsonify(ok=True, message="Изменения сохранены")


@app.post("/api/courses/<int:cid>/archive")
@admin_only
def api_archive_course(cid):
    conn = get_db()
    if conn.execute("UPDATE courses SET archived = 1 WHERE id = ?", (cid,)).rowcount == 0:
        return bad("Курс не найден", 404)
    conn.commit()
    return jsonify(ok=True, redirect="/")


@app.delete("/api/courses/<int:cid>")
@admin_only
def api_delete_course(cid):
    conn = get_db()
    if conn.execute("DELETE FROM courses WHERE id = ?", (cid,)).rowcount == 0:
        return bad("Курс не найден", 404)
    conn.commit()
    mongo.detach_course(cid)
    return jsonify(ok=True, redirect="/")


@app.post("/api/courses/<int:cid>/curator")
@admin_only
def api_change_curator(cid):
    try:
        cur = int((request.get_json(silent=True) or {}).get("curator_id"))
    except (TypeError, ValueError):
        return bad("Выберите куратора")
    conn = get_db()
    new = conn.execute("SELECT * FROM curators WHERE id = ?", (cur,)).fetchone()
    old = conn.execute("SELECT curator_id, title FROM courses WHERE id = ?", (cid,)).fetchone()
    if not new or not old:
        return bad("Куратор или курс не найден", 404)
    if old["curator_id"] != cur:
        conn.execute("UPDATE courses SET curator_id = ? WHERE id = ?", (cur, cid))
        log_event(cid, "curator", "Куратор изменён",
                  f"По курсу «{old['title']}» назначен новый куратор - **{curator_short(new)}**")
        conn.commit()
    return jsonify(ok=True)


def _module_payload(d):
    title = (d.get("title") or "").strip()
    try:
        les, tasks, video = (int(d.get(k) or 0) for k in ("lessons", "tasks", "video_hours"))
    except (TypeError, ValueError):
        return None, "Количество уроков, заданий и часов видео — целые числа"
    if not title:
        return None, "Укажите название модуля"
    if not all(0 <= n <= 1000 for n in (les, tasks, video)):
        return None, "Количество уроков, заданий и часов видео — от 0 до 1000"
    return (title[:120], les, tasks, video), None


@app.post("/api/courses/<int:cid>/modules")
@staff
def api_add_module(cid):
    course_row(cid)
    vals, err = _module_payload(request.get_json(silent=True) or {})
    if err:
        return bad(err)
    conn = get_db()
    pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM modules WHERE course_id = ?",
                       (cid,)).fetchone()[0]
    conn.execute("INSERT INTO modules(course_id, position, title, lessons, tasks, video_hours) "
                 "VALUES (?,?,?,?,?,?)", (cid, pos, *vals))
    conn.commit()
    return jsonify(ok=True), 201


@app.patch("/api/modules/<int:mid>")
@staff
def api_update_module(mid):
    course_id_of("modules", mid)
    vals, err = _module_payload(request.get_json(silent=True) or {})
    if err:
        return bad(err)
    conn = get_db()
    conn.execute("UPDATE modules SET title=?, lessons=?, tasks=?, video_hours=? WHERE id=?", (*vals, mid))
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/modules/<int:mid>")
@staff
def api_delete_module(mid):
    cid = course_id_of("modules", mid)
    conn = get_db()
    conn.execute("DELETE FROM modules WHERE id = ?", (mid,))
    for n, (i,) in enumerate(conn.execute(
            "SELECT id FROM modules WHERE course_id = ? ORDER BY position, id", (cid,)).fetchall(), 1):
        conn.execute("UPDATE modules SET position = ? WHERE id = ?", (n, i))
    conn.commit()
    return jsonify(ok=True)


# ───────────────────────── API: задания ─────────────────────────
def _assignment_payload(d, cid):
    title = (d.get("title") or "").strip()
    if not title:
        return None, "Укажите название задания"
    try:
        mx = int(d.get("max_score"))
    except (TypeError, ValueError):
        return None, "Максимальный балл — целое число"
    if not 1 <= mx <= 1000:
        return None, "Максимальный балл — от 1 до 1000"
    due = None
    if (d.get("due") or "").strip():
        try:
            due = parse_date(d["due"]).isoformat()
        except ValueError:
            return None, "Срок сдачи — в формате ДД.ММ.ГГГГ"
    module_id = None
    if str(d.get("module_id") or "").strip():
        try:
            module_id = int(d["module_id"])
        except ValueError:
            return None, "Модуль не найден"
        if not get_db().execute("SELECT 1 FROM modules WHERE id = ? AND course_id = ?", (module_id, cid)).fetchone():
            return None, "Модуль не найден"
    return dict(title=title[:120], module_id=module_id, due=due, max_score=mx,
                description=(d.get("description") or "").strip()[:4000],
                answer_key=(d.get("answer_key") or "").strip()[:200]), None


@app.post("/api/courses/<int:cid>/assignments")
@staff
def api_add_assignment(cid):
    row = course_row(cid)
    v, err = _assignment_payload(request.get_json(silent=True) or {}, cid)
    if err:
        return bad(err)
    conn = get_db()
    pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM assignments WHERE course_id = ?", (cid,)).fetchone()[0]
    conn.execute("INSERT INTO assignments(course_id, module_id, position, title, description, max_score, due_date, answer_key) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (cid, v["module_id"], pos, v["title"], v["description"], v["max_score"], v["due"], v["answer_key"]))
    log_event(cid, "added", f"Новое задание: {v['title']}")
    stats.recalc_course(conn, cid)
    conn.commit()
    return jsonify(ok=True), 201


@app.patch("/api/assignments/<int:aid>")
@staff
def api_update_assignment(aid):
    cid = course_id_of("assignments", aid)
    v, err = _assignment_payload(request.get_json(silent=True) or {}, cid)
    if err:
        return bad(err)
    conn = get_db()
    top = conn.execute("SELECT MAX(score) FROM submissions WHERE assignment_id = ?", (aid,)).fetchone()[0]
    if top is not None and v["max_score"] < top:
        return bad(f"Уже есть оценка {top} — максимальный балл не может быть меньше")
    conn.execute("UPDATE assignments SET title=?, module_id=?, description=?, max_score=?, due_date=?, answer_key=? "
                 "WHERE id=?", (v["title"], v["module_id"], v["description"], v["max_score"], v["due"], v["answer_key"], aid))
    stats.recalc_course(conn, cid)
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/assignments/<int:aid>")
@staff
def api_delete_assignment(aid):
    cid = course_id_of("assignments", aid)
    conn = get_db()
    conn.execute("DELETE FROM assignments WHERE id = ?", (aid,))
    stats.recalc_course(conn, cid)
    conn.commit()
    return jsonify(ok=True, redirect=f"/course/{cid}/tasks")


# ───────────────────────── API: студенты курса ─────────────────────────
def check_credentials(login, password):
    if not LOGIN_RE.match(login or ""):
        return "Логин — от 3 до 32 символов: латиница, цифры, точка, дефис, подчёркивание"
    if not 6 <= len(password or "") <= 128:
        return "Пароль — не короче 6 символов"
    if get_db().execute("SELECT 1 FROM users WHERE login = ?", (login,)).fetchone():
        return "Такой логин уже занят"
    return None


def create_student(full_name, login, password):
    """Новая запись ученика вместе с учётной записью. Возвращает (student_id, user_id)."""
    conn = get_db()
    sid = conn.execute("INSERT INTO students(full_name) VALUES (?)", (full_name,)).lastrowid
    uid = conn.execute("INSERT INTO users(login, password_hash, role, student_id) VALUES (?,?,?,?)",
                       (login, generate_password_hash(password), "student", sid)).lastrowid
    return sid, uid


@app.post("/api/courses/<int:cid>/students")
@staff
def api_add_student(cid):
    row = course_row(cid)
    d = request.get_json(silent=True) or {}
    conn = get_db()
    if row["enrolled"] >= row["capacity"]:
        return bad("На курсе нет свободных мест")
    if str(d.get("student_id") or "").strip():
        try:
            sid = int(d["student_id"])
        except ValueError:
            return bad("Студент не найден")
        st = next((s for s in student_candidates(cid) if s["id"] == sid), None)
        if not st:
            return bad("Этого студента нельзя записать на курс")
        name = st["full_name"]
    else:
        name = " ".join((d.get("full_name") or "").split())
        if len(name.split()) < 2:
            return bad("Введите фамилию и имя, например «Иванов Пётр»")
        err = check_credentials((d.get("login") or "").strip(), d.get("password"))
        if err:
            return bad(err)
        sid, _ = create_student(name, d["login"].strip(), d["password"])
    status = "enrolled" if course_view(row)["status"] == "upcoming" else "learning"
    conn.execute("INSERT INTO enrollments(course_id, student_id, enrolled_at, progress, status) "
                 "VALUES (?,?,?,?,?)", (cid, sid, today().isoformat(), 0, status))
    stats.recalc_progress(conn, cid, sid)
    log_event(cid, "added", f"{short_name(name)} добавлен куратором",
              f"**{short_name(name)}** присоединился(-ась) к курсу «{row['title']}»")
    conn.commit()
    return jsonify(ok=True), 201


@app.patch("/api/enrollments/<int:eid>")
@staff
def api_update_enrollment(eid):
    course_id_of("enrollments", eid)
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in STATUS_LABELS:
        return bad("Неизвестный статус")
    conn = get_db()
    r = conn.execute("SELECT e.course_id, e.student_id, s.full_name FROM enrollments e "
                     "JOIN students s ON s.id = e.student_id WHERE e.id = ?", (eid,)).fetchone()
    if status == "finished":
        conn.execute("UPDATE enrollments SET status = ?, progress = 100 WHERE id = ?", (status, eid))
    else:
        conn.execute("UPDATE enrollments SET status = ? WHERE id = ?", (status, eid))
    log_event(r["course_id"], "status", f"{short_name(r['full_name'])}: {STATUS_LABELS[status].lower()}")
    u = conn.execute("SELECT id FROM users WHERE student_id = ?", (r["student_id"],)).fetchone()
    if u:
        gamify.award(conn, u["id"], r["student_id"], today())
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/enrollments/<int:eid>")
@staff
def api_remove_enrollment(eid):
    course_id_of("enrollments", eid)
    conn = get_db()
    r = conn.execute("SELECT e.course_id, s.full_name, c.title FROM enrollments e "
                     "JOIN students s ON s.id = e.student_id JOIN courses c ON c.id = e.course_id "
                     "WHERE e.id = ?", (eid,)).fetchone()
    conn.execute("DELETE FROM enrollments WHERE id = ?", (eid,))        # учётная запись ученика остаётся
    log_event(r["course_id"], "leave", f"{short_name(r['full_name'])} отчислен",
              f"**{short_name(r['full_name'])}** покинул(-а) курс «{r['title']}»")
    conn.commit()
    return jsonify(ok=True)


# ───────────────────────── API: пользователи и пароли ─────────────────────────
@app.post("/api/users")
@admin_only
def api_create_user():
    d = request.get_json(silent=True) or {}
    role = d.get("role")
    name = " ".join((d.get("full_name") or "").split())
    login, password = (d.get("login") or "").strip(), d.get("password")
    if role not in ROLE_LABELS:
        return bad("Выберите роль")
    parts = name.split()
    if role == "admin" and not name:
        return bad("Укажите имя")
    if role == "student" and len(parts) < 2:
        return bad("Введите фамилию и имя, например «Иванов Пётр»")
    if role == "curator" and len(parts) < 2:
        return bad("Введите фамилию, имя и отчество")
    if len(name) > 80:
        return bad("Слишком длинное имя")
    err = check_credentials(login, password)
    if err:
        return bad(err)
    conn = get_db()
    if role == "student":
        create_student(name, login, password)
    elif role == "curator":
        cid = conn.execute("INSERT INTO curators(last_name, first_name, patronymic) VALUES (?,?,?)",
                           (parts[0], parts[1], " ".join(parts[2:]))).lastrowid
        conn.execute("INSERT INTO users(login, password_hash, role, curator_id) VALUES (?,?,?,?)",
                     (login, generate_password_hash(password), "curator", cid))
    else:
        conn.execute("INSERT INTO users(login, password_hash, role, full_name) VALUES (?,?,?,?)",
                     (login, generate_password_hash(password), "admin", name))
    conn.commit()
    return jsonify(ok=True), 201


@app.delete("/api/users/<int:uid>")
@admin_only
def api_delete_user(uid):
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not u:
        return bad("Пользователь не найден", 404)
    if uid == g.user["id"]:
        return bad("Нельзя удалить самого себя")
    if u["role"] == "curator":
        n = conn.execute("SELECT COUNT(*) FROM courses WHERE curator_id = ?", (u["curator_id"],)).fetchone()[0]
        if n:
            return bad(f"Куратор ведёт курсов: {n}. Сначала назначьте им другого куратора")
        conn.execute("DELETE FROM curators WHERE id = ?", (u["curator_id"],))      # учётная запись удалится каскадом
    elif u["role"] == "student":
        conn.execute("DELETE FROM students WHERE id = ?", (u["student_id"],))      # MySQL: записи, работы, учётная запись
        mongo.delete_user_data(uid)                                                  # MongoDB: входы и достижения
    else:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/users/<int:uid>/password")
@staff
def api_set_password(uid):
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not u:
        return bad("Пользователь не найден", 404)
    if g.user["role"] == "curator":      # куратор меняет пароли только ученикам своих курсов
        ok = u["role"] == "student" and conn.execute(
            "SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id WHERE e.student_id = ? AND c.curator_id = ?",
            (u["student_id"], g.user["curator_id"])).fetchone()
        if not ok:
            abort(403)
    password = (request.get_json(silent=True) or {}).get("password") or ""
    if not 6 <= len(password) <= 128:
        return bad("Пароль — не короче 6 символов")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), uid))
    conn.commit()
    if uid == g.user["id"]:                # свою сессию не обрываем
        session["ph"] = conn.execute("SELECT password_hash FROM users WHERE id = ?", (uid,)).fetchone()[0][-16:]
    return jsonify(ok=True)


db.init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
