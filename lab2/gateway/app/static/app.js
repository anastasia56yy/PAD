/* Libraria storefront: every call goes through the API Gateway (/api/...). */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = cents => `${(cents / 100).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} MDL`;
const when = iso => new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const LANG = { ro: "Română", en: "English", ru: "Русский" };
const STATUS = {
  pending: ["Проверяем наличие", "info"], awaiting_payment: ["Ожидает оплаты", "info"],
  paid: ["Оплачен", "ok"], confirmed: ["Подтверждён · оплата при получении", "ok"],
  shipped: ["Отправлен", "ok"], delivered: ["Доставлен", "ok"], cancelled: ["Отменён", "bad"],
};

/* ---------- session ---------- */
const store = {
  get() { try { return JSON.parse(localStorage.getItem("libraria.session")); } catch { return null; } },
  set(v) { try { localStorage.setItem("libraria.session", JSON.stringify(v)); } catch { /* private mode */ } },
  clear() { try { localStorage.removeItem("libraria.session"); } catch { /* ignore */ } },
};
let session = store.get();
if (session && session.expires_at < Date.now()) { store.clear(); session = null; }

async function api(path, { method = "GET", body, headers = {} } = {}) {
  const res = await fetch(path, {
    method, body: body ? JSON.stringify(body) : undefined,
    headers: { "Content-Type": "application/json", ...(session ? { Authorization: `Bearer ${session.token}` } : {}), ...headers },
  });
  if (res.status === 401 && session) { signOut(); throw new Error("Сессия истекла — войдите снова"); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const details = data.error?.details?.map(d => d.field).join(", ");
    throw new Error((data.error?.message || `Ошибка ${res.status}`) + (details ? ` (${details})` : ""));
  }
  return data;
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 2600);
}

/* ---------- catalog ---------- */
const state = { q: "", genre: null, sort: "title" };

function coverHtml(b, withBadge = true) {
  const out = withBadge && b.available === 0 ? '<span class="out">Нет в наличии</span>' : "";
  return `<span class="cover-title">${esc(b.title)}</span><span class="cover-author">${esc(b.author)}</span>${out}`;
}

function stockText(b) {
  if (b.available === null || b.available === undefined) return ["Наличие неизвестно", ""];
  if (b.available === 0) return ["Нет в наличии", "low"];
  if (b.available <= 3) return [`Осталось ${b.available}`, "low"];
  return [`В наличии: ${b.available}`, ""];
}

function cardNode(b, i) {
  const el = document.createElement("article");
  el.className = "card";
  el.style.animationDelay = `${Math.min(i, 12) * 30}ms`;
  const [stock, cls] = stockText(b);
  el.innerHTML = `
    <button class="cover" type="button" style="--c:${esc(b.cover_color)}" aria-label="Подробнее: ${esc(b.title)}">${coverHtml(b)}</button>
    <div class="meta">
      <p class="meta-title">${esc(b.title)}</p>
      <p class="meta-author">${esc(b.author)} · ${b.year}</p>
      <span class="stock ${cls}">${stock}</span>
      <div class="buy"><span class="price">${money(b.price_cents)}</span>
        <button class="add" type="button" ${b.available === 0 ? "disabled" : ""}>В корзину</button></div>
    </div>`;
  $(".cover", el).addEventListener("click", () => openBook(b));
  $(".add", el).addEventListener("click", () => addToCart(b.id, 1));
  return el;
}

let loadSeq = 0;
async function loadBooks() {
  const seq = ++loadSeq;
  const grid = $("#grid");
  grid.innerHTML = Array.from({ length: 8 }, () => '<div class="skeleton-card"></div>').join("");
  const params = new URLSearchParams({ limit: "60", sort: state.sort });
  if (state.q) params.set("q", state.q);
  if (state.genre) params.set("genre", state.genre);
  try {
    const page = await api(`/api/books?${params}`);
    if (seq !== loadSeq) return;  // a newer search already started
    grid.innerHTML = "";
    $("#result-count").textContent = `${page.total} ${plural(page.total, "книга", "книги", "книг")}`;
    $("#degraded").hidden = !page.items.some(b => b.available === null);
    if (!page.items.length) grid.innerHTML = '<p class="empty">Ничего не нашлось. Попробуйте другой запрос.</p>';
    page.items.forEach((b, i) => grid.appendChild(cardNode(b, i)));
  } catch (err) {
    grid.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

async function loadGenres() {
  const nav = $("#genres");
  try {
    const genres = await api("/api/genres");
    const total = genres.reduce((s, g) => s + g.books, 0);
    const chip = (label, value, n) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (state.genre === value ? " is-active" : "");
      b.innerHTML = `${esc(label)}<small>${n}</small>`;
      b.addEventListener("click", () => {
        state.genre = value;
        $$(".chip", nav).forEach(c => c.classList.remove("is-active"));
        b.classList.add("is-active");
        loadBooks();
      });
      return b;
    };
    nav.append(chip("Все", null, total), ...genres.map(g => chip(g.genre, g.genre, g.books)));
  } catch { /* catalog down: the grid shows the error */ }
}

let searchTimer;
$("#search").addEventListener("input", e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); loadBooks(); }, 250);
});
$("#sort").addEventListener("change", e => { state.sort = e.target.value; loadBooks(); });

/* ---------- book sheet ---------- */
async function openBook(b) {
  const fresh = await api(`/api/books/${b.id}`).catch(() => b);
  const [stock, cls] = stockText(fresh);
  $("#book-sheet").innerHTML = `
    <div class="cover" style="--c:${esc(fresh.cover_color)}">${coverHtml(fresh, false)}</div>
    <div>
      <h2 id="book-title">${esc(fresh.title)}</h2>
      <p class="by">${esc(fresh.author)}</p>
      <p class="desc">${esc(fresh.description)}</p>
      <div class="facts"><span class="fact">${esc(fresh.genre)}</span><span class="fact">${fresh.year}</span>
        <span class="fact">${fresh.pages} стр.</span><span class="fact">${LANG[fresh.language] || fresh.language}</span></div>
      <p class="stock ${cls}">${stock}</p>
      <div class="buy"><span class="price" style="font-size:22px">${money(fresh.price_cents)}</span>
        <button class="primary" id="sheet-add" type="button" ${fresh.available === 0 ? "disabled" : ""}>В корзину</button></div>
    </div>`;
  $("#sheet-add").addEventListener("click", async () => { await addToCart(fresh.id, 1); $("#book-dialog").close(); });
  $("#book-dialog").showModal();
}

/* ---------- cart ---------- */
let cart = { items: [], total_cents: 0, count: 0 };

function setBadge(el, n) {
  el.textContent = n;
  el.hidden = !n;
  el.classList.remove("bump");
  void el.offsetWidth;
  el.classList.add("bump");
}

async function refreshCart() {
  if (!session) { cart = { items: [], total_cents: 0, count: 0 }; setBadge($("#cart-badge"), 0); return; }
  try { cart = await api("/api/cart"); setBadge($("#cart-badge"), cart.count); } catch { /* keep old */ }
}

async function addToCart(bookId, delta) {
  if (!session) { openAuth("Войдите, чтобы добавлять книги в корзину"); return; }
  const line = cart.items.find(i => i.book_id === bookId);
  const quantity = Math.min((line?.quantity || 0) + delta, 10);
  try {
    cart = await api(`/api/cart/items/${bookId}`, { method: "PUT", body: { quantity } });
    setBadge($("#cart-badge"), cart.count);
    toast("Добавлено в корзину");
  } catch (err) { toast(err.message); }
}

async function setQty(bookId, quantity) {
  cart = quantity < 1
    ? await api(`/api/cart/items/${bookId}`, { method: "DELETE" })
    : await api(`/api/cart/items/${bookId}`, { method: "PUT", body: { quantity: Math.min(quantity, 10) } });
  setBadge($("#cart-badge"), cart.count);
  renderCart();
}

function renderCart() {
  const body = $("#panel-body"), foot = $("#panel-foot");
  if (!cart.items.length) {
    body.innerHTML = '<p class="panel-empty">Корзина пуста. Добавьте книгу из каталога.</p>';
    foot.innerHTML = "";
    return;
  }
  body.innerHTML = cart.items.map(i => `
    <div class="line" data-id="${i.book_id}">
      <span class="mini-cover" style="--c:${esc(i.cover_color)}"></span>
      <div><p class="line-title">${esc(i.title)}</p><p class="line-sub">${esc(i.author)} · ${money(i.unit_price_cents)}</p></div>
      <div class="qty"><button type="button" data-d="-1" aria-label="Меньше">−</button><span>${i.quantity}</span>
        <button type="button" data-d="1" aria-label="Больше">+</button></div>
    </div>`).join("");
  $$(".line", body).forEach(row => $$("button", row).forEach(btn => btn.addEventListener("click", () => {
    const item = cart.items.find(i => i.book_id === Number(row.dataset.id));
    setQty(item.book_id, item.quantity + Number(btn.dataset.d)).catch(err => toast(err.message));
  })));
  foot.innerHTML = `<div class="total"><span>Итого</span><b>${money(cart.total_cents)}</b></div>
    <button class="primary wide" id="to-checkout" type="button">Оформить заказ</button>`;
  $("#to-checkout").addEventListener("click", openCheckout);
}

/* ---------- panel ---------- */
let panelView = null;
function openPanel(title, view) {
  panelView = view;
  $("#panel-title").textContent = title;
  $("#panel-body").innerHTML = '<p class="panel-empty">Загрузка…</p>';
  $("#panel-foot").innerHTML = "";
  $("#panel").classList.add("is-open");
  $("#panel").setAttribute("aria-hidden", "false");
}
function closePanel() { $("#panel").classList.remove("is-open"); $("#panel").setAttribute("aria-hidden", "true"); panelView = null; }
$("#panel-close").addEventListener("click", closePanel);
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("dialog[open]")) closePanel(); });

$("#btn-cart").addEventListener("click", async () => {
  if (!session) { openAuth("Войдите, чтобы открыть корзину"); return; }
  openPanel("Корзина", "cart");
  await refreshCart();
  renderCart();
});

function statusBadge(status) {
  const [label, cls] = STATUS[status] || [status, "info"];
  return `<span class="status ${cls}">${label}</span>`;
}

function orderCard(o, admin = false) {
  const items = o.items.map(i => `${esc(i.title)} × ${i.quantity}`).join(", ");
  const reason = o.cancel_reason_text ? ` · ${esc(o.cancel_reason_text)}` : "";
  let action = "";
  if (!admin && ["pending", "confirmed"].includes(o.status)) action = `<button class="small-btn" data-cancel="${o.id}">Отменить</button>`;
  if (admin && ["paid", "confirmed"].includes(o.status)) action = `<button class="small-btn dark" data-ship="${o.id}">Отправить</button>`;
  if (admin && o.status === "shipped") action = `<button class="small-btn dark" data-deliver="${o.id}">Доставлен</button>`;
  return `<div class="order-card">
    <div class="order-head"><b>Заказ №${o.id}${admin ? ` · клиент ${o.user_id}` : ""}</b>${statusBadge(o.status)}</div>
    <p class="order-items">${items}</p>
    <div class="order-foot"><span>${when(o.created_at)} · ${money(o.total_cents)}${reason}</span>${action}</div>
  </div>`;
}

async function showOrders() {
  openPanel("Мои заказы", "orders");
  try {
    const orders = await api("/api/orders");
    $("#panel-body").innerHTML = orders.length ? orders.map(o => orderCard(o)).join("") : '<p class="panel-empty">Заказов пока нет.</p>';
    $$("[data-cancel]").forEach(b => b.addEventListener("click", async () => {
      try { await api(`/api/orders/${b.dataset.cancel}/cancel`, { method: "POST" }); toast("Заказ отменён"); showOrders(); }
      catch (err) { toast(err.message); }
    }));
  } catch (err) { $("#panel-body").innerHTML = `<p class="panel-empty">${esc(err.message)}</p>`; }
}

async function showNotifications() {
  openPanel("Уведомления", "notifications");
  try {
    const items = await api("/api/notifications");
    $("#panel-body").innerHTML = items.length ? items.map(n => `
      <div class="note ${n.read ? "" : "is-unread"}"><p class="n-title">${esc(n.title)}</p>
        <p class="n-body">${esc(n.body)}</p><p class="n-time">${when(n.created_at)}</p></div>`).join("")
      : '<p class="panel-empty">Уведомлений нет.</p>';
    await Promise.all(items.filter(n => !n.read).map(n => api(`/api/notifications/${n.id}/read`, { method: "POST" })));
    refreshBell();
  } catch (err) { $("#panel-body").innerHTML = `<p class="panel-empty">${esc(err.message)}</p>`; }
}

async function showAdmin() {
  openPanel("Администратор", "admin");
  try {
    const [stats, orders] = await Promise.all([api("/api/admin/stats"), api("/api/admin/orders")]);
    const by = stats.orders_by_status;
    $("#panel-body").innerHTML = `<div class="stats">
        <div class="stat"><b>${money(stats.revenue_cents)}</b><span>выручка</span></div>
        <div class="stat"><b>${Object.values(by).reduce((a, b) => a + b, 0)}</b><span>заказов</span></div>
        <div class="stat"><b>${(by.paid || 0) + (by.confirmed || 0)}</b><span>ждут отправки</span></div>
        <div class="stat"><b>${by.cancelled || 0}</b><span>отменено</span></div></div>
      ${orders.map(o => orderCard(o, true)).join("") || '<p class="panel-empty">Заказов нет.</p>'}`;
    const act = (attr, path) => $$(`[data-${attr}]`).forEach(b => b.addEventListener("click", async () => {
      try { await api(`/api/admin/orders/${b.dataset[attr]}/${path}`, { method: "POST" }); showAdmin(); }
      catch (err) { toast(err.message); }
    }));
    act("ship", "ship");
    act("deliver", "deliver");
  } catch (err) { $("#panel-body").innerHTML = `<p class="panel-empty">${esc(err.message)}</p>`; }
}

$("#btn-orders").addEventListener("click", showOrders);
$("#btn-bell").addEventListener("click", showNotifications);
$("#btn-admin").addEventListener("click", showAdmin);

async function refreshBell() {
  if (!session) return;
  try { setBadge($("#bell-badge"), (await api("/api/notifications?unread_only=true")).length); } catch { /* ignore */ }
}

/* ---------- checkout + live saga tracking ---------- */
let checkoutKey = null;

async function openCheckout() {
  if (!cart.items.length) return;
  const f = $("#checkout-form");
  try {
    const p = await api("/api/users/me");
    for (const k of ["full_name", "phone", "city", "street"]) if (p[k] && !f[k].value) f[k].value = p[k];
  } catch { /* profile is created asynchronously, may not exist yet */ }
  $("#checkout-summary").textContent = `${cart.count} ${plural(cart.count, "книга", "книги", "книг")} на ${money(cart.total_cents)}`;
  $("#checkout-error").hidden = true;
  checkoutKey = crypto.randomUUID();  // one key per checkout: a double click cannot create two orders
  $("#checkout-dialog").showModal();
}

$("#checkout-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, btn = $("#checkout-submit");
  const shipping = { full_name: f.full_name.value.trim(), phone: f.phone.value.trim(), city: f.city.value.trim(), street: f.street.value.trim() };
  btn.disabled = true;
  $("#checkout-error").hidden = true;
  try {
    const order = await api("/api/orders", { method: "POST", headers: { "Idempotency-Key": checkoutKey },
      body: { shipping, payment_method: f.payment_method.value } });
    api("/api/users/me", { method: "PATCH", body: shipping }).catch(() => {});  // remember the address
    $("#checkout-dialog").close();
    closePanel();
    track(order);
  } catch (err) {
    $("#checkout-error").textContent = err.message;
    $("#checkout-error").hidden = false;
  } finally { btn.disabled = false; }
});

function setSteps(status, reason) {
  const order = ["created", "stock", "payment", "done"];
  const progress = { pending: 1, awaiting_payment: 2, paid: 4, confirmed: 4, shipped: 4, delivered: 4 }[status];
  const failedAt = status === "cancelled" ? (reason === "out_of_stock" ? 1 : reason === "payment_declined" ? 2 : null) : null;
  order.forEach((step, i) => {
    const li = $(`[data-step="${step}"]`, $("#track-steps"));
    li.className = "";
    if (failedAt !== null) li.className = i < failedAt ? "done" : i === failedAt ? "failed" : "";
    else if (i < progress) li.className = "done";
    else if (i === progress) li.className = "active";
  });
}

async function track(order) {
  $("#track-title").textContent = `Заказ №${order.id}`;
  $("#track-sub").textContent = `${money(order.total_cents)} · события проходят через RabbitMQ`;
  const result = $("#track-result");
  result.textContent = "";
  result.className = "track-result";
  setSteps(order.status);
  $("#track-dialog").showModal();
  for (let i = 0; i < 60; i++) {
    const o = await api(`/api/orders/${order.id}`).catch(() => null);
    if (o) {
      setSteps(o.status, o.cancel_reason);
      if (["paid", "confirmed", "cancelled"].includes(o.status)) {
        const ok = o.status !== "cancelled";
        result.className = `track-result ${ok ? "ok" : "bad"}`;
        result.textContent = ok ? (o.status === "paid" ? "Оплачено. Книги зарезервированы и скоро будут отправлены."
          : "Заказ подтверждён. Оплата курьеру при получении.") : `Заказ отменён: ${o.cancel_reason_text}.`;
        refreshCart(); refreshBell(); loadBooks();
        return;
      }
    }
    await new Promise(r => setTimeout(r, 400));
  }
  result.textContent = "Заказ обрабатывается дольше обычного — статус обновится в «Мои заказы».";
}

/* ---------- auth ---------- */
let authMode = "login";
function setAuthMode(mode) {
  authMode = mode;
  $("#auth-dialog").classList.toggle("is-register", mode === "register");
  $$(".seg").forEach(b => { b.classList.toggle("is-active", b.dataset.mode === mode); b.setAttribute("aria-selected", String(b.dataset.mode === mode)); });
  $("#auth-title").textContent = mode === "login" ? "С возвращением" : "Создать аккаунт";
  $("#auth-submit").textContent = mode === "login" ? "Войти" : "Зарегистрироваться";
  $("#auth-form").password.autocomplete = mode === "login" ? "current-password" : "new-password";
  $("#auth-error").hidden = true;
}
function openAuth(hint) {
  setAuthMode("login");
  $("#auth-form").reset();
  if (hint) toast(hint);
  $("#auth-dialog").showModal();
}
$$(".seg").forEach(b => b.addEventListener("click", () => setAuthMode(b.dataset.mode)));
$("#btn-signin").addEventListener("click", () => openAuth());

$("#auth-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, btn = $("#auth-submit");
  const body = { email: f.email.value.trim(), password: f.password.value };
  if (authMode === "register") body.full_name = f.full_name.value.trim();
  btn.disabled = true;
  try {
    const data = await api(`/api/auth/${authMode}`, { method: "POST", body });
    session = { token: data.access_token, role: data.role, email: body.email, name: body.full_name || body.email,
                expires_at: Date.now() + data.expires_in * 1000 };
    store.set(session);
    $("#auth-dialog").close();
    renderAccount();
    toast(authMode === "register" ? "Аккаунт создан" : "Вы вошли");
  } catch (err) {
    $("#auth-error").textContent = err.message;
    $("#auth-error").hidden = false;
  } finally { btn.disabled = false; }
});

function signOut() { store.clear(); session = null; closePanel(); renderAccount(); }
$("#btn-avatar").addEventListener("click", () => { if (confirm(`Выйти из аккаунта ${session.email}?`)) signOut(); });

$$("[data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));

function renderAccount() {
  const signed = Boolean(session);
  $("#btn-signin").hidden = signed;
  for (const id of ["#btn-avatar", "#btn-orders", "#btn-bell"]) $(id).hidden = !signed;
  $("#btn-admin").hidden = !(signed && session.role === "admin");
  if (signed) $("#btn-avatar").textContent = session.name.trim()[0].toUpperCase();
  refreshCart();
  refreshBell();
}

/* ---------- start ---------- */
renderAccount();
loadGenres();
loadBooks();
setInterval(() => { if (session && document.visibilityState === "visible") refreshBell(); }, 15000);
