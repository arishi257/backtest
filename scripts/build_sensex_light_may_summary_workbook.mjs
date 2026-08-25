import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outDir =
  "C:/Users/rishi/my_project/Backtest/runs_1s/sensex_light_may_summary_20260531";
const outputPath = path.join(outDir, "sensex_light_run_summary_may2026.xlsx");

const rows = [
  ["07-May-2026", 4.56, -11.73],
  ["14-May-2026", -25.36, -22.15],
  ["21-May-2026", 21.78, 20.07],
  ["27-May-2026", 75.22, 83.08],
];

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("SENSEX May Light");

sheet.getRange("A1").values = [["SENSEX May 2026 Light Run Summary"]];
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

sheet.getRange("A:A").format.columnWidthPx = 125;
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
  sheetName: "SENSEX May Light",
  range: `A1:C${summaryRow + 2}`,
  scale: 2,
});

await fs.mkdir(outDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
