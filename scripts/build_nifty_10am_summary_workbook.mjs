import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = "C:/Users/rishi/my_project/Backtest/runs_1s/10am";
const outputPath = path.join(outputDir, "nifty_10am_light_summary.xlsx");

const rows = [
  ["07-Oct-25", 42.74, 32.28],
  ["14-Oct-25", 25.88, 22.02],
  ["20-Oct-25", 124.66, 129.79],
  ["28-Oct-25", 57.27, 52.76],
  ["04-Nov-25", 100.21, 105.34],
  ["11-Nov-25", 20.34, 32.54],
  ["18-Nov-25", 102.38, 101.58],
  ["25-Nov-25", 49.77, 40.18],
  ["02-Dec-25", 89.56, 80.50],
  ["09-Dec-25", -2.28, -32.85],
  ["16-Dec-25", 89.68, 95.93],
  ["23-Dec-25", 64.72, 69.80],
  ["30-Dec-25", 47.78, 44.09],
  ["06-Jan-26", 25.05, 24.38],
  ["13-Jan-26", 7.34, 15.52],
  ["20-Jan-26", 87.74, 94.79],
  ["27-Jan-26", 16.90, -10.18],
  ["03-Feb-26", 150.67, 163.90],
  ["10-Feb-26", 87.58, 84.54],
  ["17-Feb-26", 42.17, 41.75],
  ["24-Feb-26", 69.67, 40.99],
  ["02-Mar-26", 41.29, -96.15],
  ["10-Mar-26", 86.51, 72.10],
  ["17-Mar-26", -15.69, -47.39],
  ["24-Mar-26", -13.49, 0.65],
  ["30-Mar-26", 122.81, 124.30],
  ["07-Apr-26", 101.20, 89.10],
  ["13-Apr-26", 62.35, 66.96],
];

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("NIFTY 10am Summary");

sheet.getRange("A1").values = [["NIFTY 10:00am Light Run Summary"]];
sheet.getRange("A1:C1").merge();
sheet.getRange("A1").format.font.bold = true;
sheet.getRange("A1").format.font.size = 14;

sheet.getRangeByIndexes(2, 0, rows.length + 1, 3).values = [
  ["Date", "Total PnL", "Frozen IV PnL"],
  ...rows,
];

const header = sheet.getRange("A3:C3");
header.format.font.bold = true;
header.format.fill.color = "#1F4E78";
header.format.font.color = "#FFFFFF";
header.format.horizontalAlignment = "Center";

sheet.getRange("A:A").format.columnWidthPx = 115;
sheet.getRange("B:C").format.columnWidthPx = 130;
sheet.getRange(`B4:C${rows.length + 3}`).numberFormat = "#,##0.00";
sheet.freezePanes.freezeRows(3);

const totalRow = rows.length + 5;
sheet.getRange(`A${totalRow}:C${totalRow}`).values = [[
  "Average",
  `=AVERAGE(B4:B${rows.length + 3})`,
  `=AVERAGE(C4:C${rows.length + 3})`,
]];
sheet.getRange(`A${totalRow}:C${totalRow}`).format.font.bold = true;
sheet.getRange(`A${totalRow}:C${totalRow}`).format.fill.color = "#D9EAF7";
sheet.getRange(`B${totalRow}:C${totalRow}`).numberFormat = "#,##0.00";

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 20 },
  summary: "formula error scan",
});
console.log(errors.ndjson);
await workbook.render({ sheetName: "NIFTY 10am Summary", range: "A1:C35", scale: 2 });

await fs.mkdir(outputDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
