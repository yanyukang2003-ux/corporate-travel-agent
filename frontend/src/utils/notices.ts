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

/**
 * 审批人打开这条申请时，第一眼要读到的那句话。
 *
 * "违规但值得破例"和"系统判不了"是两件不同的事，审批人得分得清自己在批哪一种。
 * 判不了的那种，后端已经把中文说明写进了 facts（"公司政策里还没有 Chengdu 的酒店
 * 夜费上限……"）；规则证据里的 message 是给日志看的英文，不拿来当这句话。
 */
export function approvalReasonText(option: {
  policy_outcome: string
  facts: string[]
  rule_evidence: { outcome: string; message: string }[]
}): string {
  if (option.policy_outcome === 'INSUFFICIENT_EVIDENCE') {
    const explained = factsForOptionCard(option.facts).find((fact) => !/^[A-Za-z]/.test(fact))
    return explained ?? '系统查不到判定这条方案所需的公司标准，需要你按实际情况确认。'
  }
  const violated = option.rule_evidence.find((rule) => rule.outcome === 'REQUIRES_APPROVAL')
  return violated?.message || '该方案超过自动通过阈值。'
}
