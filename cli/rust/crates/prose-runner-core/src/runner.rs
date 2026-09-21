use crate::config::{ConfigSourceKind, EffectiveConfig};
use crate::error::{
    ErrorCode, RunnerError, human_runner_command, human_safe_multiline, human_safe_scalar,
};
use crate::image::{RuntimeImage, sha256_hex};
use crate::installed_adapters;
use crate::invocation::{Action, GlobalFlags, OutputMode, ParsedInvocation, RunnerCommand};
use crate::output::{CommandOutcome, Payload, error_outcome};
use crate::prime_owned_service::{
    SettlementPolicy, prepare_prime_recovery, recover_prime_owned_service,
    settle_prime_owned_service, with_recovery_detail,
};
use crate::runtime::{Clock, IdSource};
use prose_process_supervisor::{
    CancellationToken, ContainmentClaim, EnvironmentPolicy, FailureKind, ProcessOutcome,
    RecordObserver, SupervisorFailure, VersionProbe, VersionProbeOutput, probe_command,
    probe_version, supervise, supervise_observed,
};
#[cfg(feature = "test-seams")]
use prose_process_supervisor::{
    JsonlProtocol, PrivatePromptFiles, ProcessSpec, Sensitivity, StreamLimits,
};
use serde::Serialize;
use serde_json::{Value, json};
use std::env;
use std::ffi::OsString;
use std::fmt::Write as _;
use std::io::Write as IoWrite;
use std::path::{Path, PathBuf};
use std::time::Duration;

#[cfg(all(feature = "test-seams", not(debug_assertions)))]
compile_error!("test-seams cannot be enabled for a release build");

pub const RUNNER_NAME: &str = "rust";
pub const RUNNER_VERSION: &str = env!("OPENPROSE_COMPILED_BUILD_VERSION");
pub const RUNNER_COMMIT: &str = match option_env!("OPENPROSE_BUILD_COMMIT") {
    Some(commit) => commit,
    None => "development",
};
pub const BUILD_PROFILE: &str = if cfg!(debug_assertions) {
    "development"
} else {
    "release"
};
pub const TEST_SEAMS_ENABLED: bool = cfg!(feature = "test-seams");

const DETERMINISTIC_MOCK_DESCRIPTOR: &str =
    include_str!("../../../../shared/fixtures/transport/deterministic-mock-adapter.json");
#[cfg(feature = "test-seams")]
const FAKE_PROCESS_DESCRIPTOR: &str =
    include_str!("../../../../shared/fixtures/transport/mock-adapter.json");
#[cfg(feature = "test-seams")]
const CONFORMANCE_HARNESS: &str = "OPENPROSE_CONFORMANCE_FAKE_HARNESS";
#[cfg(feature = "test-seams")]
const CONFORMANCE_SCENARIO: &str = "OPENPROSE_CONFORMANCE_FAKE_SCENARIO";
#[cfg(feature = "test-seams")]
const CONFORMANCE_OBSERVATION: &str = "OPENPROSE_CONFORMANCE_FAKE_OBSERVATION";
#[cfg(feature = "test-seams")]
const CONFORMANCE_DESCENDANTS: &str = "OPENPROSE_CONFORMANCE_DESCENDANT_IDENTITIES";
#[cfg(feature = "test-seams")]
const CONFORMANCE_DELAY_MS: &str = "OPENPROSE_CONFORMANCE_FAKE_DELAY_MS";
#[cfg(feature = "test-seams")]
const CONFORMANCE_CANCEL_MS: &str = "OPENPROSE_CONFORMANCE_CANCEL_AFTER_MS";
#[cfg(feature = "test-seams")]
const CONFORMANCE_ADAPTER_MODE: &str = "OPENPROSE_CONFORMANCE_ADAPTER_MODE";
#[cfg(feature = "test-seams")]
const CONFORMANCE_ADAPTER_PROBE: &str = "OPENPROSE_CONFORMANCE_ADAPTER_PROBE";
#[cfg(feature = "test-seams")]
const CONFORMANCE_ADAPTER_OBSERVATION: &str = "OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION";
#[cfg(feature = "test-seams")]
const CONFORMANCE_ADAPTER_CREDENTIAL_GROUP: &str = "OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP";
#[cfg(feature = "test-seams")]
const CONFORMANCE_ADAPTER_MODE_VALUE: &str = "provider-free-v1";
#[cfg(feature = "test-seams")]
const CONFORMANCE_ADAPTER_CLEANUP_FAILURE: &str = "OPENPROSE_CONFORMANCE_ADAPTER_CLEANUP_FAILURE";

pub const HELP: &str = include_str!("../../../../conformance/cases/fixtures/runner-help.txt");

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ConfigReport<'a> {
    schema: &'static str,
    cwd: crate::config::Sourced<String>,
    project_config_path: Option<String>,
    user_config_path: String,
    values: ConfigValuesReport<'a>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ConfigValuesReport<'a> {
    harness: &'a crate::config::Sourced<String>,
    transport: &'a crate::config::Sourced<String>,
    model: &'a crate::config::Sourced<Option<String>>,
    timeout: &'a crate::config::Sourced<String>,
    output: &'a crate::config::Sourced<OutputMode>,
    color: &'a crate::config::Sourced<bool>,
    verbose: &'a crate::config::Sourced<bool>,
    auth_profile: &'a crate::config::Sourced<Option<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    output_contract: Option<&'a crate::config::Sourced<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    permission_mode: Option<&'a crate::config::Sourced<Option<String>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_profile: Option<&'a crate::config::Sourced<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_max_turns: Option<&'a crate::config::Sourced<Option<String>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_timeout: Option<&'a crate::config::Sourced<Option<String>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_tool_timeout: Option<&'a crate::config::Sourced<Option<String>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_output_bytes: Option<&'a crate::config::Sourced<Option<String>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_add_dirs: Option<&'a crate::config::Sourced<Vec<String>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_allow_tools: Option<&'a crate::config::Sourced<Vec<String>>>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct HarnessStatus {
    id: &'static str,
    runtime: &'static str,
    availability: String,
    detected_version: Option<String>,
    transports: &'static [&'static str],
    auth_category: &'static str,
    billing_owner: &'static str,
    strict_wrapper_conformant: bool,
    test_only: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    runtime_prerequisites: Vec<installed_adapters::RuntimePrerequisiteObservation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    admission_block: Option<String>,
}

pub fn execute(
    parsed: &ParsedInvocation,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    clock: &dyn Clock,
    ids: &dyn IdSource,
) -> CommandOutcome {
    let cancellation = CancellationToken::default();
    execute_with_cancellation(parsed, config, image, clock, ids, &cancellation)
}

pub fn execute_with_cancellation(
    parsed: &ParsedInvocation,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
) -> CommandOutcome {
    execute_inner(parsed, config, image, clock, ids, cancellation, None)
}

/// Executes while incrementally writing admitted human assistant output.
///
/// JSON and JSONL modes deliberately ignore the sink and retain their exact
/// atomic stdout contracts. Human mode still buffers the possible terminal
/// carrier and validates the complete lifecycle before rendering it.
pub fn execute_with_cancellation_and_human_stream(
    parsed: &ParsedInvocation,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
    human_stream: &mut dyn IoWrite,
) -> CommandOutcome {
    execute_inner(
        parsed,
        config,
        image,
        clock,
        ids,
        cancellation,
        Some(human_stream),
    )
}

fn execute_inner(
    parsed: &ParsedInvocation,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
    human_stream: Option<&mut dyn IoWrite>,
) -> CommandOutcome {
    let mode = action_output_mode(parsed, config);
    if let Action::Runner { command, .. } = &parsed.action {
        if crate::service_account::is_service_command(command) {
            return crate::SystemContext::capture().and_then(|system| crate::service_account::execute_user_command(command, &parsed.globals, &system, mode, cancellation))
                .unwrap_or_else(|error| error_outcome(error, mode, clock, ids));
        }
    }
    let mut outcome = match &parsed.action {
        Action::Help => CommandOutcome::human(HELP, "", 0),
        Action::Version => CommandOutcome::human(format!("prose {RUNNER_VERSION} (rust)\n"), "", 0),
        Action::Runner { command, .. } => execute_runner_command(
            command.clone(),
            &parsed.globals,
            config,
            image,
            mode,
            clock,
            ids,
        ),
        Action::Forward { argv } => execute_forward(
            argv,
            &parsed.globals,
            config,
            image,
            mode,
            clock,
            ids,
            cancellation,
            human_stream,
        ),
    };
    if mode == OutputMode::Human
        && config.verbose.value
        && matches!(&parsed.action, Action::Forward { .. })
    {
        let transport = verbose_transport(config);
        let execution = if parsed.globals.dry_run {
            "dry-run"
        } else {
            "run"
        };
        let status = if outcome.exit_code == 0 {
            "success"
        } else {
            "failed"
        };
        if let Payload::Human { stderr, .. } = &mut outcome.payload {
            let existing = std::mem::take(stderr);
            *stderr = format!(
                "[openprose:verbose] phase=selection harness={} transport={} execution={execution}\n",
                human_safe_scalar(&config.harness.value),
                human_safe_scalar(transport),
            );
            stderr.push_str(&existing);
            if !existing.is_empty() && !existing.ends_with('\n') {
                stderr.push('\n');
            }
            let _ = writeln!(
                stderr,
                "[openprose:verbose] phase=settlement status={status} exitCode={}",
                outcome.exit_code
            );
        }
    }
    outcome
}

fn verbose_transport(config: &EffectiveConfig) -> &str {
    if config.transport.value != "auto" {
        return &config.transport.value;
    }
    match config.harness.value.as_str() {
        "openprose" => "hosted",
        "mock" => "deterministic",
        harness => installed_adapters::for_harness(harness).map_or(
            "unavailable",
            installed_adapters::InstalledAdapter::transport,
        ),
    }
}

#[must_use]
pub fn action_output_mode(parsed: &ParsedInvocation, config: &EffectiveConfig) -> OutputMode {
    match parsed.action {
        Action::Runner { json: true, .. } => OutputMode::Json,
        _ => config.output.value,
    }
}

fn validate_harness_selection(harness: &str, flags: &GlobalFlags) -> Result<(), RunnerError> {
    if harness == "openprose" {
        if flags.model.is_some() || flags.auth_profile.is_some() {
            return Err(RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail(
                "reason",
                "OpenProse hosted selection does not accept an external model or auth profile; omit both options to clear any stale external route.",
            ));
        }
        return Ok(());
    }

    let Some(adapter) = installed_adapters::for_harness(harness) else {
        return Err(RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail(
            "reason",
            format!(
                "unsupported default harness {harness:?}; expected openprose, prime, omp, codex, or claude"
            ),
        ));
    };

    if matches!(
        adapter,
        installed_adapters::InstalledAdapter::PrimeRpc
            | installed_adapters::InstalledAdapter::OmpRpc
    ) && (flags.model.is_none() || flags.auth_profile.is_none())
    {
        let mut error = RunnerError::invocation(
            "Prime and OMP selection requires explicit CLI --model and --auth-profile options; inherited configuration does not select a credential route.",
        )
            .with_detail("adapterId", adapter.id())
            .with_detail("requiredOptions", ["--model", "--auth-profile"])
            .with_detail("supportedAuthProfiles", adapter.auth_profiles());
        error.action = format!(
            "Invoke the `cli harness use {harness}` runner operation with both the `--model` and `--auth-profile` options, then retry."
        );
        return Err(error);
    }

    if let Some(profile) = flags.auth_profile.as_deref() {
        if !adapter.auth_profiles().contains(&profile) {
            return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
                .with_detail("adapterId", adapter.id())
                .with_detail(
                    "reason",
                    format!("Unknown auth_profile for {}: {profile}.", adapter.id()),
                )
                .with_detail("supportedAuthProfiles", adapter.auth_profiles()));
        }
    }

    if matches!(
        adapter,
        installed_adapters::InstalledAdapter::PrimeRpc
            | installed_adapters::InstalledAdapter::OmpRpc
    ) && flags
        .model
        .as_deref()
        .is_some_and(|model| !is_fully_qualified_provider_model(model))
    {
        return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
            .with_detail("adapterId", adapter.id())
            .with_detail(
                "reason",
                "Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments.",
            ));
    }

    Ok(())
}

fn validate_saved_harness_will_be_active(
    harness: &str,
    config: &EffectiveConfig,
) -> Result<(), RunnerError> {
    let source = match config.harness.source.kind {
        ConfigSourceKind::ProjectFile => "project-config",
        ConfigSourceKind::Environment => "environment",
        _ => return Ok(()),
    };
    if config.harness.value == harness {
        return Ok(());
    }
    Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
        .with_detail(
            "reason",
            format!(
                "Saved default {harness} would not be active because the higher-precedence {source} harness selects {}; remove that override or select the same harness.",
                config.harness.value
            ),
        )
        .with_detail("selectedHarness", harness)
        .with_detail("effectiveHarness", config.harness.value.clone())
        .with_detail("effectiveHarnessSource", source))
}

fn human_readiness_label(
    mechanically_ready: bool,
    auth_readiness: &str,
    blocked_label: &'static str,
) -> &'static str {
    if !mechanically_ready {
        return blocked_label;
    }
    if auth_readiness == "unknown" {
        "mechanically ready; authentication unverified"
    } else {
        "ready"
    }
}

fn execute_runner_command(
    command: RunnerCommand,
    flags: &GlobalFlags,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
) -> CommandOutcome {
    match command {
        RunnerCommand::ConfigExplain => {
            let report = config_report(config);
            if mode == OutputMode::Human {
                CommandOutcome::human(render_config_human(config), "", 0)
            } else {
                CommandOutcome::json(report, 0)
            }
        }
        RunnerCommand::Doctor => {
            if let Err(error) = validate_selected_transport(config) {
                return error_outcome(error, mode, clock, ids);
            }
            let mut statuses = match harness_statuses(image, config) {
                Ok(statuses) => statuses,
                Err(error) => return error_outcome(error, mode, clock, ids),
            };
            let (
                selected_transport,
                adapter_id,
                prompt_placement,
                isolation,
                auth_readiness,
                problem,
            ) = doctor_adapter_facts(config, image, &mut statuses);
            if let Some(error) = problem
                .as_ref()
                .filter(|error| error.code == ErrorCode::ProcessCleanupFailed)
            {
                return error_outcome(error.clone(), mode, clock, ids);
            }
            if problem
                .as_ref()
                .is_some_and(|error| error.code == ErrorCode::HarnessNeedsAuth)
            {
                if let Some(status) = statuses
                    .iter_mut()
                    .find(|status| status.id == config.harness.value)
                {
                    "needs-auth".clone_into(&mut status.availability);
                }
            }
            let selected_status = statuses
                .iter()
                .find(|status| status.id == config.harness.value);
            let ready = problem.is_none();
            let exit_code = problem.as_ref().map_or(0, |error| error.exit_code);
            let mut report = json!({
                "schema": "openprose.doctor-report/1",
                "runner": {"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
                "build": {"profile":BUILD_PROFILE,"testSeamsEnabled":TEST_SEAMS_ENABLED},
                "ready": ready,
                "selectedHarness": config.harness.value,
                "selectedHarnessVersion": selected_status.and_then(|status| status.detected_version.as_deref()),
                "selectedTransport": selected_transport,
                "selectedAdapterId": adapter_id,
                "promptPlacement": prompt_placement,
                "isolation": isolation,
                "authCategory": selected_status.map_or("harness-managed", |status| status.auth_category),
                "selectedAuthReadiness": auth_readiness,
                "billingOwner": selected_status.map_or("user-provider", |status| status.billing_owner),
                "problems": problem.iter().collect::<Vec<_>>(),
                "cwd": config.cwd.display().to_string(),
                "image": {
                    "formatVersion": image.manifest.image_format_version,
                    "version": image.manifest.image_version,
                    "sha256": image.aggregate_sha256(),
                    "releaseEligible": image.manifest.release_eligible
                },
                "configuration": config_report(config),
                "harnesses": statuses
            });
            if crate::kernel_startup::PUBLISHED_KERNEL_STARTUP && !cfg!(test) {
                report["imageSource"] = json!("published-on-run");
            }
            if mode == OutputMode::Human {
                let readiness = human_readiness_label(ready, auth_readiness, "not ready");
                let mut output = format!(
                    "Selected harness: {} ({readiness})\nTransport: {}\nAuth readiness: {}\nBilling owner: {}\nImage: {} ({})\n",
                    human_safe_scalar(&config.harness.value),
                    human_safe_scalar(&selected_transport),
                    human_safe_scalar(auth_readiness),
                    human_safe_scalar(billing_owner(&config.harness.value)),
                    human_safe_scalar(&image.manifest.image_version),
                    human_safe_scalar(image.aggregate_sha256())
                );
                if let Some(problem) = problem.as_ref() {
                    let _ = writeln!(
                        output,
                        "Problem: {} — {}",
                        problem.code,
                        human_safe_scalar(&problem.message)
                    );
                    if let Some(reason) = problem
                        .details
                        .as_ref()
                        .and_then(|details| details.get("reason"))
                        .and_then(Value::as_str)
                    {
                        let _ = writeln!(output, "Detail: {}", human_safe_scalar(reason));
                    }
                    for detail in problem.human_version_repair_details() {
                        let _ = writeln!(output, "{detail}");
                    }
                    let _ = writeln!(output, "Action: {}", problem.human_action());
                }
                CommandOutcome::human(output, "", exit_code)
            } else {
                CommandOutcome::json(report, exit_code)
            }
        }
        RunnerCommand::HarnessList => {
            let statuses = match harness_statuses(image, config) {
                Ok(statuses) => statuses,
                Err(error) => return error_outcome(error, mode, clock, ids),
            };
            if mode == OutputMode::Human {
                CommandOutcome::human(
                    render_harness_list_human(&statuses, &config.harness.value),
                    "",
                    0,
                )
            } else {
                CommandOutcome::json(
                    json!({
                        "schema": "openprose.harness-list/1",
                        "selected": config.harness.value,
                        "harnesses": statuses
                    }),
                    0,
                )
            }
        }
        RunnerCommand::HarnessUse(harness) => {
            if let Err(error) = validate_harness_selection(&harness, flags) {
                return error_outcome(error, mode, clock, ids);
            }
            if let Err(error) = validate_saved_harness_will_be_active(&harness, config) {
                return error_outcome(error, mode, clock, ids);
            }
            let selection = match crate::config::write_user_harness(
                config,
                &harness,
                flags.model.as_deref(),
                flags.auth_profile.as_deref(),
            ) {
                Ok(selection) => selection,
                Err(error) => return error_outcome(error, mode, clock, ids),
            };
            let report = json!({
                "schema":"openprose.harness-selection/1",
                "harness":harness,
                "scope":"user",
                "path":selection.path.display().to_string(),
                "changed":selection.changed
            });
            if mode == OutputMode::Human {
                let saved_route = match flags.auth_profile.as_deref() {
                    Some(profile) => {
                        format!("Route: {} (saved)\n", human_safe_scalar(profile))
                    }
                    None if harness == "codex" => {
                        "Route: cached-chatgpt-login (Codex default; not saved)\n".to_owned()
                    }
                    None if harness == "claude" => {
                        "Route: claude-subscription (Claude default; not saved)\n".to_owned()
                    }
                    None => "Route: OpenProse account (external route cleared)\n".to_owned(),
                };
                let saved_model = match flags.model.as_deref() {
                    Some(model) => format!("Model: {} (saved)\n", human_safe_scalar(model)),
                    None if matches!(harness.as_str(), "codex" | "claude") => {
                        "Model: harness default (not saved)\n".to_owned()
                    }
                    None => "Model: OpenProse default (external model cleared)\n".to_owned(),
                };
                CommandOutcome::human(
                    format!(
                        "Default harness: {} ({})\n{}{}Configuration: {}\nNext: {}\n",
                        human_safe_scalar(&harness),
                        if selection.changed {
                            "updated"
                        } else {
                            "already selected"
                        },
                        saved_route,
                        saved_model,
                        human_safe_scalar(&selection.path.display().to_string()),
                        human_runner_command("cli doctor")
                    ),
                    "",
                    0,
                )
            } else {
                CommandOutcome::json(report, 0)
            }
        }
        RunnerCommand::CleanupPrime(handle) => {
            execute_prime_cleanup(&handle, mode, clock, ids, &std::env::temp_dir())
        }
        RunnerCommand::AuthStatus | RunnerCommand::AuthLogin | RunnerCommand::AuthLogout | RunnerCommand::OrgList | RunnerCommand::EnvironmentShow | RunnerCommand::EnvironmentUse(_) | RunnerCommand::EnvironmentReset | RunnerCommand::Package(_) => {
            unreachable!("service commands are dispatched before harness operations")
        }
    }
}

/// Executes the opaque Prime cleanup operation without requiring harness,
/// model, account, configuration, or Skill Runtime Image readiness.
#[must_use]
pub fn execute_prime_cleanup(
    handle: &str,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    temporary_root: &Path,
) -> CommandOutcome {
    if let Err(error) =
        recover_prime_owned_service(temporary_root, handle, SettlementPolicy::default())
    {
        return error_outcome(error, mode, clock, ids);
    }
    let report = json!({
        "schema":"openprose.prime-cleanup/1",
        "adapterId":"prime/rpc",
        "cleanupHandle":handle,
        "status":"cleaned",
        "serviceSettlement":"verified",
        "sensitiveFilesRemoved":true,
        "directoryRemoved":true
    });
    if mode == OutputMode::Human {
        CommandOutcome::human(
            format!(
                "Prime cleanup: complete\nHandle: {}\nOwned service: settled\nPrivate directory: removed\n",
                human_safe_scalar(handle)
            ),
            "",
            0,
        )
    } else {
        CommandOutcome::json(report, 0)
    }
}

fn render_harness_list_human(statuses: &[HarnessStatus], selected_harness: &str) -> String {
    let mut output = "Harnesses:\n".to_owned();
    for status in statuses.iter().filter(|status| !status.test_only) {
        render_harness_status_human(&mut output, status, selected_harness);
    }
    if TEST_SEAMS_ENABLED && statuses.iter().any(|status| status.test_only) {
        output.push_str("\nTest-only harnesses:\n");
        for status in statuses.iter().filter(|status| status.test_only) {
            render_harness_status_human(&mut output, status, selected_harness);
        }
    }
    let _ = write!(
        output,
        "\nChoose Codex: {}\nChoose Claude: {}\nPrime and OMP: set PROSE_MODEL to a fully qualified provider/model installed in that harness; unset or invalid values are refused.\nChoose Prime: {}\nChoose OMP: {}\nThen verify: {}\n",
        human_runner_command("cli harness use codex"),
        human_runner_command("cli harness use claude"),
        human_runner_command(
            "cli harness use prime --model \"$PROSE_MODEL\" --auth-profile prime-harness-login"
        ),
        human_runner_command(
            "cli harness use omp --model \"$PROSE_MODEL\" --auth-profile omp-harness-login"
        ),
        human_runner_command("cli doctor")
    );
    output
}

fn render_harness_status_human(
    output: &mut String,
    status: &HarnessStatus,
    selected_harness: &str,
) {
    let selected = status.id == selected_harness;
    let _ = write!(
        output,
        "{} {} availability={} transport={}",
        if selected { '*' } else { ' ' },
        human_safe_scalar(status.id),
        human_safe_scalar(&status.availability),
        human_safe_scalar(&status.transports.join(","))
    );
    if let Some(version) = status.detected_version.as_deref() {
        let _ = write!(output, " version={}", human_safe_scalar(version));
    }
    if selected {
        output.push_str(" (selected)");
    }
    output.push('\n');
    for prerequisite in &status.runtime_prerequisites {
        let _ = writeln!(
            output,
            "    runtime prerequisite: {}; availability: {}; detected version: {}; required version: {}",
            human_safe_scalar(prerequisite.runtime),
            human_safe_scalar(prerequisite.availability),
            human_safe_scalar(
                prerequisite
                    .detected_version
                    .as_deref()
                    .unwrap_or("missing")
            ),
            match prerequisite.version_range {
                ">=1.3.14" => "1.3.14 or newer",
                other => other,
            }
        );
        let _ = writeln!(
            output,
            "    runtime repair: {}",
            human_safe_scalar(prerequisite.repair_command)
        );
    }
}

fn supported_transports(harness: &str) -> Option<&'static [&'static str]> {
    match harness {
        "openprose" => Some(&["hosted"]),
        "mock" => Some(&["deterministic", "fake-process"]),
        _ => installed_adapters::for_harness(harness).map(|adapter| match adapter {
            installed_adapters::InstalledAdapter::AgentsSdkJsonl => &["jsonl"][..],
            installed_adapters::InstalledAdapter::CodexExecJson => &["exec-json"][..],
            installed_adapters::InstalledAdapter::ClaudePrintStreamJson => {
                &["print-stream-json"][..]
            }
            installed_adapters::InstalledAdapter::PrimeRpc
            | installed_adapters::InstalledAdapter::OmpRpc => &["rpc"][..],
        }),
    }
}

fn validate_selected_transport(config: &EffectiveConfig) -> Result<(), RunnerError> {
    let Some(supported) = supported_transports(&config.harness.value) else {
        return Ok(());
    };
    if config.transport.value == "auto" || supported.contains(&config.transport.value.as_str()) {
        return Ok(());
    }
    Err(RunnerError::catalog(ErrorCode::TransportUnsupported)
        .with_detail("harness", config.harness.value.clone())
        .with_detail("requested", config.transport.value.clone())
        .with_detail("supported", supported))
}

fn execute_forward(
    argv: &[String],
    flags: &GlobalFlags,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
    human_stream: Option<&mut dyn IoWrite>,
) -> CommandOutcome {
    #[allow(unused_mut)]
    let mut human_stream = human_stream;
    if let Err(error) = validate_selected_transport(config) {
        return error_outcome(error, mode, clock, ids);
    }
    if config.harness.value == "mock"
        && (flags.harness.as_deref() != Some("mock")
            || config.harness.source.kind != ConfigSourceKind::Flag)
    {
        return forward_error_outcome(
            RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail(
                "reason",
                "the deterministic mock may be selected only with the explicit `--harness mock` invocation flag",
            ),
            argv,
            config,
            image,
            mode,
            clock,
            ids,
        );
    }

    if config.harness.value == "mock" {
        if let Some(error) = mock_admission_error(image) {
            return forward_error_outcome(error, argv, config, image, mode, clock, ids);
        }
    }

    let installed_adapter =
        match installed_adapters::select(&config.harness.value, &config.transport.value) {
            Ok(adapter) => adapter,
            Err(error) => {
                return forward_error_outcome(error, argv, config, image, mode, clock, ids);
            }
        };

    #[cfg(feature = "test-seams")]
    if let Some(adapter) = installed_adapter {
        match conformance_adapter_controls() {
            Ok(Some(controls)) => {
                if flags.dry_run {
                    return dry_run_outcome(
                        config,
                        image,
                        adapter.transport(),
                        adapter.id(),
                        None,
                        Some(adapter.prompt_placement()),
                        adapter.prompt_strictness(),
                        if adapter.isolation_guarantee() == "advisory" {
                            "partial"
                        } else {
                            "unsupported"
                        },
                        "harness-managed",
                        "unknown",
                        "user-provider",
                        None,
                        mode,
                    );
                }
                let ambient = env::vars_os().collect::<Vec<_>>();
                if let Err(error) = assert_runtime_prerequisites(adapter, &config.cwd, &ambient) {
                    return forward_error_outcome(error, argv, config, image, mode, clock, ids);
                }
                return execute_installed_adapter_probe(
                    adapter,
                    &controls,
                    argv,
                    config,
                    image,
                    mode,
                    clock,
                    ids,
                    cancellation,
                    human_stream.take(),
                );
            }
            Ok(None) => {}
            Err(error) => {
                return forward_error_outcome(error, argv, config, image, mode, clock, ids);
            }
        }
    }

    if flags.dry_run && config.harness.value == "openprose" {
        return dry_run_outcome(
            config,
            image,
            "hosted",
            "openprose/hosted",
            None,
            None,
            "unsupported",
            "unsupported",
            "openprose-account",
            "unknown",
            "openprose",
            Some(
                &RunnerError::catalog(ErrorCode::HostedUnavailable)
                    .with_detail("billingOwner", "openprose")
                    .with_detail("fallbackSelected", false),
            ),
            mode,
        );
    }
    if flags.dry_run {
        if let Some(adapter) = installed_adapter {
            let discovery = inspect_installed_adapter(adapter, config);
            return dry_run_outcome(
                config,
                image,
                adapter.transport(),
                adapter.id(),
                discovery.version.as_deref(),
                Some(adapter.prompt_placement()),
                adapter.prompt_strictness(),
                if adapter.isolation_guarantee() == "advisory" {
                    "partial"
                } else {
                    "unsupported"
                },
                "harness-managed",
                discovery.auth_readiness,
                "user-provider",
                discovery.problem.as_ref(),
                mode,
            );
        }
    }
    if flags.dry_run && config.harness.value != "mock" {
        return dry_run_outcome(
            config,
            image,
            "unavailable",
            &format!("{}/unavailable", config.harness.value),
            None,
            None,
            "unsupported",
            "unsupported",
            "harness-managed",
            "unknown",
            "user-provider",
            Some(
                &RunnerError::catalog(ErrorCode::HarnessUnavailable)
                    .with_detail("harness", config.harness.value.clone())
                    .with_detail("fallbackAttempted", false),
            ),
            mode,
        );
    }

    match config.harness.value.as_str() {
        "openprose" => forward_error_outcome(
            RunnerError::catalog(ErrorCode::HostedUnavailable)
                .with_detail("billingOwner", "openprose")
                .with_detail("fallbackSelected", false),
            argv,
            config,
            image,
            mode,
            clock,
            ids,
        ),
        "mock" => execute_mock(
            argv,
            flags.dry_run,
            config,
            image,
            mode,
            clock,
            ids,
            cancellation,
        ),
        _ if installed_adapter.is_some() => execute_installed_adapter(
            installed_adapter.expect("guarded installed adapter"),
            None,
            argv,
            config,
            image,
            mode,
            clock,
            ids,
            cancellation,
            human_stream,
        ),
        other => forward_error_outcome(
            RunnerError::catalog(ErrorCode::HarnessUnavailable)
                .with_detail("harness", other.to_owned())
                .with_detail("fallbackAttempted", false),
            argv,
            config,
            image,
            mode,
            clock,
            ids,
        ),
    }
}

#[cfg(feature = "test-seams")]
#[derive(Debug)]
struct ConformanceAdapterControls {
    executable: PathBuf,
    observation: PathBuf,
    credential_group: String,
    cleanup_failure: bool,
}

struct InstalledHumanStream<'a> {
    adapter: installed_adapters::InstalledAdapter,
    rpc_id: String,
    sink: &'a mut dyn IoWrite,
    protected: Vec<String>,
    records: Vec<Value>,
    candidates: Vec<String>,
    emitted: String,
    stalled: bool,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum OmpPreludeState {
    AwaitCommands,
    AwaitState,
    PromptSent,
}

#[derive(Debug)]
struct OmpStagedController {
    state: OmpPreludeState,
    state_id: String,
    prompt_bytes: Option<Vec<u8>>,
    pending_write: Option<Vec<u8>>,
    project_current_record: bool,
    tool_inventory: Vec<Value>,
}

impl OmpStagedController {
    fn new(invocation_id: &str, prompt_bytes: Vec<u8>) -> Self {
        Self {
            state: OmpPreludeState::AwaitCommands,
            state_id: installed_adapters::omp_rpc_id(invocation_id, "state.1"),
            prompt_bytes: Some(prompt_bytes),
            pending_write: None,
            project_current_record: false,
            tool_inventory: Vec::new(),
        }
    }

    #[allow(clippy::result_large_err)]
    fn observe(&mut self, record: &Value) -> Result<(), SupervisorFailure> {
        let record_type = record.get("type").and_then(Value::as_str);
        self.project_current_record =
            self.state != OmpPreludeState::PromptSent && record_type == Some("response");
        if record_type == Some("agent_end")
            && record.get("isTerminal").and_then(Value::as_bool) == Some(false)
        {
            return Err(stream_observer_failure(
                FailureKind::HarnessFailed,
                "unsupported_nonterminal_settlement",
            ));
        }
        if record_type == Some("extension_ui_request") {
            return match installed_adapters::omp_extension_ui_disposition(record) {
                installed_adapters::OmpExtensionUiDisposition::Presentation => Ok(()),
                installed_adapters::OmpExtensionUiDisposition::Blocked => {
                    Err(stream_observer_failure(
                        FailureKind::HarnessFailed,
                        "OMP requested unsupported interactive extension UI",
                    ))
                }
                installed_adapters::OmpExtensionUiDisposition::Malformed => {
                    Err(stream_observer_failure(
                        FailureKind::ProtocolMalformed,
                        "OMP emitted malformed extension UI",
                    ))
                }
            };
        }
        match self.state {
            OmpPreludeState::AwaitCommands => match record_type {
                Some("ready") => Ok(()),
                Some("available_commands_update")
                    if exact_record_keys(record, &["type", "commands"])
                        && record.get("commands").is_some_and(Value::is_array) =>
                {
                    #[derive(Serialize)]
                    struct StateRequest<'a> {
                        id: &'a str,
                        #[serde(rename = "type")]
                        record_type: &'static str,
                    }
                    let mut bytes = serde_json::to_vec(&StateRequest {
                        id: &self.state_id,
                        record_type: "get_state",
                    })
                    .map_err(|_| {
                        stream_observer_failure(
                            FailureKind::Internal,
                            "OMP state request could not be encoded",
                        )
                    })?;
                    bytes.push(b'\n');
                    self.pending_write = Some(bytes);
                    self.state = OmpPreludeState::AwaitState;
                    Ok(())
                }
                _ => Err(stream_observer_failure(
                    FailureKind::ProtocolMalformed,
                    "OMP emitted lifecycle data before its command inventory barrier",
                )),
            },
            OmpPreludeState::AwaitState => {
                if record_type != Some("response")
                    || record.get("id").and_then(Value::as_str) != Some(self.state_id.as_str())
                    || record.get("command").and_then(Value::as_str) != Some("get_state")
                {
                    return Err(stream_observer_failure(
                        FailureKind::ProtocolMalformed,
                        "OMP emitted lifecycle data before its correlated tool-state proof",
                    ));
                }
                if record.get("success").and_then(Value::as_bool) == Some(false) {
                    return Err(stream_observer_failure(
                        FailureKind::HarnessFailed,
                        "OMP get_state control request failed",
                    ));
                }
                if !exact_record_keys(record, &["id", "type", "command", "success", "data"])
                    || record.get("success").and_then(Value::as_bool) != Some(true)
                {
                    return Err(stream_observer_failure(
                        FailureKind::ProtocolMalformed,
                        "OMP emitted a malformed tool-state response",
                    ));
                }
                let Some(tools) = record.pointer("/data/dumpTools").and_then(Value::as_array)
                else {
                    return Err(stream_observer_failure(
                        FailureKind::ProtocolMalformed,
                        "OMP tool-state response omitted dumpTools",
                    ));
                };
                if tools.iter().any(|tool| tool.get("name").and_then(Value::as_str).is_none_or(str::is_empty)) {
                    return Err(stream_observer_failure(FailureKind::ProtocolMalformed,"OMP reported an invalid tool inventory"));
                }
                self.tool_inventory = tools.iter().map(retained_omp_tool).collect();
                self.pending_write = self.prompt_bytes.take();
                self.state = OmpPreludeState::PromptSent;
                Ok(())
            }
            OmpPreludeState::PromptSent => {
                if record_type == Some("response")
                    && record.get("command").and_then(Value::as_str) == Some("get_state")
                {
                    Err(stream_observer_failure(
                        FailureKind::ProtocolMalformed,
                        "OMP emitted a duplicate tool-state response",
                    ))
                } else {
                    Ok(())
                }
            }
        }
    }

    fn retained_record_projection(&self) -> Option<Value> {
        self.project_current_record.then(|| {
            json!({
                "id":self.state_id,
                "type":"response",
                "command":"get_state",
                "success":true,
                "data":{"dumpTools":self.tool_inventory}
            })
        })
    }
}

#[derive(Debug)]
struct PrimeStagedController {
    id: String,
    prompt: Option<Vec<u8>>,
    pending_write: Option<Vec<u8>>,
    records: Vec<Value>,
    close: bool,
}
impl PrimeStagedController {
    fn observe(&mut self, record: &Value) -> Result<(), SupervisorFailure> {
        if self.close {return Ok(());}
        self.records.push(record.clone());
        installed_adapters::validate_prime_native_prefix(&self.records,&self.id)
            .map_err(|_|stream_observer_failure(FailureKind::ProtocolMalformed,"Prime native lifecycle rejected the observed record"))?;
        if self.records.len()==1 { self.pending_write=self.prompt.take(); }
        if record["type"]=="response" && record["command"]=="prompt" && record["success"]==true {self.close=true;}
        Ok(())
    }
}

struct InstalledRunObserver<'a> {
    require_api_source:bool,
    auth_source_failed:bool,
    sdk: bool,
    native_failure: Option<Value>,
    capture: Option<NativeCapture>,
    human: Option<InstalledHumanStream<'a>>,
    omp: Option<OmpStagedController>,
    prime: Option<PrimeStagedController>,
}

impl std::fmt::Debug for InstalledRunObserver<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("InstalledRunObserver")
            .field("human", &self.human)
            .field("omp", &self.omp)
            .finish()
    }
}

impl RecordObserver for InstalledRunObserver<'_> {
    fn observe_parsed(&mut self, record: &Value) -> Result<(), SupervisorFailure> {
        if self.sdk && record.get("type").and_then(Value::as_str)==Some("error") {
            self.native_failure=Some(installed_adapters::sdk_native_failure(record));
        }
        if let Some(capture)=self.capture.as_mut(){capture.write(record)?;}
        Ok(())
    }
    fn observe(&mut self, record: &Value) -> Result<(), SupervisorFailure> {
        #[cfg(windows)]
        if let Some(capture)=self.capture.as_mut(){capture.write(record)?;}
        if self.require_api_source && record["type"]=="system" && record["subtype"]=="init" && record["apiKeySource"]!="ANTHROPIC_API_KEY" {
            self.auth_source_failed=true;
            return Err(stream_observer_failure(FailureKind::HarnessFailed,"Native init did not confirm selected API credential route"));
        }
        if let Some(prime)=self.prime.as_mut() {prime.observe(record)?;}
        let mut projection = None;
        if let Some(omp) = self.omp.as_mut() {
            omp.observe(record)?;
            projection = omp.retained_record_projection();
        }
        if let Some(human) = self.human.as_mut() {
            human.observe(projection.as_ref().unwrap_or(record))?;
        }
        Ok(())
    }

    fn take_stdin_write(&mut self) -> Result<Option<Vec<u8>>, SupervisorFailure> {
        if let Some(prime)=self.prime.as_mut() {return Ok(prime.pending_write.take());}
        Ok(self
            .omp
            .as_mut()
            .and_then(|controller| controller.pending_write.take()))
    }

    fn retained_record_projection(&self, _record: &Value) -> Option<Value> {
        self.omp
            .as_ref()
            .and_then(OmpStagedController::retained_record_projection)
    }

    fn close_stdin_requested(&self) -> bool {self.prime.as_ref().is_some_and(|p|p.close)}

    fn requires_staged_stdin(&self) -> bool {
        self.omp.is_some() || self.prime.is_some()
    }
}

fn exact_record_keys(record: &Value, keys: &[&str]) -> bool {
    record.as_object().is_some_and(|object| {
        object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct HumanStreamSettlement {
    emitted_prefix_bytes: usize,
    stalled: bool,
}

impl std::fmt::Debug for InstalledHumanStream<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("InstalledHumanStream")
            .field("adapter", &self.adapter)
            .field("rpc_id", &"[REDACTED]")
            .field("protected_count", &self.protected.len())
            .field("record_count", &self.records.len())
            .field("candidate_count", &self.candidates.len())
            .field("emitted_bytes", &self.emitted.len())
            .field("stalled", &self.stalled)
            .finish()
    }
}

impl<'a> InstalledHumanStream<'a> {
    fn new(
        adapter: installed_adapters::InstalledAdapter,
        rpc_id: &str,
        sink: &'a mut dyn IoWrite,
        protected: Vec<String>,
    ) -> Self {
        Self {
            adapter,
            rpc_id: rpc_id.to_owned(),
            sink,
            protected,
            records: Vec::new(),
            candidates: Vec::new(),
            emitted: String::new(),
            stalled: false,
        }
    }

    #[allow(clippy::result_large_err)]
    fn reconcile_candidates(&mut self, candidates: Vec<String>) -> Result<(), SupervisorFailure> {
        if candidates.len() < self.candidates.len()
            || self.candidates.iter().enumerate().any(|(index, previous)| {
                let Some(current) = candidates.get(index) else {
                    return true;
                };
                if candidates.len() == self.candidates.len() && index + 1 == self.candidates.len() {
                    !current.starts_with(previous)
                } else {
                    current != previous
                }
            })
        {
            return Err(stream_observer_failure(
                FailureKind::ProtocolMalformed,
                "incremental assistant messages changed an admitted prefix",
            ));
        }
        if protected_crosses_message_boundary(&candidates, &self.protected) {
            self.candidates = candidates;
            self.stalled = true;
            return Ok(());
        }
        self.candidates = candidates;
        if self.stalled {
            return Ok(());
        }
        let projected = provisional_visible_text(&self.candidates);
        let Some(delta) = projected.strip_prefix(&self.emitted) else {
            return Err(stream_observer_failure(
                FailureKind::ProtocolMalformed,
                "incremental assistant projection changed an admitted prefix",
            ));
        };
        if delta.is_empty() {
            return Ok(());
        }
        if !safe_human_stream_message(&projected, &self.protected) {
            // Once an earlier message is withheld, later messages must also be
            // withheld so final rendering cannot reorder visible output.
            self.stalled = true;
            return Ok(());
        }
        let safe_delta = human_safe_multiline(delta);
        self.sink
            .write_all(safe_delta.as_bytes())
            .and_then(|()| self.sink.flush())
            .map_err(|_| {
                stream_observer_failure(
                    FailureKind::Internal,
                    "human assistant output could not be written",
                )
            })?;
        self.emitted.push_str(delta);
        Ok(())
    }

    fn settlement(
        &self,
        terminal: &installed_adapters::RecoveredTerminal,
    ) -> Result<HumanStreamSettlement, RunnerError> {
        if terminal.visible_text.starts_with(&self.emitted) {
            Ok(HumanStreamSettlement {
                emitted_prefix_bytes: self.emitted.len(),
                stalled: self.stalled,
            })
        } else {
            Err(
                RunnerError::catalog(ErrorCode::ProtocolMalformed).with_detail(
                    "reason",
                    "The incrementally admitted assistant prefix did not match settled output.",
                ),
            )
        }
    }
}

fn provisional_visible_text(messages: &[String]) -> String {
    let Some(carrier_index) = messages
        .iter()
        .rposition(|message| message.lines().any(|line| !line.trim().is_empty()))
    else {
        return String::new();
    };
    let mut visible = messages.to_vec();
    let mut carrier_lines = visible[carrier_index].split('\n').collect::<Vec<_>>();
    let Some(terminal_index) = carrier_lines
        .iter()
        .rposition(|line| !line.trim().is_empty())
    else {
        return String::new();
    };
    carrier_lines.remove(terminal_index);
    while carrier_lines
        .last()
        .is_some_and(|line| line.trim().is_empty())
    {
        carrier_lines.pop();
    }
    visible[carrier_index] = carrier_lines.join("\n");
    visible.retain(|message| !message.is_empty());
    visible.join("\n")
}

impl RecordObserver for InstalledHumanStream<'_> {
    fn observe(&mut self, record: &Value) -> Result<(), SupervisorFailure> {
        self.records.push(record.clone());
        let candidates = installed_adapters::admitted_assistant_messages(
            self.adapter,
            &self.records,
            &self.rpc_id,
        )
        .map_err(|_| {
            stream_observer_failure(
                FailureKind::ProtocolMalformed,
                "adapter assistant record failed incremental admission",
            )
        })?;
        if candidates.is_empty() {
            return Ok(());
        }
        self.reconcile_candidates(candidates)
    }
}

fn safe_human_stream_message(message: &str, protected: &[String]) -> bool {
    !protected
        .iter()
        .filter(|value| !value.is_empty())
        .any(|value| message.contains(value))
        && !message
            .lines()
            .any(|line| matches!(line.trim_start().as_bytes().first(), Some(b'{' | b'[')))
}

fn protected_crosses_message_boundary(candidates: &[String], protected: &[String]) -> bool {
    candidates.windows(2).any(|boundary| {
        protected
            .iter()
            .filter(|literal| !literal.is_empty())
            .any(|literal| {
                literal.char_indices().skip(1).any(|(split, _)| {
                    boundary[0].ends_with(&literal[..split])
                        && boundary[1].starts_with(&literal[split..])
                })
            })
    })
}

fn stream_observer_failure(kind: FailureKind, message: &str) -> SupervisorFailure {
    SupervisorFailure {
        transport_diagnostic: None,
        kind,
        message: message.to_owned(),
        stderr: String::new(),
        process_exit: None,
        process_signal: None,
        process_started: false,
        terminal_observed: false,
        terminal_envelope: None,
        records: Vec::new(),
    }
}

// Preserve only the advertised task defaults required by native argument validation.
// Keep every inventory entry so duplicate task names remain ambiguous.
fn retained_omp_tool(tool:&Value)->Value {
 let mut out=json!({"name":tool["name"]});let p=&tool["parameters"];
 if tool["name"]!="task"||p["type"]!="object"{return out;}
 let field=|v:&Value|v["type"]=="string"&&v["default"]=="task";
 let mut properties=json!({});
 if field(&p["properties"]["agent"]){properties["agent"]=json!({"type":"string","default":"task"});}
 let t=&p["properties"]["tasks"];
 if t["type"]=="array"&&t["items"]["type"]=="object"&&field(&t["items"]["properties"]["agent"]){properties["tasks"]=json!({"type":"array","items":{"type":"object","properties":{"agent":{"type":"string","default":"task"}}}});}
 if !properties.as_object().unwrap().is_empty(){out["parameters"]=json!({"type":"object","properties":properties});}
 out
}

#[derive(Debug)]
struct InstalledAdapterDiscovery {
    executable: Option<PathBuf>,
    version: Option<String>,
    runtime_prerequisites: Vec<installed_adapters::RuntimePrerequisiteObservation>,
    environment: Option<EnvironmentPolicy>,
    auth_group: Option<String>,
    auth_readiness: &'static str,
    problem: Option<RunnerError>,
}

fn inspect_runtime_prerequisites(
    adapter: installed_adapters::InstalledAdapter,
    cwd: &Path,
    ambient: &[(OsString, OsString)],
) -> Result<Vec<installed_adapters::RuntimePrerequisiteObservation>, RunnerError> {
    let search_path = ambient
        .iter()
        .find(|(name, _)| name == std::ffi::OsStr::new("PATH"))
        .map(|(_, value)| value.as_os_str());
    let mut observations = Vec::new();
    for requirement in adapter.runtime_prerequisites() {
        let Some(executable) =
            installed_adapters::resolve_named_executable(&[requirement.runtime], search_path)
        else {
            observations.push(installed_adapters::runtime_prerequisite_observation(
                &requirement,
                None,
                true,
            ));
            continue;
        };
        let environment =
            installed_adapters::version_probe_environment(adapter, ambient.iter().cloned());
        let probe = VersionProbe {
            argv: vec!["--version".into()],
            timeout: Duration::from_secs(5),
            max_output_bytes: 4096,
            required_substring: None,
            output: VersionProbeOutput::Stdout,
        };
        let observation = match probe_version(
            &executable,
            cwd,
            &environment,
            &probe,
            &CancellationToken::default(),
        ) {
            Ok(version) => installed_adapters::runtime_prerequisite_observation(
                &requirement,
                Some(&version),
                false,
            ),
            Err(failure) if failure.kind == FailureKind::CleanupFailed => {
                return Err(map_supervisor_failure(&failure)
                    .with_detail("adapterId", adapter.id())
                    .with_detail("phase", "runtime-prerequisite"));
            }
            Err(_) => {
                installed_adapters::runtime_prerequisite_observation(&requirement, None, false)
            }
        };
        observations.push(observation);
    }
    Ok(observations)
}

#[cfg(feature = "test-seams")]
fn assert_runtime_prerequisites(
    adapter: installed_adapters::InstalledAdapter,
    cwd: &Path,
    ambient: &[(OsString, OsString)],
) -> Result<Vec<installed_adapters::RuntimePrerequisiteObservation>, RunnerError> {
    let observations = inspect_runtime_prerequisites(adapter, cwd, ambient)?;
    if let Some(blocked) = observations
        .iter()
        .find(|observation| observation.availability != "available")
    {
        return Err(RunnerError::catalog(ErrorCode::HarnessIncompatible)
            .with_detail("adapterId", adapter.id())
            .with_detail("runtimePrerequisite", json!(blocked))
            .with_detail("fallbackAttempted", false));
    }
    Ok(observations)
}

fn selected_auth_group(
    adapter: installed_adapters::InstalledAdapter,
    config: &EffectiveConfig,
) -> Result<String, RunnerError> {
    if let Some(profile) = config.auth_profile.value.as_deref() {
        if !adapter.auth_profiles().contains(&profile) {
            return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
                .with_detail("adapterId", adapter.id())
                .with_detail(
                    "reason",
                    format!("Unknown auth_profile for {}: {profile}.", adapter.id()),
                )
                .with_detail("supportedAuthProfiles", adapter.auth_profiles()));
        }
        return Ok(profile.to_owned());
    }
    if matches!(
        adapter,
        installed_adapters::InstalledAdapter::PrimeRpc
            | installed_adapters::InstalledAdapter::OmpRpc
    ) {
        return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
            .with_detail("adapterId", adapter.id())
            .with_detail(
                "reason",
                "This harness requires an explicit auth_profile (or PROSE_AUTH_PROFILE); credential routes are never guessed.",
            )
            .with_detail("supportedAuthProfiles", adapter.auth_profiles()));
    }
    Ok(adapter.default_probe_auth_group().to_owned())
}

fn inspect_installed_adapter(
    adapter: installed_adapters::InstalledAdapter,
    config: &EffectiveConfig,
) -> InstalledAdapterDiscovery {
    if let Err(problem) = installed_adapters::assert_platform_supported(
        adapter,
        installed_adapters::HostPlatform::current(),
    ) {
        return InstalledAdapterDiscovery {
            executable: None,
            version: None,
            runtime_prerequisites: Vec::new(),
            environment: None,
            auth_group: None,
            auth_readiness: "unknown",
            problem: Some(problem),
        };
    }
    if matches!(
        adapter,
        installed_adapters::InstalledAdapter::PrimeRpc
            | installed_adapters::InstalledAdapter::OmpRpc
    ) {
        let model_problem = match config.model.value.as_deref() {
            None => Some(
                "Prime and OMP require an explicit fully qualified provider/model for functional-alpha execution (`--model` or PROSE_MODEL).",
            ),
            Some(model) if !is_fully_qualified_provider_model(model) => Some(
                "Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments.",
            ),
            Some(_) => None,
        };
        if let Some(reason) = model_problem {
            return InstalledAdapterDiscovery {
                executable: None,
                version: None,
                runtime_prerequisites: Vec::new(),
                environment: None,
                auth_group: None,
                auth_readiness: "unknown",
                problem: Some(
                    RunnerError::catalog(ErrorCode::ConfigInvalid)
                        .with_detail("adapterId", adapter.id())
                        .with_detail("reason", reason),
                ),
            };
        }
    }
    let auth_group = match selected_auth_group(adapter, config) {
        Ok(group) => group,
        Err(problem) => {
            return InstalledAdapterDiscovery {
                executable: None,
                version: None,
                runtime_prerequisites: Vec::new(),
                environment: None,
                auth_group: None,
                auth_readiness: "unknown",
                problem: Some(problem),
            };
        }
    };
    let ambient = env::vars_os().collect::<Vec<_>>();
    let search_path = ambient
        .iter()
        .find(|(name, _)| name == std::ffi::OsStr::new("PATH"))
        .map(|(_, value)| value.as_os_str());
    let Some(executable) = installed_adapters::resolve_executable(adapter, search_path) else {
        return InstalledAdapterDiscovery {
            executable: None,
            version: None,
            runtime_prerequisites: Vec::new(),
            environment: None,
            auth_group: None,
            auth_readiness: "unknown",
            problem: Some(adapter.unavailable_error()),
        };
    };
    let runtime_prerequisites = match inspect_runtime_prerequisites(adapter, &config.cwd, &ambient)
    {
        Ok(observations) => observations,
        Err(problem) => {
            return InstalledAdapterDiscovery {
                executable: Some(executable),
                version: None,
                runtime_prerequisites: Vec::new(),
                environment: None,
                auth_group: None,
                auth_readiness: "unknown",
                problem: Some(problem),
            };
        }
    };
    if let Some(blocked) = runtime_prerequisites
        .iter()
        .find(|observation| observation.availability != "available")
    {
        let problem = RunnerError::catalog(ErrorCode::HarnessIncompatible)
            .with_detail("adapterId", adapter.id())
            .with_detail("runtimePrerequisite", json!(blocked))
            .with_detail("fallbackAttempted", false);
        return InstalledAdapterDiscovery {
            executable: Some(executable),
            version: None,
            runtime_prerequisites,
            environment: None,
            auth_group: None,
            auth_readiness: "unknown",
            problem: Some(problem),
        };
    }
    let probe_environment = installed_adapters::version_probe_environment(adapter, ambient.clone());
    let version = match probe_version(
        &executable,
        &config.cwd,
        &probe_environment,
        &adapter.version_probe(),
        &CancellationToken::default(),
    ) {
        Ok(version) => version,
        Err(failure) => {
            return InstalledAdapterDiscovery {
                executable: Some(executable),
                version: None,
                runtime_prerequisites,
                environment: None,
                auth_group: None,
                auth_readiness: "unknown",
                problem: Some(map_installed_version_probe_failure(adapter, &failure)),
            };
        }
    };
    if !adapter.version_is_supported(&version) {
        return InstalledAdapterDiscovery {
            executable: Some(executable),
            version: Some(version.clone()),
            runtime_prerequisites,
            environment: None,
            auth_group: None,
            auth_readiness: "unknown",
            problem: Some(adapter.incompatible_version_error(&version)),
        };
    }
    let auth_readiness = match installed_adapters::auth_readiness(adapter, &auth_group, &ambient) {
        Ok(readiness) => readiness,
        Err(problem) => {
            return InstalledAdapterDiscovery {
                executable: Some(executable),
                version: Some(version),
                runtime_prerequisites,
                environment: None,
                auth_group: Some(auth_group),
                auth_readiness: "missing",
                problem: Some(problem),
            };
        }
    };
    let environment =
        match installed_adapters::environment_policy(adapter, &auth_group, ambient.clone()) {
            Ok(environment) => environment,
            Err(problem) => {
                return InstalledAdapterDiscovery {
                    executable: Some(executable),
                    version: Some(version),
                    runtime_prerequisites,
                    environment: None,
                    auth_group: Some(auth_group),
                    auth_readiness,
                    problem: Some(problem),
                };
            }
        };
    let should_probe_auth = matches!(
        (adapter, auth_group.as_str()),
        (
            installed_adapters::InstalledAdapter::CodexExecJson,
            "cached-chatgpt-login"
        ) | (
            installed_adapters::InstalledAdapter::ClaudePrintStreamJson,
            "claude-subscription"
        )
    );
    let auth_readiness = if should_probe_auth {
        match probe_command(
            &executable,
            &config.cwd,
            &environment,
            &adapter
                .auth_probe()
                .expect("only Codex and Claude have installed auth probes"),
            &CancellationToken::default(),
        ) {
            Ok(outcome) => match adapter.classify_auth_probe(&outcome) {
                Ok(readiness) => readiness,
                Err(problem) => {
                    return InstalledAdapterDiscovery {
                        executable: Some(executable),
                        version: Some(version),
                        runtime_prerequisites,
                        environment: Some(environment),
                        auth_group: Some(auth_group),
                        auth_readiness: "required",
                        problem: Some(problem.with_detail("fallbackAttempted", false)),
                    };
                }
            },
            Err(failure) => {
                return InstalledAdapterDiscovery {
                    executable: Some(executable),
                    version: Some(version),
                    runtime_prerequisites,
                    environment: Some(environment),
                    auth_group: Some(auth_group),
                    auth_readiness: "unknown",
                    problem: Some(
                        map_supervisor_failure(&failure)
                            .with_detail("adapterId", adapter.id())
                            .with_detail("phase", "auth-readiness")
                            .with_detail("fallbackAttempted", false),
                    ),
                };
            }
        }
    } else {
        auth_readiness
    };
    InstalledAdapterDiscovery {
        executable: Some(executable),
        version: Some(version),
        runtime_prerequisites,
        environment: Some(environment),
        auth_group: Some(auth_group),
        auth_readiness,
        problem: None,
    }
}

fn is_fully_qualified_provider_model(model: &str) -> bool {
    let mut segments = model.split('/');
    let Some(provider) = segments.next() else {
        return false;
    };
    if provider.is_empty()
        || provider.chars().any(char::is_whitespace)
        || provider.chars().any(char::is_control)
    {
        return false;
    }
    let mut model_segments = 0_usize;
    for segment in segments {
        model_segments += 1;
        if segment.is_empty()
            || segment.chars().any(char::is_whitespace)
            || segment.chars().any(char::is_control)
        {
            return false;
        }
    }
    model_segments > 0
}

#[cfg(feature = "test-seams")]
fn conformance_adapter_controls() -> Result<Option<ConformanceAdapterControls>, RunnerError> {
    let mode = env::var_os(CONFORMANCE_ADAPTER_MODE);
    let executable = env::var_os(CONFORMANCE_ADAPTER_PROBE);
    let observation = env::var_os(CONFORMANCE_ADAPTER_OBSERVATION);
    let credential_group = env::var(CONFORMANCE_ADAPTER_CREDENTIAL_GROUP).ok();
    let cleanup_failure = env::var_os(CONFORMANCE_ADAPTER_CLEANUP_FAILURE);
    if mode.is_none()
        && executable.is_none()
        && observation.is_none()
        && credential_group.is_none()
        && cleanup_failure.is_none()
    {
        return Ok(None);
    }
    if mode.as_deref() != Some(std::ffi::OsStr::new(CONFORMANCE_ADAPTER_MODE_VALUE))
        || executable.is_none()
        || observation.is_none()
        || credential_group.as_deref().is_none_or(str::is_empty)
        || cleanup_failure
            .as_deref()
            .is_some_and(|value| value != std::ffi::OsStr::new("1"))
    {
        return Err(RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail(
            "reason",
            "the internal provider-free adapter seam requires all conformance controls",
        ));
    }
    let executable = PathBuf::from(executable.expect("checked conformance executable"));
    let observation = PathBuf::from(observation.expect("checked conformance observation"));
    if !executable.is_absolute() || !observation.is_absolute() {
        return Err(RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail(
            "reason",
            "the internal provider-free adapter seam requires absolute paths",
        ));
    }
    Ok(Some(ConformanceAdapterControls {
        executable,
        observation,
        credential_group: credential_group.expect("checked conformance credential group"),
        cleanup_failure: cleanup_failure.is_some(),
    }))
}

#[cfg(feature = "test-seams")]
#[allow(clippy::too_many_lines)]
fn execute_installed_adapter_probe(
    adapter: installed_adapters::InstalledAdapter,
    controls: &ConformanceAdapterControls,
    argv: &[String],
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
    human_stream: Option<&mut dyn IoWrite>,
) -> CommandOutcome {
    execute_installed_adapter(
        adapter,
        Some(controls),
        argv,
        config,
        image,
        mode,
        clock,
        ids,
        cancellation,
        human_stream,
    )
}

#[allow(clippy::too_many_lines, clippy::too_many_arguments)]
fn execute_installed_adapter(
    adapter: installed_adapters::InstalledAdapter,
    #[cfg(feature = "test-seams")] controls: Option<&ConformanceAdapterControls>,
    #[cfg(not(feature = "test-seams"))] _controls: Option<&()>,
    argv: &[String],
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
    human_stream: Option<&mut dyn IoWrite>,
) -> CommandOutcome {
    if let Err(error) = installed_adapters::assert_platform_supported(
        adapter,
        installed_adapters::HostPlatform::current(),
    ) {
        return forward_error_outcome(error, argv, config, image, mode, clock, ids);
    }
    let invocation_id = ids.next_invocation_id();
    let timestamp = clock.now_rfc3339();
    let task = json!({
        "schema":image.manifest.task_envelope.schema_id,
        "argv":argv,
        "interactionMode":"non-interactive"
    });
    let task_bytes = serde_json::to_vec(&task).expect("task JSON");
    let task_digest = sha256_hex(&task_bytes);
    let image_bytes = image.model_visible_bytes();
    #[cfg(feature = "test-seams")]
    let (executable, mut environment, detected_version, auth_group, probe_before_run) =
        if let Some(controls) = controls {
            let environment = match installed_adapters::environment_policy(
                adapter,
                &controls.credential_group,
                env::vars_os(),
            ) {
                Ok(environment) => environment,
                Err(error) => {
                    return forward_error_outcome(error, argv, config, image, mode, clock, ids);
                }
            };
            (
                controls.executable.clone(),
                environment
                    .set(
                        "OPENPROSE_ADAPTER_OBSERVATION_PATH",
                        controls.observation.as_os_str(),
                        Sensitivity::Public,
                    )
                    .set(
                        "OPENPROSE_ADAPTER_EXPECTED_ID",
                        adapter.id(),
                        Sensitivity::Public,
                    ),
                None,
                controls.credential_group.clone(),
                false,
            )
        } else {
            let discovery = inspect_installed_adapter(adapter, config);
            if let Some(error) = discovery.problem {
                return forward_error_outcome(error, argv, config, image, mode, clock, ids);
            }
            (
                discovery.executable.expect("ready adapter has executable"),
                discovery
                    .environment
                    .expect("ready adapter has environment"),
                discovery.version,
                discovery.auth_group.expect("ready adapter has auth group"),
                false,
            )
        };
    #[cfg(not(feature = "test-seams"))]
    let (executable, mut environment, detected_version, auth_group, probe_before_run) = {
        let discovery = inspect_installed_adapter(adapter, config);
        if let Some(error) = discovery.problem {
            return forward_error_outcome(error, argv, config, image, mode, clock, ids);
        }
        (
            discovery.executable.expect("ready adapter has executable"),
            discovery
                .environment
                .expect("ready adapter has environment"),
            discovery.version,
            discovery.auth_group.expect("ready adapter has auth group"),
            false,
        )
    };
    let _ = &mut environment;
    let mut launch = match installed_adapters::prepare_launch(
        adapter,
        executable,
        &config.cwd,
        &image_bytes,
        &image.one_field_framing,
        &task_bytes,
        &invocation_id,
        config.model.value.as_deref(),
        &auth_group,
        environment,
    ) {
        Ok(launch) => launch,
        Err(error) => {
            return forward_error_outcome(error, argv, config, image, mode, clock, ids);
        }
    };
    launch.argv.splice(0..0, sdk_limit_arguments(config));
    if config.native_profile.value != "default" {
        if let Err(error)=launch.apply_workspace_profile(&auth_group,&config.native_add_dirs.value,&config.native_allow_tools.value) {
            return forward_error_outcome(error,argv,config,image,mode,clock,ids);
        }
    }
    if let Some(permission)=&config.permission_mode.value {
        if adapter == installed_adapters::InstalledAdapter::ClaudePrintStreamJson && matches!(permission.as_str(),"default"|"acceptEdits") {launch.argv.splice(0..0,["--permission-mode".into(),permission.into()]);}
        else if adapter == installed_adapters::InstalledAdapter::CodexExecJson && matches!(permission.as_str(),"workspace-write"|"read-only") {launch.argv.splice(1..1,["--sandbox".into(),permission.into()]);}
        else {return forward_error_outcome(RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail("reason","Explicit permission mode unsupported for this harness"),argv,config,image,mode,clock,ids);}
    }
    let rendered_payload_digest =
        matches!(adapter, installed_adapters::InstalledAdapter::CodexExecJson).then(|| {
            sha256_hex(
                launch
                    .stdin
                    .as_deref()
                    .expect("framed adapter always has stdin"),
            )
        });
    let timeout = parse_duration(&config.timeout.value).unwrap_or(Duration::from_secs(600));
    let native_prime=adapter == installed_adapters::InstalledAdapter::PrimeRpc && config.output_contract.value == "native";
    let mut process_spec = launch.process_spec(
        config.cwd.clone(),
        env::current_exe().ok(),
        &invocation_id,
        timeout,
        probe_before_run,
        cancellation.clone(),
    );
    let prime_controller=if native_prime {
        let prompt=process_spec.stdin.take();
        process_spec.stdin=Some(format!("{}\n",json!({"id":format!("{invocation_id}.prime.state.1"),"type":"get_state"})).into_bytes());
        process_spec.stdin_lifecycle=prose_process_supervisor::StdinLifecycle::CloseAfterTerminalEvent;
        Some(PrimeStagedController{id:invocation_id.clone(),prompt,pending_write:None,records:Vec::new(),close:false})
    } else {None};
    let mut secret_values = process_spec.environment.secret_strings();
    secret_values.extend([
        process_spec.recursion_token.clone(),
        process_spec.run_nonce.clone(),
    ]);
    secret_values
        .sort_unstable_by(|left, right| right.len().cmp(&left.len()).then_with(|| left.cmp(right)));
    secret_values.dedup();
    let mut protected = process_spec.environment.output_protected_strings();
    protected.extend(argv.iter().filter(|value| !value.is_empty()).cloned());
    protected.extend(
        config
            .model
            .value
            .iter()
            .filter(|value| !value.is_empty())
            .cloned(),
    );
    let cwd_literal = config.cwd.display().to_string();
    if !cwd_literal.is_empty() {
        protected.push(cwd_literal);
    }
    protected.push(String::from_utf8(task_bytes.clone()).expect("task JSON is UTF-8"));
    if let Ok(image_literal) = std::str::from_utf8(&image_bytes) {
        protected.push(image_literal.to_owned());
    }
    protected.push(invocation_id.clone());
    protected.push(process_spec.recursion_token.clone());
    protected.push(process_spec.run_nonce.clone());
    protected.sort();
    protected.dedup();
    let human_stream = human_stream
        .filter(|_| mode == OutputMode::Human && !native_prime && !(adapter == installed_adapters::InstalledAdapter::ClaudePrintStreamJson && config.output_contract.value == "native"))
        .map(|sink| InstalledHumanStream::new(adapter, &invocation_id, sink, protected));
    let omp_controller = launch
        .omp_prompt_bytes()
        .map(|bytes| OmpStagedController::new(&invocation_id, bytes.to_vec()));
    if config.output_contract.value == "native" { process_spec.limits.max_stdout_bytes = crate::config::native_output_bytes(config); }
    let capture=match config.native_log.value.as_ref().map(|path|NativeCapture::open(path,secret_values.clone(),crate::config::native_output_bytes(config))).transpose(){
       Ok(value)=>value,Err(_)=>return forward_error_outcome(RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail("reason","Native log must be a new writable absolute path"),argv,config,image,mode,clock,ids)
    };
    let mut run_observer = InstalledRunObserver {
        require_api_source:config.native_profile.value != "default" && auth_group=="anthropic-api-key",
        auth_source_failed:false,
        sdk: adapter == installed_adapters::InstalledAdapter::AgentsSdkJsonl,
        native_failure: None,
        capture,
        human: human_stream,
        omp: omp_controller,
        prime: prime_controller,
    };
    let native_claude=adapter == installed_adapters::InstalledAdapter::ClaudePrintStreamJson && config.output_contract.value == "native";
    let mut installed_protocol=adapter.protocol();
    installed_protocol.terminal_is_candidate=native_claude || native_prime;
    if native_prime {installed_protocol.allowed_events.insert("response".to_owned());}
    let supervised = if run_observer.prime.is_some() || run_observer.sdk || run_observer.require_api_source || run_observer.human.is_some() || run_observer.omp.is_some() || run_observer.capture.is_some() {
        supervise_observed(process_spec, &installed_protocol, &mut run_observer)
    } else {
        supervise(process_spec, &installed_protocol)
    };
    #[cfg(feature = "test-seams")]
    let inject_private_cleanup_failure = controls.is_some_and(|value| value.cleanup_failure);
    #[cfg(not(feature = "test-seams"))]
    let inject_private_cleanup_failure = false;
    if adapter == installed_adapters::InstalledAdapter::PrimeRpc {
        let settlement = settle_prime_owned_service(
            launch
                .daemon_directory()
                .expect("Prime launch retains its private daemon directory"),
            launch
                .daemon_socket_path()
                .expect("Prime launch has an exact private socket path"),
            detected_version.as_deref(),
            SettlementPolicy::default(),
        );
        if let Err(error) = settlement {
            let recovery = prepare_prime_recovery(
                &std::env::temp_dir(),
                launch
                    .daemon_directory()
                    .expect("Prime launch retains its private daemon directory"),
                launch
                    .daemon_socket_path()
                    .expect("Prime launch has an exact private socket path"),
                detected_version.as_deref(),
            )
            .ok()
            .flatten();
            let mut error = error
                .with_detail("adapterId", adapter.id())
                .with_detail("fallbackAttempted", false);
            if let Some(recovery) = recovery {
                launch.preserve_daemon_files();
                error = with_recovery_detail(error, &recovery.handle);
            } else if let Err(cleanup_error) =
                finalize_installed_private_files(&mut launch, inject_private_cleanup_failure)
            {
                error = cleanup_error;
            }
            return match supervised {
                Ok(outcome) => installed_adapter_postprocess_failure(
                    adapter,
                    &task,
                    &task_digest,
                    &invocation_id,
                    outcome,
                    detected_version.as_deref(),
                    error,
                    config,
                    image,
                    mode,
                    clock,
                ),
                Err(failure) => installed_adapter_cleanup_failure_result(
                    adapter,
                    &task,
                    &task_digest,
                    &invocation_id,
                    failure,
                    detected_version.as_deref(),
                    error,
                    config,
                    image,
                    mode,
                    clock,
                ),
            };
        }
    }
    if let Err(error) =
        finalize_installed_private_files(&mut launch, inject_private_cleanup_failure)
    {
        return match supervised {
            Ok(outcome) => installed_adapter_postprocess_failure(
                adapter,
                &task,
                &task_digest,
                &invocation_id,
                outcome,
                detected_version.as_deref(),
                error,
                config,
                image,
                mode,
                clock,
            ),
            Err(failure) => installed_adapter_cleanup_failure_result(
                adapter,
                &task,
                &task_digest,
                &invocation_id,
                failure,
                detected_version.as_deref(),
                error,
                config,
                image,
                mode,
                clock,
            ),
        };
    }
    let outcome = match supervised {
        Ok(outcome) => outcome,
        Err(failure) => {
            if run_observer.auth_source_failed {
                return installed_adapter_cleanup_failure_result(adapter,&task,&task_digest,&invocation_id,failure,detected_version.as_deref(),RunnerError::catalog(ErrorCode::HarnessNeedsAuth).with_detail("reason","Native init did not confirm selected API credential route").with_detail("fallbackAttempted",false),config,image,mode,clock);
            }
            return installed_adapter_failure_result(
                adapter,
                &task,
                &task_digest,
                &invocation_id,
                failure,
                run_observer.native_failure.clone(),
                detected_version.as_deref(),
                config,
                image,
                mode,
                clock,
            );
        }
    };
    if let Err(error)=validate_native_auth(config,&outcome.records) {
        return installed_adapter_postprocess_failure(adapter,&task,&task_digest,&invocation_id,outcome,detected_version.as_deref(),error,config,image,mode,clock);
    }
    let normalized =
        match installed_adapters::normalize_transport_mode(adapter, &outcome.records, &invocation_id,native_claude || native_prime) {
            Ok(normalized) => normalized,
            Err(error) => {
                return installed_adapter_postprocess_failure(
                    adapter,
                    &task,
                    &task_digest,
                    &invocation_id,
                    outcome,
                    detected_version.as_deref(),
                    error,
                    config,
                    image,
                    mode,
                    clock,
                );
            }
        };
    let terminal = if config.output_contract.value == "native" {
        native_output(&normalized.assistant_messages)
    } else {
        match installed_adapters::recover_terminal(image, &normalized.assistant_messages, argv) {
            Ok(terminal) => terminal,
            Err(error) => {
                return installed_adapter_postprocess_failure(
                    adapter,
                    &task,
                    &task_digest,
                    &invocation_id,
                    outcome,
                    detected_version.as_deref(),
                    error,
                    config,
                    image,
                    mode,
                    clock,
                );
            }
        }
    };
    let human_stream_settlement = match run_observer.human.as_ref().map_or(
        Ok(HumanStreamSettlement {
            emitted_prefix_bytes: 0,
            stalled: false,
        }),
        |stream| stream.settlement(&terminal),
    ) {
        Ok(length) => length,
        Err(error) => {
            return installed_adapter_postprocess_failure(
                adapter,
                &task,
                &task_digest,
                &invocation_id,
                outcome,
                detected_version.as_deref(),
                error,
                config,
                image,
                mode,
                clock,
            );
        }
    };
    installed_adapter_success_result(
        adapter,
        &task,
        &task_digest,
        &invocation_id,
        outcome,
        detected_version.as_deref(),
        &auth_group,
        rendered_payload_digest.as_deref(),
        &terminal,
        human_stream_settlement,
        &secret_values,
        config,
        image,
        mode,
        &timestamp,
    )
}

fn finalize_installed_private_files(
    launch: &mut installed_adapters::PreparedLaunch,
    inject_failure: bool,
) -> Result<(), RunnerError> {
    if inject_failure {
        return Err(launch.private_file_cleanup_failure());
    }
    launch.finalize_private_files()
}

#[allow(clippy::too_many_arguments)]
fn installed_adapter_cleanup_failure_result(
    adapter: installed_adapters::InstalledAdapter,
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    failure: SupervisorFailure,
    detected_version: Option<&str>,
    error: RunnerError,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
) -> CommandOutcome {
    render_installed_failure(
        adapter,
        task,
        task_digest,
        invocation_id,
        error,
        failure.process_started,
        failure.terminal_observed,
        failure.process_exit,
        failure.process_signal,
        detected_version,
        failure.stderr,
        config,
        image,
        mode,
        clock,
        Some(&failure.records),
    )
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
fn installed_adapter_success_result(
    adapter: installed_adapters::InstalledAdapter,
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    outcome: ProcessOutcome,
    detected_version: Option<&str>,
    _auth_group: &str,
    rendered_payload_digest: Option<&str>,
    terminal: &installed_adapters::RecoveredTerminal,
    human_stream_settlement: HumanStreamSettlement,
    secret_values: &[String],
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    timestamp: &str,
) -> CommandOutcome {
    let harness_version = detected_version
        .map(ToOwned::to_owned)
        .or(outcome.probed_version.clone());
    let invocation = installed_invocation(adapter, task, task_digest, invocation_id, config, image);
    let invocation_digest = sha256_hex(&serde_json::to_vec(&invocation).expect("invocation JSON"));
    let mut event_values = vec![
        event(
            0,
            timestamp,
            invocation_id,
            "runner.started",
            &json!({"kind":"runner.started","runnerName":RUNNER_NAME,"runnerVersion":RUNNER_VERSION}),
        ),
        event(
            1,
            timestamp,
            invocation_id,
            "harness.started",
            &json!({"kind":"harness.started","harness":adapter.harness(),"transport":adapter.transport(),"harnessVersion":harness_version}),
        ),
    ];
    let public_messages =
        redact_exact_secrets_across_messages(&terminal.visible_messages, secret_values);
    for visible_message in &public_messages {
        let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
        event_values.push(event(
            sequence,
            timestamp,
            invocation_id,
            "assistant.message",
            &json!({"kind":"assistant.message","text":visible_message}),
        ));
    }
    let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
    event_values.push(event(
        sequence,
        timestamp,
        invocation_id,
        "harness.completed",
        &json!({"kind":"harness.completed","terminalEventObserved":true,"exitCode":outcome.process_exit,"signal":outcome.process_signal}),
    ));
    let mut event_bytes = Vec::new();
    for value in &event_values {
        event_bytes.extend(serde_json::to_vec(value).expect("event JSON"));
        event_bytes.push(b'\n');
    }
    let terminal_digest = if terminal.envelope.is_null() { None } else {
        Some(sha256_hex(&serde_json::to_vec(&terminal.envelope).expect("terminal envelope JSON")))
    };
    let mut result = json!({
        "schema":"openprose.runner-result/1",
        "invocationId":invocation_id,
        "runner":{"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "adapter":{"id":adapter.id(),"harnessVersion":harness_version,"descriptorDigestSha256":sha256_hex(adapter.recipe_json().as_bytes())},
        "transport":adapter.transport(),
        "negotiatedCapabilities":{"promptPlacement":adapter.prompt_placement(),"isolation":adapter.isolation_guarantee(),"streaming":"structured","cancellation":containment_label(outcome.containment),"terminal":"structured"},
        "languageImage":{"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "digests":{"invocationSha256":invocation_digest,"taskSha256":task_digest,"normalizedEventsSha256":sha256_hex(&event_bytes),"deliveredImageSha256":image.manifest.model_visible_bytes.sha256,"renderedPayloadSha256":rendered_payload_digest},
        "cwd":{"path":config.cwd.display().to_string(),"identitySha256":sha256_hex(config.cwd.as_os_str().to_string_lossy().as_bytes())},
        "timing":{"startedAt":timestamp,"firstEventAt":timestamp,"cancellationAt":null,"terminalAt":timestamp,"durationMs":u64::try_from(outcome.duration.as_millis()).unwrap_or(u64::MAX)},
        "terminal":{"classification":"success","transportCompleted":true,"terminalEventObserved":true,"exitCode":outcome.process_exit,"signal":outcome.process_signal},
        "semantic":{"status":if terminal.envelope.is_null() {"not-applicable"} else {terminal.envelope.get("semanticStatus").and_then(Value::as_str).unwrap_or("unknown")},"terminalSchemaSha256":image.manifest.terminal_envelope.sha256,"terminalEnvelopeDigestSha256":terminal_digest},
        "usage":{"status":"unavailable"},
        "billing":{"owner":"user-provider","authCategory":"harness-managed"},
        "diagnosticRefs":[],
        "runnerExitCode":0
    });
    if let Some(limits)=crate::config::native_limits(config){result["nativeLimits"]=limits;}
    if let Some(limits)=crate::config::native_output_limits(config){result["nativeOutputLimits"]=limits;}
    if let Some(native)=native_configuration(config,Some(&outcome.records)){result["nativeConfiguration"]=native;}
    match mode {
        OutputMode::Human => {
            let remaining = &terminal.visible_text[human_stream_settlement.emitted_prefix_bytes..];
            let remaining = redact_exact_secrets(remaining, secret_values);
            let stdout = if human_stream_settlement.stalled
                && human_stream_settlement.emitted_prefix_bytes == 0
            {
                "OpenProse completed; harness output was withheld by the human-output safety policy.\n"
                    .to_owned()
            } else if human_stream_settlement.stalled {
                // A safe prefix was already written. Finish the response with
                // a runner-owned notice without rendering withheld bytes.
                let emitted =
                    &terminal.visible_text[..human_stream_settlement.emitted_prefix_bytes];
                let separator = if emitted.ends_with('\n') { "" } else { "\n" };
                format!(
                    "{separator}OpenProse completed; the remaining harness output was withheld by the human-output safety policy.\n"
                )
            } else if terminal.visible_text.is_empty() {
                "Harness completed the nonsemantic echo placeholder.\n".to_owned()
            } else if remaining.is_empty() {
                "\n".to_owned()
            } else {
                format!("{}\n", human_safe_multiline(&remaining))
            };
            CommandOutcome::human(stdout, human_safe_multiline(&outcome.stderr), 0)
        }
        OutputMode::Json => CommandOutcome::json_with_diagnostic(result, outcome.stderr, 0),
        OutputMode::Jsonl => {
            let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
            event_values.push(event(
                sequence,
                timestamp,
                invocation_id,
                "runner.completed",
                &json!({"kind":"runner.completed","result":result}),
            ));
            CommandOutcome::jsonl_with_diagnostic(event_values, outcome.stderr, 0)
        }
    }
}

fn redact_exact_secrets(value: &str, secrets: &[String]) -> String {
    let mut sanitized = value.to_owned();
    let mut ordered = secrets
        .iter()
        .filter(|secret| !secret.is_empty())
        .collect::<Vec<_>>();
    ordered.sort_by(|left, right| right.len().cmp(&left.len()).then_with(|| left.cmp(right)));
    ordered.dedup();
    for secret in ordered {
        sanitized = sanitized.replace(secret, "[REDACTED]");
    }
    sanitized
}

fn redact_exact_secrets_across_messages(messages: &[String], secrets: &[String]) -> Vec<String> {
    let combined = messages.concat();
    let mut protected = vec![false; combined.len()];
    let mut ordered = secrets
        .iter()
        .filter(|secret| !secret.is_empty())
        .collect::<Vec<_>>();
    ordered.sort_by(|left, right| right.len().cmp(&left.len()).then_with(|| left.cmp(right)));
    ordered.dedup();
    for secret in ordered {
        for (start, _) in combined.match_indices(secret) {
            protected[start..start + secret.len()].fill(true);
        }
    }

    let mut offset = 0;
    messages
        .iter()
        .map(|message| {
            let bytes = message.as_bytes();
            let mask = &protected[offset..offset + bytes.len()];
            let mut rendered = Vec::with_capacity(bytes.len());
            let mut inside_redaction = false;
            for (byte, is_protected) in bytes.iter().zip(mask) {
                if *is_protected {
                    if !inside_redaction {
                        rendered.extend_from_slice(b"[REDACTED]");
                        inside_redaction = true;
                    }
                } else {
                    rendered.push(*byte);
                    inside_redaction = false;
                }
            }
            offset += bytes.len();
            String::from_utf8(rendered).expect("redaction preserves UTF-8 boundaries")
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn installed_adapter_postprocess_failure(
    adapter: installed_adapters::InstalledAdapter,
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    outcome: ProcessOutcome,
    detected_version: Option<&str>,
    error: RunnerError,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
) -> CommandOutcome {
    render_installed_failure(
        adapter,
        task,
        task_digest,
        invocation_id,
        error,
        true,
        true,
        outcome.process_exit.into(),
        outcome.process_signal,
        detected_version.or(outcome.probed_version.as_deref()),
        outcome.stderr,
        config,
        image,
        mode,
        clock,
        Some(&outcome.records),
    )
}

#[allow(clippy::too_many_arguments)]
fn installed_adapter_failure_result(
    adapter: installed_adapters::InstalledAdapter,
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    failure: SupervisorFailure,
    native_failure: Option<Value>,
    detected_version: Option<&str>,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
) -> CommandOutcome {
    let mut error = map_supervisor_failure(&failure)
        .with_detail("adapterId", adapter.id())
        .with_detail("fallbackAttempted", false);
    if adapter == installed_adapters::InstalledAdapter::PrimeRpc
        && matches!(
            failure.kind,
            FailureKind::ProtocolMalformed | FailureKind::ProtocolTruncated
        )
    {
        error = error.with_detail(
            "adapterDiagnostic",
            installed_adapters::prime_parser_diagnostic(
                &failure.records,
                invocation_id,
                failure.protocol_failure_is_framing(),
            ),
        );
    }
    if adapter == installed_adapters::InstalledAdapter::AgentsSdkJsonl {
        if let Some(record)=failure.records.iter().rev().find(|r|r.get("type").and_then(Value::as_str)==Some("error")) {
            error=error.with_detail("nativeFailure",installed_adapters::sdk_native_failure(record));
        }
    }
    if let Some(diagnostic)=native_failure {error=error.with_detail("nativeFailure",diagnostic);}
    render_installed_failure(
        adapter,
        task,
        task_digest,
        invocation_id,
        error,
        failure.process_started,
        failure.terminal_observed,
        failure.process_exit,
        failure.process_signal,
        detected_version,
        failure.stderr,
        config,
        image,
        mode,
        clock,
        Some(&failure.records),
    )
}

#[allow(
    clippy::needless_pass_by_value,
    clippy::too_many_arguments,
    clippy::too_many_lines
)]
fn render_installed_failure(
    adapter: installed_adapters::InstalledAdapter,
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    error: RunnerError,
    process_started: bool,
    terminal_observed: bool,
    process_exit: Option<i32>,
    process_signal: Option<String>,
    harness_version: Option<&str>,
    diagnostic: String,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    native_records:Option<&[Value]>,
) -> CommandOutcome {
    let timestamp = clock.now_rfc3339();
    let invocation = installed_invocation(adapter, task, task_digest, invocation_id, config, image);
    let invocation_digest = sha256_hex(&serde_json::to_vec(&invocation).expect("invocation JSON"));
    let mut events = Vec::new();
    if process_started {
        events.push(event(
            0,
            &timestamp,
            invocation_id,
            "runner.started",
            &json!({"kind":"runner.started","runnerName":RUNNER_NAME,"runnerVersion":RUNNER_VERSION}),
        ));
        events.push(event(
            1,
            &timestamp,
            invocation_id,
            "harness.started",
            &json!({"kind":"harness.started","harness":adapter.harness(),"transport":adapter.transport(),"harnessVersion":harness_version}),
        ));
    }
    let mut event_bytes = Vec::new();
    for value in &events {
        event_bytes.extend(serde_json::to_vec(value).expect("event JSON"));
        event_bytes.push(b'\n');
    }
    let exit_code = error.exit_code;
    let mut result = json!({
        "schema":"openprose.runner-result/1",
        "invocationId":invocation_id,
        "runner":{"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "adapter":{"id":adapter.id(),"harnessVersion":harness_version,"descriptorDigestSha256":sha256_hex(adapter.recipe_json().as_bytes())},
        "transport":adapter.transport(),
        "negotiatedCapabilities":{"promptPlacement":adapter.prompt_placement(),"isolation":adapter.isolation_guarantee(),"streaming":"structured","cancellation":"process-group-best-effort","terminal":"structured"},
        "languageImage":{"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "digests":{"invocationSha256":invocation_digest,"taskSha256":task_digest,"normalizedEventsSha256":sha256_hex(&event_bytes),"deliveredImageSha256":if process_started { Some(image.manifest.model_visible_bytes.sha256.as_str()) } else { None },"renderedPayloadSha256":null},
        "cwd":{"path":config.cwd.display().to_string(),"identitySha256":sha256_hex(config.cwd.as_os_str().to_string_lossy().as_bytes())},
        "timing":{"startedAt":timestamp,"firstEventAt":if process_started { Some(timestamp.as_str()) } else { None },"cancellationAt":if error.code == ErrorCode::Cancelled { Some(timestamp.as_str()) } else { None },"terminalAt":timestamp,"durationMs":0},
        "terminal":{"classification":if error.code == ErrorCode::Cancelled { "cancelled" } else if error.code == ErrorCode::HarnessFailed && process_exit.is_some() { "exit-code" } else { "runner-error" },"transportCompleted":terminal_observed,"terminalEventObserved":terminal_observed,"exitCode":process_exit,"signal":process_signal},
        "semantic":{"status":"unknown","terminalSchemaSha256":image.manifest.terminal_envelope.sha256,"terminalEnvelopeDigestSha256":null},
        "usage":{"status":"unavailable"},
        "billing":{"owner":"user-provider","authCategory":"harness-managed"},
        "diagnosticRefs":[],
        "runnerExitCode":exit_code,
        "error":error
    });
    if let Some(limits)=crate::config::native_limits(config){result["nativeLimits"]=limits;}
    if let Some(limits)=crate::config::native_output_limits(config){result["nativeOutputLimits"]=limits;}
    if let Some(native)=native_configuration(config,native_records){result["nativeConfiguration"]=native;}
    match mode {
        OutputMode::Human => CommandOutcome::human(
            "",
            format!("{error}\n{}", human_safe_multiline(&diagnostic)),
            exit_code,
        ),
        OutputMode::Json => CommandOutcome::json_with_diagnostic(result, diagnostic, exit_code),
        OutputMode::Jsonl => {
            let sequence = u64::try_from(events.len()).expect("event count fits u64");
            events.push(event(
                sequence,
                &timestamp,
                invocation_id,
                "runner.failed",
                &json!({"kind":"runner.failed","error":error}),
            ));
            CommandOutcome::jsonl_with_diagnostic(events, diagnostic, exit_code)
        }
    }
}

fn native_configuration(config:&EffectiveConfig,records:Option<&[Value]>)->Option<Value>{
    if config.native_profile.value == "default" {return None;}
    let observed=records.and_then(|r|r.iter().find(|v|v["type"]=="system" && v["subtype"]=="init"))
        .map(|v|json!({"tools":v.get("tools").cloned().unwrap_or(Value::Null),"apiKeySource":v.get("apiKeySource").cloned().unwrap_or(Value::Null)}));
    Some(json!({"profile":config.native_profile.value,"toolsRequested":["Read","Write","Edit","Glob","Grep","Agent","Bash"],
        "additionalDirectories":config.native_add_dirs.value,"allowedToolRules":config.native_allow_tools.value,
        "permissionMode":config.permission_mode.value,"authProfile":config.auth_profile.value.as_deref().unwrap_or("claude-subscription"),
        "configOwnership":if config.auth_profile.value.as_deref()==Some("anthropic-api-key"){"runner-private"}else{"native-auth-store"},"observed":observed}))
}
fn validate_native_auth(config:&EffectiveConfig,records:&[Value])->Result<(),RunnerError>{
    if config.native_profile.value != "default" && config.auth_profile.value.as_deref()==Some("anthropic-api-key") {
        let inits:Vec<_>=records.iter().filter(|v|v["type"]=="system" && v["subtype"]=="init").collect();
        if inits.is_empty() || inits.iter().any(|v|v["apiKeySource"]!="ANTHROPIC_API_KEY") {
            return Err(RunnerError::catalog(ErrorCode::HarnessNeedsAuth).with_detail("reason","Native init did not confirm the selected ANTHROPIC_API_KEY route").with_detail("fallbackAttempted",false));
        }
    }
    Ok(())
}

fn installed_invocation(
    adapter: installed_adapters::InstalledAdapter,
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    config: &EffectiveConfig,
    image: &RuntimeImage,
) -> Value {
    let mut value=json!({
        "schema":"openprose.runner-invocation/1",
        "invocationId":invocation_id,
        "cwd":config.cwd.display().to_string(),
        "languageImage":{"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "runner":{"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "harness":adapter.harness(),
        "transport":adapter.transport(),
        "recursionToken":format!("installed-recursion-{invocation_id}"),
        "task":task,
        "taskDigestSha256":task_digest
    });
    if let Some(limits)=crate::config::native_limits(config){value["nativeLimits"]=limits;}
    if let Some(limits)=crate::config::native_output_limits(config){value["nativeOutputLimits"]=limits;}
    if let Some(native)=native_configuration(config,None){value["nativeConfiguration"]=native;}
    value
}

const fn containment_label(containment: ContainmentClaim) -> &'static str {
    match containment {
        ContainmentClaim::UnixProcessGroupBestEffort => "process-group-best-effort",
        ContainmentClaim::WindowsNativeProcessHost => "windows-job-object",
        ContainmentClaim::Unsupported => "unsupported",
    }
}

fn sentinel_contract_error() -> RunnerError {
    RunnerError::catalog(ErrorCode::TransportUnsupported).with_detail(
        "reason",
        "the mock adapter requires a release-ineligible `sentinel-transport-test` image whose closed terminal schema const-authorizes `not-applicable`",
    )
}

fn sentinel_terminal_envelope(image: &RuntimeImage) -> Result<Value, RunnerError> {
    if image.manifest.purpose != "sentinel-transport-test" || image.manifest.release_eligible {
        return Err(mock_unavailable("release-image"));
    }
    let schema: Value = serde_json::from_slice(&image.terminal_envelope_schema)
        .map_err(|_| sentinel_contract_error())?;
    let properties = schema["properties"]
        .as_object()
        .ok_or_else(sentinel_contract_error)?;
    let required = schema["required"]
        .as_array()
        .ok_or_else(sentinel_contract_error)?;
    let exact_fields = ["schema", "semanticStatus", "marker"];
    let closed_shape = schema["type"] == "object"
        && schema["additionalProperties"] == false
        && properties.len() == exact_fields.len()
        && exact_fields
            .iter()
            .all(|field| properties.contains_key(*field))
        && required.len() == exact_fields.len()
        && exact_fields.iter().all(|field| {
            required
                .iter()
                .any(|required_field| required_field.as_str() == Some(*field))
        });
    if !closed_shape {
        return Err(sentinel_contract_error());
    }
    let schema_id = properties["schema"]["const"]
        .as_str()
        .ok_or_else(sentinel_contract_error)?;
    let semantic_status = properties["semanticStatus"]["const"]
        .as_str()
        .ok_or_else(sentinel_contract_error)?;
    let marker = properties["marker"]["const"]
        .as_str()
        .ok_or_else(sentinel_contract_error)?;
    if schema_id != image.manifest.terminal_envelope.schema_id
        || semantic_status != "not-applicable"
        || marker.is_empty()
    {
        return Err(sentinel_contract_error());
    }
    Ok(json!({
        "schema":schema_id,
        "semanticStatus":semantic_status,
        "marker":marker
    }))
}

fn mock_unavailable(admission_block: &'static str) -> RunnerError {
    RunnerError::catalog(ErrorCode::HarnessUnavailable)
        .with_detail("harness", "mock")
        .with_detail("admissionStatus", "blocked")
        .with_detail("admissionBlock", admission_block)
}

fn mock_admission_error(image: &RuntimeImage) -> Option<RunnerError> {
    if !TEST_SEAMS_ENABLED {
        return Some(
            mock_unavailable("test-seams-disabled").with_detail("fallbackAttempted", false),
        );
    }
    sentinel_terminal_envelope(image).err()
}

fn execute_mock(
    argv: &[String],
    dry_run: bool,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
) -> CommandOutcome {
    #[cfg(not(feature = "test-seams"))]
    let _ = cancellation;
    let transport = match config.transport.value.as_str() {
        "auto" | "deterministic" => "deterministic",
        "fake-process" => "fake-process",
        unsupported => {
            return forward_error_outcome(
                RunnerError::catalog(ErrorCode::TransportUnsupported)
                    .with_detail("transport", unsupported.to_owned()),
                argv,
                config,
                image,
                mode,
                clock,
                ids,
            );
        }
    };
    let task = json!({
        "schema": image.manifest.task_envelope.schema_id,
        "argv": argv,
        "interactionMode": "non-interactive"
    });
    let task_bytes = serde_json::to_vec(&task).expect("JSON task serialization");
    let task_digest = sha256_hex(&task_bytes);
    let terminal_envelope = match sentinel_terminal_envelope(image) {
        Ok(terminal_envelope) => terminal_envelope,
        Err(error) => {
            return forward_error_outcome(error, argv, config, image, mode, clock, ids);
        }
    };

    if transport == "fake-process" {
        #[cfg(not(feature = "test-seams"))]
        {
            return forward_error_outcome(
                RunnerError::catalog(ErrorCode::TransportUnsupported).with_detail(
                    "reason",
                    "the internal fake-process transport is unavailable in release builds",
                ),
                argv,
                config,
                image,
                mode,
                clock,
                ids,
            );
        }
        #[cfg(feature = "test-seams")]
        {
            if dry_run {
                return dry_run_outcome(
                    config,
                    image,
                    transport,
                    "mock/fake-process",
                    Some("1.0.0"),
                    Some("developer"),
                    "strict",
                    "test-fixture",
                    "none-test-only",
                    "not-applicable",
                    "test-fixture",
                    None,
                    mode,
                );
            }
            return execute_fake_process(
                argv,
                &task,
                &task_bytes,
                &task_digest,
                config,
                image,
                mode,
                clock,
                ids,
                cancellation,
                &terminal_envelope,
            );
        }
    }

    if dry_run {
        dry_run_outcome(
            config,
            image,
            transport,
            "mock/in-memory",
            Some("1.0.0"),
            Some("developer"),
            "strict",
            "test-fixture",
            "none-test-only",
            "not-applicable",
            "test-fixture",
            None,
            mode,
        )
    } else {
        mock_result(
            &task,
            &task_digest,
            transport,
            config,
            image,
            mode,
            clock,
            ids,
            &terminal_envelope,
        )
    }
}

#[cfg(feature = "test-seams")]
#[allow(clippy::too_many_arguments)]
fn execute_fake_process(
    argv: &[String],
    task: &Value,
    task_bytes: &[u8],
    task_digest: &str,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    cancellation: &CancellationToken,
    terminal_envelope: &Value,
) -> CommandOutcome {
    let Some(executable) = env::var_os(CONFORMANCE_HARNESS).map(PathBuf::from) else {
        return forward_error_outcome(
            RunnerError::catalog(ErrorCode::TransportUnsupported).with_detail(
                "reason",
                "the fake-process transport is available only to the hermetic conformance runner",
            ),
            argv,
            config,
            image,
            mode,
            clock,
            ids,
        );
    };
    if !executable.is_absolute() {
        return forward_error_outcome(
            RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail(
                "reason",
                "the conformance fake harness must be supplied as an absolute path",
            ),
            argv,
            config,
            image,
            mode,
            clock,
            ids,
        );
    }

    let image_bytes: Vec<u8> = image
        .payload
        .iter()
        .flat_map(|entry| entry.bytes.iter().copied())
        .collect();
    let Ok(prompts) = PrivatePromptFiles::create(&image_bytes, task_bytes) else {
        return forward_error_outcome(
            RunnerError::catalog(ErrorCode::InternalRunnerFault)
                .with_detail("reason", "cannot create private adapter prompt files"),
            argv,
            config,
            image,
            mode,
            clock,
            ids,
        );
    };

    let invocation_id = ids.next_invocation_id();
    let scenario = env::var(CONFORMANCE_SCENARIO).unwrap_or_else(|_| "success".to_owned());
    let mut process_argv: Vec<OsString> = vec![
        "run".into(),
        "--scenario".into(),
        scenario.into(),
        "--image-file".into(),
        prompts.image_path().into(),
        "--task-file".into(),
        prompts.task_path().into(),
    ];
    append_test_path_option(
        &mut process_argv,
        "--observation-file",
        CONFORMANCE_OBSERVATION,
    );
    append_test_path_option(
        &mut process_argv,
        "--descendant-pid-file",
        CONFORMANCE_DESCENDANTS,
    );
    if let Some(delay) = test_u64(CONFORMANCE_DELAY_MS) {
        process_argv.extend(["--delay-ms".into(), delay.to_string().into()]);
    }

    let run_timeout = parse_duration(&config.timeout.value).unwrap_or(Duration::from_secs(600));
    let environment = ["PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "SYSTEMROOT"]
        .into_iter()
        .fold(EnvironmentPolicy::from_current(), |policy, name| {
            policy.allow_inherited(name, Sensitivity::Public)
        });
    let spec = ProcessSpec {
        executable,
        argv: process_argv,
        cwd: config.cwd.clone(),
        wrapper_executable: env::current_exe().ok(),
        version_probe: Some(VersionProbe {
            argv: vec!["--version".into()],
            timeout: run_timeout.min(Duration::from_secs(5)),
            max_output_bytes: 4096,
            required_substring: Some("1.0.0".to_owned()),
            output: prose_process_supervisor::VersionProbeOutput::Stdout,
        }),
        stdin: None,
        stdin_lifecycle: prose_process_supervisor::StdinLifecycle::CloseAfterWrite,
        environment,
        invocation_id: invocation_id.clone(),
        recursion_token: format!("recursion-{invocation_id}"),
        run_nonce: format!("run-{invocation_id}"),
        startup_timeout: run_timeout.min(Duration::from_secs(30)),
        run_timeout,
        termination_grace: Duration::from_millis(250),
        limits: StreamLimits::default(),
        cancellation: cancellation.clone(),
        cancel_after_start: test_u64(CONFORMANCE_CANCEL_MS).map(Duration::from_millis),
    };
    match supervise(spec, &JsonlProtocol::fake_harness()) {
        Ok(outcome) => fake_process_result(
            task,
            task_digest,
            &invocation_id,
            outcome,
            config,
            image,
            mode,
            clock,
            terminal_envelope,
        ),
        Err(failure) => fake_process_failure_result(
            task,
            task_digest,
            &invocation_id,
            failure,
            config,
            image,
            mode,
            clock,
        ),
    }
}

#[cfg(feature = "test-seams")]
fn append_test_path_option(argv: &mut Vec<OsString>, option: &str, variable: &str) {
    if let Some(value) = env::var_os(variable) {
        argv.extend([option.into(), value]);
    }
}

#[cfg(feature = "test-seams")]
fn test_u64(variable: &str) -> Option<u64> {
    env::var(variable).ok()?.parse().ok()
}

fn parse_duration(value: &str) -> Option<Duration> {
    let (number, multiplier) = if let Some(number) = value.strip_suffix("ms") {
        (number, 1_u64)
    } else if let Some(number) = value.strip_suffix('s') {
        (number, 1_000)
    } else if let Some(number) = value.strip_suffix('m') {
        (number, 60_000)
    } else if let Some(number) = value.strip_suffix('h') {
        (number, 3_600_000)
    } else {
        return None;
    };
    number
        .parse::<u64>()
        .ok()?
        .checked_mul(multiplier)
        .map(Duration::from_millis)
}

fn map_supervisor_failure(failure: &SupervisorFailure) -> RunnerError {
    let code = match failure.kind {
        FailureKind::HarnessUnavailable => ErrorCode::HarnessUnavailable,
        FailureKind::HarnessIncompatible | FailureKind::ContainmentUnsupported => {
            ErrorCode::HarnessIncompatible
        }
        FailureKind::RecursiveInvocation => ErrorCode::RecursiveInvocation,
        FailureKind::StartupTimeout => ErrorCode::StartupTimeout,
        FailureKind::RunTimeout | FailureKind::HarnessFailed => ErrorCode::HarnessFailed,
        FailureKind::ProtocolMalformed => ErrorCode::ProtocolMalformed,
        FailureKind::ProtocolTruncated => ErrorCode::ProtocolTruncated,
        FailureKind::Cancelled => ErrorCode::Cancelled,
        FailureKind::CleanupFailed => ErrorCode::ProcessCleanupFailed,
        FailureKind::Internal => ErrorCode::InternalRunnerFault,
    };
    let mut error = RunnerError::catalog(code)
        .with_detail(
            "processExit",
            failure.process_exit.map_or(Value::Null, Value::from),
        )
        .with_detail(
            "processSignal",
            failure
                .process_signal
                .as_ref()
                .map_or(Value::Null, |signal| Value::from(signal.clone())),
        )
        .with_detail("terminalEventObserved", failure.terminal_observed);
    if let Some(diagnostic) = failure.transport_diagnostic() {
        error = error.with_detail("transportDiagnostic", diagnostic);
    }
    if matches!(failure.kind, FailureKind::ProtocolMalformed | FailureKind::ProtocolTruncated) {
        // Closed runner-authored reasons only; never echo native data or arbitrary observer errors.
        let reason=match failure.message.as_str() {
            "harness emitted a record after its terminal record" => "record_after_terminal",
            "harness emitted a malformed JSONL record" => "invalid_json",
            "harness emitted a non-object JSONL record" => "non_object_record",
            "harness emitted a record without an event type" => "missing_event_type",
            "harness emitted an out-of-order record before session start" => "record_before_start",
            "harness emitted a duplicate session-start record" => "duplicate_start",
            _ => "protocol_admission_rejected",
        };
        return error.with_detail("reason",reason).with_detail("admittedRecordCount",failure.records.len());
    }
    if failure.kind == FailureKind::HarnessFailed
        && failure.message == "unsupported_nonterminal_settlement"
    {
        error.with_detail("reason", "unsupported_nonterminal_settlement")
    } else {
        error
    }
}

fn map_installed_version_probe_failure(
    adapter: installed_adapters::InstalledAdapter,
    failure: &SupervisorFailure,
) -> RunnerError {
    if failure.kind == FailureKind::HarnessIncompatible {
        return RunnerError::catalog(ErrorCode::HarnessIncompatible)
            .with_detail("adapterId", adapter.id())
            .with_detail("admittedVersions", adapter.admitted_versions())
            .with_detail("repairCommand", adapter.repair_command())
            .with_detail("fallbackAttempted", false);
    }
    map_supervisor_failure(failure)
        .with_detail("adapterId", adapter.id())
        .with_detail("fallbackAttempted", false)
}

#[cfg(feature = "test-seams")]
#[allow(clippy::too_many_arguments)]
fn fake_process_failure_result(
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    failure: SupervisorFailure,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
) -> CommandOutcome {
    let timestamp = clock.now_rfc3339();
    let error = map_supervisor_failure(&failure);
    let exit_code = error.exit_code;
    let invocation = json!({
        "schema":"openprose.runner-invocation/1",
        "invocationId":invocation_id,
        "cwd":config.cwd.display().to_string(),
        "languageImage":{"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "runner":{"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "harness":"mock",
        "transport":"fake-process",
        "recursionToken":format!("recursion-{invocation_id}"),
        "task":task,
        "taskDigestSha256":task_digest
    });
    let invocation_digest = sha256_hex(&serde_json::to_vec(&invocation).expect("invocation JSON"));
    let descriptor: Value =
        serde_json::from_str(FAKE_PROCESS_DESCRIPTOR).expect("shared fake-process descriptor JSON");
    let descriptor_digest = sha256_hex(&serde_json::to_vec(&descriptor).expect("descriptor JSON"));
    let terminal_digest = failure
        .terminal_envelope
        .as_ref()
        .map(|terminal| sha256_hex(&serde_json::to_vec(terminal).expect("terminal envelope JSON")));
    let harness_version_observed = failure
        .records
        .iter()
        .any(|record| record["type"] == "session.started");
    let mut event_values = Vec::new();
    if failure.process_started {
        event_values.push(event(
            0,
            &timestamp,
            invocation_id,
            "runner.started",
            &json!({"kind":"runner.started","runnerName":RUNNER_NAME,"runnerVersion":RUNNER_VERSION}),
        ));
        if failure
            .records
            .iter()
            .any(|record| record["type"] == "session.started")
        {
            event_values.push(event(
                1,
                &timestamp,
                invocation_id,
                "harness.started",
                &json!({"kind":"harness.started","harness":"mock","transport":"fake-process","harnessVersion":"1.0.0"}),
            ));
        }
        for raw in &failure.records {
            if raw["type"] == "assistant.message" {
                let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
                event_values.push(event(
                    sequence,
                    &timestamp,
                    invocation_id,
                    "assistant.message",
                    &json!({"kind":"assistant.message","text":raw["text"]}),
                ));
            }
        }
    }
    let mut event_bytes = Vec::new();
    for value in &event_values {
        event_bytes.extend(serde_json::to_vec(value).expect("event JSON"));
        event_bytes.push(b'\n');
    }
    let result = json!({
        "schema":"openprose.runner-result/1",
        "invocationId":invocation_id,
        "runner":{"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "adapter":{"id":"mock/fake-process","harnessVersion":if harness_version_observed { Some("1.0.0") } else { None },"descriptorDigestSha256":descriptor_digest},
        "transport":"fake-process",
        "negotiatedCapabilities":{"promptPlacement":"developer","isolation":"test-fixture","streaming":"structured","cancellation":"process-tree","terminal":"structured"},
        "languageImage":{"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "digests":{"invocationSha256":invocation_digest,"taskSha256":task_digest,"normalizedEventsSha256":sha256_hex(&event_bytes),"deliveredImageSha256":if failure.process_started { Some(image.aggregate_sha256()) } else { None },"renderedPayloadSha256":null},
        "cwd":{"path":config.cwd.display().to_string(),"identitySha256":sha256_hex(config.cwd.as_os_str().to_string_lossy().as_bytes())},
        "timing":{"startedAt":timestamp,"firstEventAt":if failure.process_started { Some(timestamp.as_str()) } else { None },"cancellationAt":if failure.kind == FailureKind::Cancelled { Some(timestamp.as_str()) } else { None },"terminalAt":timestamp,"durationMs":0},
        "terminal":{"classification":if failure.kind == FailureKind::Cancelled { "cancelled" } else if failure.kind == FailureKind::HarnessFailed && failure.process_exit.is_some() { "exit-code" } else { "runner-error" },"transportCompleted":false,"terminalEventObserved":failure.terminal_observed,"exitCode":failure.process_exit,"signal":failure.process_signal},
        "semantic":{"status":"unknown","terminalSchemaSha256":image.manifest.terminal_envelope.sha256,"terminalEnvelopeDigestSha256":terminal_digest},
        "usage":{"status":"unavailable"},
        "billing":{"owner":"test-fixture","authCategory":"none-test-only"},
        "diagnosticRefs":[],
        "runnerExitCode":exit_code,
        "error":error
    });
    match mode {
        OutputMode::Human => CommandOutcome::human(
            "",
            format!("{error}\n{}", human_safe_multiline(&failure.stderr)),
            exit_code,
        ),
        OutputMode::Json => CommandOutcome::json_with_diagnostic(result, failure.stderr, exit_code),
        OutputMode::Jsonl => {
            let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
            event_values.push(event(
                sequence,
                &timestamp,
                invocation_id,
                "runner.failed",
                &json!({"kind":"runner.failed","error":error}),
            ));
            CommandOutcome::jsonl_with_diagnostic(event_values, failure.stderr, exit_code)
        }
    }
}

#[cfg(feature = "test-seams")]
#[allow(clippy::too_many_arguments)]
fn fake_process_result(
    task: &Value,
    task_digest: &str,
    invocation_id: &str,
    outcome: prose_process_supervisor::ProcessOutcome,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    expected_terminal_envelope: &Value,
) -> CommandOutcome {
    if outcome.terminal_envelope != *expected_terminal_envelope {
        return fake_process_failure_result(
            task,
            task_digest,
            invocation_id,
            SupervisorFailure {
                transport_diagnostic: None,
                kind: FailureKind::ProtocolMalformed,
                message: "harness terminal envelope does not match the closed image contract"
                    .to_owned(),
                stderr: outcome.stderr,
                process_exit: Some(outcome.process_exit),
                process_signal: outcome.process_signal,
                process_started: true,
                terminal_observed: true,
                terminal_envelope: Some(outcome.terminal_envelope),
                records: outcome.records,
            },
            config,
            image,
            mode,
            clock,
        );
    }
    let started_at = clock.now_rfc3339();
    let invocation = json!({
        "schema": "openprose.runner-invocation/1",
        "invocationId": invocation_id,
        "cwd": config.cwd.display().to_string(),
        "languageImage": {
            "formatVersion": image.manifest.image_format_version,
            "version": image.manifest.image_version,
            "sha256": image.aggregate_sha256()
        },
        "runner": { "name": RUNNER_NAME, "version": RUNNER_VERSION, "commit": RUNNER_COMMIT },
        "harness": "mock",
        "transport": "fake-process",
        "recursionToken": format!("recursion-{invocation_id}"),
        "task": task,
        "taskDigestSha256": task_digest
    });
    let invocation_digest = sha256_hex(&serde_json::to_vec(&invocation).expect("invocation JSON"));
    let descriptor: Value =
        serde_json::from_str(FAKE_PROCESS_DESCRIPTOR).expect("shared fake-process descriptor JSON");
    let descriptor_digest = sha256_hex(&serde_json::to_vec(&descriptor).expect("descriptor JSON"));
    let mut event_values = vec![
        event(
            0,
            &started_at,
            invocation_id,
            "runner.started",
            &json!({"kind":"runner.started","runnerName":RUNNER_NAME,"runnerVersion":RUNNER_VERSION}),
        ),
        event(
            1,
            &started_at,
            invocation_id,
            "harness.started",
            &json!({"kind":"harness.started","harness":"mock","transport":"fake-process","harnessVersion":"1.0.0"}),
        ),
    ];
    for raw in &outcome.records {
        if raw["type"] == "assistant.message" {
            let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
            event_values.push(event(
                sequence,
                &started_at,
                invocation_id,
                "assistant.message",
                &json!({"kind":"assistant.message","text":raw["text"]}),
            ));
        }
    }
    let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
    event_values.push(event(
        sequence,
        &started_at,
        invocation_id,
        "harness.completed",
        &json!({"kind":"harness.completed","terminalEventObserved":true,"exitCode":outcome.process_exit,"signal":outcome.process_signal.clone()}),
    ));
    let mut event_bytes = Vec::new();
    for value in &event_values {
        event_bytes.extend(serde_json::to_vec(value).expect("event JSON"));
        event_bytes.push(b'\n');
    }
    let terminal_digest = sha256_hex(
        &serde_json::to_vec(&outcome.terminal_envelope).expect("terminal envelope JSON"),
    );
    let result = json!({
        "schema": "openprose.runner-result/1",
        "invocationId": invocation_id,
        "runner": {"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "adapter": {"id":"mock/fake-process","harnessVersion":"1.0.0","descriptorDigestSha256":descriptor_digest},
        "transport": "fake-process",
        "negotiatedCapabilities": {"promptPlacement":"developer","isolation":"test-fixture","streaming":"structured","cancellation":"process-tree","terminal":"structured"},
        "languageImage": {"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "digests": {
            "invocationSha256":invocation_digest,
            "taskSha256":task_digest,
            "normalizedEventsSha256":sha256_hex(&event_bytes),
            "deliveredImageSha256":image.aggregate_sha256(),
            "renderedPayloadSha256":null
        },
        "cwd": {"path":config.cwd.display().to_string(),"identitySha256":sha256_hex(config.cwd.as_os_str().to_string_lossy().as_bytes())},
        "timing": {"startedAt":started_at,"firstEventAt":started_at,"cancellationAt":null,"terminalAt":started_at,"durationMs":u64::try_from(outcome.duration.as_millis()).unwrap_or(u64::MAX)},
        "terminal": {"classification":"success","transportCompleted":true,"terminalEventObserved":true,"exitCode":outcome.process_exit,"signal":outcome.process_signal},
        "semantic": {"status":"not-applicable","terminalSchemaSha256":image.manifest.terminal_envelope.sha256,"terminalEnvelopeDigestSha256":terminal_digest},
        "usage": {"status":"unavailable"},
        "billing": {"owner":"test-fixture","authCategory":"none-test-only"},
        "diagnosticRefs": [],
        "runnerExitCode": 0
    });
    match mode {
        OutputMode::Human => CommandOutcome::human(
            "Fake process transport completed. No OpenProse language semantics were evaluated.\n",
            human_safe_multiline(&outcome.stderr),
            0,
        ),
        OutputMode::Json => CommandOutcome::json_with_diagnostic(result, outcome.stderr, 0),
        OutputMode::Jsonl => {
            let sequence = u64::try_from(event_values.len()).expect("event count fits u64");
            event_values.push(event(
                sequence,
                &started_at,
                invocation_id,
                "runner.completed",
                &json!({"kind":"runner.completed","result":result}),
            ));
            CommandOutcome::jsonl_with_diagnostic(event_values, outcome.stderr, 0)
        }
    }
}

fn dry_run_outcome(
    config: &EffectiveConfig,
    image: &RuntimeImage,
    transport: &str,
    adapter_id: &str,
    runtime_version: Option<&str>,
    prompt_placement: Option<&str>,
    prompt_strictness: &str,
    isolation: &str,
    auth_category: &str,
    auth_readiness: &str,
    billing_owner: &str,
    blocking_error: Option<&RunnerError>,
    mode: OutputMode,
) -> CommandOutcome {
    let exit_code = blocking_error.as_ref().map_or(0, |error| error.exit_code);
    let readiness = if blocking_error.is_some() {
        "blocked"
    } else {
        "ready"
    };
    let mut report = json!({
        "schema":"openprose.runner-dry-run-report/1",
        "wouldStartModel":false,
        "cwd":config.cwd.display().to_string(),
        "selection":{
            "harness":config.harness.value,
            "transport":transport,
            "adapterId":adapter_id,
            "runtimeVersion":runtime_version,
            "model":config.model.value
        },
        "prompt":{"placement":prompt_placement,"strictness":prompt_strictness},
        "isolation":isolation,
        "auth":{"category":auth_category,"readiness":auth_readiness},
        "billingOwner":billing_owner,
        "languageImage":{
            "formatVersion":image.manifest.image_format_version,
            "version":image.manifest.image_version,
            "sha256":image.aggregate_sha256(),
            "releaseEligible":image.manifest.release_eligible
        },
        "configuration":config_source_entries(config),
        "readiness":readiness,
        "blockingError":blocking_error
    });
    if let Some(limits)=crate::config::native_limits(config){report["nativeLimits"]=limits;}
    if let Some(limits)=crate::config::native_output_limits(config){report["nativeOutputLimits"]=limits;}
    if let Some(native)=native_configuration(config,None){report["nativeConfiguration"]=native;}
    if mode == OutputMode::Human {
        let human_readiness =
            human_readiness_label(blocking_error.is_none(), auth_readiness, "blocked");
        let mut output = format!(
            "Dry run; no model started.\nWorking directory: {}\nHarness: {}\nTransport: {}\nAdapter: {}\nPrompt placement: {} ({})\nIsolation: {}\nAuth: {} ({})\nBilling owner: {}\nImage: {} ({})\nReadiness: {}\n",
            human_safe_scalar(&config.cwd.display().to_string()),
            human_safe_scalar(&config.harness.value),
            human_safe_scalar(transport),
            human_safe_scalar(adapter_id),
            human_safe_scalar(prompt_placement.unwrap_or("unavailable")),
            human_safe_scalar(prompt_strictness),
            human_safe_scalar(isolation),
            human_safe_scalar(auth_category),
            human_safe_scalar(auth_readiness),
            human_safe_scalar(billing_owner),
            human_safe_scalar(&image.manifest.image_version),
            human_safe_scalar(image.aggregate_sha256()),
            human_safe_scalar(human_readiness)
        );
        if config.native_profile.value != "default" {
            let _=writeln!(output,"Native profile: {} (requested; observed tools unavailable in dry run)",config.native_profile.value);
            let _=writeln!(output,"Native configuration: {}",human_safe_scalar(&report["nativeConfiguration"].to_string()));
        }
        if let Some(error) = report["blockingError"].as_object() {
            if let (Some(code), Some(action)) = (
                error.get("code").and_then(Value::as_str),
                error.get("action").and_then(Value::as_str),
            ) {
                let _ = writeln!(output, "Blocking problem: {code}");
                if let Some(problem) = blocking_error {
                    for detail in problem.human_version_repair_details() {
                        let _ = writeln!(output, "{detail}");
                    }
                }
                let human_action = blocking_error
                    .as_ref()
                    .map_or_else(|| action.to_owned(), |error| error.human_action());
                let _ = writeln!(output, "Action: {human_action}");
            }
        }
        CommandOutcome::human(output, "", exit_code)
    } else {
        CommandOutcome::json(report, exit_code)
    }
}

fn config_source_entries(config: &EffectiveConfig) -> Vec<Value> {
    let mut entries=vec![
        config_source_entry("cwd", &config.cwd_source, false),
        config_source_entry("harness", &config.harness.source, false),
        config_source_entry("transport", &config.transport.source, false),
        config_source_entry("model", &config.model.source, false),
        config_source_entry("timeout", &config.timeout.source, false),
        config_source_entry("output", &config.output.source, false),
        config_source_entry("color", &config.color.source, false),
        config_source_entry("verbose", &config.verbose.source, false),
        config_source_entry("authProfile", &config.auth_profile.source, true),
    ];
    for (key,source) in [("outputContract",&config.output_contract.source),("permissionMode",&config.permission_mode.source),("nativeMaxTurns",&config.native_max_turns.source),("nativeTimeout",&config.native_timeout.source),("nativeToolTimeout",&config.native_tool_timeout.source),("nativeOutputBytes",&config.native_output_bytes.source),("nativeProfile",&config.native_profile.source),("nativeAddDirs",&config.native_add_dirs.source),("nativeAllowTools",&config.native_allow_tools.source)] {
        if source.kind != ConfigSourceKind::Default {entries.push(config_source_entry(key,source,false));}
    }
    entries
}

fn config_source_entry(key: &str, source: &crate::config::ConfigSource, redacted: bool) -> Value {
    let source_name = match source.kind {
        ConfigSourceKind::Flag => "flag",
        ConfigSourceKind::Environment => "environment",
        ConfigSourceKind::ProjectFile => "project",
        ConfigSourceKind::UserFile => "user",
        ConfigSourceKind::Default => "default",
    };
    json!({
        "key":key,
        "source":source_name,
        "location":source.location,
        "redacted":redacted
    })
}

fn mock_result(
    task: &Value,
    task_digest: &str,
    transport: &str,
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    terminal_envelope: &Value,
) -> CommandOutcome {
    let invocation_id = ids.next_invocation_id();
    let started_at = clock.now_rfc3339();
    let invocation = json!({
        "schema": "openprose.runner-invocation/1",
        "invocationId": invocation_id,
        "cwd": config.cwd.display().to_string(),
        "languageImage": {
            "formatVersion": image.manifest.image_format_version,
            "version": image.manifest.image_version,
            "sha256": image.aggregate_sha256()
        },
        "runner": { "name": RUNNER_NAME, "version": RUNNER_VERSION, "commit": RUNNER_COMMIT },
        "harness": "mock",
        "transport": transport,
        "recursionToken": format!("mock-{invocation_id}"),
        "task": task,
        "taskDigestSha256": task_digest
    });
    let invocation_digest = sha256_hex(&serde_json::to_vec(&invocation).expect("invocation JSON"));
    let descriptor: Value =
        serde_json::from_str(DETERMINISTIC_MOCK_DESCRIPTOR).expect("shared mock descriptor JSON");
    let descriptor_digest = sha256_hex(&serde_json::to_vec(&descriptor).expect("descriptor JSON"));
    let event_values = vec![
        event(
            0,
            &started_at,
            &invocation_id,
            "runner.started",
            &json!({"kind":"runner.started","runnerName":RUNNER_NAME,"runnerVersion":RUNNER_VERSION}),
        ),
        event(
            1,
            &started_at,
            &invocation_id,
            "harness.started",
            &json!({"kind":"harness.started","harness":"mock","transport":transport,"harnessVersion":"1.0.0"}),
        ),
        event(
            2,
            &started_at,
            &invocation_id,
            "harness.completed",
            &json!({"kind":"harness.completed","terminalEventObserved":true,"exitCode":0,"signal":null}),
        ),
    ];
    let mut event_bytes = Vec::new();
    for value in &event_values {
        event_bytes.extend(serde_json::to_vec(value).expect("event JSON"));
        event_bytes.push(b'\n');
    }
    let terminal_digest =
        sha256_hex(&serde_json::to_vec(&terminal_envelope).expect("terminal envelope JSON"));
    let result = json!({
        "schema": "openprose.runner-result/1",
        "invocationId": invocation_id,
        "runner": {"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "adapter": {"id":"mock/in-memory","harnessVersion":"1.0.0","descriptorDigestSha256":descriptor_digest},
        "transport": transport,
        "negotiatedCapabilities": {"promptPlacement":"developer","isolation":"test-fixture","streaming":"structured","cancellation":"unsupported","terminal":"structured"},
        "languageImage": {"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "digests": {
            "invocationSha256":invocation_digest,
            "taskSha256":task_digest,
            "normalizedEventsSha256":sha256_hex(&event_bytes),
            "deliveredImageSha256":image.aggregate_sha256(),
            "renderedPayloadSha256":null
        },
        "cwd": {"path":config.cwd.display().to_string(),"identitySha256":sha256_hex(config.cwd.as_os_str().to_string_lossy().as_bytes())},
        "timing": {"startedAt":started_at,"firstEventAt":started_at,"cancellationAt":null,"terminalAt":started_at,"durationMs":0},
        "terminal": {"classification":"success","transportCompleted":true,"terminalEventObserved":true,"exitCode":0,"signal":null},
        "semantic": {"status":"not-applicable","terminalSchemaSha256":image.manifest.terminal_envelope.sha256,"terminalEnvelopeDigestSha256":terminal_digest},
        "usage": {"status":"unavailable"},
        "billing": {"owner":"test-fixture","authCategory":"none-test-only"},
        "diagnosticRefs": [],
        "runnerExitCode": 0
    });
    match mode {
        OutputMode::Human => CommandOutcome::human(
            "Mock transport completed. No OpenProse language semantics were evaluated.\n",
            "",
            0,
        ),
        OutputMode::Json => CommandOutcome::json(result, 0),
        OutputMode::Jsonl => {
            let mut events = event_values;
            events.push(event(
                3,
                &started_at,
                &invocation_id,
                "runner.completed",
                &json!({"kind":"runner.completed","result":result}),
            ));
            CommandOutcome::jsonl(events, 0)
        }
    }
}

fn forward_error_outcome(
    error: RunnerError,
    argv: &[String],
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
) -> CommandOutcome {
    forward_error_outcome_with_diagnostic(error, argv, config, image, mode, clock, ids, "")
}

#[allow(clippy::too_many_arguments)]
fn forward_error_outcome_with_diagnostic(
    error: RunnerError,
    argv: &[String],
    config: &EffectiveConfig,
    image: &RuntimeImage,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
    diagnostic: &str,
) -> CommandOutcome {
    if mode != OutputMode::Json {
        let outcome = error_outcome(error, mode, clock, ids);
        if diagnostic.is_empty() {
            return outcome;
        }
        return match outcome.payload {
            crate::output::Payload::Human { stdout, mut stderr } => {
                stderr.push_str(&human_safe_multiline(diagnostic));
                CommandOutcome::human(stdout, stderr, outcome.exit_code)
            }
            crate::output::Payload::Jsonl(values) => {
                CommandOutcome::jsonl_with_diagnostic(values, diagnostic, outcome.exit_code)
            }
            crate::output::Payload::Json(value) => {
                CommandOutcome::json_with_diagnostic(value, diagnostic, outcome.exit_code)
            }
            crate::output::Payload::JsonWithDiagnostic { value, mut stderr } => {
                stderr.push_str(diagnostic);
                CommandOutcome::json_with_diagnostic(value, stderr, outcome.exit_code)
            }
            crate::output::Payload::JsonlWithDiagnostic { values, mut stderr } => {
                stderr.push_str(diagnostic);
                CommandOutcome::jsonl_with_diagnostic(values, stderr, outcome.exit_code)
            }
        };
    }
    let invocation_id = ids.next_invocation_id();
    let timestamp = clock.now_rfc3339();
    let task = json!({
        "schema": image.manifest.task_envelope.schema_id,
        "argv": argv,
        "interactionMode": "non-interactive"
    });
    let task_digest = sha256_hex(&serde_json::to_vec(&task).expect("task JSON"));
    let resolved_transport = if config.transport.value == "auto" {
        if config.harness.value == "openprose" {
            "hosted"
        } else if config.harness.value == "mock" {
            "deterministic"
        } else if let Some(adapter) = installed_adapters::for_harness(&config.harness.value) {
            adapter.transport()
        } else {
            "unavailable"
        }
    } else {
        &config.transport.value
    };
    let invocation = json!({
        "schema":"openprose.runner-invocation/1",
        "invocationId":invocation_id,
        "cwd":config.cwd.display().to_string(),
        "languageImage":{"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "runner":{"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "harness":config.harness.value,
        "transport":resolved_transport,
        "recursionToken":format!("failed-{invocation_id}"),
        "task":task,
        "taskDigestSha256":task_digest
    });
    let invocation_digest = sha256_hex(&serde_json::to_vec(&invocation).expect("invocation JSON"));
    let adapter_id = if config.harness.value == "openprose" {
        "openprose/hosted".to_owned()
    } else if let Some(adapter) = installed_adapters::for_harness(&config.harness.value) {
        adapter.id().to_owned()
    } else {
        format!("{}/unavailable", config.harness.value)
    };
    let descriptor_digest = installed_adapters::for_harness(&config.harness.value).map_or_else(
        || sha256_hex(adapter_id.as_bytes()),
        |adapter| sha256_hex(adapter.recipe_json().as_bytes()),
    );
    let capabilities = installed_adapters::for_harness(&config.harness.value).map_or_else(
        || json!({"promptPlacement":"unsupported","isolation":"unsupported","streaming":"unsupported","cancellation":"unsupported","terminal":"unsupported"}),
        |adapter| json!({"promptPlacement":adapter.prompt_placement(),"isolation":adapter.isolation_guarantee(),"streaming":"structured","cancellation":if cfg!(windows) {"unsupported"} else {"process-group-best-effort"},"terminal":"structured"}),
    );
    let (owner, auth_category) = match config.harness.value.as_str() {
        "openprose" => ("openprose", "openprose-account"),
        "mock" => ("test-fixture", "none-test-only"),
        _ => ("user-provider", "harness-managed"),
    };
    let exit_code = error.exit_code;
    let result = json!({
        "schema":"openprose.runner-result/1",
        "invocationId":invocation_id,
        "runner":{"name":RUNNER_NAME,"version":RUNNER_VERSION,"commit":RUNNER_COMMIT},
        "adapter":{"id":adapter_id,"harnessVersion":null,"descriptorDigestSha256":descriptor_digest},
        "transport":resolved_transport,
        "negotiatedCapabilities":capabilities,
        "languageImage":{"formatVersion":image.manifest.image_format_version,"version":image.manifest.image_version,"sha256":image.aggregate_sha256()},
        "digests":{"invocationSha256":invocation_digest,"taskSha256":task_digest,"normalizedEventsSha256":sha256_hex(b""),"deliveredImageSha256":null,"renderedPayloadSha256":null},
        "cwd":{"path":config.cwd.display().to_string(),"identitySha256":sha256_hex(config.cwd.as_os_str().to_string_lossy().as_bytes())},
        "timing":{"startedAt":timestamp,"firstEventAt":null,"cancellationAt":null,"terminalAt":timestamp,"durationMs":0},
        "terminal":{"classification":"runner-error","transportCompleted":false,"terminalEventObserved":false,"exitCode":null,"signal":null},
        "semantic":{"status":"unknown","terminalSchemaSha256":image.manifest.terminal_envelope.sha256,"terminalEnvelopeDigestSha256":null},
        "usage":{"status":"unavailable"},
        "billing":{"owner":owner,"authCategory":auth_category},
        "diagnosticRefs":[],
        "runnerExitCode":exit_code,
        "error":error
    });
    if diagnostic.is_empty() {
        CommandOutcome::json(result, exit_code)
    } else {
        CommandOutcome::json_with_diagnostic(result, diagnostic, exit_code)
    }
}

fn doctor_adapter_facts(
    config: &EffectiveConfig,
    image: &RuntimeImage,
    statuses: &mut [HarnessStatus],
) -> (
    String,
    String,
    Option<&'static str>,
    &'static str,
    &'static str,
    Option<RunnerError>,
) {
    if config.harness.value == "openprose" {
        return (
            "hosted".to_owned(),
            "openprose/hosted".to_owned(),
            None,
            "unsupported",
            "unknown",
            Some(
                RunnerError::catalog(ErrorCode::HostedUnavailable)
                    .with_detail("billingOwner", "openprose")
                    .with_detail("fallbackSelected", false),
            ),
        );
    }
    if config.harness.value == "mock" {
        let transport = if config.transport.value == "auto" {
            "deterministic".to_owned()
        } else {
            config.transport.value.clone()
        };
        let problem = mock_admission_error(image);
        let admitted = problem.is_none();
        return (
            transport,
            "mock/in-memory".to_owned(),
            admitted.then_some("developer"),
            if admitted { "enforced" } else { "unsupported" },
            "not-applicable",
            problem,
        );
    }
    match installed_adapters::select(&config.harness.value, &config.transport.value) {
        Ok(Some(adapter)) => {
            let discovery = inspect_installed_adapter(adapter, config);
            if let Some(status) = statuses
                .iter_mut()
                .find(|status| status.id == config.harness.value)
            {
                status.detected_version.clone_from(&discovery.version);
                if adapter != installed_adapters::InstalledAdapter::OmpRpc
                    || !discovery.runtime_prerequisites.is_empty()
                {
                    status
                        .runtime_prerequisites
                        .clone_from(&discovery.runtime_prerequisites);
                }
                status.availability = match discovery.problem.as_ref().map(|error| error.code) {
                    None => "available",
                    Some(ErrorCode::HarnessNeedsAuth) => "needs-auth",
                    Some(ErrorCode::HarnessUnavailable) => "missing",
                    Some(ErrorCode::ConfigInvalid) => status.availability.as_str(),
                    Some(_) => "incompatible",
                }
                .to_owned();
            }
            (
                adapter.transport().to_owned(),
                adapter.id().to_owned(),
                Some(adapter.prompt_placement()),
                adapter.isolation_guarantee(),
                match discovery.auth_readiness {
                    "missing" => "required",
                    readiness => readiness,
                },
                discovery.problem,
            )
        }
        Err(error) => (
            config.transport.value.clone(),
            installed_adapters::for_harness(&config.harness.value).map_or_else(
                || format!("{}/unavailable", config.harness.value),
                |adapter| adapter.id().to_owned(),
            ),
            None,
            "unsupported",
            "unknown",
            Some(error),
        ),
        Ok(None) => (
            config.transport.value.clone(),
            format!("{}/unavailable", config.harness.value),
            None,
            "unsupported",
            "unknown",
            Some(
                RunnerError::catalog(ErrorCode::HarnessUnavailable)
                    .with_detail("harness", config.harness.value.clone())
                    .with_detail("fallbackAttempted", false),
            ),
        ),
    }
}

fn event(
    sequence: u64,
    timestamp: &str,
    invocation_id: &str,
    event_type: &str,
    payload: &Value,
) -> Value {
    json!({
        "schema":"openprose.normalized-event/1",
        "sequence":sequence,
        "timestamp":timestamp,
        "invocationId":invocation_id,
        "type":event_type,
        "payload":payload
    })
}

fn config_report(config: &EffectiveConfig) -> ConfigReport<'_> {
    ConfigReport {
        schema: "openprose.configuration-explanation/1",
        cwd: crate::config::Sourced {
            value: config.cwd.display().to_string(),
            source: config.cwd_source.clone(),
        },
        project_config_path: config
            .project_config
            .as_ref()
            .map(|path| path.display().to_string()),
        user_config_path: config
            .user_config
            .as_ref()
            .map(|path| path.display().to_string())
            .expect("resolved configuration always has a user config candidate"),
        values: ConfigValuesReport {
            harness: &config.harness,
            transport: &config.transport,
            model: &config.model,
            timeout: &config.timeout,
            output: &config.output,
            color: &config.color,
            verbose: &config.verbose,
            auth_profile: &config.auth_profile,
            output_contract:(config.output_contract.source.kind!=ConfigSourceKind::Default).then_some(&config.output_contract),
            permission_mode:(config.permission_mode.source.kind!=ConfigSourceKind::Default).then_some(&config.permission_mode),
            native_max_turns:config.native_max_turns.value.as_ref().map(|_|&config.native_max_turns),
            native_timeout:config.native_timeout.value.as_ref().map(|_|&config.native_timeout),
            native_tool_timeout:config.native_tool_timeout.value.as_ref().map(|_|&config.native_tool_timeout),
            native_output_bytes:config.native_output_bytes.value.as_ref().map(|_|&config.native_output_bytes),
            native_profile:(config.native_profile.source.kind!=ConfigSourceKind::Default).then_some(&config.native_profile),
            native_add_dirs:(config.native_add_dirs.source.kind!=ConfigSourceKind::Default).then_some(&config.native_add_dirs),
            native_allow_tools:(config.native_allow_tools.source.kind!=ConfigSourceKind::Default).then_some(&config.native_allow_tools),
        },
    }
}

fn render_config_human(config: &EffectiveConfig) -> String {
    let mut output = format!(
        "Working directory: {}\nHarness: {} ({})\nTransport: {} ({})\nModel: {} ({})\nTimeout: {} ({})\nOutput: {:?} ({})\nColor: {} ({})\nVerbose: {} ({})\nAuth profile: {} ({})\n",
        human_safe_scalar(&config.cwd.display().to_string()),
        human_safe_scalar(&config.harness.value),
        source_label(&config.harness.source),
        human_safe_scalar(&config.transport.value),
        source_label(&config.transport.source),
        human_safe_scalar(config.model.value.as_deref().unwrap_or("unset")),
        source_label(&config.model.source),
        human_safe_scalar(&config.timeout.value),
        source_label(&config.timeout.source),
        config.output.value,
        source_label(&config.output.source),
        config.color.value,
        source_label(&config.color.source),
        config.verbose.value,
        source_label(&config.verbose.source),
        human_safe_scalar(config.auth_profile.value.as_deref().unwrap_or("unset")),
        source_label(&config.auth_profile.source),
    );
    for (name,value,source) in [("outputContract",config.output_contract.value.as_str(),&config.output_contract.source),("permissionMode",config.permission_mode.value.as_deref().unwrap_or("unset"),&config.permission_mode.source)] {
        if source.kind!=ConfigSourceKind::Default {let _=writeln!(output,"{} = {} ({})",name,human_safe_scalar(value),source_label(source));}
    }
    output
}

fn source_label(source: &crate::config::ConfigSource) -> String {
    match &source.location {
        Some(location) => format!("{:?}: {}", source.kind, human_safe_scalar(location)),
        None => format!("{:?}", source.kind),
    }
}

fn harness_statuses(
    image: &RuntimeImage,
    config: &EffectiveConfig,
) -> Result<Vec<HarnessStatus>, RunnerError> {
    let mock_problem = mock_admission_error(image);
    let mock_available = mock_problem.is_none();
    let mock_admission_block = if mock_available {
        None
    } else if !TEST_SEAMS_ENABLED {
        Some("test-seams-disabled".to_owned())
    } else if image.manifest.purpose != "sentinel-transport-test" || image.manifest.release_eligible
    {
        Some("release-image".to_owned())
    } else {
        Some("terminal-envelope".to_owned())
    };
    let mut statuses = vec![
        HarnessStatus {
            id: "openprose",
            runtime: "hosted-runtime",
            availability: "not-implemented".to_owned(),
            detected_version: None,
            transports: &["hosted"],
            auth_category: "openprose-account",
            billing_owner: "openprose",
            strict_wrapper_conformant: false,
            test_only: false,
            runtime_prerequisites: Vec::new(),
            admission_block: None,
        },
        HarnessStatus {
            id: "mock",
            runtime: "deterministic-mock",
            availability: if mock_available {
                "available".to_owned()
            } else if TEST_SEAMS_ENABLED {
                "blocked".to_owned()
            } else {
                "unavailable".to_owned()
            },
            detected_version: mock_available.then(|| "1.0.0".to_owned()),
            transports: &["deterministic", "fake-process"],
            auth_category: "none-test-only",
            billing_owner: "test-fixture",
            strict_wrapper_conformant: mock_available,
            test_only: true,
            runtime_prerequisites: Vec::new(),
            admission_block: mock_admission_block,
        },
    ];
    for adapter in [
        installed_adapters::InstalledAdapter::PrimeRpc,
        installed_adapters::InstalledAdapter::OmpRpc,
        installed_adapters::InstalledAdapter::CodexExecJson,
        installed_adapters::InstalledAdapter::ClaudePrintStreamJson,
        installed_adapters::InstalledAdapter::AgentsSdkJsonl,
    ] {
        let (availability, detected_version, runtime_prerequisites) =
            inspect_installed_binary(adapter, config)?;
        let status = HarnessStatus {
            id: adapter.harness(),
            runtime: "installed-process",
            availability,
            detected_version,
            transports: supported_transports(adapter.harness())
                .expect("every installed adapter has a transport declaration"),
            auth_category: "harness-managed",
            billing_owner: "user-provider",
            strict_wrapper_conformant: false,
            test_only: false,
            runtime_prerequisites,
            admission_block: None,
        };
        let index = match adapter {
            installed_adapters::InstalledAdapter::AgentsSdkJsonl => 5,
            installed_adapters::InstalledAdapter::PrimeRpc => 1,
            installed_adapters::InstalledAdapter::OmpRpc => 2,
            installed_adapters::InstalledAdapter::CodexExecJson => 3,
            installed_adapters::InstalledAdapter::ClaudePrintStreamJson => 4,
        };
        statuses.insert(index, status);
    }
    Ok(statuses)
}

fn inspect_installed_binary(
    adapter: installed_adapters::InstalledAdapter,
    config: &EffectiveConfig,
) -> Result<
    (
        String,
        Option<String>,
        Vec<installed_adapters::RuntimePrerequisiteObservation>,
    ),
    RunnerError,
> {
    let ambient = env::vars_os().collect::<Vec<_>>();
    let runtime_prerequisites = || inspect_runtime_prerequisites(adapter, &config.cwd, &ambient);
    if installed_adapters::assert_platform_supported(
        adapter,
        installed_adapters::HostPlatform::current(),
    )
    .is_err()
    {
        return Ok(("incompatible".to_owned(), None, runtime_prerequisites()?));
    }
    let search_path = ambient
        .iter()
        .find(|(name, _)| name == std::ffi::OsStr::new("PATH"))
        .map(|(_, value)| value.as_os_str());
    let Some(executable) = installed_adapters::resolve_executable(adapter, search_path) else {
        return Ok(("missing".to_owned(), None, runtime_prerequisites()?));
    };
    let runtime_prerequisites = runtime_prerequisites()?;
    if runtime_prerequisites
        .iter()
        .any(|observation| observation.availability != "available")
    {
        return Ok(("incompatible".to_owned(), None, runtime_prerequisites));
    }
    let environment = installed_adapters::version_probe_environment(adapter, ambient);
    match probe_version(
        &executable,
        &config.cwd,
        &environment,
        &adapter.version_probe(),
        &CancellationToken::default(),
    ) {
        Ok(version) if adapter.version_is_supported(&version) => {
            Ok(("available".to_owned(), Some(version), runtime_prerequisites))
        }
        Ok(version) => Ok((
            "incompatible".to_owned(),
            Some(version),
            runtime_prerequisites,
        )),
        Err(failure) if failure.kind == FailureKind::CleanupFailed => {
            Err(map_supervisor_failure(&failure)
                .with_detail("adapterId", adapter.id())
                .with_detail("phase", "version-probe"))
        }
        Err(_) => Ok(("incompatible".to_owned(), None, runtime_prerequisites)),
    }
}

fn billing_owner(harness: &str) -> &'static str {
    match harness {
        "openprose" => "openprose",
        "mock" => "test-fixture",
        _ => "user-provider",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::path::PathBuf;
    use tempfile::TempDir;

    fn sentinel_image() -> RuntimeImage {
        RuntimeImage::load(
            &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../shared/image/sentinel-v1"),
        )
        .unwrap()
    }

    fn installed_config(
        root: &Path,
        harness: &str,
        transport: &str,
        model: Option<&str>,
        auth_profile: &str,
    ) -> EffectiveConfig {
        let flags = GlobalFlags {
            harness: Some(harness.to_owned()),
            transport: Some(transport.to_owned()),
            model: model.map(str::to_owned),
            auth_profile: Some(auth_profile.to_owned()),
            ..GlobalFlags::default()
        };
        crate::config::resolve_config(
            &flags,
            &crate::config::SystemContext {
                current_dir: root.to_owned(),
                home_dir: Some(root.join("home")),
                xdg_config_home: Some(root.join("xdg")),
                appdata: None,
                environment: BTreeMap::new(),
                platform: crate::config::Platform::current(),
            },
        )
        .unwrap()
    }

    #[test]
    fn prime_prelude_gates_prompt_and_closes_only_after_ack() {
        let f:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/tool-lifecycle/prime-drain.json")).unwrap();
        let make=||PrimeStagedController{id:"fixture-drain".into(),prompt:Some(b"prompt\n".to_vec()),pending_write:None,records:vec![],close:false};
        let mut p=make();assert!(p.pending_write.is_none());p.observe(&f["stateResponse"]).unwrap();assert_eq!(p.pending_write.take(),Some(b"prompt\n".to_vec()));assert!(!p.close);p.observe(&f["promptResponse"]).unwrap();assert!(p.close);
        for bad in [f["promptResponse"].clone(),json!({"id":"fixture-drain.prime.state.1","type":"response","command":"get_state","success":false,"data":{}})] {let mut p=make();assert!(p.observe(&bad).is_err());assert!(p.pending_write.is_none());assert!(!p.close);}
    }

    #[test]
    fn sdk_error_is_captured_safely_before_admission_without_log() {
        let mut observer=InstalledRunObserver{require_api_source:false,auth_source_failed:false,sdk:true,native_failure:None,capture:None,human:None,omp:None,prime:None};
        observer.observe_parsed(&json!({"type":"error","error_type":"MaxTurnsExceeded","elapsed_seconds":2,"message":"do not expose"})).unwrap();
        assert_eq!(observer.native_failure,Some(json!({"kind":"max-turns","elapsedSeconds":2.0})));
        observer.sdk=false;observer.native_failure=None;
        observer.observe_parsed(&json!({"type":"error","error_type":"MaxTurnsExceeded"})).unwrap();assert!(observer.native_failure.is_none());
    }

    #[test]
    fn optional_reporting_preserves_defaults_and_explicit_selections() {
        let fixture:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/config/optional-reporting.json")).unwrap();
        let temp=TempDir::new().unwrap();let mut config=installed_config(temp.path(),"agents-sdk","jsonl",Some("fixture"),"openai-api-key");
        let base=serde_json::to_value(config_report(&config)).unwrap();
        assert_eq!(config.output_contract.value,"image-envelope");assert!(config.permission_mode.value.is_none());
        for k in fixture["defaultsOmitted"].as_array().unwrap(){assert!(base["values"].get(k.as_str().unwrap()).is_none());}
        for case in fixture["cases"].as_array().unwrap(){
            config.output_contract.value=case["outputContract"].as_str().unwrap().into(); config.output_contract.source=crate::config::ConfigSource{kind:ConfigSourceKind::Flag,location:Some("--output-contract".into())};
            config.permission_mode.value=Some(case["permissionMode"].as_str().unwrap().into()); config.permission_mode.source=crate::config::ConfigSource{kind:ConfigSourceKind::Flag,location:Some("--permission-mode".into())};
            let report=serde_json::to_value(config_report(&config)).unwrap();
            for key in ["outputContract","permissionMode"] {assert_eq!(report["values"][key]["value"],case[key]);assert_eq!(report["values"][key]["source"]["kind"],"flag");assert!(config_source_entries(&config).iter().any(|v|v["key"]==key));}
        }
    }

    #[test]
    fn sdk_tool_timeout_arguments_preserve_parent_defaults() {
        let temp=TempDir::new().unwrap();
        let mut config=installed_config(temp.path(),"agents-sdk","jsonl",Some("fixture-model"),"openai-api-key");
        config.harness.value = "agents-sdk".into();
        assert!(sdk_limit_arguments(&config).is_empty());
        for (value, seconds) in [("30s", "30"), ("180s", "180"), ("1ms", "0.001")] {
            config.native_tool_timeout.value = Some(value.into());
            assert_eq!(sdk_limit_arguments(&config), vec![std::ffi::OsString::from("--tool-timeout"), seconds.into()]);
            let limits = crate::config::native_limits(&config).unwrap();
            assert_eq!(limits["timeoutSeconds"], 180);
            assert_eq!(limits["toolTimeoutSeconds"].as_f64().unwrap(), seconds.parse::<f64>().unwrap());
        }
    }

    #[test]
    fn sdk_budget_arguments_and_invocation_metadata() {
        let f:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/sdk-native-limits.json")).unwrap();
        let temp=TempDir::new().unwrap();let mut config=installed_config(temp.path(),"agents-sdk","jsonl",Some("fixture-model"),"openai-api-key");
        config.harness.value="agents-sdk".into();
        assert!(sdk_limit_arguments(&config).is_empty());
        assert_eq!(crate::config::native_limits(&config).unwrap(),f["defaults"]);
        config.native_max_turns.value=Some("40".into());config.native_timeout.value=Some("5m".into());
        let args:Vec<String>=sdk_limit_arguments(&config).iter().map(|s|s.to_string_lossy().into_owned()).collect();
        assert_eq!(serde_json::to_value(args).unwrap(),f["override"]["argv"]);
        config.native_timeout.value=Some("1ms".into());assert_eq!(sdk_limit_arguments(&config).last().unwrap(),"0.001");
    }

    #[test]
    fn workspace_native_metadata_and_auth_evidence(){
        let temp=TempDir::new().unwrap();let mut config=installed_config(temp.path(),"claude","print-stream-json",None,"anthropic-api-key");
        assert!(native_configuration(&config,None).is_none());
        config.native_profile.value="claude-workspace-tools".into();
        let good=json!({"type":"system","subtype":"init","tools":["Read","Task"],"apiKeySource":"ANTHROPIC_API_KEY"});
        assert!(validate_native_auth(&config,&[good.clone()]).is_ok());
        let meta=native_configuration(&config,Some(&[good])).unwrap();assert_eq!(meta["observed"]["tools"],json!(["Read","Task"]));assert_eq!(meta["configOwnership"],"runner-private");
        for records in [vec![],vec![json!({"type":"system","subtype":"init"})],vec![json!({"type":"system","subtype":"init","apiKeySource":"oauth"})]] {assert_eq!(validate_native_auth(&config,&records).unwrap_err().code,ErrorCode::HarnessNeedsAuth);}
        let mut observer=InstalledRunObserver{require_api_source:true,auth_source_failed:false,sdk:false,native_failure:None,capture:None,human:None,omp:None,prime:None};
        assert!(observer.observe(&json!({"type":"system","subtype":"init","apiKeySource":"oauth"})).is_err());assert!(observer.auth_source_failed);
        config.auth_profile.value=Some("claude-subscription".into());assert!(validate_native_auth(&config,&[]).is_ok());assert_eq!(native_configuration(&config,None).unwrap()["configOwnership"],"native-auth-store");
    }

    #[test]
    fn unsupported_auth_profiles_are_rejected_before_installed_adapter_discovery() {
        let temp = TempDir::new().unwrap();
        for (adapter, harness, transport, model) in [
            (
                installed_adapters::InstalledAdapter::PrimeRpc,
                "prime",
                "rpc",
                Some("fixture/model"),
            ),
            (
                installed_adapters::InstalledAdapter::OmpRpc,
                "omp",
                "rpc",
                Some("fixture/model"),
            ),
            (
                installed_adapters::InstalledAdapter::CodexExecJson,
                "codex",
                "exec-json",
                None,
            ),
            (
                installed_adapters::InstalledAdapter::ClaudePrintStreamJson,
                "claude",
                "print-stream-json",
                None,
            ),
        ] {
            let config = installed_config(
                temp.path(),
                harness,
                transport,
                model,
                "unsupported-profile",
            );
            let discovery = inspect_installed_adapter(adapter, &config);
            let problem = discovery.problem.unwrap();
            assert_eq!(problem.code, ErrorCode::ConfigInvalid, "{}", adapter.id());
            assert_eq!(discovery.executable, None, "{}", adapter.id());
            assert_eq!(discovery.version, None, "{}", adapter.id());
            assert!(
                discovery.runtime_prerequisites.is_empty(),
                "{}",
                adapter.id()
            );
            assert_eq!(
                problem.details.unwrap().get("reason"),
                Some(&Value::String(format!(
                    "Unknown auth_profile for {}: unsupported-profile.",
                    adapter.id()
                ))),
                "{}",
                adapter.id()
            );
        }
    }

    #[test]
    fn prime_and_omp_model_validation_precedes_auth_profile_validation() {
        let temp = TempDir::new().unwrap();
        for (adapter, harness) in [
            (installed_adapters::InstalledAdapter::PrimeRpc, "prime"),
            (installed_adapters::InstalledAdapter::OmpRpc, "omp"),
        ] {
            let config = installed_config(
                temp.path(),
                harness,
                "rpc",
                Some("unqualified-model"),
                "unsupported-profile",
            );
            let problem = inspect_installed_adapter(adapter, &config).problem.unwrap();
            assert_eq!(problem.code, ErrorCode::ConfigInvalid, "{}", adapter.id());
            assert_eq!(
                problem.details.unwrap().get("reason"),
                Some(&Value::String(
                    "Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments."
                        .to_owned()
                )),
                "{}",
                adapter.id()
            );
        }
    }

    #[test]
    fn human_readiness_distinguishes_unverified_auth_only_on_success() {
        assert_eq!(
            human_readiness_label(true, "unknown", "not ready"),
            "mechanically ready; authentication unverified"
        );
        assert_eq!(human_readiness_label(true, "ready", "not ready"), "ready");
        assert_eq!(
            human_readiness_label(true, "not-applicable", "not ready"),
            "ready"
        );
        assert_eq!(
            human_readiness_label(false, "unknown", "not ready"),
            "not ready"
        );
        assert_eq!(
            human_readiness_label(false, "unknown", "blocked"),
            "blocked"
        );
    }

    #[test]
    fn human_harness_inventory_confines_a_hostile_detected_version_to_one_line() {
        let detected = "codex-cli 0.149.0-alpha.4.1\nAction: forged\t\u{001B}[31m\u{2028}next\u{2029}paragraph";
        let status = HarnessStatus {
            id: "codex",
            runtime: "installed-process",
            availability: "available".to_owned(),
            detected_version: Some(detected.to_owned()),
            transports: &["exec-json"],
            auth_category: "harness-managed",
            billing_owner: "user-provider",
            strict_wrapper_conformant: true,
            test_only: false,
            runtime_prerequisites: Vec::new(),
            admission_block: None,
        };
        let mut rendered = String::new();
        render_harness_status_human(&mut rendered, &status, "openprose");
        assert!(rendered.contains(&format!("version={}", human_safe_scalar(detected))));
        assert_eq!(rendered.lines().count(), 1);
        for unsafe_value in ["\nAction: forged", "\t", "\u{001B}", "\u{2028}", "\u{2029}"] {
            assert!(!rendered.contains(unsafe_value), "{unsafe_value:?}");
        }
        assert_eq!(status.detected_version.as_deref(), Some(detected));
    }

    #[test]
    fn sentinel_terminal_contract_is_derived_from_the_verified_image_schema() {
        let image = sentinel_image();
        assert_eq!(
            sentinel_terminal_envelope(&image).unwrap(),
            json!({
                "schema":"openprose.sentinel-terminal-envelope/1",
                "semanticStatus":"not-applicable",
                "marker":"OPENPROSE_SENTINEL_TERMINAL_V1"
            })
        );
    }

    #[test]
    fn mock_terminal_contract_rejects_non_sentinel_and_non_authorizing_images() {
        let mut release_image = sentinel_image();
        release_image.manifest.release_eligible = true;
        assert_eq!(
            sentinel_terminal_envelope(&release_image).unwrap_err().code,
            ErrorCode::HarnessUnavailable
        );

        let mut purpose_image = sentinel_image();
        purpose_image.manifest.purpose = "canonical-runtime".to_owned();
        assert_eq!(
            sentinel_terminal_envelope(&purpose_image).unwrap_err().code,
            ErrorCode::HarnessUnavailable
        );

        let mut schema_image = sentinel_image();
        let mut schema: Value =
            serde_json::from_slice(&schema_image.terminal_envelope_schema).unwrap();
        schema["properties"]["semanticStatus"]["const"] = Value::String("success".to_owned());
        schema_image.terminal_envelope_schema = serde_json::to_vec(&schema).unwrap();
        assert_eq!(
            sentinel_terminal_envelope(&schema_image).unwrap_err().code,
            ErrorCode::TransportUnsupported
        );
    }

    #[test]
    fn human_stream_filter_rejects_protected_values_and_control_json_lines() {
        let protected = vec!["task-secret".to_owned(), "model-secret".to_owned()];
        assert!(safe_human_stream_message(
            "A safe, already-framed assistant paragraph.",
            &protected
        ));
        assert!(!safe_human_stream_message(
            "The task-secret must never be streamed.",
            &protected
        ));
        assert!(!safe_human_stream_message(
            "Visible preface\n{\"schema\":\"terminal\"}",
            &protected
        ));
        assert!(!safe_human_stream_message("[1,2,3]", &protected));
    }

    #[test]
    fn human_stream_preserves_prose_lines_and_escapes_terminal_controls() {
        let raw = "ordinary \\ path\nOSC:\u{001B}]0;owned\u{0007}\tend\u{0085}\u{2028}next\u{2029}paragraph";
        let mut output = Vec::new();
        let mut stream = InstalledHumanStream::new(
            installed_adapters::InstalledAdapter::CodexExecJson,
            "fixture-rpc-id",
            &mut output,
            Vec::new(),
        );
        stream
            .reconcile_candidates(vec![format!("{raw}\nterminal carrier")])
            .unwrap();
        drop(stream);
        assert_eq!(
            String::from_utf8(output).unwrap(),
            human_safe_multiline(raw)
        );
    }

    #[test]
    fn public_machine_text_redacts_exact_selected_secrets_longest_first() {
        let secrets = vec![
            "key-short".to_owned(),
            "key-short-with-suffix".to_owned(),
            "雪-secret".to_owned(),
            String::new(),
        ];
        assert_eq!(
            redact_exact_secrets(
                "before key-short-with-suffix / key-short / 雪-secret after",
                &secrets,
            ),
            "before [REDACTED] / [REDACTED] / [REDACTED] after"
        );
        assert_eq!(
            redact_exact_secrets("ordinary output", &secrets),
            "ordinary output"
        );
    }

    #[test]
    fn public_machine_text_redacts_secrets_split_across_messages() {
        let messages = vec![
            "before key-short".to_owned(),
            "-with-suffix between \u{96ea}".to_owned(),
            "-secret after".to_owned(),
        ];
        let rendered = redact_exact_secrets_across_messages(
            &messages,
            &[
                "key-short-with-suffix".to_owned(),
                "\u{96ea}-secret".to_owned(),
            ],
        );
        assert_eq!(
            rendered,
            vec![
                "before [REDACTED]".to_owned(),
                "[REDACTED] between [REDACTED]".to_owned(),
                "[REDACTED] after".to_owned(),
            ]
        );
        let reconstructed = rendered.concat();
        assert!(!reconstructed.contains("key-short-with-suffix"));
        assert!(!reconstructed.contains("\u{96ea}-secret"));
    }

    #[test]
    fn human_stream_rejects_the_exact_nonempty_configured_cwd() {
        let cwd = "/private/workspace with spaces/secret-project".to_owned();
        assert!(!safe_human_stream_message(
            "I inspected /private/workspace with spaces/secret-project before answering.",
            &[cwd]
        ));
    }

    #[test]
    fn human_stream_protects_one_two_and_three_byte_literals() {
        for literal in ["q", "uv", "wxy"] {
            let mut output = Vec::new();
            let mut stream = InstalledHumanStream::new(
                installed_adapters::InstalledAdapter::CodexExecJson,
                "fixture-rpc-id",
                &mut output,
                vec![literal.to_owned()],
            );
            stream
                .reconcile_candidates(vec![format!("leak:{literal}\nterminal")])
                .unwrap();
            assert!(stream.stalled, "{literal:?} was not protected");
            drop(stream);
            assert!(output.is_empty(), "{literal:?} was disclosed");
        }
    }

    #[test]
    fn human_stream_rejects_split_protected_values_and_control_fragments() {
        let mut protected_output = Vec::new();
        let mut protected_stream = InstalledHumanStream::new(
            installed_adapters::InstalledAdapter::CodexExecJson,
            "fixture-rpc-id",
            &mut protected_output,
            vec!["runner-selected-model".to_owned()],
        );
        protected_stream
            .reconcile_candidates(vec!["runner-selected-".to_owned()])
            .unwrap();
        protected_stream
            .reconcile_candidates(vec![
                "runner-selected-".to_owned(),
                "model\nterminal".to_owned(),
            ])
            .unwrap();
        assert!(protected_stream.stalled);
        drop(protected_stream);
        assert!(protected_output.is_empty());

        let mut control_output = Vec::new();
        let mut control_stream = InstalledHumanStream::new(
            installed_adapters::InstalledAdapter::CodexExecJson,
            "fixture-rpc-id",
            &mut control_output,
            Vec::new(),
        );
        control_stream
            .reconcile_candidates(vec!["{\"schema\":".to_owned()])
            .unwrap();
        control_stream
            .reconcile_candidates(vec![
                "{\"schema\":".to_owned(),
                "\"split-control\"}\nterminal".to_owned(),
            ])
            .unwrap();
        assert!(control_stream.stalled);
        drop(control_stream);
        assert!(control_output.is_empty());
        assert!(!safe_human_stream_message("  [\"partial\"", &[]));
    }

    #[test]
    fn human_stream_holds_the_terminal_candidate_and_stalls_on_unsafe_prefixes() {
        let record = |text: &str| {
            json!({
                "type":"item.completed",
                "item":{"id":"fixture","type":"agent_message","text":text}
            })
        };

        let mut output = Vec::new();
        let mut stream = InstalledHumanStream::new(
            installed_adapters::InstalledAdapter::CodexExecJson,
            "fixture-rpc-id",
            &mut output,
            vec!["task-secret".to_owned()],
        );
        stream
            .observe(&json!({"type":"thread.started","thread_id":"fixture-thread"}))
            .unwrap();
        stream.observe(&json!({"type":"turn.started"})).unwrap();
        stream.observe(&record("safe first message")).unwrap();
        assert!(stream.emitted.is_empty());
        stream
            .observe(&record("{\"schema\":\"must-not-stream\"}"))
            .unwrap();
        assert_eq!(stream.emitted, "safe first message");
        stream.observe(&record("safe later message")).unwrap();
        assert!(stream.stalled);
        assert_eq!(stream.emitted, "safe first message");
        drop(stream);
        assert_eq!(output, b"safe first message");

        let mut output = Vec::new();
        let mut stream = InstalledHumanStream::new(
            installed_adapters::InstalledAdapter::CodexExecJson,
            "fixture-rpc-id",
            &mut output,
            vec!["task-secret".to_owned()],
        );
        stream
            .observe(&json!({"type":"thread.started","thread_id":"fixture-thread"}))
            .unwrap();
        stream.observe(&json!({"type":"turn.started"})).unwrap();
        stream
            .observe(&record("contains task-secret and stays buffered"))
            .unwrap();
        stream.observe(&record("safe second message")).unwrap();
        assert!(stream.stalled);
        drop(stream);
        assert!(output.is_empty());
    }

    #[test]
    fn every_installed_adapter_projects_visible_text_before_its_transport_terminal() {
        let scenarios = [
            (
                installed_adapters::InstalledAdapter::CodexExecJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/codex-exec-json.v1.json"
                ),
            ),
            (
                installed_adapters::InstalledAdapter::ClaudePrintStreamJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/claude-print-stream-json.v1.json"
                ),
            ),
            (
                installed_adapters::InstalledAdapter::PrimeRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"),
            ),
            (
                installed_adapters::InstalledAdapter::OmpRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/omp-rpc.v1.json"),
            ),
        ];
        for (adapter, scenario) in scenarios {
            let scenario: Value = serde_json::from_str(scenario).unwrap();
            let records = scenario["fakeStdout"].as_array().unwrap();
            let terminal_index = records
                .iter()
                .position(|record| {
                    record.get("type").and_then(Value::as_str)
                        == Some(adapter.protocol().terminal_event.as_str())
                })
                .unwrap();
            let mut output = Vec::new();
            let mut stream = InstalledHumanStream::new(
                adapter,
                "fixture-invocation-0001",
                &mut output,
                Vec::new(),
            );
            let mut first_emission = None;
            for (index, record) in records.iter().enumerate() {
                stream.observe(record).unwrap();
                if first_emission.is_none() && !stream.emitted.is_empty() {
                    first_emission = Some(index);
                }
            }
            assert!(
                first_emission.is_some_and(|index| index < terminal_index),
                "{} did not expose admitted assistant text before settlement",
                adapter.id()
            );
            assert!(stream.emitted.starts_with("Echoed task argv:"));
        }
    }

    #[test]
    fn omp_controller_stages_only_state_then_prompt_and_fails_closed() {
        let prompt =
            b"{\"id\":\"fixture.omp.prompt.1\",\"message\":\"task\",\"type\":\"prompt\"}\n"
                .to_vec();
        let mut controller = OmpStagedController::new("fixture", prompt.clone());
        controller.observe(&json!({"type":"ready"})).unwrap();
        assert!(controller.pending_write.is_none());
        controller
            .observe(&json!({"type":"available_commands_update","commands":[]}))
            .unwrap();
        assert_eq!(
            controller.pending_write.take().unwrap(),
            b"{\"id\":\"fixture.omp.state.1\",\"type\":\"get_state\"}\n"
        );
        controller
            .observe(&json!({
                "id":"fixture.omp.state.1",
                "type":"response",
                "command":"get_state",
                "success":true,
                "data":{"dumpTools":[],"candidateSecret":"must-not-be-projected"}
            }))
            .unwrap();
        let retained = controller.retained_record_projection().unwrap();
        assert_eq!(retained["data"], json!({"dumpTools":[]}));
        assert!(!retained.to_string().contains("candidateSecret"));
        assert_eq!(controller.pending_write.take().unwrap(), prompt);

        for (response, kind) in [
            (
                json!({
                    "id":"fixture.omp.state.1","type":"response","command":"get_state",
                    "success":true,"data":{"dumpTools":[{"name":""}],"candidateSecret":"must-not-be-retained"}
                }),
                FailureKind::ProtocolMalformed,
            ),
            (
                json!({
                    "id":"wrong","type":"response","command":"get_state",
                    "success":true,"data":{"dumpTools":[]}
                }),
                FailureKind::ProtocolMalformed,
            ),
        ] {
            let mut candidate = OmpStagedController::new("fixture", Vec::from(&b"prompt\n"[..]));
            candidate.observe(&json!({"type":"ready"})).unwrap();
            candidate
                .observe(&json!({"type":"available_commands_update","commands":[]}))
                .unwrap();
            candidate.pending_write.take();
            assert_eq!(candidate.observe(&response).unwrap_err().kind, kind);
            assert!(
                !candidate
                    .retained_record_projection()
                    .unwrap()
                    .to_string()
                    .contains("candidateSecret")
            );
            assert!(candidate.pending_write.is_none());
        }

        let failure = controller
            .observe(&json!({"type":"agent_end","messages":[],"isTerminal":false}))
            .unwrap_err();
        assert_eq!(failure.kind, FailureKind::HarnessFailed);
        assert_eq!(
            map_supervisor_failure(&failure).details.unwrap()["reason"],
            "unsupported_nonterminal_settlement"
        );
    }
}

// Called only after native transport normalization has accepted terminal settlement.
fn native_output(messages:&[String])->installed_adapters::RecoveredTerminal {
 installed_adapters::RecoveredTerminal { envelope:Value::Null,visible_messages:messages.to_vec(),visible_text:messages.join("\n") }
}
#[test]
fn native_output_preserves_arbitrary_prose_without_an_envelope(){
 let fixture:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/native-output.v1.json")).unwrap();
 let messages:Vec<String>=serde_json::from_value(fixture["messages"].clone()).unwrap();
 let output=native_output(&messages);
 assert!(output.envelope.is_null());
 assert_eq!(output.visible_text,fixture["visibleText"].as_str().unwrap());
}

#[test]
fn native_output_requires_a_native_terminal_before_rendering(){
 let records=vec![json!({"type":"thread.started","thread_id":"fixture"}),json!({"type":"turn.started"}),json!({"type":"item.completed","item":{"type":"agent_message","text":"arbitrary prose"}})];
 assert!(installed_adapters::normalize_transport(installed_adapters::InstalledAdapter::CodexExecJson,&records,"fixture").is_err());
}

struct NativeCapture { file:std::fs::File, secrets:Vec<String>, bytes:usize, limit:usize }
impl NativeCapture {
 fn open(path:&str,secrets:Vec<String>,limit:usize)->std::io::Result<Self>{
   if !std::path::Path::new(path).is_absolute(){return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput,"absolute path required"));}
   let mut options=std::fs::OpenOptions::new();options.write(true).create_new(true);
   #[cfg(unix)] {use std::os::unix::fs::OpenOptionsExt;options.mode(0o600);}
   Ok(Self {file:options.open(path)?,secrets,bytes:0,limit})
 }
 fn write(&mut self,record:&Value)->Result<(),SupervisorFailure>{
   fn scrub(value:&mut Value,secrets:&[String]){match value {Value::String(text)=>{for secret in secrets{if !secret.is_empty(){*text=text.replace(secret,"[REDACTED]");}}},Value::Array(items)=>for item in items{scrub(item,secrets)},Value::Object(items)=>for item in items.values_mut(){scrub(item,secrets)},_=>{}}}
   let mut record=record.clone();scrub(&mut record,&self.secrets);
   let mut bytes=serde_json::to_vec(&record).expect("native JSON");bytes.push(b'\n');
   if self.bytes.checked_add(bytes.len()).is_none_or(|n| n>self.limit){return Err(stream_observer_failure(FailureKind::Internal,"native capture size exceeded"));}
   self.file.write_all(&bytes).map_err(|_|stream_observer_failure(FailureKind::Internal,"native capture write failed"))?;self.bytes+=bytes.len();Ok(())
 }
}

#[test]
fn native_capture_is_private_new_bounded_and_redacts_known_values(){
 let dir=tempfile::tempdir().unwrap();let path=dir.path().join("native.jsonl");
 let mut capture=NativeCapture::open(path.to_str().unwrap(),vec!["fixture-secret".into()],64*1024*1024).unwrap();
 capture.write(&json!({"type":"tool_call","text":"prefix fixture-secret suffix"})).unwrap();
 assert!(NativeCapture::open(path.to_str().unwrap(),vec![],64*1024*1024).is_err());
 assert!(std::fs::read_to_string(&path).unwrap().contains("[REDACTED]"));
 #[cfg(unix)] {use std::os::unix::fs::PermissionsExt;assert_eq!(std::fs::metadata(path).unwrap().permissions().mode() & 0o777,0o600);}
 capture.bytes=64*1024*1024;assert!(capture.write(&json!({"type":"final"})).is_err());
}

#[test]
fn protocol_diagnostics_do_not_echo_native_or_observer_content(){
 let failure=stream_observer_failure(FailureKind::ProtocolMalformed,"harness emitted a record after its terminal record");
 let error=map_supervisor_failure(&failure);
 assert_eq!(error.details.as_ref().unwrap().get("reason"),Some(&json!("record_after_terminal")));
 assert_eq!(error.details.as_ref().unwrap().get("admittedRecordCount"),Some(&json!(0)));
 let failure=stream_observer_failure(FailureKind::ProtocolMalformed,"private arbitrary content");
 assert_eq!(map_supervisor_failure(&failure).details.as_ref().unwrap().get("reason"),Some(&json!("protocol_admission_rejected")));
}

#[test]
fn rendered_error_keeps_safe_transport_diagnostic() {
    let failure=stream_observer_failure(FailureKind::ProtocolMalformed,"harness emitted a malformed JSONL record");
    let rendered=serde_json::to_value(map_supervisor_failure(&failure)).unwrap();
    assert_eq!(rendered["details"]["transportDiagnostic"]["reason"],"invalid-json");
    assert_eq!(rendered["exitCode"],22);
}

fn sdk_limit_arguments(config: &EffectiveConfig) -> Vec<std::ffi::OsString> {
    let mut args = Vec::new();
    if config.harness.value != "agents-sdk" {return args;}
    if let Some(v)=&config.native_max_turns.value {
        args.extend([std::ffi::OsString::from("--max-turns"),v.into()]);
    }
    if let Some(v)=&config.native_timeout.value {
        args.extend([std::ffi::OsString::from("--timeout"),(crate::config::validate_native_timeout(v).expect("validated") as f64/1000.0).to_string().into()]);
    }
    if let Some(v)=&config.native_tool_timeout.value {
        args.extend([std::ffi::OsString::from("--tool-timeout"),(crate::config::validate_native_timeout(v).expect("validated") as f64/1000.0).to_string().into()]);
    }
    args
}

#[test]
fn native_capture_exact_utf8_budget_after_redaction(){
 let d=tempfile::tempdir().unwrap();let p=d.path().join("capture");let expected="{\"text\":\"é[REDACTED]\"}\n";
 let mut c=NativeCapture::open(p.to_str().unwrap(),vec!["x".into()],expected.len()).unwrap();
 c.write(&json!({"text":"éx"})).unwrap();assert!(c.write(&json!({"text":"next"})).is_err());drop(c);
 assert_eq!(std::fs::read_to_string(p).unwrap(),expected);
}
