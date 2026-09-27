"use client";
import { useCallback, useEffect, useState } from "react";
import { readJson } from "@/lib/scout";

export type Resource<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: T };

// One read-only GET, with an explicit error state: a failed read is shown as
// unavailable, never as zero.
export function useResource<T>(path: string | null, version: number) {
  const [resource, setResource] = useState<Resource<T>>({ state: "loading" });
  const load = useCallback(async () => {
    if (path === null) return;
    setResource({ state: "loading" });
    try {
      setResource({ state: "ready", data: await readJson<T>(path) });
    } catch (error) {
      setResource({
        state: "error",
        message: error instanceof Error ? error.message : "unavailable",
      });
    }
  }, [path]);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load, version]);
  return resource;
}
