#!/usr/bin/env node
// URLs arrive over stdin, never as process arguments or shell command text.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const home=process.env.HYPRIAL_HOME || process.env.H2B_HOME || join(process.env.HOME,'.h2b');
let text=''; for await(const chunk of process.stdin) {text+=chunk;if(text.length>16384)throw new Error('GUI opener input too large');}
const {urls}=JSON.parse(text);
if(!Array.isArray(urls)||urls.length<1||urls.length>2)throw new Error('One or two GUI URLs required');
const validated=urls.map(value=>{const u=new URL(value);if(u.protocol!=='http:'||!['localhost','127.0.0.1','[::1]'].includes(u.hostname)||u.username||u.password)throw new Error('GUI opener requires a local HTTP URL');return u.href;});
const require=createRequire(join(home,'apps/gui/runtime/dsh/package.json'));
const {default:open}=await import(pathToFileURL(require.resolve('open')).href);
for(const url of validated) await open(url);
console.log('已在浏览器打开 GUI。');
