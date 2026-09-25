#!/usr/bin/env bun

import { basename } from "node:path";
import { defaultDependencies, runCli } from "./cli";
import { recordInvokedName } from "./core/service/render";

// A developer build names itself in copyable commands exactly as it was
// invoked (argv[0]); without one, by its executable's file name.
recordInvokedName(process.argv0 !== undefined && process.argv0 !== "" ? process.argv0 : basename(process.execPath));
process.exitCode = await runCli(process.argv.slice(2), defaultDependencies());
