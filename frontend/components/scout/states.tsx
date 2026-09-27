import type { ReactNode } from "react";
import type { Resource } from "./use-resource";

export function Loading({ what }: { what: string }) {
  return (
    <p className="scout-state" role="status">
      Loading {what}…
    </p>
  );
}

export function Failed({ what, message }: { what: string; message: string }) {
  return (
    <p className="scout-state scout-error" role="alert">
      {what} unavailable ({message}). Nothing is shown rather than a guess.
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="scout-state">{children}</p>;
}

export function Gate<T>({
  resource,
  what,
  children,
}: {
  resource: Resource<T>;
  what: string;
  children: (data: T) => ReactNode;
}) {
  if (resource.state === "loading") return <Loading what={what} />;
  if (resource.state === "error")
    return <Failed what={what} message={resource.message} />;
  return <>{children(resource.data)}</>;
}
