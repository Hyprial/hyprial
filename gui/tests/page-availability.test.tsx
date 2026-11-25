/*
 * Decision record (PR 1f/F0b, 2026-11-14): contract of the shared
 * page-availability fact. A control that navigates to another destination
 * reads it; the shell provides the hidden set. Outside a provider every
 * page is available.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PageAvailabilityProvider, usePageAvailable } from '../src/app/page-availability';
import type { GuiPage } from '../src/state/gui-view-model';

function Probe({ page }: { readonly page: GuiPage }) {
  return <span data-testid="probe">{usePageAvailable(page) ? 'available' : 'hidden'}</span>;
}

describe('page availability', () => {
  it('treats every page as available without a provider', () => {
    render(<Probe page="contacts" />);
    expect(screen.getByTestId('probe')).toHaveTextContent('available');
  });

  it('reports a hidden page as unavailable and leaves the others available', () => {
    const hidden = new Set<GuiPage>(['contacts']);
    const { rerender } = render(
      <PageAvailabilityProvider hiddenPages={hidden}><Probe page="contacts" /></PageAvailabilityProvider>,
    );
    expect(screen.getByTestId('probe')).toHaveTextContent('hidden');
    rerender(
      <PageAvailabilityProvider hiddenPages={hidden}><Probe page="org" /></PageAvailabilityProvider>,
    );
    expect(screen.getByTestId('probe')).toHaveTextContent('available');
  });
});
