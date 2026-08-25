import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = "C:/Users/rishi/my_project/Backtest/runs_1s/10am";
const outputPath = path.join(outputDir, "sensex_10am_light_summary.xlsx");

const rows = [
  ["01-Oct-25", 76.10, 61.58],
  ["09-Oct-25", 18.52, 23.21],
  ["16-Oct-25", 24.77, 56.85],
  ["23-Oct-25", 25.25, 38.93],
  ["30-Oct-25", 86.37, 85.02],
  ["06-Nov-25", 10.29, 17.05],
  ["13-Nov-25", 14.35, 4.79],
  ["20-Nov-25", 75.23, 73.72],
  ["27-Nov-25", -17.74, -11.59],
  ["04-Dec-25", 24.60, 13.02],
  ["11-Dec-25", -18.43, -19.45],
  ["18-Dec-25", 46.32, 45.71],
  ["24-Dec-25", 41.00, 41.36],
  ["01-Jan-26", 40.56, 40.29],
  ["08-Jan-26", -24.57, -2.91],
  ["14-Jan-26", -23.20, -31.00],
  ["22-Jan-26", -76.67, -103.40],
  ["29-Jan-26", 35.61, 27.13],
  ["05-Feb-26", 128.03, 128.70],
  ["12-Feb-26", 76.08, 69.68],
  ["19-Feb-26", -90.63, -57.43],
  ["26-Feb-26", 31.93, 27.48],
  ["05-Mar-26", 11.60, 35.91],
  ["12-Mar-26", 167.27, 195.03],
  ["19-Mar-26", 144.91, 170.05],
  ["25-Mar-26", 123.33, 101.47],
  ["02-Apr-26", -40.34, 51.18],
  ["09-Apr-26", 167.83, 163.10],
  ["16-Apr-26", 56.51, 43.84],
  ["23-Apr-26", 79.89, 81.74],
  ["30-Apr-26", -6.23, -6.19],
  ["07-May-26", -3.67, -13.20],
  ["14-May-26", -61.38, -43.64],
  ["21-May-26", 15.48, 6.54],
  ["27-May-26", 81.69, 67.44],
];

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("SENSEX 10am Summary");

sheet.getRange("A1").values = [["SENSEX 10:00am Light Run Summary"]];
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
await workbook.render({ sheetName: "SENSEX 10am Summary", range: "A1:C42", scale: 2 });

await fs.mkdir(outputDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
