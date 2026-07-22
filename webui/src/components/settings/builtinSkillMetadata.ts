import { normalizeLocale } from "@/i18n/config";
import type { SkillSummary } from "@/lib/types";

interface SkillPresentation {
  name: string;
  description: string;
}

const SIMPLIFIED_CHINESE_BUILTINS: Record<string, SkillPresentation> = {
  cron: {
    name: "定时任务",
    description: "创建提醒和周期性任务。",
  },
  memory: {
    name: "记忆",
    description: "通过 Dream 管理知识文件的双层记忆系统。",
  },
  "update-setup": {
    name: "更新配置",
    description: "初始化并配置 nanobot 的升级能力。",
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
