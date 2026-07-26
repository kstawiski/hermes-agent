import type { MouseTrackingMode } from '@hermes/ink'

import { isTermuxTuiMode } from '../lib/termux.js'

const truthy = (v?: string) => /^(?:1|true|yes|on)$/i.test((v ?? '').trim())
const falsy = (v?: string) => /^(?:0|false|no|off)$/i.test((v ?? '').trim())

const parseToggle = (v?: string): boolean | null => {
  const raw = (v ?? '').trim()

  if (!raw) {
    return null
  }

  if (truthy(raw)) {
    return true
  }

  if (falsy(raw)) {
    return false
  }

  return null
}

export const TERMUX_TUI_MODE = isTermuxTuiMode()

export const STARTUP_RESUME_ID = (process.env.HERMES_TUI_RESUME ?? '').trim()
export const STARTUP_QUERY = (process.env.HERMES_TUI_QUERY ?? '').trim()
export const STARTUP_IMAGE = (process.env.HERMES_TUI_IMAGE ?? '').trim()

// Mouse and buffer behavior at startup. Config sync can refine mouse tracking
// after the gateway connects, but boot must already preserve native selection:
// a transient DEC mouse mode is enough for terminals to intercept a drag.
// VS Code normally identifies itself with TERM_PROGRAM=vscode. A tmux server
// retains the environment of the client that created it, so VS Code markers
// cannot identify the terminal attached later. Keep this guard and marker
// family aligned with detectVSCodeLikeTerminal() in
// lib/terminalSetup.ts. Importing that helper here would also import its
// filesystem-backed setup implementation during boot.
export const isVsCodeTerminal = (env: NodeJS.ProcessEnv = process.env): boolean =>
  !String(env.TMUX ?? '').trim() &&
  (String(env.TERM_PROGRAM ?? '').trim().toLowerCase() === 'vscode' ||
    Boolean(String(env.VSCODE_INJECTION ?? '').trim()) ||
    Boolean(String(env.VSCODE_IPC_HOOK_CLI ?? '').trim()) ||
    Boolean(String(env.VSCODE_GIT_ASKPASS_MAIN ?? '').trim()) ||
    Boolean(String(env.VSCODE_GIT_IPC_HANDLE ?? '').trim()) ||
    Boolean(String(env.CURSOR_TRACE_ID ?? '').trim()))

export const resolveBootTerminalModes = (
  env: NodeJS.ProcessEnv = process.env
): { inline: boolean; mouseTracking: MouseTrackingMode } => {
  const termux = isTermuxTuiMode(env)
  const vscode = isVsCodeTerminal(env)
  const mouseTrackingOverride = parseToggle(env.HERMES_TUI_MOUSE_TRACKING)
  const mouseTrackingDisabledLegacy = truthy(env.HERMES_TUI_DISABLE_MOUSE)
  const mouseEnabled = mouseTrackingOverride ?? (termux || vscode ? false : !mouseTrackingDisabledLegacy)
  const inlineOverride = parseToggle(env.HERMES_TUI_INLINE)

  return {
    // Primary-buffer rendering gives VS Code normal scrollback and text copy.
    // Explicit HERMES_TUI_INLINE always wins for users who prefer full-screen.
    inline: inlineOverride ?? (termux || vscode),
    mouseTracking: mouseEnabled ? 'all' : 'off'
  }
}

const bootTerminalModes = resolveBootTerminalModes()

export const MOUSE_TRACKING: MouseTrackingMode = bootTerminalModes.mouseTracking

export const NO_CONFIRM_DESTRUCTIVE = truthy(process.env.HERMES_TUI_NO_CONFIRM)

// Set by the dashboard PTY launcher. This is intentionally narrower than
// INLINE_MODE: users can opt into inline terminal rendering locally, but the
// browser-embedded TUI has no healthy restart path after an idle exit.
export const DASHBOARD_TUI_MODE = truthy(process.env.HERMES_TUI_DASHBOARD)

// HERMES_DEV_CREDITS — dev-only live-spend readout (Δ status segment + "(dev credits)"
// banner). Throwaway dev scaffolding; the whole readout gates on this one flag.
export const DEV_CREDITS_MODE = truthy(process.env.HERMES_DEV_CREDITS)

// Skip AlternateScreen — TUI renders into the primary buffer so the host
// terminal's native scrollback captures whatever scrolls off the top. Termux
// and VS Code default to this straightforward copy-friendly mode; an explicit
// HERMES_TUI_INLINE=0/1 override remains authoritative.
export const INLINE_MODE = bootTerminalModes.inline

// Live FPS counter overlay, fed by ink's onFrame (real render rate, not a
// synthetic timer).
export const SHOW_FPS = truthy(process.env.HERMES_TUI_FPS)
