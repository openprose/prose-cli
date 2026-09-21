import { invocationFailure } from "./errors";
export interface PackageCommand {
  operation: "publish" | "fetch" | "list" | "withdraw";
  input: string;
  organization?: string;
  name?: string;
  version?: string;
  public?: boolean;
  outputDir?: string;
  sha256?: string;
  cursor?: string;
}
export function parsePackageCommand(args: readonly string[]): PackageCommand {
  const invalid = (): never => { throw invocationFailure("Invalid package command or options."); };
  const operation = args[0];
  if (operation !== "publish" && operation !== "fetch" && operation !== "list" && operation !== "withdraw") return invalid();
  const input = args[1];
  if (input === undefined || input.length === 0 || input.startsWith("--")) return invalid();
  const command: PackageCommand = { operation, input };
  const allowed = operation === "publish" ? ["organization", "name", "version", "public"] : operation === "fetch" ? ["output-dir", "sha256"] : operation === "list" ? ["cursor"] : [];
  const seen = new Set<string>();
  for (let index = 2; index < args.length; index++) {
    const raw = args[index]!;
    const equals = raw.indexOf("=");
    const option = (equals < 0 ? raw : raw.slice(0, equals)).slice(2);
    if (!raw.startsWith("--") || !allowed.includes(option) || seen.has(option)) return invalid();
    seen.add(option);
    if (option === "public") {
      if (equals >= 0) return invalid();
      command.public = true;
      continue;
    }
    const value = equals < 0 ? args[++index] : raw.slice(equals + 1);
    if (value === undefined || value.length === 0 || value.startsWith("--")) return invalid();
    Object.assign(command, { [option === "output-dir" ? "outputDir" : option]: value });
  }
  if (operation === "publish" && (!command.organization || !command.name || !command.version)) return invalid();
  if (operation === "fetch" && !command.outputDir) return invalid();
  return command;
}
