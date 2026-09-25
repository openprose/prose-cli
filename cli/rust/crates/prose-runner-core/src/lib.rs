//! Runtime-neutral mechanics for the `OpenProse` shell runner.
//!
//! This crate deliberately does not depend on, parse, or interpret the
//! `OpenProse` language. It protects the invocation and transport envelope.
#![allow(clippy::too_many_arguments, clippy::too_many_lines)]

#[cfg(test)]
#[path = "../build_support.rs"]
mod build_support;

pub mod config;
pub mod error;
pub mod image;
pub mod installed_adapters;
pub mod invocation;
pub mod output;
mod prime_owned_service;
pub mod runner;
pub mod runtime;

pub use config::{ConfigSource, EffectiveConfig, SystemContext, resolve_config};
pub use error::{ErrorCode, RunnerError};
pub use invocation::{
    Action, GlobalFlags, OutputMode, ParsedInvocation, RunnerCommand, parse_invocation,
};
pub use prose_process_supervisor::{CancellationToken, SignalCancellationGuard};
pub use runtime::{Clock, IdSource, SystemClock, SystemIdSource};

pub mod kernel_startup;

mod credential_store;
pub mod registry;
pub mod service;
pub mod service_account;
