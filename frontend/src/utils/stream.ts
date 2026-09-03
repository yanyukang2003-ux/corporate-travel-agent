/**
 * SSE（Server-Sent Events）解析：把流式响应的字节块切成一个个事件。
 *
 * 流式创建/跟进任务用它：后端边跑边推 `step` 事件（和 `GET /steps` 同一结构），
 * 最后推 `task` 或 `error`。解析是纯函数，方便单测；网络层在 client.ts。
 */

/** 一条 SSE 事件。 */
export interface SseEvent {
  event: string
  data: unknown
}

/**
 * 喂入新到的文本块，切出完整事件；不完整的尾巴原样返回，等下一块拼上。
 * 事件以空行分隔；`data:` 是 JSON（后端保证）。解析失败的块原样丢弃并计入 `dropped`。
 */
export function feedSse(buffer: string, chunk: string): {
  events: SseEvent[]
  rest: string
  dropped: number
} {
  const text = buffer + chunk
  const blocks = text.split('\n\n')
  const rest = blocks.pop() ?? ''
  const events: SseEvent[] = []
  let dropped = 0
  for (const block of blocks) {
    const lines = block.split('\n').filter(Boolean)
    const eventLine = lines.find((line) => line.startsWith('event: '))
    const dataLine = lines.find((line) => line.startsWith('data: '))
    if (!eventLine || !dataLine) {
      if (lines.length > 0) dropped += 1
      continue
    }
    try {
      events.push({
        event: eventLine.slice('event: '.length),
        data: JSON.parse(dataLine.slice('data: '.length)),
      })
    } catch {
      dropped += 1
    }
  }
  return { events, rest, dropped }
}
