const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function constantTimeEqual(left, right) {
  const a = new TextEncoder().encode(left || "");
  const b = new TextEncoder().encode(right || "");
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index] || 0) ^ (b[index] || 0);
  }
  return difference === 0;
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

  const required = ["REFRESH_KEY", "GITHUB_ACTIONS_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"];
  if (required.some((name) => !env[name])) {
    return jsonResponse({ detail: "직접 갱신 서버 설정이 완료되지 않았습니다." }, 503);
  }
  if (!constantTimeEqual(request.headers.get("X-Refresh-Key"), env.REFRESH_KEY)) {
    return jsonResponse({ detail: "갱신키가 올바르지 않습니다." }, 401);
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

  const dispatched = await githubRequest(`${base}/dispatches`, env.GITHUB_ACTIONS_TOKEN, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref: branch, inputs: { group } }),
  });
  if (!dispatched.ok) {
    return jsonResponse({ detail: "GitHub 갱신 작업을 시작하지 못했습니다." }, 502);
  }
  return jsonResponse({ detail: "직접 갱신을 요청했습니다." }, 202);
}
