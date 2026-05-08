import { Route, Routes } from "react-router-dom";

import { ProjectIndex } from "@/components/projects/ProjectIndex";
import { ProjectView } from "@/components/project/ProjectView";
import { MonitorShell } from "@/components/shell/MonitorShell";

export default function App() {
  return (
    <Routes>
      <Route element={<MonitorShell />}>
        <Route index element={<ProjectIndex />} />
        <Route path=":name" element={<ProjectView />} />
      </Route>
    </Routes>
  );
}
