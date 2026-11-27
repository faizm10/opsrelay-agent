import type { Metadata } from "next";
import { Geist, Geist_Mono, Instrument_Serif } from "next/font/google";
import { Analytics } from "@vercel/analytics/next";
import { AssistantChat } from "@/components/assistant-chat";
import { DashboardTheme } from "@/components/dashboard-theme";
import { Toaster } from "@/components/sonner";
import "./globals.css";

const display = Instrument_Serif({ weight: "400", subsets: ["latin"], variable: "--font-instrument-serif", display: "swap" });
const sans = Geist({ subsets: ["latin"], variable: "--font-geist", display: "swap" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono", display: "swap" });

export const metadata: Metadata = {
  title: "OpsRelay Status",
  description: "Monitor service uptime, investigate incidents, and track recovery with OpsRelay.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable} ${display.variable}`}>
      <body>
        <DashboardTheme>
          {children}
          <AssistantChat />
          <Toaster />
        </DashboardTheme>
        {process.env.VERCEL && <Analytics />}
      </body>
    </html>
  );
}
