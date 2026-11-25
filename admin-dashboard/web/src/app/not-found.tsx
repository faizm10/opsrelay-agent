import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-6 text-center">
      <p className="font-mono text-sm text-faint">404</p>
      <h1 className="page-title mt-2">Nothing to monitor here</h1>
      <Link href="/" className="btn mt-6">Back to overview</Link>
    </div>
  );
}
