"use client";

import { useEffect } from "react";

export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="min-h-[420px] flex items-center justify-center px-6">
      <div className="w-full max-w-xl rounded-lg border border-danger/30 bg-danger/[0.06] p-5">
        <div className="text-[12px] font-medium text-rose-300">
          Runtime error
        </div>
        <h2 className="mt-2 text-[17px] font-medium text-ink-100">
          The dashboard could not render this view.
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-ink-300">
          {error.message || "Unexpected client error."}
        </p>
        {error.digest ? (
          <div className="mt-3 font-mono text-[11px] text-ink-500">
            {error.digest}
          </div>
        ) : null}
        <button
          type="button"
          onClick={() => reset()}
          className="mt-4 rounded-md border border-accent-400/50 bg-accent-400/10 px-3 py-1.5 text-sm text-accent-300 hover:bg-accent-400/20"
        >
          Retry
        </button>
      </div>
    </div>
  );
}
