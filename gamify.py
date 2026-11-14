"""Ежедневные входы (серия) и достижения учеников."""
from datetime import date, datetime, timedelta

import mongo
from stats import PASS_PCT

# kind — по какому показателю считается прогресс; target — сколько нужно набрать
ACHIEVEMENTS = [
    dict(code="first_login", title="Первый вход", desc="Зайти в личный кабинет", icon="bolt", kind="logins", target=1),
    dict(code="streak_3", title="Три дня подряд", desc="Заходить в кабинет 3 дня подряд", icon="flame", kind="streak", target=3),
    dict(code="streak_7", title="Неделя без пропусков", desc="Заходить 7 дней подряд", icon="flame", kind="streak", target=7),
    dict(code="streak_14", title="Две недели подряд", desc="Заходить 14 дней подряд", icon="flame", kind="streak", target=14),
    dict(code="streak_30", title="Месяц дисциплины", desc="Заходить 30 дней подряд", icon="flame", kind="streak", target=30),
    dict(code="tasks_1", title="Первое задание", desc="Выполнить первое задание", icon="check", kind="tasks", target=1),
    dict(code="tasks_5", title="Пять заданий", desc="Выполнить 5 заданий", icon="check", kind="tasks", target=5),
    dict(code="tasks_10", title="Десять заданий", desc="Выполнить 10 заданий", icon="check", kind="tasks", target=10),
    dict(code="perfect", title="Без единой ошибки", desc="Получить максимальный балл за задание", icon="star", kind="perfect", target=1),
    dict(code="retry", title="Упорство", desc="Выполнить задание после доработки", icon="redo", kind="retry", target=1),
    dict(code="course_done", title="Курс пройден", desc="Завершить курс", icon="flag", kind="courses", target=1),
]
BY_CODE = {a["code"]: a for a in ACHIEVEMENTS}


def record_login(user_id, today):
    """Засчитывает сегодняшний вход (коллекция logins в MongoDB). True — если за сегодня это первый вход."""
    return mongo.record_login(user_id, today.isoformat())


def login_days(user_id):
    return {date.fromisoformat(d) for d in mongo.login_days(user_id)}


def streaks(days, today):
    """(текущая серия, лучшая серия). Серия не прерывается, если сегодня ещё не заходили, а вчера заходили."""
    best, run, prev = 0, 0, None
    for d in sorted(days):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        best, prev = max(best, run), d
    cur, d = 0, today if today in days else today - timedelta(days=1)
    while d in days:
        cur += 1
        d -= timedelta(days=1)
    return cur, best


def metrics(conn, user_id, student_id, today):
    days = login_days(user_id)
    cur, best = streaks(days, today)
    q = ("SELECT COUNT(*) FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
         "WHERE s.student_id = ? AND s.status = 'graded' AND s.score * 100 >= %d * a.max_score" % PASS_PCT)
    return dict(
        streak=cur, best=best, logins=len(days), days=days,
        tasks=conn.execute(q, (student_id,)).fetchone()[0],
        perfect=conn.execute(q + " AND s.score >= a.max_score", (student_id,)).fetchone()[0],
        retry=conn.execute(q + " AND s.attempts >= 2", (student_id,)).fetchone()[0],
        courses=conn.execute("SELECT COUNT(*) FROM enrollments WHERE student_id = ? AND status = 'finished'",
                             (student_id,)).fetchone()[0])


def _earned_value(a, m):
    return m["best"] if a["kind"] == "streak" else m[a["kind"]]


def award(conn, user_id, student_id, today):
    """Выдаёт заслуженные, но ещё не выданные достижения (хранятся в MongoDB). Возвращает список новых."""
    have = mongo.achievements_of(user_id)
    m = metrics(conn, user_id, student_id, today)
    new = []
    for a in ACHIEVEMENTS:
        if a["code"] not in have and _earned_value(a, m) >= a["target"]:
            if mongo.give_achievement(user_id, a["code"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")):
                new.append(a)
    return new


def overview(conn, user_id, student_id, today):
    """Все достижения с состоянием: получено / прогресс."""
    got = mongo.achievements_of(user_id)
    m = metrics(conn, user_id, student_id, today)
    out = []
    for a in ACHIEVEMENTS:
        row = got.get(a["code"])
        cur = a["target"] if row else min(m["streak"] if a["kind"] == "streak" else m[a["kind"]], a["target"])
        out.append(dict(a, earned=bool(row), earned_at=row["earned_at"][:10] if row else None,
                        new=bool(row and not row["seen"]), cur=cur, pct=round(cur * 100 / a["target"])))
    return out, m


def week_strip(days, today):
    """Последние 7 дней: заходил / не заходил."""
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    return [dict(label=names[(today - timedelta(days=i)).weekday()], on=(today - timedelta(days=i)) in days,
                 today=i == 0) for i in range(6, -1, -1)]
