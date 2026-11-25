// @vitest-environment node
// implG3 (2026-11-02)：create/stop 契约声明错误码的机械覆盖测试。
// 直接读取 contract/daemon-request/openapi.json 的声明错误集，逐一断言：
// 1. 每个声明 code 都有专属文案（不落通用兜底）；
// 2. 错误文案绝不含「成功」；
// 3. 结算未知类 code 文案含「结果未知」；未送达类含「未执行/未送达」；
// 4. SUBMIT_OUTCOME_UNKNOWN 的 operationId（error.data）出现在文案中。
// implS2 (2026-11-02, second submission)：agent.start 重新入契约（66 码）；
// start 的 SUBMIT_OUTCOME_UNKNOWN 文案是「启动受理结果未知」，不再沿用 stop 前缀。
// 读方法的失败文案表已随无消费者的 readFailureMessage 删除（2026-11-14）。
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import {
  AgentRequestError,
  agentFailureMessage,
  startFailureMessage,
  CREATE_DECLARED_ERROR_TEXT,
  START_DECLARED_ERROR_TEXT,
  STOP_DECLARED_ERROR_TEXT,
} from '../../../src/features/agents/impl/api/agent-writes';

const contractPath = fileURLToPath(
  new URL('../../../../contract/daemon-request/openapi.json', import.meta.url),
);

const document = JSON.parse(await readFile(contractPath, 'utf8'));

interface ContractEntry {
  readonly name: string;
  readonly errors: readonly { readonly code: string }[];
}

function declaredCodes(method: string): readonly string[] {
  const entry = (document['x-hyprial-methods'] as readonly ContractEntry[]).find(
    (candidate) => candidate.name === method,
  );
  expect(entry, `${method} must stay declared in the contract`).toBeTruthy();
  return (entry as ContractEntry).errors.map((error) => error.code);
}

const CREATE_CODES = declaredCodes('agent.create');
const STOP_CODES = declaredCodes('agent.stop');
const START_CODES = declaredCodes('agent.start');

it.each([agentFailureMessage, startFailureMessage])('dispatched parameter refusals do not claim the request was unsent', (project) => {
  const result = project(new AgentRequestError('INVALID_ARGUMENT', 'invalid cwd', 'dispatched'));
  expect(result.dispatch).toBe('dispatched');
  expect(result.text).toContain('参数未通过校验');
  expect(result.text).not.toContain('未发送');
});

const UNKNOWN_OUTCOME_CODES: ReadonlySet<string> = new Set([
  'SUBMIT_OUTCOME_UNKNOWN',
  'LIFECYCLE_OPERATION_UNSETTLED',
  'LIFECYCLE_OPERATION_TIMEOUT',
  'LIFECYCLE_COMPENSATION_TIMEOUT',
  'DOMAIN_COMMAND_TIMEOUT',
  'AGENT_DESTROY_SETTLEMENT_UNKNOWN',
]);

const UNDELIVERED_CODES: ReadonlySet<string> = new Set([
  'PORT_CLOSING',
  'PORT_OVERLOADED',
  'SENDER_UNRESOLVED',
  'ACTOR_STOPPED_UNDELIVERED',
  'ACTOR_RESTARTED_UNDELIVERED',
  'ACTOR_QUARANTINED_UNDELIVERED',
]);

function messageFor(code: string, data: unknown = null) {
  return agentFailureMessage(
    new AgentRequestError(code, 'contract error', 'dispatched', data, true),
  );
}

describe.each([
  ['agent.create', CREATE_CODES, CREATE_DECLARED_ERROR_TEXT],
  ['agent.stop', STOP_CODES, STOP_DECLARED_ERROR_TEXT],
] as const)('%s declared error codes have honest UI wording', (_method, codes, map) => {
  it('every declared code has a dedicated mapped text (no generic fallback)', () => {
    const unmapped = codes.filter((code) => typeof map[code] !== 'string' || map[code] === '');
    expect(unmapped).toEqual([]);
  });

  it('no mapped text claims success', () => {
    for (const code of codes) {
      const { text } = messageFor(code);
      expect(text, code).not.toMatch(/成功/);
      expect(text, code).not.toMatch(/已受理/);
    }
  });

  it('settlement-unknown codes keep the 结果未知 wording', () => {
    for (const code of codes.filter((candidate) => UNKNOWN_OUTCOME_CODES.has(candidate))) {
      const { text } = messageFor(code);
      expect(text, code).toContain('结果未知');
    }
  });

  it('undelivered codes keep the 未执行/未送达 wording', () => {
    for (const code of codes.filter((candidate) => UNDELIVERED_CODES.has(candidate))) {
      const { text } = messageFor(code);
      expect(text, code).toMatch(/未执行|未送达/);
    }
  });
});

describe('SUBMIT_OUTCOME_UNKNOWN operationId honesty', () => {
  it('the declared data.operationId survives into the UI text', () => {
    const { text, dispatch } = messageFor('SUBMIT_OUTCOME_UNKNOWN', {
      operationId: 'op-123',
    });
    expect(text).toContain('结果未知');
    expect(text).toContain('op-123');
    expect(dispatch).toBe('dispatched');
  });

  it('without data the text stays honest and never claims success', () => {
    const { text } = messageFor('SUBMIT_OUTCOME_UNKNOWN');
    expect(text).toContain('结果未知');
    expect(text).not.toMatch(/成功|已受理/);
  });
});

describe('agent.start declared error codes have honest UI wording (implS2)', () => {
  it('missing runtime points to installation and daemon PATH without exposing raw errors', () => {
    const { text } = startFailureMessage(new AgentRequestError(
      'HARNESS_BINARY_MISSING', 'private-path-and-key-sentinel', 'dispatched', null, true,
    ));
    expect(text).toContain('未找到所选运行程序');
    expect(text).toContain('安装');
    expect(text).toContain('PATH');
    expect(text).not.toMatch(/参数未通过|private-path-and-key-sentinel|结果未知/);
    expect(startFailureMessage(new AgentRequestError(
      'HARNESS_BINARY_MISSING', 'private-path-and-key-sentinel', 'unknown', null, true,
    )).text).toContain('结果未知');
  });

  it('DAEMON_NOT_READY stays undeclared and unmapped across the write corpus', () => {
    expect(CREATE_CODES).not.toContain('DAEMON_NOT_READY');
    expect(STOP_CODES).not.toContain('DAEMON_NOT_READY');
    expect(START_CODES).not.toContain('DAEMON_NOT_READY');
    expect(CREATE_DECLARED_ERROR_TEXT['DAEMON_NOT_READY']).toBeUndefined();
    expect(STOP_DECLARED_ERROR_TEXT['DAEMON_NOT_READY']).toBeUndefined();
    expect(START_DECLARED_ERROR_TEXT['DAEMON_NOT_READY']).toBeUndefined();
  });

  it('every declared code has a dedicated mapped text (no generic fallback)', () => {
    const unmapped = START_CODES.filter(
      (code) =>
        typeof START_DECLARED_ERROR_TEXT[code] !== 'string' ||
        START_DECLARED_ERROR_TEXT[code] === '',
    );
    expect(unmapped).toEqual([]);
  });

  it('no mapped text claims success', () => {
    for (const code of START_CODES) {
      const { text } = startFailureMessage(
        new AgentRequestError(code, 'contract error', 'dispatched', null, true),
      );
      expect(text, code).not.toMatch(/成功/);
    }
  });

  it('settlement-unknown codes keep the 结果未知 wording', () => {
    for (const code of START_CODES.filter((candidate) =>
      UNKNOWN_OUTCOME_CODES.has(candidate),
    )) {
      const { text } = startFailureMessage(
        new AgentRequestError(code, 'contract error', 'dispatched', null, true),
      );
      expect(text, code).toContain('结果未知');
    }
  });

  it('undelivered codes keep the 未执行/未送达 wording', () => {
    for (const code of START_CODES.filter((candidate) =>
      UNDELIVERED_CODES.has(candidate),
    )) {
      const { text } = startFailureMessage(
        new AgentRequestError(code, 'contract error', 'dispatched', null, true),
      );
      expect(text, code).toMatch(/未执行|未送达/);
    }
  });

  it('the start outcome-unknown text is start-specific with the operationId', () => {
    const { text } = startFailureMessage(
      new AgentRequestError(
        'SUBMIT_OUTCOME_UNKNOWN',
        'contract error',
        'dispatched',
        { operationId: 'op-start-1' },
        true,
      ),
    );
    expect(text).toContain('启动受理结果未知');
    expect(text).not.toContain('停止');
    expect(text).toContain('op-start-1');
  });

  it('AGENT_ALREADY_RUNNING exposes the running binding from error data', () => {
    const { text, runningBinding } = startFailureMessage(
      new AgentRequestError(
        'AGENT_ALREADY_RUNNING',
        'agent is already being served',
        'dispatched',
        {
          actor: 'agent:acme:n1:worker',
          existing: {
            actor: 'agent:acme:n1:worker',
            harness: 'claude',
            runtime: 'headless',
            sessionId: null,
            boundAtMs: 1760000000000,
          },
        },
        true,
      ),
    );
    expect(text).toContain('已在运行');
    expect(runningBinding).toEqual({
      actor: 'agent:acme:n1:worker',
      harness: 'claude',
      runtime: 'headless',
      sessionId: null,
      boundAtMs: 1760000000000,
    });
  });

  it('unknown dispatch stays 结果未知 and never claims success', () => {
    const { text, dispatch } = startFailureMessage(
      new AgentRequestError('DAEMON_ERROR', 'lost', 'unknown'),
    );
    expect(text).toContain('结果未知');
    expect(text).not.toMatch(/成功/);
    expect(dispatch).toBe('unknown');
  });
});

it('shows a definite model refusal without echoing raw diagnostics', () => {
  const error = new AgentRequestError('HARNESS_START_FAILED', 'configured Pi model is unavailable secret=not-for-ui', 'dispatched');
  expect(startFailureMessage(error)).toMatchObject({ dispatch: 'dispatched',
    text: '启动失败：未找到所选模型，请修改模型配置后重新启动。' });
});
