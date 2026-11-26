import { createSharedCredentialsApiFromClient } from "../../../../src/features/connections/shared-credentials-api";
import { DomainError } from "../../../../src/client";

const route = {
  endpoint: "http://127.0.0.1:45678/v1",
  protocol: "openai-completions",
  vendorId: "deepseek",
} as const;
const connection = {
  version: 1,
  endpoint: route.endpoint,
  protocol: route.protocol,
  profile: "openai-chat-completions/v1",
  preset: { id: "custom", version: 1 },
} as const;
const input = {
  label: "private-service",
  value: "synthetic-test-value",
  connection,
};

function setup(entries: unknown[] = []) {
  const secret = {
    providerList: vi.fn(async () => ({ ok: true, entries })),
    providerWrite: vi.fn(),
    providerConfigure: vi.fn(
      async (params: { entryId: string; fieldName: string }) => ({
        ok: true,
        entryId: params.entryId,
        fieldNames: [params.fieldName],
        connections: {
          [params.fieldName]: {
            ...connection,
            connectionRef: "0123456789abcdef0123456789abcdef",
            configRevision: 1,
          },
        },
      }),
    ),
    providerTest: vi.fn(async () => ({
      ok: true,
      outcome: "authentication-rejected",
      httpStatus: 401,
      latencyMs: 12,
    })),
  };
  return {
    secret,
    api: createSharedCredentialsApiFromClient({ agent: { secret } } as never),
  };
}

it("keeps the exact field selector and projects the declared route vendor", async () => {
  const { api } = setup([
    {
      entryId: "private-service",
      fieldNames: ["token"],
      routes: { token: route },
    },
  ]);
  expect(await api.listKeys()).toEqual([
    expect.objectContaining({
      keyId: "private-service:token",
      provider: "deepseek",
      endpoint: route.endpoint,
      protocol: route.protocol,
    }),
  ]);
});

it("refuses malformed route metadata instead of treating it as an ordinary key", async () => {
  const { api } = setup([
    {
      entryId: "private-service",
      fieldNames: ["token"],
      routes: { token: { ...route, protocol: "unknown" } },
    },
  ]);
  await expect(api.listKeys()).rejects.toThrow();
});

it("saves address and value together through configure, never the ordinary writer", async () => {
  const { api, secret } = setup();
  expect((await api.configureKey!(input)).status).toBe("saved");
  expect(secret.providerConfigure).toHaveBeenCalledExactlyOnceWith({
    entryId: input.label,
    fieldName: "key",
    value: input.value,
    connection,
    expectedRevision: 0,
  });
  expect(secret.providerList).not.toHaveBeenCalled();
  expect(secret.providerWrite).not.toHaveBeenCalled();
});

it("projects the daemon rejection of an existing name without retrying or listing", async () => {
  const { api, secret } = setup();
  secret.providerConfigure.mockRejectedValueOnce(
    new DomainError("INVALID_ARGUMENT", "Entry already exists", null, {
      method: "agent.secret.provider-configure",
      requestId: null,
      status: 400,
      dispatch: "dispatched",
      catalogDigest: null,
      attempts: 1,
      startedAt: 0,
      durationMs: 1,
    }),
  );
  expect((await api.configureKey!(input)).status).toBe("failed");
  expect(secret.providerConfigure).toHaveBeenCalledTimes(1);
  expect(secret.providerList).not.toHaveBeenCalled();
});

it("reports a mismatched route receipt as unknown without retrying", async () => {
  const { api, secret } = setup();
  secret.providerConfigure.mockResolvedValueOnce({
    ok: true,
    entryId: input.label,
    fieldNames: ["key"],
    connections: {},
  });
  expect((await api.configureKey!(input)).status).toBe("unknown");
  expect(secret.providerConfigure).toHaveBeenCalledTimes(1);
});

it("does not turn successful probe transport into credential success or persistence", async () => {
  const { api, secret } = setup();
  const result = await api.testKey!({
    provider: "deepseek",
    secret: input.value,
    model: "deepseek-flash",
    endpoint: route.endpoint,
    protocol: route.protocol,
  });
  expect(result).toMatchObject({
    ok: false,
    outcome: "authentication-rejected",
    status: 401,
  });
  expect(secret.providerWrite).not.toHaveBeenCalled();
  expect(secret.providerConfigure).not.toHaveBeenCalled();
});

it("leaves optional methods absent on an older client without downgrading", () => {
  const api = createSharedCredentialsApiFromClient({
    agent: { secret: { providerList: vi.fn(), providerWrite: vi.fn() } },
  } as never);
  expect(api.configureKey).toBeUndefined();
  expect(api.testKey).toBeUndefined();
});
