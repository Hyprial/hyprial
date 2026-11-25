/*
 * Decision record (PR 1f/F4; S2d 2026-11-22): a refusal before dispatch
 * reaches its handler in a classifiable form and never as a sentence.
 * Adapters carry `{ refusal }` on the result object the consumer expects (model
 * key save); the consumer notifies once when the user operates the control and
 * leaves the form editable.
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { createConnectedModelKeyActions, createModelKeySaveAdapter } from '../../../src/features/model';
import { ModelStep } from '../../../src/features/onboarding/impl/ModelStep';
import type { ModelKeySaveResult } from '../../../src/features/onboarding';
import { expectUnavailableToast, renderWithNotifications } from '../../support/notify-harness';

const refuse = (code: string) => () => ({ available: false, code, reason: 'catalog sentence' }) as const;

describe('refusals reach the handler in a classifiable form', () => {
  it('the model key save carries the refusal on its result and never a message', async () => {
    const providerWrite = vi.fn();
    const adapter = createModelKeySaveAdapter({ agent: { secret: { providerWrite } } } as never,
      { entryId: 'e', fieldName: 'f' }, refuse('WRITE_METHOD_UNVERIFIED'));

    const result = await adapter.save('synthetic-key');

    expect(result).toEqual({ status: 'unavailable', refusal: { code: 'WRITE_METHOD_UNVERIFIED', source: 'agent.secret.provider-write' } });
    expect(providerWrite).not.toHaveBeenCalled();
  });

  it('the grant inventory refusal is an unavailable result, a dispatched failure is a failed one', async () => {
    const refused = createConnectedModelKeyActions({ agent: { secret: { list: vi.fn() } } } as never, refuse('METHOD_NOT_FOUND'));
    await expect(refused.saveModelKey!('synthetic-key')).resolves.toEqual({
      status: 'unavailable', refusal: { code: 'METHOD_NOT_FOUND', source: 'agent.secret.list' },
    });
    const lost = createConnectedModelKeyActions({ agent: { secret: { list: vi.fn().mockRejectedValue(new Error('lost')) } } } as never);
    await expect(lost.saveModelKey!('synthetic-key')).resolves.toMatchObject({ status: 'failed' });
  });
});

describe('first-run model step with a refused save', () => {
  function Harness({ onSave }: { onSave: (key: string) => Promise<ModelKeySaveResult> }) {
    const [draft, setDraft] = useState('');
    return <ModelStep state="active" saved={false} draft={draft} onDraftChange={setDraft}
      onBack={() => {}} onSave={onSave} onSavedChange={() => {}} onContinue={() => {}} />;
  }

  it('notifies once, keeps the typed key and the form editable, and renders no inline result', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (): Promise<ModelKeySaveResult> => ({
      status: 'unavailable', refusal: { code: 'METHOD_NOT_FOUND', source: 'agent.secret.provider-write' },
    }));
    renderWithNotifications(<Harness onSave={onSave} />);
    const field = screen.getByLabelText(/DeepSeek API Key/);
    await user.type(field, 'synthetic-key');

    await user.click(screen.getByRole('button', { name: '保存并继续' }));

    await waitFor(() => expectUnavailableToast('保存并继续暂不可用。'));
    expect(field).toHaveValue('synthetic-key');
    expect(screen.getByRole('button', { name: '保存并继续' })).toBeEnabled();
    expect(document.querySelector('.ui-notice')).toBeNull();
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});
