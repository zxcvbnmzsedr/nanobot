import type { SkillMarketItem } from "@/lib/types";

interface ParsedSemVerV1 {
  major: string;
  minor: string;
  patch: string;
  prerelease: string[] | null;
}

const SEMVER_V1_PATTERN = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;

function parseSemVerV1(version: string): ParsedSemVerV1 | null {
  const match = SEMVER_V1_PATTERN.exec(version);
  if (!match) return null;
  const prerelease = match[4]?.split(".") ?? null;
  if (prerelease?.some((identifier) => /^\d+$/.test(identifier) && identifier.length > 1 && identifier.startsWith("0"))) {
    return null;
  }
  return {
    major: match[1],
    minor: match[2],
    patch: match[3],
    prerelease,
  };
}

function compareNumericIdentifiers(left: string, right: string): number {
  if (left.length !== right.length) return left.length < right.length ? -1 : 1;
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/** Compare two strict SemVer 2.0 values. Invalid values return null. */
export function compareSemVerV1(left: string, right: string): number | null {
  const parsedLeft = parseSemVerV1(left);
  const parsedRight = parseSemVerV1(right);
  if (!parsedLeft || !parsedRight) return null;

  for (const key of ["major", "minor", "patch"] as const) {
    const comparison = compareNumericIdentifiers(parsedLeft[key], parsedRight[key]);
    if (comparison !== 0) return comparison;
  }

  if (!parsedLeft.prerelease && !parsedRight.prerelease) return 0;
  if (!parsedLeft.prerelease) return 1;
  if (!parsedRight.prerelease) return -1;

  const length = Math.max(parsedLeft.prerelease.length, parsedRight.prerelease.length);
  for (let index = 0; index < length; index += 1) {
    const leftIdentifier = parsedLeft.prerelease[index];
    const rightIdentifier = parsedRight.prerelease[index];
    if (leftIdentifier === undefined) return -1;
    if (rightIdentifier === undefined) return 1;
    if (leftIdentifier === rightIdentifier) continue;

    const leftNumeric = /^\d+$/.test(leftIdentifier);
    const rightNumeric = /^\d+$/.test(rightIdentifier);
    if (leftNumeric && rightNumeric) {
      return compareNumericIdentifiers(leftIdentifier, rightIdentifier);
    }
    if (leftNumeric) return -1;
    if (rightNumeric) return 1;
    return leftIdentifier < rightIdentifier ? -1 : 1;
  }
  return 0;
}

/** Return published releases strictly older than the installed version, newest first. */
export function publishedRollbackVersions(skill: SkillMarketItem): string[] {
  const installedVersion = skill.installedVersion;
  if (!installedVersion || !parseSemVerV1(installedVersion)) return [];

  const candidates = (skill.versions ?? [])
    .filter((release) => release.status === "published" && release.revoked !== true)
    .map((release) => release.version)
    .filter((version) => compareSemVerV1(version, installedVersion) === -1);

  return [...new Set(candidates)].sort((left, right) => (
    compareSemVerV1(right, left) ?? 0
  ));
}
