"use client"

import { useEffect, useRef, useState, type ReactNode } from "react"
import { fetchApi } from "../lib/api-config"
import { useT } from "../lib/i18n/provider"
import { checkMatches, checkSchema, contextSchema, haReady, jobSchema, nativeFinal, newMount, type NativeCheck, type NativeContext, type NativeDraft, type NativeJob } from "../lib/native-lxc"
import { Button } from "./ui/button"
import { ArrowLeft, ArrowRight, Check, ChevronRight, ClipboardCheck, HardDrive, History, LoaderCircle, KeyRound, Network, Server } from "lucide-react"

const BASE = "/api/create/lxc/v1"
export function templateLabel(name: string, arch: string | null) {
  const match = /^(alpine|debian|ubuntu|fedora|archlinux|centos|rockylinux|almalinux|opensuse)[-_]([\d.]+)/i.exec(name)
  if (!match) return name
  const titles: Record<string, string> = { alpine: "Alpine Linux", debian: "Debian", ubuntu: "Ubuntu", fedora: "Fedora", archlinux: "Arch Linux", centos: "CentOS", rockylinux: "Rocky Linux", almalinux: "AlmaLinux", opensuse: "openSUSE" }
  const build = /_([^_]+)_(?:amd64|arm64|aarch64|i386)\.tar(?:\.[a-z0-9]+)?$/i.exec(name)?.[1]
  return titles[match[1].toLowerCase()] + " " + match[2] + (build ? " · " + build : "") + (arch ? " · " + arch : "")
}
type Failure = { code: string; field?: string }
const control = "min-h-8 w-full min-w-0 rounded-md border border-input bg-background px-2.5 py-1 text-base sm:text-sm"
const groups = [
  { key: "system", icon: Server, sections: ["general", "template", "options"] },
  { key: "access", icon: KeyRound, sections: ["access"] },
  { key: "resources", icon: HardDrive, sections: ["disks", "cpu", "memory"] },
  { key: "network", icon: Network, sections: ["network", "dns"] },
  { key: "review", icon: ClipboardCheck, sections: ["confirm"] },
] as const

export function NativeLxcWizard({ onBack, active = true }: { onBack: () => void; active?: boolean }) {
  const translate = useT(), t = (key: string) => translate("create.native." + key)
  const [context, setContext] = useState<NativeContext | null>(null)
  const [draft, setDraft] = useState<NativeDraft | null>(null)
  const [step, setStep] = useState(0), [templateStorage, setTemplateStorage] = useState("")
  const [furthest, setFurthest] = useState(0)
  const [password, setPassword] = useState(""), [confirmPassword, setConfirmPassword] = useState("")
  const [checked, setChecked] = useState<NativeCheck | null>(null), [confirmations, setConfirmations] = useState<string[]>([])
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState<Failure | null>(null), [job, setJob] = useState<NativeJob | null>(null)
  const [uncertain, setUncertain] = useState(false), [history, setHistory] = useState<NativeJob[] | null>(null)
  const [saved, setSaved] = useState(false)
  const epoch = useRef(0), live = useRef(true), checkAbort = useRef<AbortController | null>(null)
  const form = useRef<HTMLFormElement>(null), heading = useRef<HTMLHeadingElement>(null)
  const checkDeadline = useRef(0), submitted = useRef(false), intent = useRef("")
  const contextAbort = useRef<AbortController | null>(null)
  const historyAbort = useRef<AbortController | null>(null), historyEpoch = useRef(0)
  const [historyDetail, setHistoryDetail] = useState(false)
  const scrollBody = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!active) {
      if (context) invalidate()
      setPassword(""); setConfirmPassword("")
      setDraft(value => value ? { ...value, password_set: false } : value)
    }
  }, [active])
  const home = <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={onBack}><ArrowLeft className="mr-2 h-4 w-4" aria-hidden="true" />{t("backToChoice")}</Button>

  function fail(error: unknown, fallback: string) {
    const body = (error as { body?: { error?: unknown; field?: unknown } })?.body
    setFailure({ code: typeof body?.error === "string" ? body.error : fallback, field: typeof body?.field === "string" ? body.field : undefined })
  }
  function invalidate() {
    epoch.current++; checkAbort.current?.abort(); setChecked(null); setConfirmations([]); setFailure(null); setSaved(false)
  }
  function update(path: string, value: unknown) {
    invalidate()
    setDraft(old => {
      if (!old) return old
      const next = structuredClone(old)
      const parts = path.split(".")
      let cursor = next as unknown as Record<string, unknown>
      for (const key of parts.slice(0, -1)) cursor = cursor[key] as Record<string, unknown>
      cursor[parts[parts.length - 1]] = value
      if (path === "unprivileged" && value === false) next.nesting = false
      if (path === "unprivileged" && value === true) { next.rootfs.quota = false; next.mounts.forEach(m => { m.quota = false }) }
      if (path.endsWith(".ro") && value === true) {
        const mount = next.mounts[Number(parts[1])]
        if (mount) mount.mount_options = mount.mount_options.filter(option => ["nodev", "noexec", "nosuid"].includes(option))
      }
      if (path.endsWith(".storage")) {
        const disk = parts[0] === "rootfs" ? next.rootfs : next.mounts[Number(parts[1])]
        const storage = context?.storages.find(item => item.id === value)
        if (disk) { if (!storage?.quota) disk.quota = false; if (!storage?.acl || (disk.acl === false && storage.acl_disable !== true)) disk.acl = null }
      }
      return next
    })
  }
  async function load() {
    const generation = ++epoch.current
    contextAbort.current?.abort(); const controller = new AbortController(); contextAbort.current = controller
    setLoading(true); setFailure(null)
    try {
      const reply = await fetchApi<{ success: boolean; context: unknown }>(BASE + "/context", { signal: controller.signal })
      const data = contextSchema.parse(reply.context)
      if (!reply.success) throw new Error()
      if (!live.current || generation !== epoch.current) return
      const initial = structuredClone(data.defaults)
      const storage = data.storages.find(x => x.active && x.content.includes("rootdir"))
      const template = data.templates.find(x => x.downloaded && x.arch === data.arch)
      initial.rootfs.storage = storage?.id || ""
      if (initial.network) initial.network.bridge = data.bridges.find(x => x.active)?.id || data.bridges[0]?.id || ""
      initial.template = template?.id || ""
      setContext(data); setDraft(initial); setTemplateStorage(template?.storage || ""); setStep(0); setFurthest(0)
    } catch (error) { if (live.current && generation === epoch.current && !controller.signal.aborted) fail(error, "context_unavailable") }
    finally { if (live.current && generation === epoch.current) setLoading(false) }
  }
  useEffect(() => {
    live.current = true; void load()
    return () => { live.current = false; epoch.current++; historyEpoch.current++; contextAbort.current?.abort(); checkAbort.current?.abort(); historyAbort.current?.abort() }
  }, [])
  useEffect(() => {
    heading.current?.focus({ preventScroll: true })
    scrollBody.current?.scrollTo({ top: 0 })
  }, [step, job?.id, history !== null])
  useEffect(() => {
    if (!checked) return
    const timer = setInterval(() => {
      if (performance.now() >= checkDeadline.current) { setChecked(null); setConfirmations([]); setFailure({ code: "check_expired" }) }
    }, 1000)
    return () => clearInterval(timer)
  }, [checked])
  useEffect(() => {
    if (!job || nativeFinal.has(job.status)) return
    let stopped = false, timer: ReturnType<typeof setTimeout>, delay = 2000
    const controller = new AbortController()
    async function poll() {
      try {
        const reply = await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs/" + job!.id, { signal: controller.signal })
        const next = jobSchema.parse(reply.job)
        if (!reply.success || next.id !== job!.id || next.intent !== job!.intent || next.node !== job!.node || next.vmid !== job!.vmid) throw new Error()
        if (stopped) return
        setJob(next); delay = 2000
        if (nativeFinal.has(next.status)) return
      } catch { if (stopped) return; setFailure({ code: "tracking_unavailable" }); delay = Math.min(delay * 2, 10000) }
      if (!stopped) timer = setTimeout(poll, delay)
    }
    timer = setTimeout(poll, delay)
    return () => { stopped = true; clearTimeout(timer); controller.abort() }
  }, [job?.id, job?.status])

  function cleanDraft(): NativeDraft {
    const value = structuredClone(draft!)
    value.tags = value.tags.filter(Boolean); value.dns.nameservers = value.dns.nameservers.filter(Boolean); value.dns.searchdomains = value.dns.searchdomains.filter(Boolean); value.firewall.ssh_sources = value.firewall.ssh_sources.filter(Boolean)
    return value
  }
  function accessValid() {
    if ((password && password.length < 5) || password !== confirmPassword || (!password && !draft?.ssh_public_keys.trim())) {
      setFailure({ code: "access_required" }); return false
    }
    return true
  }
  async function check() {
    if (!active || !draft || draft.vmid === null || !context || busy || submitted.current || !form.current?.reportValidity() || !accessValid()) return
    invalidate(); const generation = epoch.current; const controller = new AbortController(); checkAbort.current = controller
    setBusy(true)
    try {
      const reply = await fetchApi<{ success: boolean; context?: unknown } & NativeCheck>(BASE + "/checks", { method: "POST", body: JSON.stringify({ draft: cleanDraft() }), signal: controller.signal })
      const value = checkSchema.parse(reply)
      const freshContext = reply.context ? contextSchema.parse(reply.context) : context
      if (!reply.success || freshContext.node !== context.node || freshContext.arch !== context.arch || !checkMatches(value, draft, freshContext)) throw new Error()
      if (live.current && generation === epoch.current) { setContext(freshContext); checkDeadline.current = performance.now() + (value.expires_at - value.server_time) * 1000; setChecked(value) }
    } catch (error) { if (live.current && generation === epoch.current && !controller.signal.aborted) fail(error, "check_failed") }
    finally { if (live.current) setBusy(false) }
  }
  async function submit() {
    if (!active || !draft || draft.vmid === null || !context?.can_execute || !checked?.can_execute || busy || submitted.current || performance.now() >= checkDeadline.current || !accessValid()) return
    if (!checked.plan.warnings.every(w => confirmations.includes(w))) { setFailure({ code: "confirmation_required" }); return }
    submitted.current = true; intent.current = crypto.randomUUID().replace(/-/g, ""); setBusy(true); setFailure(null)
    const generation = epoch.current
    try {
      const reply = await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs", { method: "POST", body: JSON.stringify({ draft: cleanDraft(), check: checked.check, intent: intent.current, confirmations, credentials: { password } }) })
      const value = jobSchema.parse(reply.job)
      if (!reply.success || value.intent !== intent.current || value.vmid !== draft.vmid || value.node !== draft.node) throw new Error()
      if (live.current && generation === epoch.current) { setJob(value); setPassword(""); setConfirmPassword("") }
    } catch (error) {
      if (!live.current || generation !== epoch.current) return
      const status = (error as { status?: number })?.status
      if (status && [400, 401, 403, 409, 413, 415, 422].includes(status)) { submitted.current = false; setChecked(null); fail(error, "check_failed") }
      else { setUncertain(true); setFailure({ code: "submission_unknown" }) }
    } finally { if (live.current) { setBusy(false); setPassword(""); setConfirmPassword("") } }
  }
  async function showHistory() {
    const generation = ++historyEpoch.current
    historyAbort.current?.abort(); const controller = new AbortController(); historyAbort.current = controller
    setBusy(true); setFailure(null)
    try {
      const reply = await fetchApi<{ success: boolean; jobs: unknown[] }>(BASE + "/jobs", { signal: controller.signal })
      if (!reply.success || !Array.isArray(reply.jobs)) throw new Error()
      const values = reply.jobs.map(value => jobSchema.parse(value))
      if (!live.current || generation !== historyEpoch.current) return
      if (uncertain) {
        const admitted = values.find(value => value.intent === intent.current)
        if (admitted) { setJob(admitted); setUncertain(false) } else setFailure({ code: "submission_unknown" })
      } else { setJob(null); setHistory(values); setHistoryDetail(false) }
    } catch (error) { if (live.current && generation === historyEpoch.current && !controller.signal.aborted) fail(error, "tracking_unavailable") }
    finally { if (live.current && generation === historyEpoch.current) setBusy(false) }
  }
  async function openHistoryJob(item: NativeJob) {
    const generation = ++historyEpoch.current
    historyAbort.current?.abort(); const controller = new AbortController(); historyAbort.current = controller
    setBusy(true); setFailure(null)
    try {
      const reply = await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs/" + item.id, { signal: controller.signal })
      const value = jobSchema.parse(reply.job)
      if (!reply.success || value.id !== item.id || value.intent !== item.intent || value.node !== item.node || value.vmid !== item.vmid) throw new Error()
      if (live.current && generation === historyEpoch.current) { setJob(value); setHistory(null); setHistoryDetail(true) }
    } catch (error) { if (live.current && generation === historyEpoch.current && !controller.signal.aborted) fail(error, "tracking_unavailable") }
    finally { if (live.current && generation === historyEpoch.current) setBusy(false) }
  }
  function leaveHistory() {
    historyEpoch.current++; historyAbort.current?.abort(); setBusy(false); setFailure(null); setHistory(null)
  }
  function passwordChanged(value: string) { setPassword(value); update("password_set", Boolean(value)) }
  function setKeys(value: string) {
    if (/PRIVATE KEY/.test(value)) { update("ssh_public_keys", ""); setFailure({ code: "invalid_ssh_key" }); return }
    update("ssh_public_keys", value)
  }
  async function loadKey(file: File | undefined) {
    if (!file) return
    if (file.size > 32768) { setFailure({ code: "invalid_ssh_key" }); return }
    const generation = epoch.current
    try { const text = await file.text(); if (live.current && generation === epoch.current) setKeys((draft?.ssh_public_keys ? draft.ssh_public_keys + "\n" : "") + text.trim()) }
    catch { if (live.current) setFailure({ code: "invalid_ssh_key" }) }
  }
  function exportPlan() {
    if (!checked) return
    const value = { schema: "native.lxc.export.v1", plan: checked.plan }
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }))
    const anchor = document.createElement("a")
    anchor.href = url; anchor.download = "lxc-plan-" + checked.plan.vmid + ".json"
    anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); setSaved(true)
  }
  const field = (key: string, child: ReactNode, hint?: string) => <label className="block min-w-0 space-y-1 text-sm"><span>{t(key)}</span>{child}{hint && <span className="block text-xs text-muted-foreground">{t(hint)}</span>}</label>
  const input = (path: string, value: string | number | null, type = "text", attrs: Record<string, string | number | boolean> = {}) => <input className={control} type={type} data-native-field={path} value={value ?? ""} onChange={e => update(path, type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value)} {...attrs} />
  const select = (path: string, value: string, options: [string, string][], disabled = false) => <select className={control} data-native-field={path} value={value} disabled={disabled} required={!["pool", "network.ipv4.mode", "network.ipv6.mode", "firewall.mode"].includes(path)} onChange={e => update(path, e.target.value)}>{options.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>
  const checkbox = (path: string, key: string, value: boolean, disabled = false) => <label className="flex min-h-7 items-center gap-2 py-0.5 text-sm"><input className="h-4 w-4 shrink-0" type="checkbox" data-native-field={path} checked={value} disabled={disabled} onChange={e => update(path, e.target.checked)} /><span>{t(key)}</span></label>
  const notice = (key: string) => <p className="rounded-md border bg-muted/30 px-2.5 py-2 text-xs text-muted-foreground">{t(key)}</p>
  const errorStep = failure?.code === "access_required" ? 1 : failure?.field ? ({ node: 0, vmid: 0, hostname: 0, unprivileged: 0, nesting: 0, pool: 0, ha_managed: 0, tags: 0, startup: 0, onboot: 0, password_set: 1, ssh_public_keys: 1, template: 0, rootfs: 2, mounts: 2, cpu: 2, memory: 2, network: 3, firewall: 3, dns: 3 } as Record<string, number>)[failure.field.split(".")[0]] : undefined
  const error = failure && <div role="alert" className="space-y-2 rounded-lg border border-destructive/40 p-3 text-sm text-destructive"><p>{t("errors." + (translate("create.native.errors." + failure.code) === "create.native.errors." + failure.code ? "invalid_field" : failure.code))}</p>{errorStep !== undefined && <Button type="button" variant="outline" onClick={() => setStep(errorStep)}>{t("fieldAttention")}: {t("guided." + groups[errorStep].key)}</Button>}</div>


  if (history) return <section className="flex min-h-0 flex-1 flex-col" data-native-wizard aria-busy={busy}><div className="shrink-0 border-b px-3 py-1">{home}</div><div className="min-h-0 space-y-2.5 overflow-y-auto p-3"><h2 ref={heading} tabIndex={-1} className="text-lg font-semibold outline-none">{t("history")}</h2>{notice("historyHint")}{history.length ? history.map(item => <Button type="button" variant="outline" className="h-auto w-full justify-between gap-3 whitespace-normal p-3 text-left" key={item.id} disabled={busy} onClick={() => void openHistoryJob(item)}><span className="min-w-0 break-words">{item.node} · LXC {item.vmid}<span className="block text-xs font-normal text-muted-foreground">{typeof item.plan?.create?.hostname === "string" && item.plan.create.hostname}<span className="block">{new Date(item.created_at * 1000).toLocaleString()}</span></span></span><span className="ml-auto text-right text-sm font-normal">{t("status." + item.status)}</span><ChevronRight className="h-4 w-4 shrink-0" aria-hidden="true" /></Button>) : <p>{t("emptyHistory")}</p>}<Button type="button" variant="outline" onClick={leaveHistory}>{t("back")}</Button>{error}</div></section>
  if (job || uncertain) return <section className="flex min-h-0 flex-1 flex-col" data-native-wizard><div className="shrink-0 border-b px-3 py-1">{home}</div><div className="min-h-0 space-y-2.5 overflow-y-auto p-3"><h2 ref={heading} tabIndex={-1} className="text-lg font-semibold">{t(job ? "status." + job.status : "status.unknown")}</h2>{notice(job?.status === "succeeded" ? "successHint" : job && nativeFinal.has(job.status) ? "recoveryHint" : "progressHint")}{job && <p className="text-sm">{job.node} · LXC {job.vmid}{job.result && " · " + t("guestStatus." + job.result.status)}</p>}{job && job.task_warnings.length > 0 && <div role="status" className="rounded border border-amber-500/40 p-3 text-sm text-amber-600 dark:text-amber-400"><p>{t("taskWarnings")}</p>{job.task_warnings.map(warning => <details key={warning.upid}><summary>{warning.exitstatus}</summary><p className="break-all text-xs">{warning.phase} · {warning.upid}</p></details>)}</div>}{job?.upid && <details><summary className="cursor-pointer text-sm">{t("technical")}</summary><p className="break-all text-xs">{job.upid}</p></details>}{error}<div className="flex flex-wrap gap-2">{historyDetail && <Button type="button" variant="outline" disabled={busy} onClick={() => void showHistory()}><History className="mr-2 h-4 w-4" aria-hidden="true" />{t("history")}</Button>}{uncertain && <Button disabled={busy} onClick={() => void showHistory()}>{t("recoverTracking")}</Button>}</div></div></section>
  if (loading || !draft || !context) return <section className="min-h-[min(50dvh,360px)] space-y-3 p-4" data-native-wizard>{loading ? <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />{t("loading")}</p> : error}{loading && <div className="grid grid-cols-2 gap-3 pt-3" aria-hidden="true">{[0,1,2,3,4,5].map(n => <div key={n} className="h-12 animate-pulse rounded bg-muted" />)}</div>}<div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="ghost" onClick={onBack}>{t("backToChoice")}</Button><Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void showHistory()}><History className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />{t("history")}</Button>{!loading && <Button type="button" size="sm" onClick={() => void load()}>{t("retry")}</Button>}</div></section>

  const rootStorages = context.storages.filter(x => x.active && x.content.includes("rootdir"))
  const mountFields = (index: number) => {
    const m = draft.mounts[index], path = "mounts." + index
    return <div key={m.id} className="space-y-3 rounded-lg border p-3"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">{t("mount")} {m.id}</h3><Button type="button" variant="ghost" onClick={() => update("mounts", draft.mounts.filter((_, i) => i !== index))}>{t("remove")}</Button></div><div className="grid gap-3 sm:grid-cols-2">
      {field("mountKind", <select className={control} data-native-field={path + ".kind"} value={m.kind} onChange={e => { const bind = e.target.value === "bind"; update("mounts." + index, { ...m, kind: e.target.value, storage: bind ? "" : rootStorages[0]?.id || "", size_gib: bind ? null : 8, path_id: bind ? Object.keys(context.bind_paths)[0] || "" : "", ro: bind, backup: !bind, acl: null, quota: false, keepattrs: false, mount_options: [] }) }}><option value="volume">{t("volume")}</option><option value="bind" disabled={!context.capabilities.bind_mounts}>{t("bind")}</option></select>)}
      {field("target", input(path + ".target", m.target, "text", { required: true }))}
      {m.kind === "volume" ? <>{field("storage", select(path + ".storage", m.storage, [["", t("select")], ...rootStorages.map(x => [x.id, x.id] as [string, string])]))}{field("diskSize", input(path + ".size_gib", m.size_gib, "number", { min: 1, step: 0.1, required: true }))}</> : field("bindSource", select(path + ".path_id", m.path_id, Object.entries(context.bind_paths).map(([id, x]) => [id, x.path])))}
    </div>{checkbox(path + ".ro", "readOnly", m.ro)}{m.kind === "volume" ? checkbox(path + ".backup", "backup", m.backup) : notice("bindHint")}<details><summary className="cursor-pointer text-sm">{t("advanced")}</summary>{diskAdvanced(path, m, false)}</details></div>
  }
  function diskAdvanced(path: string, disk: NativeDraft["rootfs"] | NativeDraft["mounts"][number], root: boolean) {
    const storage = context?.storages.find(x => x.id === disk.storage)
    const options = !root && (disk as NativeDraft["mounts"][number]).ro ? ["nodev", "noexec", "nosuid"] : ["discard", "lazytime", "noatime", "nodev", "noexec", "nosuid"]
    return <div className="grid gap-3 pt-3 sm:grid-cols-2">
      {field("acl", <div><select className={control} data-native-field={path + ".acl"} disabled={!storage?.acl} value={disk.acl === null ? "default" : String(disk.acl)} onChange={e => update(path + ".acl", e.target.value === "default" ? null : e.target.value === "true")}><option value="default">{t("default")}</option><option value="true">{t("enabled")}</option><option value="false" disabled={storage?.acl_disable !== true}>{t("disabled")}</option></select>{storage?.acl && storage.acl_disable !== true && <p className="mt-1 text-xs text-muted-foreground">{t("aclDisableUnavailable")}</p>}</div>)}
      {checkbox(path + ".quota", "quota", disk.quota, draft!.unprivileged || !storage?.quota)}
      {checkbox(path + ".replicate", "replicate", disk.replicate)}
      <fieldset className="space-y-1 text-sm"><legend>{t("mountOptions")}</legend><div className="flex flex-wrap gap-3">{options.map(option => <label className="flex min-h-10 items-center gap-1" key={option}><input type="checkbox" checked={disk.mount_options.includes(option)} onChange={e => update(path + ".mount_options", e.target.checked ? [...disk.mount_options, option] : disk.mount_options.filter(x => x !== option))} />{option}</label>)}</div></fieldset>
      {!root && checkbox(path + ".keepattrs", "keepattrs", (disk as NativeDraft["mounts"][number]).keepattrs, !context?.capabilities.keepattrs)}
    </div>
  }
  const net = draft.network
  const content: Record<string, ReactNode> = {
    general: <><div className="grid gap-3 sm:grid-cols-2">{field("node", <input className={control} value={context.node} readOnly />)}{field("vmid", <div className="space-y-1">{input("vmid", draft.vmid, "number", { min: 100, max: 999999999, required: context.write_policy?.reason !== "ids_exhausted" })}{context.write_policy?.reason === "ids_exhausted" && <p className="text-xs text-amber-600 dark:text-amber-400" role="status">{t("idsExhausted")} {t("preparationOnly")}</p>}</div>, "idHint")}{field("hostname", input("hostname", draft.hostname, "text", { maxLength: 253 }))}<details className="sm:col-span-2"><summary className="cursor-pointer text-sm">{t("advanced")}</summary>{field("pool", select("pool", draft.pool, [["", t("none")], ...context.pools.map(x => [x, x] as [string, string])], context.pool_state !== "available"))}</details>
</div></>,
    options: <details><summary className="cursor-pointer text-sm">{t("containerOptions")}</summary><div className="space-y-1 pt-2">{checkbox("unprivileged", "unprivileged", draft.unprivileged)}<p className="text-xs text-muted-foreground">{t("friendly.unprivilegedHint")}</p>{!draft.unprivileged && notice("privilegedHint")}{checkbox("nesting", "nesting", draft.nesting, !draft.unprivileged)}<p className="text-xs text-muted-foreground">{t("friendly.nestingHint")}</p>{checkbox("ha_managed", "haManaged", draft.ha_managed, !haReady(context) && !draft.ha_managed)}{!haReady(context) && <div className="text-xs text-muted-foreground">{t(!context.capabilities.ha ? "haUnsupported" : context.ha_readiness.reason === "services_inactive" ? "haUnavailable" : context.ha_readiness.reason === "no_quorum" ? "haNoQuorum" : "haUnknown")}</div>}<details><summary className="cursor-pointer text-sm">{t("advanced")}</summary><div className="grid gap-3 pt-3 sm:grid-cols-2">{field("tags", <input className={control} data-native-field="tags" value={draft.tags.join(" ")} onChange={e => update("tags", e.target.value.split(/[ ;]+/))} />)}{checkbox("onboot", "onboot", draft.onboot)}{field("startOrder", input("startup.order", draft.startup.order, "number", { min: 0 }))}{field("startupDelay", input("startup.up", draft.startup.up, "number", { min: 0 }))}{field("shutdownTimeout", input("startup.down", draft.startup.down, "number", { min: 0 }))}</div></details></div></details>,
    access: <><div className="grid gap-3 sm:grid-cols-2">      {field("password", <input className={control} type="password" data-native-field="password" autoComplete="new-password" value={password} minLength={5} required={!draft.ssh_public_keys.trim()} maxLength={1024} onChange={e => passwordChanged(e.target.value)} />)}{field("confirmPassword", <input className={control} type="password" data-native-field="confirmPassword" autoComplete="new-password" value={confirmPassword} required={Boolean(password)} onChange={e => { invalidate(); setConfirmPassword(e.target.value) }} />)}
      <div className="sm:col-span-2">{field("sshKeys", <textarea className={control + " min-h-24"} data-native-field="ssh_public_keys" value={draft.ssh_public_keys} maxLength={32768} onChange={e => setKeys(e.target.value)} />)}{field("loadKey", <input className={control} type="file" accept=".pub,.txt" onChange={e => { void loadKey(e.target.files?.[0]); e.target.value = "" }} />)}</div></div>{notice("accessHint")}</>,
    template: <>{field("templateStorage", <select className={control} data-native-template-storage value={templateStorage} onChange={e => { setTemplateStorage(e.target.value); update("template", "") }}><option value="">{t("select")}</option>{context.storages.filter(x => x.active && x.content.includes("vztmpl")).map(x => <option key={x.id}>{x.id}</option>)}</select>)}{field("template", select("template", draft.template, [["", t("select")], ...context.templates.filter(x => x.storage === templateStorage && x.arch === context.arch && x.downloaded).map(x => [x.id, templateLabel(x.name, x.arch)] as [string, string])]))}<details><summary className="cursor-pointer text-xs text-muted-foreground">{t("technical")}</summary><p className="break-all text-xs">{context.templates.find(x => x.id === draft.template)?.name}</p>{notice("templateHint")}</details><p className="text-xs text-muted-foreground">{t("downloadDisabled")}</p></>,
    disks: <><div className="space-y-3 rounded-lg border p-3"><h3 className="font-medium">{t("rootfs")}</h3><div className="grid gap-3 sm:grid-cols-2">{field("storage", select("rootfs.storage", draft.rootfs.storage, [["", t("select")], ...rootStorages.map(x => [x.id, x.id] as [string, string])]))}{field("diskSize", input("rootfs.size_gib", draft.rootfs.size_gib, "number", { min: 1, step: 0.1, required: true }))}</div><details><summary className="cursor-pointer text-sm">{t("advanced")}</summary>{diskAdvanced("rootfs", draft.rootfs, true)}</details></div>{draft.mounts.map((_, index) => mountFields(index))}<Button type="button" variant="outline" disabled={draft.mounts.length >= 256} onClick={() => { const used = new Set(draft.mounts.map(x => x.id)); let id = 0; while (used.has(id)) id++; update("mounts", [...draft.mounts, { ...newMount(id), storage: rootStorages[0]?.id || "" }]) }}>{t("addMount")}</Button>{notice("backupHint")}<details><summary className="cursor-pointer text-sm">{t("devices")}</summary>{notice("devicesHint")}</details></>,
    cpu: <>{field("cores", input("cpu.cores", draft.cpu.cores, "number", { min: 1, max: 8192 }), "unlimitedHint")}<details><summary className="cursor-pointer text-sm">{t("advanced")}</summary><div className="grid gap-3 pt-3 sm:grid-cols-2">{field("cpuLimit", input("cpu.limit", draft.cpu.limit, "number", { min: 0, step: 0.1 }), "unlimitedHint")}{field("cpuUnits", input("cpu.units", draft.cpu.units, "number", { min: 8, max: context.cgroup === 2 ? 10000 : 500000 }), context.cgroup === 2 ? "unitsV2" : "unitsV1")}</div></details></>,
    memory: <div className="grid gap-3 sm:grid-cols-2">{field("memory", input("memory.memory", draft.memory.memory, "number", { min: 16, required: true }))}{field("swap", input("memory.swap", draft.memory.swap, "number", { min: 0, required: true }))}</div>,
    network: <><label className="flex min-h-10 items-center gap-2 text-sm"><input type="checkbox" data-native-network-enabled checked={net !== null} onChange={e => update("network", e.target.checked ? { ...context.defaults.network!, bridge: context.bridges[0]?.id || "" } : null)} />{t("networkEnabled")}</label>{net && <><div className="grid gap-3 sm:grid-cols-2">{field("interfaceName", input("network.name", net.name, "text", { required: true, maxLength: 15 }))}{field("mac", input("network.hwaddr", net.hwaddr), "autoHint")}{field("bridge", select("network.bridge", net.bridge, [["", t("select")], ...context.bridges.map(x => [x.id, x.id] as [string, string])]))}{field("vlan", input("network.tag", net.tag, "number", { min: 1, max: 4094 }))}</div>{checkbox("network.firewall", "interfaceFirewall", net.firewall)}<div className="grid gap-3 sm:grid-cols-2">{(["ipv4", "ipv6"] as const).map(version => <div className="space-y-3" key={version}>{field(version, select("network." + version + ".mode", net[version].mode, [["none", t("none")], ["static", t("static")], ["dhcp", "DHCP"], ...(version === "ipv6" ? [["auto", "SLAAC"] as [string, string]] : [])]))}{net[version].mode === "static" && <>{field("cidr", input("network." + version + ".address", net[version].address, "text", { required: true }))}{field("gateway", input("network." + version + ".gateway", net[version].gateway))}</>}</div>)}</div><details><summary className="cursor-pointer text-sm">{t("advanced")}</summary><div className="grid gap-3 pt-3 sm:grid-cols-2">{checkbox("network.link_down", "disconnect", net.link_down)}{field("mtu", input("network.mtu", net.mtu, "number", { min: 576, max: 65535 }), "bridgeMtuHint")}{field("rate", input("network.rate", net.rate, "number", { min: 0, max: 10240, step: 0.1 }), "unlimitedHint")}{checkbox("network.host_managed", "hostManaged", net.host_managed, !context.capabilities.host_managed)}</div></details></>}{notice("firewallHint")}<details><summary className="cursor-pointer text-sm">{t("firewallRules")}</summary>{field("firewallMode", <select className={control} data-native-field="firewall.mode" value={draft.firewall.mode} onChange={e => update("firewall.mode", e.target.value)}><option value="inherit">{t("inherit")}</option><option value="ssh_only" disabled={!context.capabilities.firewall_active || !net?.firewall}>{t("sshOnly")}</option></select>)}{draft.firewall.mode === "ssh_only" && <>{field("sshSources", <input className={control} data-native-field="firewall.ssh_sources" value={draft.firewall.ssh_sources.join(" ")} onChange={e => update("firewall.ssh_sources", e.target.value.split(/[ ,;]+/))} />)}{checkbox("firewall.log", "firewallLog", draft.firewall.log)}{notice("sshOnlyHint")}</>}</details></>,
    dns: <><div className="grid gap-3 sm:grid-cols-2">{field("dnsDomain", <input className={control} data-native-field="dns.searchdomains" value={draft.dns.searchdomains.join(" ")} onChange={e => update("dns.searchdomains", e.target.value.split(/[ ,;]+/))} placeholder={t("hostDefaults")} />)}{field("dnsServers", <input className={control} data-native-field="dns.nameservers" value={draft.dns.nameservers.join(" ")} onChange={e => update("dns.nameservers", e.target.value.split(/[ ,;]+/))} placeholder={t("hostDefaults")} />)}</div>{notice("dnsHint")}</>,
    confirm: <>{notice("reviewHint")}{[
      { key: "system", step: 0, rows: [["node", draft.node], ["vmid", draft.vmid ?? t("none")], ["hostname", draft.hostname || (draft.vmid === null ? t("none") : "CT" + draft.vmid)], ["template", templateLabel(context.templates.find(x => x.id === draft.template)?.name || draft.template, context.arch)], ["pool", draft.pool || t("none")], ["haManaged", t(draft.ha_managed ? "yes" : "no")], ["tags", draft.tags.filter(Boolean).join(" ") || t("none")], ["onboot", t(draft.onboot ? "yes" : "no")], ["startOrder", draft.startup.order ?? t("default")], ["startupDelay", draft.startup.up ?? t("default")], ["shutdownTimeout", draft.startup.down ?? t("default")], ["unprivileged", t(draft.unprivileged ? "yes" : "no")], ["nesting", t(draft.nesting ? "yes" : "no")]] },
      { key: "access", step: 1, rows: [["access", [draft.password_set && t("password"), draft.ssh_public_keys.trim() && t("sshKeys")].filter(Boolean).join(" + ") || t("none")]] },
      { key: "resources", step: 2, rows: [["storage", draft.rootfs.storage], ["diskSize", draft.rootfs.size_gib + " GiB"], ["cores", draft.cpu.cores ?? t("unlimited")], ["cpuLimit", draft.cpu.limit ?? t("unlimited")], ["cpuUnits", draft.cpu.units ?? (context.cgroup === 2 ? 100 : 1024)], ["memory", draft.memory.memory + " MiB"], ["swap", draft.memory.swap + " MiB"]] },
      { key: "network", step: 3, rows: [["networkEnabled", net ? net.name + " · " + net.bridge + (net.tag !== null ? " · VLAN " + net.tag : "") : t("none")], ["ipv4", net?.ipv4.mode === "static" ? net.ipv4.address : net?.ipv4.mode === "dhcp" ? "DHCP" : t("none")], ["gateway", net?.ipv4.gateway || t("none")], ["ipv6", net?.ipv6.mode === "static" ? net.ipv6.address : net?.ipv6.mode === "auto" ? "SLAAC" : net?.ipv6.mode === "dhcp" ? "DHCP" : t("none")], ["dnsServers", draft.dns.nameservers.filter(Boolean).join(" ") || t("hostDefaults")], ["dnsDomain", draft.dns.searchdomains.filter(Boolean).join(" ") || t("hostDefaults")]] },
    ].map(group => <section key={group.key} className="space-y-2 rounded-lg border p-3" data-native-review={group.key}><div className="flex items-center justify-between"><h3 className="text-sm font-semibold">{t("guided." + group.key)}</h3><Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => { invalidate(); setStep(group.step) }}>{t("friendly.edit")}</Button></div><dl className="grid gap-2 text-sm sm:grid-cols-2">{group.rows.map(([key, value]) => <div key={key} className="min-w-0"><dt className="text-xs text-muted-foreground">{t(String(key))}</dt><dd className="break-words font-medium">{value}</dd></div>)}</dl>{group.key === "resources" && draft.mounts.map(m => <p className="break-words text-xs [overflow-wrap:anywhere]" key={m.id}>{m.kind === "bind" ? context.bind_paths[m.path_id]?.path : m.storage + " · " + m.size_gib + " GiB"} → {m.target} · {t("backup")}: {t(m.backup ? "yes" : "no")}</p>)}</section>)}{draft.vmid === null && notice("friendly.idRequired")}{checkbox("start", "start", draft.start)}<div className="flex flex-wrap gap-2"><Button type="button" disabled={draft.vmid === null || busy || submitted.current} onClick={() => void check()}>{busy ? t("checking") : t("check")}</Button></div>{checked && <div className="space-y-3" data-native-checked><p role="status" className="text-sm">{t("checked")}</p>{checked.plan.warnings.map(w => <label key={w} className="flex items-start gap-2 rounded border p-3 text-sm"><input type="checkbox" className="mt-1" checked={confirmations.includes(w)} onChange={e => setConfirmations(e.target.checked ? [...confirmations, w] : confirmations.filter(x => x !== w))} /><span>{t("warnings." + w)}</span></label>)}<details><summary className="cursor-pointer text-sm">{t("technical")}</summary><pre className="mt-2 whitespace-pre-wrap break-all rounded border p-3 text-xs">{JSON.stringify({ ...checked.plan.create, ...checked.plan.final_config, firewall: checked.plan.firewall, start: checked.plan.start, access: { password_set: checked.plan.access.password_set, ssh_key_count: checked.plan.access.ssh_key_count } }, null, 2)}</pre></details><div className="flex flex-wrap gap-2"><Button type="button" variant="outline" onClick={exportPlan}>{t("save")}</Button><Button type="button" disabled={!context.can_execute || !checked.can_execute || busy || !checked.plan.warnings.every(w => confirmations.includes(w))} onClick={() => void submit()}>{t("create")}</Button></div>{saved && <p role="status" className="text-sm">{t("saved")}</p>}</div>}{!context.can_execute && notice("executionDisabled")}</>,
  }
  function goTo(index: number) {
    if (busy || index > furthest) return
    if (index > step && (!form.current?.reportValidity() || (step === 1 && !accessValid()))) return
    setFailure(null); setStep(index)
  }
  const nextStep = () => {
    if (!form.current?.reportValidity() || (step === 1 && !accessValid())) return
    setFailure(null); setStep(step + 1); setFurthest(Math.max(furthest, step + 1))
  }
  return <section className="flex min-h-0 flex-1 flex-col sm:flex-initial" data-native-wizard aria-busy={busy}>
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b px-3 py-1">
      {home}
      <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs text-muted-foreground" disabled={busy} onClick={() => void showHistory()}><History className="mr-2 h-4 w-4" aria-hidden="true" />{t("history")}</Button>
    </div>
    <div className="flex min-h-0 flex-1 flex-col">
      <nav className="shrink-0 border-b bg-muted/20 px-3 py-1.5" aria-label={t("stepsLabel")}>
        <ol className="grid grid-cols-5 gap-1">{groups.map(({ key }, i) => <li key={key} aria-current={i === step ? "step" : undefined}>
          <Button type="button" size="sm" variant={i === step ? "secondary" : "ghost"} className={"h-8 w-full min-w-0 gap-1.5 px-1.5 text-xs " + (i === step ? "font-semibold" : "text-muted-foreground")} disabled={busy || i > furthest} onClick={() => goTo(i)} aria-label={t("guided." + key)}>
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded border text-[11px]" aria-hidden="true">{i < step ? <Check className="h-3 w-3" /> : i + 1}</span>
            <span className="hidden min-w-0 sm:block">{t("guided." + key)}</span>
          </Button>
        </li>)}</ol>
      </nav>
      <div ref={scrollBody} data-native-scroll className="min-h-0 min-w-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-3 sm:max-h-[calc(min(86dvh,700px)-190px)]">
        <header><h2 ref={heading} tabIndex={-1} className="text-sm font-semibold outline-none">{t("guided." + groups[step].key)}</h2><p className="mt-0.5 text-xs text-muted-foreground">{t("guided." + groups[step].key + "Hint")}</p></header>
        {!context.can_execute && context.write_policy?.reason !== "ids_exhausted" && notice(context.write_policy?.reason === "lock_unavailable" ? "lockUnavailable" : "activationUnavailable")}
        <form ref={form} className="space-y-3" onSubmit={e => { e.preventDefault(); if (step === 4) void check(); else nextStep() }}>
          <fieldset className="min-w-0 space-y-3" disabled={busy}>{groups[step].sections.map(key => <section className="min-w-0 space-y-2.5" key={key} data-native-section={key}>{groups[step].sections.length > 1 && key !== "general" && key !== "options" && <h3 className="border-b pb-1 text-sm font-semibold">{t("steps." + key)}</h3>}{content[key]}</section>)}</fieldset>
        </form>
        {error}
      </div>
    </div>
    <footer data-native-footer className="flex shrink-0 items-center justify-between gap-3 border-t bg-card px-3 py-2">
      <Button type="button" size="sm" variant="outline" disabled={step === 0 || busy} onClick={() => { setFailure(null); setStep(step - 1) }}><ArrowLeft className="mr-2 h-4 w-4" aria-hidden="true" />{t("back")}</Button>
      <span className="text-xs text-muted-foreground">{step + 1} / {groups.length}</span>
      {step < groups.length - 1 ? <Button type="button" size="sm" disabled={busy} onClick={nextStep}>{t("next")}<ArrowRight className="ml-2 h-4 w-4" aria-hidden="true" /></Button> : <span className="text-xs text-muted-foreground">{t("guided.reviewFooter")}</span>}
    </footer>
  </section>
}
