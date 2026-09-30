use super::{
    ErrorCode, OmpExtensionUiDisposition, RunnerError, TransportNormalization, Value,
    has_exact_keys, json, omp_extension_ui_disposition, omp_rpc_id, prime_bounded_json,
    record_type, valid_omp_ready,
};
use std::collections::BTreeMap;

pub(super) fn rich(record: &Value) -> bool {
    record
        .pointer("/message/content")
        .and_then(Value::as_array)
        .is_some_and(|items| {
            items
                .iter()
                .any(|b| b["type"] == "toolCall" || b.get("index").is_some())
        })
        || record
            .pointer("/assistantMessageEvent/type")
            .and_then(Value::as_str)
            .is_some_and(|s| s.starts_with("toolcall_"))
        || record_type(record).is_some_and(|s| s.starts_with("tool_execution_"))
        || record
            .pointer("/data/dumpTools")
            .and_then(Value::as_array)
            .is_some_and(|t| !t.is_empty())
}
// Child progress is telemetry only: it never settles a parent tool or message.
fn valid_prime_queue(record: &Value) -> bool {
    if !has_exact_keys(record, &["type", "actions"]) {
        return false;
    }
    let Some(a) = record["actions"].as_object() else {
        return false;
    };
    if a.keys()
        .any(|k| !["queuedCount", "steering", "followUps", "active"].contains(&k.as_str()))
        || a.get("queuedCount")
            .and_then(Value::as_f64)
            .is_none_or(|n| {
                !n.is_finite() || n < 0.0 || n.fract() != 0.0 || n > 9_007_199_254_740_991.0
            })
    {
        return false;
    }
    for k in ["steering", "followUps"] {
        if a.get(k)
            .and_then(Value::as_array)
            .is_none_or(|xs| xs.iter().any(|v| !v.is_string()))
        {
            return false;
        }
    }
    if let Some(active) = a.get("active") {
        let Some(x) = active.as_object() else {
            return false;
        };
        if x.keys()
            .any(|k| !["kind", "phase", "label"].contains(&k.as_str()))
            || !matches!(
                x.get("kind").and_then(Value::as_str),
                Some("turn" | "session_command")
            )
            || !matches!(
                x.get("phase").and_then(Value::as_str),
                Some("preparing" | "committing" | "running")
            )
            || x.get("label").is_some_and(|v| !v.is_string())
        {
            return false;
        }
    }
    true
}

fn valid_prime_child_update(record: &Value) -> bool {
    if !has_exact_keys(record, &["type", "child"]) {
        return false;
    }
    let Some(child) = record["child"].as_object() else {
        return false;
    };
    let allowed = [
        "id",
        "label",
        "status",
        "sessionDir",
        "parentId",
        "activeSessionId",
        "sessionName",
        "model",
        "answerPreview",
        "recap",
        "error",
        "durationMs",
        "toolUseCount",
        "tokenCount",
        "repliedSinceTask",
        "activity",
    ];
    if child.keys().any(|key| !allowed.contains(&key.as_str()))
        || ["id", "sessionDir"].iter().any(|key| {
            child
                .get(*key)
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
        })
        || child.get("label").and_then(Value::as_str).is_none()
        || !matches!(
            child.get("status").and_then(Value::as_str),
            Some("queued" | "running" | "done" | "error" | "cancelled")
        )
    {
        return false;
    }
    for key in [
        "parentId",
        "activeSessionId",
        "sessionName",
        "model",
        "answerPreview",
        "recap",
        "error",
    ] {
        if child.get(key).is_some_and(|v| !v.is_string()) {
            return false;
        }
    }
    if child
        .get("durationMs")
        .is_some_and(|v| v.as_f64().is_none_or(|n| !n.is_finite() || n < 0.0))
    {
        return false;
    }
    for key in ["toolUseCount", "tokenCount"] {
        if child.get(key).is_some_and(|v| {
            v.as_f64().is_none_or(|n| {
                !n.is_finite() || n < 0.0 || n.fract() != 0.0 || n > 9_007_199_254_740_991.0
            })
        }) {
            return false;
        }
    }
    if child
        .get("repliedSinceTask")
        .is_some_and(|v| !v.is_boolean())
    {
        return false;
    }
    if let Some(activity) = child.get("activity") {
        let Some(a) = activity.as_object() else {
            return false;
        };
        if a.keys()
            .any(|key| !["kind", "toolName"].contains(&key.as_str()))
            || !matches!(
                a.get("kind").and_then(Value::as_str),
                Some("waiting" | "writing" | "executing")
            )
            || a.get("toolName").is_some_and(|v| !v.is_string())
        {
            return false;
        }
    }
    true
}

fn valid_custom(m: &Value) -> bool {
    let Some(o) = m.as_object() else {
        return false;
    };
    if o.keys().any(|k| {
        ![
            "role",
            "customType",
            "content",
            "display",
            "details",
            "attribution",
            "timestamp",
        ]
        .contains(&k.as_str())
    }) || m["role"] != "custom"
        || !m["customType"].is_string()
        || !m["display"].is_boolean()
        || m["timestamp"]
            .as_f64()
            .is_none_or(|n| !n.is_finite() || n < 0.0)
        || o.get("attribution")
            .is_some_and(|v| !matches!(v.as_str(), Some("user" | "agent")))
    {
        return false;
    }
    if m["content"].is_string() {
        return true;
    }
    m["content"].as_array().is_some_and(|xs| {
        xs.iter().all(|b| {
            let Some(o) = b.as_object() else {
                return false;
            };
            if b["type"] == "text" {
                return o
                    .keys()
                    .all(|k| ["type", "text", "textSignature"].contains(&k.as_str()))
                    && b["text"].is_string()
                    && o.get("textSignature").is_none_or(Value::is_string);
            }
            if b["type"] != "image"
                || o.keys().any(|k| {
                    !["type", "data", "mimeType", "detail", "providerFile", "url"]
                        .contains(&k.as_str())
                })
                || !b["data"].is_string()
                || !b["mimeType"].is_string()
                || o.get("url").is_some_and(|v| !v.is_string())
                || o.get("detail").is_some_and(|v| {
                    !matches!(v.as_str(), Some("auto" | "low" | "high" | "original"))
                })
            {
                return false;
            }
            if let Some(f) = o.get("providerFile") {
                let Some(o) = f.as_object() else {
                    return false;
                };
                if o.keys()
                    .any(|k| !["provider", "id", "uri", "expiresAt"].contains(&k.as_str()))
                    || !matches!(
                        f["provider"].as_str(),
                        Some("openai" | "anthropic" | "google")
                    )
                    || ["id", "uri"]
                        .iter()
                        .any(|k| o.get(*k).is_some_and(|v| !v.is_string()))
                    || o.get("expiresAt")
                        .is_some_and(|v| v.as_f64().is_none_or(|n| !n.is_finite()))
                {
                    return false;
                }
            }
            true
        })
    })
}

fn same_message(a: &Value, b: &Value, omp: bool) -> bool {
    if !omp || a["role"] == "custom" || b["role"] == "custom" {
        return a == b;
    }
    let mut a = a.clone();
    let mut b = b.clone();
    if let Some(o) = a.as_object_mut() {
        o.remove("completedAt");
    }
    if let Some(o) = b.as_object_mut() {
        o.remove("completedAt");
    }
    if a["role"].as_str() == Some("toolResult")
        && a["prunedAt"]
            .as_f64()
            .is_some_and(|n| n.is_finite() && n >= 0.0)
        && [
            "[Superseded by a newer read of this file]",
            "[Uneventful result elided]",
        ]
        .iter()
        .any(|text| a["content"] == serde_json::json!([{"type":"text","text":text}]))
    {
        a.as_object_mut().unwrap().remove("prunedAt");
        a["content"] = b["content"].clone();
    }
    a == b
}

#[derive(Default)]
struct PrimeDrain {
    session: String,
    segment_closed: bool,
    resumed: bool,
    queue_observed: bool,
    queue_empty: bool,
    children: BTreeMap<String, String>,
    previews: std::collections::BTreeSet<String>,
    used: std::collections::BTreeSet<String>,
    used_previews: std::collections::BTreeSet<String>,
}
fn drain_message_equal(a: &Value, b: &Value) -> bool {
    fn usage(v: &Value) -> bool {
        has_exact_keys(
            v,
            &[
                "input",
                "output",
                "cacheRead",
                "cacheWrite",
                "totalTokens",
                "cost",
            ],
        ) && ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]
            .iter()
            .all(|k| v[*k].as_f64().is_some_and(|n| n.is_finite() && n >= 0.0))
            && has_exact_keys(
                &v["cost"],
                &["input", "output", "cacheRead", "cacheWrite", "total"],
            )
            && v["cost"]
                .as_object()
                .unwrap()
                .values()
                .all(|v| v.as_f64().is_some_and(|n| n.is_finite() && n >= 0.0))
    }
    if a["role"] != "assistant"
        || b["role"] != "assistant"
        || (a.get("usage").is_none() && b.get("usage").is_none())
    {
        return a == b;
    }
    if !usage(&a["usage"]) || !usage(&b["usage"]) {
        return false;
    }
    let mut a = a.clone();
    let mut b = b.clone();
    a.as_object_mut().unwrap().remove("usage");
    b.as_object_mut().unwrap().remove("usage");
    a == b
}
impl PrimeDrain {
    fn child(&mut self, c: &Value) {
        if let (Some(id), Some(name)) = (c["activeSessionId"].as_str(), c["sessionName"].as_str()) {
            self.children.insert(id.into(), name.into());
        }
    }
    fn queue(&mut self, a: &Value, candidate: bool) -> bool {
        if candidate && a.get("active").is_some() {
            return false;
        }
        self.queue_observed = true;
        self.queue_empty = a["queuedCount"] == 0
            && a["steering"].as_array().is_some_and(Vec::is_empty)
            && a["followUps"].as_array().is_some_and(Vec::is_empty)
            && a.get("active").is_none();
        for k in ["steering", "followUps"] {
            for v in a[k].as_array().unwrap() {
                self.previews.insert(v.as_str().unwrap().into());
            }
        }
        true
    }
    fn history(&mut self, observed: &[Value], streamed: &[Value]) -> bool {
        if observed.len() == streamed.len()
            && observed
                .iter()
                .zip(streamed)
                .all(|(a, b)| drain_message_equal(a, b))
        {
            return true;
        }
        if !self.resumed
            || observed.len() != streamed.len() + 1
            || !observed[1..]
                .iter()
                .zip(streamed)
                .all(|(a, b)| drain_message_equal(a, b))
        {
            return false;
        }
        let m = &observed[0];
        let d = &m["details"];
        let from = &d["from"];
        let target = &d["target"];
        let nonempty = |v: &Value| v.as_str().is_some_and(|s| !s.is_empty());
        if !has_exact_keys(
            m,
            &[
                "role",
                "customType",
                "content",
                "display",
                "details",
                "timestamp",
            ],
        ) || m["role"] != "custom"
            || m["customType"] != "agent_message"
            || m["display"] != true
            || m["timestamp"]
                .as_f64()
                .is_none_or(|n| !n.is_finite() || n < 0.0)
            || !has_exact_keys(d, &["id", "message", "from", "fromRelationship", "target"])
            || !nonempty(&d["id"])
            || !nonempty(&d["message"])
            || d["fromRelationship"] != "child"
            || !has_exact_keys(
                from,
                &[
                    "activeSessionId",
                    "sessionId",
                    "sessionName",
                    "clientId",
                    "runtimeKind",
                ],
            )
            || from["runtimeKind"] != "subagent"
            || ["activeSessionId", "sessionId", "sessionName", "clientId"]
                .iter()
                .any(|k| !nonempty(&from[*k]))
            || target.as_object().is_none_or(|o| {
                o.keys().any(|k| {
                    !["activeSessionId", "sessionId", "sessionName", "runtimeKind"]
                        .contains(&k.as_str())
                })
            })
            || target["runtimeKind"] != "top-level"
            || !nonempty(&target["activeSessionId"])
            || target["sessionId"] != self.session
            || target.get("sessionName").is_some_and(|v| !v.is_string())
        {
            return false;
        }
        let id = d["id"].as_str().unwrap();
        let body = d["message"].as_str().unwrap();
        let sender = from["activeSessionId"].as_str().unwrap();
        if self.used.contains(id)
            || self.children.get(sender).map(String::as_str) != from["sessionName"].as_str()
        {
            return false;
        }
        let preview = format!("Agent message received: {body}");
        if !self.previews.contains(&preview) || self.used_previews.contains(&preview) {
            return false;
        }
        let fmt = |s: &str| {
            s.split(|c: char| c.is_whitespace() || matches!(c, ',' | '[' | ']'))
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
        };
        let get = |v: &Value, k: &str| fmt(v[k].as_str().unwrap());
        let sender = [
            get(from, "sessionName"),
            format!("active {}", get(from, "activeSessionId")),
            format!("session {}", get(from, "sessionId")),
            format!("client {}", get(from, "clientId")),
        ]
        .into_iter()
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(", ");
        let endpoint = format!(
            "{}active {}, session {}",
            target
                .get("sessionName")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(|s| format!("{}, ", fmt(s)))
                .unwrap_or_default(),
            get(target, "activeSessionId"),
            get(target, "sessionId")
        );
        let content = format!(
            "[from child:{}]\nAgent-to-agent message received.\nSource: agent_message\nFrom: {sender}\nTo: {endpoint}\nMessage id: {id}\n\n{body}",
            get(from, "sessionName")
        );
        if m["content"] != content {
            return false;
        }
        self.used.insert(id.into());
        self.previews.remove(&preview);
        self.used_previews.insert(preview);
        true
    }
}

/// Validates only native transport state. Prefix mode projects already-complete
/// messages without manufacturing terminal records or claiming settlement.
pub(super) fn normalize(
    records: &[Value],
    id: &str,
    omp: bool,
    terminal: bool,
) -> Result<TransportNormalization, RunnerError> {
    normalize_mode(records, id, omp, terminal, false)
}

pub(super) fn normalize_mode(
    records: &[Value],
    id: &str,
    omp: bool,
    terminal: bool,
    drain: bool,
) -> Result<TransportNormalization, RunnerError> {
    let mut drain_state = PrimeDrain::default();
    let bad = || RunnerError::catalog(ErrorCode::ProtocolMalformed);
    let fail = || RunnerError::catalog(ErrorCode::HarnessFailed);
    let mut ready = !omp;
    let mut commands = !omp;
    let mut inventory = !omp;
    let mut ack = false;
    let mut started = false;
    let mut turn = false;
    let mut user = false;
    let mut ended = false;
    let mut open: Option<Value> = None;
    let mut assistant: Option<Value> = None;
    let mut last_stop: Option<String> = None;
    let mut history = Vec::<Value>::new();
    let mut results = Vec::<Value>::new();
    let mut texts = Vec::<String>::new();
    let mut block_types = BTreeMap::<usize, String>::new();
    let mut task_defaults = (false, false);
    let mut async_tasks = BTreeMap::<String, (Value, String)>::new();
    let mut calls = BTreeMap::<String, (String, Value, u8, Option<Value>, Option<bool>)>::new();
    for (record_index, r) in records.iter().enumerate() {
        let phase = if ended {
            "complete"
        } else if !started {
            "tool-await-agent-start"
        } else if open.is_some() {
            "tool-message-open"
        } else if turn {
            "tool-turn-open"
        } else if last_stop.as_deref() == Some("toolUse") {
            "tool-await-next-turn"
        } else {
            "tool-await-agent-end"
        };
        let bad = || {
            let error = RunnerError::catalog(ErrorCode::ProtocolMalformed);
            if omp {
                error
            } else {
                error.with_detail("adapterDiagnostic",json!({"schema":"openprose.adapter-diagnostic/1","adapterId":"prime/rpc","stage":"prime-lifecycle","phase":phase,"counters":{"acceptedRecords":record_index.min(u32::MAX as usize),"thinkingDeltas":records[..record_index].iter().filter(|v|v.pointer("/assistantMessageEvent/type").and_then(Value::as_str)==Some("thinking_delta")).count().min(u32::MAX as usize),"textDeltas":records[..record_index].iter().filter(|v|v.pointer("/assistantMessageEvent/type").and_then(Value::as_str)==Some("text_delta")).count().min(u32::MAX as usize),"saturated":record_index>u32::MAX as usize}}))
            }
        };
        if !r.is_object() || !prime_bounded_json(r, 0) {
            return Err(bad());
        }
        let kind = record_type(r).ok_or_else(bad)?;
        if omp && kind == "extension_ui_request" {
            match omp_extension_ui_disposition(r) {
                OmpExtensionUiDisposition::Presentation => continue,
                OmpExtensionUiDisposition::Blocked => return Err(fail()),
                _ => return Err(bad()),
            }
        }
        if !ready {
            if !valid_omp_ready(r) {
                return Err(bad());
            }
            ready = true;
            continue;
        }
        if kind == "available_commands_update" && omp {
            if commands
                || started
                || !has_exact_keys(r, &["type", "commands"])
                || !r["commands"].is_array()
            {
                return Err(bad());
            }
            commands = true;
            continue;
        }
        if !commands {
            return Err(bad());
        }
        if kind == "response" {
            if drain && drain_state.session.is_empty() {
                if !has_exact_keys(r, &["id", "type", "command", "success", "data"])
                    || !r["data"].is_object()
                    || r["id"] != format!("{id}.prime.state.1")
                    || r["command"] != "get_state"
                    || r["success"] != true
                    || r["data"]["isStreaming"] != false
                    || r["data"]["messageCount"] != 0
                {
                    return Err(bad());
                }
                drain_state.session = r["data"]["sessionId"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or_else(bad)?
                    .to_owned();
                continue;
            }
            if omp && !inventory {
                if r["id"] != omp_rpc_id(id, "state.1")
                    || r["command"] != "get_state"
                    || !has_exact_keys(r, &["id", "type", "command", "success", "data"])
                {
                    return Err(bad());
                }
                if r["success"] != true {
                    return Err(fail());
                }
                let tools = r
                    .pointer("/data/dumpTools")
                    .and_then(Value::as_array)
                    .ok_or_else(bad)?;
                if tools
                    .iter()
                    .any(|t| t["name"].as_str().is_none_or(str::is_empty))
                {
                    return Err(bad());
                }
                task_defaults = omp_task_defaults(tools);
                inventory = true;
                continue;
            }
            let expected = if omp {
                omp_rpc_id(id, "prompt.1")
            } else {
                id.to_owned()
            };
            if ack
                || r["id"] != expected
                || r["command"] != "prompt"
                || !has_exact_keys(r, &["id", "type", "command", "success"])
            {
                return Err(bad());
            }
            if r["success"] != true {
                return Err(fail());
            }
            ack = true;
            continue;
        }
        if !inventory || (!omp && !ack) || (ended && !(drain && kind == "session_action_update")) {
            return Err(bad());
        }
        if omp && kind == "tool_execution_update" {
            if let Some((args, job)) = r["toolCallId"].as_str().and_then(|id| async_tasks.get(id)) {
                let a = &r["partialResult"]["details"]["async"];
                if !started
                    || r["toolName"] != "task"
                    || !task_args_match(&r["args"], args, true, "task", task_defaults)
                    || a["type"] != "task"
                    || a["jobId"].as_str() != Some(job.as_str())
                    || !matches!(
                        a["state"].as_str(),
                        Some("running" | "completed" | "failed")
                    )
                {
                    return Err(bad());
                }
                continue;
            }
        }
        if kind == "session_action_update" {
            if omp || !started || !valid_prime_queue(r) {
                return Err(bad());
            }
            if drain && !drain_state.queue(&r["actions"], ended) {
                return Err(bad());
            }
            continue;
        }
        if kind == "rlm_child_update" {
            if omp || !started || !valid_prime_child_update(r) {
                return Err(bad());
            }
            if drain {
                drain_state.child(&r["child"]);
            }
            continue;
        }
        if drain && drain_state.segment_closed && matches!(kind, "turn_start" | "message_start") {
            drain_state.segment_closed = false;
            drain_state.resumed = true;
            drain_state.queue_empty = false;
            history.clear();
        }
        match kind {
            "agent_start" => {
                if started {
                    return Err(bad());
                }
                started = true;
            }
            "turn_start" => {
                if !started
                    || turn
                    || open.is_some()
                    || last_stop.as_ref().is_some_and(|s| s != "toolUse")
                {
                    return Err(bad());
                }
                turn = true;
                assistant = None;
                calls.clear();
                results.clear();
            }
            "message_start" => {
                let m = r.get("message").filter(|m| m.is_object()).ok_or_else(bad)?;
                // Native-only inference of both missing markers; no emitted event or history deletion.
                if drain
                    && started
                    && turn
                    && open.is_none()
                    && assistant
                        .as_ref()
                        .is_some_and(|a| a["stopReason"] == "toolUse")
                    && m["role"] == "assistant"
                    && m["content"].as_array().is_some_and(Vec::is_empty)
                    && !calls.is_empty()
                    && results.len() == calls.len()
                    && calls.values().all(|c| c.2 == 3)
                {
                    last_stop = Some("toolUse".to_owned());
                    turn = false;
                }
                // Observed Prime boundary omission: preserve messages and require all prior tools settled.
                if !omp
                    && started
                    && !turn
                    && open.is_none()
                    && last_stop.as_deref() == Some("toolUse")
                    && m["role"] == "assistant"
                    && m["content"].as_array().is_some_and(Vec::is_empty)
                    && !calls.is_empty()
                    && calls.values().all(|c| c.2 == 3)
                {
                    turn = true;
                    assistant = None;
                    calls.clear();
                    results.clear();
                }
                if !turn || open.is_some() {
                    return Err(bad());
                }
                match m["role"].as_str() {
                    Some("user") if !user && assistant.is_none() => {}
                    Some("custom") if omp && assistant.is_none() && valid_custom(m) => {}
                    Some("assistant") if user && assistant.is_none() => {
                        block_types.clear();
                    }
                    Some("toolResult") => {
                        let call = calls
                            .get(m["toolCallId"].as_str().ok_or_else(bad)?)
                            .ok_or_else(bad)?;
                        if call.2 != 2 || m["toolName"] != call.0 {
                            return Err(bad());
                        }
                    }
                    _ => return Err(bad()),
                }
                open = Some(m.clone());
            }
            "message_update" => {
                if open.as_ref().is_none_or(|m| m["role"] != "assistant")
                    || r.pointer("/message/role") != Some(&json!("assistant"))
                {
                    return Err(bad());
                }
                let e = &r["assistantMessageEvent"];
                let t = e["type"].as_str().ok_or_else(bad)?;
                if ![
                    "text_start",
                    "text_delta",
                    "text_end",
                    "thinking_start",
                    "thinking_delta",
                    "thinking_end",
                    "toolcall_start",
                    "toolcall_delta",
                    "toolcall_end",
                ]
                .contains(&t)
                    || e["contentIndex"].as_u64().is_none()
                    || !r["message"]["content"].is_array()
                    || (t.ends_with("_delta") && !e["delta"].is_string())
                {
                    return Err(bad());
                }
                let index = e["contentIndex"].as_u64().ok_or_else(bad)? as usize;
                let expected = if t.starts_with("toolcall_") {
                    "toolCall"
                } else {
                    t.split('_').next().ok_or_else(bad)?
                };
                if r["message"]["content"]
                    .get(index)
                    .and_then(|b| b["type"].as_str())
                    != Some(expected)
                    || block_types.get(&index).is_some_and(|t| t != expected)
                {
                    return Err(bad());
                }
                block_types.insert(index, expected.into());
            }
            "message_end" => {
                let m = r.get("message").ok_or_else(bad)?;
                let current = open.as_ref().ok_or_else(bad)?;
                if m["role"] != current["role"]
                    || (m["role"] != "custom" && !m["content"].is_array())
                {
                    return Err(bad());
                }
                match m["role"].as_str() {
                    Some("custom") => {
                        if !omp || !valid_custom(m) || m != current {
                            return Err(bad());
                        }
                    }
                    Some("user") => {
                        if m != current {
                            return Err(bad());
                        }
                        user = true;
                    }
                    Some("assistant") => {
                        if block_types.iter().any(|(i, t)| {
                            m["content"].get(*i).and_then(|b| b["type"].as_str())
                                != Some(t.as_str())
                        }) {
                            return Err(bad());
                        }
                        let stop = m["stopReason"].as_str().ok_or_else(bad)?;
                        if !["stop", "toolUse"].contains(&stop) {
                            return Err(fail());
                        }
                        let mut text = String::new();
                        for b in m["content"].as_array().ok_or_else(bad)? {
                            match b["type"].as_str() {
                                Some("text") => text.push_str(b["text"].as_str().ok_or_else(bad)?),
                                Some("thinking") => {
                                    if !b["thinking"].is_string() {
                                        return Err(bad());
                                    }
                                }
                                Some("toolCall") => {
                                    let key = b["id"]
                                        .as_str()
                                        .filter(|s| !s.is_empty())
                                        .ok_or_else(bad)?;
                                    let name = b["name"]
                                        .as_str()
                                        .filter(|s| !s.is_empty())
                                        .ok_or_else(bad)?;
                                    let mut args =
                                        b["arguments"].as_object().ok_or_else(bad)?.clone();
                                    if omp
                                        && b["intent"].is_string()
                                        && args.get("i") == b.get("intent")
                                    {
                                        args.remove("i");
                                    }
                                    if calls
                                        .insert(
                                            key.into(),
                                            (name.into(), Value::Object(args), 0, None, None),
                                        )
                                        .is_some()
                                    {
                                        return Err(bad());
                                    }
                                }
                                _ => return Err(bad()),
                            }
                        }
                        if (stop == "toolUse") == calls.is_empty() {
                            return Err(bad());
                        }
                        texts.push(text);
                        assistant = Some(m.clone());
                    }
                    Some("toolResult") => {
                        let call = calls
                            .get_mut(m["toolCallId"].as_str().ok_or_else(bad)?)
                            .ok_or_else(bad)?;
                        if m != current
                            || call.2 != 2
                            || m["toolName"] != call.0
                            || !m["isError"].is_boolean()
                        {
                            return Err(bad());
                        }
                        if call.3.as_ref() != m.get("content") || call.4 != m["isError"].as_bool() {
                            return Err(bad());
                        }
                        call.2 = 3;
                        results.push(m.clone());
                    }
                    _ => return Err(bad()),
                }
                history.push(m.clone());
                open = None;
            }
            "tool_execution_start" | "tool_execution_update" | "tool_execution_end" => {
                if !turn || open.is_some() {
                    return Err(bad());
                }
                let call = calls
                    .get_mut(r["toolCallId"].as_str().ok_or_else(bad)?)
                    .ok_or_else(bad)?;
                if r["toolName"] != call.0 {
                    return Err(bad());
                }
                if kind == "tool_execution_start" {
                    if call.2 != 0
                        || !task_args_match(&r["args"], &call.1, omp, &call.0, task_defaults)
                    {
                        return Err(bad());
                    }
                    call.2 = 1;
                } else if kind == "tool_execution_update" {
                    if call.2 != 1
                        || !task_args_match(&r["args"], &call.1, omp, &call.0, task_defaults)
                        || !r["partialResult"].is_object()
                    {
                        return Err(bad());
                    }
                } else {
                    if call.2 != 1 || !r["isError"].is_boolean() || !r["result"].is_object() {
                        return Err(bad());
                    }
                    call.3 = r["result"].get("content").cloned();
                    call.4 = r["isError"].as_bool();
                    call.2 = 2;
                    let a = &r["result"]["details"]["async"];
                    if omp
                        && call.0 == "task"
                        && r["isError"] == false
                        && a["type"] == "task"
                        && a["state"] == "running"
                    {
                        if let Some(job) = a["jobId"].as_str().filter(|s| !s.is_empty()) {
                            async_tasks.insert(
                                r["toolCallId"].as_str().ok_or_else(bad)?.to_owned(),
                                (call.1.clone(), job.to_owned()),
                            );
                        }
                    }
                }
            }
            "turn_end" => {
                if !turn
                    || open.is_some()
                    || assistant
                        .as_ref()
                        .is_none_or(|m| !same_message(&r["message"], m, omp))
                    || r["toolResults"] != json!(results)
                    || calls.values().any(|c| c.2 != 3)
                {
                    return Err(bad());
                }
                last_stop = assistant
                    .as_ref()
                    .and_then(|m| m["stopReason"].as_str().map(str::to_owned));
                turn = false;
            }
            "agent_end" => {
                let msgs = r["messages"].as_array().ok_or_else(bad)?;
                if !started || turn || open.is_some() || calls.values().any(|c| c.2 != 3) {
                    return Err(bad());
                }
                if drain {
                    if drain_state.segment_closed || !drain_state.history(msgs, &history) {
                        return Err(bad());
                    }
                    if last_stop.as_deref() == Some("toolUse") {
                        drain_state.segment_closed = true;
                        continue;
                    }
                    if last_stop.as_deref() != Some("stop") {
                        return Err(bad());
                    }
                } else if last_stop.as_deref() != Some("stop")
                    || msgs.len() != history.len()
                    || msgs
                        .iter()
                        .zip(&history)
                        .any(|(a, b)| !same_message(a, b, omp))
                {
                    return Err(bad());
                }
                if omp && r["isTerminal"] != true {
                    return Err(fail());
                }
                ended = true;
            }
            _ => return Err(bad()),
        }
    }
    if terminal
        && (!ended
            || !ack
            || (drain
                && (drain_state.resumed || drain_state.queue_observed)
                && !drain_state.queue_empty))
    {
        return Err(bad());
    }
    Ok(TransportNormalization {
        terminal_event: "agent_end",
        assistant_messages: texts,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn drain_frames() -> Vec<Value> {
        let f: Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/adapters/tool-lifecycle/prime-drain.json"
        ))
        .unwrap();
        let mut r = vec![f["stateResponse"].clone(), f["promptResponse"].clone()];
        r.extend(f["frames"].as_array().unwrap().clone());
        r
    }
    #[test]
    fn prime_drain_requires_complete_correlated_settlement() {
        let run = |r: &[Value], terminal| normalize_mode(r, "fixture-drain", false, terminal, true);
        let r = drain_frames();
        assert!(run(&r, true).is_ok());
        let ends: Vec<_> = r
            .iter()
            .enumerate()
            .filter(|(_, v)| v["type"] == "agent_end")
            .map(|(i, _)| i)
            .collect();
        assert!(run(&r[..=ends[0]], false).is_ok());
        assert!(run(&r[..=ends[0]], true).is_err());
        for path in ["/sessionId", "/isStreaming", "/messageCount"] {
            let mut bad = r.clone();
            *bad[0]["data"].pointer_mut(path).unwrap() = json!("wrong");
            assert!(run(&bad, true).is_err());
        }
        let mut bad = r.clone();
        bad.remove(0);
        assert!(run(&bad, true).is_err());
        for path in [
            "/messages/0/details/target/sessionId",
            "/messages/0/details/from/activeSessionId",
            "/messages/0/details/message",
            "/messages/0/content",
        ] {
            let mut bad = r.clone();
            *bad[ends[1]].pointer_mut(path).unwrap() = json!("wrong");
            assert!(run(&bad, true).is_err());
        }
        let mut bad = r.clone();
        bad.pop();
        assert!(run(&bad, true).is_err());
        let mut stale = r.clone();
        let empty = stale.pop().unwrap();
        stale.insert(ends[0], empty);
        assert!(run(&stale, true).is_err());
        let mut bad = r.clone();
        let pos = bad
            .iter()
            .position(|v| v["type"] == "tool_execution_end")
            .unwrap();
        bad.remove(pos);
        assert!(run(&bad, true).is_err());
        let mut bad = r.clone();
        bad[ends[0]]["messages"][0]["content"] = json!([]);
        assert!(run(&bad, true).is_err());
        let mut bad = r.clone();
        bad[1]["success"] = json!(false);
        assert!(run(&bad, true).is_err());
        let mut bad = r.clone();
        bad.push(json!({"type":"session_action_update","actions":{"queuedCount":0,"steering":[],"followUps":[],"active":{"kind":"turn","phase":"running"}}}));
        assert!(run(&bad, true).is_err());
        let mut bad = r.clone();
        bad.push(json!({"type":"turn_start"}));
        assert!(run(&bad, true).is_err());
    }
    #[test]
    fn prime_missing_both_markers_requires_reported_tools_and_final_settlement() {
        let f: Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/adapters/tool-lifecycle/prime-turn-transition.json"
        ))
        .unwrap();
        let mut r = vec![f["stateResponse"].clone(), f["promptResponse"].clone()];
        r.extend(f["frames"].as_array().unwrap().clone());
        let run = |r: &[Value], terminal| normalize_mode(r, "fixture-drain", false, terminal, true);
        assert!(run(&r, true).is_ok());
        assert!(run(&r[..15], true).is_err());
        assert!(run(&r[..15], false).is_ok());
        assert!(normalize(&r[1..], "fixture-drain", false, true).is_err());
        for index in [7usize, 9, 14] {
            let mut bad = r.clone();
            bad.remove(index + 2);
            assert!(run(&bad, true).is_err());
        }
        let mut bad = r.clone();
        bad[14]["message"]["content"] = json!([{"type":"text","text":"not empty"}]);
        assert!(run(&bad, true).is_err());
        let mut bad = r.clone();
        bad[11]["message"]["toolCallId"] = json!("other");
        assert!(run(&bad, true).is_err());
        let mut bad = r.clone();
        bad.insert(12, bad[11].clone());
        assert!(run(&bad, true).is_err());
        let mut bad = r.clone();
        bad.pop();
        assert!(run(&bad, true).is_err());
    }
    #[test]
    fn prime_usage_projection_is_typed_and_only_history() {
        let usage = json!({"input":1,"output":2,"cacheRead":0,"cacheWrite":0,"totalTokens":3,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}});
        let a = json!({"role":"assistant","content":[],"usage":usage});
        let mut b = a.clone();
        b["usage"]["output"] = json!(8);
        assert!(drain_message_equal(&a, &b));
        for value in [json!(-1), json!("8"), Value::Null] {
            let mut b = b.clone();
            b["usage"]["output"] = value;
            assert!(!drain_message_equal(&a, &b));
            assert!(!drain_message_equal(&b, &b));
        }
        let mut b = b.clone();
        b["usage"]["extra"] = json!(0);
        assert!(!drain_message_equal(&a, &b));
        let mut b = a.clone();
        b["content"] = json!([{"type":"text","text":"changed"}]);
        assert!(!drain_message_equal(&a, &b));
    }
    fn frames(omp: bool) -> Vec<Value> {
        serde_json::from_str(if omp {
            include_str!("../../../../../shared/fixtures/adapters/tool-lifecycle/omp.json")
        } else {
            include_str!("../../../../../shared/fixtures/adapters/tool-lifecycle/prime.json")
        })
        .unwrap()
    }
    #[test]
    fn prime_child_telemetry_is_typed_and_cannot_settle_parent() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/adapters/tool-lifecycle/prime-child-telemetry.json"
        ))
        .unwrap();
        let original = frames(false);
        let at = original
            .iter()
            .position(|r| r["type"] == "tool_execution_start")
            .unwrap()
            + 1;
        let expected = normalize(&original, "fixture-tools", false, true).unwrap();
        for record in fixture["valid"].as_array().unwrap() {
            assert!(valid_prime_child_update(record));
            let mut with_child = original.clone();
            with_child.insert(at, record.clone());
            let actual = normalize(&with_child, "fixture-tools", false, true).unwrap();
            assert_eq!(actual, expected);
            // Child completion cannot replace native parent terminal evidence.
            with_child.pop();
            assert!(normalize(&with_child, "fixture-tools", false, false).is_ok());
            assert!(normalize(&with_child, "fixture-tools", false, true).is_err());
            // Nor can it supply the still-pending parent tool result.
            let mut pending = original[..at].to_vec();
            pending.push(record.clone());
            assert!(normalize(&pending, "fixture-tools", false, false).is_ok());
            assert!(normalize(&pending, "fixture-tools", false, true).is_err());
            for position in [1, original.len()] {
                let mut outside = original.clone();
                outside.insert(position, record.clone());
                assert!(normalize(&outside, "fixture-tools", false, true).is_err());
            }
            let mut omp = frames(true);
            let at = omp
                .iter()
                .position(|r| r["type"] == "tool_execution_start")
                .unwrap()
                + 1;
            omp.insert(at, record.clone());
            assert!(normalize(&omp, "fixture-tools", true, true).is_err());
        }
        for record in fixture["invalid"].as_array().unwrap() {
            assert!(!valid_prime_child_update(record), "{record}");
            let mut invalid = original.clone();
            invalid.insert(at, record.clone());
            assert!(normalize(&invalid, "fixture-tools", false, true).is_err());
        }
    }

    #[test]
    fn prime_queue_telemetry_is_typed_and_cannot_settle_parent() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/adapters/tool-lifecycle/prime-queue-telemetry.json"
        ))
        .unwrap();
        let original = frames(false);
        let at = original
            .iter()
            .position(|r| r["type"] == "tool_execution_start")
            .unwrap()
            + 1;
        let expected = normalize(&original, "fixture-tools", false, true).unwrap();
        for record in fixture["valid"].as_array().unwrap() {
            assert!(valid_prime_queue(record));
            let mut with_child = original.clone();
            with_child.insert(at, record.clone());
            let actual = normalize(&with_child, "fixture-tools", false, true).unwrap();
            assert_eq!(actual, expected);
            // Child completion cannot replace native parent terminal evidence.
            with_child.pop();
            assert!(normalize(&with_child, "fixture-tools", false, false).is_ok());
            assert!(normalize(&with_child, "fixture-tools", false, true).is_err());
            // Nor can it supply the still-pending parent tool result.
            let mut pending = original[..at].to_vec();
            pending.push(record.clone());
            assert!(normalize(&pending, "fixture-tools", false, false).is_ok());
            assert!(normalize(&pending, "fixture-tools", false, true).is_err());
            for position in [1, original.len()] {
                let mut outside = original.clone();
                outside.insert(position, record.clone());
                assert!(normalize(&outside, "fixture-tools", false, true).is_err());
            }
            let mut omp = frames(true);
            let at = omp
                .iter()
                .position(|r| r["type"] == "tool_execution_start")
                .unwrap()
                + 1;
            omp.insert(at, record.clone());
            assert!(normalize(&omp, "fixture-tools", true, true).is_err());
        }
        for record in fixture["invalid"].as_array().unwrap() {
            assert!(!valid_prime_queue(record), "{record}");
            let mut invalid = original.clone();
            invalid.insert(at, record.clone());
            assert!(normalize(&invalid, "fixture-tools", false, true).is_err());
        }
    }

    #[test]
    fn actual_tool_streams_and_corruptions() {
        for omp in [false, true] {
            let frames = frames(omp);
            assert!(normalize(&frames, "fixture-tools", omp, true).is_ok());
            let end = frames
                .iter()
                .position(|r| r["type"] == "agent_end")
                .unwrap();
            assert!(normalize(&frames[..end], "fixture-tools", omp, true).is_err());
            assert!(normalize(&frames[..end], "fixture-tools", omp, false).is_ok());
            let mut bad = frames.clone();
            bad.iter_mut()
                .find(|r| r["type"] == "tool_execution_end")
                .unwrap()["toolCallId"] = json!("wrong");
            assert!(normalize(&bad, "fixture-tools", omp, true).is_err());
        }
    }
}

// Recognize only explicitly advertised task agent defaults, never arbitrary schema defaults.
fn omp_task_defaults(tools: &[Value]) -> (bool, bool) {
    let tasks: Vec<_> = tools.iter().filter(|t| t["name"] == "task").collect();
    if tasks.len() != 1 {
        return (false, false);
    }
    let p = &tasks[0]["parameters"];
    if p["type"] != "object" {
        return (false, false);
    }
    let field = |v: &Value| v["type"] == "string" && v["default"] == "task";
    let items = &p["properties"]["tasks"];
    (
        field(&p["properties"]["agent"]),
        items["type"] == "array"
            && items["items"]["type"] == "object"
            && field(&items["items"]["properties"]["agent"]),
    )
}
fn task_args_match(
    actual: &Value,
    declared: &Value,
    omp: bool,
    name: &str,
    defaults: (bool, bool),
) -> bool {
    if !omp || name != "task" || defaults == (false, false) {
        return native_args_match(actual, declared, omp);
    }
    let mut d = declared.clone();
    fn apply(a: &Value, b: &mut Value) {
        if a.is_object() && a["agent"] == "task" {
            if let Some(o) = b.as_object_mut() {
                if !o.contains_key("agent") {
                    o.insert("agent".into(), json!("task"));
                }
            }
        }
    }
    if defaults.0 {
        apply(actual, &mut d);
    }
    if defaults.1 {
        if let (Some(a), Some(b)) = (
            actual.get("tasks").and_then(Value::as_array),
            d.get_mut("tasks").and_then(Value::as_array_mut),
        ) {
            if a.len() == b.len() {
                for (x, y) in a.iter().zip(b) {
                    apply(x, y);
                }
            }
        }
    }
    native_args_match(actual, &d, true)
}

// OMP may omit schema-optional null fields before native execution.
fn native_args_match(actual: &Value, declared: &Value, omp: bool) -> bool {
    if !omp {
        return actual == declared;
    }
    match (actual, declared) {
        (Value::Object(a), Value::Object(d)) => {
            a.iter().all(|(k, v)| {
                d.get(k)
                    .is_some_and(|other| native_args_match(v, other, true))
            }) && d
                .iter()
                .all(|(k, v)| a.contains_key(k) || v.is_null() || v.as_str() == Some("null"))
        }
        (Value::Array(a), Value::Array(d)) => {
            a.len() == d.len() && a.iter().zip(d).all(|(v, o)| native_args_match(v, o, true))
        }
        _ => actual == declared,
    }
}
#[test]
fn omp_optional_null_omission_does_not_permit_changed_values() {
    let declared = serde_json::json!({"op":"init","optional":null});
    assert!(native_args_match(
        &serde_json::json!({"op":"init"}),
        &declared,
        true
    ));
    assert!(!native_args_match(
        &serde_json::json!({"op":"erase"}),
        &declared,
        true
    ));
    assert!(!native_args_match(
        &serde_json::json!({"op":"init"}),
        &declared,
        false
    ));
}

#[test]
fn omp_superseded_terminal_projection_preserves_tool_identity() {
    let original = serde_json::json!({"role":"toolResult","toolCallId":"t1","toolName":"read","isError":true,"content":[{"type":"text","text":"not found"}]});
    let mut summary = original.clone();
    summary["prunedAt"] = serde_json::json!(12);
    summary["content"] =
        serde_json::json!([{"type":"text","text":"[Superseded by a newer read of this file]"}]);
    assert!(same_message(&summary, &original, true));
    assert!(!same_message(&summary, &original, false));
    summary["toolCallId"] = serde_json::json!("invented");
    assert!(!same_message(&summary, &original, true));
}

#[test]
fn prime_implicit_boundary_requires_settled_tools_and_native_terminal() {
    let frames: Vec<Value> = serde_json::from_str(include_str!(
        "../../../../../shared/fixtures/adapters/tool-lifecycle/prime-implicit-turn.json"
    ))
    .unwrap();
    assert!(normalize(&frames, "fixture-tools", false, true).is_ok());
    assert!(normalize(&frames[..frames.len() - 1], "fixture-tools", false, false).is_ok());
    assert!(normalize(&frames[..frames.len() - 1], "fixture-tools", false, true).is_err());
    let boundary = (1..frames.len())
        .find(|&i| frames[i]["type"] == "message_start" && frames[i - 1]["type"] == "turn_end")
        .unwrap();
    for message in [
        json!({"role":"user","content":[]}),
        json!({"role":"assistant","content":[{"type":"text","text":"unexpected"}]}),
    ] {
        let mut bad = frames.clone();
        bad[boundary]["message"] = message;
        let error = normalize(&bad, "fixture-tools", false, true).unwrap_err();
        assert_eq!(
            serde_json::to_value(error).unwrap()["details"]["adapterDiagnostic"]["phase"],
            "tool-await-next-turn"
        );
    }
    let mut pending = frames.clone();
    pending.remove(boundary - 1);
    assert!(normalize(&pending, "fixture-tools", false, true).is_err());
    let mut omp: Vec<Value> = serde_json::from_str(include_str!(
        "../../../../../shared/fixtures/adapters/tool-lifecycle/omp.json"
    ))
    .unwrap();
    let boundary = omp
        .iter()
        .enumerate()
        .filter(|(_, r)| r["type"] == "turn_start")
        .nth(1)
        .unwrap()
        .0;
    omp.remove(boundary);
    assert!(normalize(&omp, "fixture-tools", true, true).is_err());
}

#[test]
#[ignore = "Explicit provider-free replay path is supplied by developer"]
fn prime_recorded_prefix_replay() {
    let text =
        std::fs::read_to_string(std::env::var("PRIME_REPLAY_PATH").expect("replay path")).unwrap();
    let mut frames: Vec<Value> = text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let id = frames[0]["id"].as_str().unwrap().to_owned();
    assert!(normalize(&frames, &id, false, false).is_ok());
    assert!(normalize(&frames, &id, false, true).is_err());
    let mut message = frames.last().unwrap()["message"].clone();
    message["content"] = json!([{"type":"text","text":"synthetic continuation"}]);
    message["stopReason"] = json!("stop");
    let mut history: Vec<Value> = frames
        .iter()
        .filter(|r| r["type"] == "message_end")
        .map(|r| r["message"].clone())
        .collect();
    history.push(message.clone());
    frames.push(json!({"type":"message_end","message":message}));
    frames.push(json!({"type":"turn_end","message":message,"toolResults":[]}));
    assert!(normalize(&frames, &id, false, true).is_err());
    frames.push(json!({"type":"agent_end","messages":history}));
    assert!(normalize(&frames, &id, false, true).is_ok());
}

#[cfg(test)]
mod late_task_tests {
    use super::*;
    #[test]
    fn late_async_progress_preserves_terminal_and_correlation() {
        let f: Vec<Value> = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/adapters/tool-lifecycle/omp-late-progress.json"
        ))
        .unwrap();
        assert!(normalize(&f, "fixture-tools", true, true).is_ok());
        assert!(normalize(&f[..f.len() - 1], "fixture-tools", true, false).is_ok());
        assert!(normalize(&f[..f.len() - 1], "fixture-tools", true, true).is_err());
        let at = f
            .iter()
            .enumerate()
            .position(|(i, x)| {
                i > 0
                    && x["type"] == "tool_execution_update"
                    && f[i - 1]["type"] == "tool_execution_end"
            })
            .unwrap();
        for n in 0..6 {
            let mut bad = f.clone();
            match n {
                0 => bad[at]["partialResult"]["details"]["async"]["jobId"] = json!("other"),
                1 => bad[at]["args"] = json!({"different":true}),
                2 => {
                    bad[at - 1]["result"]["details"]
                        .as_object_mut()
                        .unwrap()
                        .remove("async");
                }
                3 => bad[at]["toolName"] = json!("read"),
                4 => bad.insert(at, bad[at - 1].clone()),
                _ => bad.push(bad[at].clone()),
            }
            assert!(normalize(&bad, "fixture-tools", true, true).is_err());
        }
    }
}

#[cfg(test)]
mod custom_tests {
    use super::*;
    fn fixture() -> Vec<Value> {
        serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/adapters/tool-lifecycle/omp-custom.json"
        ))
        .unwrap()
    }
    #[test]
    fn custom_pair_history_and_terminal() {
        let f = fixture();
        assert!(normalize(&f, "fixture-tools", true, true).is_ok());
        assert!(normalize(&f[..f.len() - 1], "fixture-tools", true, true).is_err());
    }
    #[test]
    fn custom_typed_content() {
        for c in [
            json!("opaque"),
            json!([{"type":"text","text":"opaque","textSignature":"sig"}]),
            json!([{"type":"image","data":"AA==","mimeType":"image/png","detail":"original","providerFile":{"provider":"openai","id":"x"},"url":"https://example.invalid/x"}]),
        ] {
            let mut f = fixture();
            f[9]["message"]["content"] = c.clone();
            f[10]["message"]["content"] = c.clone();
            f[14]["messages"][1]["content"] = c;
            assert!(normalize(&f, "fixture-tools", true, true).is_ok());
        }
    }
    #[test]
    fn custom_rejects_invalid_pairs() {
        for variant in 0..12 {
            let mut f = fixture();
            match variant {
                0 => f[10]["message"]["content"] = json!("changed"),
                1 => f[14]["messages"][1]["display"] = json!(true),
                2 => f[9]["message"]["extra"] = json!(true),
                3 => f[9]["message"]["content"] = json!([{"type":"toolCall","id":"x"}]),
                4 => f[9]["message"]["attribution"] = json!("system"),
                5 => f[9]["message"]["timestamp"] = json!(-1),
                6 => {
                    f.remove(9);
                }
                7 => f.insert(10, f[9].clone()),
                8 => f.insert(11, f[10].clone()),
                9 => f.insert(6, f[9].clone()),
                10 => f.push(f[9].clone()),
                _ => {
                    f.drain(11..14);
                }
            }
            assert!(
                normalize(&f, "fixture-tools", true, true).is_err(),
                "variant {variant}"
            );
        }
    }
}
#[test]
fn omp_task_default_shared_cases() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../shared/fixtures/adapters/tool-lifecycle/omp-task-defaults.json");
    let cases: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    for c in cases.as_array().unwrap() {
        let result = normalize(
            c["records"].as_array().unwrap(),
            "fixture-tools",
            true,
            true,
        );
        assert_eq!(
            result.is_ok(),
            c["expected"] == 0,
            "{}: {:?}",
            c["name"],
            result.err()
        );
    }
    assert!(!task_args_match(
        &json!({"agent":"task"}),
        &json!({}),
        false,
        "task",
        (true, true)
    ));
}
