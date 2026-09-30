import { createHash } from "node:crypto";
import { failure } from "../errors";
import { RunnerFailure } from "../types";
import type { Environment } from "./endpoint";

/**
 * Developer builds only (`PROSE_DEV_BUILD=true`; `endpoint.ts` is the one
 * caller and public builds compile this module out). OPENPROSE_API_URL, an
 * https origin, replaces the production service. The key is still
 * OPENPROSE_API_KEY, but a login is stored under a credential-store service
 * scoped to the origin, so it never overwrites the production key.
 *
 * Contract shared with the Rust `dev-endpoint` feature (both ports read the
 * same OS credential store and journal tree):
 * - digest = first 16 lowercase hex digits of sha256(origin), origin in its
 *   normalized `scheme://host[:port]` form (no trailing slash);
 * - store service `org.openprose.cli.custom-<digest>`, account `api-key`;
 * - journal directory `custom-<digest>`;
 * - envelope `environment` is `custom`; label `OpenProse (custom endpoint <origin>)`.
 */
export const DEV_ENDPOINT_VARIABLE = "OPENPROSE_API_URL";
/** The Action of an invalid override (identical in both ports). */
export const DEV_ENDPOINT_INVALID_ACTION = "Set OPENPROSE_API_URL to an https origin such as https://host.example, or unset it, then retry.";

export function customEnvironment(env: Readonly<Record<string, string | undefined>>): Environment | undefined {
  const raw = env[DEV_ENDPOINT_VARIABLE];
  if (raw === undefined || raw.length === 0) return undefined;
  const origin = customOrigin(raw);
  const digest = createHash("sha256").update(origin).digest("hex").slice(0, 16);
  return {
    name: "custom",
    origin,
    credentialEnv: "OPENPROSE_API_KEY",
    storeService: `org.openprose.cli.custom-${digest}`,
    label: `OpenProse (custom endpoint ${origin})`,
    journal: `custom-${digest}`,
  };
}

/**
 * The normalized origin of `raw` (`https://host[:port]`, lowercase host, no
 * default port, no trailing slash), with the Rust port's strict ASCII grammar
 * (shared/fixtures/dev-endpoint-origins.json): the scheme matches without
 * case; host labels are letters, digits and inner hyphens; the port is
 * 1-65535; at most one trailing slash follows.
 */
export function customOrigin(raw: string): string {
  const invalid = (why: string): RunnerFailure => {
    const error = failure("CONFIG_INVALID", { reason: `${DEV_ENDPOINT_VARIABLE} ${why}; expected an https origin such as https://host.example` });
    return new RunnerFailure({
      code: error.code, boundary: error.boundary, message: error.message, action: DEV_ENDPOINT_INVALID_ACTION,
      exitCode: error.exitCode, retryable: error.retryable, ...(error.details === undefined ? {} : { details: error.details }),
    });
  };
  const separator = raw.indexOf("://");
  if (separator < 0) throw invalid("is not a URL");
  if (raw.slice(0, separator).toLowerCase() !== "https") throw invalid("must use https");
  const rest = raw.slice(separator + 3);
  const end = rest.search(/[/?#]/u);
  const [authority, tail] = end < 0 ? [rest, ""] : [rest.slice(0, end), rest.slice(end)];
  if (authority.includes("@")) throw invalid("must not contain credentials");
  if (tail !== "" && tail !== "/") throw invalid("must be an origin with no path, query or fragment");
  const colon = authority.lastIndexOf(":");
  const [hostText, port] = colon < 0 ? [authority, undefined] : [authority.slice(0, colon), authority.slice(colon + 1)];
  if (hostText.length === 0) throw invalid("has no host");
  const host = hostText.replace(/[A-Z]/gu, (letter) => letter.toLowerCase());
  const label = (part: string) => part.length > 0 && !part.startsWith("-") && !part.endsWith("-") && /^[a-z0-9-]+$/u.test(part);
  if (host.length > 253 || !host.split(".").every(label)) throw invalid("is not a URL");
  let number: number | undefined;
  if (port !== undefined) {
    number = /^[0-9]+$/u.test(port) ? Number(port) : NaN;
    if (!Number.isInteger(number) || number < 1 || number > 65_535) throw invalid("is not a URL");
  }
  return number === undefined || number === 443 ? `https://${host}` : `https://${host}:${number}`;
}
