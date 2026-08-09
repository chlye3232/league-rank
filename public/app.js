const grid = document.querySelector("#league-grid");
const copyAllButton = document.querySelector("#copy-all");
const manualRefreshButton = document.querySelector("#manual-refresh");
const refreshGrid = document.querySelector("#refresh-grid");
const refreshSelectAllButton = document.querySelector("#refresh-select-all");
const refreshClearAllButton = document.querySelector("#refresh-clear-all");
const refreshSelectionCount = document.querySelector("#refresh-selection-count");
const toast = document.querySelector("#toast");
const LEAGUE_CODES = [
  "A1", "A2", "A3",
  "B1", "B2", "B3", "B4", "B5",
  "C1", "C2", "C3", "C4", "C5",
  "D1", "D2", "D3", "D4", "D5",
];

let toastTimer = null;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
}

function selectedRefreshLeagues() {
  return Array.from(refreshGrid.querySelectorAll("input:checked"), (input) => input.value);
}

function updateRefreshControls() {
  const count = selectedRefreshLeagues().length;
  refreshSelectionCount.textContent = `${count}개 선택`;
  manualRefreshButton.disabled = count === 0;
  manualRefreshButton.textContent = count
    ? `선택한 ${count}개 리그 갱신`
    : "갱신할 리그를 선택하세요";
}

function setRefreshOptions(checked) {
  refreshGrid.querySelectorAll("input").forEach((input) => {
    input.checked = checked;
  });
  updateRefreshControls();
}

function setRefreshOptionsDisabled(disabled) {
  refreshGrid.querySelectorAll("input").forEach((input) => {
    input.disabled = disabled;
  });
  refreshSelectAllButton.disabled = disabled;
  refreshClearAllButton.disabled = disabled;
}

function renderRefreshOptions() {
  LEAGUE_CODES.forEach((code) => {
    const label = document.createElement("label");
    label.className = "refresh-option";

    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = code;
    input.setAttribute("aria-label", `${code} 리그 갱신 선택`);
    input.addEventListener("change", updateRefreshControls);

    const text = document.createElement("span");
    text.textContent = code;
    label.append(input, text);
    refreshGrid.append(label);
  });
  updateRefreshControls();
}

function uniqueIds(leagues) {
  const seen = new Set();
  const result = [];
  leagues.forEach((league) => {
    league.items.forEach((item) => {
      const id = String(item.id || "").trim();
      const key = id.toLowerCase();
      if (id && !seen.has(key)) {
        seen.add(key);
        result.push(id);
      }
    });
  });
  return result;
}

async function copyIds(ids, label) {
  if (!ids.length) {
    showToast("복사할 ID가 없습니다.");
    return;
  }
  try {
    await navigator.clipboard.writeText(ids.join("\n"));
    showToast(`${label} ${ids.length}개를 복사했습니다.`);
  } catch {
    const area = document.createElement("textarea");
    area.value = ids.join("\n");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    showToast(`${label} ${ids.length}개를 복사했습니다.`);
  }
}

function render(data) {
  const allIds = uniqueIds(data.leagues);
  copyAllButton.disabled = false;
  copyAllButton.onclick = () => copyIds(allIds, "전체");

  grid.replaceChildren();
  data.leagues.forEach((league) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "league-button";
    button.setAttribute("aria-label", `${league.code} 리그 ID ${league.count}개 복사`);
    button.textContent = league.code;
    button.addEventListener("click", () => copyIds(uniqueIds([league]), league.code));
    grid.append(button);
  });
  grid.setAttribute("aria-busy", "false");
}

async function loadRankings({ quiet = false } = {}) {
  const sources = [
    `/api/rankings?v=${Date.now()}`,
    `./data/rankings.json?v=${Date.now()}`,
  ];
  let lastError = new Error("데이터를 불러오지 못했습니다.");

  try {
    for (const source of sources) {
      try {
        const response = await fetch(source, { cache: "no-store" });
        if (!response.ok) {
          let detail = "데이터를 불러오지 못했습니다.";
          try {
            detail = (await response.json()).detail || detail;
          } catch {
            // Use the generic message and try the fallback source.
          }
          throw new Error(detail);
        }
        render(await response.json());
        return;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError;
  } catch (error) {
    if (!quiet) showToast(error.message);
  }
}

async function requestManualRefresh() {
  const leagues = selectedRefreshLeagues();
  if (!leagues.length) {
    showToast("갱신할 리그를 하나 이상 선택해주세요.");
    return;
  }

  manualRefreshButton.disabled = true;
  manualRefreshButton.textContent = "요청 중…";
  setRefreshOptionsDisabled(true);
  try {
    const response = await fetch("/api/refresh", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ leagues }),
    });
    const responseText = await response.text();
    let result = {};
    try {
      result = JSON.parse(responseText);
    } catch {
      result = {};
    }
    if (!response.ok) {
      const fallback = responseText && responseText.length <= 180
        ? responseText.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim()
        : `직접 갱신 서버 HTTP ${response.status}`;
      throw new Error(result.detail || fallback || "직접 갱신을 요청하지 못했습니다.");
    }
    showToast(result.detail || "직접 갱신을 요청했습니다.");
  } catch (error) {
    showToast(error.message);
  } finally {
    setRefreshOptionsDisabled(false);
    updateRefreshControls();
  }
}

refreshSelectAllButton.addEventListener("click", () => setRefreshOptions(true));
refreshClearAllButton.addEventListener("click", () => setRefreshOptions(false));
manualRefreshButton.addEventListener("click", requestManualRefresh);
renderRefreshOptions();
loadRankings();
