import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { LiveMap } from "./components/LiveMap";
import { LiveProvider } from "./hooks";
import { PAGES, Sidebar, TopBar, type Page } from "./layout";
import { Experiments } from "./pages/Experiments";
import { Findings } from "./pages/Findings";
import { History } from "./pages/History";
import { Overview } from "./pages/Overview";
import { ToastProvider } from "./ui";

const fromHash = (): Page => {
  const h = window.location.hash.slice(1) as Page;
  return PAGES.some(p => p.id === h) ? h : "overview";
};

function App() {
  const [page, setPage] = useState<Page>(fromHash);
  useEffect(() => {
    const on = () => setPage(fromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = (p: Page) => { window.location.hash = p; };
  const title = PAGES.find(p => p.id === page)!.label;

  return (
    <div className="flex h-full min-w-[1024px]">
      <Sidebar page={page} onNavigate={go} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar title={title} />
        <main className="flex-1 overflow-auto px-8 py-6">
          <div className="mx-auto max-w-[1400px]">
            <LiveMap />
            {page === "overview" && <Overview />}
            {page === "experiments" && <Experiments />}
            {page === "findings" && <Findings />}
            {page === "history" && <History />}
          </div>
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <LiveProvider>
        <App />
      </LiveProvider>
    </ToastProvider>
  </StrictMode>,
);
