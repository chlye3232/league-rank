const grid = document.querySelector("#league-grid");
const updatedAt = document.querySelector("#updated-at");
const nextRefresh = document.querySelector("#next-refresh");
const refreshState = document.querySelector("#refresh-state");
const copyAllButton = document.querySelector("#copy-all");
const totalCount = document.querySelector("#total-count");
const refreshButton = document.querySelector("#refresh-button");
const toast = document.querySelector("#toast");

let rankingData = null;
let toastTimer = null;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
}

function formatDate(value) {
  if (!value) return "없음";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
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
  rankingData = data;
  updatedAt.textContent = formatDate(data.updatedAt);
  nextRefresh.textContent = `다음 ${formatDate(data.refresh.nextRefreshAt)}`;
  refreshState.textContent = "저장 데이터 사용 중";
  refreshState.style.color = "#147d64";
  refreshButton.disabled = false;
  refreshButton.textContent = "저장 데이터 다시 불러오기";

  const allIds = uniqueIds(data.leagues);
  copyAllButton.disabled = false;
  totalCount.textContent = `${allIds.length.toLocaleString("ko-KR")}개`;
  copyAllButton.onclick = () => copyIds(allIds, "전체");

  grid.replaceChildren();
  data.leagues.forEach((league) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "league-button";
    button.setAttribute("aria-label", `${league.code} 리그 ID ${league.count}개 복사`);
    button.innerHTML = `<span class="league-code">${league.code}</span><span class="league-count">${league.count} IDs</span>`;
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
    refreshState.textContent = "데이터 확인 필요";
    refreshState.style.color = "#b42318";
  }
}

refreshButton.addEventListener("click", async () => {
  refreshButton.disabled = true;
  try {
    await loadRankings();
    showToast("최신 저장 데이터를 다시 불러왔습니다.");
  } finally {
    refreshButton.disabled = false;
  }
});

loadRankings();
setInterval(() => loadRankings({ quiet: true }), 300_000);
