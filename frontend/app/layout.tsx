import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RH Agents · Trading overview",
  description:
    "Multi-Agent On-Chain Trading Console. Real market observations and clearly labeled paper portfolio previews.",
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
