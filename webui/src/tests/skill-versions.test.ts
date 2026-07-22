import { describe, expect, it } from "vitest";

import {
  compareSemVerV1,
  publishedRollbackVersions,
} from "@/components/settings/skillVersions";
import type { SkillMarketItem } from "@/lib/types";

describe("Skill marketplace version ordering", () => {
  it("compares SemVer precedence, including prereleases and large numeric identifiers", () => {
    expect(compareSemVerV1("1.10.0", "1.9.0")).toBe(1);
    expect(compareSemVerV1("1.0.0-beta.11", "1.0.0-beta.2")).toBe(1);
    expect(compareSemVerV1("1.0.0-beta", "1.0.0")).toBe(-1);
    expect(compareSemVerV1("99999999999999999999.0.0", "2.0.0")).toBe(1);
    expect(compareSemVerV1("1.0.0+build.2", "1.0.0+build.1")).toBe(0);
    expect(compareSemVerV1("1.0", "1.0.0")).toBeNull();
    expect(compareSemVerV1("1.0.0-beta.01", "1.0.0-beta.1")).toBeNull();
  });

  it("only offers published releases strictly below the installed version", () => {
    const skill: SkillMarketItem = {
      skillId: "sales-helper",
      installedVersion: "2.0.0",
      previousVersion: "9.0.0",
      versions: [
        { version: "3.0.0", status: "published" },
        { version: "1.9.0", status: "published" },
        { version: "1.10.0", status: "published" },
        { version: "1.9.0", status: "published" },
        { version: "1.8.0", status: "published", revoked: true },
        { version: "1.7.0", status: "revoked" },
        { version: "1.6.0", status: "stable" },
        { version: "not-semver", status: "published" },
      ],
    };

    expect(publishedRollbackVersions(skill)).toEqual(["1.10.0", "1.9.0"]);
  });

  it("orders published prereleases from newest to oldest", () => {
    expect(publishedRollbackVersions({
      skillId: "preview-helper",
      installedVersion: "1.0.0",
      versions: [
        { version: "1.0.0-beta.2", status: "published" },
        { version: "1.0.0-beta.11", status: "published" },
        { version: "1.0.0-alpha", status: "published" },
      ],
    })).toEqual(["1.0.0-beta.11", "1.0.0-beta.2", "1.0.0-alpha"]);
  });
});
