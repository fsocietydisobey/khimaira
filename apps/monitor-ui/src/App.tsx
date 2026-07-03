import { Route, Routes } from "react-router-dom";

import { CostDashboard } from "@/components/observer/CostDashboard";
import { TraceWaterfall } from "@/components/observer/TraceWaterfall";
import { KgMapper } from "@/components/kg/KgMapper";
import { Notebook } from "@/components/notebook/Notebook";
import { ProjectIndex } from "@/components/projects/ProjectIndex";
import { ProjectView } from "@/components/project/ProjectView";
import { MonitorShell } from "@/components/shell/MonitorShell";

export default function App() {
  return (
    <Routes>
      <Route element={<MonitorShell />}>
        <Route index element={<ProjectIndex />} />
        <Route path=":name" element={<ProjectView />} />
        <Route path=":name/cost" element={<CostDashboard />} />
        <Route path=":name/trace/:correlationId" element={<TraceWaterfall />} />
        <Route path=":name/kg" element={<KgMapper />} />
        <Route path=":name/notebook" element={<Notebook />} />
      </Route>
    </Routes>
  );
}
