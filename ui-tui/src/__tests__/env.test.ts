import { describe, expect, it } from 'vitest'

import { resolveBootTerminalModes } from '../config/env.js'

describe('resolveBootTerminalModes', () => {
  it('uses native VS Code scrollback and selection without terminal mouse capture', () => {
    expect(
      resolveBootTerminalModes({
        TERM_PROGRAM: 'vscode'
      })
    ).toEqual({ inline: true, mouseTracking: 'off' })
  })

  it('detects VS Code when tmux masks TERM_PROGRAM', () => {
    expect(
      resolveBootTerminalModes({
        TERM_PROGRAM: 'tmux',
        VSCODE_GIT_ASKPASS_MAIN: '/vscode/extensions/git/dist/askpass-main.js'
      })
    ).toEqual({ inline: true, mouseTracking: 'off' })
  })

  it('keeps explicit TUI overrides authoritative in VS Code', () => {
    expect(
      resolveBootTerminalModes({
        HERMES_TUI_INLINE: '0',
        HERMES_TUI_MOUSE_TRACKING: '1',
        TERM_PROGRAM: 'vscode'
      })
    ).toEqual({ inline: false, mouseTracking: 'all' })
  })

  it('preserves the traditional full-screen defaults elsewhere', () => {
    expect(resolveBootTerminalModes({ TERM_PROGRAM: 'xterm' })).toEqual({
      inline: false,
      mouseTracking: 'all'
    })
  })

  it('keeps the existing Termux primary-buffer behavior', () => {
    expect(resolveBootTerminalModes({ TERMUX_VERSION: '0.118' })).toEqual({
      inline: true,
      mouseTracking: 'off'
    })
  })
})
