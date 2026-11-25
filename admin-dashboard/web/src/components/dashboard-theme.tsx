"use client";

import { Theme } from "@astryxdesign/core/theme";
import { stoneTheme } from "@astryxdesign/theme-stone/built";

export function DashboardTheme({ children }: { children: React.ReactNode }) {
  return <Theme theme={stoneTheme} mode="light">{children}</Theme>;
}
