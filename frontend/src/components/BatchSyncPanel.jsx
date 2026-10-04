import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const STATUS_LABEL = {
  staged: "staged (retryable)",
  validated: "validated (retryable)",
  applied: "applied",
  blocked: "blocked — needs review",
  failed: "failed validation",
  discarded: "discarded by reviewer",
};

const EXAMPLE_ROWS = `[
  {"plot": "P01", "field_number": "301", "species": "OAK",
   "x_m": 500012.0, "y_m": 4000012.0, "status": "alive_measured",
   "dbh_raw": 232, "dbh_unit": "mm", "height_raw": 15.1, "height_unit": "m"}
]`;

/**
 * Survey-batch sync console for offline field tablets.
 *
 * Submit is idempotent on (campaign, client key): an identical re-send
 * returns the stored receipt without writing again; the same key with a
 * different payload is refused. Batches overlapping first-arrived tree
 * records are blocked here for human review — they can only be discarded,
 * never forced over the earlier data.
 */
export default function BatchSyncPanel({ ctx }) {
  const { campaigns } = ctx;
  const [campaign, setCampaign] = useState("");
  const [clientKey, setClientKey] = useState("");
  const [rowsText, setRowsText] = useState("");
  const [progress, setProgress] = useState([]);
  const [batches, setBatches] = useState([]);
  const [blocked, setBlocked] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [result, setResult] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    const [p, b, c] = await Promise.all([
      api.batchProgress(), api.batches(), api.batchConflicts()]);
    setProgress(p);
    setBatches(b);
    setBlocked(c);
  }
  useEffect(() => { load().catch((e) => setErr(e.message)); }, []);

  useEffect(() => {
    if (!campaign && campaigns.length) setCampaign(campaigns[0].code);
  }, [campaigns]);

  useEffect(() => {
    if (openId == null) return;
    api.batch(openId).then(setDetail).catch((e) => setErr(e.message));
  }, [openId, batches]);

  async function submit() {
    setBusy(true); setErr(""); setResult(null);
    try {
      let rows;
      try {
        rows = JSON.parse(rowsText);
      } catch {
        throw new Error("rows must be a JSON array of field rows");
      }
      const r = await api.submitBatch({ campaign, client_key: clientKey, rows });
      setResult(r);
      await load();
      setOpenId(r.batch.id);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function resume(b) {
    setBusy(true); setErr("");
    try {
      const r = await api.applyBatch(b.id);
      setResult(r);
      await load();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function discard(b) {
    const note = window.prompt(
      `Discard batch ${b.client_key}? First-arrived data is kept. Note:`,
      "duplicate / superseded upload");
    if (note === null) return;
    setBusy(true); setErr("");
    try {
      await api.resolveBatch(b.id, { decision: "discard", note });
      await load();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div>
      <h2>Survey batch sync <small>resumable, idempotent upload</small></h2>
      <p className="hint">
        Offline tablets upload one survey in batches. Each batch is validated
        as a whole and applied atomically; re-sending the same key and
        payload returns the original receipt. Death, ingrowth and identity
        conflicts are computed only once the adjacent survey's batches are
        complete.
      </p>
      {err && <div className="error">{err}</div>}

      <section className="eq-picker">
        <h3>Submit a batch</h3>
        <div className="batch-form">
          <label>campaign
            <select value={campaign}
                    onChange={(e) => setCampaign(e.target.value)}>
              {campaigns.map((c) => (
                <option key={c.code} value={c.code}>{c.code}</option>))}
            </select>
          </label>
          <label>client key (idempotency)
            <input value={clientKey} placeholder="tablet-07-batch-003"
                   onChange={(e) => setClientKey(e.target.value)} />
          </label>
          <label>rows (JSON)
            <textarea rows={6} value={rowsText} placeholder={EXAMPLE_ROWS}
                      onChange={(e) => setRowsText(e.target.value)} />
          </label>
          <div>
            <button disabled={busy || !clientKey.trim() || !rowsText.trim()}
                    onClick={submit}>submit batch</button>
            <button onClick={() => setRowsText(EXAMPLE_ROWS)}>
              fill example</button>
          </div>
        </div>
        {result && (
          <div className={result.batch.status === "applied" ? "ok" : "error"}>
            {result.replayed
              ? "Identical batch already received — original receipt returned, nothing re-written."
              : `Batch ${result.batch.client_key}: ${STATUS_LABEL[result.batch.status]}.`}
            {result.conflicts?.length > 0 &&
              ` Identity scan found ${result.conflicts.length} conflict(s).`}
            {result.batch.status === "blocked" &&
              " Overlaps first-arrived data — see review queue below."}
          </div>
        )}
      </section>

      <section>
        <h3>Upload progress</h3>
        <table>
          <thead>
            <tr><th>campaign</th><th>batches</th><th>rows applied</th>
            <th>measurements</th><th>state</th></tr>
          </thead>
          <tbody>
            {progress.map((p) => (
              <tr key={p.campaign}>
                <td>{p.campaign}</td>
                <td>{p.n_batches}
                  {" "}({Object.entries(p.by_status)
                    .map(([s, n]) => `${n} ${s}`).join(", ") || "—"})</td>
                <td>
                  <div className="progress-bar">
                    <div className="progress-fill"
                         style={{ width: p.rows_total
                           ? `${(100 * p.rows_applied / p.rows_total).toFixed(0)}%`
                           : "0%" }} />
                    <span>{p.rows_applied}/{p.rows_total}</span>
                  </div>
                </td>
                <td>{p.n_measurements}</td>
                <td>{p.complete
                  ? <span className="locked">complete</span>
                  : <span className="status-draft">awaiting batches</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {blocked.length > 0 && (
        <section>
          <h3>Blocked batches — human review</h3>
          <p className="hint">
            These batches overlap tree records already applied for the same
            campaign. First-arrived data always wins; a blocked batch can
            only be discarded, never forced over it.
          </p>
          {blocked.map((b) => (
            <div key={b.id} className="blocked-card">
              <strong>{b.campaign_code} · {b.client_key}</strong>
              <span className="chip warn-chip">{b.overlaps?.length} overlap(s)</span>
              <table>
                <thead>
                  <tr><th>row</th><th>plot</th><th>number</th>
                  <th>first batch</th><th>reason</th></tr>
                </thead>
                <tbody>
                  {(b.overlaps || []).map((o) => (
                    <tr key={o.index}>
                      <td>#{o.index}</td><td>{o.plot}</td>
                      <td>{o.field_number}</td>
                      <td>{o.existing_batch || "legacy import"}</td>
                      <td>{o.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <button className="danger" disabled={busy}
                      onClick={() => discard(b)}>
                discard batch, keep first-arrived data</button>
            </div>
          ))}
        </section>
      )}

      <section>
        <h3>Batches</h3>
        <table className="version-table">
          <thead>
            <tr><th>id</th><th>campaign</th><th>client key</th>
            <th>status</th><th>rows</th><th>hash</th><th></th></tr>
          </thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.id} onClick={() => setOpenId(b.id)}>
                <td>#{b.id}</td>
                <td>{b.campaign_code}</td>
                <td>{b.client_key}</td>
                <td className={`batch-${b.status}`}>
                  {STATUS_LABEL[b.status]}</td>
                <td>{b.n_applied}/{b.n_rows}</td>
                <td><code>{b.content_hash.slice(0, 10)}…</code></td>
                <td onClick={(e) => e.stopPropagation()}>
                  {(b.status === "staged" || b.status === "validated") && (
                    <button disabled={busy} onClick={() => resume(b)}>
                      resume apply</button>)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail && <BatchReceipt batch={detail} />}
    </div>
  );
}

function BatchReceipt({ batch }) {
  const rows = batch.receipt?.rows || [];
  return (
    <section className="edition">
      <h3>Batch #{batch.id} — {batch.client_key}
        <span className={`badge batch-${batch.status}`}>
          {STATUS_LABEL[batch.status]}</span>
      </h3>
      {batch.error && <p className="error">{batch.error}</p>}
      {batch.resolution_note &&
        <p className="hint">review note: {batch.resolution_note}</p>}
      <table>
        <thead>
          <tr><th>row</th><th>plot</th><th>number</th><th>result</th>
          <th>tree</th><th>audit</th><th>detail</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.index}
                className={r.result === "rejected" ? "mortality" : "ingrowth"}>
              <td>#{r.index}</td>
              <td>{r.plot}</td>
              <td>{r.field_number}</td>
              <td>{r.result}{r.renumber ? " (renumber)" : ""}</td>
              <td>{r.tree_id ?? "—"}</td>
              <td>{r.audit_id ? `audit #${r.audit_id}` : "—"}</td>
              <td>{r.reason ||
                (r.dbh_cm != null
                  ? `dbh ${r.dbh_cm} cm · h ${r.height_m ?? "—"} m`
                  : "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
