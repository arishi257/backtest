import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "C:/Users/rishi/my_project/Backtest";
const cachePath = path.join(root, ".backtest_data_cache", "nifty_1m_0dte_dates_2019_2026.json");
const outputDir = path.join(root, "runs_1m");
const outputPath = path.join(outputDir, "nifty_0dte_dates_2019_2026.xlsx");

const payload = JSON.parse(await fs.readFile(cachePath, "utf8"));
const workbook = Workbook.create();

for (const year of payload.years.map(String)) {
  const sheet = workbook.worksheets.add(year);
  const rows = (payload.results[year] || []).map((isoDate) => [
    formatDate(isoDate),
  ]);
  sheet.getRange("A1").values = [[`NIFTY 0DTE Dates - ${year}`]];
  sheet.getRange("A1").format.font.bold = true;
  sheet.getRange("A1").format.font.size = 14;
  sheet.getRange("A3").values = [["Date"]];
  sheet.getRange("A3").format.font.bold = true;
  sheet.getRange("A3").format.fill.color = "#1F4E78";
  sheet.getRange("A3").format.font.color = "#FFFFFF";
  if (rows.length) {
    sheet.getRangeByIndexes(3, 0, rows.length, 1).values = rows;
  }
  const countRow = rows.length + 5;
  sheet.getRange(`A${countRow}`).values = [[`Count: ${rows.length}`]];
  sheet.getRange(`A${countRow}`).format.font.bold = true;
  sheet.getRange("A:A").format.columnWidthPx = 130;
  sheet.freezePanes.freezeRows(3);
}

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 20 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

for (const year of payload.years.map(String)) {
  await workbook.render({ sheetName: year, range: "A1:A20", scale: 2 });
}

await fs.mkdir(outputDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);

function formatDate(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${String(day).padStart(2, "0")}-${months[month - 1]}-${String(year).slice(2)}`;
}
