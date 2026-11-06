export const MANAGER_BASE = "/api/create/oci/manager/v1"
export const managerFinal = new Set(["completed", "cancelled", "expired", "failed", "recovery_required"])
export type ManagerQuestion = {
  id: string; kind: string; text: string; title: string | null; default: unknown
  options: [string, string][]; expires_at: number; back: boolean; execution?: boolean; required?: boolean; show_tags?: boolean; limit_bytes?: number
}
export type ManagerJob = {
  id: string; operation: string; parameters: Record<string, unknown>; node: string; status: string
  question: ManagerQuestion | null; error: string | null; created_at: number; server_time: number
  progress?: string; result: { operation: string; notices: string[]; installation?: {
    vmid: number | null; stack_vmids: Record<string, number>; urls: { label: string; url: string }[]; credentials_available: boolean
  } } | null
}
const kinds = new Set(["ask", "password", "confirm", "choose", "checklist", "detail_menu", "message", "review", "multiline"])
const object = (v: unknown): v is Record<string, any> => Boolean(v && typeof v === "object" && !Array.isArray(v))
const vmid = (v: unknown): v is number => Number.isInteger(v) && Number(v) >= 100 && Number(v) <= 999999999
export function validManagerView(v: unknown): v is Record<string, any> {
  if (!object(v) || v.success !== true || v.schema !== "oci.manager.v1" || typeof v.node !== "string"
    || !Array.isArray(v.operations) || !v.operations.every((op: unknown) => typeof op === "string")) return false
  for (const key of ["installed", "registries", "updates"]) if (!object(v[key]) || typeof v[key].available !== "boolean") return false
  if (v.installed.available && (!Array.isArray(v.installed.items) || !v.installed.items.every((a: any) => object(a)
    && vmid(a.vmid) && ["title", "hostname", "image", "status"].every(k => typeof a[k] === "string")
    && ["stack", "pending", "stack_pending", "supports_update", "watchdog"].every(k => typeof a[k] === "boolean")
    && Array.isArray(a.members) && a.members.every((m: any) => object(m) && vmid(m.vmid) && typeof m.name === "string")))) return false
  if (v.registries.available && (!Array.isArray(v.registries.items) || !v.registries.items.every((a: any) => object(a)
    && typeof a.registry === "string" && (a.account === null || typeof a.account === "string")
    && typeof a.method === "string" && typeof a.status === "string"))) return false
  if (v.updates.available && (!object(v.updates.state) || typeof v.updates.state.checked !== "boolean"
    || !Number.isInteger(v.updates.state.unknown) || v.updates.state.unknown < 0
    || !Array.isArray(v.updates.state.available) || !v.updates.state.available.every((a: any) => object(a) && vmid(a.vmid) && typeof a.title === "string"))) return false
  return true
}
export function validManagerJob(value: unknown): value is ManagerJob {
  if (!object(value)) return false
  const v = value as ManagerJob
  if (typeof v.id !== "string" || !/^[a-f0-9]{32}$/.test(v.id) || typeof v.operation !== "string" || typeof v.node !== "string"
    || !v.parameters || typeof v.parameters !== "object" || Array.isArray(v.parameters)
    || !Number.isFinite(v.server_time) || !Number.isFinite(v.created_at)
    || (v.progress !== undefined && v.progress !== null && typeof v.progress !== "string")
    || (v.error !== null && typeof v.error !== "string")
    || (v.status !== "question" && v.question !== null)
    || (!managerFinal.has(v.status) && !["starting", "configuring", "question", "installing", "finishing"].includes(v.status))) return false
  if (v.status === "question") {
    const q = v.question
    if (!object(q) || typeof q.id !== "string" || !/^[a-f0-9]{32}$/.test(q.id) || !kinds.has(q.kind) || typeof q.text !== "string"
      || typeof q.back !== "boolean" || (q.title !== null && typeof q.title !== "string")
      || (q.required !== undefined && typeof q.required !== "boolean")
      || (q.show_tags !== undefined && typeof q.show_tags !== "boolean")
      || !Number.isFinite(q.expires_at) || !Array.isArray(q.options)
      || !q.options.every(o => Array.isArray(o) && o.length === 2 && o.every(s => typeof s === "string"))
      || (q.execution !== undefined && typeof q.execution !== "boolean")
      || (q.kind === "password" && q.default !== null)
      || (q.kind === "multiline" && (!Number.isInteger(q.limit_bytes) || q.limit_bytes! < 1 || q.limit_bytes! > 2 * 1024 * 1024 || q.default !== null))) return false
  }
  if (v.result !== null) {
    if (!object(v.result) || v.result.operation !== v.operation || !Array.isArray(v.result.notices) || !v.result.notices.every(n => typeof n === "string")) return false
    const i = v.result.installation
    if (i && (!object(i) || (i.vmid !== null && !vmid(i.vmid)) || !object(i.stack_vmids)
      || !Object.values(i.stack_vmids).every(vmid)
      || !Array.isArray(i.urls) || !i.urls.every(u => object(u) && typeof u.label === "string" && typeof u.url === "string" && /^https?:\/\/[^\s/@]+(?:\/[^\s]*)?$/.test(u.url))
      || typeof i.credentials_available !== "boolean")) return false
  }
  return v.status !== "completed" || v.result !== null
}
