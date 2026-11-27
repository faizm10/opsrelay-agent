"use client";

// shadcn/ui Combobox pattern: a searchable select built from Popover + Command.
import { Check, ChevronsUpDown } from "lucide-react";
import { useState } from "react";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/popover";
import { cn } from "@/lib/utils";

export type ComboOption = { value: string; label: string; hint?: string };

export function Combobox({ id, options, value, onChange, placeholder, searchPlaceholder = "Search…", empty = "No match.", disabled }: {
  id?: string;
  options: ComboOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  searchPlaceholder?: string;
  empty?: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const picked = options.find((o) => o.value === value);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild disabled={disabled}>
        <button id={id} type="button" role="combobox" aria-expanded={open}
          className="input flex items-center justify-between gap-2 text-left disabled:opacity-60">
          <span className={cn("truncate", !picked && "text-faint")}>{picked?.label ?? placeholder}</span>
          <ChevronsUpDown className="h-4 w-4 shrink-0 text-faint" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-(--radix-popover-trigger-width) p-0">
        <Command>
          <CommandInput placeholder={searchPlaceholder} />
          <CommandList>
            <CommandEmpty>{empty}</CommandEmpty>
            <CommandGroup>
              {options.map((o) => (
                <CommandItem key={o.value} value={o.value} onSelect={() => { onChange(o.value); setOpen(false); }}>
                  <Check className={cn("h-4 w-4 shrink-0 text-accent", o.value === value ? "opacity-100" : "opacity-0")} aria-hidden="true" />
                  <span className="truncate">{o.label}</span>
                  {o.hint && <span className="ml-auto shrink-0 text-xs text-faint">{o.hint}</span>}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
