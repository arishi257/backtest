import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "C:/Users/rishi/my_project/Backtest";
const runDir = process.env.RUN_DIR || path.join(root, "outputs");
const underlying = process.env.UNDERLYING || "SENSEX";
const tradeDate = process.env.TRADE_DATE || "2026-04-16";
const cleanInputs = process.env.CLEAN_INPUTS === "1";
const pnlCsvPath = path.join(runDir, "pnl_timeseries.csv");
const summaryCsvPath = path.join(runDir, "summary_metrics.csv");
const outputPath = path.join(
  runDir,
  `${underlying.toLowerCase()}_1s_${tradeDate.replaceAll("-", "")}_pnl_only.xlsx`,
);

const pnlRows = parseCsv(await fs.readFile(pnlCsvPath, "utf8"));
const summaryRows = parseCsv(await fs.readFile(summaryCsvPath, "utf8"));
const summaryMetrics = Object.fromEntries(
  summaryRows.map((row) => [row.metric, cellValue(row.value)]),
);

const workbook = Workbook.create();
const summary = workbook.worksheets.add("Summary");
const pnlSheet = workbook.worksheets.add("PnL Timeseries");

writeSummary(summary, pnlRows, summaryMetrics);
writePnlSheet(pnlSheet, pnlRows);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 20 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

await workbook.render({ sheetName: "Summary", range: "A1:B14", scale: 2 });

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
if (cleanInputs) {
  await Promise.allSettled([
    fs.rm(pnlCsvPath, { force: true }),
    fs.rm(summaryCsvPath, { force: true }),
  ]);
}
console.log(outputPath);

function writeSummary(sheet, rows, metrics) {
  const first = rows[0] || {};
  const final = rows[rows.length - 1] || {};
  sheet.getRange("A1").values = [[`${underlying} 1s PnL Only - ${tradeDate}`]];
  sheet.getRange("A1:B1").merge();
  sheet.getRange("A1").format.font.bold = true;
  sheet.getRange("A1").format.font.size = 14;
  sheet.getRange("A3:B12").values = [
    ["Metric", "Value"],
    ["Rows", rows.length],
    ["First timestamp", first.timestamp ?? null],
    ["Last timestamp", final.timestamp ?? null],
    ["Universal mid at start", metrics.universal_mid_start],
    ["portfolio_total_pnl", metrics.portfolio_total_pnl],
    ["frozen_iv_total_pnl", metrics.frozen_iv_total_pnl],
    ["data_load_seconds", metrics.data_load_seconds],
    ["simulation_seconds", metrics.simulation_seconds],
    ["workbook_seconds", metrics.workbook_seconds],
  ];
  sheet.getRange("A3:B3").format.font.bold = true;
  sheet.getRange("A3:B3").format.fill.color = "#D9EAF7";
  sheet.getRange("A:A").format.columnWidthPx = 170;
  sheet.getRange("B:B").format.columnWidthPx = 145;
  sheet.getRange("B8:B12").numberFormat = "#,##0.00";
}

function writePnlSheet(sheet, rows) {
  const columns = [
    "timestamp",
    "universal_mid",
    "running_total_pnl",
    "frozen_iv_running_total_pnl",
  ];
  const values = [
    columns,
    ...rows.map((row) => columns.map((column) => cellValue(row[column]))),
  ];
  sheet.getRangeByIndexes(0, 0, values.length, columns.length).values = values;
  const used = sheet.getUsedRange();
  used.format.font.name = "Calibri";
  used.format.font.size = 10;
  const header = sheet.getRangeByIndexes(0, 0, 1, columns.length);
  header.format.font.bold = true;
  header.format.fill.color = "#1F4E78";
  header.format.font.color = "#FFFFFF";
  header.format.horizontalAlignment = "Center";
  sheet.freezePanes.freezeRows(1);
  sheet.getRange("A:A").format.columnWidthPx = 145;
  sheet.getRange("B:D").format.columnWidthPx = 135;
  sheet.getRange("B:D").numberFormat = "#,##0.00";
}

function cellValue(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && String(value).trim() !== "" ? number : value;
}

function parseCsv(text) {
  const trimmed = text.trim();
  if (!trimmed) return [];
  const lines = trimmed.split(/\r?\n/);
  if (lines.length <= 1) return [];
  const headers = splitCsvLine(lines[0]);
  return lines.slice(1).map((line) => {
    const values = splitCsvLine(line);
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
}

function splitCsvLine(line) {
  const values = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"' && line[i + 1] === '"') {
      current += '"';
      i += 1;
    } else if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === "," && !inQuotes) {
      values.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  values.push(current);
  return values;
}
