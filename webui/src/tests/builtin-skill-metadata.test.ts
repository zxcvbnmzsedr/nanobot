import { describe, expect, it } from "vitest";

import { builtinSkillPresentation } from "@/components/settings/builtinSkillMetadata";

const cron = {
  name: "cron",
  description: "Schedule reminders and recurring tasks.",
  source: "builtin",
};

describe("builtinSkillPresentation", () => {
  it("localizes known built-in skills for Simplified Chinese", () => {
    expect(builtinSkillPresentation(cron, "zh-CN")).toEqual({
      name: "定时任务",
      description: "创建提醒和周期性任务。",
    });
  });

  it("preserves original metadata outside Simplified Chinese", () => {
    expect(builtinSkillPresentation(cron, "en")).toEqual({
      name: cron.name,
      description: cron.description,
    });
  });

  it("preserves unknown and workspace skills", () => {
    const unknown = { name: "custom", description: "Custom skill", source: "builtin" };
    expect(builtinSkillPresentation(unknown, "zh-CN")).toEqual({
      name: unknown.name,
      description: unknown.description,
    });
    expect(builtinSkillPresentation({ ...cron, source: "workspace" }, "zh-CN")).toEqual({
      name: cron.name,
      description: cron.description,
    });
  });
});
