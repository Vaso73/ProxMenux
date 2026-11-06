"use client"

import { useEffect, useMemo, useState, type ReactNode } from "react"
import { AlertTriangle, Check, ChevronLeft, ChevronRight, Layers3, Search } from "lucide-react"
import { useT } from "../lib/i18n/provider"
import { Button } from "./ui/button"
import { Input } from "./ui/input"

export type CatalogApp = {
  id: string; source_id: string; provider: "oci" | "community"; workload_type: "lxc" | "vm"
  variant: string; development: boolean; title: string; description: string; website: string; icon: string
  category: string; category_label: string; architectures: string[]; template_status: string
  requires_privileged_lxc: boolean; requires_host_pid_namespace: boolean
  requires_relaxed_confinement: boolean; requires_security_confirmation: boolean
}
export type CatalogResponse = {
  success: boolean; apps: CatalogApp[]
  sources: Record<"oci" | "community", { available: boolean; count: number | null; origin: string }>
}
const CATEGORY_IDS = new Set([
  "adblock", "ai", "arr", "automation", "backup", "business", "communication", "containers",
  "creative", "databases", "desktop", "documents", "downloads", "finance", "gaming",
  "management", "media", "messaging", "misc", "monitoring", "network", "nvr",
  "productivity", "remote", "security", "sharing", "smarthome", "tools", "web", "dashboards", "os", "smartdevices",
])
export function categoryText(app: CatalogApp, t: ReturnType<typeof useT>) {
  return CATEGORY_IDS.has(app.category) ? t("create.wizard.categories." + app.category) : app.category_label || app.category
}
export function methodText(app: CatalogApp) {
  return (app.provider === "oci" ? "OCI" : "Community Scripts") + " · " + app.workload_type.toUpperCase() + (app.variant ? " · " + app.variant : "")
}

export type CatalogSource = "all" | "oci" | "community"

function CatalogIcon({ src }: { src: string }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => { setFailed(false) }, [src])
  return src && !failed ? <img src={src} alt="" onError={() => setFailed(true)} className="h-8 w-8 shrink-0 rounded object-contain" />
    : <Layers3 className="h-8 w-8 shrink-0 text-muted-foreground" aria-hidden="true" />
}

export function CreateCatalogBrowser({ data, error, selectedId, onSelect, source: controlledSource, onSourceChange, fixedSource, providerOnly = false, renderHeader }: {
  data?: CatalogResponse; error?: unknown; selectedId?: string | null; onSelect: (app: CatalogApp) => void
  source?: CatalogSource; onSourceChange?: (source: CatalogSource) => void
  fixedSource?: Exclude<CatalogSource, "all">
  providerOnly?: boolean
  renderHeader?: (controls: ReactNode) => ReactNode
}) {
  const t = useT()
  const [query, setQuery] = useState("")
  const [localSource, setSource] = useState<CatalogSource>(providerOnly ? "oci" : "all")
  const source = fixedSource ?? (providerOnly && controlledSource === "all" ? localSource : controlledSource) ?? localSource
  const [category, setCategory] = useState("__all__")
  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1); setCategory("__all__") }, [source])
  const unavailable = Boolean(error || (data && !data.success))
  const apps = data?.success && !unavailable && Array.isArray(data.apps) ? data.apps : []
  const scopedApps = apps.filter(app => (source === "all" || source === app.provider) && data?.sources?.[app.provider]?.available !== false)
  const categories = Array.from(new Map(scopedApps.filter(app => app.category).map(app => [app.category, categoryText(app, t)])).entries())
    .sort((a, b) => a[1].localeCompare(b[1]))
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    return scopedApps.filter(app => (category === "__all__" || category === app.category) &&
      (!needle || (app.title + " " + app.description + " " + categoryText(app, t) + " " + methodText(app)).toLocaleLowerCase().includes(needle)))
  }, [scopedApps, query, category, t])
  const totalPages = Math.max(1, Math.ceil(filtered.length / 12))
  const currentPage = Math.min(page, totalPages)
  const selected = (fixedSource ? scopedApps : apps).find(app => app.id === selectedId)
  const visible = filtered.slice((currentPage - 1) * 12, currentPage * 12)
  const controls = <>
      <label className={renderHeader ? "relative w-full md:w-72" : "relative"}><span className="sr-only">{t("create.catalog.searchLabel")}</span>
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input inputMode="search" value={query} onChange={event => { setQuery(event.target.value); setPage(1) }} placeholder={t("create.catalog.searchPlaceholder")} className={renderHeader ? "h-9 border-border bg-card pl-8 focus-visible:ring-1" : "pl-9"} />
      </label>
      <label className={renderHeader ? "w-full md:w-60" : ""}><span className="sr-only">{t("create.catalog.categoryLabel")}</span>
        <select value={category} onChange={event => { setCategory(event.target.value); setPage(1) }} className={renderHeader ? "h-9 w-full rounded-md border border-border bg-card px-3 text-sm focus:outline-none focus:ring-1 focus:ring-ring" : "h-10 w-full rounded-md border border-input bg-background px-3 text-sm"}>
          <option value="__all__">{t("create.catalog.allCategories")}</option>
          {categories.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </label>
    </>
  return <div>
    {renderHeader?.(controls)}
    <div className={renderHeader ? "space-y-4 px-6 pb-6" : "space-y-4"}>
    {!fixedSource && <div role="group" aria-label={t("create.unified.sourceFilter")} className={"grid w-full gap-2 " + (providerOnly ? "grid-cols-2" : "grid-cols-3")}>
      {(providerOnly ? ["oci", "community"] as const : ["all", "oci", "community"] as const).map(value => <Button key={value} type="button" size="sm"
        variant="outline" aria-pressed={source === value}
        className={["h-auto min-h-12 w-full min-w-0 whitespace-normal rounded-lg px-2 py-2 text-center leading-tight transition-colors focus-visible:ring-blue-500",
          source === value ? "border-blue-500 bg-blue-500/15 font-semibold text-blue-700 ring-1 ring-inset ring-blue-500/30 hover:bg-blue-500/20 dark:text-blue-300"
            : "border-blue-500/20 bg-blue-500/5 text-foreground hover:border-blue-500/40 hover:bg-blue-500/10"].join(" ")}
        onClick={() => { setSource(value); onSourceChange?.(value); setPage(1) }}>{t("create.unified.sources." + value)}</Button>)}
    </div>}
    {!renderHeader && <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_240px]">{controls}</div>}
    {!unavailable && data && (["oci", "community"] as const).filter(provider => (source === "all" || source === provider) && data.sources?.[provider]?.available === false).map(provider => (
      <p role="alert" key={provider} className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
        {t(fixedSource ? "create.unified.fixedUnavailable" : "create.unified.sourceUnavailable", { source: t("create.unified.sources." + provider) })}
      </p>
    ))}
    {selected && <p className="text-sm font-medium text-blue-600 dark:text-blue-400">{t("create.wizard.selected", { appName: selected.title + " · " + methodText(selected) })}</p>}
    {unavailable ? <div role="alert" className="flex gap-3 rounded-lg border border-destructive/30 p-4 text-sm text-destructive">
      <AlertTriangle className="h-5 w-5 shrink-0" /><div><h3 className="font-medium">{t("create.catalog.unavailableTitle")}</h3><p>{t("create.catalog.unavailable")}</p></div>
    </div> : !data ? <p role="status">{t("create.catalog.loading")}</p> : filtered.length === 0 ? <p role="status">{t("create.catalog.noResults")}</p> : <>
      <p role="status" className="text-xs text-muted-foreground">{t("create.unified.range", { start: (currentPage - 1) * 12 + 1, end: Math.min(currentPage * 12, filtered.length), count: filtered.length })}</p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {visible.map(app => <Button key={app.id} type="button" variant="outline" aria-label={app.title + " · " + methodText(app)}
          data-catalog-option="true" aria-pressed={app.id === selectedId} onClick={() => onSelect(app)}
          className={["h-auto min-w-0 flex-col items-stretch justify-start whitespace-normal p-3 text-left", app.id === selectedId ? "border-blue-500 bg-blue-500/10 ring-1 ring-blue-500" : ""].join(" ")}>
          <span className="flex items-start gap-2">
            <CatalogIcon src={app.icon} />
            <span className="min-w-0 flex-1"><span className="block truncate font-semibold">{app.title}</span><span className="block truncate text-xs font-normal text-muted-foreground">{categoryText(app, t)}</span></span>
            {app.id === selectedId && <span className="flex shrink-0 items-center gap-1 text-xs text-blue-600 dark:text-blue-400"><Check className="h-4 w-4" aria-hidden="true" />{t("create.friendly.selected")}</span>}
          </span>
          <span className="mt-2 flex flex-wrap items-center gap-1.5 text-xs font-medium"><span className="rounded border px-1.5 py-0.5 text-blue-600 dark:text-blue-400">{app.provider === "oci" ? "OCI" : "Community Scripts"}</span><span className="text-muted-foreground">{app.workload_type.toUpperCase()}{app.variant ? " · " + app.variant : ""}</span></span>
          {app.development && <span className="mt-1 text-xs text-amber-700 dark:text-amber-400">{t("create.unified.development")}</span>}
          {app.description && <span className="mt-2 line-clamp-2 text-xs font-normal text-muted-foreground">{app.description}</span>}
        </Button>)}
      </div>
      {totalPages > 1 && <nav aria-label={t("create.wizard.pagination")} className="flex items-center justify-center gap-3">
        <Button type="button" variant="outline" size="icon" aria-label={t("create.wizard.previousPage")} disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}><ChevronLeft className="h-4 w-4" /></Button>
        <span className="text-xs">{t("create.wizard.page", { current: currentPage, total: totalPages })}</span>
        <Button type="button" variant="outline" size="icon" aria-label={t("create.wizard.nextPage")} disabled={currentPage === totalPages} onClick={() => setPage(currentPage + 1)}><ChevronRight className="h-4 w-4" /></Button>
      </nav>}
    </>}
    <p className="text-xs text-muted-foreground">{t(source === "oci" ? "create.engine.catalogNote" : "create.unified.snapshot")}</p>
    {source !== "oci" && data?.sources?.community?.available && data.sources.community.origin === "bundled" && <p className="text-xs text-muted-foreground">{t("create.unified.bundledSnapshot")}</p>}
    </div>
  </div>
}
