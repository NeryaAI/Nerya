"use client";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          background: "#070b14",
          color: "#e5edf7",
          fontFamily:
            'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        }}
      >
        <main
          style={{
            minHeight: "100vh",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 24,
          }}
        >
          <section
            style={{
              width: "100%",
              maxWidth: 560,
              border: "1px solid rgba(239, 85, 100, 0.35)",
              borderRadius: 8,
              background: "rgba(239, 85, 100, 0.08)",
              padding: 24,
            }}
          >
            <div
              style={{
                color: "#ffb3bd",
                fontSize: 11,
                letterSpacing: "0.16em",
                textTransform: "uppercase",
              }}
            >
              Global runtime error
            </div>
            <h1 style={{ margin: "10px 0 0", fontSize: 22 }}>
              Nerya dashboard failed to render.
            </h1>
            <p style={{ color: "#a8b3c7", lineHeight: 1.6 }}>
              {error.message || "Unexpected application error."}
            </p>
            {error.digest ? (
              <code style={{ color: "#6f7d95", fontSize: 12 }}>
                {error.digest}
              </code>
            ) : null}
            <div>
              <button
                type="button"
                onClick={() => reset()}
                style={{
                  marginTop: 18,
                  border: "1px solid rgba(45, 212, 191, 0.45)",
                  borderRadius: 6,
                  background: "rgba(45, 212, 191, 0.12)",
                  color: "#7dd3fc",
                  padding: "8px 12px",
                  cursor: "pointer",
                }}
              >
                Retry
              </button>
            </div>
          </section>
        </main>
      </body>
    </html>
  );
}
