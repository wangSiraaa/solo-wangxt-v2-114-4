import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const STATUS_LABEL = {
  staged: "staged",
  validated: "validated (retryable)",
  applied: "applied",
  blocked: "blocked — manual review",
  failed: "failed — resubmit new key",
};

function StatusChip({ value }) {
  return <span className={`batch-status st-${value}`}>{STATUS_LABEL[value] || value}</span>;
}

/**
 * Upload progress + per-row receipts for the resumable survey batch sync.
 *
 * Tablets submit batches with a client idempotency key; retransmissions
 * replay the stored receipt. Overlapping tree records never overwrite
 * earlier applied data — they wait here for a human decision.
 */
export default function UploadsPanel({ ctx }) {
  const { campaigns } = ctx;
  const [surveys, setSurveys] = useState([]);
  const [batches, setBatches] = useState([]);
  const [conflicts, setConflicts] = useState([]);
  const [openBatch, setOpenBatch] = useState(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    const [s, b, c] = await Promise.all([
      api.surveys(), api.batches(), api.batchConflicts(),
    ]);
    setSurveys(s); setBatches(b); setConflicts(c);
  }
  useEffect(() => { load().catch((e) => setErr(e.message)); }, []);

  async function run(fn, okMsg) {
    setBusy(true); setErr(""); setNote("");
    try {
      await fn();
      if (okMsg) setNote(okMsg);
      await load();
      if (openBatch) setOpenBatch(await api.batch(openBatch.id));
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const openConflicts = conflicts.filter((c) => c.status === "open");

  return (
    <div>
      <h2>Survey batch sync <small>offline tablets · idempotent, resumable</small></h2>
      <p className="hint">
        Each batch is staged and validated; only when every row passes are
        measurements written in one atomic transaction. Retransmitting a
        batch with the same key returns the original receipt without
        writing. A batch that contradicts already-applied records is
        blocked for manual review — earlier data is never overwritten
        automatically. Identity conflicts, mortality and ingrowth are
        computed only after the surveys of both adjacent campaigns are
        complete.
      </p>
      {err && <div className="error">{err}</div>}
      {note && <div className="ok">{note}</div>}

      <section>
        <h3>Surveys</h3>
        <table>
          <thead>
            <tr><th>survey</th><th>campaign</th><th>status</th>
            <th>batches applied</th><th>blocked</th><th>failed</th><th></th></tr>
          </thead>
          <tbody>
            {surveys.map((s) => (
              <tr key={s.id}>
                <td><strong>{s.code}</strong></td>
                <td>{s.campaign_code}</td>
                <td><StatusChip value={s.status} /></td>
                <td>{s.n_applied}/{s.expected_batches ?? s.n_batches}</td>
                <td>{s.n_blocked || ""}</td>
                <td>{s.n_failed || ""}</td>
                <td>
                  {s.status === "collecting" && (
                    <button disabled={busy}
                            onClick={() => run(() => api.finalizeSurvey(s.id),
                              `survey ${s.code} finalised — scan evaluated`)}>
                      finalise &amp; scan
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!surveys.length && (
              <tr><td colSpan="7" className="hint">no surveys uploaded yet</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <SubmitBatch campaigns={campaigns} onDone={load} />

      <section>
        <h3>Batches</h3>
        <table>
          <thead>
            <tr><th>#</th><th>key</th><th>survey</th><th>campaign</th>
            <th>status</th><th>rows</th><th>tries</th><th>note</th><th></th></tr>
          </thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.id} className={`batch-row st-${b.status}`}>
                <td>{b.id}</td>
                <td><code>{b.idempotency_key}</code></td>
                <td>{b.survey_code}</td>
                <td>{b.campaign_code}</td>
                <td><StatusChip value={b.status} /></td>
                <td>{b.row_count}</td>
                <td>{b.attempt_count}</td>
                <td className="hint">{b.error}</td>
                <td>
                  <button onClick={() => api.batch(b.id).then(setOpenBatch)}>
                    rows
                  </button>
                  {["validated", "staged", "blocked"].includes(b.status) && (
                    <button disabled={busy}
                            onClick={() => run(() => api.applyBatch(b.id),
                              `batch ${b.idempotency_key} applied`)}>
                      {b.status === "blocked" ? "continue apply" : "apply / retry"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!batches.length && (
              <tr><td colSpan="9" className="hint">no batches yet</td></tr>
            )}
          </tbody>
        </table>
      </section>

      {openBatch && (
        <BatchDetail batch={openBatch} onClose={() => setOpenBatch(null)} />
      )}

      <section>
        <h3>Overlapping tree records ({openConflicts.length} open)</h3>
        <p className="hint">
          A later batch claimed a tree record that an earlier batch already
          applied with different content. The earlier data stays untouched
          until a reviewer decides.
        </p>
        <table>
          <thead>
            <tr><th>batch</th><th>plot / number</th><th>incoming</th>
            <th>applied earlier</th><th>decision</th></tr>
          </thead>
          <tbody>
            {conflicts.map((c) => (
              <tr key={c.id} className={c.status === "open" ? "open" : "closed"}>
                <td><code>{c.idempotency_key}</code><br />
                  <small>{c.campaign_code}</small></td>
                <td>{c.plot}/{c.field_number}</td>
                <td><CanonicalView canon={c.detail.incoming} /></td>
                <td><CanonicalView canon={(c.detail.existing || [])[0]} /></td>
                <td>
                  {c.status === "open" ? (
                    <>
                      <button disabled={busy} onClick={() => run(
                        () => api.resolveBatchConflict(c.id,
                          { action: "keep_existing", note: "reviewed in UI" }),
                        "kept earlier record; re-apply the batch")}>
                        keep existing
                      </button>
                      <button disabled={busy} onClick={() => run(
                        () => api.resolveBatchConflict(c.id,
                          { action: "accept_incoming", note: "verified correction" }),
                        "accepted incoming as verified correction; re-apply the batch")}>
                        accept as correction
                      </button>
                      <button className="danger" disabled={busy} onClick={() => run(
                        () => api.resolveBatchConflict(c.id,
                          { action: "accept_as_new", note: "distinct individual" }),
                        "accepted as new individual; re-apply the batch")}>
                        new individual
                      </button>
                    </>
                  ) : (
                    <span className="locked">{c.status}</span>
                  )}
                  {c.resolution_note && <><br /><small>{c.resolution_note}</small></>}
                </td>
              </tr>
            ))}
            {!conflicts.length && (
              <tr><td colSpan="5" className="hint">no overlapping records</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function CanonicalView({ canon }) {
  if (!canon) return <span className="hint">—</span>;
  return (
    <small>
      {canon.species} · ({canon.x_m}, {canon.y_m}) · {canon.status}
      {canon.dbh_cm != null && <> · dbh {canon.dbh_cm} cm</>}
      {canon.height_m != null && <> · h {canon.height_m} m</>}
    </small>
  );
}

function BatchDetail({ batch, onClose }) {
  const r = batch.receipt || {};
  return (
    <section className="edition">
      <h3>
        Batch <code>{batch.idempotency_key}</code>{" "}
        <StatusChip value={batch.status} />
        <button style={{ float: "right" }} onClick={onClose}>close</button>
      </h3>
      <p className="hint">
        digest <code>{(batch.content_digest || "").slice(0, 16)}…</code> ·
        applied {r.n_applied ?? 0} · duplicates {r.n_duplicates ?? 0} ·
        rejected {r.n_rejected ?? 0} · conflicts {r.n_conflicts ?? 0}
        {r.scan && <> · scan: {r.scan.status}
          {r.scan.waiting_for?.length
            ? ` (waiting for ${r.scan.waiting_for.join(", ")})` : ""}</>}
      </p>
      <table>
        <thead>
          <tr><th>seq</th><th>plot</th><th>number</th><th>state</th>
          <th>reason</th><th>measurement</th></tr>
        </thead>
        <tbody>
          {(r.rows || []).map((row) => (
            <tr key={row.seq} className={`st-${row.state}`}>
              <td>{row.seq}</td>
              <td>{row.plot}</td>
              <td>{row.field_number}</td>
              <td>{row.state}</td>
              <td className="hint">{row.reason}</td>
              <td>{row.measurement_id ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const EXAMPLE_ROWS = `[
  {"plot": "P01", "field_number": "301", "species": "OAK",
   "x_m": 500012, "y_m": 4000012, "status": "alive_measured",
   "dbh_raw": 23.4, "dbh_unit": "cm",
   "height_raw": 15.2, "height_unit": "m"}
]`;

function SubmitBatch({ campaigns, onDone }) {
  const [open, setOpen] = useState(false);
  const [campaign, setCampaign] = useState("");
  const [survey, setSurvey] = useState("field-survey-1");
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [expected, setExpected] = useState("");
  const [rowsText, setRowsText] = useState(EXAMPLE_ROWS);
  const [result, setResult] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!campaign && campaigns.length) {
      setCampaign(campaigns[campaigns.length - 1].code);
    }
  }, [campaigns]);

  async function submit() {
    setErr(""); setResult(null);
    let rows;
    try {
      rows = JSON.parse(rowsText);
    } catch {
      setErr("rows must be a JSON array of field rows");
      return;
    }
    try {
      const receipt = await api.submitBatch({
        campaign, survey, idempotency_key: key, rows,
        ...(expected ? { expected_batches: Number(expected) } : {}),
      });
      setResult(receipt);
      onDone?.();
    } catch (e) {
      if (e.data && e.data.rows) {
        setResult(e.data);      // 409/422 still carry the per-row receipt
        onDone?.();
      } else {
        setErr(e.message);
      }
    }
  }

  if (!open) {
    return <button onClick={() => setOpen(true)}>submit a batch…</button>;
  }
  return (
    <section className="edition">
      <h3>Submit batch
        <button style={{ float: "right" }}
                onClick={() => setOpen(false)}>close</button>
      </h3>
      <div className="submit-grid">
        <label>campaign
          <select value={campaign}
                  onChange={(e) => setCampaign(e.target.value)}>
            {campaigns.map((c) => (
              <option key={c.code} value={c.code}>{c.code}</option>
            ))}
          </select>
        </label>
        <label>survey
          <input value={survey} onChange={(e) => setSurvey(e.target.value)} />
        </label>
        <label>idempotency key
          <input value={key} onChange={(e) => setKey(e.target.value)} />
          <button type="button"
                  onClick={() => setKey(crypto.randomUUID())}>new</button>
        </label>
        <label>expected batches
          <input value={expected} placeholder="optional"
                 onChange={(e) => setExpected(e.target.value)} />
        </label>
      </div>
      <textarea rows="8" value={rowsText}
                onChange={(e) => setRowsText(e.target.value)} />
      <button onClick={submit}>submit</button>
      {err && <div className="error">{err}</div>}
      {result && (
        <div className={result.status === "applied" ? "ok" : "error"}>
          <strong>{result.status}</strong>
          {result.replay && " (replayed receipt — nothing written again)"}
          {" "}· applied {result.n_applied} · duplicates {result.n_duplicates}
          {" "}· rejected {result.n_rejected} · conflicts {result.n_conflicts}
          {result.error && <> · {result.error}</>}
          <table>
            <tbody>
              {(result.rows || []).map((r) => (
                <tr key={r.seq} className={`st-${r.state}`}>
                  <td>{r.seq}</td><td>{r.plot}/{r.field_number}</td>
                  <td>{r.state}</td>
                  <td className="hint">{r.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
