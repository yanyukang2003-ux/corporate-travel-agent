/**
 * 方案是怎么排出来的。
 *
 * 后端把价格和时长加成**一个分数**，分低者胜：
 *
 *   总分 = 票价合计 + 时长(分钟) ÷ 每单位分钟数 + 偏好罚分
 *
 * "每单位分钟数"是一个写明的选择，不是自然常数：默认 10（10 分钟折 1 块钱），
 * 说了"怎么便宜怎么来"变 60，说了"越快越好"变 1。它由后端 `scoring` 字段给出，
 * 前端不猜——猜出来的比例会让这一页看着像解释、其实是编的。
 */

/** 一条方案的分数拆解。 */
export interface ScoreBreakdown {
  optionId: string
  /** 综合推荐 / 最便宜 / 最快，来自后端的 category 事实。 */
  categories: string[]
  cost: number
  minutes: number
  /** 时长折算进总分的那一部分。 */
  durationScore: number
  penalty: number
  total: number
  /** 后端给的 score 和这里重算的对不对得上。对不上就别装作解释得了。 */
  reconciles: boolean
}

interface OptionLike {
  option_id: string
  score: string | number
  total_cost: string | number
  total_duration_minutes: number
  preference_penalty: string | number
  facts?: string[]
}

const CATEGORY_LABELS: Record<string, string> = {
  best_overall: '综合最合适',
  cheapest: '最便宜',
  fastest: '最快',
  alternative: '备选',
}

/** 把 `category=best_overall|fastest` 这条机器事实翻成人话。 */
export function categoriesFromFacts(facts: string[] | undefined): string[] {
  const raw = (facts ?? []).find((fact) => fact.startsWith('category='))
  if (!raw) return []
  return raw
    .slice('category='.length)
    .split('|')
    .map((item) => CATEGORY_LABELS[item] ?? item)
    .filter(Boolean)
}

/** 按后端公式重算一条方案的分数，并核对能不能对上。 */
export function scoreBreakdown(option: OptionLike, minutesPerUnit: number): ScoreBreakdown {
  const cost = Number(option.total_cost)
  const penalty = Number(option.preference_penalty)
  const total = Number(option.score)
  const minutes = option.total_duration_minutes
  const durationScore = minutesPerUnit > 0 ? minutes / minutesPerUnit : 0
  const recomputed = cost + durationScore + penalty
  return {
    optionId: option.option_id,
    categories: categoriesFromFacts(option.facts),
    cost,
    minutes,
    durationScore,
    penalty,
    total,
    // 分是十进制小数算出来的，浮点重算允许一分钱以内的误差。
    reconciles: Number.isFinite(recomputed) && Math.abs(recomputed - total) < 0.01,
  }
}

/** 一组方案的分数拆解，按总分升序——**分低者胜**。 */
export function rankedBreakdowns(
  options: OptionLike[],
  minutesPerUnit: number,
): ScoreBreakdown[] {
  return options
    .map((option) => scoreBreakdown(option, minutesPerUnit))
    .sort((left, right) => left.total - right.total)
}
