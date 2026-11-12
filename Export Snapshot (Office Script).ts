/**
 * EXPORT SNAPSHOT — The Wayward Kitchen
 *
 * Returns the at-a-glance numbers for the shifts site as JSON. Nothing in the workbook is changed.
 * Used by a scheduled Power Automate flow:
 *   Recurrence → Excel Online (Business) "Run script" (this script) → send `result` to the site.
 *
 * Only these items leave the workbook: balances, pending bank items, bills due soon, payroll totals.
 */
interface PendingItem { date: string; description: string; amount: number; }
interface BillDue { due: string; name: string; amount: number; status: string; source: string; }
interface PayrollRun { date: string; netPay: number; taxes: number; total: number; }
interface Snapshot {
  generatedAt: string;
  bankSyncedAt: string;
  currentBalance: number;
  availableBalance: number;
  pending: PendingItem[];
  pendingTotal: number;
  billsDue: BillDue[];
  billsDueTotal: number;
  payroll: { lastRun: PayrollRun | null; monthToDate: number; previousMonth: number; };
}

function main(workbook: ExcelScript.Workbook, daysAhead: number = 14): Snapshot {
  const iso = (serial: number) => {
    const d = new Date(Math.round((serial - 25569) * 86400000));
    return d.toISOString().substring(0, 10);
  };
  const toSerial = (v: string | number | boolean): number => {
    if (typeof v === "number") return v;
    const d = new Date(String(v));
    return isNaN(d.getTime()) ? NaN : Math.round(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000) + 25569;
  };
  const num = (v: string | number | boolean) => { const n = typeof v === "number" ? v : parseFloat(String(v)); return isNaN(n) ? 0 : n; };
  const r2 = (n: number) => Math.round(n * 100) / 100;
  const now = new Date();
  const today = Math.round(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 86400000) + 25569;
  const tableRows = (name: string) => {
    const t = workbook.getTable(name);
    if (!t) return { h: [] as string[], rows: [] as (string | number | boolean)[][] };
    return { h: t.getHeaderRowRange().getValues()[0].map(String), rows: t.getRangeBetweenHeaderAndTotal().getValues() };
  };

  // ---- balances (Accounts tab row 2)
  const acct = workbook.getWorksheet("Accounts").getRange("A1:M2").getValues();
  const ah = acct[0].map(String);
  const currentBalance = num(acct[1][ah.indexOf("current_balance")]);
  const availableBalance = num(acct[1][ah.indexOf("available_balance")]);
  const bankSyncedAt = String(acct[1][ah.indexOf("last_synced_at")]);

  // ---- pending bank transactions
  const tx = tableRows("tbltransactions");
  const ti = (n: string) => tx.h.indexOf(n);
  // The bank-sync add-in keeps a pending row after the purchase posts, so skip pending rows that have a
  // posted twin (same amount, posted 0-7 days later, synced later). Each posted row cancels one pending row.
  const isPending = (r: (string | number | boolean)[]) => r[ti("pending")] === true || String(r[ti("pending")]).toUpperCase() === "TRUE";
  const posted = tx.rows.filter(r => !isPending(r));
  const used = new Set<number>();
  const pending: PendingItem[] = tx.rows
    .filter(r => isPending(r))
    .sort((a, b) => toSerial(a[ti("date")]) - toSerial(b[ti("date")]))
    .filter(r => {
      const d = toSerial(r[ti("date")]), amt = r2(num(r[ti("amount")])), synced = String(r[ti("synced_at")] ?? "");
      const twin = posted.findIndex((p, i) => !used.has(i) && r2(num(p[ti("amount")])) === amt &&
        toSerial(p[ti("date")]) - d >= 0 && toSerial(p[ti("date")]) - d <= 7 && String(p[ti("synced_at")] ?? "") >= synced);
      if (twin >= 0) { used.add(twin); return false; }
      return true;
    })
    .map(r => ({
      date: iso(toSerial(r[ti("date")])),
      description: String(r[ti("merchant_name")] || String(r[ti("description_raw")]).split(" / ")[0]),
      amount: r2(-num(r[ti("amount")])),               // negative = money out
    }))
    .sort((a, b) => a.date.localeCompare(b.date));

  // ---- bills due (Bills tab: open bills; Recurring tab: expected in the next N days)
  const billsDue: BillDue[] = [];
  const b = tableRows("tblBills");
  const bi = (n: string) => b.h.indexOf(n);
  b.rows.forEach(r => {
    const status = String(r[bi("Status")]);
    if (!status || status === "Paid") return;
    const due = toSerial(r[bi("Due Date")]);
    if (!isNaN(due) && due > today + daysAhead) return;
    billsDue.push({ due: isNaN(due) ? "" : iso(due), name: String(r[bi("Vendor")]), amount: r2(num(r[bi("Amount")])), status, source: "Bill" });
  });
  const rc = tableRows("tblRecurring");
  const ri = (n: string) => rc.h.indexOf(n);
  rc.rows.forEach(r => {
    const next = toSerial(r[ri("Next Expected")]);
    if (!String(r[ri("Name")]) || isNaN(next) || next > today + daysAhead) return;
    const amt = num(r[ri("Typical Amount")]) || num(r[ri("Last Amount")]) || num(r[ri("Avg (90 days)")]);
    billsDue.push({ due: iso(next), name: String(r[ri("Name")]), amount: r2(amt), status: String(r[ri("Status")]),
      source: String(r[ri("Autopay")]) === "Yes" ? "Recurring (autopay)" : "Recurring" });
  });
  billsDue.sort((x, y) => (x.due || "9999").localeCompare(y.due || "9999"));

  // ---- payroll from Checkbook (Gusto withdrawals + checks categorized as payroll)
  const cb = tableRows("tblRegister");
  const ci = (n: string) => cb.h.indexOf(n);
  const payCats = ["Payroll - Wages", "Payroll Taxes", "Payroll Processing Fees"];
  const monthStart = toSerial(`${now.getMonth() + 1}/1/${now.getFullYear()}`);
  const prevStart = toSerial(`${now.getMonth() === 0 ? 12 : now.getMonth()}/1/${now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear()}`);
  let mtd = 0, prev = 0;
  const gustoByDate = new Map<number, { net: number, tax: number }>();
  cb.rows.forEach(r => {
    const cat = String(r[ci("Category")]);
    if (payCats.indexOf(cat) < 0) return;
    const d = toSerial(r[ci("Date")]);
    const out = -num(r[ci("Net Amount")]);
    if (d >= monthStart) mtd += out; else if (d >= prevStart) prev += out;
    const desc = String(r[ci("Description")]).toUpperCase();
    if (desc.indexOf("GUSTO/NET") >= 0 || desc.indexOf("GUSTO/TAX") >= 0) {
      const g = gustoByDate.get(d) || { net: 0, tax: 0 };
      if (desc.indexOf("GUSTO/NET") >= 0) g.net += out; else g.tax += out;
      gustoByDate.set(d, g);
    }
  });
  let lastRun: PayrollRun | null = null;
  const lastDate = Math.max(...Array.from(gustoByDate.keys()));
  if (isFinite(lastDate)) {
    const g = gustoByDate.get(lastDate);
    lastRun = { date: iso(lastDate), netPay: r2(g.net), taxes: r2(g.tax), total: r2(g.net + g.tax) };
  }

  const snap: Snapshot = {
    generatedAt: now.toISOString(),
    bankSyncedAt,
    currentBalance: r2(currentBalance),
    availableBalance: r2(availableBalance),
    pending,
    pendingTotal: r2(pending.reduce((s, p) => s + p.amount, 0)),
    billsDue,
    billsDueTotal: r2(billsDue.reduce((s, x) => s + x.amount, 0)),
    payroll: { lastRun, monthToDate: r2(mtd), previousMonth: r2(prev) },
  };
  console.log(JSON.stringify(snap, null, 2));
  return snap;
}
