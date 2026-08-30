/**
 * 方案旁边要给人看的说明：空段、未决问题。
 * 机器事实长成 total_cost=1760；空段说明是整句人话。
 */

/** 有方案时仍要挂在旁边的未决问题。一行一条。 */
export function openQuestionsFromTask(task: {
  clarification_question: string | null
}): string[] {
  if (!task.clarification_question) return []
  return task.clarification_question
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
}

/** 卡片脚注优先展示人话；没有人话时才退回机器键值。 */
export function factsForOptionCard(facts: string[]): string[] {
  const human = facts.filter((fact) => !/^[A-Za-z][A-Za-z0-9_]*=/.test(fact))
  const chosen = human.length > 0 ? human : facts
  return chosen.slice(0, 3)
}
