// Веб-интерфейс файлового менеджера.
//
// Страница ничего не решает сама: каждое действие — команда протокола,
// которую шлюз пересылает серверу. Все данные с сервера выводятся через
// textContent, а не через HTML: имя файла вида <img onerror=...> останется
// просто текстом.
"use strict";

const API_URL = "/api/call";
const MAX_FILE_SIZE = 256 * 1024;
const PING_INTERVAL = 20000;
const OPERATION_NAMES = { create: "создание", modify: "изменение", delete: "удаление" };
const TYPE_NAMES = { file: "файл", dir: "каталог", link: "ссылка", other: "другое" };

const state = { user: null, cwd: "", entries: [], selected: new Set(), view: "files", pingTimer: null };

// ---------------------------------------------------------------- утилиты

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "value") node.value = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function humanSize(size) {
  if (size === null || size === undefined) return "—";
  let value = Number(size);
  for (const unit of ["Б", "КБ", "МБ", "ГБ", "ТБ"]) {
    if (value < 1024 || unit === "ТБ") return unit === "Б" ? `${value} ${unit}` : `${value.toFixed(1)} ${unit}`;
    value /= 1024;
  }
  return String(size);
}

const joinPath = (dir, name) => (dir ? `${dir}/${name}` : name);
const parentPath = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
const baseName = (path) => path.slice(path.lastIndexOf("/") + 1);
const extension = (name) => (name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toLowerCase() : "");
const stem = (name) => (name.includes(".") ? name.slice(0, name.lastIndexOf(".")) : name);

function toast(message, { error = false, code = null } = {}) {
  const node = el("div", { class: error ? "toast error" : "toast", role: error ? "alert" : "status" },
    code ? el("span", { class: "code", text: code }) : null, message);
  $("toasts").append(node);
  setTimeout(() => node.remove(), error ? 8000 : 4500);
}

function toastError(reply) {
  toast(reply.message || "Ошибка", { error: true, code: reply.code });
}

// ---------------------------------------------------------------- API

async function api(cmd, args = {}) {
  let response;
  try {
    response = await fetch(API_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-FM-Request": "1" },
      body: JSON.stringify({ ...args, cmd }),
    });
  } catch {
    return { status: "error", code: "NETWORK", message: "Шлюз недоступен" };
  }
  let data;
  try {
    data = await response.json();
  } catch {
    data = { status: "error", code: "BAD_REPLY", message: `Ответ шлюза не JSON (HTTP ${response.status})` };
  }
  if (data.status === "error" && data.code === "AUTH_REQUIRED" && state.user !== null) {
    showLogin("Сеанс на сервере завершён, войдите снова");
  }
  return data;
}

// ---------------------------------------------------------------- диалог

const dialog = $("dialog");
let dialogPrimary = null;

function openDialog({ title, body, actions = [], primary = null }) {
  $("dialog-title").textContent = title;
  $("dialog-body").replaceChildren(...body);
  $("dialog-foot").replaceChildren(...actions);
  dialogPrimary = primary;
  if (!dialog.open) dialog.showModal();
}

function closeDialog() {
  dialogPrimary = null;
  if (dialog.open) dialog.close();
}

function confirmDialog({ title, message, confirmText = "Удалить", danger = true }) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      dialog.removeEventListener("close", onClose);
      closeDialog();
      resolve(value);
    };
    const onClose = () => finish(false);
    openDialog({
      title,
      body: [el("p", { text: message })],
      actions: [
        el("button", { type: "button", text: "Отмена", onclick: () => finish(false) }),
        el("button", { type: "button", class: danger ? "danger solid" : "primary", text: confirmText, onclick: () => finish(true) }),
      ],
      primary: () => finish(true),
    });
    dialog.addEventListener("close", onClose);
  });
}

function field(labelText, control) {
  const id = control.id || `f-${Math.random().toString(36).slice(2)}`;
  control.id = id;
  return el("div", { class: "field" }, el("label", { for: id, text: labelText }), control);
}

function dialogError() {
  return el("p", { class: "dialog-error", role: "alert" });
}

async function busy(button, action) {
  button.disabled = true;
  try {
    return await action();
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- вход

let loginMode = "login";

function setLoginMode(mode) {
  loginMode = mode;
  const register = mode === "register";
  $("tab-login").setAttribute("aria-selected", String(!register));
  $("tab-register").setAttribute("aria-selected", String(register));
  $("login-title").textContent = register ? "Регистрация" : "Вход";
  $("login-submit").textContent = register ? "Зарегистрироваться" : "Войти";
  $("repeat-row").hidden = !register;
  $("login-hint").hidden = !register;
  $("login-password").autocomplete = register ? "new-password" : "current-password";
  $("login-error").textContent = "";
}

function showLogin(message = "") {
  state.user = null;
  clearInterval(state.pingTimer);
  closeDialog();
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  $("login-error").textContent = message;
  $("login-password").value = "";
  $("login-repeat").value = "";
  $("login-username").focus();
}

async function submitLogin(event) {
  event.preventDefault();
  const username = $("login-username").value.trim();
  const password = $("login-password").value;
  const error = $("login-error");
  error.textContent = "";
  if (!username || !password) {
    error.textContent = "Введите логин и пароль";
    return;
  }
  if (loginMode === "register" && password !== $("login-repeat").value) {
    error.textContent = "Пароли не совпадают";
    return;
  }
  const reply = await busy($("login-submit"), () => api(loginMode, { username, password }));
  $("login-password").value = "";
  $("login-repeat").value = "";
  if (reply.status !== "ok") {
    error.textContent = reply.message || "Не удалось войти";
    return;
  }
  enterApp(reply.result.username);
}

function enterApp(username) {
  state.user = username;
  $("current-user").textContent = username;
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  showView("files");
  loadDir("");
  pingServer();
  clearInterval(state.pingTimer);
  state.pingTimer = setInterval(pingServer, PING_INTERVAL);
}

async function logout() {
  await api("logout");
  showLogin("Вы вышли");
}

async function pingServer() {
  const pill = $("server-status");
  const started = performance.now();
  const reply = await api("ping");
  if (reply.status === "ok") {
    pill.textContent = `сервер: на связи · ${Math.round(performance.now() - started)} мс`;
    pill.className = "status-pill up";
  } else {
    pill.textContent = "сервер: нет связи";
    pill.className = "status-pill down";
  }
}

// ---------------------------------------------------------------- разделы

function showView(view) {
  state.view = view;
  for (const item of document.querySelectorAll(".nav-item")) {
    if (item.dataset.view === view) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  for (const name of ["files", "drives", "log", "ops"]) $(`view-${name}`).hidden = name !== view;
  if (view === "drives") loadDrives();
  if (view === "log") loadLog();
  if (view === "ops") loadOps();
  if (view === "files") loadDir(state.cwd);
}

// ---------------------------------------------------------------- файлы

async function loadDir(path) {
  const reply = await api("ls", { path });
  if (reply.status !== "ok") {
    toastError(reply);
    if (reply.code === "NOT_FOUND" && path) loadDir(parentPath(path));
    return;
  }
  state.cwd = reply.result.path || "";
  state.entries = [...reply.result.entries].sort((a, b) =>
    a.type === "dir" && b.type !== "dir" ? -1 : a.type !== "dir" && b.type === "dir" ? 1 : a.name.localeCompare(b.name, "ru"));
  state.selected.clear();
  renderBreadcrumbs();
  renderFiles(reply.result.truncated);
}

function renderBreadcrumbs() {
  const nodes = [];
  const parts = state.cwd ? state.cwd.split("/") : [];
  const rootLabel = "мои файлы";
  if (parts.length === 0) nodes.push(el("span", { class: "here", text: `/ ${rootLabel}` }));
  else nodes.push(el("button", { type: "button", text: `/ ${rootLabel}`, onclick: () => loadDir("") }));
  parts.forEach((part, index) => {
    nodes.push(el("span", { class: "sep", text: "/" }));
    const path = parts.slice(0, index + 1).join("/");
    if (index === parts.length - 1) nodes.push(el("span", { class: "here", text: part }));
    else nodes.push(el("button", { type: "button", text: part, onclick: () => loadDir(path) }));
  });
  $("breadcrumbs").replaceChildren(...nodes);
  $("btn-up").disabled = !state.cwd;
}

function kindLabel(entry) {
  if (entry.type === "dir") return "папка";
  if (entry.type !== "file") return TYPE_NAMES[entry.type] || entry.type;
  return extension(entry.name).slice(0, 4) || "файл";
}

function renderFiles(truncated) {
  const body = $("files-body");
  $("select-all").checked = false;
  updateSelection();
  if (state.entries.length === 0) {
    body.replaceChildren(el("tr", { class: "empty" },
      el("td", { colspan: "5", text: "Папка пуста. Создайте файл или загрузите его с компьютера." })));
  } else {
    body.replaceChildren(...state.entries.map(fileRow));
  }
  const note = $("files-note");
  note.hidden = !truncated;
  note.textContent = truncated ? "Показаны не все записи: сервер отдаёт не больше 1000 за раз." : "";
}

function fileRow(entry) {
  const path = joinPath(state.cwd, entry.name);
  const isDir = entry.type === "dir";
  const isZip = !isDir && extension(entry.name) === "zip";
  const check = el("input", { type: "checkbox", "aria-label": `Выбрать ${entry.name}` });
  check.addEventListener("change", () => {
    if (check.checked) state.selected.add(path);
    else state.selected.delete(path);
    updateSelection();
  });
  const actions = el("div", { class: "row-actions" },
    isZip ? el("button", { type: "button", text: "Содержимое", onclick: () => openZip(path) }) : null,
    isZip ? el("button", { type: "button", text: "Распаковать", onclick: () => unzipDialog(path) }) : null,
    el("button", { type: "button", text: "Свойства", onclick: () => showInfo(path) }),
    !isDir && !isZip ? el("button", { type: "button", text: "Скачать", onclick: () => download(path) }) : null,
    el("button", { type: "button", class: "danger", text: "Удалить", onclick: () => removeEntry(entry, path) }),
  );
  return el("tr", {},
    el("td", { class: "col-check" }, check),
    el("td", {}, el("div", { class: "name-cell" },
      el("span", { class: isDir ? "kind dir" : "kind", text: kindLabel(entry) }),
      el("button", { type: "button", text: entry.name, title: isDir ? "Открыть папку" : "Открыть файл", onclick: () => openEntry(entry, path) }))),
    el("td", { class: "col-size", text: isDir ? "—" : humanSize(entry.size) }),
    el("td", { class: "col-date mono", text: entry.modified || "" }),
    el("td", { class: "col-actions" }, actions),
  );
}

function updateSelection() {
  const count = state.selected.size;
  const button = $("btn-zip");
  button.disabled = count === 0;
  button.textContent = count ? `Упаковать в ZIP (${count})` : "Упаковать в ZIP";
}

function openEntry(entry, path) {
  if (entry.type === "dir") return loadDir(path);
  const ext = extension(entry.name);
  if (ext === "json") return openStructured(path, "json");
  if (ext === "xml") return openStructured(path, "xml");
  if (ext === "zip") return openZip(path);
  return openText(path);
}

// --- текстовый файл

async function openText(path) {
  const reply = await api("read", { path });
  if (reply.status !== "ok") return showUnreadable(path, reply);
  const area = el("textarea", { spellcheck: "false", "aria-label": "Содержимое файла" });
  area.value = reply.result.content;
  const error = dialogError();
  const save = el("button", { type: "button", class: "primary", text: "Сохранить" });
  save.addEventListener("click", () => busy(save, async () => {
    const result = await api("write", { path, content: area.value, overwrite: true });
    if (result.status !== "ok") return void (error.textContent = `${result.code}: ${result.message}`);
    toast(`Сохранено: /${result.result.path} (${humanSize(result.result.size)})`);
    closeDialog();
    loadDir(state.cwd);
  }));
  openDialog({
    title: `/${path}`,
    body: [area, error],
    actions: [
      el("button", { type: "button", text: "Скачать", onclick: () => download(path) }),
      el("button", { type: "button", text: "Свойства", onclick: () => showInfo(path) }),
      el("button", { type: "button", text: "Закрыть", onclick: closeDialog }),
      save,
    ],
  });
  area.focus();
}

function showUnreadable(path, reply) {
  const reasons = {
    NOT_TEXT: "Файл не в кодировке UTF-8. Протокол передаёт содержимое файлов только текстом, поэтому открыть и скачать его здесь нельзя.",
    FILE_TOO_LARGE: "Файл больше 256 КБ — это предел сервера для чтения и записи через протокол.",
  };
  openDialog({
    title: `/${path}`,
    body: [
      el("p", { text: reasons[reply.code] || reply.message || "Не удалось открыть файл" }),
      el("p", { class: "note" }, el("span", { class: "mono", text: reply.code })),
    ],
    actions: [
      el("button", { type: "button", text: "Свойства", onclick: () => showInfo(path) }),
      el("button", { type: "button", class: "primary", text: "Закрыть", onclick: closeDialog }),
    ],
  });
}

// --- JSON и XML: структура и текст

async function openStructured(path, kind) {
  const isJson = kind === "json";
  const [parsed, raw] = await Promise.all([
    api(isJson ? "json_read" : "xml_read", { path }),
    api("read", { path }),
  ]);
  if (raw.status !== "ok") return showUnreadable(path, raw);

  const structure = el("div", {});
  if (parsed.status === "ok") {
    structure.append(isJson
      ? el("pre", { class: "view-box", text: JSON.stringify(parsed.result.data, null, 2) })
      : el("div", { class: "view-box" }, xmlNode(parsed.result.tree, true)));
  } else {
    structure.append(
      el("p", { class: "dialog-error", text: `${parsed.code}: ${parsed.message}` }),
      el("p", { class: "note", text: "Сервер отказался разбирать файл. Текст можно посмотреть и исправить на вкладке «Текст»." }),
    );
  }

  const area = el("textarea", { spellcheck: "false", "aria-label": "Текст файла" });
  area.value = raw.result.content;
  const textPane = el("div", { hidden: true }, area);
  const error = dialogError();

  const tabStructure = el("button", { type: "button", role: "tab", "aria-selected": "true", text: isJson ? "Данные" : "Дерево" });
  const tabText = el("button", { type: "button", role: "tab", "aria-selected": "false", text: "Текст" });
  const select = (showText) => {
    tabStructure.setAttribute("aria-selected", String(!showText));
    tabText.setAttribute("aria-selected", String(showText));
    structure.hidden = showText;
    textPane.hidden = !showText;
    save.hidden = !showText;
  };
  tabStructure.addEventListener("click", () => select(false));
  tabText.addEventListener("click", () => select(true));

  const save = el("button", { type: "button", class: "primary", text: "Сохранить", hidden: true });
  save.addEventListener("click", () => busy(save, async () => {
    // JSON уходит текстом: строгий разбор на шлюзе отвергнет повторяющиеся
    // ключи, а JSON.parse в браузере молча оставил бы последнее значение.
    const result = isJson
      ? await api("json_write", { path, data_text: area.value, overwrite: true })
      : await api("xml_write", { path, content: area.value, overwrite: true });
    if (result.status !== "ok") return void (error.textContent = `${result.code}: ${result.message}`);
    toast(`Сохранено: /${result.result.path}`);
    closeDialog();
    loadDir(state.cwd);
  }));

  openDialog({
    title: `/${path}`,
    body: [el("div", { class: "subtabs", role: "tablist" }, tabStructure, tabText), structure, textPane, error],
    actions: [
      el("button", { type: "button", text: "Скачать", onclick: () => download(path) }),
      el("button", { type: "button", text: "Свойства", onclick: () => showInfo(path) }),
      el("button", { type: "button", text: "Закрыть", onclick: closeDialog }),
      save,
    ],
  });
}

function xmlNode(node, root = false) {
  const attrs = Object.entries(node.attrs || {}).map(([name, value]) => ` ${name}="${value}"`).join("");
  return el("div", { class: root ? "xml-node xml-root" : "xml-node" },
    el("span", { class: "xml-tag", text: `<${node.tag}>` }),
    attrs ? el("span", { class: "xml-attr", text: attrs }) : null,
    node.text ? el("span", { class: "xml-text", text: ` ${node.text}` }) : null,
    ...(node.children || []).map((child) => xmlNode(child)));
}

// --- свойства, скачивание, удаление

async function showInfo(path) {
  const reply = await api("info", { path });
  if (reply.status !== "ok") return toastError(reply);
  const info = reply.result;
  const rows = [
    ["Путь", `/${info.path}`],
    ["Тип", TYPE_NAMES[info.type] || info.type],
  ];
  if (info.size !== null && info.size !== undefined) rows.push(["Размер", `${humanSize(info.size)} (${info.size} байт)`]);
  rows.push(["Создан", `${info.created} UTC`], ["Изменён", `${info.modified} UTC`]);
  const list = el("dl", { class: "kv" }, ...rows.flatMap(([key, value]) => [el("dt", { text: key }), el("dd", { text: value })]));
  if (info.type === "file") {
    const record = info.record;
    list.append(el("dt", { text: "В базе" }), el("dd", {},
      record
        ? el("span", { class: "badge ok", text: `запись №${record.id}, создана ${record.created_at} UTC` })
        : el("span", { class: "badge warn", text: "нет записи: файл появился в обход сервера" })));
  }
  openDialog({
    title: `Свойства: ${baseName(info.path) || "/"}`,
    body: [list],
    actions: [el("button", { type: "button", class: "primary", text: "Закрыть", onclick: closeDialog })],
    primary: closeDialog,
  });
}

async function download(path) {
  const reply = await api("read", { path });
  if (reply.status !== "ok") {
    toast(reply.code === "NOT_TEXT" ? "Скачать можно только текстовые файлы в UTF-8" : reply.message, { error: true, code: reply.code });
    return;
  }
  const blob = new Blob([reply.result.content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: baseName(path) });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function removeEntry(entry, path) {
  const isDir = entry.type === "dir";
  const ok = await confirmDialog({
    title: isDir ? "Удалить папку?" : "Удалить файл?",
    message: isDir
      ? `Папка /${path} будет удалена. Сервер удаляет только пустые папки.`
      : `Файл /${path} будет удалён без возможности восстановления.`,
  });
  if (!ok) return;
  const reply = await api(isDir ? "rmdir" : "delete", { path });
  if (reply.status !== "ok") return toastError(reply);
  toast(`Удалено: /${reply.result.path}`);
  loadDir(state.cwd);
}

// --- создание

function newFileDialog() {
  const name = el("input", { placeholder: "заметка.txt", autocomplete: "off" });
  const kind = el("select", {},
    el("option", { value: "text", text: "Текст" }),
    el("option", { value: "json", text: "JSON (проверяется сервером)" }),
    el("option", { value: "xml", text: "XML (проверяется сервером)" }));
  const area = el("textarea", { spellcheck: "false", "aria-label": "Содержимое" });
  const error = dialogError();
  const templates = { text: "", json: "{\n  \"ключ\": \"значение\"\n}\n", xml: "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<root>\n</root>\n" };
  kind.addEventListener("change", () => {
    if (!area.value.trim() || Object.values(templates).includes(area.value)) area.value = templates[kind.value];
    const ext = { text: "txt", json: "json", xml: "xml" }[kind.value];
    if (!name.value || ["txt", "json", "xml"].includes(extension(name.value))) name.placeholder = `файл.${ext}`;
  });
  name.addEventListener("change", () => {
    const ext = extension(name.value);
    if (ext === "json" || ext === "xml") {
      kind.value = ext;
      kind.dispatchEvent(new Event("change"));
    }
  });
  let overwrite = false;
  const create = el("button", { type: "button", class: "primary", text: "Создать" });
  const submit = () => busy(create, async () => {
    const fileName = name.value.trim();
    if (!fileName) return void (error.textContent = "Введите имя файла");
    const path = joinPath(state.cwd, fileName);
    const content = area.value;
    const reply = kind.value === "json"
      ? await api("json_write", { path, data_text: content, overwrite })
      : await api(kind.value === "xml" ? "xml_write" : "write", { path, content, overwrite });
    if (reply.status === "ok") {
      toast(`${reply.result.created ? "Создан" : "Перезаписан"}: /${reply.result.path}`);
      closeDialog();
      return loadDir(state.cwd);
    }
    if (reply.code === "ALREADY_EXISTS") {
      overwrite = true;
      create.textContent = "Перезаписать";
      create.className = "danger solid";
    }
    error.textContent = `${reply.code}: ${reply.message}`;
  });
  create.addEventListener("click", submit);
  name.addEventListener("input", () => {
    overwrite = false;
    create.textContent = "Создать";
    create.className = "primary";
  });
  openDialog({
    title: `Новый файл в /${state.cwd}`,
    body: [el("div", { class: "field-row" }, field("Имя файла", name), field("Тип", kind)), field("Содержимое", area), error],
    actions: [el("button", { type: "button", text: "Отмена", onclick: closeDialog }), create],
  });
  name.focus();
}

function newDirDialog() {
  const name = el("input", { placeholder: "documents", autocomplete: "off" });
  const error = dialogError();
  const create = el("button", { type: "button", class: "primary", text: "Создать" });
  const submit = () => busy(create, async () => {
    if (!name.value.trim()) return void (error.textContent = "Введите имя папки");
    const reply = await api("mkdir", { path: joinPath(state.cwd, name.value.trim()) });
    if (reply.status !== "ok") return void (error.textContent = `${reply.code}: ${reply.message}`);
    toast(`Создана папка /${reply.result.path}`);
    closeDialog();
    loadDir(state.cwd);
  });
  create.addEventListener("click", submit);
  openDialog({
    title: `Новая папка в /${state.cwd}`,
    body: [field("Имя папки", name), error],
    actions: [el("button", { type: "button", text: "Отмена", onclick: closeDialog }), create],
    primary: submit,
  });
  name.focus();
}

// --- загрузка с компьютера

async function uploadFiles(files) {
  for (const file of files) {
    if (file.size > MAX_FILE_SIZE) {
      toast(`${file.name}: больше 256 КБ, сервер такой файл не примет`, { error: true, code: "FILE_TOO_LARGE" });
      continue;
    }
    let content;
    try {
      content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    } catch {
      toast(`${file.name}: не текст в UTF-8. Протокол передаёт только текстовые файлы.`, { error: true, code: "NOT_TEXT" });
      continue;
    }
    const path = joinPath(state.cwd, file.name);
    let reply = await api("write", { path, content, overwrite: false });
    if (reply.code === "ALREADY_EXISTS") {
      const replace = await confirmDialog({
        title: "Файл уже есть",
        message: `Файл /${path} уже существует. Заменить его загруженным?`,
        confirmText: "Заменить",
      });
      if (!replace) continue;
      reply = await api("write", { path, content, overwrite: true });
    }
    if (reply.status === "ok") toast(`Загружен: /${reply.result.path} (${humanSize(reply.result.size)})`);
    else toastError(reply);
  }
  loadDir(state.cwd);
}

// --- ZIP

function zipDialog() {
  const sources = [...state.selected];
  const first = baseName(sources[0]);
  const name = el("input", { value: sources.length === 1 ? `${stem(first)}.zip` : "архив.zip", autocomplete: "off" });
  const error = dialogError();
  let overwrite = false;
  const create = el("button", { type: "button", class: "primary", text: "Упаковать" });
  const submit = () => busy(create, async () => {
    let archive = name.value.trim();
    if (!archive) return void (error.textContent = "Введите имя архива");
    if (extension(archive) !== "zip") archive += ".zip";
    const reply = await api("zip", { archive: joinPath(state.cwd, archive), sources, overwrite });
    if (reply.status === "ok") {
      toast(`Архив /${reply.result.path}: файлов ${reply.result.files}, ${humanSize(reply.result.size)}`);
      closeDialog();
      return loadDir(state.cwd);
    }
    if (reply.code === "ALREADY_EXISTS") {
      overwrite = true;
      create.textContent = "Перезаписать";
      create.className = "danger solid";
    }
    error.textContent = `${reply.code}: ${reply.message}`;
  });
  create.addEventListener("click", submit);
  openDialog({
    title: "Упаковать в ZIP",
    body: [
      field("Имя архива", name),
      el("p", { class: "note", text: "Будут упакованы (папки — со всем содержимым):" }),
      el("pre", { class: "view-box", text: sources.map((s) => `/${s}`).join("\n") }),
      error,
    ],
    actions: [el("button", { type: "button", text: "Отмена", onclick: closeDialog }), create],
    primary: submit,
  });
}

async function openZip(path) {
  const reply = await api("zip_list", { archive: path });
  if (reply.status !== "ok") return toastError(reply);
  const rows = reply.result.entries.map((entry) => el("tr", {},
    el("td", { class: "mono", text: entry.name }),
    el("td", { class: "col-size", text: entry.is_dir ? "папка" : humanSize(entry.size) }),
    el("td", { class: "col-size", text: entry.is_dir ? "" : humanSize(entry.compressed) })));
  openDialog({
    title: `Архив /${path}`,
    body: [el("div", { class: "table-wrap" }, el("table", { class: "grid" },
      el("thead", {}, el("tr", {}, el("th", { text: "Имя в архиве" }), el("th", { class: "col-size", text: "Размер" }), el("th", { class: "col-size", text: "Сжато" }))),
      el("tbody", {}, rows.length ? rows : el("tr", { class: "empty" }, el("td", { colspan: "3", text: "Архив пуст" })))))],
    actions: [
      el("button", { type: "button", text: "Закрыть", onclick: closeDialog }),
      el("button", { type: "button", class: "primary", text: "Распаковать…", onclick: () => unzipDialog(path) }),
    ],
  });
}

function unzipDialog(path) {
  const destination = el("input", { value: joinPath(parentPath(path), stem(baseName(path))), autocomplete: "off" });
  const error = dialogError();
  const run = el("button", { type: "button", class: "primary", text: "Распаковать" });
  const submit = () => busy(run, async () => {
    const reply = await api("unzip", { archive: path, destination: destination.value.trim() });
    if (reply.status !== "ok") return void (error.textContent = `${reply.code}: ${reply.message}`);
    toast(`Распаковано в /${reply.result.destination}: файлов ${reply.result.files.length}`);
    closeDialog();
    loadDir(state.cwd);
  });
  run.addEventListener("click", submit);
  openDialog({
    title: `Распаковать /${path}`,
    body: [
      field("Папка назначения (будет создана)", destination),
      el("p", { class: "note", text: "Сервер распаковывает только в новую папку и проверяет архив: выход записей за папку, ссылки, zip-бомбы." }),
      error,
    ],
    actions: [el("button", { type: "button", text: "Отмена", onclick: closeDialog }), run],
    primary: submit,
  });
}

// ---------------------------------------------------------------- диски

async function loadDrives() {
  const reply = await api("drives");
  if (reply.status !== "ok") return toastError(reply);
  const cards = reply.result.map((drive) => {
    const title = drive.label ? `${drive.name} «${drive.label}»` : drive.name;
    const rows = [["Тип", drive.type], ["Файловая система", drive.filesystem || "—"]];
    let meter = null;
    if (drive.ready && drive.total_size) {
      const used = drive.total_size - drive.total_free;
      const fill = el("span", {});
      fill.style.width = `${Math.min(100, Math.max(0, (used / drive.total_size) * 100)).toFixed(1)}%`;
      meter = el("div", { class: "meter", role: "img", "aria-label": `Занято ${humanSize(used)} из ${humanSize(drive.total_size)}` }, fill);
      rows.push(["Объём", humanSize(drive.total_size)], ["Свободно", humanSize(drive.total_free)], ["Доступно серверу", humanSize(drive.available_free)]);
    } else {
      rows.push(["Состояние", "не готов"]);
    }
    return el("article", { class: "drive" }, el("h3", { text: title }), meter,
      el("dl", {}, ...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])));
  });
  $("drives-list").replaceChildren(...(cards.length ? cards : [el("p", { class: "note", text: "Дисков не найдено" })]));
}

// ---------------------------------------------------------------- журналы

async function loadLog() {
  const reply = await api("log", { limit: Number($("log-limit").value) });
  if (reply.status !== "ok") return toastError(reply);
  const rows = reply.result.map((entry) => el("tr", { class: entry.success ? "" : "failed" },
    el("td", { class: "mono", text: String(entry.timestamp).slice(0, 19) }),
    el("td", { text: entry.action }),
    el("td", { class: "mono", text: entry.target || "" }),
    el("td", { class: entry.success ? "ok" : "fail", text: entry.success ? "успех" : "отказ" }),
    el("td", { class: "mono", text: entry.details || "" }),
    el("td", { class: "mono", text: entry.peer || "" })));
  $("log-body").replaceChildren(...(rows.length ? rows : [el("tr", { class: "empty" }, el("td", { colspan: "6", text: "Журнал пуст" }))]));
}

async function loadOps() {
  const args = { limit: Number($("ops-limit").value) };
  const path = $("ops-path").value.trim().replace(/^\/+/, "");
  if (path) args.path = path;
  const reply = await api("ops", args);
  if (reply.status !== "ok") return toastError(reply);
  const rows = reply.result.map((entry) => el("tr", {},
    el("td", { class: "mono", text: String(entry.timestamp).slice(0, 19) }),
    el("td", { text: OPERATION_NAMES[entry.operation_type] || entry.operation_type }),
    el("td", { class: "mono", text: `/${entry.location}` }),
    el("td", { class: "mono", text: entry.file_id ? `№${entry.file_id}` : "—" })));
  $("ops-body").replaceChildren(...(rows.length ? rows : [el("tr", { class: "empty" }, el("td", { colspan: "4", text: "Операций нет" }))]));
}

// ---------------------------------------------------------------- запуск

function bind() {
  $("tab-login").addEventListener("click", () => setLoginMode("login"));
  $("tab-register").addEventListener("click", () => setLoginMode("register"));
  $("login-form").addEventListener("submit", submitLogin);
  $("logout-button").addEventListener("click", logout);
  for (const item of document.querySelectorAll(".nav-item")) item.addEventListener("click", () => showView(item.dataset.view));

  $("btn-up").addEventListener("click", () => loadDir(parentPath(state.cwd)));
  $("btn-refresh").addEventListener("click", () => loadDir(state.cwd));
  $("btn-new-file").addEventListener("click", newFileDialog);
  $("btn-new-dir").addEventListener("click", newDirDialog);
  $("btn-upload").addEventListener("click", () => $("upload-input").click());
  $("upload-input").addEventListener("change", (event) => {
    const files = [...event.target.files];
    event.target.value = "";
    if (files.length) uploadFiles(files);
  });
  $("btn-zip").addEventListener("click", zipDialog);
  $("select-all").addEventListener("change", (event) => {
    const checked = event.target.checked;
    state.selected.clear();
    for (const box of $("files-body").querySelectorAll("input[type=checkbox]")) box.checked = checked;
    if (checked) for (const entry of state.entries) state.selected.add(joinPath(state.cwd, entry.name));
    updateSelection();
  });

  $("drives-refresh").addEventListener("click", loadDrives);
  $("log-refresh").addEventListener("click", loadLog);
  $("log-limit").addEventListener("change", loadLog);
  $("ops-refresh").addEventListener("click", loadOps);
  $("ops-limit").addEventListener("change", loadOps);
  $("ops-path").addEventListener("keydown", (event) => { if (event.key === "Enter") loadOps(); });

  $("dialog-close").addEventListener("click", closeDialog);
  $("dialog-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (dialogPrimary) dialogPrimary();
  });
  dialog.addEventListener("close", () => { dialogPrimary = null; });
}

async function init() {
  bind();
  setLoginMode("login");
  const reply = await api("whoami");
  if (reply.status === "ok") enterApp(reply.result.username);
  else showLogin();
}

init();
