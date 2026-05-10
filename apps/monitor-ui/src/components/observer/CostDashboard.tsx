/**
 * CostDashboard — per-project token usage + estimated USD by model.
 *
 * Backed by GET /api/heartbeats/{project}/cost. Aggregates llm_end events
 * captured by chimera_observer (v0.4.0+) into per-model spend, plus a
 * "telemetry overhead" line item counting LangSmith API calls (which
 * are pure overhead unless the user is actually using LangSmith).
 *
 * Goals:
 *  - At-a-glance "what's this project costing today"
 *  - Surface telemetry overhead so it's visible vs. invisible
 *  - Per-model breakdown to spot expensive model usage
 *
 * Caveats (intentional):
 *  - Costs are estimates from public list prices; not invoice accounting.
 *    The note field from the API surfaces this to the user too.
 *  - Buffer-bounded: only reflects runs still in the daemon's heartbeat
 *    buffer (1h TTL). Long-term storage is out of scope here.
 */

import { useParams } from "react-router-dom";

import { useGetCostSummaryQuery, type CostByModel } from "@/api";
import { ProjectNavTabs } from "@/components/project/ProjectNavTabs";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";


function formatUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}


export function CostDashboard() {
  const { name } = useParams<{ name: string }>();
  const projectName = name ?? "";
  const { data, isLoading, error } = useGetCostSummaryQuery(projectName, {
    pollingInterval: 5000,
    skip: !projectName,
  });

  if (!projectName) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        no project — pick one from the sidebar
      </div>
    );
  }
  if (isLoading) {
    return <div className="p-6 text-sm text-muted-foreground">loading cost summary…</div>;
  }
  if (error) {
    return (
      <div className="p-6 text-sm text-destructive">
        cost summary failed: {String((error as { error?: string }).error ?? error)}
      </div>
    );
  }
  if (!data) return null;

  const models = Object.entries(data.by_model);
  const grandCost = data.total_cost_usd;
  const hasData = data.total_input_tokens + data.total_output_tokens > 0;

  return (
    <div className="flex h-full flex-col overflow-auto">
      <header className="shrink-0 border-b border-border bg-card/40 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">cost summary — {projectName}</h2>
            <p className="text-[11px] text-muted-foreground mt-0.5">
              {data.note} · refreshes every 5s · only reflects runs still in the
              observer's 1h heartbeat buffer
            </p>
          </div>
          <ProjectNavTabs projectName={projectName} />
        </div>
      </header>

      <div className="grid gap-4 p-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-[10px] uppercase tracking-wider">
              estimated total
            </CardDescription>
            <CardTitle className="text-2xl font-mono">{formatUsd(grandCost)}</CardTitle>
          </CardHeader>
          <CardContent className="text-[11px] text-muted-foreground pt-0">
            across {data.run_count} run{data.run_count === 1 ? "" : "s"} ·{" "}
            {formatTokens(data.total_input_tokens)} in /{" "}
            {formatTokens(data.total_output_tokens)} out
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-[10px] uppercase tracking-wider">
              models in use
            </CardDescription>
            <CardTitle className="text-2xl font-mono">{models.length}</CardTitle>
          </CardHeader>
          <CardContent className="text-[11px] text-muted-foreground pt-0">
            {models.length === 0
              ? "no llm_end events captured yet"
              : models.slice(0, 3).map(([m]) => m).join(", ") +
                (models.length > 3 ? ` +${models.length - 3} more` : "")}
          </CardContent>
        </Card>

        <Card
          className={cn(
            data.telemetry_calls_langsmith > 50 && "border-amber-500/40 bg-amber-500/5",
          )}
        >
          <CardHeader className="pb-2">
            <CardDescription className="text-[10px] uppercase tracking-wider">
              telemetry overhead
            </CardDescription>
            <CardTitle className="text-2xl font-mono">
              {data.telemetry_calls_langsmith}
              <span className="text-xs font-normal text-muted-foreground ml-1">
                LangSmith calls
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="text-[11px] text-muted-foreground pt-0">
            {data.telemetry_calls_langsmith > 50 ? (
              <>
                <span className="text-amber-300">heavy:</span> set{" "}
                <code>CHIMERA_DISABLE_LANGSMITH=true</code> in the app's env to
                opt out (observer v0.4.0+ shim)
              </>
            ) : (
              <>opt out via <code>CHIMERA_DISABLE_LANGSMITH=true</code> if unused</>
            )}
          </CardContent>
        </Card>
      </div>

      {hasData ? (
        <div className="px-4 pb-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">by model</CardTitle>
              <CardDescription className="text-[11px]">
                sorted by estimated USD desc
              </CardDescription>
            </CardHeader>
            <CardContent className="pt-0">
              <ModelTable models={models} grandCost={grandCost} />
            </CardContent>
          </Card>
        </div>
      ) : (
        <div className="px-4 pb-4">
          <Card>
            <CardContent className="pt-6 text-sm text-muted-foreground">
              No <code>llm_end</code> events captured yet for this project.
              Either no LangChain calls have run since the daemon started, or
              the observer isn't attached. Run{" "}
              <code>chimera attach &lt;app-path&gt;</code> + restart the app
              to start streaming heartbeats.
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}


function ModelTable({
  models,
  grandCost,
}: {
  models: Array<[string, CostByModel]>;
  grandCost: number;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
          <tr className="border-b border-border">
            <th className="py-2 text-left font-medium">model</th>
            <th className="py-2 text-right font-medium">calls</th>
            <th className="py-2 text-right font-medium">in</th>
            <th className="py-2 text-right font-medium">out</th>
            <th className="py-2 text-right font-medium">cost</th>
            <th className="py-2 text-right font-medium">% of total</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {models.map(([model, m]) => {
            const pct = grandCost > 0 ? (m.cost_usd / grandCost) * 100 : 0;
            return (
              <tr key={model} className="border-b border-border/50 last:border-0">
                <td className="py-1.5 text-foreground">{model}</td>
                <td className="py-1.5 text-right text-muted-foreground">{m.calls}</td>
                <td className="py-1.5 text-right text-muted-foreground">
                  {formatTokens(m.input_tokens)}
                </td>
                <td className="py-1.5 text-right text-muted-foreground">
                  {formatTokens(m.output_tokens)}
                </td>
                <td className="py-1.5 text-right text-foreground">{formatUsd(m.cost_usd)}</td>
                <td className="py-1.5 text-right text-muted-foreground">
                  {pct < 1 ? "<1%" : `${pct.toFixed(0)}%`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
