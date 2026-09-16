// Browser smoke for the real task-discussion React component; no DSH server or H2B traffic.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const dependencies = resolve(process.env.H2B_BROWSER_DEPS || new URL('../dashboard/', import.meta.url).pathname);
const require = createRequire(resolve(dependencies, 'package.json'));
const { build } = await import(pathToFileURL(require.resolve('vite')).href);
const { chromium } = require('playwright');
const source = await readFile(new URL('../client/workflow-workbench.inc.js', import.meta.url), 'utf8');
const entry = `
import React from 'react';
import {createRoot} from 'react-dom/client';
const actor='agent:test:local:executor';
const messages=[
 {id:'ordinary',messageId:'ordinary',direction:'inbound',sender:actor,message:'普通消息不得出现在任务中',time:1},
 {id:'a',messageId:'a',conversationId:'wfd-run-a',direction:'inbound',sender:actor,message:'任务 A 沟通回复',time:2},
 {id:'b',messageId:'b',conversationId:'wfd-run-b',direction:'inbound',sender:actor,message:'任务 B 沟通回复',time:3}
];
const carrier={chatMessages:messages,error:''};
const subscribers=new Set();
const state=window.fixture={sends:[],opens:[],networkRequests:[],created:new Set()};
const host={call:async(method,input)=>{
 state.networkRequests.push(input);
 if(method!=='h2b-workflow-workbench'||input.operation!=='discussion')throw Error('Unexpected operation');
 return {workflowId:input.id,runId:input.runId,target:input.target,revision:5,name:'测试流程',actor,conversationId:'wfd-'+input.runId,executionConversationId:'wf-'+input.runId,sessionId:'work-session'};
}};
const slots={inject:(_name,fn)=>fn(),register:()=>()=>{}};
const Component=new Function('React','host','window','slots','demoCreateContactSession','demoSubscribe','demoEntry','demoAction','demoNotify',${JSON.stringify(source + '\nreturn WorkflowTaskDiscussion;')})(
 React,host,window,slots,
 async(target,label,open)=>{state.created.add(target);state.opens.push(open);return 'unique-direct-carrier';},
 (id,fn)=>{subscribers.add(fn);return ()=>subscribers.delete(fn);},
 ()=>carrier,
 async(operation,id,fields)=>{if(operation!=='send')throw Error('Unexpected action');state.sends.push({id,...fields});if(state.deferSend)await new Promise(resolve=>{state.releaseSend=resolve;});const messageId='sent-'+state.sends.length;carrier.chatMessages.push({id:'out:'+messageId,messageId,direction:'outbound',sender:'me',message:fields.message,conversationId:fields.conversationId,time:Date.now()});for(const fn of subscribers)fn();return true;},
 ()=>{for(const fn of subscribers)fn();}
);
const root=createRoot(document.getElementById('root'));
function Panel({runId}) {
 const [focusRequest,setDiscussionFocus]=React.useState(0);
 const begin=new Function('changeNodeTab','setDiscussionFocus',${JSON.stringify(source.slice(source.indexOf('      function directNodeChat('), source.indexOf('      function button(')) + '\nreturn directNodeChat;')})(()=>{},setDiscussionFocus);
 return React.createElement(React.Fragment,null,
  React.createElement(Component,{key:runId,workflowId:'wf-test',runId,target:actor,task:'仅测试引用，禁止运行真实任务',focusRequest}),
  React.createElement('button',{onClick:begin},'开始沟通'));
}
window.showTask=(runId)=>root.render(React.createElement(Panel,{key:runId,runId}));
window.showTask('run-a');
`;
const generated = await build({
  configFile: false, root: dependencies, logLevel: 'error', define: { 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'task-discussion-fixture', resolveId(id) {
    if (id === 'virtual:task-discussion' || id.endsWith('/virtual:task-discussion')) return '\0task-discussion';
    if (['react', 'react-dom/client'].includes(id)) return require.resolve(id);
  }, load(id) { if (id === '\0task-discussion') return entry; } }],
  build: { write: false, minify: false, lib: { entry: 'virtual:task-discussion', name: 'TaskFixture', formats: ['iife'] } }
});
const outputs = (Array.isArray(generated) ? generated : [generated]).flatMap(item => item.output);
const code = outputs.filter(item => item.type === 'chunk').map(item => item.code).join('\n');
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1000, height: 850 } });
  const errors = [];
  page.on('pageerror', error => { errors.push(error.message); console.error('Browser error:', error.message); });
  page.on('console', message => { if (message.type() === 'error') console.error('Browser console:', message.text()); });
  await page.route('http://workflow-task.test/**', route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="zh"><meta charset="utf-8"><title>Workflow task discussion smoke</title><div id="root"></div></html>' }));
  await page.goto('http://workflow-task.test/');
  await page.addScriptTag({ content: code });
  await page.getByText('任务 A 沟通回复', { exact: true }).waitFor();
  assert.equal(await page.getByText('任务 B 沟通回复', { exact: true }).count(), 0);
  assert.equal(await page.getByText('普通消息不得出现在任务中', { exact: true }).count(), 0);
  assert.equal(await page.evaluate(() => fixture.sends.length), 0, 'opening must not send');
  for (let attempt = 0; attempt < 2; attempt++) {
    await page.getByRole('button', { name: '开始沟通', exact: true }).click();
    await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '任务沟通消息');
    assert.equal(await page.evaluate(() => fixture.sends.length), 0, 'focus button must never send');
  }
  await page.getByRole('textbox', { name: '任务沟通消息' }).fill('A 的未发送草稿');
  await page.evaluate(() => showTask('run-b'));
  await page.getByText('任务 B 沟通回复', { exact: true }).waitFor();
  assert.equal(await page.getByRole('textbox', { name: '任务沟通消息' }).inputValue(), '');
  await page.getByRole('textbox', { name: '任务沟通消息' }).fill('请解释 B 的结果');
  await page.getByRole('button', { name: '发送任务消息', exact: true }).click();
  await page.waitForFunction(() => fixture.sends.length === 1);
  const sent = await page.evaluate(() => fixture.sends[0]);
  assert.equal(sent.id, 'unique-direct-carrier');
  assert.equal(sent.conversationId, 'wfd-run-b');
  assert.notEqual(sent.conversationId, 'wf-run-b');
  await page.evaluate(() => showTask('run-a'));
  await page.getByText('任务 A 沟通回复', { exact: true }).waitFor();
  assert.equal(await page.getByRole('textbox', { name: '任务沟通消息' }).inputValue(), 'A 的未发送草稿');
  await page.evaluate(() => { fixture.deferSend = true; });
  await page.getByRole('button', { name: '发送任务消息', exact: true }).click();
  await page.waitForFunction(() => fixture.sends.length === 2 && typeof fixture.releaseSend === 'function');
  await page.evaluate(() => showTask('run-b'));
  await page.getByText('任务 B 沟通回复', { exact: true }).waitFor();
  await page.evaluate(() => showTask('run-a'));
  await page.getByText('任务 A 沟通回复', { exact: true }).waitFor();
  assert.equal(await page.getByRole('textbox', { name: '任务沟通消息' }).isDisabled(), true, 'pending task send survives real unmount/remount');
  await page.evaluate(() => { fixture.deferSend = false; fixture.releaseSend(); });
  await page.waitForFunction(() => !document.querySelector('[aria-label="任务沟通消息"]').disabled);
  assert.equal(await page.getByRole('textbox', { name: '任务沟通消息' }).inputValue(), 'A 的未发送草稿', 'unmounted sender cannot clear remounted draft');
  assert.equal(await page.evaluate(() => fixture.sends.length), 2, 'remount never resends');
  assert.equal(await page.evaluate(() => fixture.created.size), 1);
  assert.ok(await page.evaluate(() => fixture.opens.every(value => value === false)));
  assert.deepEqual(errors, []);
  if (process.env.H2B_BROWSER_SCREENSHOT) await page.screenshot({ path: process.env.H2B_BROWSER_SCREENSHOT, fullPage: true });
  console.log('PASS real Chromium + React: task isolation, per-task drafts, one carrier, explicit send, independent conversation ID, no page errors. H2B transport mocked; no actual messages sent.');
} finally { await browser.close(); }
