const API_ROOT = "https://api.github.com";

function settings(env) {
  const values = {
    // These identifiers are intentionally non-secret defaults.  Keeping them
    // here means the Cloudflare dashboard needs only one protected setting:
    // the fine-grained GitHub workflow token.
    owner: env.GITHUB_OWNER || "yuling168",
    repository: env.GITHUB_REPOSITORY || "cb-radar",
    workflow: env.GITHUB_WORKFLOW || "daily-collector.yml",
    ref: env.GITHUB_REF || "main",
    token: env.GITHUB_WORKFLOW_TOKEN,
  };
  for (const [name, value] of Object.entries(values)) {
    if (!value) throw new Error(`Missing Cloudflare Worker setting: ${name}`);
  }
  return values;
}

function headers(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": "cb-radar-external-scheduler",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function activeRuns(config) {
  const endpoint = `${API_ROOT}/repos/${config.owner}/${config.repository}` +
    `/actions/workflows/${config.workflow}/runs`;
  const requests = ["queued", "in_progress"].map(async (status) => {
    const response = await fetch(`${endpoint}?status=${status}&per_page=1`, {
      headers: headers(config.token),
    });
    if (!response.ok) throw new Error(`GitHub run check failed: ${response.status}`);
    const payload = await response.json();
    return payload.total_count > 0;
  });
  return (await Promise.all(requests)).some(Boolean);
}

async function dispatch(config) {
  if (await activeRuns(config)) {
    console.log("Skipped dispatch because CB Daily Collector is already queued or running.");
    return "skipped";
  }
  const endpoint = `${API_ROOT}/repos/${config.owner}/${config.repository}` +
    `/actions/workflows/${config.workflow}/dispatches`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { ...headers(config.token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: config.ref, inputs: {} }),
  });
  if (response.status !== 204) throw new Error(`GitHub workflow dispatch failed: ${response.status}`);
  console.log("Dispatched CB Daily Collector.");
  return "dispatched";
}

export default {
  async scheduled(_event, env, context) {
    context.waitUntil(dispatch(settings(env)));
  },
  async fetch(_request, env) {
    // This endpoint is intentionally minimal: it permits a manual health check
    // but never dispatches a workflow outside the configured Cron Triggers.
    settings(env);
    return new Response("CB Radar scheduler is configured.", { status: 200 });
  },
};
