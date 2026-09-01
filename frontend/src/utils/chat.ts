/**
 * 聊天视图要显示的对话轮次。
 *
 * 后端的 `messages` 只有两种消息：用户说的话，和助手**提问**那一条。
 * 助手的推荐理由（`agentic_proposal.summary`）**不在** `messages` 里——它一旦进了
 * 对话，就等于改了下一轮喂给模型的输入。所以拼接这件事只能在前端做：
 * 把推荐理由放回它该在的位置（提问之前），并且不能重复显示。
 */

/** 一条可显示的对话轮次。 */
export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
  /** 助手这条是不是"还没定的事"，UI 用它换一种样式。 */
  kind: 'say' | 'question'
  key: string
}

interface MessageLike {
  role: string
  content: string
  created_at?: string
}

interface TaskLike {
  messages?: MessageLike[]
  clarification_question?: string | null
  agentic_proposal?: { summary?: string; open_questions?: string[] } | null
}

/** 去掉空白，用来判断两段话是不是同一段。 */
function same(left: string, right: string): boolean {
  return left.replace(/\s+/g, '') === right.replace(/\s+/g, '')
}

/**
 * 把任务拼成聊天轮次。
 *
 * 规则：
 * 1. 用户消息按原顺序显示。
 * 2. 助手消息里，内容等于当前未决问题的那条标成 `question`。
 * 3. 推荐理由插在**最后一条助手提问之前**；没有提问就放在末尾。
 * 4. 推荐理由已经出现在某条消息里时不再重复插入。
 */
export function chatTurns(task: TaskLike | null): ChatTurn[] {
  if (!task) return []
  const messages = task.messages ?? []
  const summary = task.agentic_proposal?.summary?.trim() ?? ''
  const turns: ChatTurn[] = messages.map((item, index) => ({
    role: item.role === 'assistant' ? 'assistant' : 'user',
    content: item.content,
    kind: item.role === 'assistant' ? 'question' : 'say',
    key: `${item.created_at ?? 'turn'}-${index}`,
  }))
  if (!summary) return turns
  if (turns.some((turn) => same(turn.content, summary) || turn.content.includes(summary))) {
    return turns
  }
  const proposalTurn: ChatTurn = {
    role: 'assistant',
    content: summary,
    kind: 'say',
    key: 'agentic-proposal',
  }
  // 最后一条助手消息是"还没定的事"，推荐理由要排在它前面：先说查到了什么，再说还差什么。
  let insertAt = turns.length
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    if (turns[index].role !== 'assistant') break
    insertAt = index
  }
  return [...turns.slice(0, insertAt), proposalTurn, ...turns.slice(insertAt)]
}
