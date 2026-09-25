#[path = "build_support.rs"]
mod build_support;

use std::env;

const BUILD_VERSION_ENV: &str = "OPENPROSE_BUILD_VERSION";
const COMPILED_VERSION_ENV: &str = "OPENPROSE_COMPILED_BUILD_VERSION";

fn main() {
    let image_inputs = [
        "OPENPROSE_IMAGE_SOURCE_DIR",
        "OPENPROSE_IMAGE_BUNDLE",
        "OPENPROSE_IMAGE_BUNDLE_CHECKSUM",
    ];
    for key in image_inputs
        .iter()
        .chain(["CARGO_FEATURE_TEST_SEAMS"].iter())
    {
        println!("cargo:rerun-if-env-changed={key}");
    }
    let published = env::var_os("CARGO_FEATURE_TEST_SEAMS").is_none()
        && image_inputs.iter().all(|key| env::var_os(key).is_none());
    println!(
        "cargo:rustc-env=OPENPROSE_KERNEL_STARTUP={}",
        if published { "published" } else { "embedded" }
    );
    println!("cargo:rerun-if-env-changed={BUILD_VERSION_ENV}");

    let package_version = env::var("CARGO_PKG_VERSION").expect("Cargo package version is present");
    let version = match env::var_os(BUILD_VERSION_ENV) {
        Some(value) => value
            .into_string()
            .unwrap_or_else(|_| panic!("{BUILD_VERSION_ENV} must be valid UTF-8 SemVer")),
        None => package_version,
    };
    build_support::validate_semver(&version)
        .unwrap_or_else(|reason| panic!("{BUILD_VERSION_ENV} must be valid SemVer: {reason}"));
    println!("cargo:rustc-env={COMPILED_VERSION_ENV}={version}");
}
