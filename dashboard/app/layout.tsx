import "./globals.css";
import type { Metadata } from "next";
import { Sidebar } from "../components/Sidebar";
import { TopHeader } from "../components/TopHeader";

export const metadata: Metadata = {
  title: "Nerya · Self-evolving trading agent",
  description:
    "Skill-first, trading-native, self-evolving autonomous agent runtime.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-ink-950 text-ink-100 font-sans antialiased">
        <div className="flex min-h-screen">
          <Sidebar />
          <main className="flex-1 min-w-0 flex flex-col">
            <TopHeader />
            <div className="relative flex-1">
              {/* Faint grid + subtle vignette behind content. The grid
                  helps the eye anchor the data-heavy panels; the
                  vignette stops the aurora from washing out the cards
                  at the centre of the viewport. */}
              <div className="absolute inset-0 grid-bg opacity-20 pointer-events-none" />
              <div
                className="absolute inset-0 pointer-events-none"
                style={{
                  background:
                    "radial-gradient(75% 60% at 50% 0%, rgba(139,92,246,0.06), transparent 70%)",
                }}
              />
              <div className="relative px-6 lg:px-10 py-6 max-w-[1500px] mx-auto">
                {children}
              </div>
            </div>
          </main>
        </div>
      </body>
    </html>
  );
}
