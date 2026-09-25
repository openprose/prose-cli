use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
/// Native cleanup correlation only: no task text or program interpretation.
pub(super) fn has_fresh_result(records: &[Value]) -> bool {
    let Some(index) = records.iter().rposition(|r| r["type"] == "result") else {
        return false;
    };
    let candidate = &records[index];
    let session = &records[0]["session_id"];
    if &candidate["session_id"] != session
        || candidate["subtype"] != "success"
        || candidate["is_error"] != false
    {
        return false;
    }
    if index + 1 == records.len() {
        return true;
    }
    let mut calls = BTreeSet::new();
    let mut known = BTreeMap::new();
    for record in &records[..index] {
        if let Some(blocks) = record["message"]["content"].as_array() {
            for block in blocks {
                if block["type"] == "tool_use" && block["name"] == "Bash" {
                    if let Some(id) = block["id"].as_str() {
                        calls.insert(id);
                    }
                }
            }
        }
        if record["type"] != "system" || &record["session_id"] != session {
            continue;
        }
        if record["subtype"] == "task_started"
            && record["task_type"] == "local_bash"
            && record["is_backgrounded"] == true
        {
            if let (Some(task), Some(call)) =
                (record["task_id"].as_str(), record["tool_use_id"].as_str())
            {
                if calls.contains(call) {
                    known.insert(task, call);
                }
            }
        }
        if record["subtype"] == "task_notification"
            && matches!(
                record["status"].as_str(),
                Some("completed" | "failed" | "stopped")
            )
        {
            if let Some(task) = record["task_id"].as_str() {
                known.remove(task);
            }
        }
    }
    let mut pending = BTreeSet::new();
    let mut closed = BTreeSet::new();
    let mut inventory = false;
    for record in &records[index + 1..] {
        if record["type"] != "system"
            || &record["session_id"] != session
            || record["uuid"].as_str().is_none_or(str::is_empty)
        {
            return false;
        }
        match record["subtype"].as_str() {
            Some("background_tasks_changed") => {
                if !record["tasks"]
                    .as_array()
                    .is_some_and(std::vec::Vec::is_empty)
                {
                    return false;
                }
                inventory = true;
            }
            Some("task_updated") => {
                let Some(task) = record["task_id"].as_str() else {
                    return false;
                };
                let Some(patch) = record["patch"].as_object() else {
                    return false;
                };
                if !inventory
                    || !known.contains_key(task)
                    || pending.contains(task)
                    || closed.contains(task)
                    || patch.len() != 2
                    || patch.get("status") != Some(&Value::from("killed"))
                    || patch
                        .get("end_time")
                        .and_then(Value::as_u64)
                        .is_none_or(|n| n > 9_007_199_254_740_991)
                {
                    return false;
                }
                pending.insert(task);
            }
            Some("task_notification") => {
                let Some(task) = record["task_id"].as_str() else {
                    return false;
                };
                if !pending.contains(task)
                    || record["status"] != "stopped"
                    || record["tool_use_id"].as_str() != known.get(task).copied()
                {
                    return false;
                }
                pending.remove(task);
                closed.insert(task);
            }
            _ => return false,
        }
    }
    pending.is_empty() && !closed.is_empty()
}
