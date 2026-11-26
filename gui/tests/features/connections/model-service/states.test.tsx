import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CredentialsSection } from "../../../../src/features/connections/CredentialsSection";
import { ConnectionProbe } from "../../../../src/features/connections/impl/model-connections/Probe";
import type {
  CredentialEntry,
  CredentialProbe,
  SharedCredentialsApi,
  SharedCredentialWrite,
} from "../../../../src/features/connections/shared-credentials-api";
import { DomainError } from "../../../../src/client";
const metadata = {
  version: 1,
  endpoint: "https://model.example/v1",
  protocol: "openai-completions",
  profile: "openai-chat-completions/v1",
  preset: { id: "custom", version: 1 },
  connectionRef: "0123456789abcdef0123456789abcdef",
  configRevision: 4,
} as const;
const entry: CredentialEntry = {
  keyId: "gateway:key",
  provider: "connection",
  label: "gateway",
  endpoint: metadata.endpoint,
  protocol: metadata.protocol,
  connection: metadata,
  state: "active",
  createdAt: null,
  lastUsedAt: null,
  grants: [],
};
function setup(
  overrides: Partial<SharedCredentialsApi> = {},
): SharedCredentialsApi {
  return {
    listKeys: vi.fn(async () => [entry]),
    listSecretGrants: async () => [],
    addKey: vi.fn(),
    replaceKey: vi.fn(),
    revokeGrant: vi.fn(),
    configureKey: vi.fn(async () => ({ status: "saved" as const })),
    testKey: vi.fn(async () => ({
      kind: "model" as const,
      ok: true,
      status: 200,
      latencyMs: 5,
    })),
    ...overrides,
  };
}
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.setPointerCapture ??= () => {};
  window.Element.prototype.releasePointerCapture ??= () => {};
});
async function form(api: SharedCredentialsApi) {
  const user = userEvent.setup();
  const rendered = render(<CredentialsSection api={api} />);
  await screen.findByText("gateway");
  await user.click(screen.getByRole("button", { name: "添加连接" }));
  await user.type(screen.getByLabelText("名称"), "new-connection");
  await user.type(
    screen.getByLabelText("服务地址"),
    "https://model.example/v1",
  );
  await user.type(screen.getByLabelText("Key"), "synthetic");
  return { user, rendered };
}
it("locks unknown writes until successful readback, never replays them", async () => {
  const api = setup({
    configureKey: vi.fn(async () => ({
      status: "unknown" as const,
      message: "结果未知",
    })),
  });
  const { user } = await form(api);
  await user.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByText("结果未知")).toBeVisible();
  expect(screen.getByRole("button", { name: "添加连接" })).toBeDisabled();
  vi.mocked(api.listKeys).mockRejectedValueOnce(new Error("read fail"));
  await user.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("凭据读取失败，请刷新后重试。");
  expect(screen.getByRole("button", { name: "添加连接" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("gateway");
  expect(screen.getByRole("button", { name: "添加连接" })).toBeEnabled();
  expect(api.configureKey).toHaveBeenCalledTimes(1);
});
it("locks a confirmed write whose following inventory read failed", async () => {
  const api = setup();
  const { user } = await form(api);
  let fail!: (error: Error) => void;
  vi.mocked(api.listKeys).mockImplementationOnce(
    () =>
      new Promise((_, reject) => {
        fail = reject;
      }),
  );
  await user.click(screen.getByRole("button", { name: "保存" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => fail(new Error("lost readback")));
  await screen.findByText(/已保存，但清单回读失败/);
  expect(screen.getByRole("button", { name: "添加连接" })).toBeDisabled();
  expect(api.configureKey).toHaveBeenCalledTimes(1);
});
it("does not publish a late write after leaving the page", async () => {
  let resolve!: (r: SharedCredentialWrite) => void;
  const changed = vi.fn();
  const api = setup({
    configureKey: () =>
      new Promise((r) => {
        resolve = r;
      }),
  });
  const { user, rendered } = await form(api);
  rendered.rerender(<CredentialsSection api={api} onKeysChanged={changed} />);
  await user.click(screen.getByRole("button", { name: "保存" }));
  rendered.unmount();
  await act(async () => resolve({ status: "saved" as const }));
  expect(changed).not.toHaveBeenCalled();
  expect(api.listKeys).toHaveBeenCalledTimes(1);
});
it("uses saved revision without asking for or echoing a Key and aborts on Escape", async () => {
  const api = setup({
    testKey: vi.fn(() => new Promise<CredentialProbe>(() => {})),
  });
  const user = userEvent.setup();
  render(<CredentialsSection api={api} />);
  await screen.findByText("gateway");
  await user.click(screen.getByRole("button", { name: "查看详情" }));
  await user.click(screen.getByRole("button", { name: "测试已保存连接" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).queryByLabelText("Key")).toBeNull();
  await user.type(within(dialog).getByLabelText("测试模型 ID"), "hy4-flash");
  await user.click(within(dialog).getByRole("button", { name: "测试" }));
  expect(api.testKey).toHaveBeenCalledWith(
    {
      connection: {
        entryId: "gateway",
        fieldName: "key",
        connectionRef: metadata.connectionRef,
        configRevision: 4,
      },
      model: "hy4-flash",
    },
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
  );
  const signal = vi.mocked(api.testKey!).mock.calls[0][1]!.signal!;
  await user.keyboard("{Escape}");
  expect(signal.aborted).toBe(true);
});
it("cancels, ignores late completion and can start a new explicit test", async () => {
  const finishes: Array<(r: CredentialProbe) => void> = [];
  const api = setup({
    testKey: vi.fn(() => new Promise<CredentialProbe>((r) => finishes.push(r))),
  });
  const user = userEvent.setup();
  render(
    <ConnectionProbe
      api={api}
      identity="one"
      request={(model) => ({
        provider: "custom",
        secret: "synthetic",
        model,
        endpoint: metadata.endpoint,
        protocol: metadata.protocol,
      })}
    />,
  );
  await user.type(screen.getByLabelText("测试模型 ID"), "hy4-flash");
  await user.click(screen.getByRole("button", { name: "测试" }));
  const first = vi.mocked(api.testKey!).mock.calls[0][1]!.signal!;
  await user.click(screen.getByRole("button", { name: "取消测试" }));
  expect(first.aborted).toBe(true);
  await user.click(screen.getByRole("button", { name: "测试" }));
  await act(async () =>
    finishes[0]({ kind: "model", ok: true, status: 200, latencyMs: 1 }),
  );
  expect(screen.queryByText(/测试通过/)).toBeNull();
  await act(async () =>
    finishes[1]({
      kind: "model",
      ok: false,
      status: 401,
      latencyMs: 2,
      outcome: "authentication-rejected",
    }),
  );
  expect(screen.getByText(/Key 被模型服务拒绝/)).toBeVisible();
  expect(api.testKey).toHaveBeenCalledTimes(2);
});
it("shows stale revision as refresh-required without retry or private diagnostic", async () => {
  const error = new DomainError(
    "MODEL_CONFIGURATION_CONFLICT",
    "private-diagnostic",
    null,
    {
      method: "agent.secret.provider-test",
      requestId: null,
      status: 409,
      dispatch: "dispatched",
      catalogDigest: null,
      attempts: 1,
      startedAt: 0,
      durationMs: 1,
    },
  );
  const api = setup({
    testKey: vi.fn(async () => {
      throw error;
    }),
  });
  const user = userEvent.setup();
  render(
    <ConnectionProbe
      api={api}
      identity="one"
      request={(model) => ({
        connection: {
          entryId: "gateway",
          fieldName: "key",
          connectionRef: metadata.connectionRef,
          configRevision: 4,
        },
        model,
      })}
    />,
  );
  await user.type(screen.getByLabelText("测试模型 ID"), "hy4-flash");
  await user.click(screen.getByRole("button", { name: "测试" }));
  expect(await screen.findByText(/连接已变更/)).toBeVisible();
  expect(screen.queryByText(/private-diagnostic/)).toBeNull();
  expect(api.testKey).toHaveBeenCalledTimes(1);
});
