import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RH Agents · Trading overview",
  description:
    "Autonomous intelligence. Deterministic risk. Phase 0 paper trading workspace.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
