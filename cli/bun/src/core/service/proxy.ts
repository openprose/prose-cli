// Shared service proxy rules, independently implemented in Rust http.rs.
// Always select the dispatcher explicitly: runtime environment defaults differ between
// pinned Bun versions, including empty NO_PROXY entries and ALL_PROXY.
export function proxyFor(url: string, env: Readonly<Record<string, string | undefined>>): string | false {
  const target = new URL(url);
  const first = (names: string[]) => names.map(name => env[name]).find(value => value !== undefined && value !== "");
  const proxy = target.protocol === "https:" ? first(["https_proxy", "HTTPS_PROXY"])
    : target.protocol === "http:" ? first(["http_proxy", "HTTP_PROXY"]) : undefined;
  if (proxy === undefined || proxy === '""' || proxy === "''") return false;
  const host = target.hostname.toLowerCase().replace(/^\[|\]$/gu, "");
  const bypass = first(["no_proxy", "NO_PROXY"]);
  if (bypass !== undefined) {
    for (const raw of bypass.split(",")) {
      const entry = raw.replace(/^[\t\n\v\f\r ]+|[\t\n\v\f\r ]+$/gu, "");
      if (entry === "*") return false;
      const domain = entry.replace(/^\./u, "").toLowerCase();
      if (domain !== "" && (host === domain || host.endsWith(`.${domain}`))) return false;
    }
  }
  // A selected but invalid/unsupported proxy must fail, never connect directly.
  if (new URL(proxy).protocol !== "http:") throw new Error("unsupported service proxy");
  return proxy;
}
