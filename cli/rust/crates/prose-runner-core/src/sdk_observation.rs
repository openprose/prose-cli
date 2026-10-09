//! Closed, bounded public projections of SDK observations. These are not bills.
use serde_json::{Map, Value, json};

const SAFE_INTEGER: u64 = 9_007_199_254_740_991;
const COUNTERS: [&str; 6] = [
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_tokens_details.cached_tokens",
    "input_tokens_details.cache_write_tokens",
    "output_tokens_details.reasoning_tokens",
];
const TIERS: [&str; 7] = [
    "auto",
    "default",
    "flex",
    "scale",
    "priority",
    "fast",
    "ultrafast",
];
fn count(value: &Value) -> Option<u64> {
    if let Some(n) = value.as_u64() {
        return (n <= SAFE_INTEGER).then_some(n);
    }
    let n = value.as_f64()?;
    if !n.is_finite() || n < 0.0 || n.fract() != 0.0 || n > 9_007_199_254_740_991.0 {
        return None;
    }
    if n == 0.0 {
        return Some(0);
    }
    // JSON Schema integers include 3.0 and 3e0. Normalize their representation
    // without an unchecked numeric cast, matching the independent JS product.
    format!("{n:.0}").parse().ok()
}
fn public_model(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._/-".contains(&c))
}
fn tokens(value: &Value) -> Option<Value> {
    let source = value.as_object()?;
    let mut output = Map::new();
    for key in COUNTERS {
        if let Some(value) = source.get(key).and_then(count) {
            output.insert(key.to_owned(), json!(value));
        }
    }
    Some(Value::Object(output))
}
fn usage(record: &Value) -> Option<Value> {
    let source = record.get("usageObservation")?;
    if source.get("source")? != "sdk_completed_response_raw_usage"
        || source.get("aggregationScope")? != "unique_completed_responses_in_parent_and_children"
        || !source.get("outstandingProviderRequestCount")?.is_null()
        || source.get("totalRunUsageKnown")? != false
    {
        return None;
    }
    let mut output = json!({"source":"sdk_completed_response_raw_usage", "aggregationScope":"unique_completed_responses_in_parent_and_children", "outstandingProviderRequestCount":null, "totalRunUsageKnown":false});
    for key in [
        "startedCallCount",
        "completedResponseCount",
        "duplicateResponseCallbackCount",
        "outstandingCallCount",
    ] {
        output[key] = json!(count(source.get(key)?)?);
    }
    for key in ["observedTokenTotals", "fieldResponseCounts"] {
        output[key] = tokens(source.get(key)?)?;
    }
    Some(output)
}
fn model(record: &Value, requested: Option<&str>) -> Option<Value> {
    let requested = requested.filter(|v| public_model(v))?;
    let source = record.get("modelIdentity")?.as_object()?;
    let observed = source.get("observed")?.as_array()?;
    let mut identities: std::collections::BTreeSet<&str> = std::collections::BTreeSet::new();
    // Work is bounded by the already admitted native record byte limit.
    for value in observed {
        if let Some(value) = value.as_str().filter(|v| public_model(v)) {
            identities.insert(value);
        }
    }
    let tier = source.get("serviceTier")?.as_object()?;
    if tier.get("requested")? != "default" {
        return None;
    }
    let observed_tiers: std::collections::BTreeSet<&str> = tier
        .get("observed")?
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .filter(|v| TIERS.contains(v))
        .collect();
    Some(
        json!({"requested":requested, "observed":identities.into_iter().take(128).collect::<Vec<_>>(), "serviceTier":{"requested":"default", "observed":observed_tiers}}),
    )
}
/// Update independent valid groups; absent or invalid groups do not erase prior observations.
pub(crate) fn update(output: &mut Value, record: &Value, requested: Option<&str>) {
    if let Some(value) = usage(record) {
        output["usageObservation"] = value;
    }
    if let Some(value) = model(record, requested) {
        output["modelIdentity"] = value;
    }
}
pub(crate) fn from_records(records: &[Value], requested: Option<&str>) -> Value {
    let mut output = json!({});
    for record in records {
        update(&mut output, record, requested);
    }
    output
}
pub(crate) fn apply(output: &mut Value, observations: &Value) {
    for key in ["usageObservation", "modelIdentity"] {
        if let Some(value) = observations.get(key) {
            output[key] = value.clone();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn observations_are_closed_and_invocation_owned() {
        let mut record = json!({"usageObservation":{"source":"sdk_completed_response_raw_usage", "aggregationScope":"unique_completed_responses_in_parent_and_children", "startedCallCount":4,"completedResponseCount":3,"duplicateResponseCallbackCount":0,"outstandingCallCount":1,"outstandingProviderRequestCount":null,"totalRunUsageKnown":false,"observedTokenTotals":{"input_tokens":9,"unknown":77,"output_tokens":-1},"fieldResponseCounts":{"input_tokens":3}},"modelIdentity":{"requested":"untrusted","observed":["gpt-6.1-sol","gpt-6.1-sol","secret\ntext","é"],"serviceTier":{"requested":"default","observed":["default","unknown"]}}});
        let value = from_records(&[record.clone()], Some("gpt-6.1-sol"));
        assert_eq!(value["modelIdentity"]["requested"], "gpt-6.1-sol");
        assert_eq!(value["modelIdentity"]["observed"], json!(["gpt-6.1-sol"]));
        assert_eq!(
            value["usageObservation"]["observedTokenTotals"],
            json!({"input_tokens":9})
        );
        record["usageObservation"]["startedCallCount"] = json!(4.0);
        assert_eq!(
            from_records(&[record.clone()], Some("gpt-6.1-sol"))["usageObservation"]["startedCallCount"],
            json!(4)
        );
        for raw in ["-0.0", "0e0", "3.0"] {
            let numeric: Value = serde_json::from_str(raw).unwrap();
            record["usageObservation"]["startedCallCount"] = numeric;
            assert!(
                from_records(&[record.clone()], Some("gpt-6.1-sol"))
                    .get("usageObservation")
                    .is_some()
            );
        }
        record["usageObservation"]["startedCallCount"] = json!(-1);
        assert!(
            from_records(&[record], Some("gpt-6.1-sol"))
                .get("usageObservation")
                .is_none()
        );
    }
}
