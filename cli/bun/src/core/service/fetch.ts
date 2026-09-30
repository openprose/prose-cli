import { request, ProxyAgent, Client, errors, type Dispatcher } from "undici/index.js";
import { proxyFor } from "./proxy";

const proxies = new Map<string, ProxyAgent>();
// Explicit dependency injection for provider-free unit tests.
export const serviceHttp = { fetch: rawFetch };

export function serviceFetch(url: string, options: RequestInit, env: Readonly<Record<string, string | undefined>>): Promise<Response> {
  return serviceHttp.fetch(url, options, env);
}

/** Pinned Bun's native fetch inherits proxy defaults that disagree with the
 * shared contract. Use Undici's raw transport with an explicit dispatcher;
 * avoid both runtime proxy inheritance and mutation of process environment. */
async function rawFetch(url: string, options: RequestInit, env: Readonly<Record<string, string | undefined>>): Promise<Response> {
  const proxy = proxyFor(url, env);
  let dispatcher: ProxyAgent | undefined;
  if (proxy !== false) {
    dispatcher = proxies.get(proxy);
    if (dispatcher === undefined) { dispatcher = new ProxyAgent({ uri: proxy, clientFactory(origin, options) {
      const client = new Client(origin, options as Client.Options);
      const connect = client.connect.bind(client);
      // A dropped CONNECT is final. Undici otherwise reconnects to the proxy
      // while waiting for a tunnel, which violates the no-retry contract.
      client.connect = ((options: Parameters<Client["connect"]>[0]) =>
        connect(options).catch(() => { throw new errors.RequestAbortedError("service proxy connection failed"); })
      ) as Client["connect"];
      return client;
    } }); proxies.set(proxy, dispatcher); }
  }
  const headers = Object.fromEntries(new Headers(options.headers).entries());
  let response;
  try {
    response = await request(url, {
      method: (options.method ?? "GET") as Dispatcher.HttpMethod, headers,
      ...(options.body == null ? {} : { body: options.body as string | Uint8Array }),
      ...(options.signal == null ? {} : { signal: options.signal }), bodyTimeout: 0, headersTimeout: 0, idempotent: false,
      ...(dispatcher === undefined ? {} : { dispatcher }),
    });
  } catch (error) {
    if (dispatcher !== undefined) { proxies.delete(proxy as string); await dispatcher.destroy(); }
    throw error;
  }
  const iterator = response.body[Symbol.asyncIterator]();
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try { const next = await iterator.next(); if (next.done) controller.close(); else controller.enqueue(next.value); }
      catch (error) { controller.error(error); }
    },
    async cancel() { response.body.destroy(); await iterator.return?.(); },
  });
  const outputHeaders = new Headers();
  for (const [name, value] of Object.entries(response.headers)) {
    if (Array.isArray(value)) for (const entry of value) outputHeaders.append(name, entry);
    else if (value !== undefined) outputHeaders.set(name, value);
  }
  const empty = options.method === "HEAD" || [204, 205, 304].includes(response.statusCode);
  if (empty) { response.body.destroy(); await iterator.return?.(); }
  return new Response(empty ? null : body, { status: response.statusCode, headers: outputHeaders });
}
