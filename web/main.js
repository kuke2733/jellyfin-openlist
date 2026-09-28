const form = document.querySelector("#form");
const upstreamInput = document.querySelector("#upstream");
const rulesEl = document.querySelector("#rules");
const emptyEl = document.querySelector("#empty");
const ignoreCase = document.querySelector("#ignore-case");
const saveButton = document.querySelector("#save");
const statusEl = document.querySelector("#status");

let meta = { version: 1, comment: "" };

function setStatus(text, isError) {
  statusEl.textContent = text || "";
  statusEl.classList.toggle("error", Boolean(isError));
}

function syncEmpty() {
  emptyEl.hidden = rulesEl.children.length > 0;
}

function addRule(rule) {
  const row = document.createElement("div");
  row.className = "rule";

  const local = field("本地路径", "local", "/媒体库/电影");
  const base = field("OpenList 前缀", "base", "https://openlist.example:5244/d/媒体库/电影");
  const note = field("备注", "note", "可空");
  note.classList.add("span");

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove";
  remove.textContent = "删除";
  remove.addEventListener("click", () => {
    row.remove();
    syncEmpty();
  });

  local.querySelector("input").value = rule?.localPath || "";
  base.querySelector("input").value = rule?.urlBase || "";
  note.querySelector("input").value = rule?.comment || "";

  const grid = document.createElement("div");
  grid.className = "rule-grid";
  grid.append(local, base, remove);
  row.append(grid, note);
  rulesEl.append(row);
  syncEmpty();
  return row;
}

function field(label, className, placeholder) {
  const wrap = document.createElement("label");
  wrap.className = "field";
  const caption = document.createElement("span");
  caption.textContent = label;
  const input = document.createElement("input");
  input.className = className;
  input.type = "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = placeholder;
  wrap.append(caption, input);
  return wrap;
}

function collectRules() {
  const rules = [];
  for (const row of rulesEl.querySelectorAll(".rule")) {
    rules.push({
      localPath: row.querySelector(".local").value.trim(),
      urlBase: row.querySelector(".base").value.trim(),
      comment: row.querySelector("input.note").value.trim(),
    });
  }
  return rules;
}

async function load() {
  setStatus("");
  const response = await fetch("/api/mapping");
  if (!response.ok) {
    setStatus("读取配置失败", true);
    return;
  }
  const data = await response.json();
  meta = { version: data.version || 1, comment: data.comment || "" };
  upstreamInput.value = data.upstream || "";
  ignoreCase.checked = Boolean(data.pathRulesCaseInsensitive);
  rulesEl.replaceChildren();
  for (const rule of data.pathRules || []) addRule(rule);
  syncEmpty();
  if (data.parseError) {
    setStatus(`当前文件无法解析（${data.parseError}）。保存会覆盖它。`, true);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus("");
  saveButton.disabled = true;
  try {
    const response = await fetch("/api/mapping", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        version: meta.version,
        comment: meta.comment,
        upstream: upstreamInput.value.trim(),
        pathRulesCaseInsensitive: ignoreCase.checked,
        pathRules: collectRules(),
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      setStatus(data.error || "保存失败", true);
      return;
    }
    upstreamInput.value = data.upstream || upstreamInput.value.trim();
    if (data.restarted) setStatus("已保存。反代已切换上游。");
    else if (data.restart) setStatus("已保存。更改上游地址后需要重启。");
    else setStatus("已保存。");
  } catch {
    setStatus("保存失败", true);
  } finally {
    saveButton.disabled = false;
  }
});

document.querySelector("#add").addEventListener("click", () => {
  const row = addRule();
  row.querySelector(".local").focus();
});

load().catch(() => setStatus("读取配置失败", true));
