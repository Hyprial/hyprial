/*
 * Dev-only global declarations (WP2, gui-dev-product-parity-2026-11-09):
 * - __HYPRIAL_DEV_PREVIEW__ is declared here inside the dev-only src/dev tree
 *   so the WP5 static gate keeps product sources free of __HYPRIAL_DEV_*
 *   identifiers, while the composition root (src/main.tsx, registered
 *   composition-root exception) keeps consuming the token.
 * - The define itself stays in gui/vite.config.ts; dev preview logic is
 *   unchanged by the parity build switch.
 */
export {};

declare global {
  const __HYPRIAL_DEV_PREVIEW__: boolean;
}
