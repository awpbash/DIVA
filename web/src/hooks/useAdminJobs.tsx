import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { AdminJob, getAdminJobs } from "../api";

// App-level background-job state. One poll for the whole app: the ticker strip
// can show extraction/refresh progress on EVERY tab, and AdminDashboard reads
// the same state instead of running its own interval. Polls only for admins
// (the endpoint is admin-gated), fast while a job runs, slow when idle so a
// parked browser tab does not keep a sleeping deploy awake.

const RUNNING_POLL_MS = 4000;
const IDLE_POLL_MS = 20000;

interface JobsState {
  jobs: Record<string, AdminJob>;
  /** Mark a job running right away (an upload or extract just fired) so the
   * UI reacts before the first poll lands. */
  seed: (docId: string, job: AdminJob) => void;
}

const JobsCtx = createContext<JobsState>({ jobs: {}, seed: () => {} });

export function AdminJobsProvider({ admin, children }: {
  admin: boolean; children: React.ReactNode;
}) {
  const [jobs, setJobs] = useState<Record<string, AdminJob>>({});

  const seed = useCallback((docId: string, job: AdminJob) => {
    setJobs(prev => ({ ...prev, [docId]: job }));
  }, []);

  const anyRunning = Object.values(jobs).some(j => j.status === "running");

  useEffect(() => {
    if (!admin) { setJobs({}); return; }
    let stop = false;
    const tick = async () => {
      const j = await getAdminJobs();
      if (!stop) setJobs(j);
    };
    tick();
    const id = window.setInterval(tick, anyRunning ? RUNNING_POLL_MS : IDLE_POLL_MS);
    return () => { stop = true; window.clearInterval(id); };
  }, [admin, anyRunning]);

  return <JobsCtx.Provider value={{ jobs, seed }}>{children}</JobsCtx.Provider>;
}

export const useAdminJobs = () => useContext(JobsCtx);
