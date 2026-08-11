export type SetupRequirement = "agentloop" | "local-agent";

interface SetupReadinessSnapshot {
  agentLoopConfigured: boolean | null;
  localAgentCount: number | null;
}

export function missingSetupRequirements(snapshot: SetupReadinessSnapshot): SetupRequirement[] {
  const missing: SetupRequirement[] = [];
  if (snapshot.agentLoopConfigured === false) missing.push("agentloop");
  if (snapshot.localAgentCount !== null && snapshot.localAgentCount <= 0) missing.push("local-agent");
  return missing;
}
