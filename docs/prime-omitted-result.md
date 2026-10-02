# Native Prime omitted result notifications

Native-output mode can provisionally continue when Prime starts an empty assistant message after exactly one declared tool has completed, but omits the result end (and possibly its start) and the turn markers. The prior assistant must have stopped for tool use; there must be no other open message, reported result, pending tool or terminal candidate. If a result start was observed, its complete message must equal the final-history entry. The completed tool must have an observed result array and Boolean error status.

Continuation does not establish delivery or completion. The parser retains a comparison position for that result. At a real agent_end, producer history must supply a toolResult with the same call identity, tool name, content, details and error status. Optional timestamps must be finite and nonnegative, and unknown fields are rejected. All other history must satisfy the existing exact comparison rules. A missing, duplicate or changed result fails settlement. This is corroboration between observations from the same producer, not authenticated independent evidence.

The parser emits no replacement events and makes no claim about unobserved hooks. Multiple unreported tools are outside this rule. Legacy envelope mode and OMP keep their existing behavior. Final stop, turn completion, required fresh queue state and zero process exit remain required. An accepted prefix cannot certify a complete operation or contract fulfillment.

The shared synthetic fixture reproduces notification omissions observed during a document workflow. The original live run ended at the rejected assistant start and does not contain a successful final history. Tests add an explicitly synthetic continuation, including mismatched-result and truncated-history controls; a later live run must establish whether this compatibility rule permits the actual workflow to finish.

## Qualification limit

This compatibility rule has not qualified the affected live document workflow. A later native trace omitted the declaring assistant message and tool start before emitting a tool update. The same omission was present in independently recorded producer stdout, and this candidate correctly rejects that broader event loss. Passing the narrow synthetic fixture does not establish a repair for that transport failure. Keep the candidate separate from a release claim until an actual native workflow completes with the required evidence.
