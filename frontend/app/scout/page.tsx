import type { Metadata } from "next";
import { ScoutCockpit } from "@/components/scout/cockpit";

export const metadata: Metadata = {
  title: "RH Agents · Early Discovery",
  description:
    "Read-only early-discovery scout cockpit. Observations, not trade recommendations.",
};

export default function ScoutPage() {
  return <ScoutCockpit />;
}
