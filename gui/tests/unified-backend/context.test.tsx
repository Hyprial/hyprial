import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { BackendProvider, createBackendConnection, useBackend } from '@/app/backend';

function Status() {
  const backend = useBackend();
  return <output>{backend.status === 'connected' ? backend.connection.descriptor.generation : backend.status}</output>;
}

describe('backend injection', () => {
  it('defaults to unavailable without choosing a mock or creating a connection', () => {
    render(<Status />);
    expect(screen.getByRole('status')).toHaveTextContent('unavailable');
  });

  it('exposes the supplied connection and supports an explicit transitioning state', () => {
    const invoke = vi.fn();
    const connection = createBackendConnection({ invoke }, {
      interfaceVersion: 1, driver: 'daemon', phase: 'ready', generation: 'g-1', catalogDigest: 'digest',
    });
    const view = render(<BackendProvider value={{ status: 'connected', connection }}><Status /></BackendProvider>);
    expect(screen.getByRole('status')).toHaveTextContent('g-1');
    connection.retire();
    view.rerender(<BackendProvider value={{ status: 'transitioning' }}><Status /></BackendProvider>);
    expect(screen.getByRole('status')).toHaveTextContent('transitioning');
    expect(invoke).not.toHaveBeenCalled();
  });
});
