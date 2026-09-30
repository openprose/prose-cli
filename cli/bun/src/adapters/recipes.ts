import sdkJson from "../../../shared/capabilities/adapters/recipes/agents-sdk-jsonl.v1.json" with { type: "json" };
import oracleJson from "../../../shared/capabilities/adapters/oracle.v1.json" with { type: "json" };
import claudeJson from "../../../shared/capabilities/adapters/recipes/claude-print-stream-json.v1.json" with { type: "json" };
import codexJson from "../../../shared/capabilities/adapters/recipes/codex-exec-json.v1.json" with { type: "json" };
import codexDeveloperJson from "../../../shared/capabilities/adapters/recipes/codex-exec-json-developer.v1.json" with { type: "json" };
import codexBaseJson from "../../../shared/capabilities/adapters/recipes/codex-exec-json-base.v1.json" with { type: "json" };
import { createHash } from "node:crypto";
import { CODEX_INSTRUCTION_PLACEMENT, type CodexInstructionPlacement } from "../core/build";
import ompJson from "../../../shared/capabilities/adapters/recipes/omp-rpc.v1.json" with { type: "json" };
import primeJson from "../../../shared/capabilities/adapters/recipes/prime-rpc.v1.json" with { type: "json" };
import { failure } from "../core/errors";
import type {
  CredentialRequirement,
  InstalledAdapterDefinition,
  InstalledAdapterId,
  InstalledAdapterRecipe,
  RuntimePrerequisiteRequirement,
} from "./types";

const recipeValues = [codexJson, claudeJson, primeJson, ompJson, sdkJson] as unknown as InstalledAdapterRecipe[];
const recipeDigests: Record<InstalledAdapterId, string> = {
  "agents-sdk/jsonl": "1141c7ed0040d8f0783e5f8870f174c1b66a0eb3299d12e4edebfa98c29228e6",
  "codex/exec-json": "41c1fd72796defe256a338f92f5ebf850e6522d2eb265c8746512a40a2a03f82",
  "claude/print-stream-json": "25440e90c81774619f937d36a79786b9fd4bcb85b8ecfbc486f825307429213c",
  "prime/rpc": "7a9f69497424e56ca214d6cbcd7946b74dbbc03a61f4c110cde8d28a3ad562e1",
  "omp/rpc": "8347937e721cfd2a18a25636519a6cce7707ddaf7bdac8e337992e1b918fd35f",
};
const oracle = oracleJson as unknown as {
  baseEnvironmentAllowlist: string[];
  environmentRules: {
    alwaysStrip: string[];
    adapterOwnedControls: Partial<Record<InstalledAdapterId, Record<string, string>>>;
  };
  adapters: Array<{
    adapterId: InstalledAdapterId;
    credentialGroups: Record<string, string[]>;
    credentialRequirements: Record<string, CredentialRequirement>;
    strictAdmission: { status: string };
  }>;
};

const definitions = new Map<InstalledAdapterId, InstalledAdapterDefinition>();
for (const recipe of recipeValues) {
  const admission = oracle.adapters.find((item) => item.adapterId === recipe.adapterId);
  if (admission === undefined || admission.strictAdmission.status !== "blocked") {
    throw new Error(`Installed adapter oracle drift for ${recipe.adapterId}`);
  }
  if (recipe.state !== "frozen" || recipe.admissionClaims.length !== 0) {
    throw new Error(`Unadmitted installed adapter recipe changed state for ${recipe.adapterId}`);
  }
  const runtimePrerequisites = parseRuntimePrerequisites(recipe);
  definitions.set(recipe.adapterId, {
    id: recipe.adapterId,
    recipe,
    recipeSha256: recipeDigests[recipe.adapterId],
    runtimePrerequisites,
    credentialGroups: admission.credentialGroups,
    credentialRequirements: admission.credentialRequirements,
    strictAdmission: "blocked",
    billingOwner: "user-provider",
    authCategory: "harness-managed",
  });
}

function parseRuntimePrerequisites(
  recipe: InstalledAdapterRecipe,
): readonly RuntimePrerequisiteRequirement[] {
  const values = recipe.support.runtimePrerequisites ?? [];
  if (recipe.adapterId !== "omp/rpc") {
    if (values.length !== 0) throw new Error(`Unexpected runtime prerequisite for ${recipe.adapterId}`);
    return Object.freeze([]);
  }
  if (values.length !== 1) throw new Error("OMP must declare exactly one runtime prerequisite");
  const value = values[0];
  if (
    value?.runtime !== "bun"
    || value.versionRange !== ">=1.3.14"
    || value.repairCommand !== "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9"
  ) {
    throw new Error("OMP runtime prerequisite drifted from the admitted contract");
  }
  return Object.freeze([{ ...value }]);
}

export const installedAdapterIds = Object.freeze([...definitions.keys()]);
export const adapterBaseEnvironmentAllowlist = Object.freeze([...oracle.baseEnvironmentAllowlist]);
export const adapterAlwaysStrip = Object.freeze([...oracle.environmentRules.alwaysStrip]);

export function adapterOwnedEnvironmentControls(id: InstalledAdapterId): Readonly<Record<string, string>> {
  return Object.freeze({ ...(oracle.environmentRules.adapterOwnedControls[id] ?? {}) });
}

export function installedAdapterDefinition(id: string, codexPlacement: CodexInstructionPlacement = CODEX_INSTRUCTION_PLACEMENT): InstalledAdapterDefinition {
  const definition = definitions.get(id as InstalledAdapterId);
  if (definition === undefined) {
    throw failure("TRANSPORT_UNSUPPORTED", { adapterId: id, fallbackAttempted: false });
  }
  if (id !== "codex/exec-json" || codexPlacement === "framed") return definition;
  if (codexPlacement !== "developer" && codexPlacement !== "base") {
    throw failure("CONFIG_INVALID", { reason: "Unknown compiled Codex instruction placement." });
  }
  const recipe = (codexPlacement === "developer" ? codexDeveloperJson : codexBaseJson) as unknown as InstalledAdapterRecipe;
  return {
    ...definition,
    recipe,
    recipeSha256: createHash("sha256").update(JSON.stringify(recipe, null, 2) + "\n").digest("hex"),
  };
}
