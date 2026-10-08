// pi provider device-code relogin helper — spawned by hyprial's
// provider_auth coordinator when a worker's OAuth refresh grant dies.
//
// Usage: node pi_device_login.mjs <piPackageRoot> <providerId>
//
// Why this runs pi's own code instead of reimplementing the flow in Python:
// the client_id, the endpoints, and the credential write (shape + file lock)
// all live in pi; a second implementation is three drift surfaces.  pi
// exports ModelRuntime from its package root, and
// ModelRuntime.login(providerId, "oauth", interaction) runs the whole flow
// and persists the credential itself — the interaction callbacks are the
// seam that makes it non-interactive.
//
// Wire contract with the Python parent (one JSON object per stdout line):
//   {"type":"device_code","userCode":…,"verificationUri":…,
//    "intervalSeconds":…,"expiresInSeconds":…}   — the two human-facing
//    values, exactly as pi's AuthEvent delivers them.
//   {"type":"ok"}                                 — login completed and the
//    credential is written; exit 0.
// Exit codes: 0 success; 3 the device code expired (pi's "Device flow timed
// out" family — the parent may start another round); 2 unexpected prompt
// (flow drifted from what we scripted); 1 anything else.
//
// ⛔ Security: the polling secret (device_code / device_auth_id) NEVER
// leaves this process — it is not printed, not logged, not written anywhere
// by us (pi's own AuthStorage writes the resulting credential to auth.json;
// that is pi's job, not ours).  stderr carries only the error class name,
// never the provider's error body (token endpoints echo request fields back).

const [, , piPackageRoot, providerId] = process.argv;

if (!piPackageRoot || !providerId) {
  console.error("usage: node pi_device_login.mjs <piPackageRoot> <providerId>");
  process.exit(1);
}

const runtime = await import(`${piPackageRoot}/dist/index.js`);
const { ModelRuntime } = runtime;

const modelRuntime = await ModelRuntime.create({
  allowModelNetwork: false,
  refreshOnCreate: false,
});

let announced = false;

function notify(event) {
  if (event.type !== "device_code" || announced) {
    return;
  }
  announced = true;
  process.stdout.write(
    JSON.stringify({
      type: "device_code",
      userCode: event.userCode,
      verificationUri: event.verificationUri,
      intervalSeconds: event.intervalSeconds ?? null,
      expiresInSeconds: event.expiresInSeconds ?? null,
    }) + "\n",
  );
}

function prompt(promptSpec) {
  // openai-codex asks browser-vs-device first; we always take the device
  // flow.  kimi-coding never prompts.  Anything else means the provider's
  // flow drifted — fail loudly rather than improvising an answer.
  if (promptSpec.type === "select") {
    const device = promptSpec.options.find((option) => option.id === "device_code");
    if (device) {
      return Promise.resolve("device_code");
    }
  }
  return Promise.reject(new Error(`unexpected-prompt:${promptSpec.type}`));
}

try {
  await modelRuntime.login(providerId, "oauth", { prompt, notify });
  process.stdout.write(JSON.stringify({ type: "ok" }) + "\n");
  process.exit(0);
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  if (message.startsWith("unexpected-prompt:")) {
    console.error(message);
    process.exit(2);
  }
  if (message.includes("Device flow timed out")) {
    console.error("device-flow-timed-out");
    process.exit(3);
  }
  console.error("login-failed");
  process.exit(1);
}
