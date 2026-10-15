import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";

const [artworkPath, logoPath, outputPath] = process.argv.slice(2);

if (!artworkPath || !logoPath || !outputPath) {
  throw new Error("Usage: node compose_product_poster_4k.mjs <artwork.png> <logo.svg> <output.svg>");
}

const toDataUri = async (path, mimeType) => {
  const data = await readFile(resolve(path));
  return `data:${mimeType};base64,${data.toString("base64")}`;
};

const artwork = await toDataUri(artworkPath, "image/png");
const logo = await toDataUri(logoPath, "image/svg+xml");

const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="3840" height="1792" viewBox="0 0 3840 1792">
  <defs>
    <linearGradient id="leftShade" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#041725" stop-opacity="0.96"/>
      <stop offset="0.64" stop-color="#071E32" stop-opacity="0.88"/>
      <stop offset="1" stop-color="#071E32" stop-opacity="0.20"/>
    </linearGradient>
    <linearGradient id="titleFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#F9FCFD"/>
      <stop offset="0.48" stop-color="#C7E4F4"/>
      <stop offset="0.72" stop-color="#88B9D6"/>
      <stop offset="0.73" stop-color="#F1B24B"/>
      <stop offset="1" stop-color="#D78032"/>
    </linearGradient>
    <linearGradient id="chip" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0C5B93"/>
      <stop offset="1" stop-color="#0A2B47"/>
    </linearGradient>
    <filter id="titleShadow" x="-15%" y="-20%" width="140%" height="160%">
      <feDropShadow dx="16" dy="22" stdDeviation="15" flood-color="#000D18" flood-opacity="0.8"/>
    </filter>
    <filter id="logoShadow" x="-20%" y="-20%" width="150%" height="150%">
      <feDropShadow dx="0" dy="16" stdDeviation="16" flood-color="#000A13" flood-opacity="0.48"/>
    </filter>
    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
      <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#B7D2E4" stroke-opacity="0.08" stroke-width="1"/>
    </pattern>
  </defs>
  <rect width="3840" height="1792" fill="#061B2F"/>
  <image xlink:href="${artwork}" x="0" y="0" width="3840" height="1805" preserveAspectRatio="xMidYMid slice"/>
  <path d="M0 0H1660C1740 365 1732 1430 1650 1792H0Z" fill="url(#leftShade)"/>
  <rect x="0" y="0" width="1640" height="1792" fill="url(#grid)"/>
  <path d="M0 66H1290" stroke="#E9A034" stroke-width="9"/>
  <path d="M0 88H960" stroke="#B9D5E7" stroke-opacity="0.34" stroke-width="2"/>
  <g filter="url(#logoShadow)">
    <rect x="170" y="152" width="205" height="205" rx="30" fill="#F7FBFD"/>
    <image xlink:href="${logo}" x="186" y="168" width="173" height="173" preserveAspectRatio="xMidYMid meet"/>
  </g>
  <text x="430" y="245" fill="#E9A034" font-size="30" font-weight="700" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif" letter-spacing="8">AI VISION · PROCESS INTELLIGENCE</text>
  <text x="430" y="303" fill="#D1E4F1" font-size="26" font-weight="500" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif" letter-spacing="5">视觉理解驱动的工艺传承平台</text>
  <g filter="url(#titleShadow)" transform="skewX(-6)">
    <text x="166" y="748" fill="#071525" stroke="#06101C" stroke-width="20" stroke-linejoin="round" font-size="286" font-weight="900" font-family="Songti SC, Songti, STSong, STHeiti, 'PingFang SC', sans-serif" letter-spacing="20">工序智传</text>
    <text x="166" y="731" fill="url(#titleFill)" stroke="#EAF6FC" stroke-opacity="0.55" stroke-width="3" paint-order="stroke" font-size="286" font-weight="900" font-family="Songti SC, Songti, STSong, STHeiti, 'PingFang SC', sans-serif" letter-spacing="20">工序智传</text>
  </g>
  <path d="M183 806H646" stroke="#E9A034" stroke-width="10"/>
  <path d="M183 828H484" stroke="#70B7DE" stroke-opacity="0.65" stroke-width="3"/>
  <text x="178" y="935" fill="#F4F8FB" font-size="52" font-weight="600" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif">让经验看得见，让技能传下去</text>
  <text x="180" y="1010" fill="#B8D0DF" font-size="31" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif">以多模态理解连接教学视频、标准 SOP 与新人操作反馈</text>
  <g font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif">
    <rect x="180" y="1125" width="360" height="116" rx="16" fill="url(#chip)" stroke="#64A7D0" stroke-width="2"/>
    <text x="360" y="1175" fill="#F7FBFE" text-anchor="middle" font-size="31" font-weight="700">示教视频理解</text>
    <text x="360" y="1215" fill="#BBD6E8" text-anchor="middle" font-size="22">沉淀关键步骤与证据</text>
    <path d="M564 1183H677" stroke="#E9A034" stroke-width="5"/>
    <circle cx="624" cy="1183" r="12" fill="#E9A034"/>
    <rect x="700" y="1125" width="360" height="116" rx="16" fill="url(#chip)" stroke="#64A7D0" stroke-width="2"/>
    <text x="880" y="1175" fill="#F7FBFE" text-anchor="middle" font-size="31" font-weight="700">SOP 自动生成</text>
    <text x="880" y="1215" fill="#BBD6E8" text-anchor="middle" font-size="22">形成可复用工艺知识</text>
    <path d="M1084 1183H1197" stroke="#E9A034" stroke-width="5"/>
    <circle cx="1144" cy="1183" r="12" fill="#E9A034"/>
    <rect x="1220" y="1125" width="360" height="116" rx="16" fill="url(#chip)" stroke="#64A7D0" stroke-width="2"/>
    <text x="1400" y="1175" fill="#F7FBFE" text-anchor="middle" font-size="31" font-weight="700">操作智能考评</text>
    <text x="1400" y="1215" fill="#BBD6E8" text-anchor="middle" font-size="22">输出证据化改进反馈</text>
  </g>
  <g transform="translate(180,1433)">
    <circle cx="18" cy="18" r="18" fill="#E9A034"/>
    <path d="M11 18h14M18 11v14" stroke="#08243B" stroke-width="3" stroke-linecap="round"/>
    <text x="54" y="27" fill="#D8E7F0" font-size="27" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif" letter-spacing="3">面向制造业技能传承与标准化培训</text>
  </g>
  <g transform="translate(3110,1544)">
    <rect width="540" height="92" rx="14" fill="#07263E" fill-opacity="0.9" stroke="#6FA4C2" stroke-width="2"/>
    <circle cx="39" cy="46" r="13" fill="#E9A034"/>
    <text x="76" y="57" fill="#F3F8FB" font-size="28" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif">工艺步骤 · 视觉理解 · 反馈闭环</text>
  </g>
</svg>`;

await mkdir(dirname(resolve(outputPath)), { recursive: true });
await writeFile(resolve(outputPath), svg, "utf8");
console.log(`Composed 4K poster SVG: ${resolve(outputPath)}`);
