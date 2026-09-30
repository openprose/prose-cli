import { customEnvironment } from "./dev-endpoint";

/**
 * The OpenProse service a command talks to. Public builds know exactly one:
 * production. A developer build (`bun run build:dev`, compiled with
 * `PROSE_DEV_BUILD=true`) may instead point at a custom origin; see
 * `dev-endpoint.ts`. Nothing else selects a service.
 */
export interface Environment {
  /** The JSON envelope `environment` value: `production`, or `custom` in a dev build. */
  name: "production" | "custom";
  origin: string;
  /** The environment variable holding the API key (always OPENPROSE_API_KEY). */
  credentialEnv: "OPENPROSE_API_KEY";
  /** The OS credential-store service the key is saved under (account name `api-key`). */
  storeService: string;
  /** The human label: `OpenProse`, or `OpenProse (custom endpoint <origin>)`. */
  label: string;
  /** The run-journal directory under `$XDG_STATE_HOME/openprose/cli/`. */
  journal: string;
}

export const PRODUCTION: Environment = Object.freeze({
  name: "production",
  origin: "https://run-prose-production.openprose.workers.dev",
  credentialEnv: "OPENPROSE_API_KEY",
  storeService: "org.openprose.cli.production",
  label: "OpenProse",
  journal: "production",
});

// Compile-time only: every standalone build defines it (`scripts/image-bundle.ts`).
// The guard below must stay inline at this call site: with the define set to
// false, Bun folds the branch away and drops `dev-endpoint.ts` from the bundle,
// so a public binary contains no path that reads the override variable.
declare const PROSE_DEV_BUILD: boolean | undefined;

/** The service for this process. Public builds always return production. */
export function serviceEnvironment(env: Readonly<Record<string, string | undefined>>): Environment {
  if (typeof PROSE_DEV_BUILD === "boolean" && PROSE_DEV_BUILD) {
    return customEnvironment(env) ?? PRODUCTION;
  }
  return PRODUCTION;
}

export function environmentLabel(environment: Environment): string {
  return environment.label;
}

export function journalComponent(environment: Environment): string {
  return environment.journal;
}
