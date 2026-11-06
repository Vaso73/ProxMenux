"use client"

import { useEffect, useRef, useState } from "react"
import { fetchApi } from "../lib/api-config"
import { useI18n } from "../lib/i18n/provider"
import { MANAGER_BASE as BASE, managerFinal, validManagerJob, type ManagerJob } from "../lib/oci-manager"
import { Button } from "./ui/button"
import { Input } from "./ui/input"

export function OciManagerOperation({ operation, parameters, resume, onChanged, onStatusChange, active = true, initialSource }: {
  operation?: string; parameters?: Record<string, unknown>; resume?: string; onChanged?: () => void
  active?: boolean; initialSource?: "image" | "paste"
  onStatusChange?: (status: ManagerJob["status"]) => void
}) {
  const { t, language } = useI18n()
  const [job, setJob] = useState<ManagerJob | null>(null)
  const [error, setError] = useState(false)
  const [fileError, setFileError] = useState(false)
  const [fileReading, setFileReading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [text, setText] = useState("")
  const [repeated, setRepeated] = useState("")
  const [selected, setSelected] = useState<string[]>([])
  const [clock, setClock] = useState(0)
  const [credentials, setCredentials] = useState<unknown>(null)
  const requestId = useRef("")
  const mutation = useRef(false)
  const answered = useRef("")
  const sequence = useRef(0)
  const fileSequence = useRef(0)
  const intentLanguage = useRef(language === "sk" ? "sk" : "en")
  const identity = JSON.stringify(parameters || {})
  const q = job?.question
  const offset = useRef(0)
  const initialSourceSent = useRef(false)
  const visible = useRef(active)
  visible.current = active
  const pendingBegin = useRef<null | (() => Promise<void>)>(null)
  useEffect(() => {
    if (!active) { setText(""); setRepeated(""); setSelected([]); setCredentials(null); fileSequence.current++; setFileReading(false) }
    else if (pendingBegin.current) { const begin = pendingBegin.current; pendingBegin.current = null; void begin() }
  }, [active])
  function accept(value: { success: boolean; job: unknown }) {
    if (!value.success || !validManagerJob(value.job)) throw Error("response_invalid")
    if (operation && value.job.operation !== operation) throw Error("operation_changed")
    offset.current = Date.now() - value.job.server_time * 1000
    setClock(Date.now()); setJob(value.job); setError(false)
  }
  useEffect(() => {
    let active = true
    const mine = ++sequence.current
    setJob(null); setError(false); setCredentials(null); setText(""); setRepeated(""); mutation.current = false
    requestId.current ||= crypto.randomUUID().replace(/-/g, "")
    async function begin() {
      try {
        if (resume) {
          const value = await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs/" + resume)
          if (active && mine === sequence.current) accept(value)
        } else {
          const context = await fetchApi<{ success: boolean; node: string; operations: string[] }>(BASE + "/context")
          if (!context.success || !operation || !context.operations.includes(operation)) throw Error("unavailable")
          if (!active || mine !== sequence.current) return
          async function admit() {
            if (!active || mine !== sequence.current || !visible.current) return
            try {
              const value = await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ operation, parameters: JSON.parse(identity), request_id: requestId.current, node: context.node, language: intentLanguage.current }),
              })
              if (active && mine === sequence.current) accept(value)
            } catch { if (active) setError(true) }
          }
          if (visible.current) await admit()
          else pendingBegin.current = admit
        }
      } catch { if (active) setError(true) }
    }
    void begin()
    return () => { active = false; pendingBegin.current = null; sequence.current++; fileSequence.current++ }
  }, [operation, identity, resume])
  useEffect(() => {
    if (!job || managerFinal.has(job.status)) return
    let active = true
    const timer = setInterval(async () => {
      const mine = ++sequence.current
      try {
        const value = await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs/" + job.id)
        if (active && mine === sequence.current && !mutation.current) accept(value)
      } catch { if (active) setError(true) }
    }, 1500)
    return () => { active = false; clearInterval(timer) }
  }, [job?.id, job?.status])
  useEffect(() => {
    setText(q?.kind !== "password" && q?.kind !== "multiline" && typeof q?.default === "string" ? q.default : "")
    setRepeated("")
    setSelected(Array.isArray(q?.default) ? q.default.filter(v => typeof v === "string") : [])
    setFileError(false)
    setFileReading(false)
    fileSequence.current++
    answered.current = ""
  }, [q?.id])
  useEffect(() => { if (job?.status === "completed") onChanged?.() }, [job?.id, job?.status])
  useEffect(() => { if (job) onStatusChange?.(job.status) }, [job?.id, job?.status, onStatusChange])
  useEffect(() => { const timer = setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer) }, [])
  const expired = Boolean(q && clock - offset.current >= q.expires_at * 1000)
  const waiting = busy || fileReading || Boolean(q && answered.current === q.id)
  const passwordInvalid = q?.kind === "password" && (text ? text !== repeated : q.required !== false)
  const booleanDefault = q?.kind === "confirm" && !q.execution && typeof q.default === "boolean"
  useEffect(() => {
    // Only the original custom source question can consume an explicitly chosen source.
    // A failed answer is never replayed automatically; refresh/recovery stays read-only.
    const sources = ["paste", "file", "url", "run", "image", "oci-compose-paste", "oci-compose", "oci-compose-url"]
    if (!active || !initialSource || initialSourceSent.current || operation !== "custom.install" || q?.kind !== "choose" || q.execution || q.options.length !== sources.length || !sources.every(key => q.options.some(([id]) => id === key))) return
    initialSourceSent.current = true
    void answer(initialSource)
  }, [active, q?.id, initialSource])
  async function answer(value: unknown, cancel = false) {
    if (!active || !job || !q || busy || mutation.current || answered.current === q.id || expired) return
    if (!cancel && typeof value === "string" && passwordInvalid) return
    mutation.current = true; answered.current = q.id; setBusy(true); ++sequence.current
    try {
      accept(await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs/" + job.id + "/answer", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cancel ? { cancel: true } : { question_id: q.id, value }),
      }))
      setText("")
      setRepeated("")
    } catch { answered.current = ""; setError(true) } finally { setBusy(false); mutation.current = false }
  }
  async function refresh() {
    if (!job || mutation.current) return
    try { accept(await fetchApi<{ success: boolean; job: unknown }>(BASE + "/jobs/" + job.id)) } catch { setError(true) }
  }
  const installation = job?.result?.installation
  return <div className="space-y-4" data-oci-manager-operation>
    {error && <p role="alert">{t("ociManager.connection")}</p>}
    {!job && !error && <p aria-live="polite">{t("ociManager.loading")}</p>}
    {job && managerFinal.has(job.status) ? <div className="space-y-3">
      <h3 className="font-medium">{t("ociManager.states." + job.status)}</h3>
      {job.result?.notices.map((message, index) => <p className="whitespace-pre-wrap break-words text-sm" key={index}>{message}</p>)}
      {installation && <div className="rounded-lg border p-3 space-y-2">
        {installation.vmid && <p>LXC {installation.vmid}</p>}
        {Object.entries(installation.stack_vmids).map(([name, id]) => <p key={name}>{name}: LXC {id}</p>)}
        {installation.urls.map((url, i) => <p key={i}><a className="text-blue-600 underline dark:text-blue-400" href={url.url} target="_blank" rel="noreferrer">{url.label}</a></p>)}
        {installation.credentials_available && <Button variant="outline" onClick={async () => {
          try { const value = await fetchApi<{ success: boolean; credentials: unknown }>(BASE + "/jobs/" + job.id + "/credentials", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); if (!value.success) throw Error(); if (visible.current) setCredentials(value.credentials) } catch { setError(true) }
        }}>{t("ociManager.credentials")}</Button>}
        {credentials !== null && <pre className="whitespace-pre-wrap break-all text-sm">{JSON.stringify(credentials, null, 2)}</pre>}
      </div>}
    </div> : q ? <div className="space-y-3">
      {q.title && <h3 className="font-medium">{q.title}</h3>}
      <p className="whitespace-pre-wrap break-words text-sm">{q.text}</p>
      {q.execution && <p className="text-sm text-muted-foreground">{t("ociManager.finalConfirmation")}</p>}
      {booleanDefault && <p className="text-sm text-muted-foreground">{t("ociManager.defaultChoice", { value: t(q.default ? "ociManager.yes" : "ociManager.no") })}</p>}
      {q.kind === "choose" || q.kind === "detail_menu" ? <select className="w-full rounded-md border bg-background p-2" aria-label={q.title || q.text} value={text || (typeof q.default === "string" ? q.default : "")} disabled={busy || expired} onChange={e => setText(e.target.value)}>
        <option value="">{t("ociManager.select")}</option>{q.options.map(([key, label]) => <option key={key} value={key}>{q.show_tags ? `${key} · ${label}` : label}</option>)}
      </select> : q.kind === "checklist" ? <div className="space-y-2">{q.options.map(([key, label]) => <label className="flex items-start gap-2" key={key}><input type="checkbox" checked={selected.includes(key)} disabled={waiting || expired} onChange={e => setSelected(v => e.target.checked ? [...v, key] : v.filter(k => k !== key))} />{label}</label>)}</div>
      : q.kind === "multiline" ? <><label className="block text-sm">{t("ociManager.upload")}<input className="block w-full" type="file" disabled={busy || expired} onChange={async e => {
        const file = e.target.files?.[0], mine = ++fileSequence.current
        setFileReading(false)
        if (!file) return
        if (file.size > q.limit_bytes!) { setFileError(true); return }
        setFileReading(true)
        try { const value = await file.text(); if (mine === fileSequence.current) { setText(value); setFileError(false) } }
        catch { if (mine === fileSequence.current) setFileError(true) }
        finally { if (mine === fileSequence.current) setFileReading(false) }
      }} /></label><textarea className="min-h-48 w-full rounded-md border bg-background p-3 font-mono text-sm" aria-label={q.text} value={text} disabled={busy || expired} onChange={e => { fileSequence.current++; setFileReading(false); setText(e.target.value); setFileError(false) }} />{fileError && <p role="alert">{t("ociManager.fileError")}</p>}</>
      : ["ask", "password"].includes(q.kind) ? <><Input aria-label={q.text} autoComplete="off" type={q.kind === "password" ? "password" : "text"} value={text} disabled={busy || expired} onChange={e => { setText(e.target.value); setRepeated("") }} />
        {q.kind === "password" && text !== "" && <><label className="block text-sm">{t("ociManager.repeatPassword")}<Input autoComplete="off" type="password" value={repeated} disabled={busy || expired} onChange={e => setRepeated(e.target.value)} /></label>
          {repeated !== "" && text !== repeated && <p role="alert" className="text-sm">{t("ociManager.passwordMismatch")}</p>}</>}
      </> : null}
      <div className="flex flex-wrap gap-2">
        {q.back && <Button variant="outline" disabled={waiting || expired} onClick={() => void answer({ back: true })}>{t("ociManager.back")}</Button>}
        {["confirm", "review"].includes(q.kind) ? <><Button variant={booleanDefault && !q.default ? "outline" : "default"} disabled={waiting || expired} onClick={() => void answer(true)}>{t(q.execution ? "ociManager.confirm" : "ociManager.yes")}</Button><Button variant={booleanDefault && !q.default ? "default" : "outline"} disabled={waiting || expired} onClick={() => void answer(false)}>{t("ociManager.no")}</Button></>
        : <Button disabled={waiting || expired || passwordInvalid || (q.kind === "multiline" && (fileError || text.includes("\0") || new TextEncoder().encode(text).length > q.limit_bytes!))} onClick={() => void answer(q.kind === "message" ? true : q.kind === "checklist" ? selected : text || q.default || "")}>{t("ociManager.next")}</Button>}
        <Button variant="ghost" disabled={waiting || expired} onClick={() => void answer(null, true)}>{t("ociManager.cancel")}</Button>
      </div>
      {expired && <p role="alert" className="text-sm">{t("ociManager.expired")}</p>}
    </div> : job && <p aria-live="polite">{job.progress || t(["starting", "configuring"].includes(job.status) ? "ociManager.preparing" : "ociManager.working")}</p>}
    {job && (error || expired) && <Button variant="outline" onClick={() => void refresh()}>{t("ociManager.refresh")}</Button>}
    {job && !managerFinal.has(job.status) && <p className="text-xs text-muted-foreground">{t("ociManager.background")}</p>}
  </div>
}
