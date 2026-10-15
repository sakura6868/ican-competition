import { readFile, writeFile, mkdir } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";

const [artworkPath, logoPath, outputPath] = process.argv.slice(2);

if (!artworkPath || !logoPath || !outputPath) {
  throw new Error("Usage: node compose_product_poster.mjs <artwork.png> <logo.svg> <output.svg>");
}

const toDataUri = async (path, mimeType) => {
  const data = await readFile(resolve(path));
  return `data:${mimeType};base64,${data.toString("base64")}`;
};

const artwork = await toDataUri(artworkPath, "image/png");
const logo = await toDataUri(logoPath, "image/svg+xml");
const title = "工序智传";

const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="1200" height="560" viewBox="0 0 1200 560">
  <defs>
    <linearGradient id="shade" x1="0" y1="0" x2="0.78" y2="0">
      <stop offset="0" stop-color="#061C30" stop-opacity="0.12"/>
      <stop offset="0.45" stop-color="#061C30" stop-opacity="0.05"/>
      <stop offset="0.74" stop-color="#061C30" stop-opacity="0.58"/>
      <stop offset="1" stop-color="#061C30" stop-opacity="0.10"/>
    </linearGradient>
    <linearGradient id="panel" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0B385B" stop-opacity="0.96"/>
      <stop offset="1" stop-color="#061A2D" stop-opacity="0.86"/>
    </linearGradient>
    <filter id="softShadow" x="-10%" y="-10%" width="120%" height="130%">
      <feDropShadow dx="0" dy="8" stdDeviation="10" flood-color="#00101E" flood-opacity="0.28"/>
    </filter>
  </defs>
  <rect width="1200" height="560" fill="#071C31"/>
  <image xlink:href="${artwork}" x="0" y="0" width="1200" height="565" preserveAspectRatio="xMidYMid slice"/>
  <rect width="1200" height="560" fill="url(#shade)"/>
  <path d="M0 0H472C505 115 501 417 468 560H0Z" fill="#061B30" fill-opacity="0.72"/>
  <path d="M0 0H465" stroke="#E9A034" stroke-width="5"/>
  <g filter="url(#softShadow)">
    <rect x="52" y="42" width="96" height="96" rx="16" fill="#F6F8FA" fill-opacity="0.98"/>
    <image xlink:href="${logo}" x="60" y="50" width="80" height="80" preserveAspectRatio="xMidYMid meet"/>
  </g>
  <text x="169" y="82" fill="#F8FBFD" font-size="16" font-weight="600" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif" letter-spacing="2">AI 视觉工艺传承平台</text>
  <text x="52" y="212" fill="#FFFFFF" font-size="58" font-weight="700" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif" letter-spacing="1">${title}</text>
  <rect x="54" y="237" width="82" height="5" rx="2.5" fill="#E9A034"/>
  <text x="52" y="288" fill="#D7E5F0" font-size="21" font-weight="400" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif">让经验看得见，让技能传下去</text>
  <text x="52" y="326" fill="#AFC5D8" font-size="15" font-weight="400" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif">从教学视频到标准 SOP，再到新人操作的证据化反馈</text>
  <g font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif" font-size="14" font-weight="600">
    <rect x="52" y="372" width="124" height="38" rx="19" fill="#0E6AB7" fill-opacity="0.95"/>
    <text x="114" y="397" fill="#FFFFFF" text-anchor="middle">视频生成 SOP</text>
    <rect x="188" y="372" width="128" height="38" rx="19" fill="#294E6E" fill-opacity="0.95" stroke="#6F94B3" stroke-width="1"/>
    <text x="252" y="397" fill="#F3F7FA" text-anchor="middle">智能过程考评</text>
    <rect x="52" y="424" width="164" height="38" rx="19" fill="#294E6E" fill-opacity="0.95" stroke="#6F94B3" stroke-width="1"/>
    <text x="134" y="449" fill="#F3F7FA" text-anchor="middle">可追溯证据报告</text>
  </g>
  <g transform="translate(892,476)">
    <rect width="256" height="42" rx="7" fill="url(#panel)" stroke="#6D92B2" stroke-opacity="0.75"/>
    <circle cx="22" cy="21" r="6" fill="#E9A034"/>
    <text x="38" y="26" fill="#F2F7FB" font-size="14" font-family="Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif">工艺步骤 · 视觉理解 · 反馈闭环</text>
  </g>
</svg>`;

await mkdir(dirname(resolve(outputPath)), { recursive: true });
await writeFile(resolve(outputPath), svg, "utf8");
console.log(`Composed poster SVG: ${basename(outputPath)}`);
