"use client";

import { useState, useSyncExternalStore, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, GripVertical } from "lucide-react";

const prefix = "rh-agents.layout.v1.";
const groups = ["kpis", "agents", "analytics", "trading"] as const;
const memory = new Map<string, string>();
const eventName = "rh-agents-layout-change";
function subscribe(callback: () => void) {
  const storage = (event: StorageEvent) => {
    if (event.key === null) memory.clear();
    else memory.delete(event.key);
    callback();
  };
  window.addEventListener("storage", storage);
  window.addEventListener(eventName, callback);
  return () => {
    window.removeEventListener("storage", storage);
    window.removeEventListener(eventName, callback);
  };
}
function read(key: string, fallback: string) {
  try {
    return memory.get(key) ?? window.localStorage.getItem(key) ?? fallback;
  } catch {
    return memory.get(key) ?? fallback;
  }
}
export function resetDashboardLayout() {
  for (const group of groups) {
    const key = prefix + group;
    memory.set(key, "[]");
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* Session-only layout. */
    }
  }
  window.dispatchEvent(new Event(eventName));
}
export function SortableGroup({
  group,
  className,
  label,
  items,
}: {
  group: (typeof groups)[number];
  className: string;
  label: string;
  items: { id: string; label: string; content: ReactNode }[];
}) {
  const key = prefix + group;
  const defaults = items.map((item) => item.id);
  const fallback = JSON.stringify(defaults);
  const stored = useSyncExternalStore(
    subscribe,
    () => read(key, fallback),
    () => fallback,
  );
  let order = defaults;
  try {
    const parsed: unknown = JSON.parse(stored);
    if (
      Array.isArray(parsed) &&
      parsed.length === defaults.length &&
      new Set(parsed).size === defaults.length &&
      parsed.every((id) => typeof id === "string" && defaults.includes(id))
    )
      order = parsed;
  } catch {
    /* Invalid/old saved layouts use the complete default set. */
  }
  const [dragging, setDragging] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  function move(id: string, target: number) {
    const next = [...order];
    next.splice(next.indexOf(id), 1);
    next.splice(target, 0, id);
    const serialized = JSON.stringify(next);
    memory.set(key, serialized);
    try {
      window.localStorage.setItem(key, serialized);
    } catch {
      /* Keep session order. */
    }
    window.dispatchEvent(new Event(eventName));
    setAnnouncement(
      `${items.find((item) => item.id === id)?.label} moved to position ${target + 1} of ${items.length}`,
    );
  }
  return (
    <div
      className={`sortable-group ${className}`}
      aria-label={label}
      data-sort-group={group}
    >
      <span className="sr-only" aria-live="polite">
        {announcement}
      </span>
      {order.map((id, index) => {
        const item = items.find((item) => item.id === id)!;
        return (
          <div
            className={`sortable-tile ${dragging === id ? "is-dragging" : ""} ${target === id ? "drop-target" : ""}`}
            key={id}
            data-tile-id={id}
          >
            <div
              className="sort-tools"
              role="group"
              aria-label={`Arrange ${item.label}`}
            >
              <button
                className="drag-handle"
                aria-label={`Drag ${item.label} to reorder`}
                title="Drag to reorder; use arrows for keyboard or touch"
                onPointerDown={(event) => {
                  if (event.button !== 0) return;
                  event.preventDefault();
                  event.currentTarget.focus();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  setDragging(id);
                }}
                onPointerMove={(event) => {
                  if (!event.currentTarget.hasPointerCapture(event.pointerId))
                    return;
                  const tile = document
                    .elementFromPoint(event.clientX, event.clientY)
                    ?.closest<HTMLElement>("[data-tile-id]");
                  setTarget(
                    tile?.closest<HTMLElement>("[data-sort-group]")?.dataset
                      .sortGroup === group
                      ? (tile.dataset.tileId ?? null)
                      : null,
                  );
                }}
                onPointerUp={(event) => {
                  if (!event.currentTarget.hasPointerCapture(event.pointerId))
                    return;
                  const tile = document
                    .elementFromPoint(event.clientX, event.clientY)
                    ?.closest<HTMLElement>("[data-tile-id]");
                  const destination = tile?.dataset.tileId;
                  if (
                    tile?.closest<HTMLElement>("[data-sort-group]")?.dataset
                      .sortGroup === group &&
                    destination &&
                    destination !== id &&
                    order.includes(destination)
                  )
                    move(id, order.indexOf(destination));
                  setDragging(null);
                  setTarget(null);
                }}
                onLostPointerCapture={() => {
                  setDragging(null);
                  setTarget(null);
                }}
              >
                <GripVertical size={13} />
              </button>
              <button
                aria-label={`Move ${item.label} earlier`}
                disabled={index === 0}
                onClick={() => move(id, index - 1)}
              >
                <ArrowLeft size={11} />
              </button>
              <button
                aria-label={`Move ${item.label} later`}
                disabled={index === order.length - 1}
                onClick={() => move(id, index + 1)}
              >
                <ArrowRight size={11} />
              </button>
            </div>
            {item.content}
          </div>
        );
      })}
    </div>
  );
}
