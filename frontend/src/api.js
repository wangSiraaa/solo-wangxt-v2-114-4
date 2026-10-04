const BASE = "/api";

async function get(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `${path}: ${res.status}`);
    err.status = res.status;
    err.data = data;            // batch receipts travel even on 409/422
    throw err;
  }
  return data;
}

export const api = {
  plots: () => get("/plots/"),
  measurements: (campaign) =>
    get(`/measurements/?campaign=${encodeURIComponent(campaign)}`),
  campaigns: () => get("/campaigns/"),
  equations: () => get("/equations/"),
  conflicts: (status) =>
    get(`/conflicts/${status ? `?status=${status}` : ""}`),
  resolveConflict: (id, payload) =>
    post(`/conflicts/${id}/resolve/`, payload),
  estimates: () => get("/estimates/"),
  estimate: (id) => get(`/estimates/${id}/`),
  createEstimate: (payload) => post("/estimates/", payload),
  confirmEstimate: (id) => post(`/estimates/${id}/confirm/`),

  // ---- resumable survey batch sync ----
  surveys: (campaign) =>
    get(`/surveys/${campaign ? `?campaign=${encodeURIComponent(campaign)}` : ""}`),
  finalizeSurvey: (id) => post(`/surveys/${id}/finalize/`),
  batches: (params = {}) => {
    const q = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v)).toString();
    return get(`/batches/${q ? `?${q}` : ""}`);
  },
  batch: (id) => get(`/batches/${id}/`),
  submitBatch: (payload) => post("/batches/", payload),
  applyBatch: (id) => post(`/batches/${id}/apply/`),
  batchConflicts: (status) =>
    get(`/batch-conflicts/${status ? `?status=${status}` : ""}`),
  resolveBatchConflict: (id, payload) =>
    post(`/batch-conflicts/${id}/resolve/`, payload),
  importRows: (params = {}) => {
    const q = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v)).toString();
    return get(`/import-rows/${q ? `?${q}` : ""}`);
  },
};
