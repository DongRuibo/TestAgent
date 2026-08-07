import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [, , artifactRoot, inputPath, outputPath, qaDir] = process.argv;
if (!artifactRoot || !inputPath || !outputPath || !qaDir) {
  throw new Error("Usage: node render_briefing.mjs <artifact-tool-root> <analysis.json> <output.pptx> <qa-dir>");
}

const artifact = await import(pathToFileURL(path.join(path.resolve(artifactRoot), "dist/artifact_tool.mjs")).href);
const { Presentation, PresentationFile } = artifact;
const analysis = JSON.parse(await fs.readFile(inputPath, "utf8"));
const summary = analysis.summary || {};
const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const colors = { ink: "#0F172A", muted: "#475569", blue: "#2563EB", pale: "#EFF6FF", line: "#CBD5E1", white: "#FFFFFF" };

function textbox(slide, value, position, fontSize, options = {}) {
  const shape = slide.shapes.add({ geometry: "textbox", position, fill: "none", line: { style: "solid", fill: "none", width: 0 } });
  shape.text = String(value ?? "");
  shape.text.style = { fontSize, typeface: "Arial", color: options.color || colors.ink, bold: options.bold || false, autoFit: "shrinkText", verticalAlignment: options.verticalAlignment || "top" };
  return shape;
}

function baseSlide(title, number) {
  const slide = presentation.slides.add();
  slide.background.fill = colors.white;
  textbox(slide, title, { left: 56, top: 36, width: 1120, height: 72 }, 38, { bold: true });
  textbox(slide, number, { left: 1180, top: 660, width: 44, height: 24 }, 13, { color: colors.muted });
  return slide;
}

{
  const slide = presentation.slides.add();
  slide.background.fill = colors.white;
  textbox(slide, "BUSINESS DATA", { left: 56, top: 48, width: 420, height: 32 }, 18, { color: colors.blue, bold: true });
  textbox(slide, "经营数据简报", { left: 56, top: 190, width: 900, height: 120 }, 66, { bold: true, verticalAlignment: "bottom" });
  textbox(slide, analysis.source_file || "Spreadsheet", { left: 56, top: 345, width: 720, height: 60 }, 28, { color: colors.muted });
  textbox(slide, "结构 · 质量 · 异常 · 管理提示", { left: 56, top: 540, width: 720, height: 44 }, 22, { color: colors.muted });
}

{
  const slide = baseSlide("核心经营概览", "02");
  const cards = [
    ["数据规模", `${summary.row_count || 0} 行`, `${summary.sheet_count || 0} 个工作表`],
    ["数据完整性", `${summary.missing_ratio_percent || 0}%`, "缺失单元格占比"],
    ["异常提示", `${summary.anomaly_count || 0} 个`, "IQR 规则识别"],
  ];
  cards.forEach(([label, stat, detail], index) => {
    const left = 56 + index * 402;
    slide.shapes.add({ geometry: "roundRect", position: { left, top: 220, width: 360, height: 330 }, fill: colors.pale, line: { style: "solid", fill: colors.line, width: 1 } });
    textbox(slide, label, { left: left + 28, top: 250, width: 300, height: 40 }, 20, { color: colors.blue, bold: true });
    textbox(slide, stat, { left: left + 28, top: 340, width: 300, height: 80 }, 46, { bold: true });
    textbox(slide, detail, { left: left + 28, top: 460, width: 300, height: 48 }, 18, { color: colors.muted });
  });
}

{
  const slide = baseSlide(analysis.chart?.title || "数据分布", "03");
  const labels = (analysis.chart?.labels || []).slice(0, 5);
  const values = (analysis.chart?.values || []).slice(0, 5).map(Number);
  if (labels.length) {
    slide.charts.add("bar", {
      position: { left: 56, top: 150, width: 700, height: 480 },
      categories: labels,
      series: [{ name: "Value", values, fill: colors.blue }],
      hasLegend: false,
      dataLabels: { showValue: true, position: "outEnd" },
      yAxis: { majorGridlines: { style: "solid", fill: colors.line, width: 1 } },
      barOptions: { direction: "column", grouping: "clustered", gapWidth: 80 },
    });
  }
  textbox(slide, "管理提示", { left: 820, top: 170, width: 330, height: 40 }, 24, { color: colors.blue, bold: true });
  textbox(slide, (analysis.management_notes || []).map((note) => `• ${note}`).join("\n\n"), { left: 820, top: 240, width: 350, height: 300 }, 20, { color: colors.muted });
}

{
  const slide = baseSlide("数据质量明细", "04");
  const sheet = (analysis.sheets || [])[0] || {};
  const columns = (sheet.columns || []).slice(0, 8);
  textbox(slide, `${sheet.name || "Primary sheet"} · ${sheet.row_count || 0} 行 · ${sheet.column_count || 0} 列`, { left: 56, top: 125, width: 1120, height: 44 }, 22, { color: colors.muted });
  const leftText = columns.slice(0, 4).map((column) => `${column.name}\n类型：${column.inferred_type}  缺失：${column.missing_count}`).join("\n\n");
  const rightText = columns.slice(4, 8).map((column) => `${column.name}\n类型：${column.inferred_type}  缺失：${column.missing_count}`).join("\n\n");
  slide.shapes.add({ geometry: "roundRect", position: { left: 56, top: 205, width: 550, height: 390 }, fill: colors.pale, line: { style: "solid", fill: colors.line, width: 1 } });
  slide.shapes.add({ geometry: "roundRect", position: { left: 650, top: 205, width: 550, height: 390 }, fill: colors.pale, line: { style: "solid", fill: colors.line, width: 1 } });
  textbox(slide, leftText || "暂无列信息", { left: 86, top: 235, width: 490, height: 330 }, 19);
  textbox(slide, rightText || "暂无更多列", { left: 680, top: 235, width: 490, height: 330 }, 19);
}

await fs.mkdir(qaDir, { recursive: true });
for (const [index, slide] of presentation.slides.items.entries()) {
  const png = await presentation.export({ slide, format: "png", scale: 1 });
  await fs.writeFile(path.join(qaDir, `slide-${String(index + 1).padStart(2, "0")}.png`), new Uint8Array(await png.arrayBuffer()));
  const layout = await slide.export({ format: "layout" });
  await fs.writeFile(path.join(qaDir, `slide-${String(index + 1).padStart(2, "0")}.layout.json`), await layout.text());
}
const montage = await presentation.export({ format: "webp", montage: true, scale: 1 });
await fs.writeFile(path.join(qaDir, "deck-montage.webp"), new Uint8Array(await montage.arrayBuffer()));
const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(outputPath);
