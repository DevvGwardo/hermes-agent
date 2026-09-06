import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { useGateway } from '../app/gatewayContext.js'
import { patchOverlayState } from '../app/overlayStore.js'
import { $uiState } from '../app/uiStore.js'
import type { AgentListResponse, AgentSessionItem } from '../gatewayTypes.js'
import { asRpcResult } from '../lib/rpc.js'
import type { Theme } from '../theme.js'

const VISIBLE = 14

const STATUS_STYLE: Record<AgentSessionItem['status'], { glyph: string; color: keyof Theme['color'] }> = {
  running: { glyph: '\u25cf', color: 'accent' },
  waiting: { glyph: '\u25cf', color: 'warn' },
  queued: { glyph: '\u25cb', color: 'muted' },
  idle: { glyph: '\u25cb', color: 'muted' },
  done: { glyph: '\u2713', color: 'statusGood' },
  error: { glyph: '\u2717', color: 'error' },
  starting: { glyph: '\u21bb', color: 'primary' },
}

const fmtTime = (seconds: number): string => {
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

const sourceIcon = (source: string): string => {
  switch (source) {
    case 'telegram': return '\ud83d\udce8'
    case 'discord': return '\ud83d\udcac'
    case 'slack': return '\ud83c\udfe2'
    case 'cli':
    case 'tui': return '\ud83d\udcbb'
    case 'api':
    case 'webhook': return '\u26a1'
    default: return '\ud83c\udf10'
  }
}

export function AgentListView({ onClose }: { gw?: unknown; onClose: () => void; t: Theme }) {
  const { gw } = useGateway()
  const ui = useStore($uiState)
  const [items, setItems] = useState<AgentSessionItem[]>([])
  const [cursor, setCursor] = useState(0)
  const [loading, setLoading] = useState(true)
  const { stdout } = useStdout()

  const fetchList = useMemo(() => () => {
    gw.request<AgentListResponse>('agent.list', {
      current_session_id: ui.sid || '',
    }).then(r => {
      const res = asRpcResult<AgentListResponse>(r)
      if (res?.sessions) {
        setItems(res.sessions)
        setLoading(false)
      }
    }).catch(() => {
      setLoading(false)
    })
  }, [gw, ui.sid])

  useEffect(() => {
    fetchList()
    const id = setInterval(fetchList, 2000)
    return () => clearInterval(id)
  }, [fetchList])

  useEffect(() => {
    if (cursor >= items.length) {
      setCursor(Math.max(0, items.length - 1))
    }
  }, [cursor, items.length])

  useInput((ch, key) => {
    if (ch === 'q' || key.escape) {
      patchOverlayState({ agentList: false })
      return
    }
    if (key.upArrow || ch === 'k') {
      setCursor(c => Math.max(0, c - 1))
      return
    }
    if (key.downArrow || ch === 'j') {
      setCursor(c => Math.min(Math.max(0, items.length - 1), c + 1))
      return
    }
    if (key.return || ch === 'l' || key.rightArrow) {
      const sel = items[cursor]
      if (sel) {
        if (!sel.is_current) {
          gw.request('agent.resume', { session_id: sel.session_id }).then(r => {
            if (asRpcResult(r)?.switched) {
              patchOverlayState({ agentList: false })
            }
          }).catch(() => {})
        } else {
          patchOverlayState({ agentList: false })
        }
      }
      return
    }
  })

  const width = Math.min(120, (stdout?.columns ?? 80) - 2)
  const offset = Math.max(0, cursor - Math.floor(VISIBLE / 2))

  const counts = useMemo(() => {
    const c = { running: 0, waiting: 0, idle: 0 }
    items.forEach(it => {
      if (it.status === 'running') c.running++
      else if (it.status === 'waiting') c.waiting++
      else c.idle++
    })
    return c
  }, [items])

  if (loading && items.length === 0) {
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={ui.theme.color.primary}>agent view</Text>
        <Text color={ui.theme.color.muted}>loading sessions\u2026</Text>
      </Box>
    )
  }

  return (
    <Box flexDirection="column" width={width}>
      <Box>
        <Text bold color={ui.theme.color.primary}>agent view</Text>
        <Text color={ui.theme.color.muted}>
          {' '}\u00b7 {items.length} sessions
          {counts.running > 0 ? ` \u00b7 ${counts.running} running` : ''}
          {counts.waiting > 0 ? ` \u00b7 ${counts.waiting} waiting` : ''}
        </Text>
      </Box>

      {items.length === 0 ? (
        <Text color={ui.theme.color.muted}>no active sessions</Text>
      ) : (
        <Box flexDirection="column">
          {offset > 0 && <Text color={ui.theme.color.muted}>  \u2191 {offset} more</Text>}

          {items.slice(offset, offset + VISIBLE).map((item, i) => {
            const isActive = offset + i === cursor
            const style = STATUS_STYLE[item.status]
            const statusColor = ui.theme.color[style.color]
            const preview = item.preview || item.title || '(untitled)'
            const modelShort = item.model ? item.model.split('/').pop()! : 'inherit'
            const icon = sourceIcon(item.source)
            const badges = [icon, modelShort].filter(Boolean).join(' \u00b7 ')

            return (
              <Box key={item.session_key}>
                <Text bold={isActive} color={isActive ? ui.theme.color.accent : ui.theme.color.muted} inverse={isActive}>
                  {isActive ? '\u25b8 ' : '  '}
                </Text>

                <Text color={statusColor} bold={isActive}>
                  {style.glyph}{' '}
                </Text>

                <Box flexGrow={1}>
                  <Text bold={isActive} color={isActive ? ui.theme.color.accent : ui.theme.color.text} wrap="truncate-end" inverse={isActive}>
                    {preview || '(untitled)'}
                  </Text>
                </Box>

                <Text color={ui.theme.color.muted} marginLeft={1}>
                  {fmtTime(item.elapsed)}
                  {badges ? ` \u00b7 ${badges}` : ''}
                </Text>
              </Box>
            )
          })}

          {offset + VISIBLE < items.length && (
            <Text color={ui.theme.color.muted}>  \u2193 {items.length - offset - VISIBLE} more</Text>
          )}
        </Box>
      )}

      <Box marginTop={1}>
        <Text color={ui.theme.color.muted}>\u2191\u2193/jk navigate \u00b7 enter attach \u00b7 q close</Text>
      </Box>
    </Box>
  )
}
