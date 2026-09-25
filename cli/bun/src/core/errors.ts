import taxonomy from "../../../shared/errors/taxonomy.v1.json" with { type: "json" };
import { RunnerFailure, type RunnerErrorCode, type RunnerErrorShape } from "./types";

export function failure(code: RunnerErrorCode, details?: Record<string, unknown>): RunnerFailure {
  const definition = taxonomy.errors.find((item) => item.code === code);
  if (definition === undefined) throw new Error(`Missing shared error taxonomy entry for ${code}`);
  return new RunnerFailure({
    code,
    boundary: definition.boundary as RunnerErrorShape["boundary"],
    message: definition.message,
    action: definition.action,
    exitCode: definition.exitCode,
    retryable: definition.retryable,
    ...(details === undefined ? {} : { details }),
  });
}

/**
 * The Action of a failed hosted run whose reason names a known cause
 * (mirrors Rust `run_failure_action`; `shared/fixtures/human/run-failure-actions.json`):
 * a run that ran out of budget or hit the step limit is not fixed by
 * correcting its inputs. Undefined keeps the catalog Action.
 */
export function runFailureAction(reason: string): string | undefined {
  const text = reason.replace(/[A-Z]/gu, (letter) => letter.toLowerCase());
  if (text.includes("budget")) return "Raise the run budget, choose another environment, or check `cli wallet balance`, then submit again.";
  if (["step limit", "max steps", "maximum steps", "max_steps", "too many steps"].some((cause) => text.includes(cause))) {
    return "Simplify the program or split it into steps, then submit again.";
  }
  return undefined;
}

/** HOSTED_RUN_FAILED with `details.reason`, its Action chosen by {@link runFailureAction} (mirrors Rust `RunnerError::hosted_run_failed`). */
export function hostedRunFailed(details: Record<string, unknown> & { reason: string }): RunnerFailure {
  const error = failure("HOSTED_RUN_FAILED", details);
  const action = runFailureAction(details.reason);
  if (action === undefined) return error;
  const { schema: _schema, ...shape } = error.toJSON();
  return new RunnerFailure({ ...shape, action });
}

export function invocationFailure(reason: string): RunnerFailure {
  return failure("INVOCATION_INVALID", { reason });
}
