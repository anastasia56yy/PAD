const $ = (sel, root = document) => root.querySelector(sel);

const form = $("#composer");
const input = $("#question");
const sendBtn = $("#send");
const thread = $("#thread");
const hero = $("#hero");

const LANG = { ro: "RO", en: "EN", ru: "RU" };
const STEPS = ["Ищу в документах…", "Отбираю фрагменты…", "Проверяю релевантность…", "Формулирую ответ…"];

const ICONS = {
  external: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
  chevron: '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
  info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
};

/* ---------- helpers ---------- */
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Minimal formatting for model output: paragraphs, "- " lists, **bold**, [n] citations. */
function renderAnswer(text, nSources) {
  const inline = s => escapeHtml(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[(\d+)\]/g, (m, n) => (+n >= 1 && +n <= nSources)
      ? `<button type="button" class="cite" data-n="${n}" aria-label="Источник ${n}">${n}</button>` : "");
  const out = [];
  let list = null;
  for (const raw of text.split(/\n+/)) {
    // small models sometimes open a sentence with its citation: "[1] Text" -> "Text [1]"
    const line = raw.trim().replace(/^((?:\[\d+\]\s*)+)(.+)$/, (m, cites, rest) => `${rest} ${cites.trim()}`);
    if (!line) continue;
    const item = line.match(/^[-•*]\s+(.*)$/) || line.match(/^\d+[.)]\s+(.*)$/);
    if (item) {
      if (!list) { list = []; out.push(list); }
      list.push(`<li>${inline(item[1])}</li>`);
    } else {
      list = null;
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  return out.map(x => Array.isArray(x) ? `<ul>${x.join("")}</ul>` : x).join("");
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function scrollToBottom() {
  requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }));
}

/** Bring a new answer into view from its question, so the reader starts at the top of the answer. */
function scrollToTurn(questionEl) {
  const top = questionEl.getBoundingClientRect().top + window.scrollY - 84;
  requestAnimationFrame(() => window.scrollTo({ top, behavior: "smooth" }));
}

function autosize() {
  input.style.height = "44px";  // reset to one line, then grow to the content
  if (input.value) input.style.height = Math.min(Math.max(input.scrollHeight, 44), 160) + "px";
  sendBtn.disabled = input.value.trim().length < 3;
}

function leaveHero() {
  if (document.body.classList.contains("has-thread")) return;
  document.body.classList.add("has-thread");
  setTimeout(() => hero.classList.add("is-gone"), 560);
}

/* ---------- rendering ---------- */
function questionNode(text) {
  const node = $("#tpl-question").content.firstElementChild.cloneNode(true);
  $(".bubble", node).textContent = text;
  return node;
}

function loadingNode() {
  const node = $("#tpl-loading").content.firstElementChild.cloneNode(true);
  const status = $(".status", node);
  let i = 0;
  // honest progress text: cycles through the pipeline stages while the request runs
  node._timer = setInterval(() => { i = Math.min(i + 1, STEPS.length - 1); status.textContent = STEPS[i]; }, 1400);
  return node;
}

function sourceNode(s, index) {
  const el = document.createElement("article");
  el.className = "source";
  el.id = `src-${s.n}-${index}`;
  el.dataset.n = s.n;
  el.style.animationDelay = `${80 + s.n * 45}ms`;
  const meta = [hostOf(s.url), s.section].filter(Boolean).join(" · ");
  el.innerHTML = `
    <div class="source-top">
      <span class="source-n">${s.n}</span>
      <h3 class="source-title"></h3>
    </div>
    <p class="source-meta"></p>
    <div class="source-actions">
      <button type="button" class="ghost toggle" aria-expanded="false">${ICONS.chevron}Фрагмент</button>
      <a class="ghost" target="_blank" rel="noopener noreferrer">${ICONS.external}Открыть</a>
    </div>
    <div class="excerpt"><div><p></p></div></div>`;
  $(".source-title", el).textContent = s.title || "Документ";
  $(".source-meta", el).textContent = meta;
  $(".source-meta", el).title = meta;
  $("a", el).href = s.url;
  $(".excerpt p", el).textContent = s.text;
  const toggle = $(".toggle", el);
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    $(".excerpt", el).classList.toggle("is-open", open);
  });
  return el;
}

let answerCount = 0;

function answerNode(data) {
  const id = ++answerCount;
  const card = document.createElement("article");
  card.className = "msg card glass" + (data.refused ? " is-refusal" : "");
  const st = data.stats;
  const head = `
    <div class="card-head">
      <span class="dot" aria-hidden="true"></span>
      <span class="status">${data.refused ? "В базе нет ответа" : "Ответ"}</span>
      <span class="tag">${LANG[data.lang] || ""}</span>
      <span class="spacer"></span>
      <span class="timing">${st.total_s.toFixed(1)} с</span>
    </div>`;

  let body;
  if (data.refused) {
    body = `
      <div class="notice">${ICONS.info}
        <div><p></p>
          <p class="hint">Система отвечает только по документам utm.md и fcim.utm.md и не придумывает ответы. Попробуйте спросить о регламентах, ECTS, стипендиях, Erasmus+ или о факультете.</p>
        </div>
      </div>`;
  } else {
    body = `<div class="answer">${renderAnswer(data.answer, data.sources.length)}</div>`;
  }

  const pipeline = `
    <p class="pipeline" aria-label="Как найден ответ">
      <span><b>${st.retrieved}</b> найдено</span><span class="arrow">→</span>
      <span><b>${st.filtered}</b> после фильтров</span><span class="arrow">→</span>
      <span><b>${st.context}</b> в контексте модели</span>
      ${st.translated ? '<span class="arrow">·</span><span>переведено</span>' : ""}
    </p>`;

  const sources = data.sources.length
    ? `<h2 class="sources-title">Источники</h2><div class="sources"></div>` : "";

  card.innerHTML = head + body + pipeline + sources;
  if (data.refused) $(".notice p", card).textContent = data.answer;

  const grid = $(".sources", card);
  if (grid) data.sources.forEach(s => grid.appendChild(sourceNode(s, id)));

  // citation -> source card
  card.addEventListener("click", e => {
    const cite = e.target.closest(".cite");
    if (!cite) return;
    const target = $(`#src-${cite.dataset.n}-${id}`, card);
    if (!target) return;
    card.querySelectorAll(".cite.is-active, .source.is-highlighted").forEach(x => x.classList.remove("is-active", "is-highlighted"));
    cite.classList.add("is-active");
    target.classList.add("is-highlighted");
    target.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  return card;
}

function errorNode(message, question) {
  const card = document.createElement("article");
  card.className = "msg card glass is-error";
  card.innerHTML = `
    <div class="card-head"><span class="dot" aria-hidden="true"></span><span class="status">Не удалось получить ответ</span></div>
    <div class="notice">${ICONS.info}<div><p></p><p class="hint">Проверьте, что запущена Ollama, и повторите запрос.</p></div></div>
    <button type="button" class="ghost retry">Повторить</button>`;
  $(".notice p", card).textContent = message;
  $(".retry", card).addEventListener("click", () => { card.remove(); ask(question, { echo: false }); });
  return card;
}

/* ---------- flow ---------- */
let busy = false;

async function ask(question, { echo = true } = {}) {
  if (busy) return;
  busy = true;
  leaveHero();
  sendBtn.disabled = true;
  sendBtn.classList.add("is-busy");
  input.setAttribute("aria-busy", "true");

  if (echo) thread.appendChild(questionNode(question));
  const questionEl = thread.lastElementChild;
  const loading = loadingNode();
  thread.appendChild(loading);
  scrollToBottom();

  let node;
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail?.[0]?.msg || data.detail || `Ошибка сервера (${res.status})`);
    node = answerNode(data);
  } catch (err) {
    node = errorNode(err.message || "Сервер недоступен", question);
  } finally {
    clearInterval(loading._timer);
    loading.replaceWith(node);
    busy = false;
    sendBtn.classList.remove("is-busy");
    input.removeAttribute("aria-busy");
    autosize();
    scrollToTurn(questionEl);
  }
}

form.addEventListener("submit", e => {
  e.preventDefault();
  const q = input.value.trim();
  if (q.length < 3 || busy) return;
  input.value = "";
  autosize();
  ask(q);
});

input.addEventListener("input", autosize);
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll(".chip").forEach(chip =>
  chip.addEventListener("click", () => ask(chip.textContent.trim())));

fetch("/api/info").then(r => r.json()).then(info => {
  const n = new Intl.NumberFormat("ru-RU").format(info.documents);
  $("#meta").textContent = `${n} документов · ${info.llm}`;
}).catch(() => {});

autosize();
input.focus({ preventScroll: true });
