const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};
const COOLDOWN_SECONDS = 10 * 60;
const COOLDOWN_COOKIE = "league_refresh_cooldown";

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...extraHeaders },
  });
}

function hasCooldownCookie(request) {
  const cookie = request.headers.get("Cookie") || "";
  return cookie.split(";").some((part) => part.trim().startsWith(`${COOLDOWN_COOKIE}=`));
}

async function rateLimitCacheKey(request) {
  const clientIp = request.headers.get("CF-Connecting-IP") || "unknown";
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(clientIp));
  const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return new Request(`https://league-refresh-rate-limit.invalid/${hash}`, { method: "GET" });
}

async function isIpCoolingDown(request) {
  try {
    const key = await rateLimitCacheKey(request);
    return Boolean(await caches.default.match(key));
  } catch {
    return false;
  }
}

async function setIpCooldown(request) {
  try {
    const key = await rateLimitCacheKey(request);
    await caches.default.put(
      key,
      new Response("1", {
        headers: { "Cache-Control": `public, max-age=${COOLDOWN_SECONDS}` },
      }),
    );
    return key;
  } catch {
    return null;
  }
}

async function clearIpCooldown(key) {
  if (!key) return;
  try {
    await caches.default.delete(key);
  } catch {
    // The browser cookie still prevents accidental repeated clicks.
  }
}

async function githubRequest(url, token, init = {}) {
  return fetch(url, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2026-03-10",
      "User-Agent": "league-rank-pages-function",
      ...(init.headers || {}),
    },
  });
}

export async function onRequest({ request, env }) {
  if (request.method !== "POST") {
    return jsonResponse({ detail: "POST 요청만 허용됩니다." }, 405);
  }

  const requestOrigin = request.headers.get("Origin");
  if (requestOrigin !== new URL(request.url).origin) {
    return jsonResponse({ detail: "웹페이지의 직접 갱신 버튼을 이용해주세요." }, 403);
  }

  const required = ["GITHUB_ACTIONS_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"];
  const missing = required.filter((name) => !String(env[name] || "").trim());
  if (missing.length) {
    return jsonResponse(
      { detail: `직접 갱신 서버 설정 누락: ${missing.join(", ")}` },
      503,
    );
  }

  if (hasCooldownCookie(request) || await isIpCoolingDown(request)) {
    return jsonResponse(
      { detail: "직접 갱신은 한 기기·IP에서 10분에 한 번만 요청할 수 있습니다." },
      429,
      { "Retry-After": String(COOLDOWN_SECONDS) },
    );
  }

  let body = {};
  try {
    body = await request.json();
  } catch {
    body = {};
  }
  const group = ["all", "A", "B", "C", "D"].includes(body.group) ? body.group : "all";
  const workflow = env.GITHUB_WORKFLOW_FILE || "refresh-and-deploy.yml";
  const branch = env.GITHUB_BRANCH || "main";
  const base = `https://api.github.com/repos/${encodeURIComponent(env.GITHUB_OWNER)}/${encodeURIComponent(env.GITHUB_REPO)}/actions/workflows/${encodeURIComponent(workflow)}`;

  for (const status of ["queued", "in_progress"]) {
    const active = await githubRequest(`${base}/runs?status=${status}&per_page=1`, env.GITHUB_ACTIONS_TOKEN);
    if (!active.ok) {
      return jsonResponse({ detail: "GitHub 실행 상태를 확인하지 못했습니다." }, 502);
    }
    const payload = await active.json();
    if (Array.isArray(payload.workflow_runs) && payload.workflow_runs.length) {
      return jsonResponse({ detail: "이미 갱신 작업이 실행 또는 대기 중입니다." }, 409);
    }
  }

  const cooldownKey = await setIpCooldown(request);
  const dispatched = await githubRequest(`${base}/dispatches`, env.GITHUB_ACTIONS_TOKEN, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref: branch, inputs: { group } }),
  });
  if (!dispatched.ok) {
    await clearIpCooldown(cooldownKey);
    return jsonResponse({ detail: "GitHub 갱신 작업을 시작하지 못했습니다." }, 502);
  }
  return jsonResponse(
    { detail: "직접 갱신을 요청했습니다." },
    202,
    {
      "Set-Cookie": `${COOLDOWN_COOKIE}=1; Max-Age=${COOLDOWN_SECONDS}; Path=/; Secure; HttpOnly; SameSite=Strict`,
    },
  );
}
