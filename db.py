"""MySQL: схема, подключение, тестовые данные. Журналы и входы — в MongoDB (см. mongo.py).

Что где лежит
  MySQL    — curators, students, users, courses, modules, enrollments, assignments, submissions
  MongoDB  — feed, events, logins, achievements
"""
import os
import random
from datetime import date, datetime, timedelta
from decimal import Decimal

import pymysql
from pymysql.constants import CLIENT

import mongo

BASE = os.path.dirname(os.path.abspath(__file__))
MYSQL = dict(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", 3306)),
    user=os.environ.get("MYSQL_USER", "teamonline"),
    password=os.environ.get("MYSQL_PASSWORD", "teamonline"),
    database=os.environ.get("MYSQL_DATABASE", "teamonline"),
)

# Внешние ключи объявлены на уровне таблицы (в MySQL запись REFERENCES прямо у столбца игнорируется).
TABLE_OPTS = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
TABLES = ["submissions", "assignments", "modules", "enrollments", "users", "courses", "students", "curators"]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS curators (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    last_name  VARCHAR(60) NOT NULL,
    first_name VARCHAR(60) NOT NULL,
    patronymic VARCHAR(60) NOT NULL DEFAULT ''
)""",
    """CREATE TABLE IF NOT EXISTS courses (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    title       VARCHAR(120) NOT NULL,
    direction   VARCHAR(80)  NOT NULL DEFAULT '',
    description VARCHAR(1000) NOT NULL DEFAULT '',
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    capacity    INT NOT NULL CHECK (capacity > 0),
    curator_id  INT NOT NULL,
    archived    TINYINT NOT NULL DEFAULT 0,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    rating      DECIMAL(3,1) NULL,                      -- средняя оценка курса
    reviews     INT NOT NULL DEFAULT 0,                 -- число отзывов
    FOREIGN KEY (curator_id) REFERENCES curators(id)
)""",
    """CREATE TABLE IF NOT EXISTS students (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    full_name VARCHAR(120) NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS enrollments (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    course_id   INT NOT NULL,
    student_id  INT NOT NULL,
    enrolled_at DATE NOT NULL,
    progress    INT NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    status      VARCHAR(10) NOT NULL DEFAULT 'enrolled'
                CHECK (status IN ('enrolled','learning','finished','behind','failed')),
    UNIQUE (course_id, student_id),
    FOREIGN KEY (course_id)  REFERENCES courses(id)  ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
)""",
    """CREATE TABLE IF NOT EXISTS modules (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    course_id   INT NOT NULL,
    position    INT NOT NULL,
    title       VARCHAR(120) NOT NULL,
    lessons     INT NOT NULL DEFAULT 0,
    tasks       INT NOT NULL DEFAULT 0,
    video_hours INT NOT NULL DEFAULT 0,
    FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
)""",
    # Учётная запись. Куратор и ученик привязаны к своим записям, у администратора ФИО хранится здесь.
    # Логин уникален без учёта регистра — этого даёт сравнение utf8mb4_unicode_ci.
    """CREATE TABLE IF NOT EXISTS users (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    login         VARCHAR(32)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(10)  NOT NULL CHECK (role IN ('admin','curator','student')),
    full_name     VARCHAR(120) NOT NULL DEFAULT '',
    curator_id    INT NULL UNIQUE,
    student_id    INT NULL UNIQUE,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login    DATETIME NULL,
    FOREIGN KEY (curator_id) REFERENCES curators(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
)""",
    """CREATE TABLE IF NOT EXISTS assignments (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    course_id   INT NOT NULL,
    module_id   INT NULL,
    position    INT NOT NULL DEFAULT 0,
    title       VARCHAR(120) NOT NULL,
    description TEXT NOT NULL,
    max_score   INT NOT NULL DEFAULT 100 CHECK (max_score > 0),
    due_date    DATE NULL,
    answer_key  VARCHAR(200) NOT NULL DEFAULT '',       -- если задан — ответ проверяется автоматически
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
    FOREIGN KEY (module_id) REFERENCES modules(id) ON DELETE SET NULL
)""",
    # Одна строка на пару «задание — ученик»; attempts растёт при каждой отправке
    """CREATE TABLE IF NOT EXISTS submissions (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    assignment_id INT NOT NULL,
    student_id    INT NOT NULL,
    answer        TEXT NOT NULL,
    status        VARCHAR(10) NOT NULL CHECK (status IN ('submitted','graded','returned')),
    score         INT NULL,
    attempts      INT NOT NULL DEFAULT 1,
    feedback      VARCHAR(2000) NOT NULL DEFAULT '',
    submitted_at  DATETIME NOT NULL,
    graded_at     DATETIME NULL,
    UNIQUE (assignment_id, student_id),
    FOREIGN KEY (assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id)    REFERENCES students(id)    ON DELETE CASCADE
)""",
]

# ───────────────────────── подключение ─────────────────────────
class Row(tuple):
    """Строка результата: доступ и по номеру (row[0]), и по имени (row["title"]), как у sqlite3.Row."""

    def __new__(cls, cols, values):
        self = super().__new__(cls, values)
        self._cols = cols
        return self

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._cols.index(key))
            except ValueError:
                raise KeyError(key) from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._cols)

    def get(self, key, default=None):
        return self[key] if key in self._cols else default


def _py(v):
    """MySQL → Python: даты и время — строками ISO (с ними работает вся логика приложения), числа — int/float."""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v) if v.as_tuple().exponent < 0 else int(v)
    return v


class Cursor:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid, self.rowcount = cur.lastrowid, cur.rowcount
        self._cols = [d[0] for d in cur.description] if cur.description else []

    def _row(self, values):
        return Row(self._cols, [_py(v) for v in values])

    def fetchone(self):
        r = self._cur.fetchone()
        return None if r is None else self._row(r)

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Conn:
    """Тонкая обёртка над соединением pymysql: conn.execute(sql, params) с плейсхолдерами «?»."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=None):
        cur = self._raw.cursor()
        if params:
            # pymysql подставляет значения через %s, поэтому «?» меняем, а настоящие «%» экранируем
            cur.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
        else:
            cur.execute(sql)
        return Cursor(cur)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def connect():
    """Новое соединение с MySQL. FOUND_ROWS: rowcount = число найденных строк, а не изменённых."""
    raw = pymysql.connect(charset="utf8mb4", client_flag=CLIENT.FOUND_ROWS, autocommit=False, **MYSQL)
    return Conn(raw)


CURATORS = [
    ("Сидоров", "Алексей", "Викторович"),
    ("Фомин", "Игорь", "Дмитриевич"),
    ("Пучков", "Артём", "Сергеевич"),
    ("Козлова", "Мария", "Андреевна"),
]

COURSES = [
    # title, direction, start, end, capacity, curator_idx, created_days_ago, rating, reviews, description
    ("Python", "Программирование", "2026-11-12", "2026-11-18", 30, 0, 5, 4.8, 42,
     "Базовый курс по Python: синтаксис, функции, работа с данными"),
    ("Основы SQL", "Базы данных", "2026-12-01", "2026-12-16", 25, 1, 12, 4.6, 17,
     "Практический курс по реляционным базам данных: SELECT, JOIN, подзапросы"),
    ("HTML и CSS", "Веб-разработка", "2026-06-01", "2026-06-12", 20, 2, 170, 4.5, 21,
     "Вёрстка с нуля: семантика, формы, Flexbox и адаптивные страницы"),
]

MODULES = {
    "Python": [("Основы языка", 4, 6, 2), ("Функции и структуры данных", 5, 8, 3),
               ("Работа с файлами и итоговый проект", 3, 10, 1)],
    "Основы SQL": [("Введение в SQL", 4, 6, 2), ("Выборка и фильтрация", 5, 8, 3),
                   ("JOIN и подзапросы", 3, 10, 1)],
    "HTML и CSS": [("Основы HTML", 4, 5, 2), ("Стили CSS", 5, 8, 3),
                   ("Адаптивная вёрстка", 3, 6, 2)],
}

M_LAST = ["Иванов", "Петров", "Смирнов", "Попов", "Васильев", "Новиков", "Фёдоров", "Волков",
          "Соловьёв", "Козлов", "Зайцев", "Павлов", "Семёнов", "Голубев", "Виноградов",
          "Богданов", "Воробьёв", "Никитин", "Захаров", "Тарасов", "Белов", "Комаров",
          "Киселёв", "Макаров", "Андреев", "Ковалёв", "Ильин", "Гусев", "Титов", "Кузьмин"]
F_LAST = ["Смирнова", "Попова", "Васильева", "Петрова", "Соколова", "Михайлова", "Новикова",
          "Фёдорова", "Волкова", "Алексеева", "Семёнова", "Егорова", "Павлова", "Козлова",
          "Степанова", "Николаева", "Андреева", "Макарова", "Никитина", "Зайцева"]
M_FIRST = ["Александр", "Дмитрий", "Максим", "Иван", "Артём", "Никита", "Михаил", "Даниил",
           "Егор", "Андрей", "Кирилл", "Илья", "Роман", "Сергей", "Владимир"]
F_FIRST = ["Анна", "Мария", "Екатерина", "Дарья", "Анастасия", "Полина", "Елена", "Ольга",
           "Виктория", "Ксения", "Алиса", "Вера", "Татьяна", "Юлия"]

# Первые пять студентов Python — ровно как в макете
PYTHON_FIXED = [
    ("Сидоров Павел", "2026-08-12", 70, "learning"),
    ("Орлов Дмитрий", "2026-08-12", 55, "learning"),
    ("Лебедев Михаил", "2026-08-11", 40, "learning"),
    ("Кузнецова Анна", "2026-08-10", 100, "finished"),
    ("Морозова Екатерина", "2026-08-09", 15, "behind"),
]


def _rand_date(rng, a, b):
    d0 = datetime.fromisoformat(a)
    span = (datetime.fromisoformat(b) - d0).days
    return (d0 + timedelta(days=rng.randint(0, span))).strftime("%Y-%m-%d")


def _gen_names(rng, n, used):
    out = []
    while len(out) < n:
        if rng.random() < 0.5:
            full = f"{rng.choice(M_LAST)} {rng.choice(M_FIRST)}"
        else:
            full = f"{rng.choice(F_LAST)} {rng.choice(F_FIRST)}"
        key = (full.split()[0], full.split()[1][0])
        if key in used:
            continue
        used.add(key)
        out.append(full)
    return out


def init_db(force=False, demo=None):
    """Создаёт таблицы и индексы; на пустой базе добавляет демо-данные (demo=False или NO_DEMO=1 — только администратор).
    force=True стирает ВСЁ в обеих базах."""
    if demo is None:
        demo = os.environ.get("NO_DEMO") != "1"
    try:
        conn = connect()
    except pymysql.err.OperationalError as e:
        raise RuntimeError(f"Не удалось подключиться к MySQL ({MYSQL['host']}:{MYSQL['port']}): {e}") from e
    try:
        mongo.ping()
    except Exception as e:                      # pymongo.errors.PyMongoError
        raise RuntimeError(f"Не удалось подключиться к MongoDB ({mongo.MONGO_URI}): {e}") from e
    if force:
        conn.execute("SET FOREIGN_KEY_CHECKS = 0")
        for t in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("SET FOREIGN_KEY_CHECKS = 1")
        mongo.reset()
    for stmt in SCHEMA:
        conn.execute(stmt + TABLE_OPTS)
    mongo.ensure_indexes()
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():     # пустая база — первый запуск
        mongo.reset()                                                  # на случай остатков от прошлых запусков
        if demo:
            _seed(conn)
            _seed_portal(conn)
        else:
            _create_admin(conn)
    conn.commit()
    conn.close()


def _seed(conn):
    rng = random.Random(2026)
    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    ts = lambda **kw: (now - timedelta(**kw)).strftime(fmt)

    for last, first, pat in CURATORS:
        conn.execute("INSERT INTO curators(last_name, first_name, patronymic) VALUES (?,?,?)",
                     (last, first, pat))

    ids = {}
    for title, direction, start, end, cap, cur_idx, age, rating, reviews, descr in COURSES:
        cur = conn.execute(
            "INSERT INTO courses(title, direction, description, start_date, end_date, capacity, "
            "curator_id, created_at, rating, reviews) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title, direction, descr, start, end, cap, cur_idx + 1, ts(days=age), rating, reviews))
        ids[title] = cur.lastrowid
        for n, (mt, les, tasks, video) in enumerate(MODULES[title], 1):
            conn.execute("INSERT INTO modules(course_id, position, title, lessons, tasks, video_hours)"
                         " VALUES (?,?,?,?,?,?)", (cur.lastrowid, n, mt, les, tasks, video))

    used = {("Сидоров", "П"), ("Орлов", "Д"), ("Лебедев", "М"), ("Кузнецова", "А"),
            ("Морозова", "Е")}

    def add(course, full, when, progress, status):
        sid = conn.execute("INSERT INTO students(full_name) VALUES (?)", (full,)).lastrowid
        conn.execute("INSERT INTO enrollments(course_id, student_id, enrolled_at, progress, status)"
                     " VALUES (?,?,?,?,?)", (ids[course], sid, when, progress, status))

    for full, when, prog, st in PYTHON_FIXED:
        add("Python", full, when, prog, st)
    rest = _gen_names(rng, 20, used)
    kinds = ["finished"] * 7 + ["behind"] * 3 + ["learning"] * 10
    rng.shuffle(kinds)
    for full, st in zip(rest, kinds):
        prog = {"finished": 100, "behind": rng.randint(8, 30)}.get(st, rng.randint(35, 90))
        add("Python", full, _rand_date(rng, "2026-07-20", "2026-08-08"), prog, st)

    for full in _gen_names(rng, 14, used):
        add("Основы SQL", full, _rand_date(rng, "2026-08-13", "2026-11-10"), 0, "enrolled")

    for i, full in enumerate(_gen_names(rng, 18, used)):
        if i < 16:
            add("HTML и CSS", full, _rand_date(rng, "2026-05-05", "2026-05-31"), 100, "finished")
        else:
            add("HTML и CSS", full, _rand_date(rng, "2026-05-05", "2026-05-31"),
                rng.randint(40, 85), "failed")

    # события курса «Python» (правая колонка страницы курса)
    for kind, text, when in [
        ("join", "Сидоров П. присоединился", ts(minutes=2)),
        ("join", "Орлов Д. присоединился", ts(hours=2)),
        ("added", "добавлен куратором", ts(hours=3)),
        ("curator", "Куратор изменён", ts(days=2)),
        ("created", "Курс создан", ts(days=5)),
    ]:
        mongo.add_event(ids["Python"], kind, text, when)
    for title, age in (("Основы SQL", 12), ("HTML и CSS", 170)):
        mongo.add_event(ids[title], "created", "Курс создан", ts(days=age))

    # общая лента на главной
    yesterday = (now - timedelta(days=1)).replace(hour=19, minute=40, second=0).strftime(fmt)
    for kind, text, when, title in [
        ("join", "**Белов Я.С.** присоединился(-ась) к курсу «Основы SQL»", ts(minutes=2), "Основы SQL"),
        ("leave", "**Кузнецов Д.И.** покинул(-а) курс «Python»", ts(minutes=10), "Python"),
        ("curator", "По курсу «Основы SQL» назначен новый куратор - **Фомин И.Д.**", ts(hours=6), "Основы SQL"),
        ("join", "**Лебедева И.С.** присоединился(-ась) к курсу «Основы SQL»", yesterday, "Основы SQL"),
        ("join", "**Говард Л.** присоединился(-ась) к курсу «Основы SQL»", ts(days=2), "Основы SQL"),
        ("leave", "**Митрофанов М.Ю.** покинул курс «Python»", ts(days=3), "Python"),
    ]:
        mongo.add_feed(ids[title], kind, text, when)


# ───────────────────────── личные кабинеты: демо-данные ─────────────────────────
DEMO_PASSWORDS = {"admin": "admin123", "curator": "curator123", "student": "student123"}

TRANSLIT = dict(zip("абвгдежзийклмнопрстуфхцчшщыэюя", "a b v g d e zh z i i k l m n o p r s t u f kh ts ch sh sch y e yu ya".split()))
TRANSLIT.update({"ё": "e", "ъ": "", "ь": ""})

# курс -> задания: (позиция модуля, название, условие, макс. балл, правильный ответ, срок: дней от начала курса)
ASSIGNMENTS = {
    "Python": [
        (1, "Типы данных", "Какой тип данных вернёт выражение 7 / 2 в Python 3? Ответьте одним словом — названием типа.", 10, "float", 1),
        (1, "Ввод и вывод", "Напишите программу, которая спрашивает имя пользователя и выводит приветствие «Привет, <имя>!». Пришлите код.", 100, "", 2),
        (1, "FizzBuzz", "Выведите числа от 1 до 100. Вместо кратных трём напечатайте Fizz, кратных пяти — Buzz, кратных обоим — FizzBuzz.", 100, "", 3),
        (2, "Срезы строк", "Что выведет print('python'[1:4])? Ответьте тем, что появится на экране.", 10, "yth", 3),
        (2, "Функции", "Напишите функцию is_prime(n), которая возвращает True для простых чисел. Добавьте 3 примера вызова.", 100, "", 4),
        (2, "Словари и множества", "Посчитайте, сколько раз каждое слово встречается в строке. Результат — словарь слово → количество.", 100, "", 5),
        (3, "Чтение файла", "Прочитайте текстовый файл и выведите количество строк и слов. Обработайте случай, когда файла нет.", 100, "", 6),
        (3, "Итоговый проект", "Соберите небольшую консольную программу «Записная книжка»: добавление, поиск и сохранение записей в файл.", 200, "", 6),
    ],
    "Основы SQL": [
        (1, "Первый запрос", "Какая команда выбирает все столбцы таблицы users? Ответьте запросом без точки с запятой.", 10, "select * from users", 3),
        (1, "Создание таблицы", "Опишите таблицу books (id, title, year) и напишите CREATE TABLE для неё.", 100, "", 4),
        (2, "Фильтрация", "Выберите из таблицы orders заказы дороже 1000 за 2026 год, отсортируйте по сумме по убыванию.", 100, "", 6),
        (2, "Агрегаты", "Как называется функция, считающая число строк? Ответьте одним словом.", 10, "count", 7),
        (3, "JOIN", "Выведите имена клиентов и суммы их заказов, включая клиентов без заказов.", 100, "", 10),
        (3, "Подзапросы", "Найдите клиентов, чьи заказы больше среднего по всем заказам. Решите подзапросом.", 100, "", 12),
    ],
    "HTML и CSS": [
        (1, "Семантика", "Каким тегом оформляют основное содержимое страницы? Ответьте тегом без скобок.", 10, "main", 2),
        (1, "Форма обратной связи", "Сверстайте форму: имя, e-mail, сообщение и кнопка. Пришлите HTML.", 100, "", 3),
        (2, "Flexbox", "Какое свойство включает flex-контейнер? Ответьте в виде «свойство: значение».", 10, "display: flex", 6),
        (2, "Карточки товаров", "Сверстайте сетку из 6 карточек товаров на Flexbox или Grid.", 100, "", 8),
        (3, "Адаптивность", "Сделайте страницу из прошлого задания адаптивной: 3 колонки на десктопе, 1 на телефоне.", 100, "", 10),
        (3, "Лендинг", "Итоговая работа: лендинг из пяти блоков. Пришлите ссылку или код.", 200, "", 11),
    ],
}
# вероятность, что очередная попытка по заданию не будет засчитана (чем выше — тем сложнее)
HARDNESS = {
    "Python": [0.10, 0.15, 0.35, 0.25, 0.55, 0.60, 0.50, 0.70],
    "Основы SQL": [0.1, 0.2, 0.3, 0.2, 0.5, 0.6],
    "HTML и CSS": [0.1, 0.2, 0.2, 0.35, 0.45, 0.5],
}
FEEDBACK_RETURN = ["Почти. Не обработан пустой ввод — добавьте проверку и отправьте снова.",
                   "Решение работает не на всех примерах. Проверьте граничные значения.",
                   "Код нужно разбить на функции и добавить комментарии."]
FEEDBACK_GOOD = ["Хорошая работа!", "Всё верно, аккуратно оформлено.", "Решение рабочее. Можно короче, но к сути претензий нет.", ""]


def translit(text):
    return "".join(TRANSLIT.get(ch, ch) for ch in text.lower() if ch.isalpha() or ch in ".-")


def _today():
    iso = os.environ.get("TODAY")
    return datetime.fromisoformat(iso).date() if iso else datetime.now().date()


def _unique_login(conn, base):
    login, n = base, 1
    while conn.execute("SELECT 1 FROM users WHERE login = ?", (login,)).fetchone():
        n += 1
        login = f"{base}{n}"
    return login


def _create_admin(conn, password_hash=None):
    """Единственная учётная запись при чистом старте. Логин/пароль: ADMIN_LOGIN / ADMIN_PASSWORD (по умолчанию admin / admin123)."""
    from werkzeug.security import generate_password_hash
    conn.execute("INSERT INTO users(login, password_hash, role, full_name) VALUES (?,?,?,?)",
                 (os.environ.get("ADMIN_LOGIN", "admin"),
                  password_hash or generate_password_hash(os.environ.get("ADMIN_PASSWORD", DEMO_PASSWORDS["admin"])),
                  "admin", "Администратор"))


def _seed_portal(conn):
    """Учётные записи (администратор, кураторы, ученики) и демонстрационные задания с работами."""
    from werkzeug.security import generate_password_hash
    import gamify
    import stats

    hashes = {k: generate_password_hash(v) for k, v in DEMO_PASSWORDS.items()}
    _create_admin(conn, hashes["admin"] if not os.environ.get("ADMIN_PASSWORD") else None)

    for r in conn.execute("SELECT * FROM curators ORDER BY id").fetchall():
        login = _unique_login(conn, translit(r["last_name"]))
        conn.execute("INSERT INTO users(login, password_hash, role, curator_id) VALUES (?,?,?,?)",
                     (login, hashes["curator"], "curator", r["id"]))

    for r in conn.execute("SELECT * FROM students ORDER BY id").fetchall():
        parts = r["full_name"].split()
        base = translit(parts[0]) + (("." + translit(parts[1][0])) if len(parts) > 1 else "")
        conn.execute("INSERT INTO users(login, password_hash, role, student_id) VALUES (?,?,?,?)",
                     (_unique_login(conn, base), hashes["student"], "student", r["id"]))

    if conn.execute("SELECT 1 FROM assignments LIMIT 1").fetchone():
        return
    rng = random.Random(7)
    today = _today()
    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"

    for course in conn.execute("SELECT * FROM courses ORDER BY id").fetchall():
        if course["title"] not in ASSIGNMENTS:
            continue
        mods = {r["position"]: r["id"] for r in conn.execute("SELECT id, position FROM modules WHERE course_id = ?", (course["id"],))}
        start = datetime.fromisoformat(course["start_date"]).date()
        end = datetime.fromisoformat(course["end_date"]).date()
        asg = []
        for n, (mpos, title, descr, mx, key, due) in enumerate(ASSIGNMENTS[course["title"]], 1):
            due_day = min(start + timedelta(days=due), end)
            aid = conn.execute(
                "INSERT INTO assignments(course_id, module_id, position, title, description, max_score, due_date, answer_key) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (course["id"], mods.get(mpos), n, title, descr, mx, due_day.isoformat(), key)).lastrowid
            asg.append(dict(id=aid, max=mx, auto=bool(key)))
        hard = HARDNESS[course["title"]]

        win_a, win_b = max(start, today - timedelta(days=13)), min(end, today)
        if win_a > win_b:
            win_a = win_b = start
        span = max((win_b - win_a).days, 0)

        for e in conn.execute("SELECT * FROM enrollments WHERE course_id = ?", (course["id"],)).fetchall():
            if e["status"] == "enrolled" or start > today:
                continue
            n = len(asg)
            if e["status"] == "finished":
                reached = n
            elif e["status"] == "failed":
                reached = n - rng.choice([1, 2])
            elif e["status"] == "behind":
                reached = rng.choice([1, 2])
            else:
                reached = max(1, min(n - 1, round(e["progress"] / 100 * n)))
            for i in range(reached):
                a, last = asg[i], i == reached - 1
                tries = 1
                while rng.random() < hard[i] and tries < 4:
                    tries += 1
                day = win_a + timedelta(days=round(span * (i + rng.random() * 0.8) / max(n, 1)))
                when = min(datetime(day.year, day.month, day.day, rng.randint(9, 21), rng.randint(0, 59)), now)
                graded_at = (when + timedelta(hours=rng.randint(1, 20))).strftime(fmt)
                if graded_at > now.strftime(fmt):
                    graded_at = now.strftime(fmt)
                stuck = last and reached > 1 and e["status"] in ("learning", "behind", "failed") and rng.random() < 0.55
                if stuck and not a["auto"]:
                    state = rng.choice(["submitted", "submitted", "returned"])
                    conn.execute(
                        "INSERT INTO submissions(assignment_id, student_id, answer, status, score, attempts, feedback, submitted_at, graded_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (a["id"], e["student_id"], "def solve():\n    ...", state, None, tries,
                         rng.choice(FEEDBACK_RETURN) if state == "returned" else "", when.strftime(fmt),
                         graded_at if state == "returned" else None))
                    continue
                if stuck and a["auto"]:
                    score, answer, feedback = 0, "не знаю", ""
                elif a["auto"]:
                    score, answer, feedback = a["max"], ASSIGNMENTS[course["title"]][i][4], ""
                else:
                    score = a["max"] if rng.random() < 0.25 else rng.randint(round(a["max"] * 0.62), a["max"])
                    answer, feedback = "def solve():\n    return 42", rng.choice(FEEDBACK_GOOD)
                conn.execute(
                    "INSERT INTO submissions(assignment_id, student_id, answer, status, score, attempts, feedback, submitted_at, graded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (a["id"], e["student_id"], answer, "graded", score, tries, feedback, when.strftime(fmt), graded_at))
        stats.recalc_course(conn, course["id"])

    # история входов: у кого-то серия, у кого-то редкие заходы, кто-то пропал
    for u in conn.execute("SELECT id FROM users WHERE role = 'student' ORDER BY id").fetchall():
        kind = rng.choices(["streak", "sporadic", "absent"], [0.45, 0.35, 0.2])[0]
        if kind == "streak":
            length = rng.randint(2, 14)
            end_back = 0 if rng.random() < 0.4 else 1
            days = [today - timedelta(days=end_back + k) for k in range(length)]
        elif kind == "sporadic":
            days = [today - timedelta(days=k) for k in range(14) if rng.random() < 0.4]
        else:
            days = [today - timedelta(days=k) for k in range(rng.randint(4, 10), 14) if rng.random() < 0.6]
        for d in days:
            mongo.record_login(u["id"], d.isoformat())
        if days:
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?",
                         (datetime.combine(max(days), datetime.min.time()).replace(hour=rng.randint(9, 21)).strftime(fmt), u["id"]))
    for u in conn.execute("SELECT id, student_id FROM users WHERE role = 'student'").fetchall():
        gamify.award(conn, u["id"], u["student_id"], today)
    mongo.mark_seen()


if __name__ == "__main__":
    import sys
    clean = "--empty" in sys.argv
    if "--force" in sys.argv:
        init_db(force=True, demo=not clean)
        print("MySQL и MongoDB пересозданы: " + ("пусто, есть только администратор." if clean else "добавлены демо-данные."))
    else:
        init_db(demo=not clean)
        print("Базы проверены. Стереть всё и начать заново: python db.py --force [--empty]")
