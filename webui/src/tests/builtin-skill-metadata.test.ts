import { describe, expect, it } from "vitest";

import { builtinSkillPresentation } from "@/components/settings/builtinSkillMetadata";

const weather = {
  name: "weather",
  description: "Get current weather and forecasts (no API key required).",
  source: "builtin",
};

describe("builtinSkillPresentation", () => {
  it("localizes known built-in skills for Simplified Chinese", () => {
    expect(builtinSkillPresentation(weather, "zh-CN")).toEqual({
      name: "天气",
      description: "查询当前天气和天气预报，无需 API Key。",
    });
  });

  it("preserves original metadata outside Simplified Chinese", () => {
    expect(builtinSkillPresentation(weather, "en")).toEqual({
      name: weather.name,
      description: weather.description,
    });
  });

  it("preserves unknown and workspace skills", () => {
    const unknown = { name: "custom", description: "Custom skill", source: "builtin" };
    expect(builtinSkillPresentation(unknown, "zh-CN")).toEqual({
      name: unknown.name,
      description: unknown.description,
    });
    expect(builtinSkillPresentation({ ...weather, source: "workspace" }, "zh-CN")).toEqual({
      name: weather.name,
      description: weather.description,
    });
  });
});
