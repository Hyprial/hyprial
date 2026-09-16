#!/usr/bin/env node
// Exercise the shipped plugin against the installed runtime, without credentials
// or generation. Called only with the release gate's isolated web profile.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve, join } from 'node:path';
const profile = resolve(process.argv[2]);
const require = createRequire(join(profile, 'node_modules/dsh-codex/package.json'));
const moduleFor = name => import(pathToFileURL(require.resolve(name)).href);
const { Context } = await moduleFor('@deepseek-ai/cordis');
const { default: Llm } = await moduleFor('@deepseek-ai/dsh-llm');
const { default: Web } = await moduleFor('@deepseek-ai/dsh-web');
const Codex = await moduleFor('dsh-codex');
const ctx = new Context();
try {
  await ctx.plugin(Llm);
  await ctx.plugin(Web);
  await ctx.plugin(Codex, { contextWindow: 512000 });
  const models = await ctx.llm.listModels('openai-codex');
  assert.ok(models.some(model => model.id === 'gpt-6-astra'));
  for (const model of models) {
    const info = await ctx.llm.resolveModelInfo('openai-codex', model.id);
    assert.ok(info.context.contextWindow > 0, `${model.id}: missing context capacity`);
  }
  await assert.rejects(ctx.llm.resolveModelInfo('openai-codex', 'h2b-nonexistent-model'),
    error => error.code === 'UNKNOWN_MODEL');
  console.log(`Codex catalog and resolution passed for ${models.length} models; no generation requests`);
} finally {
  await ctx.fiber.dispose();
}
