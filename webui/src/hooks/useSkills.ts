import { useEffect, useState } from "react";

import { fetchSkills } from "@/lib/api";
import type { SkillSummary } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

export function useSkills(token: string): SkillSummary[] {
  const { client } = useClient();
  const [skills, setSkills] = useState<SkillSummary[]>([]);

  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      void fetchSkills(token)
        .then(({ skills: nextSkills }) => !cancelled && setSkills(nextSkills))
        .catch(() => !cancelled && setSkills([]));
    };
    refresh();
    const unsubscribe = client.onSkillsUpdated(refresh);
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [client, token]);

  return skills;
}
