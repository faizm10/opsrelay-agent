import Image from "next/image";
import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Logo } from "@/components/ui";
import recorder from "./chart-recorder.webp";

export const metadata = { title: "OpsRelay | Uptime monitoring and incident response" };

export default function LandingPage() {
  return (
    <div className="min-h-svh bg-page font-sans text-ink">
      <header className="border-b border-line">
        <nav aria-label="Main" className="mx-auto flex h-18 max-w-360 items-center justify-between px-6 sm:px-10 lg:px-16">
          <Logo />
          <Link href="/sign-in" className="btn px-5 text-sm">Log in</Link>
        </nav>
      </header>
      <main className="mx-auto grid max-w-360 items-center gap-9 px-6 py-10 sm:px-10 md:min-h-[calc(100svh-4.5rem)] md:grid-cols-2 lg:gap-16 lg:px-16">
        <div className="max-w-xl">
          <h1 className="font-display text-[clamp(3.75rem,6.8vw,7rem)] leading-[0.94] tracking-[-0.045em] text-ink">
            Know when your site needs you.
          </h1>
          <p className="mt-6 max-w-md text-lg leading-relaxed text-muted">
            Monitor uptime, see which checks failed, and follow each incident through recovery.
          </p>
          <Link href="/sign-up" className="btn btn-primary mt-7 gap-3 px-6 py-3 text-sm">
            Create an account <ArrowRight size={16} aria-hidden="true" />
          </Link>
        </div>
        <Image src={recorder} alt="" priority sizes="(max-width: 767px) 100vw, (max-width: 1440px) 45vw, 624px"
          className="aspect-square w-full rounded-3xl object-cover" />
      </main>
    </div>
  );
}
