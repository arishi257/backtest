import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outDir =
  "C:/Users/rishi/my_project/Backtest/runs_1s/sensex_batch_summary_20260531";
const outputPath = path.join(
  outDir,
  "sensex_fast_pnl_summary_oct2025_mar2026.xlsx",
);

const rows = [
  ["01-Oct-2025", 68.25, 55.77],
  ["09-Oct-2025", 33.37, 40.71],
  ["16-Oct-2025", 57.74, 81.38],
  ["23-Oct-2025", 86.86, 91.4],
  ["30-Oct-2025", 56.25, 97.46],
  ["06-Nov-2025", 34.66, 17.26],
  ["13-Nov-2025", -17.81, -23.48],
  ["20-Nov-2025", 61.03, 65.65],
  ["27-Nov-2025", -3.84, -23.99],
  ["04-Dec-2025", 19.79, 16.15],
  ["11-Dec-2025", -28.92, -6.58],
  ["18-Dec-2025", 69.19, 54.73],
  ["24-Dec-2025", 84.31, 86.25],
  ["01-Jan-2026", 20.13, 36.65],
  ["08-Jan-2026", -6.52, 11.09],
  ["14-Jan-2026", 12.88, 2.25],
  ["22-Jan-2026", -60.94, -112.78],
  ["29-Jan-2026", 44.66, 41.96],
  ["05-Feb-2026", 88.23, 94.32],
  ["12-Feb-2026", 95.81, 88.13],
  ["19-Feb-2026", -100.43, -102.21],
  ["26-Feb-2026", -6.27, -10.59],
  ["05-Mar-2026", 66.72, 78.31],
  ["12-Mar-2026", 45.56, 1.31],
  ["19-Mar-2026", 54.41, 105.13],
  ["25-Mar-2026", -0.98, 22.01],
];

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("SENSEX PnL Summary");

sheet.getRange("A1").values = [["SENSEX Fast PnL Summary"]];
sheet.getRange("A1:C1").merge();
sheet.getRange("A1").format.font.bold = true;
sheet.getRange("A1").format.font.size = 14;

sheet.getRange("A3:C3").values = [["Date", "Total PnL", "Frozen IV PnL"]];
sheet.getRangeByIndexes(3, 0, rows.length, 3).values = rows;

const header = sheet.getRange("A3:C3");
header.format.font.bold = true;
header.format.fill.color = "#1F4E78";
header.format.font.color = "#FFFFFF";
header.format.horizontalAlignment = "Center";

sheet.getRange("A:A").format.columnWidthPx = 120;
sheet.getRange("B:C").format.columnWidthPx = 125;
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
  sheetName: "SENSEX PnL Summary",
  range: `A1:C${summaryRow + 2}`,
  scale: 2,
});

await fs.mkdir(outDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
