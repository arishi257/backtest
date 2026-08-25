import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "C:/Users/rishi/my_project/Backtest";
const runDir = process.env.RUN_DIR || path.join(root, "outputs");
const underlying = process.env.UNDERLYING || "NIFTY";
const tradeDate = process.env.TRADE_DATE || "2026-05-26";
const startTimeLabel = process.env.START_TIME_LABEL || "09:20";
const intervalLabel = process.env.INTERVAL_LABEL || "1s";
const cleanInputs = process.env.CLEAN_INPUTS === "1";
const diagnosticsCsvPath = path.join(runDir, "diagnostics.csv");
const portfolioCsvPath = path.join(runDir, "portfolio_0920.csv");
const summaryCsvPath = path.join(runDir, "summary_metrics.csv");
const outputPath = path.join(runDir, `${underlying.toLowerCase()}_${intervalLabel}_${tradeDate.replaceAll("-", "")}_diagnostics.xlsx`);

const diagnosticsRows = parseCsv(await fs.readFile(diagnosticsCsvPath, "utf8"));
const portfolioRows = parseCsv(await fs.readFile(portfolioCsvPath, "utf8"));
const summaryMetricRows = parseCsv(await fs.readFile(summaryCsvPath, "utf8"));
const summaryMetrics = Object.fromEntries(
  summaryMetricRows.map((row) => [row.metric, cellValue(row.value)]),
);

const workbook = Workbook.create();
const liveSheet = workbook.worksheets.add("Live Diagnostics");
const frozenSheet = workbook.worksheets.add("Frozen IV Diagnostics");
const summary = workbook.worksheets.add("Summary");

const liveColumns = [
  "timestamp",
  "universal_mid",
  "running_total_pnl",
  "gamma_lots",
  "threshold_lots",
  "net_delta_lots_options_plus_hedge",
  "hedge_delta_lots",
  "traded_delta_lots",
  "traded_universal_mid",
];
const frozenColumns = [
  "timestamp",
  "universal_mid",
  "frozen_iv_running_total_pnl",
  "frozen_iv_gamma_lots",
  "frozen_iv_threshold_lots",
  "frozen_iv_net_delta_lots_options_plus_hedge",
  "frozen_iv_hedge_delta_lots",
  "frozen_iv_traded_delta_lots",
  "frozen_iv_traded_universal_mid",
];

writeSheet(liveSheet, liveColumns, diagnosticsRows);
writeSheet(frozenSheet, frozenColumns, diagnosticsRows);
writeSummary(summary, diagnosticsRows, portfolioRows, summaryMetrics);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 20 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

await workbook.render({ sheetName: "Summary", range: "A1:I38", scale: 2 });
await workbook.render({ sheetName: "Live Diagnostics", range: "A1:I25", scale: 2 });
await workbook.render({ sheetName: "Frozen IV Diagnostics", range: "A1:I25", scale: 2 });

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
if (cleanInputs) {
  await Promise.allSettled([
    fs.rm(diagnosticsCsvPath, { force: true }),
    fs.rm(portfolioCsvPath, { force: true }),
    fs.rm(summaryCsvPath, { force: true }),
  ]);
}
console.log(outputPath);

function writeSheet(sheet, columns, rows) {
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
  sheet.getRange("B:I").format.columnWidthPx = 120;
  sheet.getRange("B:I").numberFormat = "#,##0.00";
}

function writeSummary(sheet, rows, portfolioRows, summaryMetrics) {
  const first = rows[0];
  const final = rows[rows.length - 1];
  sheet.getRange("A1").values = [[`${underlying} ${intervalLabel} Diagnostics - ${tradeDate}`]];
  sheet.getRange("A1:I1").merge();
  sheet.getRange("A1").format.font.bold = true;
  sheet.getRange("A1").format.font.size = 14;
  sheet.getRange("A3:B25").values = [
    ["Metric", "Value"],
    ["Rows", rows.length],
    ["First timestamp", first.timestamp],
    ["Last timestamp", final.timestamp],
    [`Universal mid @ ${startTimeLabel}`, cellValue(first.universal_mid)],
    ["portfolio_total_pnl", summaryMetrics.portfolio_total_pnl],
    ["portfolio_gamma_l", summaryMetrics.portfolio_gamma_l],
    ["portfolio_gamma_diff_total", summaryMetrics.portfolio_gamma_diff_total],
    ["park_gamma_pnl_diff_total", summaryMetrics.park_gamma_pnl_diff_total],
    ["gk_gamma_pnl_diff_total", summaryMetrics.gk_gamma_pnl_diff_total],
    ["frozen_iv_total_pnl", summaryMetrics.frozen_iv_total_pnl],
    ["c2c_spot_vol", summaryMetrics.c2c_spot_vol],
    ["park_vol", summaryMetrics.park_vol],
    ["gk_vol", summaryMetrics.gk_vol],
    ["c2c_synth_vol", summaryMetrics.c2c_synth_vol],
    ["hedge_vol", summaryMetrics.hedge_vol],
    ["Final live threshold lots", cellValue(final.threshold_lots)],
    ["Final live net delta lots", cellValue(final.net_delta_lots_options_plus_hedge)],
    ["Final frozen-IV gamma lots", cellValue(final.frozen_iv_gamma_lots)],
    ["Final frozen-IV net delta lots", cellValue(final.frozen_iv_net_delta_lots_options_plus_hedge)],
    ["Liquidation timestamp", summaryMetrics.liquidation_timestamp],
    ["Liquidation gamma lots", summaryMetrics.liquidation_gamma_l],
  ];
  sheet.getRange("A3:B3").format.font.bold = true;
  sheet.getRange("A3:B3").format.fill.color = "#D9EAF7";
  sheet.getRange("A:A").format.columnWidthPx = 190;
  sheet.getRange("B:B").format.columnWidthPx = 145;
  sheet.getRange("B14:B18").numberFormat = '0.00"%"';
  sheet.getRange("B8:B13").numberFormat = "#,##0.00";
  sheet.getRange("B19:B25").numberFormat = "#,##0.00";

  sheet.getRange("A26:I26").values = [[
    "timestamp",
    "universal_mid",
    "underlying",
    "maturity",
    "strike",
    "option_type",
    "lots",
    "qty",
    "mult",
  ]];
  sheet.getRange("A26:I26").format.font.bold = true;
  sheet.getRange("A26:I26").format.fill.color = "#D9EAF7";
  if (portfolioRows.length) {
    sheet.getRangeByIndexes(26, 0, portfolioRows.length, 9).values = portfolioRows.map((row) => [
      row.timestamp,
      cellValue(row.universal_mid),
      row.underlying,
      row.maturity,
      cellValue(row.strike),
      row.option_type,
      cellValue(row.lots),
      cellValue(row.qty),
      cellValue(row.mult),
    ]);
  }
  sheet.getRange("A:I").format.columnWidthPx = 120;
  sheet.getRange("B:B").numberFormat = "#,##0.00";
}

function cellValue(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && String(value).trim() !== "" ? number : value;
}

function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/);
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
