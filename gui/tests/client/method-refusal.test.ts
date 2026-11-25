/*
 * Decision record (unavailable copy to notifications, PR 1f/F0b, 2026-11-14):
 * contract of the one refusal classifier. A refusal is a fact known before
 * dispatch (the pinned catalog or the composition did not admit a method); it
 * carries machine tokens only (`code`, `method`). The catalog reason sentence
 * is kept on the error as `reason` for transitional presentation but is never
 * part of `message` and never part of the classification result.
 */
import { describe, expect, it } from 'vitest';
import {
  DomainError,
  HttpError,
  TransportError,
  type MethodAccess,
  type RequestDiagnostics,
} from '../../src/client';
import {
  METHOD_REFUSAL_CODES,
  MethodRefusedError,
  accessRefusal,
  assertMethodAvailable,
  methodRefusal,
} from '../../src/client/refusal';
import { unavailableNotice } from '../../src/components/notifications';

function diagnostics(method: string, dispatch: RequestDiagnostics['dispatch']): RequestDiagnostics {
  return { method, requestId: null, status: null, dispatch, catalogDigest: 'd', attempts: 0, startedAt: 0, durationMs: 0 };
}

const refusing = (code?: string, reason?: string): MethodAccess => () => ({
  available: false,
  ...(code === undefined ? {} : { code }),
  ...(reason === undefined ? {} : { reason }),
});

describe('METHOD_REFUSAL_CODES', () => {
  it('lists the catalog and capability refusal codes', () => {
    expect([...METHOD_REFUSAL_CODES].sort()).toEqual([
      'GUI_CAPABILITY_UNAVAILABLE',
      'M1_WRITE_UNAVAILABLE',
      'METHOD_NOT_AVAILABLE',
      'METHOD_NOT_CALLABLE',
      'METHOD_NOT_FOUND',
      'WRITE_METHOD_UNVERIFIED',
    ]);
  });
});

describe('assertMethodAvailable', () => {
  it('passes when there is no access function or the method is available', () => {
    expect(() => assertMethodAvailable(undefined, 'agent.list')).not.toThrow();
    expect(() => assertMethodAvailable(() => ({ available: true }), 'agent.list')).not.toThrow();
  });

  it('throws a not-dispatched refusal with the catalog code and keeps the reason out of the message', () => {
    const reason = '当前后端目录没有声明 agent.create（METHOD_NOT_FOUND）。';
    let caught: unknown;
    try {
      assertMethodAvailable(refusing('METHOD_NOT_FOUND', reason), 'agent.create', { name: 'a' });
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(MethodRefusedError);
    const error = caught as MethodRefusedError;
    expect(error.method).toBe('agent.create');
    expect(error.code).toBe('METHOD_NOT_FOUND');
    expect(error.dispatch).toBe('not-dispatched');
    expect(error.message).toBe('METHOD_NOT_FOUND: agent.create');
    expect(error.message).not.toContain(reason);
    expect(error.reason).toBe(reason);
  });

  it('defaults the code to METHOD_NOT_AVAILABLE and forwards params to the access function', () => {
    const seen: unknown[] = [];
    const access: MethodAccess = (method, params) => {
      seen.push([method, params]);
      return { available: false };
    };
    expect(() => assertMethodAvailable(access, 'x.y', { a: 1 })).toThrowError('METHOD_NOT_AVAILABLE: x.y');
    expect(seen).toEqual([['x.y', { a: 1 }]]);
  });
});

describe('methodRefusal', () => {
  it('classifies the refusal error, a not-dispatched client error and a plain carrier', () => {
    expect(methodRefusal(new MethodRefusedError('agent.list', 'WRITE_METHOD_UNVERIFIED')))
      .toEqual({ code: 'WRITE_METHOD_UNVERIFIED', source: 'agent.list' });
    expect(methodRefusal(new TransportError('configuration', 'METHOD_NOT_CALLABLE', '文本', diagnostics('org.create', 'not-dispatched'))))
      .toEqual({ code: 'METHOD_NOT_CALLABLE', source: 'org.create' });
    expect(methodRefusal({ code: 'GUI_CAPABILITY_UNAVAILABLE', dispatch: 'not-dispatched', diagnostics: { method: 'message.send' } }))
      .toEqual({ code: 'GUI_CAPABILITY_UNAVAILABLE', source: 'message.send' });
    expect(methodRefusal({ code: 'GUI_CAPABILITY_UNAVAILABLE', dispatch: 'not-dispatched', data: { method: 'agent.create' } }))
      .toEqual({ code: 'GUI_CAPABILITY_UNAVAILABLE', source: 'agent.create' });
    expect(methodRefusal({ code: 'M1_WRITE_UNAVAILABLE', dispatch: 'not-dispatched' }))
      .toEqual({ code: 'M1_WRITE_UNAVAILABLE' });
  });

  it('classifies every refusal code', () => {
    for (const code of METHOD_REFUSAL_CODES) {
      expect(methodRefusal(new MethodRefusedError('m', code))?.code).toBe(code);
    }
  });

  it('returns null for anything that is not a refusal before dispatch', () => {
    expect(methodRefusal(new DomainError('CALLER_NOT_AUTHORIZED', 'x', null, diagnostics('m', 'dispatched')))).toBeNull();
    expect(methodRefusal(new HttpError('INTERNAL', 'x', 500, diagnostics('m', 'dispatched')))).toBeNull();
    expect(methodRefusal(new TransportError('network', 'NETWORK', 'x', diagnostics('m', 'not-dispatched')))).toBeNull();
    expect(methodRefusal(new TransportError('configuration', 'METHOD_NOT_FOUND', 'x', diagnostics('m', 'dispatched')))).toBeNull();
    expect(methodRefusal(new Error('METHOD_NOT_FOUND'))).toBeNull();
    expect(methodRefusal({ code: 'METHOD_NOT_FOUND' })).toBeNull();
    expect(methodRefusal(null)).toBeNull();
    expect(methodRefusal(undefined)).toBeNull();
    expect(methodRefusal('METHOD_NOT_FOUND')).toBeNull();
  });
});

describe('accessRefusal', () => {
  it('is null without an access function and when the method is callable', () => {
    expect(accessRefusal(undefined, 'agent.create')).toBeNull();
    expect(accessRefusal(() => ({ available: true }), 'agent.create')).toBeNull();
  });

  it('returns the code and the method as source when refused, without the reason', () => {
    const refusal = accessRefusal(refusing('METHOD_NOT_FOUND', '一句人话'), 'agent.create');
    expect(refusal).toEqual({ code: 'METHOD_NOT_FOUND', source: 'agent.create' });
    expect(JSON.stringify(refusal)).not.toContain('一句人话');
    expect(accessRefusal(refusing(), 'agent.create')).toEqual({ code: 'METHOD_NOT_AVAILABLE', source: 'agent.create' });
  });

  it('feeds the notification entry with `method · CODE` and no sentence of its own', () => {
    const refusal = accessRefusal(refusing('WRITE_METHOD_UNVERIFIED', '人话'), 'agent.create');
    const notice = unavailableNotice('创建 Agent', refusal ?? undefined);
    expect(notice).toEqual({
      tone: 'warning',
      text: '创建 Agent暂不可用。',
      source: 'agent.create · WRITE_METHOD_UNVERIFIED',
      autoDismiss: true,
    });
    const caught = (() => {
      try {
        assertMethodAvailable(refusing('METHOD_NOT_FOUND'), 'org.create');
      } catch (error) {
        return error;
      }
      return null;
    })();
    expect(unavailableNotice('新建组织', methodRefusal(caught) ?? undefined).source).toBe('org.create · METHOD_NOT_FOUND');
  });
});
