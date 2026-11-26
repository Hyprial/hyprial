import {
  createSharedCredentialsApiFromClient,
  connectionLabel,
} from "../../../../src/features/connections/shared-credentials-api";

const connection = {
  version: 1,
  endpoint: "https://gateway.example/v1",
  protocol: "openai-completions",
  profile: "openai-chat-completions/v1",
  preset: { id: "custom", version: 1 },
} as const;
const metadata = {
  ...connection,
  connectionRef: "0123456789abcdef0123456789abcdef",
  configRevision: 1,
};
function setup() {
  const secret = {
    providerList: vi.fn(async () => ({
      ok: true,
      entries: [
        {
          entryId: "gateway",
          fieldNames: ["key"],
          connections: { key: metadata },
        },
        { entryId: "legacy", fieldNames: ["DEEPSEEK_API_KEY"] },
      ],
    })),
    providerConfigure: vi.fn(
      async (p: {
        entryId: string;
        fieldName: string;
        expectedRevision: number;
      }) => ({
        ok: true,
        entryId: p.entryId,
        fieldNames: [p.fieldName],
        connections: {
          [p.fieldName]: {
            ...metadata,
            configRevision: p.expectedRevision + 1,
          },
        },
      }),
    ),
    providerWrite: vi.fn(),
    providerTest: vi.fn(async () => ({
      ok: true,
      outcome: "passed",
      httpStatus: 200,
      latencyMs: 2,
    })),
  };
  return {
    secret,
    api: createSharedCredentialsApiFromClient({ agent: { secret } } as never),
  };
}
it("keeps generic connections distinct from legacy vendor keys", async () => {
  const { api, secret } = setup();
  const rows = await api.listKeys();
  expect(secret.providerList).toHaveBeenCalledWith({ connectionVersion: 1 });
  expect(rows[0]).toMatchObject({
    provider: "connection",
    keyId: "gateway:key",
    connection: metadata,
  });
  expect(rows[1]).toMatchObject({ provider: "deepseek" });
});
it("saves a generic connection atomically without model or vendor default", async () => {
  const { api, secret } = setup();
  expect(
    await api.configureKey!({
      label: "gateway",
      value: "synthetic",
      connection,
    }),
  ).toMatchObject({ status: "saved" });
  expect(secret.providerConfigure).toHaveBeenCalledExactlyOnceWith({
    entryId: "gateway",
    fieldName: "key",
    value: "synthetic",
    connection,
    expectedRevision: 0,
  });
  expect(secret.providerWrite).not.toHaveBeenCalled();
  expect(secret.providerTest).not.toHaveBeenCalled();
});
it("rotates by exact connection revision and verifies the next receipt", async () => {
  const { api, secret } = setup();
  const [row] = await api.listKeys();
  expect(await api.replaceConnection!(row, "new-synthetic")).toMatchObject({
    status: "saved",
  });
  expect(secret.providerConfigure).toHaveBeenCalledWith({
    entryId: "gateway",
    fieldName: "key",
    value: "new-synthetic",
    connection,
    expectedRevision: 1,
  });
  secret.providerConfigure.mockResolvedValueOnce({
    ok: true,
    entryId: "gateway",
    fieldNames: ["key"],
    connections: { key: metadata },
  });
  expect(await api.replaceConnection!(row, "new-synthetic")).toMatchObject({
    status: "unknown",
  });
});
it("tests exact saved selection with one 30s request and cancellable 35s envelope", async () => {
  const { api, secret } = setup();
  const controller = new AbortController();
  const selection = {
    entryId: "gateway",
    fieldName: "key",
    connectionRef: metadata.connectionRef,
    configRevision: 1,
  };
  await api.testKey!(
    { connection: selection, model: "hy4-flash" },
    { signal: controller.signal },
  );
  expect(secret.providerTest).toHaveBeenCalledExactlyOnceWith(
    { connection: selection, model: "hy4-flash", timeoutSeconds: 30 },
    { signal: controller.signal, budgetMs: 35000 },
  );
});
it.each([" hy4-flash", "hy4-flash ", "hy4\nflash", ""])(
  "refuses ambiguous model bytes before transport: %j",
  async (model) => {
    const { api, secret } = setup();
    await expect(
      api.testKey!({
        provider: "custom",
        secret: "synthetic",
        model,
        endpoint: connection.endpoint,
        protocol: connection.protocol,
      }),
    ).rejects.toThrow();
    expect(secret.providerTest).not.toHaveBeenCalled();
  },
);
it("never substitutes a stored connection on malformed metadata", async () => {
  const { api, secret } = setup();
  secret.providerList.mockResolvedValueOnce({
    ok: true,
    entries: [
      {
        entryId: "gateway",
        fieldNames: ["key"],
        connections: { key: { ...metadata, protocol: "unsupported" } },
      },
    ],
  } as never);
  await expect(api.listKeys()).rejects.toThrow();
});

it("checks the configure capability for connection rotation, legacy write for old keys", async () => {
  const { secret } = setup();
  const access = (method: string) =>
    ({
      available: method !== "agent.secret.provider-write",
      code: "METHOD_NOT_FOUND",
      reason: "unsupported",
    }) as const;
  const api = createSharedCredentialsApiFromClient(
    { agent: { secret } } as never,
    access,
  );
  const [connectionRow, legacyRow] = await api.listKeys();
  expect(api.refusal?.("replace", connectionRow)).toBeNull();
  expect(api.refusal?.("replace", legacyRow)).toMatchObject({
    source: "agent.secret.provider-write",
  });
});
it("does not label a custom endpoint official merely because its preset ID matches", async () => {
  const saved = { ...metadata, preset: { id: "openai", version: 1 } };
  const providerList = vi.fn(async () => ({
    ok: true,
    entries: [
      { entryId: "gateway", fieldNames: ["key"], connections: { key: saved } },
    ],
    presets: [
      {
        label: "OpenAI",
        connection: {
          ...connection,
          endpoint: "https://official.example/v1",
          preset: saved.preset,
        },
      },
    ],
  }));
  const api = createSharedCredentialsApiFromClient({
    agent: { secret: { providerList } },
  } as never);
  const [row] = await api.listKeys();
  expect(connectionLabel(row)).toBe("自定义连接");
});
