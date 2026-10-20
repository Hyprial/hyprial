"""Launch-scoped Cordis plugin that injects Harness wakes into dsh-tui.

A ``wake`` frame is answered with the outcome of the turn it queued, observed
on the agent's own events for that exact message inside a bounded window
(``outcomeTimeoutMs`` from the frame, clamped): the model's first streamed
content chunk is ``accepted`` (the stream's ``start`` frame and a terminal
``finish`` chunk carrying an adapter failure are both published without the
model answering, so neither proves anything); a claim or no event yet is ``claimed``/``queued`` (a running
turn may hold it); a turn error after the claim, a discard, or a throwing
``followup()`` is ``ok: false`` with a code. Answering ``ok`` as soon as
``followup()`` returned made a rejected turn indistinguishable from a
delivered wake.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_FILENAME = "hyprial-bridge-plugin.mjs"

PLUGIN_SOURCE = r'''import { randomUUID } from 'node:crypto'
import { chmodSync, unlinkSync } from 'node:fs'
import { createServer } from 'node:net'

export const name = 'hyprial-bridge'
export const inject = ['agents']

const DEFAULT_OUTCOME_TIMEOUT_MS = 8000
const CONTENT_CHUNKS = new Set(['block-start', 'text-delta', 'reasoning-delta', 'tool-call-delta'])
const MAX_OUTCOME_TIMEOUT_MS = 30000

function reply(socket, value) {
  if (socket.destroyed || !socket.writable) return
  socket.write(JSON.stringify(value) + '\n')
}

function deepFreeze(value) {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child)
    Object.freeze(value)
  }
  return value
}

function userMessage(prompt) {
  return deepFreeze(structuredClone({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text: prompt }],
    source: {
      kind: 'hyprial-bridge',
      plugin: 'hyprial-bridge',
      form: 'notice',
      summary: 'Harness Network message',
    },
  }))
}

function outcomeTimeout(value) {
  if (!Number.isInteger(value) || value < 0) return DEFAULT_OUTCOME_TIMEOUT_MS
  return Math.min(value, MAX_OUTCOME_TIMEOUT_MS)
}

function errorText(error) {
  const text = String(error?.message ?? error)
  return text.length > 300 ? text.slice(0, 299) + '…' : text
}

function wakeOutcome(ctx, agent, prompt, timeoutMs) {
  const message = userMessage(prompt)
  const sessionId = String(agent.session.id)
  return new Promise((resolve) => {
    let claimedTurn = null
    let settled = false
    const disposers = []
    const finish = (value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      for (const dispose of disposers.splice(0)) {
        try { dispose() } catch {}
      }
      resolve(value)
    }
    const listen = (event, listener) => {
      const dispose = ctx.on(event, listener)
      if (typeof dispose === 'function') disposers.push(dispose)
    }
    const ours = (payload) => payload?.agent === agent
    listen('agent/inbox/claimed', (payload) => {
      if (ours(payload) && payload.message?.id === message.id) claimedTurn = payload.turn
    })
    listen('agent/inbox/discarded', (payload) => {
      if (ours(payload) && payload.message?.id === message.id) {
        finish({ ok: false, code: 'WAKE_DISCARDED', sessionId })
      }
    })
    listen('agent/error', (payload) => {
      if (ours(payload) && claimedTurn !== null && payload.turn === claimedTurn) {
        finish({ ok: false, code: 'TURN_ERROR', detail: errorText(payload.error), sessionId })
      }
    })
    listen('agent/assistant-stream', (payload) => {
      if (
        ours(payload) && claimedTurn !== null && payload.frame?.type === 'chunk'
        && CONTENT_CHUNKS.has(payload.frame.chunk?.type)
      ) {
        finish({ ok: true, state: 'accepted', sessionId })
      }
    })
    const timer = setTimeout(() => {
      finish({ ok: true, state: claimedTurn === null ? 'queued' : 'claimed', sessionId })
    }, timeoutMs)
    try {
      agent.followup(message)
    } catch (error) {
      finish({ ok: false, code: 'FOLLOWUP_REJECTED', detail: errorText(error), sessionId })
    }
  })
}

export function apply(ctx, config) {
  const socketPath = config?.socketPath
  const pinnedSessionId = config?.sessionId
  if (typeof socketPath !== 'string' || socketPath === '') {
    throw new Error('hyprial bridge requires config.socketPath')
  }
  try { unlinkSync(socketPath) } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }
  const selectAgent = () => {
    const roots = ctx.agents.roots()
    if (typeof pinnedSessionId === 'string' && pinnedSessionId !== '') {
      return roots.find((agent) => String(agent.session.id) === pinnedSessionId)
    }
    return roots.length === 1 ? roots[0] : undefined
  }
  const server = createServer((socket) => {
    socket.on('error', () => {})
    socket.setEncoding('utf8')
    let buffer = ''
    socket.on('data', (chunk) => {
      buffer += chunk
      let index = buffer.indexOf('\n')
      while (index >= 0) {
        const line = buffer.slice(0, index)
        buffer = buffer.slice(index + 1)
        try {
          const frame = JSON.parse(line)
          if (frame?.type === 'ping') {
            const agent = selectAgent()
            reply(socket, {
              ok: agent !== undefined,
              ready: agent !== undefined,
              sessionId: agent === undefined ? null : String(agent.session.id),
              roots: ctx.agents.roots().length,
            })
          } else if (frame?.type === 'wake') {
            const prompt = frame?.prompt
            if (typeof prompt !== 'string' || prompt === '') {
              reply(socket, { ok: false, code: 'INVALID_WAKE' })
            } else {
              const agent = selectAgent()
              if (agent === undefined) {
                reply(socket, { ok: false, code: 'NO_AGENT' })
              } else {
                wakeOutcome(ctx, agent, prompt, outcomeTimeout(frame?.outcomeTimeoutMs))
                  .then(
                    (outcome) => reply(socket, outcome),
                    (error) => reply(socket, { ok: false, code: 'WAKE_FAILED', detail: errorText(error) }),
                  )
              }
            }
          } else {
            reply(socket, { ok: false, code: 'UNKNOWN_FRAME' })
          }
        } catch (error) {
          reply(socket, { ok: false, code: 'BAD_FRAME', detail: String(error) })
        }
        index = buffer.indexOf('\n')
      }
    })
  })
  server.on('error', (error) => {
    ctx.logger?.warn?.(`hyprial bridge socket failed: ${String(error)}`)
  })
  ctx.effect(() => {
    server.listen(socketPath, () => {
      try { chmodSync(socketPath, 0o600) } catch {}
    })
    return () => {
      server.close()
      try { unlinkSync(socketPath) } catch {}
    }
  }, 'hyprial-bridge.socket')
}
'''


def write_dsh_tui_plugin(directory: Path) -> Path:
    """Write the launch-scoped JS plugin with restrictive permissions."""

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / PLUGIN_FILENAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(PLUGIN_SOURCE)
    return path
