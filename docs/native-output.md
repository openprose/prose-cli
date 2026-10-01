# Native output

`--output-contract native` settles only when the selected native harness reports completion through its validated protocol. Assistant text is retained as text; no final JSON line is requested, recovered or synthesized. The result reports semantic status `not-applicable` and a null envelope digest. Native completion establishes that the harness finished. It does not establish that the program satisfied its requirements. Evaluate program artifacts separately.

`--output-contract image-envelope` remains the default compatibility mode. It additionally requires the image-declared model-authored terminal envelope. Configuration also supports `output_contract` and `PROSE_OUTPUT_CONTRACT`. This option is independent of output rendering (`human`, `json`, `jsonl`), authentication, and permissions.

For a Markdown program, pair native mode with an image containing only a pointer to the workspace entry instructions. The current lab image is `lab/tools/cli-baseline/native-image`; its entry asks the agent to read README.md and the task's program. It contains no terminal-format instruction. The packaging manifest retains a terminal schema for compatibility with image loading, but native mode does not apply it to assistant text.

Example (with the chosen native executable on PATH and API key in the child environment):

```sh
prose --harness claude --model claude-haiku-4-5 --auth-profile anthropic-api-key --permission-mode acceptEdits --output-contract native --cwd /absolute/workspace --output jsonl run test/PROGRAM.md
```

No language constructs are interpreted by this option. Harness errors, missing native terminal events and malformed native streams remain transport failures. An envelope-only legacy compatibility test should continue selecting image-envelope explicitly.

OMP event compatibility: its schema validator can omit optional null or string `null` fields between model tool declaration and native execution. The adapter permits only that omission while requiring every actual field and all non-null declarations to match. This checks correlated events, not full schema equivalence; event data does not expose enough schema context to prove optionality. The original failed OpenAI todo trace remains in the lab as regression evidence.

OMP may also prune its terminal history after executing tools. Two verified native markers (`[Superseded by a newer read of this file]` and `[Uneventful result elided]`) with a finite nonnegative `prunedAt` timestamp are accepted only as terminal tool-result content projections. All tool identity and other metadata must still match the recorded original event. The original native tool output remains the evidence; the placeholder is not a substitute result. Other invented or altered terminal histories remain rejected.
