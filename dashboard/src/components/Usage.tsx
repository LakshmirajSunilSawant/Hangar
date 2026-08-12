/** CPU and memory for one app — PRD Milestone 5's owner-facing half.
 *
 * Charts are hand-rolled SVG rather than a charting library. Two sparklines do
 * not justify 200kB of JavaScript on a page served from a free VM, and the
 * whole implementation is shorter than the import would be.
 */

import { useEffect, useState } from "react";
import { api } from "../api";
import type { Metrics, Sample } from "../types";

export function Usage({ appId, live }: { appId: string; live: boolean }) {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const fresh = await api.metrics(appId);
        if (!cancelled) {
          setMetrics(fresh);
          setError("");
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    };

    void load();
    // Following the collector's own cadence would be the obvious choice, but
    // it samples on its own clock — polling a little faster keeps the newest
    // reading from sitting invisible for most of an interval.
    if (!live) return;
    const period = Math.max((metrics?.interval ?? 15) * 500, 5000);
    const timer = setInterval(() => void load(), period);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [appId, live, metrics?.interval]);

  if (error) return <div className="error">{error}</div>;
  if (!metrics) return <p className="muted">Loading usage…</p>;

  const { current, samples } = metrics;

  if (samples.length === 0) {
    return (
      <div className="card">
        <p style={{ marginTop: 0 }}>No readings yet.</p>
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          {live
            ? `Usage is sampled every ${metrics.interval}s. The first reading should appear shortly.`
            : "Usage is only sampled for running apps."}
        </p>
        <p className="muted" style={{ marginBottom: 0, fontSize: 13 }}>
          Caps for this app: {metrics.memory_limit_mb} MB memory,{" "}
          {metrics.cpu_limit} CPU.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="facts card" style={{ marginBottom: 16 }}>
        <Reading
          label="CPU"
          value={`${current?.cpu_percent.toFixed(1) ?? "—"}%`}
          detail={`cap ${(metrics.cpu_limit * 100).toFixed(0)}% of a core`}
        />
        <Reading
          label="Memory"
          value={`${current?.memory_mb.toFixed(0) ?? "—"} MB`}
          detail={`of ${metrics.memory_limit_mb} MB (${current?.memory_percent.toFixed(0) ?? "—"}%)`}
          warn={(current?.memory_percent ?? 0) > 85}
        />
        <Reading
          label="Window"
          value={`${samples.length} samples`}
          detail={`every ${metrics.interval}s, up to ${metrics.window_minutes} min`}
        />
      </div>

      <Chart
        title="CPU"
        unit="%"
        samples={samples}
        pick={(s) => s.cpu_percent}
        ceiling={Math.max(metrics.cpu_limit * 100, 1)}
        ceilingLabel={`cap ${(metrics.cpu_limit * 100).toFixed(0)}%`}
      />
      <Chart
        title="Memory"
        unit=" MB"
        samples={samples}
        pick={(s) => s.memory_mb}
        ceiling={metrics.memory_limit_mb}
        ceilingLabel={`cap ${metrics.memory_limit_mb} MB`}
      />

      <p className="hint">
        Sampled in the control plane and kept in memory, not on disk — history
        starts empty after a restart. Enough to answer “is this app about to be
        killed for using too much memory”, which is the question that matters
        on a small box.
      </p>
    </div>
  );
}

function Reading({
  label,
  value,
  detail,
  warn,
}: {
  label: string;
  value: string;
  detail: string;
  warn?: boolean;
}) {
  return (
    <div className="fact">
      <div className="k">{label}</div>
      <div className="v" style={warn ? { color: "var(--bad, #b4232a)" } : undefined}>
        {value}
        <div className="muted" style={{ fontSize: 12 }}>{detail}</div>
      </div>
    </div>
  );
}

const WIDTH = 600;
const HEIGHT = 90;

function Chart({
  title,
  unit,
  samples,
  pick,
  ceiling,
  ceilingLabel,
}: {
  title: string;
  unit: string;
  samples: Sample[];
  pick: (s: Sample) => number;
  ceiling: number;
  ceilingLabel: string;
}) {
  const values = samples.map(pick);
  const peak = Math.max(...values);
  // Scale to the cap so the shape means "how close to the limit", but grow
  // past it if an app somehow exceeds one — a line clipped at the top edge
  // would hide exactly the moment worth seeing.
  const top = Math.max(ceiling, peak) * 1.05 || 1;

  const x = (i: number) =>
    values.length === 1 ? WIDTH : (i / (values.length - 1)) * WIDTH;
  const y = (v: number) => HEIGHT - (v / top) * HEIGHT;

  const line = values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(v)}`).join(" ");
  const area = `${line} L${WIDTH},${HEIGHT} L0,${HEIGHT} Z`;
  const capY = y(ceiling);

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="spread" style={{ marginBottom: 6 }}>
        <strong style={{ fontSize: 13 }}>{title}</strong>
        <span className="muted" style={{ fontSize: 12 }}>
          peak {peak.toFixed(title === "CPU" ? 1 : 0)}
          {unit} · {ceilingLabel}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        style={{ width: "100%", height: HEIGHT, display: "block" }}
        role="img"
        aria-label={`${title} over the last ${samples.length} samples, peak ${peak.toFixed(1)}${unit}`}
      >
        <path d={area} fill="currentColor" opacity="0.12" />
        <path d={line} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
        {capY >= 0 && capY <= HEIGHT && (
          <line
            x1="0"
            x2={WIDTH}
            y1={capY}
            y2={capY}
            stroke="currentColor"
            strokeWidth="1"
            strokeDasharray="4 4"
            opacity="0.45"
            vectorEffect="non-scaling-stroke"
          />
        )}
      </svg>
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        oldest → newest
      </div>
    </div>
  );
}
