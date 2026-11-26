import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CredentialsSection } from "../../../../src/features/connections/CredentialsSection";
import type {
  SharedCredentialsApi,
  CredentialProbe,
} from "../../../../src/features/connections/shared-credentials-api";
import { keyCompatibleWithHarness } from "../../../../src/features/agents/impl/AgentForm";

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

function setup() {
  const api: SharedCredentialsApi = {
    listKeys: vi.fn(async () => []),
    listSecretGrants: async () => [],
    addKey: vi.fn(async () => ({ status: "saved" as const })),
    configureKey: vi.fn(async () => ({ status: "saved" as const })),
    replaceKey: vi.fn(async () => ({ status: "saved" as const })),
    revokeGrant: vi.fn(async () => ({ status: "saved" as const })),
    testKey: vi.fn(async () => ({
      kind: "model" as const,
      status: 200,
      ok: true,
      latencyMs: 10,
    })),
  };
  return api;
}

async function openCustom() {
  const user = userEvent.setup();
  await screen.findByText("暂无 Key");
  await user.click(screen.getByRole("button", { name: "添加连接" }));
  await user.type(screen.getByLabelText("名称"), "gateway");
  await user.type(
    screen.getByLabelText("服务地址"),
    "http://127.0.0.1:45678/v1",
  );
  await user.type(screen.getByLabelText("Key"), "synthetic");
  return user;
}

it("uses configure for custom save without a hidden probe or ordinary write", async () => {
  const api = setup();
  render(<CredentialsSection api={api} />);
  const user = await openCustom();
  expect(screen.queryByText(/deepseek-flash/)).toBeNull();
  await user.click(screen.getByRole("button", { name: "保存" }));
  expect(api.configureKey).toHaveBeenCalledExactlyOnceWith({
    label: "gateway",
    value: "synthetic",
    connection: {
      version: 1,
      endpoint: "http://127.0.0.1:45678/v1",
      protocol: "openai-completions",
      profile: "openai-chat-completions/v1",
      preset: { id: "custom", version: 1 },
    },
  });
  expect(api.addKey).not.toHaveBeenCalled();
  expect(api.testKey).not.toHaveBeenCalled();
});

it("discards a late probe result after closing and reopening the form", async () => {
  const api = setup();
  let finish!: (value: CredentialProbe) => void;
  api.testKey = vi.fn(
    () =>
      new Promise<CredentialProbe>((resolve) => {
        finish = resolve;
      }),
  );
  render(<CredentialsSection api={api} />);
  const user = await openCustom();
  await user.type(screen.getByLabelText("测试模型 ID"), "deepseek-flash");
  await user.click(screen.getByRole("button", { name: "测试" }));
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  const signal = vi.mocked(api.testKey!).mock.calls[0][1]?.signal;
  expect(signal?.aborted).toBe(false);
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(signal?.aborted).toBe(true);
  await user.click(screen.getByRole("button", { name: "添加连接" }));
  await act(async () =>
    finish({ kind: "model", status: 200, ok: true, latencyMs: 10 }),
  );
  expect(screen.queryByText(/测试通过/)).toBeNull();
  expect(screen.getByLabelText("Key")).toHaveValue("");
  expect(api.configureKey).not.toHaveBeenCalled();
});

it("admits only an explicit supported Pi route, while retaining ordinary compatibility", () => {
  expect(
    keyCompatibleWithHarness(
      "pi",
      "deepseek",
      "https://example.invalid/v1",
      "openai-completions",
    ),
  ).toBe(true);
  expect(
    keyCompatibleWithHarness("pi", "deepseek", "https://example.invalid/v1"),
  ).toBe(false);
  expect(
    keyCompatibleWithHarness(
      "codex",
      "deepseek",
      "https://example.invalid/v1",
      "openai-completions",
    ),
  ).toBe(false);
  expect(
    keyCompatibleWithHarness(
      "pi",
      "deepseek",
      "https://example.invalid/v1",
      "anthropic-messages",
    ),
  ).toBe(false);
  expect(keyCompatibleWithHarness("pi", "deepseek")).toBe(true);
});

it("does not advertise unsupported protocols or disguise generic connections as DeepSeek", async () => {
  const api = setup();
  render(<CredentialsSection api={api} />);
  const user = await openCustom();
  expect(screen.queryByLabelText("模型服务商")).toBeNull();
  expect(screen.getByLabelText("兼容协议")).toHaveTextContent(
    "OpenAI Chat Completions",
  );
  expect(
    keyCompatibleWithHarness(
      "pi",
      "connection",
      "https://gateway.example/v1",
      "openai-completions",
    ),
  ).toBe(false);
  await user.click(screen.getByRole("button", { name: "取消" }));
});
