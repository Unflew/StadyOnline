(() => {
  const $ = (s, r = document) => r.querySelector(s);

  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove('show'), 2800);
  }

  const csrf = () => (document.querySelector('meta[name=csrf-token]') || {}).content || '';

  async function api(url, method, body) {
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) { location.href = '/login'; throw new Error(data.error || 'Войдите заново'); }
    if (!res.ok) throw new Error(data.error || 'Что-то пошло не так. Попробуйте ещё раз.');
    return data;
  }

  window.TO = { api, toast, $ };

  const closeMenus = (except) =>
    document.querySelectorAll('.menu-wrap.open').forEach((m) => m !== except && m.classList.remove('open'));

  let showAll = false;
  const card = $('#students');
  async function loadStudents() {
    if (!card) return;
    const q = $('#student-search').value;
    const res = await fetch(`/course/${card.dataset.course}/students?q=${encodeURIComponent(q)}&all=${showAll ? 1 : 0}`);
    if (res.ok) $('#students-body').innerHTML = await res.text();
  }

  document.addEventListener('click', async (e) => {
    const t = e.target;

    const opener = t.closest('[data-open]');
    if (opener) {
      const dlg = document.getElementById(opener.dataset.open);
      dlg.showModal();
      const first = dlg.querySelector('input, select');
      if (first) first.focus();
      return;
    }
    if (t.closest('[data-close]')) return t.closest('dialog').close();
    if (t.tagName === 'DIALOG') return t.close();

    const dots = t.closest('.dots-btn');
    if (dots && dots.closest('.menu-wrap')) {
      const wrap = dots.closest('.menu-wrap');
      closeMenus(wrap);
      wrap.classList.toggle('open');
      return;
    }
    closeMenus();

    if (t.closest('[data-soon]')) { e.preventDefault(); return toast('Этот раздел пока недоступен'); }

    const del = t.closest('[data-delete]');
    if (del) {
      if (!confirm(del.dataset.confirm || 'Удалить?')) return;
      try {
        const data = await api(del.dataset.delete, 'DELETE');
        if (data.redirect) location.href = data.redirect; else location.reload();
      } catch (err) { toast(err.message); }
      return;
    }

    const post = t.closest('[data-post]');
    if (post) {
      if (post.dataset.confirm && !confirm(post.dataset.confirm)) return;
      try {
        const data = await api(post.dataset.post, 'POST');
        if (data.redirect) location.href = data.redirect; else location.reload();
      } catch (err) { toast(err.message); }
      return;
    }

    const st = t.closest('[data-status-url]');
    if (st) {
      try { await api(st.dataset.statusUrl, 'PATCH', { status: st.dataset.status }); location.reload(); }
      catch (err) { toast(err.message); }
      return;
    }

    const nav = t.closest('.cal-nav');
    if (nav) {
      const res = await fetch(`/calendar?y=${nav.dataset.y}&m=${nav.dataset.m}`);
      if (res.ok) $('#calendar-body').innerHTML = await res.text();
      return;
    }

    if (t.closest('[data-toggle-all]')) { showAll = !showAll; loadStudents(); return; }

    /* модули программы */
    const mform = $('#module-form');
    const madd = t.closest('[data-module-add]');
    const medit = t.closest('[data-module-edit]');
    if (madd || medit) {
      const src = madd || medit;
      mform.dataset.api = src.dataset.moduleAdd || src.dataset.moduleEdit;
      mform.dataset.method = madd ? 'POST' : 'PATCH';
      $('#module-heading').textContent = madd ? 'Новый модуль' : 'Изменить модуль';
      mform.title.value = medit ? medit.dataset.title : '';
      mform.lessons.value = medit ? medit.dataset.lessons : 0;
      mform.tasks.value = medit ? medit.dataset.tasks : 0;
      mform.video_hours.value = medit ? medit.dataset.video : 0;
      const delBtn = $('#module-delete');
      delBtn.hidden = !medit;
      delBtn.dataset.url = medit ? medit.dataset.moduleEdit : '';
      $('.form-error', mform).hidden = true;
      $('#module-modal').showModal();
      mform.title.focus();
      return;
    }
    if (t.closest('#module-delete')) {
      if (!confirm('Удалить модуль?')) return;
      try { await api($('#module-delete').dataset.url, 'DELETE'); location.reload(); }
      catch (err) { toast(err.message); }
    }
  });
  document.addEventListener('keydown', (e) => e.key === 'Escape' && closeMenus());

  /* формы: модалки и «Основная информация» */
  document.querySelectorAll('form[data-api], #module-form').forEach((form) => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const err = $('.form-error', form);
      if (err) err.hidden = true;
      try {
        const data = await api(form.dataset.api, form.dataset.method || 'POST', Object.fromEntries(new FormData(form)));
        if (form.dataset.toast) { toast(form.dataset.toast); setTimeout(() => location.reload(), 500); return; }
        if (data.redirect) location.href = data.redirect; else location.reload();
      } catch (ex) {
        if (err) { err.textContent = ex.message; err.hidden = false; } else toast(ex.message);
      }
    });
  });

  /* фильтры списка курсов */
  const filters = $('#filters');
  if (filters) {
    filters.querySelectorAll('[data-auto]').forEach((s) => s.addEventListener('change', () => filters.submit()));
    const q = $('input[name=q]', filters);
    let timer;
    q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => filters.submit(), 450); });
    if (new URLSearchParams(location.search).get('q')) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  }

  /* поиск студентов */
  const search = $('#student-search');
  if (search) {
    let timer;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => { showAll = search.value.trim() !== ''; loadStudents(); }, 250);
    });
  }
})();
