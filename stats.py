"""Статистика курса: прогресс, активность учеников и сложность заданий.

Сложность задания считается по тому, как его реально выполнили ученики (см. difficulty_index).
"""
from datetime import date, datetime, timedelta

import mongo

PASS_PCT = 60          # задание считается выполненным, если набрано не меньше 60 % баллов
MIN_OUTCOMES = 3       # меньше трёх проверенных попыток — сложность не оцениваем
INACTIVE_DAYS = 3      # столько дней без входа — ученик «в зоне внимания»

PASSED_SQL = "s.status = 'graded' AND s.score * 100 >= %d * a.max_score" % PASS_PCT


def recalc_progress(conn, course_id, student_id):
    """Прогресс = доля выполненных заданий курса. Статус 'finished' ставится при 100 %."""
    total = conn.execute("SELECT COUNT(*) FROM assignments WHERE course_id = ?", (course_id,)).fetchone()[0]
    enr = conn.execute("SELECT id, status FROM enrollments WHERE course_id = ? AND student_id = ?",
                       (course_id, student_id)).fetchone()
    if not total or not enr or enr["status"] == "finished":
        return
    done = conn.execute(
        "SELECT COUNT(*) FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
        f"WHERE a.course_id = ? AND s.student_id = ? AND {PASSED_SQL}", (course_id, student_id)).fetchone()[0]
    progress = round(done * 100 / total)
    status = enr["status"]
    if progress >= 100:
        status = "finished"
    elif status == "enrolled" and conn.execute(
            "SELECT 1 FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
            "WHERE a.course_id = ? AND s.student_id = ?", (course_id, student_id)).fetchone():
        status = "learning"
    conn.execute("UPDATE enrollments SET progress = ?, status = ? WHERE id = ?", (progress, status, enr["id"]))


def recalc_course(conn, course_id):
    for (sid,) in conn.execute("SELECT student_id FROM enrollments WHERE course_id = ?", (course_id,)).fetchall():
        recalc_progress(conn, course_id, sid)


# ───────────────────────── сложность заданий ─────────────────────────
def difficulty_index(avg_pct, fail_pct, avg_attempts, completion):
    """0–100, чем больше — тем сложнее.

    45 % веса — насколько мало баллов набирают в среднем, 25 % — доля неудачных попыток,
    15 % — сколько попыток нужно (1 попытка = 0, 3 и больше = максимум),
    15 % — какая доля учеников так и не выполнила задание.
    """
    attempts_part = min(max(avg_attempts - 1, 0) * 50, 100)
    return round(0.45 * (100 - avg_pct) + 0.25 * fail_pct + 0.15 * attempts_part + 0.15 * (100 - completion))


def difficulty_label(idx):
    if idx is None:
        return None, "Мало данных"
    if idx < 16:
        return "easy", "Лёгкое"
    if idx < 27:
        return "mid", "Среднее"
    return "hard", "Сложное"


def assignment_stats(conn, course_id):
    enrolled = conn.execute("SELECT COUNT(*) FROM enrollments WHERE course_id = ?", (course_id,)).fetchone()[0]
    asg = conn.execute(
        "SELECT a.*, m.title AS module_title FROM assignments a LEFT JOIN modules m ON m.id = a.module_id "
        "WHERE a.course_id = ? ORDER BY COALESCE(m.position, 9999), a.position, a.id", (course_id,)).fetchall()
    subs = {}
    for r in conn.execute(
            "SELECT s.assignment_id, s.status, s.score, s.attempts FROM submissions s "
            "JOIN assignments a ON a.id = s.assignment_id "
            "JOIN enrollments e ON e.student_id = s.student_id AND e.course_id = a.course_id "
            "WHERE a.course_id = ?", (course_id,)):
        subs.setdefault(r["assignment_id"], []).append(r)

    out = []
    for a in asg:
        rows = subs.get(a["id"], [])
        mx = a["max_score"]
        graded = [r for r in rows if r["status"] == "graded"]
        returned = [r for r in rows if r["status"] == "returned"]
        pending = [r for r in rows if r["status"] == "submitted"]
        passed = [r for r in graded if r["score"] * 100 >= PASS_PCT * mx]
        pcts = [r["score"] * 100 / mx for r in graded]
        outcome_pcts = pcts + [0] * len(returned)       # «на доработке» = пока не выполнено
        d = dict(a)
        d.update(
            enrolled=enrolled, n_sub=len(rows), graded=len(graded), pending=len(pending),
            returned=len(returned), passed=len(passed),
            avg_pct=round(sum(pcts) / len(pcts)) if pcts else None,
            avg_attempts=round(sum(r["attempts"] for r in rows) / len(rows), 1) if rows else None,
            completion=round(len(passed) * 100 / enrolled) if enrolled else 0,
            fail_pct=None, index=None)
        if len(outcome_pcts) >= MIN_OUTCOMES:
            fail = sum(1 for p in outcome_pcts if p < PASS_PCT) * 100 / len(outcome_pcts)
            d["fail_pct"] = round(fail)
            d["index"] = difficulty_index(sum(outcome_pcts) / len(outcome_pcts), fail,
                                          d["avg_attempts"] or 1, d["completion"])
        d["level"], d["label"] = difficulty_label(d["index"])
        out.append(d)
    return out


# ───────────────────────── ученики и активность ─────────────────────────
def _age(day, today):
    if day is None:
        return None
    return (today - date.fromisoformat(day)).days


def age_label(days):
    if days is None:
        return "не заходил(а)"
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад"


def student_stats(conn, course_id, today):
    total = conn.execute("SELECT COUNT(*) FROM assignments WHERE course_id = ?", (course_id,)).fetchone()[0]
    since = (today - timedelta(days=13)).isoformat()
    enrolled = conn.execute(
        "SELECT e.id AS eid, e.student_id, e.progress, e.status, s.full_name, u.id AS user_id "
        "FROM enrollments e JOIN students s ON s.id = e.student_id "
        "LEFT JOIN users u ON u.student_id = s.id WHERE e.course_id = ? ORDER BY s.full_name", (course_id,)).fetchall()
    logins = mongo.login_summary([e["user_id"] for e in enrolled if e["user_id"]], since)   # входы — в MongoDB
    rows = []
    for e in enrolled:
        sub = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN s.status='graded' AND s.score*100 >= %d*a.max_score THEN 1 ELSE 0 END) AS done, "
            "SUM(CASE WHEN s.status='submitted' THEN 1 ELSE 0 END) AS pending, "
            "AVG(CASE WHEN s.status='graded' THEN s.score*100.0/a.max_score END) AS avg_pct, "
            "MAX(s.submitted_at) AS last_sub "
            "FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
            "WHERE a.course_id = ? AND s.student_id = ?" % PASS_PCT, (course_id, e["student_id"])).fetchone()
        last_login, active14 = logins.get(e["user_id"], (None, 0))
        last_sub = sub["last_sub"][:10] if sub["last_sub"] else None
        last = max([d for d in (last_login, last_sub) if d], default=None)
        idle = _age(last, today)
        rows.append(dict(
            eid=e["eid"], student_id=e["student_id"], name=e["full_name"], progress=e["progress"],
            status=e["status"], total=total, done=sub["done"] or 0, submitted=sub["n"] or 0,
            pending=sub["pending"] or 0, avg_pct=round(sub["avg_pct"]) if sub["avg_pct"] is not None else None,
            last=last, idle=idle, last_label=age_label(idle), active14=active14,
            attention=(e["status"] not in ("finished", "failed") and (idle is None or idle >= INACTIVE_DAYS)),
        ))
    return rows


def activity_days(conn, course_id, start, end):
    """По дням между start и end: сколько учеников были активны (вход или сдача) и сколько сдач."""
    a, b = start.isoformat(), end.isoformat()
    students = conn.execute(
        "SELECT e.student_id, u.id AS user_id FROM enrollments e LEFT JOIN users u ON u.student_id = e.student_id "
        "WHERE e.course_id = ?", (course_id,)).fetchall()
    by_user = {r["user_id"]: r["student_id"] for r in students if r["user_id"]}
    active, subs = {}, {}
    for uid, day in mongo.logins_between(list(by_user), a, b):               # входы — из MongoDB
        active.setdefault(day, set()).add(by_user[uid])
    for r in conn.execute(                                                    # сдачи — из MySQL
            "SELECT s.submitted_at, s.student_id FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
            "JOIN enrollments e ON e.student_id = s.student_id AND e.course_id = a.course_id "
            "WHERE a.course_id = ? AND s.submitted_at >= ? AND s.submitted_at < ?",
            (course_id, a + " 00:00:00", (end + timedelta(days=1)).isoformat() + " 00:00:00")):
        day = r["submitted_at"][:10]
        active.setdefault(day, set()).add(r["student_id"])
        subs[day] = subs.get(day, 0) + 1
    out, d = [], start
    while d <= end:
        iso = d.isoformat()
        out.append(dict(date=d, iso=iso, active=len(active.get(iso, ())), subs=subs.get(iso, 0)))
        d += timedelta(days=1)
    return out
