"use client";
import { useEffect, useRef, useState } from "react";
import { SlidersHorizontal, RotateCcw, X } from "lucide-react";
import { GlobalControls } from "./lower-panels";
import { resetDashboardLayout } from "./sortable";
export function PolicyMenu() {
  const [open, setOpen] = useState(false);
  const [reset, setReset] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    function close(event: PointerEvent) {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    }
    function key(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", key);
    };
  }, [open]);
  return (
    <div className="policy-menu" ref={ref} id="controls">
      <button
        ref={trigger}
        className="policy-trigger"
        aria-expanded={open}
        aria-controls="execution-policy"
        onClick={() => {
          setOpen(!open);
          setReset(false);
        }}
      >
        <SlidersHorizontal size={14} />
        Controls
      </button>
      {open && (
        <div
          id="execution-policy"
          className="policy-popover"
          role="region"
          aria-label="Execution policy"
        >
          <button
            className="policy-close"
            aria-label="Close controls"
            onClick={() => {
              setOpen(false);
              trigger.current?.focus();
            }}
          >
            <X size={14} />
          </button>
          <GlobalControls />
          <div className="layout-reset">
            <button
              onClick={() => {
                resetDashboardLayout();
                setReset(true);
              }}
            >
              <RotateCcw size={13} />
              Reset layout
            </button>
            <small role="status">
              {reset ? "Default order restored" : "Local layout preferences"}
            </small>
          </div>
        </div>
      )}
    </div>
  );
}
