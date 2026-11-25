/** Saved-entry references and supported environment projections; no credential values. */
export const SECRET_PROVIDER_ENVIRONMENTS: Readonly<Record<string, string>> = {
  deepseek: 'DEEPSEEK_API_KEY', openai: 'OPENAI_API_KEY', anthropic: 'ANTHROPIC_API_KEY',
  'kimi-coding': 'KIMI_API_KEY', moonshotai: 'MOONSHOT_API_KEY', google: 'GEMINI_API_KEY',
};

export function secretEntryReference(entryId: string, fieldName: string): string {
  return `${entryId}:${fieldName}`;
}

export function parseSecretEntryReference(reference: string): { entryId: string; fieldName: string } | null {
  const parts = reference.split(':');
  if (parts.length !== 2 || parts.some(part => !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(part))) return null;
  return { entryId: parts[0], fieldName: parts[1] };
}

export function secretProviderForEnvironments(names: readonly string[]): string {
  const matches = Object.entries(SECRET_PROVIDER_ENVIRONMENTS).filter(([, name]) => names.includes(name));
  return matches.length === 1 ? matches[0][0] : '';
}
