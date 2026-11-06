import { z } from "zod"

const nullableNumber = z.number().finite().nullable()
const disk = z.object({ storage: z.string(), size_gib: z.number().finite(), acl: z.boolean().nullable(), quota: z.boolean(), replicate: z.boolean(), mount_options: z.array(z.string()) }).strict()
const mount = disk.extend({ size_gib: nullableNumber, id: z.number().int(), kind: z.enum(["volume", "bind"]), path_id: z.string(), target: z.string(), ro: z.boolean(), backup: z.boolean(), keepattrs: z.boolean() }).strict()
const address = z.object({ mode: z.enum(["none", "static", "dhcp", "auto"]), address: z.string(), gateway: z.string() }).strict()
const network = z.object({ name: z.string(), hwaddr: z.string(), bridge: z.string(), tag: nullableNumber, firewall: z.boolean(), ipv4: address, ipv6: address, link_down: z.boolean(), mtu: nullableNumber, rate: nullableNumber, host_managed: z.boolean() }).strict()
export const draftSchema = z.object({
  node: z.string(), vmid: nullableNumber, hostname: z.string(), unprivileged: z.boolean(), nesting: z.boolean(), pool: z.string(), ha_managed: z.boolean(), tags: z.array(z.string()), password_set: z.boolean(), ssh_public_keys: z.string(), template: z.string(), rootfs: disk, mounts: z.array(mount),
  cpu: z.object({ cores: nullableNumber, limit: nullableNumber, units: nullableNumber }).strict(),
  memory: z.object({ memory: z.number().finite(), swap: z.number().finite() }).strict(), network: network.nullable(),
  dns: z.object({ nameservers: z.array(z.string()), searchdomains: z.array(z.string()) }).strict(),
  onboot: z.boolean(), startup: z.object({ order: nullableNumber, up: nullableNumber, down: nullableNumber }).strict(), start: z.boolean(),
  firewall: z.object({ mode: z.enum(["inherit", "ssh_only"]), ssh_sources: z.array(z.string()), log: z.boolean() }).strict(), devices: z.array(z.never()),
}).strict()
export type NativeDraft = z.infer<typeof draftSchema>
export type NativeMount = NativeDraft["mounts"][number]
export type NativeNetwork = NonNullable<NativeDraft["network"]>
export const contextSchema = z.object({
  schema: z.literal("native.lxc.context.v1"), state: z.literal("available"), node: z.string().min(1), arch: z.enum(["amd64", "arm64"]), revision: z.string().regex(/^[a-f0-9]{64}$/), observed_at: z.number().finite(), reserved: z.literal(false),
  cgroup: z.union([z.literal(1), z.literal(2)]), version: z.record(z.unknown()),
  storages: z.array(z.object({ id: z.string(), content: z.array(z.string()), active: z.boolean(), available: nullableNumber, type: z.string(), acl: z.boolean(), acl_disable: z.boolean().default(false), quota: z.boolean() })),
  templates: z.array(z.object({ id: z.string(), name: z.string(), storage: z.string(), arch: z.string().nullable(), downloaded: z.boolean() })),
  bridges: z.array(z.object({ id: z.string(), vlan_aware: z.boolean(), active: z.boolean() })), pools: z.array(z.string()), pool_state: z.enum(["available", "unavailable"]),
  capabilities: z.object({ ha: z.boolean(), host_managed: z.boolean(), keepattrs: z.boolean(), firewall_active: z.boolean(), devices: z.boolean(), bind_mounts: z.boolean() }),
  ha_readiness: z.object({ state: z.enum(["ready", "unavailable", "unknown", "unsupported"]), reason: z.enum(["ready", "services_inactive", "no_quorum", "unverified", "unsupported"]) }).default({ state: "unknown", reason: "unverified" }),
  bind_paths: z.record(z.object({ path: z.string(), state: z.string(), permissions_verified: z.boolean() })), occupied_ids: z.array(z.number().int()), nextid: z.number().int(), can_execute: z.boolean(), can_download: z.boolean(), defaults: draftSchema,
  write_policy: z.object({ state: z.enum(["enabled", "disabled", "unavailable", "legacy_pilot"]), reason: z.string(), revision: z.string().regex(/^[a-f0-9]{64}$/).nullable() }).optional(),
})
export type NativeContext = z.infer<typeof contextSchema>
export function haReady(context: NativeContext): boolean {
  return context.capabilities.ha && context.ha_readiness.state === "ready" && context.ha_readiness.reason === "ready"
}
export const planSchema = z.object({
  policy_revision: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  schema: z.literal("native.lxc.plan.v1"), node: z.string(), vmid: z.number().int(), create: z.record(z.union([z.string(), z.number().finite()])), final_config: z.record(z.union([z.string(), z.number().finite()])), start: z.boolean(), ha_managed: z.boolean(), warnings: z.array(z.enum(["privileged", "bind_backup", "firewall_restricts"])),
  access: z.object({ password_set: z.boolean(), ssh_key_count: z.number().int(), ssh_keys_sha256: z.string() }), firewall: z.object({ mode: z.enum(["inherit", "ssh_only"]), rules: z.array(z.record(z.union([z.string(), z.number()]))), options: z.record(z.union([z.string(), z.number()])) }),
  context_revision: z.string(), reserved: z.literal(false), plan_hash: z.string().regex(/^[a-f0-9]{64}$/),
}).refine(p => !Object.keys(p.create).some(k => /password|ssh-public-keys|force|restore/i.test(k)), "Unsafe plan")
export type NativePlan = z.infer<typeof planSchema>
export const checkSchema = z.object({ plan: planSchema, check: z.object({ owner: z.string(), plan_hash: z.string(), expires_at: z.number().finite(), signature: z.string().regex(/^[a-f0-9]{64}$/) }).strict(), expires_at: z.number().finite(), server_time: z.number().finite(), can_execute: z.boolean() })
export type NativeCheck = z.infer<typeof checkSchema>
export const jobSchema = z.object({
  task_warnings: z.array(z.object({ phase: z.string(), upid: z.string(), exitstatus: z.string().regex(/^WARNINGS: [0-9]+$(?![\s\S])/) })).default([]),
  id: z.string().regex(/^[a-f0-9]{32}$/), intent: z.string().regex(/^[a-f0-9]{32}$/), node: z.string(), vmid: z.number().int(),
  status: z.enum(["queued", "creating", "configuring", "starting", "succeeded", "failed_before_effect", "partial", "unknown", "needs_recovery"]), phase: z.string(), upid: z.string().nullable(), error: z.string().nullable(), plan: planSchema, created_at: z.number().finite(), updated_at: z.number().finite(), result: z.object({ node: z.string(), vmid: z.number().int(), status: z.enum(["running", "stopped"]) }).nullable(),
})
export type NativeJob = z.infer<typeof jobSchema>
export const nativeSteps = ["general", "template", "disks", "cpu", "memory", "network", "dns", "confirm"] as const
export const nativeFinal = new Set(["succeeded", "failed_before_effect", "partial", "unknown", "needs_recovery"])

export function newMount(id: number): NativeMount {
  return { id, kind: "volume", storage: "", size_gib: 8, path_id: "", target: "/data" + (id || ""), ro: false, backup: true, acl: null, quota: false, replicate: true, keepattrs: false, mount_options: [] }
}

export function checkMatches(check: NativeCheck, draft: NativeDraft, context: NativeContext): boolean {
  return check.plan.node === draft.node && check.plan.vmid === draft.vmid && check.plan.context_revision === context.revision && check.check.plan_hash === check.plan.plan_hash && check.check.expires_at === check.expires_at && check.expires_at > check.server_time
}
