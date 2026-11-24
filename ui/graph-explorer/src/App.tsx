import React, { useEffect, useMemo, useRef, useState } from 'react';
import librarianIcon from '../../../docs/design-system/assets/minimal-icon.png';
import {
  Search, ZoomIn, ZoomOut, RotateCcw, Play, Copy, Check, ChevronDown,
  Activity, Layers, Clock, Shield, Database, ArrowRight, Eye, GitBranch,
  HeartPulse, Table2, Braces,
} from 'lucide-react';

type EntType = 'person'|'project'|'place'|'org'|'tool'|'concept'|'other';
type ClaimStatus = 'grounded'|'unconfirmed'|'retracted'|'orphaned';
type EvState = 'live'|'retracted'|'superseded_version'|'withdrawn';
type EvAct = 'assert'|'retract';

interface AliasDetail {
  surface_norm: string; source: string; state: string;
  span_id?: string|null; event_time?: string|null;
}
interface Passport { span_id: string; surface: string; conf: number }
interface Entity {
  id: string; owner: string; name: string; name_norm?: string; type: EntType;
  type_db?: string; aliases: string[]; alias_detail?: AliasDetail[];
  merged_into?: string|null; has_name_vec?: boolean; passports?: Passport[];
  note: string; raw?: Record<string, unknown>;
}
interface Evidence {
  id: string; span_id?: string; extractor: string; version: string; speaker: string;
  act: EvAct; event_time: string; state: EvState; state_db?: string;
  polarity?: string; subject_surface?: string; object_surface?: string;
  object_key?: string; confidence?: number; note?: string;
}
interface Claim {
  id: string; owner: string; subject_id: string; verb: string;
  object_type: 'entity'|'literal'; object_id?: string; object_literal?: string;
  status: ClaimStatus; fact_text: string; event_date: string; span_id: string;
  span_excerpt?: string; valid_from?: string; valid_to?: string|null;
  superseded_by?: string|null; polarity?: string; uptake?: string;
  has_fact_vec?: boolean; embed_model?: string|null; evidence: Evidence[];
  raw?: Record<string, unknown>;
}
interface Job {
  id: string; kind: string; op_id: string;
  status: 'pending'|'leased'|'done'|'retryable'|'failed';
  fence: string; lease: string; attempts: number; owner: string; last_error: string;
  session_id?: string; claimed_by?: string; priority?: number;
  raw?: Record<string, unknown>;
}
interface Span {
  id: string; session_id: string; role: string; excerpt: string;
  content_len: number; ts?: string|null; drain_status?: string|null;
}
interface SchemaRow { table: string; role: string; keys: string; count: number }
interface Mutation { kind: string; claimId?: string; evidenceId?: string; at: string }

const TYPE_COLOR: Record<EntType, string> = {
  person: 'var(--entity-person)', project: 'var(--entity-project)', place: 'var(--entity-place)',
  org: 'var(--entity-org)', tool: 'var(--entity-tool)', concept: 'var(--entity-concept)',
  other: 'var(--entity-other)',
};
const STATUS_COLOR: Record<ClaimStatus, string> = {
  grounded: 'var(--status-grounded)', unconfirmed: 'var(--status-unconfirmed)',
  retracted: 'var(--status-retracted)', orphaned: 'var(--status-orphaned)',
};
const DEFAULT_FUNCTIONAL = ['based_in','owned_by','works_for','lives_in','role_is','status_is'];

function apiUrl(path: string): string {
  return path.startsWith('/') ? path : `/${path}`;
}

let FUNCTIONAL = [...DEFAULT_FUNCTIONAL];
function clone<T>(x: T): T { return JSON.parse(JSON.stringify(x)); }
function nowISO() { return new Date().toISOString(); }
function liveEvidence(c: Claim) { return c.evidence.filter(e => e.state === 'live'); }
function recomputeClaimStatus(c: Claim): ClaimStatus {
  const live = liveEvidence(c);
  if (live.length === 0) return 'orphaned';
  const asserts = live.filter(e => e.act === 'assert');
  const retracts = live.filter(e => e.act === 'retract');
  if (retracts.length > 0 && asserts.length === 0) return 'retracted';
  if (retracts.length > 0 && asserts.length > 0) {
    const newestAssert = [...asserts].sort((a, b) => a.event_time.localeCompare(b.event_time)).pop()!;
    const newestRetract = [...retracts].sort((a, b) => a.event_time.localeCompare(b.event_time)).pop()!;
    if (newestRetract.event_time > newestAssert.event_time) return 'retracted';
  }
  if (asserts.some(e => e.speaker === 'user')) return 'grounded';
  if (asserts.some(e => e.speaker === 'assistant')) return 'unconfirmed';
  if (asserts.length > 0) return 'grounded';
  return 'orphaned';
}
function rebuildFunctionalTimeline(allClaims: Claim[], owner: string, subject_id: string, verb: string): Claim[] {
  const claims = clone(allClaims);
  const group = claims.filter(c => c.owner === owner && c.subject_id === subject_id && c.verb === verb);
  if (group.length === 0) return claims;
  type Evt = { claim: Claim; time: string; date: string };
  const events: Evt[] = [];
  for (const c of group) {
    const st = recomputeClaimStatus(c);
    if (st === 'retracted' || st === 'orphaned') continue;
    const liveAsserts = c.evidence.filter(e => e.state === 'live' && e.act === 'assert')
      .sort((a, b) => a.event_time.localeCompare(b.event_time));
    if (liveAsserts.length === 0) continue;
    events.push({ claim: c, time: liveAsserts[0].event_time, date: liveAsserts[0].event_time.slice(0, 10) });
  }
  events.sort((a, b) => a.time.localeCompare(b.time) || a.claim.id.localeCompare(b.claim.id));
  const runs: Evt[] = [];
  for (const ev of events) {
    const prev = runs[runs.length - 1];
    const sameObj = prev && prev.claim.object_type === ev.claim.object_type && (
      prev.claim.object_type === 'literal'
        ? prev.claim.object_literal === ev.claim.object_literal
        : prev.claim.object_id === ev.claim.object_id
    );
    if (sameObj) continue;
    runs.push(ev);
  }
  const runIds = new Set(runs.map(r => r.claim.id));
  return claims.map(c => {
    if (c.owner !== owner || c.subject_id !== subject_id || c.verb !== verb) return c;
    const st = recomputeClaimStatus(c);
    if (st === 'retracted') {
      const liveRetracts = c.evidence.filter(e => e.state === 'live' && e.act === 'retract')
        .sort((a, b) => a.event_time.localeCompare(b.event_time));
      const rt = liveRetracts.length ? liveRetracts[liveRetracts.length - 1].event_time.slice(0, 10) : c.valid_to;
      return { ...c, status: 'retracted', valid_to: rt || c.valid_to, superseded_by: null };
    }
    if (st === 'orphaned') return { ...c, status: 'orphaned' };
    if (!runIds.has(c.id)) {
      const idx = events.findIndex(e => e.claim.id === c.id);
      if (idx >= 0) {
        const nextRun = runs.find(r => r.time > events[idx].time);
        return {
          ...c, status: st, valid_from: events[idx].date,
          valid_to: nextRun ? nextRun.date : c.valid_to,
          superseded_by: nextRun ? nextRun.claim.id : null,
        };
      }
      return { ...c, status: st };
    }
    const runIdx = runs.findIndex(r => r.claim.id === c.id);
    const next = runs[runIdx + 1];
    return {
      ...c, status: st, valid_from: runs[runIdx].date,
      valid_to: next ? next.date : null, superseded_by: next ? next.claim.id : null,
    };
  });
}

function applyMutationTo(claims: Claim[], entities: Entity[], merged: boolean, m: Mutation) {
  let nc = clone(claims); let ne = clone(entities); let nm = merged;
  const appendEvidence = (claimId: string, e: Evidence) => {
    nc = nc.map(c => c.id === claimId ? { ...c, evidence: [...c.evidence, e] } : c);
  };
  if (m.kind === 'confirm' && m.claimId) {
    appendEvidence(m.claimId, {
      id: 'ev-' + Date.now(), extractor: 'uptake', version: 'u-2.1', speaker: 'user',
      act: 'assert', event_time: nowISO(), state: 'live',
    });
    const target = nc.find(c => c.id === m.claimId);
    if (target) {
      nc = FUNCTIONAL.includes(target.verb)
        ? rebuildFunctionalTimeline(nc, target.owner, target.subject_id, target.verb)
        : nc.map(c => c.id === m.claimId ? { ...c, status: recomputeClaimStatus(c) } : c);
    }
  }
  if (m.kind === 'retract' && m.claimId) {
    appendEvidence(m.claimId, {
      id: 'evr-' + Date.now(), extractor: 'uptake', version: 'u-2.1', speaker: 'user',
      act: 'retract', event_time: nowISO(), state: 'live',
    });
    const target = nc.find(c => c.id === m.claimId);
    if (target) {
      nc = FUNCTIONAL.includes(target.verb)
        ? rebuildFunctionalTimeline(nc, target.owner, target.subject_id, target.verb)
        : nc.map(c => c.id === m.claimId ? { ...c, status: recomputeClaimStatus(c) } : c);
    }
  }
  if ((m.kind === 'retract_evidence' || m.kind === 'withdraw') && m.claimId && m.evidenceId) {
    nc = nc.map(c => c.id === m.claimId
      ? { ...c, evidence: c.evidence.map(e => e.id === m.evidenceId ? { ...e, state: 'retracted' as EvState } : e) }
      : c);
    const target = nc.find(c => c.id === m.claimId);
    if (target) {
      nc = FUNCTIONAL.includes(target.verb)
        ? rebuildFunctionalTimeline(nc, target.owner, target.subject_id, target.verb)
        : nc.map(c => c.id === m.claimId ? { ...c, status: recomputeClaimStatus(c) } : c);
    }
  }
  return { claims: nc, entities: ne, merged: nm };
}

function pretty(obj: unknown) {
  try { return JSON.stringify(obj, null, 2); } catch { return String(obj); }
}

export default function App() {
  const [owners, setOwners] = useState<string[]>([]);
  const [owner, setOwner] = useState('');
  const [mode, setMode] = useState<'vector'|'shadow'|'hybrid'>('vector');
  const [globalSearch, setGlobalSearch] = useState('');
  const [entities, setEntities] = useState<Entity[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [spans, setSpans] = useState<Record<string, Span>>({});
  const [schema, setSchema] = useState<SchemaRow[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [merged, setMerged] = useState(false);
  const [tab, setTab] = useState<'graph'|'claims'|'timelines'|'schema'|'query'|'jobs'|'shadow'>('graph');
  const [selectedEntity, setSelectedEntity] = useState('');
  const [selectedClaim, setSelectedClaim] = useState<string|null>(null);
  const [showRaw, setShowRaw] = useState(true);
  const [entitySearch, setEntitySearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<EntType[]>([]);
  const [statusFilter, setStatusFilter] = useState<'all'|ClaimStatus>('all');
  const [showLiterals, setShowLiterals] = useState(true);
  const [showMerged, setShowMerged] = useState(false);
  const [hop, setHop] = useState<1|2>(2);
  const [pan, setPan] = useState({ x: 0, y: 0, k: 1 });
  const [hover, setHover] = useState<{ name: string; type: string; grounded: number; x: number; y: number }|null>(null);
  const [focusNode, setFocusNode] = useState<string|null>(null);
  const [claimSearch, setClaimSearch] = useState('');
  const [claimStatus, setClaimStatus] = useState<'all'|ClaimStatus>('all');
  const [claimVerb, setClaimVerb] = useState('all');
  const [currentOnly, setCurrentOnly] = useState(false);
  const [extractorFilter, setExtractorFilter] = useState<'all'|'rules'|'model'|'uptake'|'legacy'>('all');
  const [sortKey, setSortKey] = useState<'fact_text'|'event_date'|'verb'|'status'>('event_date');
  const [sortDir, setSortDir] = useState<1|-1>(-1);
  const [query, setQuery] = useState('Where does Raul live?');
  const [queryRan, setQueryRan] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobMsg, setJobMsg] = useState('');
  const [saveMsg, setSaveMsg] = useState('');
  const [loadedMsg, setLoadedMsg] = useState('Loading live graph…');
  const [copyDone, setCopyDone] = useState('');
  const [busy, setBusy] = useState(false);
  const dragRef = useRef<{ x: number; y: number }|null>(null);
  const copyRef = useRef<HTMLInputElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);

  async function loadOwners() {
    const res = await fetch(apiUrl('/api/librarian/explorer/owners'));
    if (!res.ok) throw new Error('owners ' + res.status);
    const j = await res.json();
    const list: string[] = Array.isArray(j.owners) ? j.owners.map(String) : [];
    setOwners(list);
    return list;
  }

  async function loadSnapshot(nextOwner: string) {
    if (!nextOwner) return;
    setBusy(true);
    try {
      const res = await fetch(apiUrl(`/api/librarian/explorer/snapshot?owner=${encodeURIComponent(nextOwner)}`));
      if (!res.ok) throw new Error('snapshot ' + res.status);
      const j = await res.json();
      const ents: Entity[] = (j.entities || []).map((e: any) => ({
        id: String(e.id), owner: String(e.owner), name: String(e.name),
        name_norm: e.name_norm ? String(e.name_norm) : undefined,
        type: (e.type in TYPE_COLOR ? e.type : 'other') as EntType,
        type_db: e.type_db ? String(e.type_db) : String(e.type),
        aliases: Array.isArray(e.aliases) ? e.aliases.map(String) : [],
        alias_detail: Array.isArray(e.alias_detail) ? e.alias_detail : [],
        merged_into: e.merged_into != null ? String(e.merged_into) : null,
        has_name_vec: !!e.has_name_vec,
        passports: Array.isArray(e.passports) ? e.passports : [],
        note: String(e.note || ''),
        raw: e.raw,
      }));
      const cls: Claim[] = (j.claims || []).map((c: any) => ({
        id: String(c.id), owner: String(c.owner), subject_id: String(c.subject_id),
        verb: String(c.verb),
        object_type: c.object_type === 'literal' ? 'literal' : 'entity',
        object_id: c.object_id != null ? String(c.object_id) : undefined,
        object_literal: c.object_literal != null ? String(c.object_literal) : undefined,
        status: (['grounded','unconfirmed','retracted','orphaned'].includes(c.status) ? c.status : 'orphaned') as ClaimStatus,
        fact_text: String(c.fact_text || ''), event_date: String(c.event_date || ''),
        span_id: String(c.span_id || ''), span_excerpt: String(c.span_excerpt || ''),
        valid_from: c.valid_from || undefined, valid_to: c.valid_to ?? null,
        superseded_by: c.superseded_by ?? null, polarity: c.polarity, uptake: c.uptake,
        has_fact_vec: !!c.has_fact_vec, embed_model: c.embed_model, raw: c.raw,
        evidence: Array.isArray(c.evidence) ? c.evidence.map((ev: any) => ({
          id: String(ev.id), span_id: ev.span_id != null ? String(ev.span_id) : undefined,
          extractor: String(ev.extractor), version: String(ev.version),
          speaker: String(ev.speaker), act: ev.act === 'retract' ? 'retract' : 'assert',
          event_time: String(ev.event_time || ''),
          state: String(ev.state || 'live') as EvState,
          state_db: ev.state_db, polarity: ev.polarity,
          subject_surface: ev.subject_surface, object_surface: ev.object_surface,
          object_key: ev.object_key, confidence: ev.confidence, note: ev.note,
        })) : [],
      }));
      const jbs: Job[] = (j.jobs || []).map((job: any) => ({
        id: String(job.id), kind: String(job.kind), op_id: String(job.op_id),
        status: job.status, fence: String(job.fence), lease: String(job.lease || '—'),
        attempts: Number(job.attempts || 0), owner: String(job.owner),
        last_error: String(job.last_error || ''),
        session_id: job.session_id, claimed_by: job.claimed_by, priority: job.priority,
        raw: job.raw,
      }));
      const spanMap: Record<string, Span> = {};
      if (j.spans && typeof j.spans === 'object') {
        for (const [k, v] of Object.entries(j.spans as Record<string, any>)) {
          spanMap[k] = {
            id: String(v.id || k), session_id: String(v.session_id || ''),
            role: String(v.role || ''), excerpt: String(v.excerpt || ''),
            content_len: Number(v.content_len || 0), ts: v.ts, drain_status: v.drain_status,
          };
        }
      }
      if (Array.isArray(j.functional_verbs) && j.functional_verbs.length) {
        FUNCTIONAL = j.functional_verbs.map(String);
      }
      setEntities(ents); setClaims(cls); setJobs(jbs); setSpans(spanMap);
      setSchema(Array.isArray(j.schema) ? j.schema : []);
      setCounts(j.counts && typeof j.counts === 'object' ? j.counts : {});
      setMerged(false);
      const live = ents.filter(e => !e.merged_into);
      setSelectedEntity(live[0]?.id || ents[0]?.id || '');
      setSelectedClaim(null); setFocusNode(null);
      setLoadedMsg(`Live · ${live.length} entities · ${cls.length} claims · ${Object.keys(spanMap).length} spans · owner ${nextOwner}`);
      setSaveMsg('');
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    (async () => {
      try {
        const list = await loadOwners();
        const first = list[0] || '';
        if (!first) setLoadedMsg('No owners in the live DB yet — drain conversations to populate entities/claims.');
        setOwner(first);
      } catch (e: any) {
        setLoadedMsg(`Live API unavailable (${e?.message || e}). Start graph_api on :7890.`);
      }
    })();
  }, []);
  useEffect(() => {
    if (!owner) return;
    loadSnapshot(owner).catch((e: any) => setLoadedMsg(`Snapshot failed: ${e?.message || e}`));
  }, [owner]);

  function persistAndApply(m: Mutation) {
    setBusy(true);
    setSaveMsg('Preview only — confirm/retract writes are not wired to the store yet.');
    const out = applyMutationTo(claims, entities, merged, m);
    setClaims(out.claims); setEntities(out.entities); setMerged(out.merged);
    setBusy(false);
  }

  const ownerEntities = useMemo(
    () => entities.filter(e => e.owner === owner && (showMerged || !e.merged_into)),
    [entities, owner, showMerged],
  );
  const ownerClaims = useMemo(() => claims.filter(c => c.owner === owner), [claims, owner]);
  const filteredEntities = useMemo(
    () => ownerEntities.filter(e =>
      (!entitySearch || e.name.toLowerCase().includes(entitySearch.toLowerCase())
        || e.aliases.some(a => a.includes(entitySearch.toLowerCase())))
      && (typeFilter.length === 0 || typeFilter.includes(e.type))
      && (!globalSearch || e.name.toLowerCase().includes(globalSearch.toLowerCase())
        || e.aliases.some(a => a.includes(globalSearch.toLowerCase())))),
    [ownerEntities, entitySearch, typeFilter, globalSearch],
  );
  const claimCountFor = (id: string) => ownerClaims.filter(c => c.subject_id === id || c.object_id === id).length;
  const groundedCountFor = (id: string) => ownerClaims.filter(c => (c.subject_id === id || c.object_id === id) && c.status === 'grounded').length;
  const stats = useMemo(() => ({
    entities: ownerEntities.filter(e => !e.merged_into).length,
    grounded: ownerClaims.filter(c => c.status === 'grounded').length,
    unconfirmed: ownerClaims.filter(c => c.status === 'unconfirmed').length,
    orphaned: ownerClaims.filter(c => c.status === 'orphaned').length,
  }), [ownerEntities, ownerClaims]);
  const typeCounts = useMemo(() => {
    const m: Record<string, number> = {};
    ownerEntities.forEach(e => { if (!e.merged_into) m[e.type] = (m[e.type] || 0) + 1; });
    return m;
  }, [ownerEntities]);

  const groundedCurrentClaims = useMemo(
    () => ownerClaims.filter(c => c.status === 'grounded' && c.valid_to == null),
    [ownerClaims],
  );
  const visibleClaims = useMemo(
    () => ownerClaims.filter(c => {
      if (statusFilter === 'orphaned') return c.status === 'orphaned';
      if (statusFilter !== 'all' && c.status !== statusFilter) return false;
      if (c.status === 'orphaned' && statusFilter === 'all') return true;
      return true;
    }),
    [ownerClaims, statusFilter],
  );
  const graphNodes = useMemo(() => {
    let nodes = ownerEntities.filter(e => !e.merged_into);
    if (focusNode) {
      const neigh = new Set<string>([focusNode]);
      ownerClaims.forEach(c => {
        if (c.subject_id === focusNode && c.object_id) neigh.add(c.object_id);
        if (c.object_id === focusNode) neigh.add(c.subject_id);
      });
      nodes = nodes.filter(n => neigh.has(n.id));
    }
    return nodes;
  }, [ownerEntities, focusNode, ownerClaims]);
  const positions = useMemo(() => {
    const map: Record<string, { x: number; y: number }> = {};
    const n = graphNodes.length || 1;
    graphNodes.forEach((node, i) => {
      const ang = (i / n) * Math.PI * 2 - Math.PI / 2;
      const rx = i % 2 === 0 ? 300 : 210;
      const ry = i % 2 === 0 ? 210 : 150;
      map[node.id] = { x: 400 + Math.cos(ang) * rx, y: 280 + Math.sin(ang) * ry };
    });
    return map;
  }, [graphNodes]);
  const literalNodes = useMemo(() => {
    if (!showLiterals) return [];
    const seen = new Map<string, string>();
    visibleClaims.forEach(c => {
      if (c.object_type === 'literal' && c.object_literal) seen.set(c.object_literal, c.object_literal);
    });
    return [...seen.keys()].map((lit, i) => ({
      id: 'lit:' + lit, label: lit, x: 640 + (i % 3) * 22, y: 50 + i * 48,
    }));
  }, [visibleClaims, showLiterals]);
  const traversal = useMemo(() => {
    if (!selectedEntity) return new Set<string>();
    const seen = new Set<string>([selectedEntity]);
    let frontier = [selectedEntity];
    for (let d = 0; d < hop; d++) {
      const next: string[] = [];
      groundedCurrentClaims.forEach(c => {
        if (frontier.includes(c.subject_id) && c.object_id && !seen.has(c.object_id)) {
          seen.add(c.object_id); next.push(c.object_id);
        }
        if (frontier.includes(c.object_id || '') && !seen.has(c.subject_id)) {
          seen.add(c.subject_id); next.push(c.subject_id);
        }
      });
      frontier = next;
    }
    return seen;
  }, [selectedEntity, hop, groundedCurrentClaims]);
  const traversalEdges = useMemo(
    () => new Set(groundedCurrentClaims
      .filter(c => c.object_id && traversal.has(c.subject_id) && traversal.has(c.object_id))
      .map(c => c.id)),
    [groundedCurrentClaims, traversal],
  );

  const filteredClaims = useMemo(() => {
    let list = ownerClaims.filter(c =>
      (claimStatus === 'all' || c.status === claimStatus)
      && (claimVerb === 'all' || c.verb === claimVerb)
      && (!currentOnly || c.valid_to == null)
      && (extractorFilter === 'all' || c.evidence.some(e => e.extractor === extractorFilter))
      && (!claimSearch && !globalSearch
        || c.fact_text.toLowerCase().includes((claimSearch || globalSearch).toLowerCase())
        || (c.span_excerpt || '').toLowerCase().includes((claimSearch || globalSearch).toLowerCase())));
    list = [...list].sort((a, b) => {
      const av = String((a as any)[sortKey] || '');
      const bv = String((b as any)[sortKey] || '');
      return av.localeCompare(bv) * sortDir;
    });
    return list;
  }, [ownerClaims, claimStatus, claimVerb, claimSearch, globalSearch, sortKey, sortDir, currentOnly, extractorFilter]);
  const verbs = useMemo(() => [...new Set(ownerClaims.map(c => c.verb))], [ownerClaims]);
  const selectedEnt = entities.find(e => e.id === selectedEntity && e.owner === owner) || ownerEntities[0];
  const selectedCl = claims.find(c => c.id === selectedClaim);
  const entName = (id?: string) => entities.find(e => e.id === id)?.name || (id || '—');
  const objLabel = (c: Claim) => c.object_type === 'literal' ? (c.object_literal || '') : entName(c.object_id);
  const spanFor = (id?: string) => (id ? spans[id] : undefined);
  const passportSpans = useMemo(() => {
    if (!selectedEnt) return [] as string[];
    const fromPassports = (selectedEnt.passports || []).map(p => p.span_id);
    const fromClaims = ownerClaims
      .filter(c => c.subject_id === selectedEnt.id || c.object_id === selectedEnt.id)
      .map(c => c.span_id);
    return [...new Set([...fromPassports, ...fromClaims].filter(Boolean))];
  }, [ownerClaims, selectedEnt]);
  const provenanceValue = selectedCl ? selectedCl.span_id : (passportSpans[0] || '');

  const queryResult = useMemo(() => {
    const norm = (w: string) => w === 'lives' ? 'live' : w === 'living' ? 'live' : w === 'lived' ? 'live' : w;
    const tokens = query.toLowerCase().replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean).map(norm);
    const linked = ownerEntities.filter(e => !e.merged_into).filter(e => {
      const name = e.name.toLowerCase();
      const aliasText = e.aliases.join(' ').toLowerCase();
      return tokens.some(t => t.length > 2 && (name.includes(t) || aliasText.includes(t)));
    });
    const linkedIds = new Set(linked.map(e => e.id));
    const seen = new Set<string>([...linkedIds]);
    let frontier = [...linkedIds];
    for (let d = 0; d < 2; d++) {
      const next: string[] = [];
      groundedCurrentClaims.forEach(c => {
        if (frontier.includes(c.subject_id) && c.object_id && !seen.has(c.object_id)) {
          seen.add(c.object_id); next.push(c.object_id);
        }
        if (frontier.includes(c.object_id || '') && !seen.has(c.subject_id)) {
          seen.add(c.subject_id); next.push(c.subject_id);
        }
      });
      frontier = next;
    }
    const pathClaims = groundedCurrentClaims.filter(c =>
      seen.has(c.subject_id) && (c.object_id ? seen.has(c.object_id) : true)
      && (linkedIds.has(c.subject_id) || (c.object_id && linkedIds.has(c.object_id))));
    const packed = groundedCurrentClaims.filter(c =>
      linkedIds.has(c.subject_id) || (c.object_id && linkedIds.has(c.object_id)));
    const score = (c: Claim) => {
      const text = (c.fact_text + ' ' + c.verb + ' ' + objLabel(c)).toLowerCase()
        .replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).map(norm);
      return tokens.filter(t => text.includes(t)).length;
    };
    const vectorPick = ownerClaims.length
      ? [...ownerClaims].sort((a, b) => score(b) - score(a) || a.event_date.localeCompare(b.event_date))[0]
      : null;
    const hybridPick = packed.length
      ? [...packed].sort((a, b) => score(b) - score(a))[0]
      : (pathClaims[0] || null);
    const fused = [...packed].sort((a, b) => score(b) - score(a)).slice(0, 5)
      .map((c, i) => ({ claim: c, rank: i + 1, score: score(c) }));
    return { tokens, linked, pathClaims, packed, vectorPick, hybridPick, fused };
  }, [query, ownerEntities, ownerClaims, groundedCurrentClaims]);

  function zoomBy(factor: number) {
    setPan(p => {
      const newK = Math.min(2, Math.max(0.6, p.k * factor));
      const cx = 400, cy = 280;
      const ratio = newK / p.k;
      return { k: newK, x: cx - (cx - p.x) * ratio, y: cy - (cy - p.y) * ratio };
    });
  }
  function handleHover(e: React.MouseEvent, n: Entity) {
    const rect = wrapperRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = e.clientX - rect.left; const y = e.clientY - rect.top;
    setHover({
      name: n.name, type: n.type, grounded: groundedCountFor(n.id),
      x: Math.min(Math.max(8, x), 560), y: Math.min(Math.max(8, y), 480),
    });
  }
  async function doCopy(text: string) {
    const val = text || provenanceValue || '';
    if (!val) return;
    try {
      await navigator.clipboard.writeText(val);
      setCopyDone('Copied ' + val);
    } catch {
      if (copyRef.current) {
        copyRef.current.value = val; copyRef.current.select();
        try { document.execCommand('copy'); setCopyDone('Copied ' + val); }
        catch { setCopyDone(val); }
      }
    }
  }
  function claimAction(kind: 'confirm'|'retract', claim: Claim) {
    persistAndApply({ kind, claimId: claim.id, at: nowISO() });
  }
  function retractEv(claim: Claim, evId: string) {
    persistAndApply({ kind: 'retract_evidence', claimId: claim.id, evidenceId: evId, at: nowISO() });
  }

  const ownerJobs = useMemo(() => jobs.filter(j => j.owner === owner), [jobs, owner]);
  function jobClaim() {
    const j = ownerJobs.find(x => x.status === 'pending');
    if (!j) { setJobMsg('No pending job to claim.'); return; }
    const num = parseInt(j.fence.replace('f-', ''), 10) || 0;
    const newFence = 'f-' + (num + 1);
    setJobs(jobs.map(x => x.id === j.id
      ? { ...x, status: 'leased', lease: '05:00 left', fence: newFence, last_error: '' } : x));
    setJobMsg(`Claimed ${j.op_id}. Fence ${j.fence} → ${newFence} (preview).`);
  }
  function jobHeartbeat() {
    const j = ownerJobs.find(x => x.status === 'leased');
    if (!j) { setJobMsg('No leased job to heartbeat.'); return; }
    setJobs(jobs.map(x => x.id === j.id ? { ...x, lease: '05:00 left' } : x));
    setJobMsg(`Heartbeat sent for ${j.op_id} (preview).`);
  }
  function jobPublish() {
    const j = ownerJobs.find(x => x.status === 'leased');
    if (!j) { setJobMsg('Claim a job before publishing.'); return; }
    setJobs(jobs.map(x => x.id === j.id ? { ...x, status: 'done', lease: '—', last_error: '' } : x));
    setJobMsg(`Published ${j.op_id} (preview).`);
  }
  function jobFail() {
    const j = [...ownerJobs].reverse().find(x => x.status === 'leased' || x.status === 'pending');
    if (!j) { setJobMsg('No active job to fail.'); return; }
    setJobs(jobs.map(x => x.id === j.id
      ? { ...x, status: 'retryable', attempts: x.attempts + 1, lease: '—', last_error: 'Publish failed — marked retryable' }
      : x));
    setJobMsg(`Marked ${j.op_id} retryable (preview).`);
  }

  const timelineGroups = useMemo(() => {
    const groups: Record<string, Claim[]> = {};
    ownerClaims.filter(c => FUNCTIONAL.includes(c.verb)).forEach(c => {
      const k = c.subject_id + '|' + c.verb;
      groups[k] = groups[k] || [];
      groups[k].push(c);
    });
    return Object.entries(groups).map(([k, cs]) => ({
      key: k, subject: cs[0].subject_id, verb: cs[0].verb,
      claims: [...cs].sort((a, b) => (a.valid_from || '').localeCompare(b.valid_from || '')),
    }));
  }, [ownerClaims]);
  const timeToPct = (d: string) => {
    const start = new Date('2025-06-01').getTime();
    const end = new Date('2026-06-01').getTime();
    return Math.max(0, Math.min(100, ((new Date(d).getTime() - start) / (end - start)) * 100));
  };
  const edgeGroups = useMemo(() => {
    const map = new Map<string, Claim[]>();
    visibleClaims.filter(c => c.object_type === 'entity' && c.object_id
      && positions[c.subject_id] && positions[c.object_id]).forEach(c => {
      const key = c.subject_id + '|' + c.object_id;
      const arr = map.get(key) || []; arr.push(c); map.set(key, arr);
    });
    return map;
  }, [visibleClaims, positions]);
  const edgeIndex = useMemo(() => {
    const m = new Map<string, { index: number; total: number }>();
    edgeGroups.forEach(arr => arr.forEach((c, i) => m.set(c.id, { index: i, total: arr.length })));
    return m;
  }, [edgeGroups]);

  const rawPayload = selectedCl?.raw || selectedEnt?.raw || null;

  return (
    <div className="hx-shell" data-safe-area-inset-root>
      <header className="hx-header" data-safe-area-sticky-top>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
              style={{ background: 'var(--brand-ink)' }}>
              <img src={librarianIcon} alt="" className="h-7 w-auto" />
            </div>
            <div className="min-w-0">
              <h1 className="text-[clamp(1.35rem,2.8vw,1.75rem)] leading-none font-semibold truncate"
                style={{ color: 'var(--text)' }}>
                The Hermes Librarian
              </h1>
              <p className="text-[13px] mt-1" style={{ color: 'var(--text-muted)' }}>
                Graph Explorer — claims · evidence · storage · owner-scoped
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 ml-auto flex-wrap">
            <div className="flex rounded-full p-1 max-w-full overflow-x-auto" style={{ background: 'var(--surface-sunken)', border: '1px solid var(--line)' }}>
              {(owners.length ? owners : ['—']).map(o => (
                <button key={o} disabled={o === '—' || busy}
                  onClick={() => { setOwner(o); setSelectedClaim(null); setFocusNode(null); }}
                  className="px-3 py-1.5 rounded-full text-[10px] font-semibold whitespace-nowrap"
                  style={owner === o
                    ? { background: 'var(--accent)', color: 'var(--on-accent)' }
                    : { color: 'var(--text-muted)' }}>
                  {o}
                </button>
              ))}
            </div>
            <div className="flex rounded-full p-1" style={{ background: 'var(--surface-sunken)', border: '1px solid var(--line)' }}>
              {(['vector', 'shadow', 'hybrid'] as const).map(m => (
                <button key={m} onClick={() => setMode(m)}
                  className="px-3 py-1.5 rounded-full text-[10px] font-semibold capitalize"
                  style={mode === m
                    ? { background: 'var(--accent)', color: 'var(--on-accent)' }
                    : { color: 'var(--text-muted)' }}>
                  {m}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 rounded-full px-3 py-2 text-sm min-w-[180px] flex-1 sm:flex-none"
              style={{ border: '1px solid var(--line-strong)', background: 'var(--surface-raised)' }}>
              <Search size={15} /><input value={globalSearch} onChange={e => setGlobalSearch(e.target.value)}
                placeholder="Search graph…" className="bg-transparent outline-none w-full text-sm"
                aria-label="Global search" />
            </label>
          </div>
        </div>
        <div className="flex flex-wrap gap-2 mt-3 items-center">
          {[
            { l: 'Entities', v: stats.entities },
            { l: 'Grounded', v: stats.grounded },
            { l: 'Unconfirmed', v: stats.unconfirmed },
            { l: 'Orphaned', v: stats.orphaned },
            { l: 'Spans', v: Object.keys(spans).length },
          ].map(s => (
            <span key={s.l} className="hx-chip">{s.l} <b className="ml-1 mono">{s.v}</b></span>
          ))}
          <span className="hx-chip">
            Retrieval: {mode} · Owner: {owner || '—'}
          </span>
          <span className="mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
            {loadedMsg} {saveMsg}
          </span>
        </div>
      </header>

      <main className="flex flex-col lg:flex-row items-start gap-3 px-3 pb-4 overflow-x-clip">
        <aside className="hx-sidebar w-full lg:w-[280px] shrink-0 p-4">
          <p className="text-[10px] tracking-[0.18em] font-semibold" style={{ color: 'var(--seal)' }}>
            ENTITY BROWSER
          </p>
          <label className="mt-3 flex items-center gap-2 rounded-lg px-3 py-2 text-sm"
            style={{ background: 'var(--surface-sunken)', border: '1px solid var(--line-strong)' }}>
            <Search size={15} />
            <input value={entitySearch} onChange={e => setEntitySearch(e.target.value)}
              placeholder="Search entities / aliases" className="bg-transparent outline-none w-full placeholder:text-[color:var(--text-muted)]"
              aria-label="Search entities" />
          </label>
          <label className="mt-2 flex items-center gap-2 text-[11px]">
            <input type="checkbox" checked={showMerged} onChange={e => setShowMerged(e.target.checked)} />
            Show merged-away rows
          </label>
          <div className="mt-4 space-y-1">
            {(['person','project','place','org','tool','concept','other'] as EntType[]).map(t => {
              const active = typeFilter.includes(t);
              return (
                <button key={t} onClick={() => setTypeFilter(active ? typeFilter.filter(x => x !== t) : [...typeFilter, t])}
                  className="w-full flex items-center justify-between rounded-lg px-2 py-1.5 text-sm"
                  style={{ background: active ? 'var(--surface-sunken)' : 'transparent', boxShadow: active ? 'inset 0 0 0 1px var(--line-strong)' : 'none' }}>
                  <span className="flex items-center gap-2 capitalize">
                    <span className="hx-gem" style={{ background: TYPE_COLOR[t] }} />{t}
                  </span>
                  <span className="mono text-xs" style={{ color: 'var(--text-muted)' }}>{typeCounts[t] || 0}</span>
                </button>
              );
            })}
          </div>
          <div className="mt-4 space-y-2 max-h-[48vh] overflow-auto">
            {filteredEntities.map(e => {
              const sel = selectedEntity === e.id;
              return (
                <button key={e.id} onClick={() => { setSelectedEntity(e.id); setSelectedClaim(null); setTab('graph'); }}
                  className="w-full text-left rounded-xl p-3 border"
                  style={{
                    background: sel ? 'var(--surface-sunken)' : 'transparent',
                    color: 'var(--text)',
                    borderColor: sel ? 'var(--accent)' : 'var(--line)',
                    opacity: e.merged_into ? 0.65 : 1,
                  }}>
                  <span className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-sm truncate">{e.name}</span>
                    <span className="hx-chip capitalize shrink-0">
                      <span className="hx-gem" style={{ background: TYPE_COLOR[e.type] }} />{e.type_db || e.type}
                    </span>
                  </span>
                  <span className="text-xs block mt-1"
                    style={{ color: 'var(--text-muted)' }}>
                    {claimCountFor(e.id)} claims · {groundedCountFor(e.id)} grounded
                    {e.aliases.length ? ` · aka ${e.aliases.slice(0, 3).join(', ')}` : ''}
                    {e.merged_into ? ` · merged→ ${e.merged_into}` : ''}
                  </span>
                </button>
              );
            })}
            {filteredEntities.length === 0 && (
              <p className="text-sm rounded-xl border border-dashed p-3" style={{ borderColor: 'var(--line)' }}>
                No entities match this filter.
              </p>
            )}
          </div>
          <div className="mt-5 rounded-xl p-3" style={{ background: 'var(--surface-sunken)', border: '1px solid var(--line)' }}>
            <p className="text-sm font-semibold">Live graph slice</p>
            <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
              Snapshot from Postgres. Confirm/retract remain preview-only.
            </p>
            <button disabled={busy || !owner} onClick={() => owner && loadSnapshot(owner)}
              className="hx-btn mt-3 w-full flex items-center justify-center gap-2">
              <RotateCcw size={14} /> RELOAD SNAPSHOT
            </button>
          </div>
        </aside>

        <section className="flex-1 min-w-0 w-full">
          <div className="hx-tabs flex-wrap w-fit" role="tablist">
            {([
              ['graph', 'Graph'], ['claims', 'Claims'], ['timelines', 'Timelines'],
              ['schema', 'Schema'], ['query', 'Query'], ['jobs', 'Jobs'], ['shadow', 'Shadow'],
            ] as const).map(([k, l]) => (
              <button key={k} onClick={() => setTab(k)} role="tab" aria-selected={tab === k}
                className={`hx-tab ${tab === k ? 'on' : ''}`}>{l}</button>
            ))}
          </div>

          {tab === 'graph' && (
            <div className="hx-panel mt-4 overflow-hidden">
              <div className="flex flex-wrap items-center gap-2 p-3 border-b"
                style={{ borderColor: 'var(--line)' }}>
                <span className="text-[11px] font-semibold tracking-[0.14em]"
                  style={{ color: 'var(--seal)' }}>GRAPH CANVAS</span>
                <select value={statusFilter} onChange={e => setStatusFilter(e.target.value as any)}
                  className="text-xs rounded-full px-2 py-1.5" style={{ border: '1px solid var(--line-strong)', background: 'var(--surface-raised)' }}
                  aria-label="Filter by status">
                  <option value="all">All statuses</option>
                  <option value="grounded">Grounded</option>
                  <option value="unconfirmed">Unconfirmed</option>
                  <option value="retracted">Retracted</option>
                  <option value="orphaned">Orphaned</option>
                </select>
                <label className="text-xs flex items-center gap-1">
                  <input type="checkbox" checked={showLiterals} onChange={e => setShowLiterals(e.target.checked)} /> Literals
                </label>
                <div className="flex rounded-full overflow-hidden text-xs" style={{ border: '1px solid var(--line-strong)' }}>
                  <button onClick={() => setHop(1)} className="px-2 py-1"
                    style={{ background: hop === 1 ? 'var(--accent)' : 'transparent', color: hop === 1 ? 'var(--on-accent)' : '' }}>1-hop</button>
                  <button onClick={() => setHop(2)} className="px-2 py-1"
                    style={{ background: hop === 2 ? 'var(--accent)' : 'transparent', color: hop === 2 ? 'var(--on-accent)' : '' }}>2-hop</button>
                </div>
                <div className="flex gap-1 ml-auto">
                  <button onClick={() => zoomBy(1.2)} className="hx-btn-ghost p-2" aria-label="Zoom in"><ZoomIn size={15} /></button>
                  <button onClick={() => zoomBy(1 / 1.2)} className="hx-btn-ghost p-2" aria-label="Zoom out"><ZoomOut size={15} /></button>
                  <button onClick={() => { setPan({ x: 0, y: 0, k: 1 }); setFocusNode(null); }}
                    className="hx-btn-ghost px-2 py-2 text-[10px] flex items-center gap-1">
                    <RotateCcw size={14} /> RESET
                  </button>
                </div>
              </div>
              <p className="text-xs px-3 pt-2 italic" style={{ color: 'var(--text-muted)' }}>
                Traversal: grounded + valid_to IS NULL only · highlight = hop path from {selectedEnt?.name || '—'}
              </p>
              <div ref={wrapperRef} className="relative" style={{ background: 'var(--surface)' }}>
                <svg viewBox="0 0 800 560" className="w-full h-[520px] sm:h-[560px] touch-none select-none"
                  onMouseDown={e => { dragRef.current = { x: e.clientX, y: e.clientY }; }}
                  onMouseMove={e => {
                    if (dragRef.current) {
                      setPan(p => ({ ...p, x: p.x + (e.clientX - dragRef.current!.x), y: p.y + (e.clientY - dragRef.current!.y) }));
                      dragRef.current = { x: e.clientX, y: e.clientY };
                    }
                  }}
                  onMouseUp={() => { dragRef.current = null; }}
                  onMouseLeave={() => { dragRef.current = null; setHover(null); }}>
                  <defs>
                    <filter id="gemGlow" x="-40%" y="-40%" width="180%" height="180%">
                      <feDropShadow dx="0" dy="2" stdDeviation="2" floodColor="#000000" floodOpacity="0.3" />
                    </filter>
                  </defs>
                  <g transform={`translate(${pan.x},${pan.y}) scale(${pan.k})`}>
                    {visibleClaims.filter(c => c.object_type === 'entity' && c.object_id
                      && positions[c.subject_id] && positions[c.object_id]).map(c => {
                      const a = positions[c.subject_id]; const b = positions[c.object_id!];
                      const isTrav = traversalEdges.has(c.id);
                      const info = edgeIndex.get(c.id) || { index: 0, total: 1 };
                      const offset = (info.index - (info.total - 1) / 2) * 22;
                      const dx = b.x - a.x, dy = b.y - a.y;
                      const len = Math.hypot(dx, dy) || 1;
                      const px = -dy / len * offset, py = dx / len * offset;
                      const cx = (a.x + b.x) / 2 + px, cy = (a.y + b.y) / 2 + py;
                      const lx = 0.25 * a.x + 0.5 * cx + 0.25 * b.x;
                      const ly = 0.25 * a.y + 0.5 * cy + 0.25 * b.y;
                      return (
                        <g key={c.id}>
                          <path d={`M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`} fill="none"
                            stroke={isTrav ? 'var(--accent)' : STATUS_COLOR[c.status]}
                            strokeWidth={isTrav ? 3 : 1.6} opacity={isTrav ? 1 : 0.55} />
                          <text x={lx} y={ly - 6} fontSize="10" fill={isTrav ? 'var(--accent)' : 'var(--text-muted)'}
                            textAnchor="middle" style={{ fontFamily: 'var(--font-mono)' }}>{c.verb}</text>
                        </g>
                      );
                    })}
                    {visibleClaims.filter(c => c.object_type === 'literal' && showLiterals).map(c => {
                      const a = positions[c.subject_id]; if (!a) return null;
                      const lit = literalNodes.find(l => l.label === c.object_literal); if (!lit) return null;
                      const isTrav = selectedEntity === c.subject_id;
                      return (
                        <g key={c.id}>
                          <line x1={a.x} y1={a.y} x2={lit.x} y2={lit.y}
                            stroke={isTrav ? 'var(--accent)' : 'var(--line-strong)'} strokeDasharray="3 3" />
                          <rect x={lit.x - 38} y={lit.y - 12} width={76} height={24} rx={5}
                            fill="var(--surface-raised)" stroke={isTrav ? 'var(--accent)' : 'var(--line-strong)'} />
                          <text x={lit.x} y={lit.y + 4} fontSize="10" textAnchor="middle" fill="var(--text)">
                            {c.object_literal}
                          </text>
                        </g>
                      );
                    })}
                    {graphNodes.map(n => {
                      const p = positions[n.id]; if (!p) return null;
                      const deg = claimCountFor(n.id);
                      const sel = n.id === selectedEntity;
                      const inTrav = traversal.has(n.id);
                      const r = 20 + Math.min(12, deg * 2);
                      return (
                        <g key={n.id} className="graph-node" transform={`translate(${p.x},${p.y})`}
                          onClick={() => { setSelectedEntity(n.id); setSelectedClaim(null); }}
                          onDoubleClick={() => setFocusNode(n.id)}
                          onMouseEnter={e => handleHover(e, n)}
                          onMouseMove={e => handleHover(e, n)}
                          onMouseLeave={() => setHover(null)}
                          style={{ cursor: 'pointer' }}>
                          <rect x={-r * 0.72} y={-r * 0.72} width={r * 1.44} height={r * 1.44}
                            rx={3} transform="rotate(45)" fill={TYPE_COLOR[n.type]}
                            stroke={sel || inTrav ? 'var(--accent)' : 'var(--surface)'}
                            strokeWidth={sel || inTrav ? 3 : 1.5} filter="url(#gemGlow)" />
                          <text y={4} textAnchor="middle" fontSize="11" fontWeight="700" fill="var(--surface)"
                            style={{ fontFamily: 'var(--font-sans)' }}>
                            {n.name.split(' ').map(w => w[0]).slice(0, 2).join('')}
                          </text>
                          <text y={r + 16} textAnchor="middle" fontSize="12" fontWeight="600" fill="var(--text)">
                            {n.name}
                          </text>
                          <text y={r + 30} textAnchor="middle" fontSize="9" fill="var(--text-muted)" className="capitalize"
                            style={{ fontFamily: 'var(--font-mono)' }}>
                            {n.type_db || n.type} · {deg}
                          </text>
                        </g>
                      );
                    })}
                  </g>
                </svg>
                {hover && (
                  <div className="absolute pointer-events-none rounded-xl px-3 py-2 text-xs shadow-lg"
                    style={{ left: hover.x, top: hover.y, background: 'var(--surface-raised)', color: 'var(--text-muted)', border: '1px solid var(--line-strong)', maxWidth: 220 }}>
                    <b style={{ color: 'var(--text)' }}>{hover.name}</b> · <span className="capitalize">{hover.type}</span><br />
                    {hover.grounded} grounded · click select · double-click focus
                  </div>
                )}
                <div className="absolute left-3 bottom-3 rounded-xl px-3 py-2 text-[11px] flex flex-wrap gap-3"
                  style={{ background: 'var(--surface-raised)', border: '1px solid var(--line)' }}>
                  <span><b>Legend</b></span>
                  {(['person','project','place','org','tool','concept'] as EntType[]).map(t => (
                    <span key={t} className="flex items-center gap-1 capitalize">
                      <span className="hx-gem" style={{ background: TYPE_COLOR[t] }} />{t}
                    </span>
                  ))}
                  <span className="flex items-center gap-1">
                    <span style={{ width: 14, height: 2, background: 'var(--accent)', display: 'inline-block' }} /> traversal
                  </span>
                </div>
              </div>
            </div>
          )}

          {tab === 'claims' && (
            <div className="hx-panel mt-4 overflow-hidden">
              <div className="flex flex-wrap gap-2 p-3 border-b" style={{ borderColor: 'var(--line)' }}>
                <input value={claimSearch} onChange={e => setClaimSearch(e.target.value)}
                  placeholder="Filter facts / excerpts…" className="rounded-full px-3 py-2 text-sm flex-1 min-w-[180px]"
                  style={{ border: '1px solid var(--line-strong)' }} aria-label="Filter claims" />
                <select value={claimStatus} onChange={e => setClaimStatus(e.target.value as any)}
                  className="text-sm rounded-full px-2 py-2" style={{ border: '1px solid var(--line-strong)' }}>
                  <option value="all">All statuses</option>
                  <option value="grounded">Grounded</option>
                  <option value="unconfirmed">Unconfirmed</option>
                  <option value="retracted">Retracted</option>
                  <option value="orphaned">Orphaned</option>
                </select>
                <select value={claimVerb} onChange={e => setClaimVerb(e.target.value)}
                  className="text-sm rounded-full px-2 py-2" style={{ border: '1px solid var(--line-strong)' }}>
                  <option value="all">All verbs</option>
                  {verbs.map(v => <option key={v} value={v}>{v}</option>)}
                </select>
                <label className="text-xs flex items-center gap-1 self-center">
                  <input type="checkbox" checked={currentOnly} onChange={e => setCurrentOnly(e.target.checked)} /> Current only
                </label>
                <span className="text-xs self-center mono" style={{ color: 'var(--text-muted)' }}>{filteredClaims.length} claims</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm min-w-[860px]">
                  <thead>
                    <tr className="text-left text-[10px] tracking-[0.12em]" style={{ color: 'var(--text-muted)' }}>
                      {([['fact_text','Fact'],['subject_id','Subject'],['verb','Verb'],['object_id','Object'],['status','Status'],['event_date','Event'],['span_id','Span']] as const).map(([k, l]) => (
                        <th key={k} className="px-3 py-2 font-medium">
                          <button onClick={() => {
                            if (sortKey === k) setSortDir(sortDir === 1 ? -1 : 1);
                            else { setSortKey(k as any); setSortDir(1); }
                          }} className="flex items-center gap-1">{l} <ChevronDown size={12} /></button>
                        </th>
                      ))}
                      <th className="px-3 py-2 font-medium">Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredClaims.map(c => (
                      <tr key={c.id} onClick={() => { setSelectedClaim(c.id); setSelectedEntity(c.subject_id); }}
                        className="border-t cursor-pointer hover:bg-black/5"
                        style={{ borderColor: 'var(--line)', background: selectedClaim === c.id ? 'var(--surface-sunken)' : '' }}>
                        <td className="px-3 py-2.5 max-w-[280px]">
                          <span className="block truncate">{c.fact_text}</span>
                          {c.span_excerpt && (
                            <span className="block text-[11px] italic truncate mt-0.5" style={{ color: 'var(--text-muted)' }}>
                              “{c.span_excerpt}”
                            </span>
                          )}
                          <span className="mono text-[10px]" style={{ color: 'var(--text-muted)' }}>{c.id}</span>
                        </td>
                        <td className="px-3 py-2.5">{entName(c.subject_id)}</td>
                        <td className="px-3 py-2.5 mono text-xs">{c.verb}</td>
                        <td className="px-3 py-2.5">{objLabel(c)}</td>
                        <td className="px-3 py-2.5">
                          <span className="hx-status shrink-0" data-status={c.status}>{c.status}</span>
                        </td>
                        <td className="px-3 py-2.5 mono text-xs">{c.event_date}</td>
                        <td className="px-3 py-2.5 mono text-xs">{c.span_id}</td>
                        <td className="px-3 py-2.5">{c.evidence.length}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {filteredClaims.length === 0 && (
                <p className="p-6 text-sm" style={{ color: 'var(--text-muted)' }}>No claims match.</p>
              )}
            </div>
          )}

          {tab === 'timelines' && (
            <div className="mt-4 space-y-4">
              <div className="hx-panel p-4">
                <h2 className="text-sm tracking-[0.14em]">FUNCTIONAL TIMELINES</h2>
                <p className="text-sm mt-1 italic" style={{ color: 'var(--text-muted)' }}>
                  Functional verbs keep one live interval. Event time — not processing time — drives the bar.
                </p>
              </div>
              {timelineGroups.map(g => (
                <div key={g.key} className="hx-panel p-4">
                  <h3 className="font-semibold">{entName(g.subject)}{' '}
                    <span className="mono text-xs font-normal">· {g.verb}</span>
                  </h3>
                  <div className="mt-4 space-y-3">
                    {g.claims.map(c => {
                      const from = c.valid_from || c.event_date;
                      const to = c.valid_to || '2026-06-01';
                      const left = timeToPct(from);
                      const width = Math.max(4, timeToPct(to) - left);
                      return (
                        <div key={c.id}>
                          <div className="flex flex-wrap justify-between gap-2 text-xs">
                            <span><b>{objLabel(c)}</b> · <span className="hx-status" data-status={c.status}>{c.status}</span></span>
                            <span className="mono">valid_from {from} · valid_to {c.valid_to || 'now'}</span>
                          </div>
                          <div className="relative h-7 rounded-full mt-1" style={{ background: 'var(--surface-sunken)' }}>
                            <div className="absolute top-0 h-7 rounded-full flex items-center px-2 text-[11px] font-semibold whitespace-nowrap overflow-hidden"
                              style={{ left: left + '%', width: width + '%', minWidth: 60, color: 'var(--text)',
                                background: `color-mix(in srgb, ${STATUS_COLOR[c.status]} 22%, var(--surface-raised))`,
                                border: `1.5px solid ${STATUS_COLOR[c.status]}` }}>
                              {objLabel(c)}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
              {timelineGroups.length === 0 && (
                <p className="hx-panel p-5 text-sm">No functional intervals for this owner.</p>
              )}
            </div>
          )}

          {tab === 'schema' && (
            <div className="mt-4 space-y-4">
              <div className="hx-panel p-4">
                <div className="flex items-center gap-2">
                  <Table2 size={16} />
                  <h2 className="text-sm tracking-[0.14em]">STORAGE MAP</h2>
                </div>
                <p className="text-sm mt-1 italic" style={{ color: 'var(--text-muted)' }}>
                  How the claim layer is stored for owner <b>{owner || '—'}</b>. Counts are live.
                </p>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-4">
                  {Object.entries(counts).map(([k, v]) => (
                    <div key={k} className="hx-metric">
                      <span className="k">{k}</span>
                      <span className="v">{v}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="hx-panel overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm min-w-[720px]">
                    <thead>
                      <tr className="text-left text-[10px] tracking-[0.12em]" style={{ color: 'var(--text-muted)' }}>
                        <th className="px-3 py-2">Table</th>
                        <th className="px-3 py-2">Role</th>
                        <th className="px-3 py-2">Keys</th>
                        <th className="px-3 py-2">Rows</th>
                      </tr>
                    </thead>
                    <tbody>
                      {schema.map(s => (
                        <tr key={s.table} className="border-t" style={{ borderColor: 'var(--line)' }}>
                          <td className="px-3 py-2.5 mono text-xs font-semibold">{s.table}</td>
                          <td className="px-3 py-2.5 text-sm">{s.role}</td>
                          <td className="px-3 py-2.5 mono text-[11px]" style={{ color: 'var(--text-muted)' }}>{s.keys}</td>
                          <td className="px-3 py-2.5 mono">{s.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
              <div className="hx-banner">
                claims are a projection of claim_evidence · turn_entity is the passport · entity_alias resolves surfaces · merged_into hides survivors&apos; predecessors
              </div>
            </div>
          )}

          {tab === 'query' && (
            <div className="mt-4 grid gap-4">
              <div className="hx-panel p-4">
                <label className="text-[11px] tracking-[0.14em] font-semibold">QUERY PLAYGROUND — {mode}</label>
                <div className="flex flex-col sm:flex-row gap-2 mt-3">
                  <input value={query} onChange={e => { setQuery(e.target.value); setQueryRan(false); }}
                    className="flex-1 rounded-full px-4 py-3 text-sm" style={{ border: '1px solid var(--line-strong)' }}
                    aria-label="Query" />
                  <button onClick={() => setQueryRan(true)} className="hx-btn flex items-center justify-center gap-2">
                    <Play size={15} /> RUN
                  </button>
                </div>
                <p className="text-xs mt-2 italic" style={{ color: 'var(--text-muted)' }}>
                  Client-side preview over the loaded snapshot — not the live packer.
                </p>
              </div>
              {queryRan ? (
                <div className="grid md:grid-cols-2 gap-4">
                  <div className="hx-panel p-4">
                    <h3 className="font-semibold flex items-center gap-2 text-sm tracking-[0.1em]"><Eye size={15} /> LINKED</h3>
                    {queryResult.linked.length === 0
                      ? <p className="text-sm mt-2" style={{ color: 'var(--text-muted)' }}>No entity matched.</p>
                      : queryResult.linked.map(e => (
                        <p key={e.id} className="text-sm mt-2">{e.name}{' '}
                          <span className="mono text-xs">({e.type} · {e.id})</span></p>
                      ))}
                    <div className="hx-term mt-4">
                      {queryResult.packed.length
                        ? queryResult.packed.map(c => `${c.fact_text}\n  span ${c.span_id} · ${c.status}`).join('\n\n')
                        : 'No grounded current facts.'}
                    </div>
                  </div>
                  <div className="hx-panel p-4">
                    <h3 className="font-semibold text-sm tracking-[0.1em]">VECTOR vs HYBRID</h3>
                    <div className="mt-3 rounded-xl p-3 text-sm" style={{ background: 'var(--surface-sunken)' }}>
                      <b>Vector-only:</b> {queryResult.vectorPick?.fact_text || '—'}<br />
                      <b>Hybrid:</b> {queryResult.hybridPick?.fact_text || 'No grounded current fact.'}
                    </div>
                  </div>
                </div>
              ) : (
                <p className="hx-panel p-5 text-sm" style={{ color: 'var(--text-muted)' }}>Run a query to see linked entities and packed facts.</p>
              )}
            </div>
          )}

          {tab === 'jobs' && (
            <div className="mt-4 space-y-4">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {[['Done', ownerJobs.filter(j => j.status === 'done').length],
                  ['Leased', ownerJobs.filter(j => j.status === 'leased').length],
                  ['Pending', ownerJobs.filter(j => j.status === 'pending').length],
                  ['Retryable', ownerJobs.filter(j => j.status === 'retryable').length],
                ].map(([l, v]) => (
                  <div key={l as string} className="hx-metric">
                    <span className="k">{l}</span><span className="v">{v}</span>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap gap-2">
                <button onClick={jobClaim} className="hx-btn-ghost">Claim next</button>
                <button onClick={jobHeartbeat} className="hx-btn-ghost"><HeartPulse size={13} className="inline mr-1" />Heartbeat</button>
                <button onClick={jobPublish} className="hx-btn">Publish</button>
                <button onClick={jobFail} className="hx-btn-ghost">Fail → retryable</button>
                {jobMsg && <span className="text-xs self-center mono">{jobMsg}</span>}
              </div>
              <div className="hx-panel overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm min-w-[760px]">
                    <thead>
                      <tr className="text-left text-[10px] tracking-[0.12em]" style={{ color: 'var(--text-muted)' }}>
                        <th className="px-3 py-2">Kind</th><th className="px-3 py-2">Op ID</th>
                        <th className="px-3 py-2">Status</th><th className="px-3 py-2">Fence</th>
                        <th className="px-3 py-2">Lease</th><th className="px-3 py-2">Attempts</th>
                        <th className="px-3 py-2">Last Error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ownerJobs.map(j => (
                        <tr key={j.id} className="border-t" style={{ borderColor: 'var(--line)' }}>
                          <td className="px-3 py-2.5 capitalize">{j.kind}</td>
                          <td className="px-3 py-2.5 mono text-xs">{j.op_id}</td>
                          <td className="px-3 py-2.5">
                            <span className="hx-status" data-status={j.status}>{j.status}</span>
                          </td>
                          <td className="px-3 py-2.5 mono text-xs">{j.fence}</td>
                          <td className="px-3 py-2.5 text-xs">{j.lease}</td>
                          <td className="px-3 py-2.5">{j.attempts}</td>
                          <td className="px-3 py-2.5 text-xs">{j.last_error || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
              <p className="text-xs flex items-center gap-2" style={{ color: 'var(--text-muted)' }}>
                <Shield size={13} /> Fence/lease controls shown here are preview simulations unless write routes land.
              </p>
            </div>
          )}

          {tab === 'shadow' && (
            <div className="mt-4 space-y-4">
              <div className="hx-banner">
                Shadow compare is illustrative until live dual-retriever metrics are exposed on the viz API.
              </div>
              <div className="grid sm:grid-cols-3 gap-3">
                <div className="hx-panel p-4" style={{ background: 'var(--accent)', color: 'var(--on-accent)', borderColor: 'var(--accent)' }}>
                  <p className="text-[10px] tracking-[0.14em] opacity-80">MULTI-HOP RECALL</p>
                  <p className="text-xl font-bold mt-1" style={{ fontFamily: 'var(--font-display)' }}>Vector → Hybrid</p>
                </div>
                <div className="hx-panel p-4">
                  <p className="text-[10px] tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>UNRELATED CONTEXT</p>
                  <p className="text-xl font-bold mt-1" style={{ fontFamily: 'var(--font-display)' }}>Hybrid keeps less noise</p>
                </div>
                <div className="hx-panel p-4">
                  <p className="text-[10px] tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>LATENCY</p>
                  <p className="text-xl font-bold mt-1" style={{ fontFamily: 'var(--font-display)' }}>p50 / p95 live TBD</p>
                </div>
              </div>
            </div>
          )}
        </section>

        <aside className="w-full lg:w-[360px] shrink-0 space-y-3">
          <div className="hx-panel p-4">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[10px] tracking-[0.18em] font-semibold" style={{ color: 'var(--text-muted)' }}>
                DETAIL PANEL
              </p>
              <button className="hx-btn-ghost text-[9px] flex items-center gap-1"
                onClick={() => setShowRaw(!showRaw)}>
                <Braces size={12} /> {showRaw ? 'HIDE RAW' : 'SHOW RAW'}
              </button>
            </div>

            {selectedCl ? (
              <div className="mt-3">
                <span className="hx-status shrink-0" data-status={selectedCl.status}>{selectedCl.status}</span>
                <h2 className="text-xl font-bold leading-tight mt-2">{selectedCl.fact_text}</h2>
                <div className="mt-4 rounded-xl p-3 text-sm flex flex-wrap items-center gap-1"
                  style={{ background: 'var(--surface-sunken)' }}>
                  <b>{entName(selectedCl.subject_id)}</b><ArrowRight size={14} />
                  <span className="mono text-xs">{selectedCl.verb}</span><ArrowRight size={14} />
                  <b>{objLabel(selectedCl)}</b>
                </div>
                <dl className="text-xs mt-4 space-y-2">
                  <div className="flex justify-between gap-2"><dt style={{ color: 'var(--text-muted)' }}>Event date</dt><dd className="mono">{selectedCl.event_date}</dd></div>
                  <div className="flex justify-between gap-2"><dt style={{ color: 'var(--text-muted)' }}>Span ID</dt><dd className="mono">{selectedCl.span_id || '—'}</dd></div>
                  <div className="flex justify-between gap-2"><dt style={{ color: 'var(--text-muted)' }}>Valid</dt><dd className="mono">{selectedCl.valid_from || '—'} → {selectedCl.valid_to || 'now'}</dd></div>
                  <div className="flex justify-between gap-2"><dt style={{ color: 'var(--text-muted)' }}>Superseded by</dt><dd className="mono">{selectedCl.superseded_by || '—'}</dd></div>
                  <div className="flex justify-between gap-2"><dt style={{ color: 'var(--text-muted)' }}>Polarity / uptake</dt><dd className="mono">{selectedCl.polarity || '—'} / {selectedCl.uptake || '—'}</dd></div>
                  <div className="flex justify-between gap-2"><dt style={{ color: 'var(--text-muted)' }}>fact_vec</dt><dd className="mono">{selectedCl.has_fact_vec ? 'present' : 'null'}{selectedCl.embed_model ? ` · ${selectedCl.embed_model}` : ''}</dd></div>
                </dl>

                {(selectedCl.span_excerpt || spanFor(selectedCl.span_id)) && (
                  <div className="mt-4">
                    <h3 className="font-semibold text-sm flex items-center gap-2"><Layers size={14} /> SPAN EXCERPT</h3>
                    <div className="hx-term mt-2">
                      {(() => {
                        const sp = spanFor(selectedCl.span_id);
                        return [
                          `span ${selectedCl.span_id}`,
                          sp ? `role=${sp.role} session=${sp.session_id} drain=${sp.drain_status ?? 'null'}` : '',
                          '',
                          selectedCl.span_excerpt || sp?.excerpt || '(empty)',
                        ].filter(Boolean).join('\n');
                      })()}
                    </div>
                  </div>
                )}

                <div className="grid grid-cols-2 gap-2 mt-4">
                  <button disabled={busy} onClick={() => claimAction('confirm', selectedCl)}
                    className="hx-btn flex items-center justify-center gap-1">
                    <Check size={14} /> CONFIRM
                  </button>
                  <button disabled={busy} onClick={() => claimAction('retract', selectedCl)}
                    className="hx-btn-danger">RETRACT</button>
                </div>

                <h3 className="font-semibold text-sm mt-5 flex items-center gap-2">
                  <Database size={14} /> EVIDENCE · {selectedCl.evidence.length}
                </h3>
                <div className="space-y-2 mt-2">
                  {selectedCl.evidence.map(e => {
                    const esp = spanFor(e.span_id);
                    return (
                      <div key={e.id} className="rounded-xl p-3 text-xs"
                        style={{ border: '1px solid var(--line)', background: 'var(--surface-raised)' }}>
                        <div className="flex justify-between gap-2">
                          <b>{e.extractor} · {e.version}</b>
                          <span className="hx-status" data-status={e.state}>{e.state_db || e.state}</span>
                        </div>
                        <p className="mt-1 mono" style={{ color: 'var(--text-muted)' }}>
                          {e.speaker} · {e.act} · {e.polarity || '—'} · conf {e.confidence ?? '—'}
                          <br />{e.subject_surface} —{e.object_key || ''}→ {e.object_surface}
                          <br />event {e.event_time}
                          {e.span_id ? <> · span {e.span_id}</> : null}
                        </p>
                        {esp?.excerpt && (
                          <p className="mt-2 italic" style={{ color: 'var(--text-muted)' }}>“{esp.excerpt}”</p>
                        )}
                        <button disabled={e.state === 'retracted' || e.state === 'withdrawn' || busy}
                          onClick={() => retractEv(selectedCl, e.id)}
                          className="mt-2 text-xs underline">Retract evidence</button>
                      </div>
                    );
                  })}
                </div>
                <button onClick={() => setSelectedClaim(null)} className="mt-4 text-xs underline">
                  Back to entity detail
                </button>
              </div>
            ) : selectedEnt ? (
              <div className="mt-3">
                <span className="hx-chip capitalize">
                  <span className="hx-gem" style={{ background: TYPE_COLOR[selectedEnt.type] }} />
                  {selectedEnt.type_db || selectedEnt.type}
                </span>
                <h2 className="text-2xl font-bold leading-tight mt-2">{selectedEnt.name}</h2>
                <p className="mono text-[11px] mt-1" style={{ color: 'var(--text-muted)' }}>
                  id {selectedEnt.id} · norm {selectedEnt.name_norm || '—'}
                  {selectedEnt.merged_into ? ` · merged_into ${selectedEnt.merged_into}` : ''}
                  {selectedEnt.has_name_vec ? ' · name_vec' : ' · no name_vec'}
                </p>

                <h3 className="font-semibold text-sm mt-5">Aliases · {(selectedEnt.alias_detail || []).length}</h3>
                <div className="space-y-1 mt-2">
                  {(selectedEnt.alias_detail || []).length === 0 && (
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {selectedEnt.aliases.length ? selectedEnt.aliases.join(', ') : 'No aliases.'}
                    </p>
                  )}
                  {(selectedEnt.alias_detail || []).map(a => (
                    <div key={a.surface_norm + a.source} className="rounded-lg px-2 py-1.5 text-xs flex justify-between gap-2"
                      style={{ background: 'var(--surface-sunken)' }}>
                      <span className="mono">{a.surface_norm}</span>
                      <span style={{ color: 'var(--text-muted)' }}>{a.source} · {a.state}</span>
                    </div>
                  ))}
                </div>

                <h3 className="font-semibold text-sm mt-5">
                  turn_entity passports · {(selectedEnt.passports || []).length}
                </h3>
                <div className="space-y-1 mt-2 max-h-36 overflow-auto">
                  {(selectedEnt.passports || []).length === 0 && (
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>No passport rows for this entity.</p>
                  )}
                  {(selectedEnt.passports || []).map(p => {
                    const sp = spanFor(p.span_id);
                    return (
                      <button key={p.span_id + p.surface} type="button"
                        onClick={() => doCopy(p.span_id)}
                        className="w-full text-left rounded-lg px-2 py-1.5 text-xs"
                        style={{ background: 'var(--surface-sunken)' }}>
                        <span className="mono">span {p.span_id}</span> · {p.surface}
                        <span className="block italic mt-0.5" style={{ color: 'var(--text-muted)' }}>
                          {sp?.excerpt || 'no excerpt loaded'}
                        </span>
                      </button>
                    );
                  })}
                </div>

                <h3 className="font-semibold text-sm mt-5">
                  Claims · {ownerClaims.filter(c => c.subject_id === selectedEnt.id || c.object_id === selectedEnt.id).length}
                </h3>
                <div className="space-y-2 mt-2">
                  {ownerClaims.filter(c => c.subject_id === selectedEnt.id || c.object_id === selectedEnt.id)
                    .slice(0, 8).map(c => (
                      <button key={c.id} onClick={() => setSelectedClaim(c.id)}
                        className="w-full text-left rounded-xl p-3 text-xs"
                        style={{ border: '1px solid var(--line)', background: 'var(--surface-raised)' }}>
                        <span className="flex justify-between gap-2">
                          <span className="font-semibold">{c.fact_text}</span>
                          <span className="hx-status shrink-0" data-status={c.status}>{c.status}</span>
                        </span>
                        <span className="mono block mt-1" style={{ color: 'var(--text-muted)' }}>
                          {c.verb} · {c.event_date} · {c.span_id}
                        </span>
                      </button>
                    ))}
                </div>
              </div>
            ) : (
              <p className="text-sm mt-3" style={{ color: 'var(--text-muted)' }}>Select an entity or claim.</p>
            )}
          </div>

          {showRaw && rawPayload && (
            <div className="hx-panel p-4">
              <h3 className="font-semibold text-sm flex items-center gap-2 tracking-[0.1em]">
                <Braces size={14} /> RAW ROW
              </h3>
              <p className="text-xs mt-1 italic" style={{ color: 'var(--text-muted)' }}>
                Direct fields from Postgres (vectors omitted as presence flags).
              </p>
              <div className="hx-term mt-3">{pretty(rawPayload)}</div>
            </div>
          )}

          <div className="hx-panel p-4">
            <h3 className="font-semibold text-sm flex items-center gap-2"><Layers size={14} /> PROVENANCE</h3>
            <p className="text-xs mt-2 italic" style={{ color: 'var(--text-muted)' }}>
              Copy a span id to trace a fact back to its turn.
            </p>
            <input ref={copyRef} value={provenanceValue} onChange={() => {}}
              className="w-full mt-3 rounded-lg px-3 py-2 mono text-xs"
              style={{ border: '1px solid var(--line-strong)', background: 'var(--surface-raised)' }}
              aria-label="Span id to copy" readOnly />
            <button onClick={() => doCopy(provenanceValue)}
              className="mt-2 w-full hx-btn-ghost flex items-center justify-center gap-2">
              <Copy size={13} /> COPY SPAN ID
            </button>
            {copyDone && <p className="text-xs mt-2" style={{ color: 'var(--status-grounded)' }}>{copyDone}</p>}
          </div>

          <div className="rounded-2xl p-4 text-xs leading-relaxed"
            style={{ background: 'var(--surface-raised)', color: 'var(--text)', border: '1px solid var(--line)' }}>
            <p className="font-semibold flex items-center gap-2 tracking-[0.1em]"
              style={{ fontFamily: 'var(--font-display)' }}>
              <Clock size={13} /> HOW TO READ THIS
            </p>
            <p className="mt-2" style={{ color: 'var(--text-muted)' }}>
              Graph shows connections. Claims show what was said. Timelines show when it was true.
              Evidence shows why. Schema shows how it is stored. Raw shows the row.
            </p>
          </div>
        </aside>
      </main>

      <footer className="px-5 pb-6 pt-1 flex flex-wrap gap-4 text-[11px]" style={{ color: 'var(--text-muted)' }}>
        <span className="flex items-center gap-1"><Activity size={12} /> Hermes Graph Explorer · live storage inspector</span>
        <span>Owner-scoped: {owner || '—'}</span>
        <span>Evidence, not counters · Event time, not processing time</span>
      </footer>
    </div>
  );
}
