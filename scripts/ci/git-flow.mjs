// Repository Git flow policy; CI consumes this file from the checked-out tree.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export const gitFlowPath = '.forgejo/git-flow.json';

export function readGitFlow(root) {
  let policy;
  try {
    policy = JSON.parse(readFileSync(join(root, gitFlowPath), 'utf8'));
  } catch {
    throw new Error(`CI_ROUTE: invalid Git flow config (${gitFlowPath})`);
  }
  const valid = policy?.schemaVersion === 1 && policy.mode === 'dev-first'
    && policy.integration?.branch === 'dev'
    && policy.integration.pullRequestSources === 'any'
    && policy.integration.suiteSelection === 'changed-paths'
    && policy.integration.wipPullRequests === 'draft'
    && policy.release?.preparationBranches === 'release/*'
    && policy.release.preparationTarget === policy.integration.branch
    && policy.release.preparationPurpose === 'preparation-only'
    && policy.release.candidateSource === 'dev-history'
    && policy.release.candidateIdentity === 'full-commit-sha'
    && policy.release.immutableVersionTag === 'v<VERSION>'
    && policy.release.entryWorkflow === 'release-tag.yml'
    && policy.tracks?.internal === 'validated-dev-push'
    && policy.tracks.nightly === 'same-sha-isolated-and-credentialed-e2e'
    && policy.tracks.stable === 'successful-formal-release'
    && policy.main?.role === 'historical' && policy.main.publishesOnPush === false
    && policy.publicationRules === 'docs/guides/release-rules.md';
  if (!valid) throw new Error(`CI_ROUTE: invalid Git flow config (${gitFlowPath})`);
  return policy;
}
