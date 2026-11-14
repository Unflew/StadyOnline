"""MongoDB: журналы и «накопительные» данные.

Здесь лежит то, что часто дописывается и не нуждается в связях и транзакциях:
  • feed          — общая лента событий на главной странице
  • events        — журнал событий каждого курса
  • logins        — дни, когда ученик заходил в кабинет (из них считается серия)
  • achievements  — выданные ученикам достижения

Всё остальное (пользователи, курсы, задания, работы) — в MySQL, см. db.py.
Идентификаторы user_id / course_id — это id строк из MySQL.
"""
import os

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.environ.get("MONGO_DB", "teamonline")

_db = None
NO_ID = {"_id": 0}


def database():
    """Подключение создаётся один раз на процесс (MongoClient сам ведёт пул соединений)."""
    global _db
    if _db is None:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=False)
        _db = client[MONGO_DB]
        ensure_indexes(_db)
    return _db


def ensure_indexes(d=None):
    d = d if d is not None else database()
    d.events.create_index([("course_id", 1), ("created_at", -1)])
    d.feed.create_index([("created_at", -1)])
    d.feed.create_index([("course_id", 1)])
    d.logins.create_index([("user_id", 1), ("day", 1)], unique=True)       # один вход в день
    d.achievements.create_index([("user_id", 1), ("code", 1)], unique=True)  # достижение выдаётся один раз


def ping():
    database().command("ping")


def reset():
    """Очищает все коллекции (используется при пересоздании демо-данных)."""
    d = database()
    for name in ("feed", "events", "logins", "achievements"):
        d[name].delete_many({})


# ───────────────────────── лента и события курса ─────────────────────────
def add_event(course_id, kind, text, created_at):
    database().events.insert_one(dict(course_id=course_id, kind=kind, text=text, created_at=created_at))


def add_feed(course_id, kind, text, created_at):
    database().feed.insert_one(dict(course_id=course_id, kind=kind, text=text, created_at=created_at))


def feed_list(course_ids=None, limit=6):
    """Последние записи ленты. course_ids=None — все; иначе только по этим курсам."""
    flt = {} if course_ids is None else {"course_id": {"$in": list(course_ids)}}
    return list(database().feed.find(flt, NO_ID).sort([("created_at", -1), ("_id", -1)]).limit(limit))


def course_events(course_id, limit=None):
    cur = database().events.find({"course_id": course_id}, NO_ID).sort([("created_at", -1), ("_id", -1)])
    return list(cur.limit(limit) if limit else cur)


def count_events(course_id):
    return database().events.count_documents({"course_id": course_id})


def detach_course(course_id):
    """Курс удалён: журнал курса стираем, записи ленты оставляем как историю (без привязки к курсу)."""
    d = database()
    d.events.delete_many({"course_id": course_id})
    d.feed.update_many({"course_id": course_id}, {"$set": {"course_id": None}})


# ───────────────────────── входы ─────────────────────────
def record_login(user_id, day):
    """Засчитывает вход за день (строка YYYY-MM-DD). True — если за этот день вход первый."""
    try:
        database().logins.insert_one(dict(user_id=user_id, day=day))
        return True
    except DuplicateKeyError:
        return False


def login_days(user_id):
    return {r["day"] for r in database().logins.find({"user_id": user_id}, NO_ID)}


def logins_between(user_ids, first_day, last_day):
    """[(user_id, day)] за период включительно."""
    if not user_ids:
        return []
    cur = database().logins.find(
        {"user_id": {"$in": list(user_ids)}, "day": {"$gte": first_day, "$lte": last_day}}, NO_ID)
    return [(r["user_id"], r["day"]) for r in cur]


def login_summary(user_ids, since):
    """{user_id: (последний день входа, число дней входа начиная с since)}."""
    out = {}
    if not user_ids:
        return out
    for r in database().logins.find({"user_id": {"$in": list(user_ids)}}, NO_ID):
        last, n = out.get(r["user_id"], (None, 0))
        out[r["user_id"]] = (max(last, r["day"]) if last else r["day"], n + (r["day"] >= since))
    return out


# ───────────────────────── достижения ─────────────────────────
def achievements_of(user_id):
    return {r["code"]: r for r in database().achievements.find({"user_id": user_id}, NO_ID)}


def give_achievement(user_id, code, earned_at):
    """True, если достижение выдано сейчас (а не было выдано раньше)."""
    try:
        database().achievements.insert_one(dict(user_id=user_id, code=code, earned_at=earned_at, seen=0))
        return True
    except DuplicateKeyError:
        return False


def mark_seen(user_id=None):
    database().achievements.update_many({} if user_id is None else {"user_id": user_id}, {"$set": {"seen": 1}})


def delete_user_data(user_id):
    d = database()
    d.logins.delete_many({"user_id": user_id})
    d.achievements.delete_many({"user_id": user_id})
