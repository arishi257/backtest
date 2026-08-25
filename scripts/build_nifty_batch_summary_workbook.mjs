import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outDir =
  "C:/Users/rishi/my_project/Backtest/runs_1s/nifty_batch_summary_20260531";
const outputPath = path.join(
  outDir,
  "nifty_fast_pnl_summary_oct2025_may2026.xlsx",
);

const rows = [
  ["07-Oct-2025", 40.91, 34.26, ""],
  ["14-Oct-2025", -16.22, -38.93, ""],
  ["20-Oct-2025", -16.07, -28.87, ""],
  ["28-Oct-2025", 42.68, 43.8, ""],
  ["04-Nov-2025", 98.9, 98.16, ""],
  ["11-Nov-2025", 37.45, 66.48, ""],
  ["18-Nov-2025", 123.61, 125.74, ""],
  ["25-Nov-2025", 53.16, 44.16, ""],
  ["02-Dec-2025", 50.49, 52.56, ""],
  ["09-Dec-2025", -15.67, -33.69, ""],
  ["16-Dec-2025", 101.33, 116.14, ""],
  ["23-Dec-2025", 100.6, 100.61, ""],
  ["30-Dec-2025", 23.22, 32.2, ""],
  ["06-Jan-2026", 33.82, 35.4, ""],
  ["13-Jan-2026", -69.34, -75.35, ""],
  ["20-Jan-2026", 1.14, 19.44, ""],
  ["27-Jan-2026", 34.31, 73.35, ""],
  ["03-Feb-2026", null, null, "Could not initialize sample portfolio"],
  ["10-Feb-2026", 100.92, 90.78, ""],
  ["17-Feb-2026", -13.84, -22.59, ""],
  ["24-Feb-2026", 62.76, 22.04, ""],
  ["02-Mar-2026", 95.28, -19.58, ""],
  ["10-Mar-2026", 133.79, 153.11, ""],
  ["17-Mar-2026", -81.79, -69.96, ""],
  ["24-Mar-2026", 61.59, 21.63, ""],
  ["30-Mar-2026", 199.59, 194.4, ""],
  ["07-Apr-2026", -22.35, -12.57, ""],
  ["13-Apr-2026", 29.33, 1.13, ""],
  ["21-Apr-2026", 143.94, 146.28, ""],
  ["28-Apr-2026", 2.96, 13.15, ""],
  ["05-May-2026", 65.63, 64.22, ""],
  ["12-May-2026", -17.02, 19.57, ""],
  ["19-May-2026", 75.23, 63.96, ""],
  ["26-May-2026", 46.43, 59.41, ""],
];

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("NIFTY PnL Summary");

sheet.getRange("A1").values = [["NIFTY Fast PnL Summary"]];
sheet.getRange("A1:D1").merge();
sheet.getRange("A1").format.font.bold = true;
sheet.getRange("A1").format.font.size = 14;

sheet.getRange("A3:D3").values = [["Date", "Total PnL", "Frozen IV PnL", "Note"]];
sheet.getRangeByIndexes(3, 0, rows.length, 4).values = rows;

const header = sheet.getRange("A3:D3");
header.format.font.bold = true;
header.format.fill.color = "#1F4E78";
header.format.font.color = "#FFFFFF";
header.format.horizontalAlignment = "Center";

sheet.getRange("A:A").format.columnWidthPx = 120;
sheet.getRange("B:C").format.columnWidthPx = 125;
sheet.getRange("D:D").format.columnWidthPx = 230;
sheet.getRange(`B4:C${rows.length + 3}`).numberFormat = "#,##0.00";
sheet.freezePanes.freezeRows(3);

const summaryRow = rows.length + 5;
sheet.getRange(`A${summaryRow}:C${summaryRow + 2}`).values = [
  ["Summary", "Total PnL", "Frozen IV PnL"],
  ["Average", `=AVERAGE(B4:B${rows.length + 3})`, `=AVERAGE(C4:C${rows.length + 3})`],
  ["Sum", `=SUM(B4:B${rows.length + 3})`, `=SUM(C4:C${rows.length + 3})`],
];
sheet.getRange(`A${summaryRow}:C${summaryRow}`).format.font.bold = true;
sheet.getRange(`A${summaryRow}:C${summaryRow}`).format.fill.color = "#D9EAF7";
sheet.getRange(`B${summaryRow + 1}:C${summaryRow + 2}`).numberFormat = "#,##0.00";

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 20 },
  summary: "formula error scan",
});
console.log(errors.ndjson);
await workbook.render({
  sheetName: "NIFTY PnL Summary",
  range: `A1:D${summaryRow + 2}`,
  scale: 2,
});

await fs.mkdir(outDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
