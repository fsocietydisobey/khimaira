import { Link } from "react-router-dom";

import { useListProjectsQuery } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface AnyConnection {
  kind?: "postgres" | "sqlite";
  var?: string;
  host?: string;
  database?: string;
  label?: string;
  path?: string;
}

export function ProjectIndex() {
  const { data, isLoading, error } = useListProjectsQuery();

  if (isLoading) return <p className="p-6 text-sm text-muted-foreground">discovering projects…</p>;
  if (error) return <p className="p-6 text-sm text-destructive">projects failed: {String(error)}</p>;
  if (!data) return null;

  if (data.length === 0) {
    return (
      <div className="p-6">
        <Card>
          <CardHeader>
            <CardTitle>no langgraph projects discovered</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            Add project paths to <code>~/.config/chimera/roots.yaml</code> and
            restart <code>chimera monitor</code>. Each root is scanned for a
            <code> langgraph</code> dependency or <code>StateGraph</code> import.
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-4 p-6 overflow-auto h-full">
      <h2 className="text-lg font-semibold">Projects</h2>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {data.map((p) => (
          <Card key={p.name}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0">
              <CardTitle className="text-sm">
                <Link to={`/${p.name}`} className="hover:underline">
                  {p.name}
                </Link>
              </CardTitle>
              <Badge variant="outline" className="text-[10px]">
                {p.detected_via}
              </Badge>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="text-xs font-mono text-muted-foreground break-all">{p.path}</p>
              {p.connections.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {(p.connections as AnyConnection[]).map((c, i) => (
                    <Badge key={i} variant="secondary" className="font-mono text-[10px]">
                      {c.kind === "sqlite"
                        ? `sqlite: ${c.label}`
                        : `${c.var}: ${c.host}/${c.database}`}
                    </Badge>
                  ))}
                </div>
              ) : (
                <p className="text-[11px] text-muted-foreground">
                  no checkpointer detected
                </p>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
