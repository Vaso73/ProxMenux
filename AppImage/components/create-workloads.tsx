"use client"

import { useEffect, useRef, useState } from "react"
import useSWR from "swr"
import { ArrowLeft, ExternalLink, Grid3x3, Plus, ShieldAlert } from "lucide-react"
import { fetchApi } from "../lib/api-config"
import { useT } from "../lib/i18n/provider"
import { Button } from "./ui/button"
import { Card, CardHeader, CardTitle } from "./ui/card"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "./ui/dialog"
import { Input } from "./ui/input"
import { Label } from "./ui/label"
import { OciManagerOperation } from "./oci-manager-operation"
import { NativeLxcWizard } from "./native-lxc-wizard"
import { createStatusText } from "./create-status"
import { managerFinal } from "../lib/oci-manager"

import { CreateCatalogBrowser, categoryText, methodText, type CatalogApp, type CatalogResponse, type CatalogSource } from "./create-catalog-browser"

const fetcher = (url: string) => fetchApi<CatalogResponse>(url)
function appRequirements(app: CatalogApp) {
  return [
    ["privilegedLxc", app.requires_privileged_lxc],
    ["hostPidNamespace", app.requires_host_pid_namespace],
    ["relaxedConfinement", app.requires_relaxed_confinement],
    ["securityConfirmation", app.requires_security_confirmation],
  ] as const
}

export function CreateWorkloads({ catalogMode = false, catalogSource, onCatalogSourceChange }: {
  catalogMode?: boolean; catalogSource?: CatalogSource; onCatalogSourceChange?: (source: CatalogSource) => void
} = {}) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState(0)
  const [custom, setCustom] = useState(false)
  const [preparingCustom, setPreparingCustom] = useState(false)
  const [nativeVisited, setNativeVisited] = useState(false)
  const [customSource, setCustomSource] = useState<"image" | "paste" | "upload">("image")
  const [catalogRun, setCatalogRun] = useState<{ id: string; app_id: string; mode: string; title: string } | null>(null)
  const [catalogStatus, setCatalogStatus] = useState<string | null>(null)
  const [customStatus, setCustomStatus] = useState<string | null>(null)
  const [method, setMethod] = useState<"choose" | "apps" | "native">("choose")
  const [engineMode, setEngineMode] = useState("default")
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [instanceLabel, setInstanceLabel] = useState("")
  const [planNotes, setPlanNotes] = useState("")
  const bodyRef = useRef<HTMLDivElement>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const cardFocusRef = useRef<HTMLElement | null>(null)
  const { data, error } = useSWR<CatalogResponse>(catalogMode || (open && ((method === "apps" && !custom) || catalogSource === "community")) ? "/api/create/catalog/installation" : null, fetcher, {
    revalidateOnFocus: false,
    errorRetryCount: 1,
  })
  const unavailable = Boolean(error || (data && !data.success))
  const fixedSource = catalogMode && catalogSource && catalogSource !== "all" ? catalogSource : undefined
  const apps = data?.success && Array.isArray(data.apps) && !unavailable ? data.apps : []
  const selectedApp = apps.find(app => app.id === selectedId && (!fixedSource || app.provider === fixedSource) && data?.sources?.[app.provider]?.available !== false)
  const engineFlow = custom || Boolean(catalogRun) || selectedApp?.provider === "oci"
  const currentStep = custom || catalogRun ? 2 : selectedApp ? Math.min(step, 2) : 0
  const stepKeys = ["select", "details", engineFlow ? "configure" : "prepare"]
  const stepTitle = t("create.wizard.steps." + stepKeys[currentStep])
  const restartCatalog = catalogStatus === "cancelled" || catalogStatus === "expired"
  const restartCustom = customStatus === "cancelled" || customStatus === "expired"

  useEffect(() => {
    if (!open) return
    if (bodyRef.current) bodyRef.current.scrollTop = 0
    if (currentStep > 0) headingRef.current?.focus()
  }, [open, currentStep])

  const changeOpen = (value: boolean) => {
    setOpen(value)
    if (!value) {
      setStep(0)
      setCustom(false)
      setPreparingCustom(false)
      setNativeVisited(false); setCatalogRun(null); setCustomSource("image")
      setCatalogStatus(null); setCustomStatus(null)
      setMethod("choose")
      setEngineMode("default")
      setSelectedId(null)
      setInstanceLabel("")
      setPlanNotes("")
    }
  }
  const home = () => { setMethod("choose"); setCustom(false) }
  const newCatalogPreparation = () => {
    setCatalogRun(null); setCatalogStatus(null); setSelectedId(null); setEngineMode("default")
    setStep(0); setInstanceLabel(""); setPlanNotes("")
  }
  const newCustomPreparation = () => {
    setPreparingCustom(false); setCustomStatus(null); setCustomSource("image")
  }
  const selectApp = (app: CatalogApp) => {
    if (catalogRun) { setMethod("apps"); setCustom(false); setStep(2); return }
    setMethod("apps")
    setCustom(false)
    if (selectedId !== app.id) {
      setInstanceLabel("")
      setPlanNotes("")
    }
    setSelectedId(app.id)
  }

  const templateTone = selectedApp?.template_status === "laboratory-validated" ? "success" : selectedApp?.template_status?.startsWith("generated-") || selectedApp?.provider === "community" ? "warning" : "neutral"
  const createTrigger = <DialogTrigger asChild>
        <Button type="button" size="sm" className="shrink-0 gap-1.5" onClick={() => { cardFocusRef.current = null }}>
          <Plus className="h-4 w-4" />{t(fixedSource === "community" ? "create.wizard.open" : "create.native.entry")}
        </Button>
      </DialogTrigger>

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      {!catalogMode && createTrigger}
      {catalogMode && <Card className="bg-card border-border" data-catalog-panel><CreateCatalogBrowser data={data} error={error} source={catalogSource} fixedSource={fixedSource} onSourceChange={onCatalogSourceChange} renderHeader={controls => <CardHeader className="flex flex-col gap-3">
        <div className="flex w-full flex-col gap-3 md:flex-row md:flex-wrap md:items-center md:justify-between">
          <CardTitle className="flex items-center gap-2 text-xl lg:text-2xl font-bold text-foreground"><Grid3x3 className="h-6 w-6 shrink-0" />{t(fixedSource ? "create.unified.views." + fixedSource + ".title" : "create.unified.catalogTitle")}</CardTitle>
          <div className="flex w-full max-w-full flex-col gap-2 md:ml-auto md:w-auto md:flex-row md:flex-wrap md:items-center">{createTrigger}{controls}</div>
        </div>
        <p className="text-sm text-muted-foreground">{t(fixedSource ? "create.unified.views." + fixedSource + ".description" : "create.unified.catalogDescription")}</p>
      </CardHeader>} onSelect={app => {
        cardFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
        selectApp(app); setStep(1); setOpen(true)
      }} /></Card>}
      <DialogContent onCloseAutoFocus={event => {
        if (cardFocusRef.current?.isConnected) { event.preventDefault(); cardFocusRef.current.focus() }
      }} className={"flex max-h-[100dvh] w-full max-w-none flex-col gap-0 overflow-hidden rounded-none p-0 sm:w-[94vw] sm:rounded-xl h-[100dvh] sm:h-auto sm:max-h-[min(86dvh,700px)] " + (method === "apps" && !custom ? "sm:w-[94vw] sm:max-w-5xl" : "sm:max-w-4xl")}>
        <DialogHeader className="shrink-0 border-b px-4 py-2.5 pr-12 text-left">
          <DialogTitle>{t(fixedSource === "community" ? "create.wizard.title" : "create.native.entry")}</DialogTitle>
          <DialogDescription className={method === "choose" ? undefined : "sr-only"}>{t(method === "native" ? "create.native.description" : method === "choose" ? "create.native.choiceDescription" : custom ? "create.engine.custom.description" : fixedSource === "community" ? "create.wizard.description" : "create.engine.description")}</DialogDescription>
          {!custom && method === "apps" && <>
          <ol aria-label={t("create.wizard.stepsLabel")} className="flex gap-2 pt-1 text-xs">
            {stepKeys.map((key, index) => (
              <li key={key} aria-current={currentStep === index ? "step" : undefined}
                className={"min-w-0 flex-1 rounded px-2 py-1.5 " + (currentStep === index ? "bg-secondary font-semibold " + createStatusText.info : "text-muted-foreground")}>
                <span className="mr-1">{index + 1}.</span>
                <span className={currentStep === index ? "" : "hidden sm:inline"}>{t("create.wizard.steps." + key)}</span>
              </li>
            ))}
          </ol></>}
        </DialogHeader>

        {method !== "native" && method !== "choose" && <div className="shrink-0 border-b px-3 py-1"><Button type="button" size="sm" variant="ghost" onClick={home}><ArrowLeft className="mr-1.5 h-4 w-4" aria-hidden="true" />{t("create.native.backToChoice")}</Button></div>}
        <div ref={bodyRef} data-create-wizard-body className={method === "native" ? "flex min-h-0 flex-1 flex-col overflow-hidden" : "min-h-0 flex-1 overflow-y-auto overscroll-contain p-3 sm:max-h-[calc(min(86dvh,700px)-150px)]"}>
          {method === "choose" && <div className="grid gap-3 sm:grid-cols-3" data-create-method-choice>
            {(["apps", "native", "custom"] as const).map(choice => <Button key={choice} type="button" variant="outline" className="h-auto min-w-0 flex-col items-start gap-2 whitespace-normal p-4 text-left" data-create-method={choice} onClick={() => { if (choice === "apps" && restartCatalog) newCatalogPreparation(); if (choice === "custom" && restartCustom) newCustomPreparation(); setMethod(choice === "native" ? "native" : "apps"); if (choice === "native") setNativeVisited(true); setCustom(choice === "custom") }}><span className="font-medium">{t("create.native.choices." + choice + ".title")}</span>{choice === "custom" && <span className="rounded border px-1.5 text-[11px] font-normal text-muted-foreground">{t("create.friendly.advanced")}</span>}<span className="text-xs font-normal text-muted-foreground">{t("create.native.choices." + choice + ".description")}</span>{((choice === "custom" && preparingCustom && !managerFinal.has(customStatus || "")) || (choice === "apps" && catalogRun && !managerFinal.has(catalogStatus || ""))) && <span className="text-xs text-blue-600 dark:text-blue-400">{t("create.friendly.resumePreparation")}</span>}</Button>)}
          </div>}
          {nativeVisited && <div hidden={method !== "native"} className={method === "native" ? "flex min-h-0 flex-1 flex-col" : "hidden"}><NativeLxcWizard active={method === "native"} onBack={home} /></div>}
          {method === "apps" && <h2 ref={headingRef} tabIndex={-1} className="mb-2 text-base font-semibold outline-none">{custom ? t("create.native.choices.custom.title") : stepTitle}</h2>}
          <div hidden={custom || currentStep !== 0 || method !== "apps"}>
            <CreateCatalogBrowser data={data} error={error} selectedId={selectedId} onSelect={selectApp} source={catalogSource} fixedSource={fixedSource} providerOnly onSourceChange={onCatalogSourceChange} />
          </div>
          <div hidden={!custom || method !== "apps"} className={custom && method === "apps" ? "space-y-3" : "hidden"}>
            {preparingCustom ? <><OciManagerOperation operation="custom.install" parameters={{}} active={custom && method === "apps"} initialSource={customSource === "image" ? "image" : "paste"} onStatusChange={setCustomStatus} />{restartCustom && <Button size="sm" onClick={newCustomPreparation}>{t("create.friendly.newPreparation")}</Button>}</> : <div className="space-y-3">
              <p className="text-sm text-muted-foreground">{t("create.friendly.customHint")}</p>
              <div className="grid gap-2 sm:grid-cols-3">{(["image", "paste", "upload"] as const).map(source => <Button key={source} type="button" variant="outline" data-custom-source={source} aria-pressed={customSource === source} onClick={() => setCustomSource(source)} className={"h-auto whitespace-normal p-3 " + (customSource === source ? "border-blue-500 bg-blue-500/10" : "")}>{t("create.friendly.sources." + source)}</Button>)}</div>
              <p className="text-xs text-muted-foreground">{t("create.friendly.customPreparation")}</p>
              <Button size="sm" onClick={() => setPreparingCustom(true)}>{t("create.wizard.next")}</Button>
            </div>}
          </div>
          {catalogRun && <div hidden={method !== "apps" || custom || currentStep !== 2} className={method === "apps" && !custom && currentStep === 2 ? "space-y-3" : "hidden"}><h3 className="font-semibold">{catalogRun.title}</h3><OciManagerOperation operation="catalog.install" parameters={{ app_id: catalogRun.app_id, mode: catalogRun.mode }} active={method === "apps" && !custom && currentStep === 2} onStatusChange={setCatalogStatus} />{restartCatalog && <Button size="sm" onClick={newCatalogPreparation}>{t("create.friendly.newPreparation")}</Button>}</div>}
          {method === "apps" && !custom && selectedApp && currentStep > 0 && (currentStep === 1 ? (
            <div className="space-y-3">
              <div className="flex items-start gap-3">
                {selectedApp.icon && <img src={selectedApp.icon} alt="" className="h-12 w-12 shrink-0 rounded object-contain" />}
                <div className="min-w-0"><h3 className="break-words text-xl font-semibold">{selectedApp.title}</h3>
                  <p className="text-sm text-muted-foreground">{categoryText(selectedApp, t)}</p><p className="mt-1 text-sm font-medium text-blue-600 dark:text-blue-400">{methodText(selectedApp)}</p></div>
              </div>
              <div className="text-sm text-muted-foreground"><p className="line-clamp-2">{selectedApp.description}</p>
                <details className="mt-1"><summary className="cursor-pointer font-medium text-foreground">{t("create.compact.fullDescription")}</summary><p className="mt-2">{selectedApp.description}</p></details>
              </div>
              {selectedApp.development && <p className="text-sm text-amber-700 dark:text-amber-400">{t("create.unified.development")}</p>}
              <dl className="grid gap-3 rounded-lg border p-3 text-sm sm:grid-cols-2">
                <div><dt className="text-muted-foreground">{t("create.details.architectures")}</dt>
                  <dd className="mt-1 font-medium">{selectedApp.architectures.join(", ") || t("create.details.notSpecified")}</dd></div>
                <div><dt className="text-muted-foreground">{t("create.details.templateStatus")}</dt>
                  <dd data-create-tone={templateTone} className={"mt-1 font-medium " + createStatusText[templateTone]}>{t(selectedApp.template_status === "laboratory-validated" ? "create.details.status.laboratoryValidated"
                    : selectedApp.template_status === "generated-unvalidated" ? "create.details.status.unvalidated"
                    : selectedApp.template_status === "generated-review-required" ? "create.details.status.reviewRequired" : selectedApp.provider === "community" ? "create.unified.communityStatus" : "create.details.notSpecified")}</dd></div>
              </dl>
              <div className="rounded-lg border p-3">
                <h3 className="mb-2 font-medium">{t("create.details.requirements")}</h3>
                {appRequirements(selectedApp).some(([, required]) => required) ? (
                  <ul className="space-y-2 text-sm">{appRequirements(selectedApp).filter(([, required]) => required).map(([key]) => (
                    <li key={key} className="flex gap-2"><ShieldAlert className="h-5 w-5 shrink-0 text-amber-600" /><span>{t("create.details.requirement." + key)}</span></li>
                  ))}</ul>
                ) : <p className="text-sm text-muted-foreground">{t(selectedApp.provider === "community" ? "create.unified.requirementsUnknown" : "create.details.noSpecialRequirements")}</p>}
              </div>
              <p className="text-sm text-muted-foreground">{t("create.wizard.catalogBoundary")}</p>
              {selectedApp.provider === "oci" && <label className="block space-y-2 text-sm">{t("ociManager.setupMode")}
                <select className="w-full rounded-md border bg-background p-2" value={engineMode} onChange={e => setEngineMode(e.target.value)}>
                  <option value="default">{t("ociManager.recommended")}</option><option value="advanced">{t("ociManager.advanced")}</option>
                </select></label>}
              {selectedApp.website && <a href={selectedApp.website} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm text-blue-600 dark:text-blue-400">
                {t("create.catalog.website")}<ExternalLink className="h-3 w-3" /></a>}
            </div>
          ) : (
            engineFlow && selectedApp ? null : <div className="space-y-5">
              <div className="rounded-lg border border-blue-500/30 bg-blue-500/5 p-4 text-sm">
                <p className="font-medium">{t("create.prepare.planOnlyTitle")}</p>
                <p className="mt-1 text-muted-foreground">{t("create.prepare.planOnlyDescription")}</p>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="min-w-0 space-y-2">
                  <Label htmlFor="create-instance-label">{t("create.prepare.instanceLabel")}</Label>
                  <Input id="create-instance-label" maxLength={100} value={instanceLabel}
                    onChange={event => setInstanceLabel(event.target.value)} placeholder={t("create.prepare.instancePlaceholder")} />
                </div>
                <div className="min-w-0 space-y-2">
                  <Label htmlFor="create-plan-notes">{t("create.prepare.notesLabel")}</Label>
                  <Input id="create-plan-notes" maxLength={500} value={planNotes}
                    onChange={event => setPlanNotes(event.target.value)} placeholder={t("create.prepare.notesPlaceholder")} />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">{t("create.prepare.inputsDescription")}</p>
              <div className="rounded-lg border bg-muted/30 p-4">
                <h3 className="mb-3 font-medium">{t("create.wizard.summary")}</h3>
                <dl className="grid gap-4 text-sm sm:grid-cols-2">
                  <div><dt className="text-muted-foreground">{t("create.prepare.app")}</dt><dd className="break-words font-medium">{selectedApp.title}</dd></div>
                  <div><dt className="text-muted-foreground">{t("create.unified.method")}</dt><dd className="break-words font-medium">{methodText(selectedApp)}</dd></div>
                  <div><dt className="text-muted-foreground">{t("create.prepare.architectures")}</dt><dd className="font-medium">{selectedApp.architectures.join(", ") || t("create.details.notSpecified")}</dd></div>
                  <div className="min-w-0"><dt className="text-muted-foreground">{t("create.prepare.instanceLabel")}</dt><dd className="break-all font-medium">{instanceLabel || t("create.prepare.notSet")}</dd></div>
                  <div className="min-w-0"><dt className="text-muted-foreground">{t("create.prepare.notesLabel")}</dt><dd className="break-all font-medium">{planNotes || t("create.prepare.notSet")}</dd></div>
                </dl>
              </div>
              <p className="text-sm text-muted-foreground">{t("create.prepare.summaryBoundary")}</p>
            </div>
          ))}
        </div>

        {method !== "native" && <footer className="flex shrink-0 items-center justify-between gap-2 border-t bg-card px-3 py-2">
          <Button type="button" size="sm" variant="ghost" onClick={() => changeOpen(false)}>{t("actions.close")}</Button>
          {method === "apps" && !custom && <div className="ml-auto flex gap-2">
            {currentStep > 0 && !(engineFlow && currentStep === 2) && <Button type="button" size="sm" variant="outline" onClick={() => setStep(currentStep - 1)}>{t("create.wizard.back")}</Button>}
            {currentStep < stepKeys.length - 1 ? <Button type="button" size="sm" disabled={!selectedApp} onClick={() => { if (currentStep === 1 && selectedApp?.provider === "oci" && !catalogRun) setCatalogRun({ id: selectedApp.id, app_id: selectedApp.source_id, mode: engineMode, title: selectedApp.title }); setStep(currentStep + 1) }}>{t("create.wizard.next")}</Button>
              : null}
          </div>}
        </footer>}
      </DialogContent>
    </Dialog>
  )
}
