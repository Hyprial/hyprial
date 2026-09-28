// DSH selects PowerShell on Windows and Bash on Unix. Only package-owned
// constant commands may enter this adapter; user values remain in env/stdin.
export function shellEnvironmentCommand(command, platform = process.platform) {
  return platform === 'win32'
    ? command.replace(/\$([A-Z][A-Z0-9_]*)/g, (_, name) => `$env:${name}`)
    : command;
}
