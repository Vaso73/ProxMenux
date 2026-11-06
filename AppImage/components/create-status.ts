/** Keep palette literals with components so Tailwind includes every shared accent. */
export const createStatusText = {
  info: "text-blue-700 dark:text-blue-400",
  success: "text-green-700 dark:text-green-400",
  warning: "text-amber-700 dark:text-amber-400",
  error: "text-red-700 dark:text-red-400",
  neutral: "text-muted-foreground",
} as const

export type CreateStatusTone = keyof typeof createStatusText
