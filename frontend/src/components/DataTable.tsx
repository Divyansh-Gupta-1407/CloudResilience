import { useMemo, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Search } from "lucide-react";
import { cx } from "../ui";

export interface Column<T> {
  key: keyof T & string;
  header: string;
  render?: (row: T) => ReactNode;
  sortValue?: (row: T) => string | number | null;
  className?: string;
}

/** Sortable, filterable table. `filterable` adds a text filter over all column values. */
export function DataTable<T>({ rows, columns, filterable, empty = "No rows", maxHeight = 480, initialSort }: {
  rows: T[]; columns: Column<T>[]; filterable?: boolean; empty?: string; maxHeight?: number;
  initialSort?: { key: string; dir: "asc" | "desc" };
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState(initialSort ?? null);

  const view = useMemo(() => {
    const q = query.trim().toLowerCase();
    let out = q ? rows.filter(r => columns.some(c => String(r[c.key] ?? "").toLowerCase().includes(q))) : rows;
    if (sort) {
      const col = columns.find(c => c.key === sort.key);
      const val = (r: T) => (col?.sortValue ? col.sortValue(r) : (r[sort.key as keyof T] as unknown as string | number | null));
      out = [...out].sort((a, b) => {
        const x = val(a), y = val(b);
        if (x == null) return 1;
        if (y == null) return -1;
        const c = x < y ? -1 : x > y ? 1 : 0;
        return sort.dir === "asc" ? c : -c;
      });
    }
    return out;
  }, [rows, columns, query, sort]);

  const toggle = (key: string) =>
    setSort(s => (!s || s.key !== key ? { key, dir: "asc" } : s.dir === "asc" ? { key, dir: "desc" } : null));

  return (
    <div>
      {filterable && (
        <div className="mb-3 flex items-center gap-2">
          <div className="relative w-72">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
            <input value={query} onChange={e => setQuery(e.target.value)} placeholder="Filter…"
              className="h-8 w-full rounded-lg border border-line bg-surface-2 pl-8 pr-3 text-[13px] outline-none focus:border-accent/60" />
          </div>
          <span className="font-mono text-xs text-ink-3">{view.length} / {rows.length}</span>
        </div>
      )}
      <div className="overflow-auto rounded-lg border border-line" style={{ maxHeight }}>
        <table className="w-full border-collapse text-[13px]">
          <thead className="sticky top-0 z-10 bg-surface-2">
            <tr>
              {columns.map(c => (
                <th key={c.key} onClick={() => toggle(c.key)}
                  className="cursor-pointer select-none whitespace-nowrap border-b border-line px-3 py-2 text-left text-[11.5px] font-semibold uppercase tracking-wider text-ink-3 hover:text-ink-2">
                  <span className="inline-flex items-center gap-1">
                    {c.header}
                    {sort?.key === c.key ? (sort.dir === "asc" ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />) : <ArrowUpDown className="h-3 w-3 opacity-30" />}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.length === 0 ? (
              <tr><td colSpan={columns.length} className="px-3 py-8 text-center text-ink-3">{empty}</td></tr>
            ) : view.map((r, i) => (
              <tr key={i} className="border-b border-line/60 last:border-0 hover:bg-surface-2/60">
                {columns.map(c => (
                  <td key={c.key} className={cx("px-3 py-2 align-top", c.className)}>{c.render ? c.render(r) : String(r[c.key] ?? "—")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
