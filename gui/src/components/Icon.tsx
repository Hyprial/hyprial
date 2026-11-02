import type { ReactNode } from 'react';

export type AccountIconName = 'settings' | 'connection' | 'help' | 'logout';

const PATHS: Record<AccountIconName, ReactNode> = {
  settings: (
    <>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 2.4v1.2M8 12.4v1.2M2.4 8h1.2M12.4 8h1.2" />
      <path d="m4 4 1 1M11 11l1 1M12 4l-1 1M5 11l-1 1" />
    </>
  ),
  connection: (
    <>
      <path d="M6.4 9.6a3 3 0 0 1 0-4.2l1.2-1.2a3 3 0 0 1 4.2 4.2l-1 1" />
      <path d="M9.6 6.4a3 3 0 0 1 0 4.2l-1.2 1.2a3 3 0 0 1-4.2-4.2l1-1" />
    </>
  ),
  help: (
    <>
      <circle cx="8" cy="8" r="5.4" />
      <path d="M6.5 6.4a1.6 1.6 0 1 1 2.4 1.4c-.6.3-.9.7-.9 1.3" />
      <path d="M8 11.2h.01" />
    </>
  ),
  logout: (
    <>
      <path d="M6.2 3H3.8A.8.8 0 0 0 3 3.8v8.4c0 .4.4.8.8.8h2.4" />
      <path d="M9.2 5.4 12 8l-2.8 2.6" />
      <path d="M12 8H6.6" />
    </>
  ),
};

export function AccountIcon({ name }: { name: AccountIconName }) {
  return (
    <svg
      className="menu-icon"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
