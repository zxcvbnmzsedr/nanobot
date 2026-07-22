import { normalizeLocale } from "@/i18n/config";
import type { SkillSummary } from "@/lib/types";

interface SkillPresentation {
  name: string;
  description: string;
}

const SIMPLIFIED_CHINESE_BUILTINS: Record<string, SkillPresentation> = {
  clawhub: {
    name: "ClawHub 技能库",
    description: "从公共 ClawHub 技能仓库搜索并安装 Agent 技能。",
  },
  cron: {
    name: "定时任务",
    description: "创建提醒和周期性任务。",
  },
  github: {
    name: "GitHub",
    description: "使用 gh 命令行与 GitHub 交互，可处理 Issue、PR、CI 运行和高级查询。",
  },
  "image-generation": {
    name: "图像生成",
    description: "生成图片，并对已保存的图像进行持续编辑。",
  },
  memory: {
    name: "记忆",
    description: "通过 Dream 管理知识文件的双层记忆系统。",
  },
  my: {
    name: "运行状态",
    description: "查看并按需调整 Agent 的模型、上下文、Token 用量、工具配置和子 Agent 状态。",
  },
  "skill-creator": {
    name: "技能创建器",
    description: "创建或更新 Agent Skill，用于设计、组织和打包技能内容。",
  },
  summarize: {
    name: "内容摘要",
    description: "从网页、播客和本地文件中提取或总结文本与转录内容。",
  },
  tmux: {
    name: "终端会话",
    description: "通过发送按键和读取窗格内容，远程控制 tmux 交互式命令行会话。",
  },
  "update-setup": {
    name: "更新配置",
    description: "初始化并配置 nanobot 的升级能力。",
  },
  weather: {
    name: "天气",
    description: "查询当前天气和天气预报，无需 API Key。",
  },
};

export function builtinSkillPresentation(
  skill: Pick<SkillSummary, "name" | "description" | "source">,
  locale: string | null | undefined,
): SkillPresentation {
  if (skill.source !== "builtin" || normalizeLocale(locale) !== "zh-CN") {
    return { name: skill.name, description: skill.description };
  }
  return SIMPLIFIED_CHINESE_BUILTINS[skill.name]
    ?? { name: skill.name, description: skill.description };
}
