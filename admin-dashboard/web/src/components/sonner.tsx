"use client";

// shadcn/ui Sonner toaster, styled with the dashboard's colour tokens.
import { Toaster as Sonner, type ToasterProps } from "sonner";

export function Toaster(props: ToasterProps) {
  return (
    <Sonner
      theme="light"
      position="bottom-left"
      closeButton
      toastOptions={{
        classNames: {
          toast: "!rounded-xl !border !border-line !bg-surface !text-ink !shadow-lg font-sans text-sm",
          description: "!text-muted",
          success: "[&_[data-icon]]:!text-ok",
          error: "[&_[data-icon]]:!text-crit",
          closeButton: "!border-line !bg-surface !text-muted",
        },
      }}
      {...props}
    />
  );
}
