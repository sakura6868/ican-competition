import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const sourceDir = path.dirname(fileURLToPath(import.meta.url));
const workspaceDir = path.resolve(sourceDir, "../..");
const skillDir = process.env.CODEX_PRESENTATIONS_SKILL_DIR;
const runtimePython = process.env.RUNTIME_PYTHON;
if (!skillDir || !runtimePython) {
  throw new Error("Set CODEX_PRESENTATIONS_SKILL_DIR and RUNTIME_PYTHON before rebuilding the deck.");
}
const buildDir = path.join(workspaceDir, "docs/presentation-src/.build");
const previewDir = path.join(buildDir, "previews");
const finalPath = path.join(workspaceDir, "docs/presentations/工序智传_iCAN智慧教育答辩_2026-10-15_v2.pptx");

const W = 1280;
const H = 720;
const FONT = "Microsoft YaHei";
const C = {
  paper: "F7F5F0",
  ink: "17223B",
  muted: "667085",
  pale: "EDE9E1",
  red: "E85A4F",
  cyan: "22A6A1",
  navy: "23395D",
  yellow: "F2C14E",
  green: "3A9D72",
  white: "FFFFFF",
  rose: "FBE9E7",
  mint: "E5F4F2",
};

const deck = Presentation.create({ slideSize: { width: W, height: H } });

function shape(slide, geometry, left, top, width, height, fill, opts = {}) {
  return slide.shapes.add({
    geometry,
    position: { left, top, width, height },
    fill: fill ?? "none",
    line: { style: "solid", fill: opts.line ?? "none", width: opts.lineWidth ?? 0 },
    ...(opts.radius ? { borderRadius: opts.radius } : {}),
    ...(opts.shadow ? { shadow: opts.shadow } : {}),
  });
}

function text(slide, value, left, top, width, height, size = 28, color = C.ink, opts = {}) {
  const box = shape(slide, "textbox", left, top, width, height, "none");
  box.text = value;
  box.text.style = {
    fontFamily: FONT,
    fontSize: size,
    color,
    bold: opts.bold ?? false,
    ...(opts.align ? { alignment: opts.align } : {}),
    ...(opts.valign ? { verticalAlignment: opts.valign } : {}),
  };
  return box;
}

function line(slide, left, top, width, height = 2, color = C.pale) {
  return shape(slide, "rect", left, top, width, height, color);
}

function dot(slide, x, y, d, fill) {
  return shape(slide, "ellipse", x, y, d, d, fill);
}

function pill(slide, label, left, top, width, fill = C.navy, color = C.white) {
  shape(slide, "roundRect", left, top, width, 34, fill, { radius: "rounded-full" });
  text(slide, label, left, top + 1, width, 30, 14, color, { bold: true, align: "center", valign: "middle" });
}

function base(slide, section, index, dark = false) {
  slide.background.fill = dark ? C.ink : C.paper;
  if (!dark) line(slide, 60, 54, 1160, 2, C.pale);
  text(slide, section, 60, 24, 520, 24, 12, dark ? "A9B4C8" : C.muted, { bold: true });
  text(slide, String(index).padStart(2, "0"), 1165, 24, 55, 24, 12, dark ? "A9B4C8" : C.muted, { bold: true, align: "right" });
}

function title(slide, heading, sub = "") {
  text(slide, heading, 60, 78, 1120, 74, 40, C.ink, { bold: true });
  if (sub) text(slide, sub, 62, 150, 1080, 46, 17, C.muted);
}

function label(slide, value, left, top, color = C.red) {
  text(slide, value, left, top, 220, 24, 13, color, { bold: true });
}

async function imageBytes(rel) {
  const bytes = await fs.readFile(path.join(workspaceDir, rel));
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

async function addImage(slide, rel, left, top, width, height, fit = "cover") {
  shape(slide, "roundRect", left - 8, top - 8, width + 16, height + 16, C.white, { line: C.pale, lineWidth: 1, radius: "rounded-xl", shadow: "shadow-sm" });
  slide.images.add({
    blob: await imageBytes(rel),
    contentType: rel.endsWith(".jpg") ? "image/jpeg" : "image/png",
    alt: path.basename(rel),
    fit,
    position: { left, top, width, height },
    geometry: "roundRect",
    borderRadius: "rounded-lg",
  });
}

function metric(slide, value, caption, left, top, accent = C.red, width = 210) {
  text(slide, value, left, top, width, 58, 42, accent, { bold: true });
  text(slide, caption, left, top + 58, width, 52, 15, C.muted);
}

function stepBlock(slide, no, heading, body, left, top, width, accent) {
  dot(slide, left, top + 4, 40, accent);
  text(slide, String(no), left, top + 5, 40, 35, 16, C.white, { bold: true, align: "center", valign: "middle" });
  text(slide, heading, left + 58, top, width - 58, 34, 20, C.ink, { bold: true });
  text(slide, body, left + 58, top + 38, width - 58, 70, 15, C.muted);
}

// 01 Cover
{
  const s = deck.slides.add();
  s.background.fill = C.ink;
  shape(s, "rect", 0, 0, 20, H, C.red);
  dot(s, 930, 82, 250, C.navy);
  dot(s, 1010, 420, 120, C.cyan);
  line(s, 86, 112, 170, 3, C.red);
  text(s, "iCAN 大学生创新创业大赛 · 智慧教育", 86, 72, 660, 28, 15, "BBC6D8", { bold: true });
  text(s, "工序智传", 84, 176, 650, 100, 64, C.white, { bold: true });
  text(s, "把一段教学视频，变成一条\n会判断、会纠错、会推进的学习路径", 88, 292, 790, 150, 35, C.white, { bold: true });
  pill(s, "教学视频 → SOP", 88, 492, 170, C.red);
  pill(s, "实时步骤核验", 270, 492, 170, C.cyan);
  pill(s, "教师学情闭环", 452, 492, 170, C.navy);
  text(s, "桌面网页端原型 · 可部署演示版 · 2026", 88, 630, 640, 30, 16, "A9B4C8");
  text(s, "01", 1160, 650, 56, 24, 12, "A9B4C8", { bold: true, align: "right" });
}

// 02 Problem
{
  const s = deck.slides.add(); base(s, "01 / 教育问题", 2); title(s, "视频能示范，却很难陪学生练到会", "技能学习缺少的是操作过程中的即时判断，而不是更多播放按钮。");
  const xs = [72, 425, 778];
  const nums = ["01", "02", "03"];
  const heads = ["教师制作成本高", "学生卡住没人知道", "结果评价来得太晚"];
  const bodies = ["逐帧拆步骤、写条件、配素材，通常要重复劳动。", "同一段视频反复看，错误动作仍可能被带到下一步。", "只看成品或期末成绩，无法定位具体薄弱工序。"];
  const accents = [C.red, C.cyan, C.yellow];
  for (let i = 0; i < 3; i++) {
    text(s, nums[i], xs[i], 230, 290, 90, 68, accents[i], { bold: true });
    line(s, xs[i], 320, 270, 3, accents[i]);
    text(s, heads[i], xs[i], 352, 290, 44, 23, C.ink, { bold: true });
    text(s, bodies[i], xs[i], 408, 292, 110, 17, C.muted);
  }
  shape(s, "roundRect", 72, 560, 1060, 78, C.ink, { radius: "rounded-xl" });
  text(s, "核心缺口", 100, 581, 110, 30, 14, C.red, { bold: true });
  text(s, "学习过程没有被结构化，也没有在每一步形成“示范—尝试—反馈—掌握”的闭环。", 220, 576, 860, 40, 22, C.white, { bold: true });
}

// 03 Solution loop
{
  const s = deck.slides.add(); base(s, "02 / 解决方案", 3); title(s, "一套系统，连接教师发布与学生练习", "教师沉淀标准，学生逐步练习，数据再回到教学设计。");
  label(s, "教师端", 82, 220, C.red);
  stepBlock(s, 1, "导入完整示范视频", "系统生成候选分段、步骤名称和完成条件。", 82, 258, 420, C.red);
  stepBlock(s, 2, "校对并发布课程", "教师保留最终解释权，设置关键步骤与人工条件。", 82, 400, 420, C.red);
  label(s, "学生端", 724, 220, C.cyan);
  stepBlock(s, 3, "看一步、练一步", "摄像头采集操作结果，一次提交三帧核验。", 724, 258, 430, C.cyan);
  stepBlock(s, 4, "正确前进，错误修正", "完成后自动跳步；异常给出当前条件对应提示。", 724, 400, 430, C.cyan);
  line(s, 554, 250, 2, 310, C.pale);
  dot(s, 574, 330, 132, C.ink);
  text(s, "SOP\n闭环", 574, 352, 132, 72, 24, C.white, { bold: true, align: "center", valign: "middle" });
  line(s, 502, 395, 72, 3, C.red); line(s, 706, 395, 18, 3, C.cyan);
  text(s, "学情反哺课程", 505, 548, 230, 30, 16, C.ink, { bold: true, align: "center" });
  text(s, "薄弱步骤 · 纠错次数 · 完成率 · 调用时延", 430, 588, 380, 28, 14, C.muted, { align: "center" });
}

// 04 Teacher workflow
{
  const s = deck.slides.add(); base(s, "03 / 教师备课", 4); title(s, "从完整视频到可发布 SOP，教师只做关键校对", "自动化负责提取候选结构，专业教师负责确认教学含义。");
  const data = [
    ["01", "视频预处理", "抽帧 / 音轨 / 时间轴", C.red],
    ["02", "候选分段", "识别动作与语义变化", C.yellow],
    ["03", "条件生成", "写出可观察的完成条件", C.cyan],
    ["04", "教师审核", "改步骤、关键帧和核验策略", C.navy],
    ["05", "版本发布", "课程元数据 + SOP 留痕", C.green],
  ];
  const y = 250;
  data.forEach((d, i) => {
    const x = 68 + i * 236;
    dot(s, x, y, 54, d[3]);
    text(s, d[0], x, y + 10, 54, 32, 15, C.white, { bold: true, align: "center" });
    if (i < data.length - 1) line(s, x + 54, y + 25, 182, 4, C.pale);
    text(s, d[1], x, y + 82, 190, 34, 19, C.ink, { bold: true });
    text(s, d[2], x, y + 125, 188, 60, 14, C.muted);
  });
  shape(s, "roundRect", 68, 520, 1110, 98, C.mint, { radius: "rounded-xl" });
  text(s, "人机分工原则", 94, 540, 170, 30, 15, C.cyan, { bold: true });
  text(s, "AI 给出候选，教师确认标准；系统不把自动分段当成已发布教学结论。", 264, 535, 850, 42, 22, C.ink, { bold: true });
  text(s, "支持课程标题、目标、难度、受众、预计时长与版本记录。", 264, 583, 830, 25, 14, C.muted);
}

// 05 Student state machine
{
  const s = deck.slides.add(); base(s, "04 / 学生陪练", 5); title(s, "一次三帧核验，让普通步骤在 3–6 秒内给反馈", "状态机确保“看不清、做错、做对”走向不同分支。");
  const nodes = [
    ["采集", "约 1 秒\n连续三帧", 86, C.navy],
    ["核验", "一次多模态\nAPI 调用", 326, C.red],
    ["通过", "记录完成\n自动下一步", 574, C.green],
    ["修正", "指出未满足\n的具体条件", 822, C.red],
    ["补拍", "4 / 8 / 12 / 15 秒\n递增等待", 1060, C.yellow],
  ];
  nodes.forEach((n, i) => {
    dot(s, n[2], 276, 112, n[3]);
    text(s, n[0], n[2], 300, 112, 32, 19, C.white, { bold: true, align: "center" });
    text(s, n[1], n[2] - 30, 414, 172, 56, 14, C.muted, { bold: true, align: "center" });
    if (i < nodes.length - 1) line(s, n[2] + 112, 330, 128, 4, C.pale);
  });
  pill(s, "satisfied", 564, 226, 130, C.green);
  pill(s, "violated", 812, 226, 130, C.red);
  pill(s, "unknown", 1050, 226, 130, C.yellow, C.ink);
  shape(s, "roundRect", 82, 540, 1100, 88, C.ink, { radius: "rounded-xl" });
  text(s, "关键步骤", 108, 562, 122, 30, 15, C.yellow, { bold: true });
  text(s, "两轮新画面确认，目标 8–12 秒；拓扑、扭矩等难以仅凭视觉判断的条件交给教师人工确认。", 238, 555, 900, 42, 20, C.white, { bold: true });
}

// 06 Generality
{
  const s = deck.slides.add(); base(s, "05 / 通用能力", 6); title(s, "通用性来自“完成条件”，而不是绑定某一种工种", "每个步骤由可观察条件、核验方式与确认策略共同定义。");
  const cols = [
    ["视觉条件", "位置、数量、朝向、完整度、装配关系", "模型核验", C.cyan],
    ["关键条件", "安全顺序、锁止状态、需要稳定重复观察", "双确认", C.red],
    ["不可视条件", "扭矩、内部结构、触感或专业责任判断", "人工确认", C.yellow],
  ];
  cols.forEach((d, i) => {
    const x = 72 + i * 382;
    line(s, x, 234, 300, 5, d[3]);
    text(s, d[0], x, 262, 320, 38, 25, C.ink, { bold: true });
    text(s, d[1], x, 320, 318, 90, 17, C.muted);
    pill(s, d[2], x, 432, 132, d[3], d[3] === C.yellow ? C.ink : C.white);
  });
  line(s, 72, 520, 1060, 2, C.pale);
  text(s, "可迁移案例", 72, 552, 150, 28, 14, C.muted, { bold: true });
  ["手工制作", "实验操作", "设备点检", "急救训练", "职业技能"].forEach((v, i) => {
    text(s, v, 240 + i * 182, 548, 150, 34, 20, C.ink, { bold: true, align: "center" });
  });
  text(s, "边界：当前原型验证的是技术闭环；跨场景准确率需要独立学员与现场数据继续评估。", 72, 625, 1060, 26, 14, C.red, { bold: true });
}

// 07 Product views
{
  const s = deck.slides.add(); base(s, "06 / 产品原型", 7); title(s, "教师在同一网页完成拆解、校对与发布", "桌面端已实现课程元数据、步骤时间轴、完成条件和版本管理。");
  await addImage(s, "output/playwright/education-studio.png", 76, 220, 810, 402, "contain");
  text(s, "发布前检查", 930, 236, 250, 34, 21, C.ink, { bold: true });
  ["步骤名称与顺序", "每步完成条件", "关键帧与示范片段", "普通 / 关键 / 人工核验", "课程目标与适用人群"].forEach((v, i) => {
    dot(s, 932, 302 + i * 56, 20, i === 4 ? C.green : C.red);
    text(s, v, 968, 296 + i * 56, 230, 32, 16, C.muted, { bold: true });
  });
  text(s, "教师始终拥有最终发布权", 930, 590, 260, 30, 16, C.red, { bold: true });
}

// 08 Learning evidence
{
  const s = deck.slides.add(); base(s, "07 / 学情闭环", 8); title(s, "每次练习都留下可追踪的步骤证据", "学生看到个人复盘；教师看到班级薄弱步骤与服务时延。");
  await addImage(s, "output/playwright/student-report.png", 66, 220, 548, 342, "contain");
  await addImage(s, "output/playwright/teacher-analytics.png", 666, 220, 548, 342, "contain");
  pill(s, "学生结课报告", 84, 580, 160, C.cyan);
  text(s, "完成率 · 纠错次数 · 步骤耗时 · 建议复习", 260, 582, 350, 30, 14, C.muted, { bold: true });
  pill(s, "教师学情看板", 684, 580, 160, C.red);
  text(s, "班级概览 · 薄弱步骤 · 个体报告 · API 时延", 862, 582, 350, 30, 14, C.muted, { bold: true });
  text(s, "练习指数仅由系统记录生成，用于反馈，不等同于标准化考试成绩。", 66, 644, 900, 24, 13, C.red, { bold: true });
}

// 09 Validation
{
  const s = deck.slides.add(); base(s, "08 / 技术验证", 9); title(s, "三个不同技能案例，验证同一条状态机", "数据来自公开教学视频的同源帧回放；它证明链路可运行，不代表真实学习准确率。");
  const headers = ["案例", "核验规模", "结果", "API 平均时延", "验证重点"];
  const widths = [160, 180, 250, 180, 350];
  shape(s, "rect", 58, 210, 1138, 54, C.ink);
  let x = 72;
  headers.forEach((h, i) => { text(s, h, x, 224, widths[i], 34, 14, C.white, { bold: true }); x += widths[i]; });
  const rows = [
    ["系鞋带", "4 步 / 5 次", "5/5 符合预期", "3.24 s", "一次三帧通过即跳步；遮挡补拍"],
    ["折纸杯", "5 步 / 15 次", "15/15 符合预期", "4.34 s", "多步骤状态机；素材仅本地 QA"],
    ["单套结", "前 2 步 / 6 次", "关键步骤双确认", "5.06 s", "视觉 + 人工条件；未跑完整课程"],
  ];
  rows.forEach((row, r) => {
    const top = 286 + r * 92;
    if (r % 2 === 0) shape(s, "rect", 58, top - 10, 1138, 78, C.white);
    let xx = 72;
    row.forEach((v, i) => {
      text(s, v, xx, top, widths[i] - 14, 48, i === 3 ? 20 : 15, i === 3 ? C.red : C.ink, { bold: i === 0 || i === 2 || i === 3 });
      xx += widths[i];
    });
  });
  shape(s, "roundRect", 72, 570, 1088, 72, C.rose, { radius: "rounded-xl" });
  text(s, "证据边界", 96, 590, 110, 30, 14, C.red, { bold: true });
  text(s, "尚未开展真人学习效果实验；下一阶段用独立摄像头、不同学员和现场光照验证准确率与教学增益。", 212, 584, 900, 36, 18, C.ink, { bold: true });
}

// 10 Architecture
{
  const s = deck.slides.add(); base(s, "09 / 系统架构", 10); title(s, "本地状态机保证流程，云端模型提供视觉判断", "API 只负责理解多帧画面；步骤推进、重试和审计由系统自己控制。");
  const blocks = [
    ["浏览器", "Vue 3\n教师端 / 学生端", 70, C.cyan],
    ["应用服务", "FastAPI\n权限 / 课程 / 报告", 330, C.navy],
    ["流程内核", "SOP 状态机\n校验 / 重试 / 跳步", 590, C.red],
    ["视觉能力", "兼容多模态 API\n一次三帧判断", 850, C.yellow],
  ];
  blocks.forEach((b, i) => {
    shape(s, "roundRect", b[2], 246, 210, 160, C.white, { line: b[3], lineWidth: 3, radius: "rounded-xl" });
    pill(s, b[0], b[2] + 22, 266, 116, b[3], b[3] === C.yellow ? C.ink : C.white);
    text(s, b[1], b[2] + 22, 322, 170, 64, 17, C.ink, { bold: true, align: "center" });
    if (i < blocks.length - 1) { line(s, b[2] + 210, 322, 50, 4, C.pale); dot(s, b[2] + 245, 316, 16, C.pale); }
  });
  shape(s, "roundRect", 1110, 246, 102, 160, C.ink, { radius: "rounded-xl" });
  text(s, "审计\n收据", 1110, 298, 102, 64, 18, C.white, { bold: true, align: "center" });
  line(s, 1060, 322, 50, 4, C.pale);
  const bottoms = [
    ["断网或模型异常", "停在原步骤并提示，不伪造通过", C.red],
    ["关键/安全动作", "两次新画面确认或教师人工确认", C.yellow],
    ["可复现实验", "输入、结果、时延和版本统一留痕", C.green],
  ];
  bottoms.forEach((b, i) => {
    const x0 = 72 + i * 376;
    line(s, x0, 488, 320, 4, b[2]);
    text(s, b[0], x0, 510, 310, 30, 18, C.ink, { bold: true });
    text(s, b[1], x0, 550, 310, 52, 14, C.muted);
  });
  text(s, "开源技术栈：FastAPI · SQLAlchemy · Vue 3 · Playwright · ONNX Runtime · MediaPipe", 72, 644, 1060, 24, 13, C.muted);
}

// 11 Value and roadmap
{
  const s = deck.slides.add(); base(s, "10 / 参赛价值", 11); title(s, "P0 已形成可演示闭环，P1 已补齐赛场可靠性", "当前成果能完整讲清教育价值，也保留了下一步可验证的研究问题。");
  label(s, "P0 · 核心闭环", 72, 222, C.red);
  ["视频生成候选 SOP", "教师校对并发布", "学生逐步核验与跳步", "完成报告与薄弱步骤"].forEach((v, i) => {
    dot(s, 76, 270 + i * 56, 22, C.green);
    text(s, "✓", 76, 270 + i * 56, 22, 22, 13, C.white, { bold: true, align: "center" });
    text(s, v, 112, 264 + i * 56, 340, 32, 17, C.ink, { bold: true });
  });
  label(s, "P1 · 比赛可靠性", 490, 222, C.cyan);
  ["一键准备 / 启停脚本", "教师、学生、观察员角色", "模型收据与时延审计", "API 故障提示与工程回放标识"].forEach((v, i) => {
    dot(s, 494, 270 + i * 56, 22, C.green);
    text(s, "✓", 494, 270 + i * 56, 22, 22, 13, C.white, { bold: true, align: "center" });
    text(s, v, 530, 264 + i * 56, 360, 32, 17, C.ink, { bold: true });
  });
  shape(s, "roundRect", 914, 212, 286, 278, C.ink, { radius: "rounded-xl" });
  text(s, "下一阶段验证", 940, 240, 230, 36, 21, C.white, { bold: true });
  text(s, "20+ 学员 × 3 类技能", 940, 308, 230, 34, 18, C.yellow, { bold: true });
  text(s, "比较“普通视频学习”与“逐步陪练”的完成时间、求助次数与步骤错误。", 940, 360, 224, 90, 15, "D9E0EC");
  line(s, 72, 536, 1128, 2, C.pale);
  metric(s, "367", "后端自动化测试通过", 72, 560, C.red, 250);
  metric(s, "24", "前端单元测试通过", 370, 560, C.cyan, 250);
  metric(s, "2/2", "桌面浏览器关键路径通过", 668, 560, C.green, 280);
  metric(s, "1 键", "赛场环境准备与启动", 1000, 560, C.yellow, 190);
}

// 12 Closing
{
  const s = deck.slides.add();
  s.background.fill = C.ink;
  shape(s, "rect", 0, 0, 20, H, C.red);
  text(s, "工序智传", 84, 70, 420, 50, 18, C.red, { bold: true });
  text(s, "让每一段示范视频，\n都能陪学生练到下一步。", 82, 154, 820, 164, 49, C.white, { bold: true });
  line(s, 84, 356, 920, 2, C.navy);
  const demo = [
    ["01", "教师发布", "导入视频 → 校对 SOP → 发布课程"],
    ["02", "学生练习", "三帧核验 → 修正或自动跳步"],
    ["03", "教师复盘", "查看薄弱步骤与个人报告"],
  ];
  demo.forEach((d, i) => {
    const x = 84 + i * 355;
    text(s, d[0], x, 402, 60, 30, 14, C.cyan, { bold: true });
    text(s, d[1], x, 440, 250, 32, 21, C.white, { bold: true });
    text(s, d[2], x, 486, 290, 54, 14, "B9C3D4");
  });
  pill(s, "现场演示约 5 分钟", 84, 586, 190, C.red);
  text(s, "本材料由 AI 辅助整理；产品数据来自系统日志与自动化测试，未使用真人学习效果数据。", 300, 589, 810, 32, 13, "A9B4C8");
  text(s, "12", 1160, 650, 56, 24, 12, "A9B4C8", { bold: true, align: "right" });
}

async function writeBlob(file, blob) {
  await fs.writeFile(file, new Uint8Array(await blob.arrayBuffer()));
}

async function main() {
  await fs.mkdir(previewDir, { recursive: true });
  await fs.mkdir(path.dirname(finalPath), { recursive: true });
  for (const [i, slide] of deck.slides.items.entries()) {
    await writeBlob(path.join(previewDir, `slide-${String(i + 1).padStart(2, "0")}.png`), await deck.export({ slide, format: "png", scale: 1 }));
  }
  await writeBlob(path.join(buildDir, "montage.webp"), await deck.export({ format: "webp", montage: true }));

  const { finalizePresentation } = await import(pathToFileURL(path.join(skillDir, "container_tools/artifact_tool_utils.mjs")).href);
  const candidatePath = path.join(buildDir, "candidate-v2.pptx");
  await (await PresentationFile.exportPptx(deck)).save(candidatePath);
  await finalizePresentation({
    workspaceDir,
    candidatePath,
    finalPath,
    pythonExecutable: runtimePython,
    integrityValidatorPath: path.join(skillDir, "container_tools/inspect_presentation_package_integrity.py"),
    layoutValidatorPath: path.join(skillDir, "container_tools/inspect_presentation_layout_geometry.py"),
    layoutArgs: ["--expected-slide-size-emu", "12192000,6858000", "--validate-bullet-geometry", "--validate-heading-fit"],
    explicitTotalSlideCount: 12,
    requiredNativeTableOwnerSlides: [],
    requiredNativeChartOwnerSlides: [],
    fontPolicy: { basis: "design", families: ["Aptos"] },
    verifyArtifactToolImport: true,
    receiptPath: path.join(buildDir, "deck-v2.validation.json"),
  });
  console.log(finalPath);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
