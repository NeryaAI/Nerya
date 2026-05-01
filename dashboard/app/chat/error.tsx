"use client";

import { useEffect } from "react";

export default function ChatError({
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
    <div className="h-screen flex items-center justify-center px-6">
      <div className="w-full max-w-lg rounded-lg border border-[#ef5564]/30 bg-[#ef5564]/[0.06] p-5">
        <div className="text-xs uppercase tracking-[0.18em] text-[#ffb3bd]">
          Chat error
        </div>
        <h2 className="mt-2 text-lg font-semibold text-ink-100">
          Chat view could not render.
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-ink-300">
          {error.message || "Unexpected chat error."}
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
