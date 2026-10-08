// Product entry: `npm start`. Configuration is explicit environment only;
// missing pieces fail here with a named error before any socket is bound.
import { chmod, mkdir, rename, unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { createGuiServer } from './server-core.mjs';
import { resolveProductConfig } from './config.mjs';

// The detached caller (Python harness / desktop wrapper) names a file inside
// its own temporary home; readiness is a single atomic JSON document carrying
// the token-bearing URL, so the file is created with 0600 and the token is
// never printed anywhere else.
async function writeLaunchInfo(file, url) {
  const body = `${JSON.stringify({ schema: 'hyprial.gui-launch/v1', url })}\n`;
  await mkdir(dirname(file), { recursive: true, mode: 0o700 });
  const staging = join(dirname(file), `.${process.pid}.launch-info.tmp`);
  await writeFile(staging, body, { mode: 0o600 });
  await chmod(staging, 0o600);
  await rename(staging, file);
}

try {
  const config = resolveProductConfig(process.env);
  const server = await createGuiServer(config);
  const launchInfo = process.env.HYPRIAL_GUI_LAUNCH_INFO_FILE;
  // The launch info file already carries the full token-bearing URL to the
  // detached caller under 0600. Stdout must never duplicate the token: when
  // the caller reads readiness from the file we log the origin only; without
  // a file (plain development) the operator explicitly asked for stdout.
  if (launchInfo) await writeLaunchInfo(launchInfo, server.url);
  if (launchInfo) {
    console.log(`hyprial gui: listening on ${new URL(server.url).origin} (token URL in ${launchInfo})`);
  } else {
    console.log(`hyprial gui: ${server.url}`);
  }
  const shutdown = async () => {
    await server.close().catch(error => console.error(`hyprial gui close failed: ${error.message}`));
    if (launchInfo) await unlink(launchInfo).catch(() => {});
    process.exit(0);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
} catch (error) {
  console.error(`hyprial gui failed: ${error.code ?? 'ERROR'}: ${error.message}`);
  process.exit(1);
}
