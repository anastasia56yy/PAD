/* Sign in / sign up, history panel and notifications (all calls go through the API Gateway). */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const TOKEN_KEY = "utm.token";

  const store = {
    get() { try { return JSON.parse(localStorage.getItem(TOKEN_KEY)); } catch { return null; } },
    set(v) { try { localStorage.setItem(TOKEN_KEY, JSON.stringify(v)); } catch { /* private mode */ } },
    clear() { try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } },
  };
  let session = store.get();
  if (session && session.expires_at < Date.now()) { store.clear(); session = null; }

  const els = {
    signin: $("#btn-signin"), account: $("#account"), avatar: $("#btn-avatar"),
    history: $("#btn-history"), bell: $("#btn-bell"), badge: $("#bell-badge"),
    panel: $("#panel"), panelTitle: $("#panel-title"), panelBody: $("#panel-body"), panelClose: $("#panel-close"),
    dialog: $("#auth-dialog"), form: $("#auth-form"), title: $("#auth-title"), sub: $("#auth-sub"),
    error: $("#auth-error"), submit: $("#auth-submit"), cancel: $("#auth-cancel"),
  };

  const authHeaders = () => (session ? { Authorization: `Bearer ${session.token}` } : {});

  async function api(path, options = {}) {
    const res = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) } });
    if (res.status === 401 && session) { signOut(); throw new Error("Сессия истекла, войдите снова"); }
    if (res.status === 204) return null;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const details = data.error?.details?.map(d => `${d.field}: ${d.problem}`).join("; ");
      throw new Error(details || data.error?.message || `Ошибка ${res.status}`);
    }
    return data;
  }

  /* ---------- header state ---------- */
  function render() {
    const signedIn = Boolean(session);
    els.signin.hidden = signedIn;
    els.account.hidden = !signedIn;
    if (signedIn) {
      els.avatar.textContent = (session.name || session.email || "?").trim()[0].toUpperCase();
      els.avatar.title = `${session.name || session.email} — нажмите, чтобы выйти`;
      refreshBadge();
    }
  }

  function signOut() {
    store.clear();
    session = null;
    closePanel();
    render();
  }

  /* ---------- auth dialog ---------- */
  let mode = "login";
  function setMode(next) {
    mode = next;
    els.dialog.classList.toggle("is-register", mode === "register");
    els.dialog.querySelectorAll(".seg").forEach(b => {
      const active = b.dataset.mode === mode;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-selected", String(active));
    });
    els.title.textContent = mode === "login" ? "С возвращением" : "Создать аккаунт";
    els.sub.textContent = mode === "login"
      ? "Войдите, чтобы сохранять историю вопросов и получать уведомления."
      : "Аккаунт хранит историю ваших вопросов и уведомления.";
    els.submit.textContent = mode === "login" ? "Войти" : "Зарегистрироваться";
    els.form.password.autocomplete = mode === "login" ? "current-password" : "new-password";
    els.error.hidden = true;
  }

  function openDialog() {
    setMode("login");
    els.form.reset();
    els.dialog.showModal();
    els.form.email.focus();
  }

  els.signin.addEventListener("click", openDialog);
  els.cancel.addEventListener("click", () => els.dialog.close());
  els.dialog.querySelectorAll(".seg").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

  els.form.addEventListener("submit", async e => {
    e.preventDefault();
    const f = els.form;
    const payload = { email: f.email.value.trim(), password: f.password.value };
    if (mode === "register") Object.assign(payload, { full_name: f.full_name.value.trim(), faculty: f.faculty.value.trim() || null, language: f.language.value });
    els.submit.disabled = true;
    els.error.hidden = true;
    try {
      const data = await api(`/api/auth/${mode}`, { method: "POST", body: JSON.stringify(payload) });
      session = { token: data.access_token, user_id: data.user_id, role: data.role, email: payload.email,
                  name: payload.full_name || null, expires_at: Date.now() + data.expires_in * 1000 };
      if (!session.name) {  // login: fetch the profile name (created by user-service from an event)
        try { session.name = (await api("/api/users/me")).full_name; } catch { /* profile may lag */ }
      }
      store.set(session);
      els.dialog.close();
      render();
    } catch (err) {
      els.error.textContent = err.message;
      els.error.hidden = false;
    } finally {
      els.submit.disabled = false;
    }
  });

  els.avatar.addEventListener("click", () => {
    if (confirm(`Выйти из аккаунта ${session.email}?`)) signOut();
  });

  /* ---------- side panel ---------- */
  function openPanel(title) {
    els.panelTitle.textContent = title;
    els.panelBody.innerHTML = '<div class="skeleton"><span></span><span></span><span></span></div>';
    els.panel.classList.add("is-open");
    els.panel.setAttribute("aria-hidden", "false");
  }
  function closePanel() {
    els.panel.classList.remove("is-open");
    els.panel.setAttribute("aria-hidden", "true");
  }
  els.panelClose.addEventListener("click", closePanel);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closePanel(); });

  const fmt = iso => new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  function emptyState(text) {
    els.panelBody.innerHTML = `<p class="panel-empty"></p>`;
    $(".panel-empty", els.panelBody).textContent = text;
  }

  async function showHistory() {
    openPanel("История");
    try {
      const [page, stats] = await Promise.all([api("/api/history?limit=50"), api("/api/history/stats")]);
      if (!page.items.length) return emptyState("Здесь появятся ваши вопросы.");
      els.panelBody.innerHTML = `<p class="panel-stats"></p><ul class="list"></ul>`;
      $(".panel-stats", els.panelBody).textContent =
        `${stats.questions} вопросов · без ответа: ${stats.refused} · в среднем ${stats.avg_latency_s} с`;
      const list = $(".list", els.panelBody);
      page.items.forEach(item => {
        const li = document.createElement("li");
        li.innerHTML = `<button type="button" class="list-item"><span class="li-title"></span><span class="li-meta"></span></button>`;
        $(".li-title", li).textContent = item.question;
        $(".li-meta", li).textContent = `${fmt(item.asked_at)} · ${item.refused ? "нет ответа" : `${item.sources.length} источн.`}`;
        $("button", li).addEventListener("click", () => {
          closePanel();
          window.showAnswer(item.question, {
            lang: "", answer: item.answer, refused: item.refused, confidence: item.confidence, related: [],
            sources: item.sources.map((s, i) => ({ n: i + 1, title: s.title, url: s.url, section: "", site: "", text: "" })),
            stats: { retrieved: "—", filtered: "—", context: item.sources.length, total_s: item.latency_s, translated: false },
          });
        });
        list.appendChild(li);
      });
    } catch (err) { emptyState(err.message); }
  }

  async function showNotifications() {
    openPanel("Уведомления");
    try {
      const items = await api("/api/notifications");
      if (!items.length) return emptyState("Новых уведомлений нет.");
      els.panelBody.innerHTML = `<ul class="list"></ul>`;
      const list = $(".list", els.panelBody);
      items.forEach(n => {
        const li = document.createElement("li");
        li.className = "note" + (n.read ? "" : " is-unread");
        li.innerHTML = `<p class="li-title"></p><p class="li-body"></p><p class="li-meta"></p>`;
        $(".li-title", li).textContent = n.title;
        $(".li-body", li).textContent = n.body;
        $(".li-meta", li).textContent = fmt(n.created_at);
        list.appendChild(li);
      });
      await Promise.all(items.filter(n => !n.read).map(n => api(`/api/notifications/${n.id}/read`, { method: "POST" })));
      refreshBadge();
    } catch (err) { emptyState(err.message); }
  }

  async function refreshBadge() {
    if (!session) return;
    try {
      const unread = await api("/api/notifications?unread_only=true");
      els.badge.textContent = unread.length;
      els.badge.hidden = unread.length === 0;
    } catch { els.badge.hidden = true; }
  }

  els.history.addEventListener("click", showHistory);
  els.bell.addEventListener("click", showNotifications);

  window.Account = {
    authHeaders,
    // history and notifications are written asynchronously from RabbitMQ events: refresh a moment later
    afterAnswer: () => session && setTimeout(refreshBadge, 1500),
  };
  render();
})();
