import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outDir =
  "C:/Users/rishi/my_project/Backtest/runs_1s/nifty_light_summary_20260531";
const outputPath = path.join(
  outDir,
  "nifty_light_run_summary_oct2025_may2026.xlsx",
);

const rows = [
  ["07-Oct-2025", 40.91, 34.26],
  ["14-Oct-2025", -16.22, -39.28],
  ["20-Oct-2025", -16.07, -29.1],
  ["28-Oct-2025", 42.68, 43.8],
  ["04-Nov-2025", 98.9, 98.16],
  ["11-Nov-2025", 37.45, 66.4],
  ["18-Nov-2025", 123.61, 125.74],
  ["25-Nov-2025", 53.68, 41.94],
  ["02-Dec-2025", 50.49, 52.56],
  ["09-Dec-2025", -15.67, -24.64],
  ["16-Dec-2025", 101.33, 116.14],
  ["23-Dec-2025", 100.6, 100.61],
  ["30-Dec-2025", 23.22, 32.2],
  ["06-Jan-2026", 33.82, 35.4],
  ["13-Jan-2026", -69.34, -75.35],
  ["20-Jan-2026", 1.14, 22.14],
  ["27-Jan-2026", 34.31, 73.35],
  ["03-Feb-2026", 285.98, 369.77],
  ["10-Feb-2026", 100.92, 90.78],
  ["17-Feb-2026", -13.84, -22.59],
  ["24-Feb-2026", 62.76, 21.95],
  ["02-Mar-2026", 95.28, -19.58],
  ["10-Mar-2026", 133.79, 147.3],
  ["17-Mar-2026", -81.79, -58.48],
  ["24-Mar-2026", 61.59, 21.88],
  ["30-Mar-2026", 199.59, 194.4],
  ["07-Apr-2026", -22.35, -12.57],
  ["13-Apr-2026", 29.33, -0.47],
  ["21-Apr-2026", 143.94, 146.28],
  ["28-Apr-2026", 2.96, 13.15],
  ["05-May-2026", 65.63, 64.22],
  ["12-May-2026", -17.02, 19.57],
  ["19-May-2026", 75.23, 63.96],
  ["26-May-2026", 46.43, 59.29],
];

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("NIFTY Light Summary");

sheet.getRange("A1").values = [["NIFTY Light Run Summary"]];
sheet.getRange("A1:C1").merge();
sheet.getRange("A1").format.font.bold = true;
sheet.getRange("A1").format.font.size = 14;

sheet.getRange("A3:C3").values = [["Date", "Portfolio PnL", "Frozen IV PnL"]];
sheet.getRangeByIndexes(3, 0, rows.length, 3).values = rows;

const header = sheet.getRange("A3:C3");
header.format.font.bold = true;
header.format.fill.color = "#1F4E78";
header.format.font.color = "#FFFFFF";
header.format.horizontalAlignment = "Center";

sheet.getRange("A:A").format.columnWidthPx = 120;
sheet.getRange("B:C").format.columnWidthPx = 130;
sheet.getRange(`B4:C${rows.length + 3}`).numberFormat = "#,##0.00";
sheet.freezePanes.freezeRows(3);

const summaryRow = rows.length + 5;
sheet.getRange(`A${summaryRow}:C${summaryRow + 2}`).values = [
  ["Summary", "Portfolio PnL", "Frozen IV PnL"],
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
  sheetName: "NIFTY Light Summary",
  range: `A1:C${summaryRow + 2}`,
  scale: 2,
});

await fs.mkdir(outDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
