/**
 * 会话草稿工具：在 sessionStorage 中暂存规划输入与澄清答案。
 * 避免刷新或提交失败时丢失用户已输入内容；示例文案不会作为初始值。
 */

/** 仅用于「填入示例」按钮的演示文案，不会作为作曲器初始值。 */
export const EXAMPLE_TRIP_MESSAGE =
  '下周二从北京去上海见客户，上午出发，周三下午回来。优先高铁，酒店离客户公司近一点。'

/** 自然语言作曲器草稿在 sessionStorage 中的键名。 */
export const COMPOSER_DRAFT_KEY = 'cta.composerDraft'

/** 按任务 ID 生成澄清草稿的存储键。 */
export function clarificationDraftKey(taskId: string): string {
  return `cta.clarificationDraft.${taskId}`
}

/** 可注入的 Storage 抽象，便于测试替换 sessionStorage。 */
export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

/** 澄清面板本地草稿：自定义答案与上次失败提交。 */
export interface ClarificationDraft {
  customAnswers: Record<string, string>
  lastFailedAnswer: string
}

/** 安全获取 sessionStorage；不可用时返回 null。 */
export function defaultSessionStorage(): StorageLike | null {
  try {
    if (typeof sessionStorage === 'undefined') return null
    return sessionStorage
  } catch {
    return null
  }
}

/** 判断文本是否恰好等于演示示例行程。 */
export function isExampleTripMessage(text: string): boolean {
  return text.trim() === EXAMPLE_TRIP_MESSAGE
}

/** 读取自然语言作曲器草稿；无存储或失败时返回空串。 */
export function readComposerDraft(storage: StorageLike | null = defaultSessionStorage()): string {
  if (!storage) return ''
  try {
    return storage.getItem(COMPOSER_DRAFT_KEY) ?? ''
  } catch {
    return ''
  }
}

/**
 * 写入自然语言草稿；空内容时删除键。
 * 隐私模式/配额失败时静默忽略，仅保留内存态。
 */
export function writeComposerDraft(
  text: string,
  storage: StorageLike | null = defaultSessionStorage(),
): void {
  if (!storage) return
  try {
    if (!text.trim()) {
      storage.removeItem(COMPOSER_DRAFT_KEY)
      return
    }
    storage.setItem(COMPOSER_DRAFT_KEY, text)
  } catch {
    // 隐私模式或配额不足：仅保留内存中的草稿。
  }
}

/** 清除自然语言作曲器草稿。 */
export function clearComposerDraft(storage: StorageLike | null = defaultSessionStorage()): void {
  if (!storage) return
  try {
    storage.removeItem(COMPOSER_DRAFT_KEY)
  } catch {
    // 忽略存储不可用
  }
}

/**
 * 作曲器初始文案：仅恢复本会话已输入内容，永不回填示例句。
 */
export function initialComposerMessage(
  storage: StorageLike | null = defaultSessionStorage(),
): string {
  return readComposerDraft(storage)
}

/** 读取指定任务的澄清草稿；损坏或缺失时返回 null。 */
export function readClarificationDraft(
  taskId: string,
  storage: StorageLike | null = defaultSessionStorage(),
): ClarificationDraft | null {
  if (!storage || !taskId) return null
  try {
    const raw = storage.getItem(clarificationDraftKey(taskId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as ClarificationDraft
    if (!parsed || typeof parsed !== 'object') return null
    return {
      customAnswers: parsed.customAnswers && typeof parsed.customAnswers === 'object'
        ? parsed.customAnswers
        : {},
      lastFailedAnswer: typeof parsed.lastFailedAnswer === 'string' ? parsed.lastFailedAnswer : '',
    }
  } catch {
    return null
  }
}

/**
 * 写入澄清草稿；内容全空时删除对应键。
 */
export function writeClarificationDraft(
  taskId: string,
  draft: ClarificationDraft,
  storage: StorageLike | null = defaultSessionStorage(),
): void {
  if (!storage || !taskId) return
  try {
    const empty = !draft.lastFailedAnswer.trim()
      && Object.values(draft.customAnswers).every((value) => !value.trim())
    if (empty) {
      storage.removeItem(clarificationDraftKey(taskId))
      return
    }
    storage.setItem(clarificationDraftKey(taskId), JSON.stringify(draft))
  } catch {
    // 忽略存储不可用
  }
}

/** 清除指定任务的澄清草稿。 */
export function clearClarificationDraft(
  taskId: string,
  storage: StorageLike | null = defaultSessionStorage(),
): void {
  if (!storage || !taskId) return
  try {
    storage.removeItem(clarificationDraftKey(taskId))
  } catch {
    // 忽略存储不可用
  }
}

/**
 * 决定澄清面板「重试」按钮应重放上次失败补充，还是原指令。
 */
export function clarificationRetry(options: {
  originalInstruction: string
  lastFailedAnswer: string
  hasSubmitError: boolean
}): { text: string; label: string; mode: 'failed_supplement' | 'original' } {
  const failed = options.lastFailedAnswer.trim()
  if (options.hasSubmitError && failed) {
    return { text: failed, label: '重试刚才的补充', mode: 'failed_supplement' }
  }
  return {
    text: options.originalInstruction,
    label: '用原指令重试抽取',
    mode: 'original',
  }
}
