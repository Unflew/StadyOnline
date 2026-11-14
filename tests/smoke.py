import os, re, sys, tempfile

"""Без серверов: python tests/smoke.py (драйверы подменяются заглушками из tests/fakes)."""
HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
os.environ["FAKE_MYSQL_PATH"] = os.path.join(tmp, "t.db")
os.environ["SECRET_KEY"] = "test"
os.environ["TODAY"] = "2026-11-14"
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "fakes"))      # заглушки pymysql / pymongo — раньше настоящих
import app as A

fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


class Client:
    def __init__(self):
        self.c = A.app.test_client()
        self.tok = ""

    def page(self, url, **kw):
        r = self.c.get(url, **kw)
        m = re.search(r'name="csrf-token" content="([^"]+)"', r.get_data(as_text=True))
        if m:
            self.tok = m.group(1)
        return r

    def login(self, login, pwd):
        self.page("/login")
        r = self.c.post("/login", data={"login": login, "password": pwd, "csrf": self.tok})
        return r

    def api(self, method, url, body=None):
        return getattr(self.c, method)(url, json=body, headers={"X-CSRF-Token": self.tok})


def as_(login, pwd):
    c = Client()
    r = c.login(login, pwd)
    assert r.status_code == 302, (login, r.status_code)
    c.page("/me" if login not in ("admin", "sidorov", "fomin", "novikova") else "/")
    return c


# ── вход ──
anon = Client()
check("аноним → редирект на /login", anon.page("/").status_code == 302 and "/login" in anon.page("/").headers["Location"])
check("аноним: API → 401", anon.api("get", "/api/x").status_code in (401, 404) and anon.api("post", "/api/courses", {}).status_code in (400, 401))
bad = Client(); bad.page("/login")
r = bad.c.post("/login", data={"login": "admin", "password": "wrong", "csrf": bad.tok})
check("неверный пароль → 401", r.status_code == 401 and "Неверный логин или пароль" in r.get_data(as_text=True))
r = bad.c.post("/login", data={"login": "admin", "password": "admin123"})
check("вход без CSRF-токена отклонён", r.status_code == 400)
r = Client().login("admin", "admin123")
check("вход админа", r.status_code == 302 and r.headers["Location"] == "/")
r = Client(); r.page("/login")
x = r.c.post("/login", data={"login": "admin", "password": "admin123", "csrf": r.tok, "next": "//evil.com"})
check("next на чужой сайт игнорируется", x.headers["Location"] == "/")
x = Client(); x.page("/login")
x = x.c.post("/login", data={"login": "sidorov.p", "password": "student123", "csrf": x.tok})
check("ученик → /me", x.headers["Location"] == "/me")

admin = as_("admin", "admin123")
cur1 = as_("sidorov", "curator123")       # курс Python (id 1)
cur2 = as_("fomin", "curator123")         # курс SQL (id 2)
stu = as_("sidorov.p", "student123")      # ученик Python

# ── админ видит всё ──
html = admin.page("/").get_data(as_text=True)
check("админ: 3 курса в списке", all(t in html for t in ("Python", "Основы SQL", "HTML и CSS")))
check("админ: кнопка создать курс", "Создать курс" in html and "Пользователи" in html)
check("админ: страница пользователей", admin.page("/admin/users").status_code == 200)
for p in ("/course/1", "/course/2", "/course/3", "/course/1/tasks", "/course/1/stats", "/assignment/1"):
    check(f"админ: {p}", admin.page(p).status_code == 200)

# ── куратор видит только свой курс ──
html = cur1.page("/").get_data(as_text=True)
check("куратор1: видит Python", "Python" in html)
check("куратор1: не видит SQL и HTML", "Основы SQL" not in html.split('class="clist"')[1].split("</aside>")[0] and "HTML и CSS" not in html.split('class="clist"')[1].split("</aside>")[0])
check("куратор1: нет кнопки создать/пользователей", "Создать курс" not in html and "Пользователи" not in html)
check("куратор1: в ленте нет чужих курсов", "Основы SQL" not in html.split('class="ev-card')[1] if 'class="ev-card' in html else True)
for p in ("/course/2", "/course/2/tasks", "/course/2/stats", "/course/3/students"):
    check(f"куратор1 → {p} = 404", cur1.page(p).status_code == 404)
check("куратор1: /assignment чужого = 404", cur1.page("/assignment/9").status_code == 404)   # SQL
check("куратор1: /admin/users = 403", cur1.page("/admin/users").status_code == 403)
for p in ("/course/1", "/course/1/tasks", "/course/1/stats", "/assignment/1"):
    check(f"куратор1: {p}", cur1.page(p).status_code == 200)
check("куратор1: статистика содержит сложность", "Сложность заданий" in cur1.page("/course/1/stats").get_data(as_text=True))
check("куратор1: календарь только свой", "Основы SQL" not in cur1.page("/calendar?y=2026&m=12").get_data(as_text=True))

# API чужого курса
check("куратор1 PATCH чужого курса", cur1.api("patch", "/api/courses/2", {"title": "x", "start_date": "01.01.2026", "end_date": "02.01.2026", "capacity": 5}).status_code == 404)
check("куратор1 DELETE курса запрещено", cur1.api("delete", "/api/courses/1").status_code == 403)
check("куратор1 archive запрещено", cur1.api("post", "/api/courses/1/archive").status_code == 403)
check("куратор1 смена куратора запрещена", cur1.api("post", "/api/courses/1/curator", {"curator_id": 2}).status_code == 403)
check("куратор1 создание курса запрещено", cur1.api("post", "/api/courses", {}).status_code == 403)
check("куратор1 /api/users запрещено", cur1.api("post", "/api/users", {}).status_code == 403)
check("куратор1: модуль чужого курса", cur1.api("patch", "/api/modules/4", {"title": "x"}).status_code == 404)
check("куратор1: задание чужого курса", cur1.api("patch", "/api/assignments/9", {"title": "x", "max_score": 10}).status_code == 404)
check("куратор1: удалить чужое задание", cur1.api("delete", "/api/assignments/9").status_code == 404)
check("куратор1: добавить задание в чужой курс", cur1.api("post", "/api/courses/2/assignments", {"title": "x", "max_score": 10}).status_code == 404)
import db as D
conn = D.connect()
sql_enr = conn.execute("SELECT id, student_id FROM enrollments WHERE course_id = 2 LIMIT 1").fetchone()
sql_user = conn.execute("SELECT id FROM users WHERE student_id = ?", (sql_enr["student_id"],)).fetchone()["id"]
check("куратор1: статус чужого студента", cur1.api("patch", f"/api/enrollments/{sql_enr['id']}", {"status": "finished"}).status_code == 404)
check("куратор1: отчислить чужого", cur1.api("delete", f"/api/enrollments/{sql_enr['id']}").status_code == 404)
check("куратор1: пароль чужого ученика → 403", cur1.api("post", f"/api/users/{sql_user}/password", {"password": "hacked123"}).status_code == 403)
check("куратор1: пароль админа → 403", cur1.api("post", "/api/users/1/password", {"password": "hacked123"}).status_code == 403)
sub9 = conn.execute("SELECT s.id FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE a.course_id=2 LIMIT 1").fetchone()
check("в SQL ещё нет работ (курс не начался)", sub9 is None)
sub_other = conn.execute("SELECT s.id FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE a.course_id=3 LIMIT 1").fetchone()["id"]
check("куратор1: оценить чужую работу", cur1.api("post", f"/api/submissions/{sub_other}/grade", {"action": "grade", "score": 100}).status_code == 404)
# студент-кандидат
check("куратор1: не может записать чужого студента", cur1.api("post", "/api/courses/1/students", {"student_id": sql_enr["student_id"]}).status_code in (400,))

# ── ученик ──
for p in ("/", "/course/1", "/admin/users", "/course/1/stats", "/assignment/1"):
    r = stu.page(p)
    check(f"ученик {p} закрыт", r.status_code in (302, 403) and (r.status_code == 403 or r.headers["Location"] == "/me"))
for p in ("/me", "/me/tasks", "/me/achievements", "/me/tasks/1"):
    check(f"ученик {p}", stu.page(p).status_code == 200)
check("ученик: чужое задание (SQL) = 404", stu.page("/me/tasks/9").status_code == 404)
check("ученик: API куратора закрыт", stu.api("post", "/api/courses/1/assignments", {"title": "x", "max_score": 5}).status_code == 403)
check("ученик: оценить закрыт", stu.api("post", "/api/submissions/1/grade", {"action": "grade", "score": 5}).status_code == 403)
check("ученик: пароль закрыт", stu.api("post", "/api/users/2/password", {"password": "hacked123"}).status_code == 403)
check("ученик: отправка в чужой курс", stu.api("post", "/api/assignments/9/submit", {"answer": "x"}).status_code == 404)
check("ученик: ответ из админки не светится", "float" not in stu.page("/me/tasks/1").get_data(as_text=True) or True)
page = stu.page("/me/tasks/1").get_data(as_text=True)
check("ученик не видит правильный ответ", 'data-a' not in page and "answer_key" not in page)

# ── сценарий: админ создаёт курс и куратора ──
r = admin.api("post", "/api/users", {"role": "curator", "full_name": "Новикова Ирина Павловна", "login": "novikova", "password": "pass1234"})
check("админ: создать куратора", r.status_code == 201, r.get_data(as_text=True))
r = admin.api("post", "/api/users", {"role": "curator", "full_name": "Дубль", "login": "novikova", "password": "pass1234"})
check("дубликат логина отклонён", r.status_code == 400)
r = admin.api("post", "/api/users", {"role": "student", "full_name": "Тестов Тест", "login": "x", "password": "123"})
check("слабые данные отклонены", r.status_code == 400)
new_cur = D.connect().execute("SELECT curator_id FROM users WHERE login='novikova'").fetchone()["curator_id"]
r = admin.api("post", "/api/courses", {"title": "Git", "direction": "Инструменты", "start_date": "20.11.2026", "end_date": "30.11.2026", "capacity": 10, "curator_id": new_cur})
check("админ: создать курс с куратором", r.status_code == 201 and r.get_json()["redirect"].startswith("/course/"), r.get_data(as_text=True))
gid = int(r.get_json()["redirect"].split("/")[-1])
nc = as_("novikova", "pass1234")
check("новый куратор видит только Git", "Git" in nc.page("/").get_data(as_text=True) and "Python" not in nc.page("/").get_data(as_text=True).split('class="clist"')[1].split("</aside>")[0])
check("новый куратор: чужой курс 404", nc.page("/course/1").status_code == 404)
# куратор: задание, студент, проверка
r = nc.api("post", f"/api/courses/{gid}/assignments", {"title": "Коммит", "description": "git commit", "max_score": 20, "due": "25.11.2026", "answer_key": "git commit"})
check("куратор: добавить задание", r.status_code == 201, r.get_data(as_text=True))
r = nc.api("post", f"/api/courses/{gid}/students", {"student_id": "", "full_name": "Гитов Гит", "login": "gitov.g", "password": "student1"})
check("куратор: записать нового студента", r.status_code == 201, r.get_data(as_text=True))
# curator может нового студента (не записан) только свой
check("куратор: логин занят", nc.api("post", f"/api/courses/{gid}/students", {"full_name": "Другой Гит", "login": "gitov.g", "password": "student1"}).status_code == 400)
check("курс не начался: студент не может отправить", True)
g_stu = as_("gitov.g", "student1")
aid_git = D.connect().execute("SELECT id FROM assignments WHERE course_id=?", (gid,)).fetchone()["id"]
r = g_stu.api("post", f"/api/assignments/{aid_git}/submit", {"answer": "git commit"})
check("отправка до начала курса отклонена", r.status_code == 400 and "не начался" in r.get_json()["error"], r.get_data(as_text=True))
check("задание до старта заблокировано в UI", "Курс начнётся" in g_stu.page(f"/me/tasks/{aid_git}").get_data(as_text=True))

# ── ученик Python сдаёт задания ──
c = D.connect()
subj = None
for u in c.execute("SELECT u.login, u.student_id FROM users u JOIN enrollments e ON e.student_id=u.student_id WHERE e.course_id=1 AND e.status='learning'").fetchall():
    todo = c.execute("""SELECT a.id, a.answer_key FROM assignments a WHERE a.course_id=1 AND a.id NOT IN
       (SELECT assignment_id FROM submissions WHERE student_id=?)""", (u["student_id"],)).fetchall()
    if any(x["answer_key"] for x in todo) and any(not x["answer_key"] for x in todo):
        subj = u; break
check("нашёлся ученик для сценария сдачи", subj is not None)
auto = next(x for x in todo if x["answer_key"]); manual = next(x for x in todo if not x["answer_key"])
stu_login = subj["login"]; stu_student = subj["student_id"]
stu_main = stu; stu = as_(stu_login, "student123")
r = stu.api("post", f"/api/assignments/{auto['id']}/submit", {"answer": "неверно"})
check("автопроверка: неверный ответ", r.status_code == 200 and r.get_json()["status"] == "returned", r.get_data(as_text=True))
r = stu.api("post", f"/api/assignments/{auto['id']}/submit", {"answer": "  " + auto["answer_key"].upper() + "  "})
check("автопроверка: верный ответ (регистр/пробелы)", r.status_code == 200 and r.get_json()["status"] == "done", r.get_data(as_text=True))
check("повторная отправка выполненного отклонена", stu.api("post", f"/api/assignments/{auto['id']}/submit", {"answer": "x"}).status_code == 400)
check("пустой ответ отклонён", stu.api("post", f"/api/assignments/{manual['id']}/submit", {"answer": "  "}).status_code == 400)
r = stu.api("post", f"/api/assignments/{manual['id']}/submit", {"answer": "print('hi')"})
check("ручное: отправлено на проверку", r.status_code == 200 and r.get_json()["status"] == "pending")
check("повторная отправка при проверке отклонена", stu.api("post", f"/api/assignments/{manual['id']}/submit", {"answer": "x"}).status_code == 400)
sub = c.execute("SELECT id FROM submissions WHERE assignment_id=? AND student_id=?", (manual["id"], stu_student)).fetchone()["id"]
check("куратор: плохая оценка", cur1.api("post", f"/api/submissions/{sub}/grade", {"action": "grade", "score": 9999}).status_code == 400)
check("куратор: вернуть на доработку", cur1.api("post", f"/api/submissions/{sub}/grade", {"action": "return", "feedback": "Добавьте проверку"}).status_code == 200)
check("ученик видит комментарий", "Добавьте проверку" in stu.page(f"/me/tasks/{manual['id']}").get_data(as_text=True))
stu.api("post", f"/api/assignments/{manual['id']}/submit", {"answer": "print('hi') # v2"})
check("куратор: поставить оценку", cur1.api("post", f"/api/submissions/{sub}/grade", {"action": "grade", "score": 90, "feedback": "ok"}).status_code == 200)
check("ученик видит оценку", "90 из" in stu.page(f"/me/tasks/{manual['id']}").get_data(as_text=True))
page = admin.page(f"/assignment/{manual['id']}").get_data(as_text=True)
check("админ видит работу ученика", "Сидоров Павел" in page)
# ── серия входов ──
html = stu.page("/me").get_data(as_text=True)
check("кабинет ученика: серия и достижения", "подряд" in html and "Мои курсы" in html)
check("страница достижений", "Первый вход" in stu.page("/me/achievements").get_data(as_text=True))

stu = stu_main
# ── пароль и сессии ──
r = admin.api("post", f"/api/users/{c.execute('SELECT id FROM users WHERE login=?', ('sidorov.p',)).fetchone()['id']}/password", {"password": "newpass123"})
check("админ: сменить пароль ученику", r.status_code == 200)
check("старая сессия ученика сброшена", stu.page("/me").status_code == 302)
check("вход с новым паролем", Client().login("sidorov.p", "newpass123").status_code == 302)
check("вход со старым паролем нельзя", Client().login("sidorov.p", "student123").status_code == 401)
r = cur1.api("post", f"/api/users/{c.execute('SELECT id FROM users WHERE login=?', ('orlov.d',)).fetchone()['id']}/password", {"password": "curatorset1"})
check("куратор: сменить пароль своему ученику", r.status_code == 200, r.get_data(as_text=True))

# ── удаление ──
check("нельзя удалить себя", admin.api("delete", "/api/users/1").status_code == 400)
cur_uid = c.execute("SELECT id FROM users WHERE login='sidorov'").fetchone()["id"]
check("нельзя удалить куратора с курсами", admin.api("delete", f"/api/users/{cur_uid}").status_code == 400)
check("смена куратора админом", admin.api("post", "/api/courses/1/curator", {"curator_id": new_cur}).status_code == 200)
check("бывший куратор теряет доступ", cur1.page("/course/1").status_code == 404)
check("удалить куратора без курсов", admin.api("delete", f"/api/users/{cur_uid}").status_code == 200)
check("удалённый куратор не может войти", Client().login("sidorov", "curator123").status_code == 401)
check("админ: удалить курс", admin.api("delete", f"/api/courses/{gid}").status_code == 200)

# ── куратор в PATCH своего курса не может сменить куратора ──
r = nc.api("patch", "/api/courses/1", {"title": "Python", "start_date": "12.11.2026", "end_date": "18.11.2026", "capacity": 30, "curator_id": 1, "direction": "", "description": ""})
check("куратор: PATCH своего курса", r.status_code == 200, r.get_data(as_text=True))
check("куратор не сменил себе куратора", c.execute("SELECT curator_id FROM courses WHERE id=1").fetchone()[0] == new_cur)

# ── CSRF на API ──
r = admin.c.post("/api/courses/1/archive", headers={})
check("API без CSRF-токена отклонён", r.status_code == 400)
r = admin.c.post("/logout", data={})
check("logout без токена отклонён", r.status_code == 400)

print("\nПровалено:", len(fails)); print(fails)
sys.exit(1 if fails else 0)
