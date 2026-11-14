/* Личные кабинеты: диалоги заданий, проверка работ, пароли, отправка ответа учеником */
(() => {
  const { api, toast, $ } = window.TO;

  const openDialog = (id) => { const d = document.getElementById(id); d.showModal(); return d; };
  const showError = (form, msg) => {
    const e = $('.form-error', form);
    if (e) { e.textContent = msg; e.hidden = false; } else toast(msg);
  };
  const hideError = (form) => { const e = $('.form-error', form); if (e) e.hidden = true; };

  document.addEventListener('click', async (e) => {
    const t = e.target;

    /* ── задания: добавить / изменить / удалить ── */
    const add = t.closest('[data-assign-add]');
    const edit = t.closest('[data-assign-edit]');
    if (add || edit) {
      const f = $('#assign-form');
      const a = edit ? JSON.parse(edit.dataset.a) : {};
      f.dataset.api = add ? add.dataset.assignAdd : edit.dataset.assignEdit;
      f.dataset.method = add ? 'POST' : 'PATCH';
      $('#assign-heading').textContent = add ? 'Новое задание' : 'Изменить задание';
      f.title.value = a.title || '';
      f.module_id.value = a.module_id || '';
      f.due.value = a.due || '';
      f.description.value = a.description || '';
      f.max_score.value = a.max_score || 100;
      f.answer_key.value = a.answer_key || '';
      const del = $('#assign-delete');
      del.hidden = !edit;
      del.dataset.url = edit ? edit.dataset.assignEdit : '';
      hideError(f);
      openDialog('assign-modal');
      f.title.focus();
      return;
    }
    if (t.closest('#assign-delete')) {
      if (!confirm('Удалить задание вместе со всеми сдачами учеников?')) return;
      try {
        const data = await api($('#assign-delete').dataset.url, 'DELETE');
        location.href = data.redirect || location.href;
      } catch (err) { toast(err.message); }
      return;
    }

    /* ── проверка работы ── */
    const grade = t.closest('[data-grade]');
    if (grade) {
      const r = JSON.parse(grade.dataset.r);
      const f = $('#grade-form');
      f.dataset.sub = grade.dataset.grade;
      $('#grade-heading').textContent = r.name;
      $('#grade-answer').textContent = r.answer || '—';
      $('#grade-attempts').textContent = r.attempts ? `(попытка ${r.attempts})` : '';
      $('#grade-score').value = r.score === null || r.score === undefined ? '' : r.score;
      $('#grade-feedback').value = r.feedback || '';
      hideError(f);
      openDialog('grade-modal');
      return;
    }

    /* ── сброс пароля ── */
    const pwd = t.closest('[data-pwd]');
    if (pwd) {
      const f = $('#pwd-form');
      f.dataset.api = pwd.dataset.pwd;
      $('#pwd-who').textContent = pwd.dataset.who || '';
      f.password.value = '';
      hideError(f);
      openDialog('pwd-modal');
      f.password.focus();
    }
  });

  /* проверка: два действия одной формой */
  const gradeForm = $('#grade-form');
  if (gradeForm) {
    gradeForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const action = e.submitter ? e.submitter.dataset.action : 'grade';
      hideError(gradeForm);
      try {
        await api(`/api/submissions/${gradeForm.dataset.sub}/grade`, 'POST', {
          action,
          score: $('#grade-score').value,
          feedback: $('#grade-feedback').value,
        });
        location.reload();
      } catch (err) { showError(gradeForm, err.message); }
    });
  }

  /* запись студента: новый или уже существующий */
  const pick = $('#student-pick');
  if (pick) {
    const box = $('.new-fields', pick.form);
    const sync = () => {
      const isNew = pick.value === '';
      box.hidden = !isNew;
      box.querySelectorAll('input').forEach((i) => { i.disabled = !isNew; });
    };
    pick.addEventListener('change', sync);
    sync();
  }

  /* поиск по таблице пользователей */
  document.querySelectorAll('[data-filter]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const q = inp.value.trim().toLowerCase();
      document.querySelectorAll(`${inp.dataset.filter} .pt-row`).forEach((r) => {
        r.hidden = q !== '' && !(r.dataset.search || '').includes(q);
      });
    });
  });

  /* ученик: отправка ответа */
  const sf = $('#submit-form');
  if (sf) {
    sf.addEventListener('submit', async (e) => {
      e.preventDefault();
      hideError(sf);
      const btn = $('button[type=submit]', sf);
      btn.disabled = true;
      try {
        const data = await api(`/api/assignments/${sf.dataset.aid}/submit`, 'POST', { answer: sf.answer.value });
        const extra = (data.new_achievements || []).length ? ` · Новое достижение: ${data.new_achievements.join(', ')}` : '';
        toast(data.message + extra);
        setTimeout(() => location.reload(), data.status === 'returned' ? 1400 : 900);
      } catch (err) { showError(sf, err.message); btn.disabled = false; }
    });
  }
})();
