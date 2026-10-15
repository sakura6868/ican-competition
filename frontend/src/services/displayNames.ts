/**
 * Human-facing names only. API keys, paths and hashes intentionally remain
 * unchanged so archived evaluations stay reproducible.
 */

const processNames: Record<string, string> = {
  origami_cup_tutorial: "折纸杯入门 · 分步实训测试",
  shoe_criss_cross_lacing: "帆布鞋平行横条穿法 · 多帧核验测试",
  nps_bowline_tutorial: "单套结教学 · NPS 公开视频测试",
  impact_grinder_safe_subassembly: "角磨机安全拆装实训",
  impact_gearbox_disassembly_demo: "角磨机齿轮箱拆装实训",
  assembly101_robot_assemble_demo: "履带机器人装配实训",
  industreal_v2_assy_validation: "模块小车装配实训",
  industreal_v2_main_validation: "模块小车拆装实训",
};

export interface DatasetCatalogEntry {
  id: string;
  name: string;
  sourceName: string;
  description: string;
  recommendedUse: string;
  localPath: string;
  mediaAvailable: boolean;
}

export const datasetCatalog: DatasetCatalogEntry[] = [
  {
    id: "industreal-v2",
    name: "工业装配实训数据集",
    sourceName: "IndustReal v2",
    description: "包含模块化小车的装配、拆装、多步骤操作与部分错误样本。",
    recommendedUse: "比赛主演示：选择“模块小车装配实训/拆装实训”SOP 后上传同类视频。",
    localPath: "data/raw/industreal-v2/development/media",
    mediaAvailable: true,
  },
  {
    id: "impact-v11",
    name: "工业维修操作数据集",
    sourceName: "IMPACT v1.1",
    description: "多视角记录的真实工具拆装、维修和装配操作。",
    recommendedUse: "用于多视角、工具和手部关系的探索性演示；先导入标准视频再生成对应 SOP。",
    localPath: "data/raw/impact-v1.1/IMPACT-v1.1/sample/videos",
    mediaAvailable: true,
  },
  {
    id: "assembly101",
    name: "装配教学动作数据集",
    sourceName: "Assembly101",
    description: "面向复杂装配动作理解的公开视频与动作标注样例。",
    recommendedUse: "用于动作词、步骤顺序和新手训练场景的扩展示例。",
    localPath: "data/raw/assembly101-sample",
    mediaAvailable: true,
  },
  {
    id: "captaincook4d",
    name: "第一视角操作流程数据集",
    sourceName: "CaptainCook4D",
    description: "第一视角连续操作的标注、任务图与数据划分，不含本地原始视频。",
    recommendedUse: "用于研究流程切分与任务图；当前项目仅保存其标注资料，不可直接上传考评。",
    localPath: "data/raw/captaincook4d-annotations",
    mediaAvailable: false,
  },
];

export function displayProcessName(processKey: string): string {
  return processNames[processKey] ?? processKey;
}

export function displayAssetName(filename: string): string {
  const filenameNames: Record<string, string> = {
    "origami-cup.mp4": "折纸杯完整教学视频 · Christine’s Crafts",
    "shoe-lacing.webm": "鞋带穿法完整教学视频 · Lrcolette",
    "nps-bowline-720p.mp4": "单套结完整教学视频 · NPS",
    "01_assy_0_1.mp4": "模块小车标准装配视频 01",
    "15_main_3_1.mp4": "模块小车拆装异常样本 15",
    "C10095_rgb_low.mp4": "装配教学动作样例 C10095",
  };
  if (filenameNames[filename]) return filenameNames[filename];
  const industreal = /^(\d+)_(assy|main)_(\d+)_(\d+)\.(mp4|mov|webm)$/i.exec(filename);
  if (industreal) {
    const [, moduleId, task, variant, take] = industreal;
    return `模块小车${task === "assy" ? "装配" : "拆装"}视频 ${moduleId}-${variant}-${take}`;
  }
  if (/^(AL|ER)\d+[A-Z]+\d+_(Disassembly|Reassembly)/i.test(filename)) {
    return `工业维修${/Reassembly/i.test(filename) ? "装配" : "拆装"}多视角视频`;
  }
  return filename;
}
