const grid = document.querySelector("#league-grid");
const copyAllButton = document.querySelector("#copy-all");
const manualRefreshButton = document.querySelector("#manual-refresh");
const toast = document.querySelector("#toast");

let toastTimer = null;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
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
  try {
    const response = await fetch(`./data/rankings.json?v=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).detail || "데이터를 불러오지 못했습니다.");
    render(await response.json());
  } catch (error) {
    if (!quiet) showToast(error.message);
  }
}

async function requestManualRefresh() {
  let refreshKey = sessionStorage.getItem("leagueRefreshKey") || "";
  if (!refreshKey) {
    refreshKey = window.prompt("내부용 갱신키를 입력하세요.")?.trim() || "";
  }
  if (!refreshKey) return;

  manualRefreshButton.disabled = true;
  manualRefreshButton.textContent = "요청 중…";
  try {
    const response = await fetch("/api/refresh", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Refresh-Key": refreshKey,
      },
      body: JSON.stringify({ group: "all" }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401) sessionStorage.removeItem("leagueRefreshKey");
      throw new Error(result.detail || "직접 갱신을 요청하지 못했습니다.");
    }
    sessionStorage.setItem("leagueRefreshKey", refreshKey);
    showToast(result.detail || "직접 갱신을 요청했습니다.");
  } catch (error) {
    showToast(error.message);
  } finally {
    manualRefreshButton.disabled = false;
    manualRefreshButton.textContent = "직접 갱신";
  }
}

manualRefreshButton.addEventListener("click", requestManualRefresh);
loadRankings();
