import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "C:/Users/rishi/my_project/Backtest";
const dataPath = path.join(root, "runs_1s", "light_pnl_interval_matrix_data.json");
const outDir = path.join(root, "runs_1s", "light_pnl_interval_matrix_20260531");
const outputPath = path.join(outDir, "light_pnl_10min_interval_matrix.xlsx");

const data = JSON.parse(await fs.readFile(dataPath, "utf8"));
const intervals = data.intervals;
const workbook = Workbook.create();

writeMatrix(
  workbook.worksheets.add("NIFTY Portfolio"),
  "NIFTY Portfolio PnL: 10-minute interval changes",
  data.underlyings.NIFTY,
  "portfolio",
);
writeMatrix(
  workbook.worksheets.add("NIFTY Frozen IV"),
  "NIFTY Frozen IV PnL: 10-minute interval changes",
  data.underlyings.NIFTY,
  "frozen_iv",
);
writeMatrix(
  workbook.worksheets.add("SENSEX Portfolio"),
  "SENSEX Portfolio PnL: 10-minute interval changes",
  data.underlyings.SENSEX,
  "portfolio",
);
writeMatrix(
  workbook.worksheets.add("SENSEX Frozen IV"),
  "SENSEX Frozen IV PnL: 10-minute interval changes",
  data.underlyings.SENSEX,
  "frozen_iv",
);
writeCoverage(workbook.worksheets.add("Coverage"));

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 20 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

for (const sheetName of [
  "NIFTY Portfolio",
  "NIFTY Frozen IV",
  "SENSEX Portfolio",
  "SENSEX Frozen IV",
  "Coverage",
]) {
  await workbook.render({ sheetName, range: "A1:H12", scale: 2 });
}

await fs.mkdir(outDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);

function writeMatrix(sheet, title, rows, key) {
  const finalKey = key === "portfolio" ? "portfolio_final" : "frozen_iv_final";
  const headers = ["Date", ...intervals, "Final PnL"];
  const values = [
    headers,
    ...rows.map((row) => [
      row.date,
      ...row[key].map((value) => value ?? null),
      row[finalKey] ?? null,
    ]),
  ];
  const avgRow = values.length + 2;
  const sumRow = values.length + 3;

  sheet.getRange("A1").values = [[title]];
  sheet.getRangeByIndexes(0, 0, 1, headers.length).merge();
  sheet.getRange("A1").format.font.bold = true;
  sheet.getRange("A1").format.font.size = 14;
  sheet.getRangeByIndexes(2, 0, values.length, headers.length).values = values;

  const header = sheet.getRangeByIndexes(2, 0, 1, headers.length);
  header.format.font.bold = true;
  header.format.fill.color = "#1F4E78";
  header.format.font.color = "#FFFFFF";
  header.format.horizontalAlignment = "Center";

  sheet.getRange(`A${avgRow}:A${sumRow}`).values = [["Average"], ["Sum"]];
  for (let col = 2; col <= headers.length; col += 1) {
    const letter = columnLetter(col);
    sheet.getRangeByIndexes(avgRow - 1, col - 1, 2, 1).values = [
      [`=AVERAGE(${letter}4:${letter}${rows.length + 3})`],
      [`=SUM(${letter}4:${letter}${rows.length + 3})`],
    ];
  }
  const summary = sheet.getRangeByIndexes(avgRow - 1, 0, 2, headers.length);
  summary.format.font.bold = true;
  summary.format.fill.color = "#D9EAF7";

  sheet.freezePanes.freezeRows(3);
  sheet.getRange("A:A").format.columnWidthPx = 120;
  sheet.getRangeByIndexes(0, 1, 1, headers.length - 1).format.columnWidthPx = 105;
  sheet.getRangeByIndexes(3, 1, rows.length + 2, headers.length - 1).numberFormat = "#,##0.00";
}

function writeCoverage(sheet) {
  const rows = [];
  for (const underlying of ["NIFTY", "SENSEX"]) {
    for (const row of data.underlyings[underlying]) {
      rows.push([underlying, row.date, row.workbook]);
    }
  }
  sheet.getRange("A1").values = [["Light-run source coverage"]];
  sheet.getRange("A1:C1").merge();
  sheet.getRange("A1").format.font.bold = true;
  sheet.getRange("A1").format.font.size = 14;
  sheet.getRangeByIndexes(2, 0, rows.length + 1, 3).values = [
    ["Underlying", "Date", "Workbook"],
    ...rows,
  ];
  const header = sheet.getRange("A3:C3");
  header.format.font.bold = true;
  header.format.fill.color = "#1F4E78";
  header.format.font.color = "#FFFFFF";
  sheet.getRange("A:A").format.columnWidthPx = 90;
  sheet.getRange("B:B").format.columnWidthPx = 120;
  sheet.getRange("C:C").format.columnWidthPx = 1100;
}

function columnLetter(index) {
  let n = index;
  let s = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}
