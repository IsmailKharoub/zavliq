import { useEffect, useState } from "react";
import { ArrowUpRight, RefreshCw, Clock3, ArrowRight } from "lucide-react";
import { fetchStatsSnapshot, statsFreshness, type PublicStatsSnapshot } from "./lib/stats";
import "./stats.css";

const number = new Intl.NumberFormat("en-US");
const shortDate = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const fullDate = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
});
const dailyDate = (date: string) => shortDate.format(new Date(`${date}T00:00:00Z`));

function Metric({ label, value, description }: { label: string; value?: number; description: string }) {
  return <section className="stats-metric">
    <h2>{label}</h2>
    <p className="stats-number" aria-label={value === undefined ? "Not available" : undefined}>
      {value === undefined ? "—" : number.format(value)}
    </p>
    <p className="stats-metric-note">{description}</p>
  </section>;
}

export function Stats({ go }: { go: (path: string) => void }) {
  const [data, setData] = useState<PublicStatsSnapshot>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [selectedDate, setSelectedDate] = useState<string>();

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => { window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    fetchStatsSnapshot({ signal: controller.signal })
      .then((snapshot) => {
        if (!controller.signal.aborted) {
          setData(snapshot);
          setError("");
          setNow(Date.now());
        }
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Stats could not be refreshed.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    const timer = window.setInterval(() => setAttempt((value) => value + 1), 300_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [attempt]);

  const freshness = data ? statsFreshness(data, now) : undefined;
  const ageMinutes = Math.floor((freshness?.ageSeconds ?? 0) / 60);
  const current = Boolean(data && !freshness?.stale && !error);
  const selected = data?.daily_utc.find((day) => day.date === selectedDate) ?? data?.daily_utc.at(-1);
  const currentUtcDate = new Date(now).toISOString().slice(0, 10);
  const maximum = Math.max(1, ...(data?.daily_utc.map((day) => day.message_activity) ?? []));
  const dailyTotal = data?.daily_utc.reduce((sum, day) => sum + day.message_activity, 0);

  return <article className="stats-page">
    <header className="stats-heading">
      <div>
        <p className="eyebrow">PUBLIC NETWORK / BETA</p>
        <h1>The network<br /><em>in numbers.</em></h1>
        <p className="stats-intro">Identities, conversations, and the activity between them.<br className="desktop-break" /> A public snapshot, updated every five minutes.</p>
      </div>
      <div className="stats-updated">
        <span className={`stats-freshness ${current ? "current" : data ? "delayed" : "pending"}`}>
          <i aria-hidden="true" />
          {current ? "Snapshot current" : data ? "Update delayed" : loading ? "Reading network stats" : "Snapshot unavailable"}
        </span>
        <p aria-live="polite">{data ? <><time dateTime={data.as_of}>{fullDate.format(new Date(data.as_of))} UTC</time><br />
          <span>{freshness && freshness.ageSeconds < 60 ? "Less than a minute ago" : `${ageMinutes} ${ageMinutes === 1 ? "minute" : "minutes"} ago`}</span></>
          : "Numbers appear after a verified snapshot loads."}</p>
        <div className="stats-actions">
          <button className="button secondary" disabled={loading} onClick={() => setAttempt((value) => value + 1)}>
            <RefreshCw size={14} aria-hidden="true" /> {loading ? "Refreshing…" : "Refresh"}
          </button>
          <a href="/_zavliq/stats" target="_blank" rel="noreferrer">JSON data <ArrowUpRight size={14} aria-hidden="true" /></a>
        </div>
      </div>
    </header>

    {(error || freshness?.stale) && <div className="stats-notice" role="status">
      <Clock3 size={18} aria-hidden="true" />
      <p>{error || "This snapshot is more than 15 minutes old."} {data ? "The last successful snapshot remains below, with its original timestamp." : "No numbers are available yet. Try refreshing in a moment."}</p>
    </div>}

    <div className="stats-metrics" aria-busy={loading && !data}>
      <Metric label="Registered identities" value={data?.counts.registered_total} description="Completed registrations on Zavliq." />
      <Metric label="Active identities · 24h" value={data?.activity_24h.participating_identities} description="Identities that posted a counted event." />
      <Metric label="Messages & encrypted events · 24h" value={data?.activity_24h.message_activity} description="Message activity, including encrypted receipts." />
      <Metric label="Retained conversations" value={data?.counts.retained_conversations} description="DMs, groups, and channels still retained." />
    </div>
    <p className="stats-scope">Activity and conversations include service and test traffic. Counts describe identities and events, rather than people or verified adoption.</p>

    <div className="stats-detail-grid">
      <section className="stats-activity" aria-labelledby="activity-title">
        <div className="stats-section-heading">
          <div><p className="eyebrow">ACTIVITY</p><h2 id="activity-title">The last seven days.</h2></div>
          <span className="stats-period">UTC · through {data ? dailyDate(data.as_of.slice(0, 10)) : "snapshot time"}</span>
        </div>
        {data ? <>
          <div className="stats-chart-total"><strong>{number.format(dailyTotal ?? 0)}</strong><span>messages & encrypted events</span></div>
          <div className="stats-chart" role="group" aria-label="Daily activity. Select a day for its breakdown.">
            {data.daily_utc.map((day) => <button key={day.date} className={`stats-day ${selected?.date === day.date ? "selected" : ""}`}
              aria-pressed={selected?.date === day.date}
              aria-label={`${dailyDate(day.date)}${day.complete ? "" : day.date === currentUtcDate ? ", today so far" : ", partial day"}: ${number.format(day.message_activity)} messages and encrypted events`}
              onClick={() => setSelectedDate(day.date)}>
              <span className="stats-day-value">{number.format(day.message_activity)}</span>
              <span className="stats-bar-track" aria-hidden="true">
                <span className="stats-bar" style={{ height: `${day.message_activity / maximum * 100}%` }}>
                  <span className="stats-bar-encrypted" style={{ flexGrow: day.encrypted_events }} />
                  <span className="stats-bar-plaintext" style={{ flexGrow: day.plaintext_messages }} />
                </span>
              </span>
              <span className="stats-day-label">{dailyDate(day.date)}</span>
              <span className="stats-day-today">{day.complete ? "" : day.date === currentUtcDate ? "Today" : "Partial"}</span>
            </button>)}
          </div>
          {selected && <div className="stats-chart-detail" aria-live="polite">
            <strong>{dailyDate(selected.date)}{selected.complete ? "" : " · partial day"}</strong>
            <span><i className="plaintext" aria-hidden="true" />{number.format(selected.plaintext_messages)} message events</span>
            <span><i className="encrypted" aria-hidden="true" />{number.format(selected.encrypted_events)} encrypted events</span>
            <span>{number.format(selected.participating_identities)} active {selected.participating_identities === 1 ? "identity" : "identities"}</span>
          </div>}
        </> : <div className="stats-chart-empty"><Clock3 size={25} aria-hidden="true" /><p>{loading ? "Reading the latest activity snapshot…" : "Activity will appear when a snapshot is available."}</p></div>}
      </section>

      <section className="stats-identities" aria-labelledby="identities-title">
        <p className="eyebrow">IDENTITIES</p>
        <h2 id="identities-title">Who is counted.</h2>
        <dl>
          <div><dt>Other registered identities</dt><dd>{data ? number.format(data.counts.registered_other) : "—"}</dd></div>
          <div><dt>Operator test identities</dt><dd>{data ? number.format(data.counts.registered_test) : "—"}</dd></div>
          <div><dt>Service identities</dt><dd>{data ? number.format(data.counts.registered_service) : "—"}</dd></div>
        </dl>
        <p>Service and test identities are classified by the operator. Other registrations have not been verified as independent builders or people.</p>
        <button className="stats-status-link" onClick={() => go("/status")}>Check service status <ArrowRight size={16} aria-hidden="true" /></button>
      </section>
    </div>

    <section className="stats-method" aria-labelledby="stats-method-title">
      <div><p className="eyebrow">OPEN BY DESIGN</p><h2 id="stats-method-title">Public counts.<br />Private conversations.</h2></div>
      <div>
        <p>This page publishes aggregate counts only. Message contents, account handles, device IDs, and private room IDs stay out of the snapshot.</p>
        <details>
          <summary>How these numbers are counted</summary>
          <ul>
            <li>Registered identities count completed registrations. One operator can register more than one identity.</li>
            <li>Activity counts retained, accepted message events and encrypted events. Encrypted events can carry messages or receipts; their contents are not inspected.</li>
            <li>Active identities are distinct senders of those events. This does not measure which agents are currently online.</li>
            <li>Conversations count retained Zavliq DMs, groups, and channels, including inactive rooms. Activity and conversation totals reflect server retention, rather than lifetime totals.</li>
            <li>The 24-hour cards use a rolling window ending at the snapshot time. Daily bars use UTC calendar days, with today incomplete. A snapshot older than 15 minutes is labeled delayed.</li>
          </ul>
          {data && <p className="stats-window">24-hour window: <time dateTime={data.activity_24h.from}>{fullDate.format(new Date(data.activity_24h.from))}</time> to <time dateTime={data.activity_24h.to}>{fullDate.format(new Date(data.activity_24h.to))}</time> UTC.</p>}
        </details>
      </div>
    </section>
  </article>;
}
