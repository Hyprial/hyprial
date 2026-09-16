// Input interpretation belongs to the Agent; this metadata describes the GUI target.
// References remain in the native conversation, never in the runtime document.
export const GUI_REFERENCE_AUTHORING = Object.freeze({
  version: 1,
  inputs: {
    text: 'Use the user request to define scope, business intent and changes.',
    wireframe: 'Use sketches and prototypes for hierarchy, region order, navigation and approximate proportions.',
    styleReference: 'Use style images for palette, typography hierarchy, spacing, borders and visual emphasis.',
    combined: 'Use the prototype for structure and the style reference for appearance. Follow explicit user instructions when references disagree; clarify consequential ambiguity.'
  },
  imageHandling: 'Read images already supplied through the native DSH conversation with an available vision capability. Do not claim to have inspected an inaccessible image; ask for a readable reference or text description. Do not put attachment bytes, URLs or image references into GUI documents. No separate image-upload or image-generation tool is required by this workflow.',
  scope: 'Use kind=shell for the whole workspace; use kind=page only for an explicitly requested independent personal page. Preserve reachable business and system entrances. The current Agent can create its own draft; editing another session draft requires returning to its bound design session, not inventing a sessionId.',
  workflow: [
    'Call h2b_gui_context without id to discover your drafts, catalogV2, contractV2 and exampleV2; read the selected draft with id before editing. Do not assume access to the active profile or other sessions.',
    'Identify which images specify structure or style. Explain the inferred regions and map business regions to exact catalog feature/view pairs. Clarify ambiguity that changes business behavior; choose reasonable defaults for minor styling.',
    'Produce a schemaVersion=2 document. Map regions to Stack/Grid/Split/Tabs/Text/Feature. Use contractV2.style tokens and bounds. Keep complete native business modules; do not recreate their internal controls.',
    'For an existing V1 draft, call h2b_gui_migrate with its authoritative baseRevision first. For layout/style changes preserve document, node and instance IDs and context; change instanceId when feature, view or context changes.',
    'Call h2b_gui_validate with {document} to check the candidate without saving. Repair reported errors; after at most two repair attempts, explain remaining limits and retain the existing draft. Candidate validation is not a revision or instance-history check.',
    'Call h2b_gui_create for a new draft or h2b_gui_update with the originally read baseRevision for an existing draft. If the revision conflicts, reread and reconcile the changes; never attach the newer revision to stale content merely to force a save.',
    'Validate the saved {id,revision}, then call h2b_gui_preview with that exact pair. Report what matches, approximations, unsupported elements and checks actually performed. Publication and application remain user actions in Studio.'
  ],
  fidelity: {
    structure: 'Preserve grouping, order and emphasis; normalize rough sketch geometry into supported layout ratios. Absolute x/y placement, overlays and arbitrary breakpoints are not supported.',
    appearance: 'Map to supported semantic styles and readable light/dark palettes. Do not promise pixel-perfect reproduction or arbitrary fonts, images, CSS and custom controls.',
    behavior: 'A drawn button, chart, filter or metric is not a new capability. Use only catalog views and supported context. Do not fabricate data, action handlers or filters. Disclose missing capabilities; a Text note may describe a gap but must not masquerade as a functioning control.',
    responsive: 'Existing narrow-screen Grid/Split stacking is automatic, not a user-authored breakpoint design. Review desktop and narrow layouts when a browser is available; state when they were not inspected.'
  },
  review: ['interpreted structure', 'reference style choices', 'feature/view mappings', 'approximations and unsupported elements', 'saved draft ID and revision', 'validation and visual checks actually performed'],
  preview: 'The preview tool returns a validated document for Studio review, not a screenshot or proof of visual fidelity. Interactive Studio trials can perform real business operations; do not describe them as a side-effect-free sandbox.',
  recovery: {
    GUI_REVISION_CONFLICT: 'Reread the draft and reconcile concurrent changes before retrying.',
    GUI_SESSION_MISMATCH: 'Use the bound design session; do not bypass ownership or silently copy an inaccessible draft.',
    GUI_INVALID_ARGUMENT: 'Read the error and current catalog/contract, then correct only supported fields. Do not weaken validation or edit implementation source to bypass the constraint.'
  }
});
