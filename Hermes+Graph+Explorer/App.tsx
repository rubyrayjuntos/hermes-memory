import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Search, GitMerge, ZoomIn, ZoomOut, RotateCcw, Play, Copy, Check, ChevronDown, Activity, Layers, Clock, Shield, Database, ArrowRight, Eye, CircleDot, Sparkles, GitBranch, HeartPulse } from 'lucide-react';

type EntType = 'person'|'project'|'place'|'org'|'tool'|'concept'|'other';
type ClaimStatus = 'grounded'|'unconfirmed'|'retracted'|'orphaned';
type EvState = 'live'|'retracted'|'superseded_version';
type EvAct = 'assert'|'retract';
interface Entity{ id:string; owner:string; name:string; type:EntType; aliases:string[]; note:string }
interface Evidence{ id:string; extractor:string; version:string; speaker:string; act:EvAct; event_time:string; state:EvState; note?:string }
interface Claim{ id:string; owner:string; subject_id:string; verb:string; object_type:'entity'|'literal'; object_id?:string; object_literal?:string; status:ClaimStatus; fact_text:string; event_date:string; span_id:string; valid_from?:string; valid_to?:string|null; superseded_by?:string|null; evidence:Evidence[] }
interface Job{ id:string; kind:string; op_id:string; status:'pending'|'leased'|'done'|'retryable'|'failed'; fence:string; lease:string; attempts:number; owner:string; last_error:string }
interface Mutation{ kind:string; claimId?:string; evidenceId?:string; at:string }

const TYPE_COLOR: Record<EntType,string> = { person:'#0E6B5A', project:'#2B5EA6', place:'#B7791F', org:'#6D4AC6', tool:'#B23A6B', concept:'#1F7A4D', other:'#6B7280' };
const STATUS_COLOR: Record<ClaimStatus,string> = { grounded:'#0E6B5A', unconfirmed:'#B7791F', retracted:'#B23A6B', orphaned:'#6B7280' };
const FUNCTIONAL = ['based_in','owned_by','works_for','lives_in','role_is','status_is'];

const BASE_ENTITIES: Entity[] = [
  {id:'e-raul',owner:'raul',name:'Raul Hunter',type:'person',aliases:['Raul'],note:'Primary subject. Owner of this graph slice.'},
  {id:'e-maya',owner:'raul',name:'Maya Chen',type:'person',aliases:['Maya'],note:'Collaborator on Hermes and Lumen.'},
  {id:'e-hermes-p',owner:'raul',name:'Hermes',type:'project',aliases:['Hermes Graph'],note:'Project — distinct from the Hermes tool.'},
  {id:'e-hermes-t',owner:'raul',name:'Hermes',type:'tool',aliases:['Hermes CLI'],note:'Tool — distinct from the Hermes project.'},
  {id:'e-acme',owner:'raul',name:'Acme',type:'org',aliases:['Acme Corp'],note:'Duplicate surface form awaiting merge.'},
  {id:'e-acme-corp',owner:'raul',name:'Acme Corp',type:'org',aliases:['Acme Corporation'],note:'Canonical org after merge.'},
  {id:'e-austin',owner:'raul',name:'Austin',type:'place',aliases:['ATX'],note:'Place.'},
  {id:'e-nyc',owner:'raul',name:'NYC',type:'place',aliases:['New York'],note:'Place.'},
  {id:'e-houston',owner:'raul',name:'Houston',type:'place',aliases:['HOU'],note:'Place.'},
  {id:'e-lumen',owner:'raul',name:'Lumen',type:'project',aliases:[],note:'Second project.'},
  {id:'e-postgres',owner:'raul',name:'Postgres',type:'tool',aliases:['PostgreSQL'],note:'Tool used by Raul.'},
  {id:'e-memory',owner:'raul',name:'Memory Graph',type:'concept',aliases:[],note:'Concept explored by Raul.'},
  {id:'m-maya',owner:'maya-demo',name:'Maya Chen',type:'person',aliases:['Maya'],note:'Isolated slice for maya-demo. No Raul data here.'},
  {id:'m-lumen',owner:'maya-demo',name:'Lumen',type:'project',aliases:[],note:'Project in maya-demo slice.'},
  {id:'m-austin',owner:'maya-demo',name:'Austin',type:'place',aliases:['ATX'],note:'Place in maya-demo slice.'},
  {id:'m-hermes',owner:'maya-demo',name:'Hermes',type:'tool',aliases:[],note:'Tool in maya-demo slice.'},
];
function ev(id:string, extractor:string, version:string, speaker:string, act:EvAct, event_time:string, state:EvState='live', note?:string):Evidence{ return {id,extractor,version,speaker,act,event_time,state,note}; }
const BASE_CLAIMS: Claim[] = [
  {id:'c1',owner:'raul',subject_id:'e-raul',verb:'based_in',object_type:'entity',object_id:'e-nyc',status:'grounded',fact_text:'Raul Hunter was based in NYC.',event_date:'2025-08-01',span_id:'span_0142',valid_from:'2025-08-01',valid_to:'2025-11-02',superseded_by:'c2',evidence:[ev('ev1','rules','r-1.4','user','assert','2025-08-01T14:02:00Z'),ev('ev1b','uptake','u-2.0','assistant','assert','2025-08-01T14:03:00Z','superseded_version')]},
  {id:'c2',owner:'raul',subject_id:'e-raul',verb:'based_in',object_type:'entity',object_id:'e-austin',status:'grounded',fact_text:'Raul Hunter was based in Austin.',event_date:'2025-11-02',span_id:'span_0891',valid_from:'2025-11-02',valid_to:'2026-02-10',superseded_by:'c3',evidence:[ev('ev2','rules','r-1.4','user','assert','2025-11-02T09:11:00Z')]},
  {id:'c3',owner:'raul',subject_id:'e-raul',verb:'based_in',object_type:'entity',object_id:'e-nyc',status:'grounded',fact_text:'Raul Hunter is based in NYC.',event_date:'2026-02-10',span_id:'span_1022',valid_from:'2026-02-10',valid_to:null,superseded_by:null,evidence:[ev('ev3','uptake','u-2.1','user','assert','2026-02-10T10:20:00Z')]},
  {id:'c4',owner:'raul',subject_id:'e-raul',verb:'works_for',object_type:'entity',object_id:'e-acme-corp',status:'grounded',fact_text:'Raul Hunter works for Acme Corp.',event_date:'2025-06-15',span_id:'span_0310',valid_from:'2025-06-15',valid_to:null,evidence:[ev('ev4','model','m-3.2','assistant','assert','2025-06-15T12:00:00Z')]},
  {id:'c5',owner:'raul',subject_id:'e-raul',verb:'lives_in',object_type:'entity',object_id:'e-austin',status:'retracted',fact_text:'Raul Hunter lives in Austin.',event_date:'2025-09-01',span_id:'span_0455',valid_from:'2025-09-01',valid_to:'2025-11-02',evidence:[ev('ev5','rules','r-1.3','user','assert','2025-09-01T08:00:00Z'),ev('ev5r','uptake','u-2.0','user','retract','2025-11-02T09:12:00Z')]},
  {id:'c6',owner:'raul',subject_id:'e-hermes-p',verb:'status_is',object_type:'literal',object_literal:'active',status:'grounded',fact_text:'Hermes (project) status is active.',event_date:'2026-01-12',span_id:'span_0770',valid_from:'2026-01-12',valid_to:null,evidence:[ev('ev6','model','m-3.2','assistant','assert','2026-01-12T11:00:00Z')]},
  {id:'c7',owner:'raul',subject_id:'e-hermes-p',verb:'owned_by',object_type:'entity',object_id:'e-acme-corp',status:'grounded',fact_text:'Hermes (project) is owned by Acme Corp.',event_date:'2025-07-20',span_id:'span_0611',valid_from:'2025-07-20',valid_to:null,evidence:[ev('ev7','rules','r-1.4','user','assert','2025-07-20T10:00:00Z')]},
  {id:'c8',owner:'raul',subject_id:'e-maya',verb:'works_for',object_type:'entity',object_id:'e-acme-corp',status:'grounded',fact_text:'Maya Chen works for Acme Corp.',event_date:'2025-06-20',span_id:'span_0321',valid_from:'2025-06-20',valid_to:null,evidence:[ev('ev8','model','m-3.1','assistant','assert','2025-06-20T13:00:00Z')]},
  {id:'c9',owner:'raul',subject_id:'e-lumen',verb:'based_in',object_type:'entity',object_id:'e-houston',status:'grounded',fact_text:'Lumen is based in Houston.',event_date:'2025-08-18',span_id:'span_0512',valid_from:'2025-08-18',valid_to:null,evidence:[ev('ev9','rules','r-1.4','user','assert','2025-08-18T09:00:00Z')]},
  {id:'c10',owner:'raul',subject_id:'e-raul',verb:'uses',object_type:'entity',object_id:'e-postgres',status:'grounded',fact_text:'Raul Hunter uses Postgres.',event_date:'2025-07-01',span_id:'span_0400',evidence:[ev('ev10','uptake','u-2.0','user','assert','2025-07-01T10:00:00Z')]},
  {id:'c11',owner:'raul',subject_id:'e-raul',verb:'role_is',object_type:'literal',object_literal:'lead engineer',status:'grounded',fact_text:'Raul Hunter role is lead engineer.',event_date:'2025-06-15',span_id:'span_0312',valid_from:'2025-06-15',valid_to:null,evidence:[ev('ev11','rules','r-1.4','user','assert','2025-06-15T12:05:00Z')]},
  {id:'c12',owner:'raul',subject_id:'e-maya',verb:'based_in',object_type:'entity',object_id:'e-austin',status:'unconfirmed',fact_text:'Maya Chen is based in Austin.',event_date:'2026-03-02',span_id:'span_1201',valid_from:'2026-03-02',valid_to:null,evidence:[ev('ev12','model','m-3.2','assistant','assert','2026-03-02T15:00:00Z')]},
  {id:'c13',owner:'raul',subject_id:'e-raul',verb:'uses',object_type:'entity',object_id:'e-hermes-t',status:'grounded',fact_text:'Raul Hunter uses Hermes (tool).',event_date:'2025-08-02',span_id:'span_0411',evidence:[ev('ev13','uptake','u-2.0','user','assert','2025-08-02T10:00:00Z')]},
  {id:'c14',owner:'raul',subject_id:'e-hermes-t',verb:'owned_by',object_type:'entity',object_id:'e-acme',status:'grounded',fact_text:'Hermes (tool) is owned by Acme.',event_date:'2025-07-22',span_id:'span_0613',valid_from:'2025-07-22',valid_to:null,evidence:[ev('ev14','legacy','legacy-0.9','system','assert','2025-07-22T09:00:00Z')]},
  {id:'c15',owner:'raul',subject_id:'e-raul',verb:'collaborated_with',object_type:'entity',object_id:'e-lumen',status:'orphaned',fact_text:'Raul Hunter collaborated with Lumen.',event_date:'2025-10-01',span_id:'span_0910',evidence:[ev('ev15','legacy','legacy-0.9','system','retract','2025-10-01T09:00:00Z','retracted','source span deleted')]},
  {id:'c16',owner:'raul',subject_id:'e-maya',verb:'uses',object_type:'entity',object_id:'e-postgres',status:'grounded',fact_text:'Maya Chen uses Postgres.',event_date:'2025-08-10',span_id:'span_0520',evidence:[ev('ev16','uptake','u-2.0','user','assert','2025-08-10T10:00:00Z')]},
  {id:'c17',owner:'raul',subject_id:'e-lumen',verb:'status_is',object_type:'literal',object_literal:'beta',status:'grounded',fact_text:'Lumen status is beta.',event_date:'2026-01-20',span_id:'span_0781',valid_from:'2026-01-20',valid_to:null,evidence:[ev('ev17','model','m-3.2','assistant','assert','2026-01-20T11:00:00Z')]},
  {id:'c18',owner:'raul',subject_id:'e-hermes-p',verb:'uses',object_type:'entity',object_id:'e-postgres',status:'grounded',fact_text:'Hermes (project) uses Postgres.',event_date:'2025-07-05',span_id:'span_0415',evidence:[ev('ev18','rules','r-1.4','user','assert','2025-07-05T10:00:00Z')]},
  {id:'c19',owner:'raul',subject_id:'e-raul',verb:'explores',object_type:'entity',object_id:'e-memory',status:'grounded',fact_text:'Raul Hunter explores Memory Graph.',event_date:'2026-02-15',span_id:'span_1030',evidence:[ev('ev19','uptake','u-2.1','user','assert','2026-02-15T10:00:00Z')]},
  {id:'c20',owner:'raul',subject_id:'e-acme-corp',verb:'based_in',object_type:'entity',object_id:'e-austin',status:'unconfirmed',fact_text:'Acme Corp is based in Austin.',event_date:'2026-03-10',span_id:'span_1210',valid_from:'2026-03-10',valid_to:null,evidence:[ev('ev20','model','m-3.2','assistant','assert','2026-03-10T10:00:00Z')]},
  {id:'c21',owner:'raul',subject_id:'e-maya',verb:'role_is',object_type:'literal',object_literal:'designer',status:'unconfirmed',fact_text:'Maya Chen role is designer.',event_date:'2026-03-03',span_id:'span_1205',valid_from:'2026-03-03',valid_to:null,evidence:[ev('ev21','model','m-3.2','assistant','assert','2026-03-03T10:00:00Z')]},
  {id:'mc1',owner:'maya-demo',subject_id:'m-maya',verb:'based_in',object_type:'entity',object_id:'m-austin',status:'grounded',fact_text:'Maya Chen is based in Austin.',event_date:'2026-01-05',span_id:'span_m101',valid_from:'2026-01-05',valid_to:null,evidence:[ev('mev1','rules','r-1.4','user','assert','2026-01-05T09:00:00Z')]},
  {id:'mc2',owner:'maya-demo',subject_id:'m-maya',verb:'works_for',object_type:'entity',object_id:'m-lumen',status:'grounded',fact_text:'Maya Chen works for Lumen.',event_date:'2026-01-06',span_id:'span_m102',valid_from:'2026-01-06',valid_to:null,evidence:[ev('mev2','uptake','u-2.0','user','assert','2026-01-06T09:00:00Z')]},
  {id:'mc3',owner:'maya-demo',subject_id:'m-lumen',verb:'based_in',object_type:'entity',object_id:'m-austin',status:'grounded',fact_text:'Lumen is based in Austin.',event_date:'2026-01-07',span_id:'span_m103',valid_from:'2026-01-07',valid_to:null,evidence:[ev('mev3','rules','r-1.4','user','assert','2026-01-07T09:00:00Z')]},
  {id:'mc4',owner:'maya-demo',subject_id:'m-maya',verb:'uses',object_type:'entity',object_id:'m-hermes',status:'grounded',fact_text:'Maya Chen uses Hermes (tool).',event_date:'2026-01-08',span_id:'span_m104',evidence:[ev('mev4','uptake','u-2.0','user','assert','2026-01-08T09:00:00Z')]},
];
const INITIAL_JOBS: Job[] = [
  {id:'j1',kind:'ingest',op_id:'op_8812',status:'done',fence:'f-12',lease:'—',attempts:1,owner:'raul',last_error:''},
  {id:'j2',kind:'extract',op_id:'op_8813',status:'leased',fence:'f-13',lease:'00:42 left',attempts:1,owner:'raul',last_error:''},
  {id:'j3',kind:'merge',op_id:'op_8814',status:'pending',fence:'f-13',lease:'—',attempts:0,owner:'maya-demo',last_error:''},
  {id:'j4',kind:'shadow',op_id:'op_8815',status:'retryable',fence:'f-14',lease:'—',attempts:2,owner:'raul',last_error:'Extractor timeout'},
  {id:'j5',kind:'pack',op_id:'op_8816',status:'pending',fence:'f-14',lease:'—',attempts:0,owner:'maya-demo',last_error:''},
];

function clone<T>(x:T):T{ return JSON.parse(JSON.stringify(x)); }
function nowISO(){ return new Date().toISOString(); }
function liveEvidence(c:Claim){ return c.evidence.filter(e=>e.state==='live'); }
function recomputeClaimStatus(c:Claim):ClaimStatus{
  const live = liveEvidence(c);
  if(live.length===0) return 'orphaned';
  const asserts = live.filter(e=>e.act==='assert');
  const retracts = live.filter(e=>e.act==='retract');
  if(retracts.length>0 && asserts.length===0) return 'retracted';
  if(retracts.length>0 && asserts.length>0){
    const newestAssert = [...asserts].sort((a,b)=>a.event_time.localeCompare(b.event_time)).pop()!;
    const newestRetract = [...retracts].sort((a,b)=>a.event_time.localeCompare(b.event_time)).pop()!;
    if(newestRetract.event_time > newestAssert.event_time) return 'retracted';
  }
  if(asserts.some(e=>e.speaker==='user')) return 'grounded';
  if(asserts.some(e=>e.speaker==='assistant')) return 'unconfirmed';
  if(asserts.length>0) return 'grounded';
  return 'orphaned';
}
function rebuildFunctionalTimeline(allClaims:Claim[], owner:string, subject_id:string, verb:string):Claim[]{
  const claims = clone(allClaims);
  const group = claims.filter(c=>c.owner===owner && c.subject_id===subject_id && c.verb===verb);
  if(group.length===0) return claims;
  type Evt={claim:Claim; time:string; date:string};
  const events:Evt[]=[];
  for(const c of group){
    const st = recomputeClaimStatus(c);
    if(st==='retracted' || st==='orphaned') continue;
    const liveAsserts = c.evidence.filter(e=>e.state==='live' && e.act==='assert').sort((a,b)=>a.event_time.localeCompare(b.event_time));
    if(liveAsserts.length===0) continue;
    events.push({claim:c, time:liveAsserts[0].event_time, date:liveAsserts[0].event_time.slice(0,10)});
  }
  events.sort((a,b)=> a.time.localeCompare(b.time) || a.claim.id.localeCompare(b.claim.id));
  const runs:Evt[]=[];
  for(const ev of events){
    const prev = runs[runs.length-1];
    const sameObj = prev && prev.claim.object_type===ev.claim.object_type && (prev.claim.object_type==='literal' ? prev.claim.object_literal===ev.claim.object_literal : prev.claim.object_id===ev.claim.object_id);
    if(sameObj) continue;
    runs.push(ev);
  }
  const runIds = new Set(runs.map(r=>r.claim.id));
  return claims.map(c=>{
    if(c.owner!==owner || c.subject_id!==subject_id || c.verb!==verb) return c;
    const st = recomputeClaimStatus(c);
    if(st==='retracted'){
      const liveRetracts = c.evidence.filter(e=>e.state==='live' && e.act==='retract').sort((a,b)=>a.event_time.localeCompare(b.event_time));
      const rt = liveRetracts.length ? liveRetracts[liveRetracts.length-1].event_time.slice(0,10) : c.valid_to;
      return {...c, status:'retracted', valid_to: rt || c.valid_to, superseded_by:null};
    }
    if(st==='orphaned') return {...c, status:'orphaned'};
    if(!runIds.has(c.id)){
      const idx = events.findIndex(e=>e.claim.id===c.id);
      if(idx>=0){
        const nextRun = runs.find(r=> r.time > events[idx].time);
        return {...c, status:st, valid_from:events[idx].date, valid_to: nextRun ? nextRun.date : c.valid_to, superseded_by: nextRun ? nextRun.claim.id : null};
      }
      return {...c, status:st};
    }
    const runIdx = runs.findIndex(r=>r.claim.id===c.id);
    const next = runs[runIdx+1];
    return {...c, status:st, valid_from:runs[runIdx].date, valid_to: next ? next.date : null, superseded_by: next ? next.claim.id : null};
  });
}

function applyMutationTo(claims:Claim[], entities:Entity[], merged:boolean, m:Mutation):{claims:Claim[];entities:Entity[];merged:boolean}{
  let nc=clone(claims); let ne=clone(entities); let nm=merged;
  const appendEvidence = (claimId:string, e:Evidence)=>{ nc=nc.map(c=> c.id===claimId ? {...c, evidence:[...c.evidence, e]} : c); };
  if(m.kind==='confirm' && m.claimId){
    appendEvidence(m.claimId, {id:'ev-'+Date.now()+Math.random(),extractor:'uptake',version:'u-2.1',speaker:'user',act:'assert',event_time:nowISO(),state:'live'});
    const target = nc.find(c=>c.id===m.claimId);
    if(target){ if(FUNCTIONAL.includes(target.verb)) nc=rebuildFunctionalTimeline(nc, target.owner, target.subject_id, target.verb); else nc=nc.map(c=> c.id===m.claimId ? {...c, status:recomputeClaimStatus(c)} : c); }
  }
  if(m.kind==='retract' && m.claimId){
    appendEvidence(m.claimId, {id:'evr-'+Date.now(),extractor:'uptake',version:'u-2.1',speaker:'user',act:'retract',event_time:nowISO(),state:'live'});
    const target = nc.find(c=>c.id===m.claimId);
    if(target){ if(FUNCTIONAL.includes(target.verb)) nc=rebuildFunctionalTimeline(nc, target.owner, target.subject_id, target.verb); else nc=nc.map(c=> c.id===m.claimId ? {...c, status:recomputeClaimStatus(c)} : c); }
  }
  if((m.kind==='retract_evidence' || m.kind==='withdraw') && m.claimId && m.evidenceId){
    nc=nc.map(c=> c.id===m.claimId ? {...c,evidence:c.evidence.map(e=> e.id===m.evidenceId?{...e,state:'retracted' as EvState}:e)}:c);
    const target = nc.find(c=>c.id===m.claimId);
    if(target){ if(FUNCTIONAL.includes(target.verb)) nc=rebuildFunctionalTimeline(nc, target.owner, target.subject_id, target.verb); else nc=nc.map(c=> c.id===m.claimId ? {...c, status:recomputeClaimStatus(c)} : c); }
  }
  if(m.kind==='merge'){
    nm=true;
    const source = ne.find(e=>e.id==='e-acme');
    const survivor = ne.find(e=>e.id==='e-acme-corp');
    if(source && survivor){
      const mergedAliases = Array.from(new Set([...survivor.aliases, ...source.aliases]));
      ne=ne.map(e=> e.id==='e-acme-corp' ? {...e, aliases:mergedAliases} : e.id==='e-acme' ? {...e, aliases:[]} : e);
    }
    nc=nc.map(c=> ({...c, subject_id:c.subject_id==='e-acme'?'e-acme-corp':c.subject_id, object_id:c.object_id==='e-acme'?'e-acme-corp':c.object_id }));
    // Deduplicate colliding claims: same (owner, subject, verb, object) with both live/current
    const seen = new Map<string,string>();
    const deduped:Claim[]=[];
    for(const c of nc){
      const objKey = c.object_type==='literal' ? 'lit:'+(c.object_literal||'') : 'ent:'+(c.object_id||'');
      const key = [c.owner,c.subject_id,c.verb,objKey].join('|');
      const existingId = seen.get(key);
      if(existingId){
        const survivorClaim = deduped.find(d=>d.id===existingId);
        if(survivorClaim && (survivorClaim.status==='grounded'||survivorClaim.status==='unconfirmed') && survivorClaim.valid_to==null && (c.status==='grounded'||c.status==='unconfirmed') && c.valid_to==null){
          const mergedEv = [...survivorClaim.evidence];
          for(const e of c.evidence){ if(!mergedEv.some(x=>x.id===e.id)) mergedEv.push(e); }
          survivorClaim.evidence = mergedEv;
          survivorClaim.status = recomputeClaimStatus(survivorClaim);
          deduped.push({...c, superseded_by: survivorClaim.id, valid_to: survivorClaim.valid_from || c.valid_from || c.event_date, status: c.status});
          continue;
        }
      } else {
        seen.set(key, c.id);
      }
      deduped.push(c);
    }
    nc=deduped;
    // Rebuild functional groups touched by the merge
    const groups = new Map<string,{owner:string;subject_id:string;verb:string}>();
    for(const c of nc){ if(FUNCTIONAL.includes(c.verb)) groups.set(c.owner+'|'+c.subject_id+'|'+c.verb, {owner:c.owner,subject_id:c.subject_id,verb:c.verb}); }
    for(const g of groups.values()){ nc=rebuildFunctionalTimeline(nc, g.owner, g.subject_id, g.verb); }
  }
  if(m.kind==='unmerge'){ nm=false; }
  if(m.kind==='retract_newest'){
    const group = nc.filter(c=>c.owner==='raul'&&c.subject_id==='e-raul'&&c.verb==='based_in');
    const current = group.find(c=>c.valid_to==null && c.status==='grounded') || [...group].sort((a,b)=>(a.valid_from||'').localeCompare(b.valid_from||'')).pop();
    if(current){
      appendEvidence(current.id, {id:'evr-newest-'+Date.now(),extractor:'rules',version:'r-1.4',speaker:'user',act:'retract',event_time:nowISO(),state:'live'});
      nc=rebuildFunctionalTimeline(nc, 'raul', 'e-raul', 'based_in');
    }
  }
  if(m.kind==='reassert'){
    const newId = 'c-new-austin-' + Date.now();
    const today = new Date().toISOString().slice(0,10);
    nc.push({id:newId,owner:'raul',subject_id:'e-raul',verb:'based_in',object_type:'entity',object_id:'e-austin',status:'grounded',fact_text:'Raul Hunter is based in Austin.',event_date:today,span_id:'span_'+Math.floor(1000+Math.random()*9000),valid_from:today,valid_to:null,superseded_by:null,evidence:[{id:'ev-new-'+Date.now(),extractor:'uptake',version:'u-2.1',speaker:'user',act:'assert',event_time:nowISO(),state:'live'}]});
    nc=rebuildFunctionalTimeline(nc, 'raul', 'e-raul', 'based_in');
  }
  return {claims:nc,entities:ne,merged:nm};
}

export default function App(){
  const [owner,setOwner]=useState<'raul'|'maya-demo'>('raul');
  const [mode,setMode]=useState<'vector'|'shadow'|'hybrid'>('vector');
  const [globalSearch,setGlobalSearch]=useState('');
  const [entities,setEntities]=useState<Entity[]>(()=>clone(BASE_ENTITIES));
  const [claims,setClaims]=useState<Claim[]>(()=>clone(BASE_CLAIMS));
  const [merged,setMerged]=useState(false);
  const [tab,setTab]=useState<'graph'|'claims'|'timelines'|'query'|'jobs'|'shadow'>('graph');
  const [selectedEntity,setSelectedEntity]=useState<string>('e-raul');
  const [selectedClaim,setSelectedClaim]=useState<string|null>(null);
  const [entitySearch,setEntitySearch]=useState('');
  const [typeFilter,setTypeFilter]=useState<EntType[]>([]);
  const [statusFilter,setStatusFilter]=useState<'all'|ClaimStatus>('all');
  const [showLiterals,setShowLiterals]=useState(true);
  const [hop,setHop]=useState<1|2>(2);
  const [pan,setPan]=useState({x:0,y:0,k:1});
  const [hover,setHover]=useState<{name:string;type:string;grounded:number;x:number;y:number}|null>(null);
  const [focusNode,setFocusNode]=useState<string|null>(null);
  const [claimSearch,setClaimSearch]=useState('');
  const [claimStatus,setClaimStatus]=useState<'all'|ClaimStatus>('all');
  const [claimVerb,setClaimVerb]=useState('all');
  const [currentOnly,setCurrentOnly]=useState(false);
  const [extractorFilter,setExtractorFilter]=useState<'all'|'rules'|'model'|'uptake'|'legacy'>('all');
  const [sortKey,setSortKey]=useState<'fact_text'|'event_date'|'verb'|'status'>('event_date');
  const [sortDir,setSortDir]=useState<1|-1>(-1);
  const [query,setQuery]=useState('Where does Raul live?');
  const [queryRan,setQueryRan]=useState(false);
  const [jobs,setJobs]=useState<Job[]>(INITIAL_JOBS);
  const [jobMsg,setJobMsg]=useState('');
  const [saveMsg,setSaveMsg]=useState('');
  const [loadedMsg,setLoadedMsg]=useState('Demo data loaded locally.');
  const [copyDone,setCopyDone]=useState('');
  const [busy,setBusy]=useState(false);
  const dragRef=useRef<{x:number;y:number}|null>(null);
  const copyRef=useRef<HTMLInputElement>(null);
  const wrapperRef=useRef<HTMLDivElement>(null);

  async function callApi(action:string, args:any){
    const res = await fetch("actions", {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({action, args})});
    const j = await res.json().catch(()=>({error:'bad response'}));
    if(!res.ok || j.error) throw new Error(j.error || ('API '+res.status));
    return j.data;
  }
  async function reloadRecords(){
    const data = await callApi("records.list", {collection:'hermes_mutations'});
    const recs = data?.records || [];
    let nc=clone(BASE_CLAIMS); let ne=clone(BASE_ENTITIES); let nm=false;
    const sorted=[...recs].sort((a:any,b:any)=> String(a.created_at||'').localeCompare(String(b.created_at||'')));
    for(const r of sorted){ const v=r.value as Mutation; if(v && v.kind){ const out=applyMutationTo(nc,ne,nm,v); nc=out.claims; ne=out.entities; nm=out.merged; } }
    setClaims(nc); setEntities(ne); setMerged(nm);
    if(recs.length) setLoadedMsg(`${recs.length} saved action${recs.length===1?'':'s'} replayed from the shared store.`);
  }
  async function createRow(row:Mutation){
    const key = `${row.kind}-${row.claimId||'none'}-${row.evidenceId||'none'}-${row.at}`;
    return await callApi("records.create", {collection:'hermes_mutations', value:row, idempotency_key:key});
  }
  useEffect(()=>{ reloadRecords().catch(()=>{ setLoadedMsg('Shared store unavailable — showing demo data in this preview. Actions still update the view.'); }); },[]);

  async function persistAndApply(m:Mutation){
    setBusy(true); setSaveMsg('Saving…');
    try{ await createRow(m); const out=applyMutationTo(claims,entities,merged,m); setClaims(out.claims); setEntities(out.entities); setMerged(out.merged); setSaveMsg('Saved to the shared store.'); }
    catch(e){ const out=applyMutationTo(claims,entities,merged,m); setClaims(out.claims); setEntities(out.entities); setMerged(out.merged); setSaveMsg('Preview only — shared store unavailable, so this change was applied to the view but not saved.'); }
    setBusy(false);
  }

  const ownerEntities = useMemo(()=> entities.filter(e=>e.owner===owner && !(merged && e.id==='e-acme')),[entities,owner,merged]);
  const ownerClaims = useMemo(()=> claims.filter(c=>c.owner===owner),[claims,owner]);
  const filteredEntities = useMemo(()=> ownerEntities.filter(e=> (!entitySearch || e.name.toLowerCase().includes(entitySearch.toLowerCase())) && (typeFilter.length===0 || typeFilter.includes(e.type)) && (!globalSearch || e.name.toLowerCase().includes(globalSearch.toLowerCase())) ),[ownerEntities,entitySearch,typeFilter,globalSearch]);
  const claimCountFor = (id:string)=> ownerClaims.filter(c=>c.subject_id===id||c.object_id===id).length;
  const groundedCountFor = (id:string)=> ownerClaims.filter(c=>(c.subject_id===id||c.object_id===id)&&c.status==='grounded').length;
  const stats = useMemo(()=>({entities:ownerEntities.length, grounded:ownerClaims.filter(c=>c.status==='grounded').length, unconfirmed:ownerClaims.filter(c=>c.status==='unconfirmed').length, orphaned:ownerClaims.filter(c=>c.status==='orphaned').length}),[ownerEntities,ownerClaims]);
  const typeCounts = useMemo(()=>{ const m:Record<string,number>={}; ownerEntities.forEach(e=>m[e.type]=(m[e.type]||0)+1); return m; },[ownerEntities]);

  // Graph derivation — traversal only grounded + valid_to IS NULL (§4.3 / §6.3)
  const groundedCurrentClaims = useMemo(()=> ownerClaims.filter(c=>c.status==='grounded' && c.valid_to==null),[ownerClaims]);
  const visibleClaims = useMemo(()=> ownerClaims.filter(c=> (statusFilter==='all'||c.status===statusFilter) && c.status!=='orphaned' || (statusFilter==='orphaned'&&c.status==='orphaned')).filter(c=>{ if(c.status==='orphaned') return statusFilter==='all'||statusFilter==='orphaned'; return true; }),[ownerClaims,statusFilter]);
  const graphNodes = useMemo(()=>{
    let nodes = ownerEntities;
    if(focusNode){ const neigh=new Set<string>([focusNode]); ownerClaims.forEach(c=>{ if(c.subject_id===focusNode&&c.object_id) neigh.add(c.object_id); if(c.object_id===focusNode) neigh.add(c.subject_id); }); nodes=nodes.filter(n=>neigh.has(n.id)); }
    return nodes;
  },[ownerEntities,focusNode,ownerClaims]);
  const positions = useMemo(()=>{ const map:Record<string,{x:number;y:number}>={}; const n=graphNodes.length; graphNodes.forEach((node,i)=>{ const ang=(i/n)*Math.PI*2 - Math.PI/2; const rx= i%2===0?300:210; const ry= i%2===0?210:150; map[node.id]={x:400+Math.cos(ang)*rx, y:280+Math.sin(ang)*ry}; }); return map; },[graphNodes]);
  const literalNodes = useMemo(()=>{ if(!showLiterals) return []; const seen=new Map<string,string>(); visibleClaims.forEach(c=>{ if(c.object_type==='literal'&&c.object_literal) seen.set(c.object_literal,c.object_literal); }); return [...seen.keys()].map((lit,i)=>({id:'lit:'+lit,label:lit,x: 640 + (i%3)*22, y: 50 + i*48})); },[visibleClaims,showLiterals]);
  const traversal = useMemo(()=>{
    if(!selectedEntity) return new Set<string>();
    const seen=new Set<string>([selectedEntity]); let frontier=[selectedEntity];
    for(let d=0; d<hop; d++){ const next:string[]=[]; groundedCurrentClaims.forEach(c=>{ if(frontier.includes(c.subject_id)&&c.object_id&&!seen.has(c.object_id)){seen.add(c.object_id); next.push(c.object_id);} if(frontier.includes(c.object_id||'')&&!seen.has(c.subject_id)){seen.add(c.subject_id); next.push(c.subject_id);} }); frontier=next; }
    return seen;
  },[selectedEntity,hop,groundedCurrentClaims]);
  const traversalEdges = useMemo(()=> new Set(groundedCurrentClaims.filter(c=> c.object_id && traversal.has(c.subject_id) && traversal.has(c.object_id)).map(c=>c.id)),[groundedCurrentClaims,traversal]);

  const filteredClaims = useMemo(()=>{
    let list = ownerClaims.filter(c=> (claimStatus==='all'||c.status===claimStatus) && (claimVerb==='all'||c.verb===claimVerb) && (!currentOnly || c.valid_to==null) && (extractorFilter==='all' || c.evidence.some(e=>e.extractor===extractorFilter)) && (!claimSearch && !globalSearch || c.fact_text.toLowerCase().includes((claimSearch||globalSearch).toLowerCase()) ));
    list=[...list].sort((a,b)=>{ const av=String((a as any)[sortKey]||''); const bv=String((b as any)[sortKey]||''); return av.localeCompare(bv)*sortDir; });
    return list;
  },[ownerClaims,claimStatus,claimVerb,claimSearch,globalSearch,sortKey,sortDir,currentOnly,extractorFilter]);
  const verbs = useMemo(()=> [...new Set(ownerClaims.map(c=>c.verb))],[ownerClaims]);
  const selectedEnt = entities.find(e=>e.id===selectedEntity && e.owner===owner) || ownerEntities[0];
  const selectedCl = claims.find(c=>c.id===selectedClaim);
  const entName = (id?:string)=> entities.find(e=>e.id===id)?.name || (id||'—');
  const objLabel = (c:Claim)=> c.object_type==='literal'? (c.object_literal||'') : entName(c.object_id);
  const selectedEntityPrimarySpan = useMemo(()=>{
    if(!selectedEnt) return '';
    const rel = ownerClaims.filter(c=>c.subject_id===selectedEnt.id || c.object_id===selectedEnt.id);
    return rel.length ? rel[0].span_id : '';
  },[ownerClaims,selectedEnt]);
  const passportSpans = useMemo(()=>{
    if(!selectedEnt) return [] as string[];
    const spans: string[] = [];
    ownerClaims.filter(c=>c.subject_id===selectedEnt.id || c.object_id===selectedEnt.id).forEach(c=>{ if(!spans.includes(c.span_id)) spans.push(c.span_id); });
    return spans;
  },[ownerClaims,selectedEnt]);
  const provenanceValue = selectedCl ? selectedCl.span_id : selectedEntityPrimarySpan;

  // Live Query Playground
  const queryResult = useMemo(()=>{
    const norm = (w:string)=> w==='lives'?'live': w==='living'?'live': w==='lived'?'live': w;
    const tokens = query.toLowerCase().replace(/[^a-z0-9 ]/g,' ').split(/\s+/).filter(Boolean).map(norm);
    const tokenSet = new Set(tokens);
    const bigrams: string[] = [];
    for(let i=0;i<tokens.length-1;i++) bigrams.push(tokens[i]+' '+tokens[i+1]);
    const linked = ownerEntities.filter(e=>{
      const name = e.name.toLowerCase();
      const aliasText = e.aliases.join(' ').toLowerCase();
      if(tokens.some(t=> t.length>2 && (name.includes(t) || aliasText.includes(t)))) return true;
      if(bigrams.some(b=> name.includes(b))) return true;
      return false;
    });
    const linkedIds = new Set(linked.map(e=>e.id));
    // 2-hop over grounded + valid_to IS NULL only
    const seen = new Set<string>([...linkedIds]); let frontier=[...linkedIds];
    for(let d=0; d<2; d++){ const next:string[]=[]; groundedCurrentClaims.forEach(c=>{ if(frontier.includes(c.subject_id)&&c.object_id&&!seen.has(c.object_id)){seen.add(c.object_id); next.push(c.object_id);} if(frontier.includes(c.object_id||'')&&!seen.has(c.subject_id)){seen.add(c.subject_id); next.push(c.subject_id);} }); frontier=next; }
    const pathClaims = groundedCurrentClaims.filter(c=> seen.has(c.subject_id) && (c.object_id ? seen.has(c.object_id) : true) && (linkedIds.has(c.subject_id) || (c.object_id && linkedIds.has(c.object_id))));
    const packed = groundedCurrentClaims.filter(c=> linkedIds.has(c.subject_id) || (c.object_id && linkedIds.has(c.object_id)));
    const score = (c:Claim)=>{ const text = (c.fact_text+' '+c.verb+' '+objLabel(c)).toLowerCase().replace(/[^a-z0-9 ]/g,' ').split(/\s+/).map(norm); return tokens.filter(t=> text.includes(t)).length; };
    const vectorPick = ownerClaims.length ? [...ownerClaims].sort((a,b)=> score(b)-score(a) || a.event_date.localeCompare(b.event_date))[0] : null;
    const hybridPick = packed.length ? [...packed].sort((a,b)=> score(b)-score(a))[0] : (pathClaims[0]||null);
    const fused = [...packed].sort((a,b)=> score(b)-score(a)).slice(0,5).map((c,i)=>({claim:c, rank:i+1, score:score(c)}));
    return {tokens, linked, seen, pathClaims, packed, vectorPick, hybridPick, fused, score};
  },[query,ownerEntities,ownerClaims,groundedCurrentClaims]);
  const entNameForQuery = (id?:string)=> entName(id);

  function zoomBy(factor:number){
    setPan(p=>{ const newK = Math.min(2, Math.max(0.6, p.k*factor)); const cx=400, cy=280; const ratio = newK/p.k; return {k:newK, x: cx - (cx - p.x)*ratio, y: cy - (cy - p.y)*ratio}; });
  }
  function handleHover(e:React.MouseEvent, n:Entity){
    const rect = wrapperRef.current?.getBoundingClientRect();
    if(!rect) return;
    const x = e.clientX - rect.left; const y = e.clientY - rect.top;
    setHover({name:n.name,type:n.type,grounded:groundedCountFor(n.id),x:Math.min(Math.max(8, x), 560), y:Math.min(Math.max(8, y), 480)});
  }

  async function doCopy(text:string){
    const val = text || provenanceValue || '';
    if(!val) return;
    try{ await navigator.clipboard.writeText(val); setCopyDone('Copied '+val); }
    catch{
      if(copyRef.current){ copyRef.current.value=val; copyRef.current.select(); try{ document.execCommand('copy'); setCopyDone('Copied '+val); }catch{ setCopyDone(val); } }
    }
  }
  function claimAction(kind:'confirm'|'retract', claim:Claim){ persistAndApply({kind, claimId:claim.id, at:nowISO()}); }
  function retractEv(claim:Claim, evId:string){ persistAndApply({kind:'retract_evidence', claimId:claim.id, evidenceId:evId, at:nowISO()}); }
  function mergeDemo(){ if(merged) return; persistAndApply({kind:'merge', at:nowISO()}); }
  function retractNewest(){ persistAndApply({kind:'retract_newest', at:nowISO()}); }
  function reassertAustin(){ persistAndApply({kind:'reassert', at:nowISO()}); }

  const ownerJobs = useMemo(()=> jobs.filter(j=>j.owner===owner),[jobs,owner]);
  function jobClaim(){ const j=ownerJobs.find(x=>x.status==='pending'); if(!j){setJobMsg('No pending job to claim.');return;} const num = parseInt(j.fence.replace('f-',''),10)||0; const newFence='f-'+(num+1); setJobs(jobs.map(x=>x.id===j.id?{...x,status:'leased',lease:'05:00 left',fence:newFence,last_error:''}:x)); setJobMsg(`Claimed ${j.op_id} (${j.kind}). Fence ${j.fence} → ${newFence} held.`); }
  function jobHeartbeat(){ const j=ownerJobs.find(x=>x.status==='leased'); if(!j){setJobMsg('No leased job to heartbeat.');return;} setJobs(jobs.map(x=>x.id===j.id?{...x,lease:'05:00 left'}:x)); setJobMsg(`Heartbeat sent for ${j.op_id}. Lease extended.`); }
  function jobPublish(){ const j=ownerJobs.find(x=>x.status==='leased'); if(!j){setJobMsg('Claim a job before publishing.');return;} setJobs(jobs.map(x=>x.id===j.id?{...x,status:'done',lease:'—',last_error:''}:x)); setJobMsg(`Published ${j.op_id} with current fence ${j.fence}. Outbox marked done.`); }
  function jobStalePublish(){ const j=ownerJobs.find(x=>x.status==='leased'); if(!j){setJobMsg('No leased job to publish.');return;} setJobs(jobs.map(x=>x.id===j.id?{...x,last_error:'Fence mismatch — publish aborted'}:x)); setJobMsg(`Fence mismatch — publish aborted, nothing written (§5.1). Tried stale fence for ${j.op_id}; current fence is ${j.fence}.`); }
  function jobFail(){ const j=[...ownerJobs].reverse().find(x=>x.status==='leased'||x.status==='pending'); if(!j){setJobMsg('No active job to fail.');return;} setJobs(jobs.map(x=>x.id===j.id?{...x,status:'retryable',attempts:x.attempts+1,lease:'—',last_error:'Publish failed — marked retryable'}:x)); setJobMsg(`Marked ${j.op_id} retryable. Attempts now ${j.attempts+1}.`); }
  function runQuery(){ setQueryRan(true); }

  const timelineGroups = useMemo(()=>{
    const groups:Record<string,Claim[]>={};
    ownerClaims.filter(c=>FUNCTIONAL.includes(c.verb)).forEach(c=>{ const k=c.subject_id+'|'+c.verb; groups[k]=groups[k]||[]; groups[k].push(c); });
    return Object.entries(groups).map(([k,cs])=>({key:k, subject:cs[0].subject_id, verb:cs[0].verb, claims:[...cs].sort((a,b)=>(a.valid_from||'').localeCompare(b.valid_from||''))}));
  },[ownerClaims]);
  const timeToPct = (d:string)=>{ const start=new Date('2025-06-01').getTime(); const end=new Date('2026-06-01').getTime(); return Math.max(0,Math.min(100, ((new Date(d).getTime()-start)/(end-start))*100)); };

  // Parallel-edge grouping for the canvas
  const edgeGroups = useMemo(()=>{
    const map = new Map<string, Claim[]>();
    visibleClaims.filter(c=>c.object_type==='entity'&&c.object_id&&positions[c.subject_id]&&positions[c.object_id]).forEach(c=>{
      const key = c.subject_id+'|'+c.object_id;
      const arr = map.get(key)||[]; arr.push(c); map.set(key,arr);
    });
    return map;
  },[visibleClaims,positions]);
  const edgeIndex = useMemo(()=>{
    const m = new Map<string,{index:number;total:number}>();
    edgeGroups.forEach(arr=> arr.forEach((c,i)=> m.set(c.id,{index:i,total:arr.length})));
    return m;
  },[edgeGroups]);

  return (
  <div data-safe-area-inset-root style={{boxSizing:'border-box',paddingTop:'var(--safe-area-inset-top)',minHeight:'100vh',background:'var(--bg)'}}>
    <header data-safe-area-sticky-top style={{top:'var(--safe-area-inset-top)'}} className="sticky z-30 border-b" >
      <div style={{background:'var(--surface)'}} className="px-3 sm:px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0" style={{background:'var(--ink)',color:'var(--bg)'}}><GitBranch size={18}/></div>
            <div className="min-w-0"><h1 className="font-display text-[clamp(1.25rem,3vw,1.7rem)] leading-none font-bold truncate">Hermes Graph Explorer</h1><p className="text-[11px] mono mt-1" style={{color:'var(--muted)'}}>MEMORY PROVIDER · PLUGIN GRAPH LAYER · OWNER-SCOPED</p></div>
          </div>
          <div className="flex items-center gap-2 ml-auto flex-wrap">
            <div className="flex rounded-full p-1" style={{background:'var(--surface-2)'}}>
              {(['raul','maya-demo'] as const).map(o=> <button key={o} onClick={()=>{setOwner(o); setSelectedEntity(o==='raul'?'e-raul':'m-maya'); setSelectedClaim(null); setFocusNode(null);}} className="px-3 py-1.5 rounded-full text-xs font-semibold" style={owner===o?{background:'var(--ink)',color:'var(--bg)'}:{color:'var(--muted)'}}>{o}</button>)}
            </div>
            <div className="flex rounded-full p-1" style={{background:'var(--surface-2)'}}>
              {(['vector','shadow','hybrid'] as const).map(m=> <button key={m} onClick={()=>setMode(m)} className="px-3 py-1.5 rounded-full text-xs font-semibold capitalize" style={mode===m?{background:'var(--surface)',border:'1px solid var(--line)'}:{color:'var(--muted)'}}>{m}</button>)}
            </div>
            <label className="flex items-center gap-2 rounded-full px-3 py-2 text-sm min-w-[180px] flex-1 sm:flex-none" style={{border:'1px solid var(--line)',background:'var(--surface)'}}><Search size={15}/><input value={globalSearch} onChange={e=>setGlobalSearch(e.target.value)} placeholder="Search graph…" className="bg-transparent outline-none w-full text-sm" aria-label="Global search"/></label>
          </div>
        </div>
        <div className="flex flex-wrap gap-2 mt-3">
          {[{l:'Entities',v:stats.entities},{l:'Grounded Claims',v:stats.grounded},{l:'Unconfirmed',v:stats.unconfirmed},{l:'Orphaned',v:stats.orphaned}].map(s=> <span key={s.l} className="chip rounded-full px-3 py-1 text-xs font-semibold">{s.l} <b className="ml-1">{s.v}</b></span>)}
          <span className="rounded-full px-3 py-1 text-xs" style={{background:'var(--accent-tint)',color:'var(--ink)'}}>Retrieval: {mode} · Owner: {owner} · No cross-owner leakage</span>
          <span className="text-xs self-center" style={{color:'var(--muted)'}}>Default is vector until §9 shadow gates pass (§6 / §7.3). {loadedMsg} {saveMsg}</span>
        </div>
      </div>
    </header>

    <main className="flex flex-col lg:flex-row items-start gap-0 overflow-x-clip">
      <aside className="w-full lg:w-[280px] shrink-0 p-4" style={{background:'var(--sidebar)',color:'#EDEFF2'}}>
        <p className="text-[11px] tracking-[0.14em] font-semibold text-white/60">ENTITY BROWSER</p>
        <label className="mt-3 flex items-center gap-2 rounded-lg px-3 py-2 text-sm" style={{background:'var(--sidebar-2)'}}><Search size={15}/><input value={entitySearch} onChange={e=>setEntitySearch(e.target.value)} placeholder="Search entities" className="bg-transparent outline-none w-full text-white placeholder:text-white/50" aria-label="Search entities"/></label>
        <div className="mt-4 space-y-1">
          {(['person','project','place','org','tool','concept','other'] as EntType[]).map(t=>{ const active=typeFilter.includes(t); return <button key={t} onClick={()=>setTypeFilter(active?typeFilter.filter(x=>x!==t):[...typeFilter,t])} className="w-full flex items-center justify-between rounded-lg px-2 py-1.5 text-sm" style={{background:active?'#2A3442':'transparent'}}><span className="flex items-center gap-2 capitalize"><span style={{width:9,height:9,borderRadius:99,background:TYPE_COLOR[t],display:'inline-block'}}/>{t}</span><span className="mono text-xs text-white/60">{typeCounts[t]||0}</span></button> })}
        </div>
        <div className="mt-4 space-y-2">
          {filteredEntities.map(e=>{ const sel=selectedEntity===e.id; return <button key={e.id} onClick={()=>{setSelectedEntity(e.id); setSelectedClaim(null); setTab('graph');}} className="w-full text-left rounded-xl p-3 border" style={{background:sel?'var(--surface)':'transparent',color:sel?'var(--ink)':'#EDEFF2',borderColor:sel?'var(--line)':'#2A3442'}}>
            <span className="flex items-center justify-between gap-2"><span className="font-semibold text-sm truncate">{e.name}</span><span className="text-[10px] px-2 py-0.5 rounded-full capitalize shrink-0" style={{background:TYPE_COLOR[e.type],color:'#fff'}}>{e.type}</span></span>
            <span className="text-xs block mt-1" style={{color:sel?'var(--muted)':'rgba(255,255,255,.6)'}}>{claimCountFor(e.id)} claims · {groundedCountFor(e.id)} grounded {e.aliases.length?`· aka ${e.aliases.join(', ')}`:''}</span>
          </button> })}
          {filteredEntities.length===0 && <p className="text-sm text-white/70 rounded-xl border border-dashed p-3" style={{borderColor:'#2A3442'}}>No entities match this filter. Clear the search or type filters.</p>}
        </div>
        <div className="mt-5 rounded-xl p-3" style={{background:'var(--sidebar-2)',border:'1px solid var(--line)'}}>
          <p className="text-sm font-semibold" style={{color:'var(--ink)'}}>Merge demo — Acme / Acme Corp</p>
          <p className="text-xs mt-1" style={{color:'var(--muted)'}}>Same org, two surface forms. Merging re-points claims from Acme to Acme Corp and consolidates aliases.</p>
          <button disabled={merged||owner!=='raul'||busy} onClick={mergeDemo} className="mt-3 w-full rounded-lg py-2 text-sm font-semibold flex items-center justify-center gap-2" style={{background:'#F2C94C',color:'#171B21'}}><GitMerge size={15}/>{merged?'Merged ✓ — Acme → Acme Corp':'Merge Acme → Acme Corp'}</button>
          {merged && <p className="text-xs mt-2" style={{color:'var(--muted)'}}>Claims re-pointed. Acme is hidden; Acme Corp keeps the consolidated aliases.</p>}
        </div>
        <p className="text-[11px] leading-relaxed mt-5 text-white/55">Evidence, not counters. A claim is grounded because live evidence supports it — not because it was repeated. Event time, not processing time. Timelines use when something happened.</p>
      </aside>

      <section className="flex-1 min-w-0 w-full p-3 sm:p-5">
        <div className="flex flex-wrap gap-1 rounded-full p-1 w-fit max-w-full" style={{background:'var(--surface)',border:'1px solid var(--line)'}}>
          {([['graph','Graph'],['claims','Claims'],['timelines','Timelines'],['query','Query Playground'],['jobs','Jobs & Health'],['shadow','Shadow Compare']] as const).map(([k,l])=> <button key={k} onClick={()=>setTab(k as any)} className="px-3 py-1.5 rounded-full text-xs sm:text-sm font-semibold whitespace-nowrap" style={tab===k?{background:'var(--ink)',color:'var(--bg)'}:{}}>{l}</button>)}
        </div>

        {tab==='graph' && (
        <div className="mt-4 rounded-2xl overflow-hidden" style={{background:'var(--surface)',border:'1px solid var(--line)'}}>
          <div className="flex flex-wrap items-center gap-2 p-3 border-b" style={{borderColor:'var(--line)'}}>
            <span className="text-sm font-semibold">Graph canvas</span>
            <select value={statusFilter} onChange={e=>setStatusFilter(e.target.value as any)} className="text-xs rounded-full px-2 py-1.5" style={{border:'1px solid var(--line)',background:'var(--surface)'}} aria-label="Filter by status"><option value="all">All statuses</option><option value="grounded">Grounded</option><option value="unconfirmed">Unconfirmed</option><option value="retracted">Retracted</option><option value="orphaned">Orphaned</option></select>
            <label className="text-xs flex items-center gap-1"><input type="checkbox" checked={showLiterals} onChange={e=>setShowLiterals(e.target.checked)}/> Literals</label>
            <div className="flex rounded-full overflow-hidden text-xs" style={{border:'1px solid var(--line)'}}><button onClick={()=>setHop(1)} className="px-2 py-1" style={{background:hop===1?'var(--ink)':'transparent',color:hop===1?'var(--bg)':''}}>1-hop</button><button onClick={()=>setHop(2)} className="px-2 py-1" style={{background:hop===2?'var(--ink)':'transparent',color:hop===2?'var(--bg)':''}}>2-hop</button></div>
            <div className="flex gap-1 ml-auto"><button onClick={()=>zoomBy(1.2)} className="p-2 rounded-lg border" style={{borderColor:'var(--line)'}} aria-label="Zoom in"><ZoomIn size={15}/></button><button onClick={()=>zoomBy(1/1.2)} className="p-2 rounded-lg border" style={{borderColor:'var(--line)'}} aria-label="Zoom out"><ZoomOut size={15}/></button><button onClick={()=>{setPan({x:0,y:0,k:1}); setFocusNode(null);}} className="px-2 py-2 rounded-lg border text-xs flex items-center gap-1" style={{borderColor:'var(--line)'}}><RotateCcw size={14}/> Reset view</button></div>
          </div>
          <p className="text-xs px-3 pt-2" style={{color:'var(--muted)'}}>Traversal: grounded + valid_to IS NULL only (§4.3 / §6.3)</p>
          <div ref={wrapperRef} className="relative" style={{background:'var(--graph-bg)'}}>
            <svg viewBox="0 0 800 560" className="w-full h-[520px] sm:h-[560px] touch-none select-none" onMouseDown={e=>{dragRef.current={x:e.clientX,y:e.clientY};}} onMouseMove={e=>{ if(dragRef.current){ setPan(p=>({ ...p, x:p.x+(e.clientX-dragRef.current!.x), y:p.y+(e.clientY-dragRef.current!.y)})); dragRef.current={x:e.clientX,y:e.clientY}; } }} onMouseUp={()=>dragRef.current=null} onMouseLeave={()=>{dragRef.current=null; setHover(null);}}>
              <g transform={`translate(${pan.x},${pan.y}) scale(${pan.k})`}>
                {visibleClaims.filter(c=>c.object_type==='entity'&&c.object_id&&positions[c.subject_id]&&positions[c.object_id]).map(c=>{ const a=positions[c.subject_id]; const b=positions[c.object_id!]; const isTrav=traversalEdges.has(c.id); const info=edgeIndex.get(c.id)||{index:0,total:1}; const offset=(info.index-(info.total-1)/2)*22; const dx=b.x-a.x, dy=b.y-a.y; const len=Math.hypot(dx,dy)||1; const px=-dy/len*offset, py=dx/len*offset; const cx=(a.x+b.x)/2+px, cy=(a.y+b.y)/2+py; const lx=0.25*a.x+0.5*cx+0.25*b.x, ly=0.25*a.y+0.5*cy+0.25*b.y; return <g key={c.id}><path d={`M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`} fill="none" stroke={isTrav?'#E85D2A':STATUS_COLOR[c.status]} strokeWidth={isTrav?3:1.4} opacity={isTrav?1:0.55}/><text x={lx} y={ly-6} fontSize="11" fill={isTrav?'#E85D2A':'#4B5563'} textAnchor="middle" style={{fontFamily:'IBM Plex Mono'}}>{c.verb}</text></g> })}
                {visibleClaims.filter(c=>c.object_type==='literal'&&showLiterals).map((c,i)=>{ const a=positions[c.subject_id]; if(!a) return null; const lit=literalNodes.find(l=>l.label===c.object_literal); if(!lit) return null; const isTrav=selectedEntity===c.subject_id; return <g key={c.id}><line x1={a.x} y1={a.y} x2={lit.x} y2={lit.y} stroke={isTrav?'#E85D2A':'#9AA3AF'} strokeDasharray="3 3"/><rect x={lit.x-38} y={lit.y-12} width={76} height={24} rx={5} fill="var(--surface)" stroke={isTrav?'#E85D2A':'#D1D5DB'}/><text x={lit.x} y={lit.y+4} fontSize="10" textAnchor="middle" fill="var(--ink)">{c.object_literal}</text></g> })}
                {graphNodes.map(n=>{ const p=positions[n.id]; if(!p) return null; const deg=claimCountFor(n.id); const sel=n.id===selectedEntity; const inTrav=traversal.has(n.id); return <g key={n.id} className="graph-node" transform={`translate(${p.x},${p.y})`} onClick={()=>{setSelectedEntity(n.id); setSelectedClaim(null);}} onDoubleClick={()=>setFocusNode(n.id)} onMouseEnter={e=>handleHover(e,n)} onMouseMove={e=>handleHover(e,n)} onMouseLeave={()=>setHover(null)} style={{cursor:'pointer'}}><circle r={22+Math.min(14,deg*2)} fill={TYPE_COLOR[n.type]} stroke={sel?'#E85D2A':inTrav?'#E85D2A':'#fff'} strokeWidth={sel||inTrav?4:2}/><text y={4} textAnchor="middle" fontSize="11" fontWeight="700" fill="#fff">{n.name.split(' ').map(w=>w[0]).slice(0,2).join('')}</text><text y={38+Math.min(14,deg*2)} textAnchor="middle" fontSize="11" fontWeight="600" fill="var(--ink)">{n.name}</text><text y={52+Math.min(14,deg*2)} textAnchor="middle" fontSize="9" fill="var(--muted)" className="capitalize">{n.type} · {deg} claims</text></g> })}
              </g>
            </svg>
            {hover && <div className="absolute pointer-events-none rounded-xl px-3 py-2 text-xs shadow-lg" style={{left:hover.x,top:hover.y,background:'var(--ink)',color:'var(--bg)',maxWidth:220}}><b>{hover.name}</b> · <span className="capitalize">{hover.type}</span><br/>{hover.grounded} grounded claims · Click to select · Double-click to expand 1 hop</div>}
            <div className="absolute left-3 bottom-3 rounded-xl px-3 py-2 text-[11px] flex flex-wrap gap-3" style={{background:'var(--surface)',color:'var(--ink)',border:'1px solid var(--line)'}}><span><b>Legend</b></span>{(['person','project','place','org','tool','concept'] as EntType[]).map(t=> <span key={t} className="flex items-center gap-1 capitalize"><span style={{width:8,height:8,borderRadius:99,background:TYPE_COLOR[t],display:'inline-block'}}/>{t}</span>)}<span className="flex items-center gap-1"><span style={{width:14,height:2,background:'#E85D2A',display:'inline-block'}}/> traversal</span><span>□ literal</span></div>
          </div>
          <p className="text-xs p-3" style={{color:'var(--muted)'}}>Drag to pan. Click a node to select it; double-click to focus its 1-hop neighbourhood. Orange edges are the {hop}-hop traversal from <b>{selectedEnt?.name}</b>. {focusNode?`Focused on ${entName(focusNode)} — Reset view restores the full graph.`:''}</p>
        </div>
        )}

        {tab==='claims' && (
        <div className="mt-4 rounded-2xl overflow-hidden" style={{background:'var(--surface)',border:'1px solid var(--line)'}}>
          <div className="flex flex-wrap gap-2 p-3 border-b" style={{borderColor:'var(--line)'}}><input value={claimSearch} onChange={e=>setClaimSearch(e.target.value)} placeholder="Filter facts…" className="rounded-full px-3 py-2 text-sm flex-1 min-w-[180px]" style={{border:'1px solid var(--line)'}} aria-label="Filter claims"/><select value={claimStatus} onChange={e=>setClaimStatus(e.target.value as any)} className="text-sm rounded-full px-2 py-2" style={{border:'1px solid var(--line)'}}><option value="all">All statuses</option><option value="grounded">Grounded</option><option value="unconfirmed">Unconfirmed</option><option value="retracted">Retracted</option><option value="orphaned">Orphaned</option></select><select value={claimVerb} onChange={e=>setClaimVerb(e.target.value)} className="text-sm rounded-full px-2 py-2" style={{border:'1px solid var(--line)'}}><option value="all">All verbs</option>{verbs.map(v=><option key={v} value={v}>{v}</option>)}</select><label className="text-xs flex items-center gap-1 self-center"><input type="checkbox" checked={currentOnly} onChange={e=>setCurrentOnly(e.target.checked)}/> Current only (valid_to IS NULL)</label><label className="text-xs flex items-center gap-1 self-center">Extractor: <select value={extractorFilter} onChange={e=>setExtractorFilter(e.target.value as any)} className="text-sm rounded-full px-2 py-1" style={{border:'1px solid var(--line)'}}><option value="all">all</option><option value="rules">rules</option><option value="model">model</option><option value="uptake">uptake</option><option value="legacy">legacy</option></select></label><span className="text-xs self-center" style={{color:'var(--muted)'}}>{filteredClaims.length} claims</span></div>
          <p className="text-xs px-3 pt-2" style={{color:'var(--muted)'}}>Orphaned = no live evidence remains (all supporting evidence retracted or superseded). It does not mean the object entity is missing.</p>
          <div className="overflow-x-auto"><table className="w-full text-sm min-w-[760px]"><thead><tr className="text-left text-xs" style={{color:'var(--muted)'}}>{([['fact_text','Fact'],['subject_id','Subject'],['verb','Verb'],['object_id','Object'],['status','Status'],['event_date','Event Date'],['span_id','Span ID']] as const).map(([k,l])=> <th key={k} className="px-3 py-2 font-medium"><button onClick={()=>{ if(sortKey===k) setSortDir(sortDir===1?-1:1); else {setSortKey(k as any); setSortDir(1);} }} className="flex items-center gap-1">{l} <ChevronDown size={12}/></button></th>)}<th className="px-3 py-2 font-medium">Evidence</th></tr></thead><tbody>{filteredClaims.map(c=> <tr key={c.id} onClick={()=>{setSelectedClaim(c.id); setSelectedEntity(c.subject_id);}} className="border-t cursor-pointer hover:bg-black/5" style={{borderColor:'var(--line)',background:selectedClaim===c.id?'var(--callout)':''}}><td className="px-3 py-2.5 max-w-[280px]"><span className="block truncate">{c.fact_text}</span><span className="mono text-[10px]" style={{color:'var(--muted)'}}>{c.id}</span></td><td className="px-3 py-2.5">{entName(c.subject_id)}</td><td className="px-3 py-2.5 mono text-xs">{c.verb}</td><td className="px-3 py-2.5">{objLabel(c)}</td><td className="px-3 py-2.5"><span className="text-[11px] px-2 py-1 rounded-full text-white capitalize" style={{background:STATUS_COLOR[c.status]}}>{c.status}</span></td><td className="px-3 py-2.5 mono text-xs">{c.event_date}</td><td className="px-3 py-2.5 mono text-xs">{c.span_id}</td><td className="px-3 py-2.5">{c.evidence.length}</td></tr>)}</tbody></table></div>
          {filteredClaims.length===0 && <p className="p-6 text-sm" style={{color:'var(--muted)'}}>No claims match. Try clearing filters — orphaned claims still appear when you filter to Orphaned.</p>}
        </div>
        )}

        {tab==='timelines' && (
        <div className="mt-4 space-y-4">
          <div className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><h2 className="font-display text-xl font-bold">Functional timelines</h2><p className="text-sm mt-1" style={{color:'var(--muted)'}}>Functional verbs keep one live interval at a time. Supersession closes the old interval and opens the new one. Event time — not processing time — drives the bar. Mutations append evidence and recompute the whole chain.</p></div>
          {timelineGroups.map(g=> <div key={g.key} className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><div className="flex flex-wrap items-center gap-3"><h3 className="font-semibold">{entName(g.subject)} <span className="mono text-xs font-normal">· {g.verb}</span></h3>{g.subject==='e-raul'&&g.verb==='based_in'&&owner==='raul'&& <div className="flex gap-2 ml-auto"><button disabled={busy} onClick={retractNewest} className="px-3 py-1.5 rounded-full text-xs font-semibold border" style={{borderColor:'var(--line)'}}>Retract newest</button><button disabled={busy} onClick={reassertAustin} className="px-3 py-1.5 rounded-full text-xs font-semibold" style={{background:'var(--accent)',color:'#fff'}}>Re-assert Austin</button></div>}</div>
            <div className="mt-4 space-y-3">{g.claims.map(c=>{ const from=c.valid_from||c.event_date; const to=c.valid_to||'2026-06-01'; const left=timeToPct(from); const width=Math.max(4,timeToPct(to)-left); return <div key={c.id}><div className="flex flex-wrap justify-between gap-2 text-xs"><span><b>{objLabel(c)}</b> · <span className="capitalize" style={{color:STATUS_COLOR[c.status]}}>{c.status}</span> · {c.id}</span><span className="mono">valid_from {from} · valid_to {c.valid_to||'now'}</span></div><div className="relative h-7 rounded-full mt-1" style={{background:'var(--surface-2)'}}><div className="absolute top-0 h-7 rounded-full flex items-center px-2 text-[11px] font-semibold text-white whitespace-nowrap overflow-hidden" style={{left:left+'%',width:width+'%',background:STATUS_COLOR[c.status],minWidth:60}}>{objLabel(c)}</div></div></div> })}</div>
            <p className="text-xs mt-3 mono" style={{color:'var(--muted)'}}>Superseded chain: {g.claims.map(c=>c.id).join('  →  superseded_by  →  ')} {g.claims.some(c=>c.superseded_by)?'· arrows follow superseded_by':''}</p>
          </div>)}
          {timelineGroups.length===0 && <p className="rounded-2xl p-5 text-sm" style={{background:'var(--surface)',border:'1px solid var(--line)'}}>No functional intervals for this owner.</p>}
        </div>
        )}

        {tab==='query' && (
        <div className="mt-4 grid gap-4">
          <div className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><label className="text-sm font-semibold">Query Playground — {mode} retrieval</label><div className="flex flex-col sm:flex-row gap-2 mt-3"><input value={query} onChange={e=>{setQuery(e.target.value); setQueryRan(false);}} className="flex-1 rounded-full px-4 py-3 text-sm" style={{border:'1px solid var(--line)'}} aria-label="Query"/><button onClick={runQuery} className="px-5 py-3 rounded-full text-sm font-semibold flex items-center justify-center gap-2" style={{background:'var(--ink)',color:'var(--bg)'}}><Play size={15}/> Run</button></div><p className="text-xs mt-2" style={{color:'var(--muted)'}}>Try “Where does Raul live?” · “Where is Maya based?” · “Who works for Acme Corp?” Live over current client state — retractions and re-assertions change the answer.</p></div>
          {queryRan ? <div className="grid md:grid-cols-2 gap-4">
            <div className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><h3 className="font-semibold flex items-center gap-2"><Eye size={15}/> Linked entities</h3>{queryResult.linked.length===0 ? <p className="text-sm mt-2" style={{color:'var(--muted)'}}>No entity matched the query tokens ({queryResult.tokens.join(', ')}).</p> : queryResult.linked.map(e=> <p key={e.id} className="text-sm mt-2">{e.name} <span className="mono text-xs">({e.type} · {e.id})</span></p>)}
            <p className="text-sm mt-3"><b>2-hop traversal path (grounded + valid_to IS NULL)</b></p><p className="mono text-xs mt-1 leading-relaxed">{queryResult.pathClaims.length ? queryResult.pathClaims.map(c=> `${entNameForQuery(c.subject_id)} —${c.verb}→ ${objLabel(c)}`).join('\n') : 'No grounded current path from the linked entities.'}</p>
            <div className="mt-4 rounded-xl p-3" style={{background:'var(--accent-tint)',color:'var(--ink)'}}><p className="text-xs font-bold tracking-wide">GROUNDED FACTS — AS PACKED FOR THE PROMPT</p>{queryResult.packed.length ? queryResult.packed.map(c=> <div key={c.id} className="mt-2"><p className="text-sm">{c.fact_text}</p><p className="mono text-xs mt-1" style={{color:'var(--muted)'}}>Event date: {c.event_date} · Span: {c.span_id} · Status: {c.status}</p></div>) : <p className="text-sm mt-2">No grounded current facts for the linked entities.</p>}</div></div>
            <div className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><h3 className="font-semibold">Supporting turns</h3><ul className="text-sm mt-2 space-y-2">{queryResult.packed.map(c=> <li key={c.id}>{c.span_id} — {c.fact_text}</li>)}{queryResult.packed.length===0 && <li>No supporting turns in live state.</li>}</ul><h3 className="font-semibold mt-4">RRF fusion preview</h3><p className="mono text-xs mt-1">{queryResult.fused.length ? queryResult.fused.map(f=> `#${f.rank} ${f.claim.fact_text} (token overlap ${f.score})`).join('\n') : 'No fused candidates.'}</p><h3 className="font-semibold mt-4">Token budget</h3><div className="h-2.5 rounded-full mt-2 overflow-hidden" style={{background:'var(--surface-2)'}}><div className="h-2.5 rounded-full" style={{width:'38%',background:'var(--accent)'}}/></div><p className="text-xs mt-1">{queryResult.packed.length*52} / 820 tokens used · live packed facts</p><div className="mt-4 rounded-xl p-3 text-sm" style={{background:'var(--callout)',color:'var(--ink)'}}><b>Vector-only would have said:</b> {queryResult.vectorPick ? `${queryResult.vectorPick.fact_text} (${queryResult.vectorPick.status}${queryResult.vectorPick.valid_to ? ', superseded' : ''})` : '—'}<br/><b>Hybrid says:</b> {queryResult.hybridPick ? queryResult.hybridPick.fact_text : 'No grounded current fact.'}</div></div>
          </div> : <p className="rounded-2xl p-5 text-sm" style={{background:'var(--surface)',border:'1px solid var(--line)',color:'var(--muted)'}}>Run a query to see linked entities, the traversal path, and the grounded facts block.</p>}
        </div>
        )}

        {tab==='jobs' && (
        <div className="mt-4 space-y-4">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">{[['Done',ownerJobs.filter(j=>j.status==='done').length],['Leased',ownerJobs.filter(j=>j.status==='leased').length],['Pending',ownerJobs.filter(j=>j.status==='pending').length],['Retryable',ownerJobs.filter(j=>j.status==='retryable').length]].map(([l,v])=> <div key={l as string} className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><p className="text-xs" style={{color:'var(--muted)'}}>{l}</p><p className="text-2xl font-bold">{v}</p></div>)}</div>
          <div className="flex flex-wrap gap-2"><button onClick={jobClaim} className="px-3 py-2 rounded-full text-xs font-semibold border" style={{borderColor:'var(--line)',background:'var(--surface)'}}>Claim next</button><button onClick={jobHeartbeat} className="px-3 py-2 rounded-full text-xs font-semibold border" style={{borderColor:'var(--line)',background:'var(--surface)'}}><HeartPulse size={13} className="inline mr-1"/>Heartbeat</button><button onClick={jobPublish} className="px-3 py-2 rounded-full text-xs font-semibold" style={{background:'var(--accent)',color:'#fff'}}>Publish</button><button onClick={jobStalePublish} className="px-3 py-2 rounded-full text-xs font-semibold border" style={{borderColor:'var(--line)',background:'var(--surface)'}}>Stale publish (wrong fence)</button><button onClick={jobFail} className="px-3 py-2 rounded-full text-xs font-semibold border" style={{borderColor:'var(--line)',background:'var(--surface)'}}>Fail → retryable</button>{jobMsg && <span className="text-xs self-center">{jobMsg}</span>}</div>
          <p className="text-xs" style={{color:'var(--muted)'}}>Jobs filtered by selected owner: {owner}. Claiming increments the fence; publishing with a stale fence is rejected.</p>
          <div className="rounded-2xl overflow-hidden" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><div className="overflow-x-auto"><table className="w-full text-sm min-w-[760px]"><thead><tr className="text-left text-xs" style={{color:'var(--muted)'}}><th className="px-3 py-2">Kind</th><th className="px-3 py-2">Op ID</th><th className="px-3 py-2">Status</th><th className="px-3 py-2">Fence</th><th className="px-3 py-2">Lease</th><th className="px-3 py-2">Attempts</th><th className="px-3 py-2">Owner</th><th className="px-3 py-2">Last Error</th></tr></thead><tbody>{ownerJobs.map(j=> <tr key={j.id} className="border-t" style={{borderColor:'var(--line)'}}><td className="px-3 py-2.5 capitalize">{j.kind}</td><td className="px-3 py-2.5 mono text-xs">{j.op_id}</td><td className="px-3 py-2.5"><span className="text-[11px] px-2 py-1 rounded-full text-white capitalize" style={{background:j.status==='done'?'#0E6B5A':j.status==='leased'?'#2B5EA6':j.status==='retryable'?'#B7791F':j.status==='failed'?'#B23A6B':'#6B7280'}}>{j.status}</span></td><td className="px-3 py-2.5 mono text-xs">{j.fence}</td><td className="px-3 py-2.5 text-xs">{j.lease}</td><td className="px-3 py-2.5">{j.attempts}</td><td className="px-3 py-2.5 text-xs">{j.owner}</td><td className="px-3 py-2.5 text-xs">{j.last_error||'—'}</td></tr>)}</tbody></table></div></div>
          <p className="text-xs flex items-center gap-2" style={{color:'var(--muted)'}}><Shield size={13}/> Fences prevent two workers from owning the same op. Leases expire; heartbeats renew them. Failed jobs stay retryable — they are not silently dropped.</p>
        </div>
        )}

        {tab==='shadow' && (
        <div className="mt-4 space-y-4">
          <div className="grid sm:grid-cols-3 gap-3"><div className="rounded-2xl p-4" style={{background:'#0E6B5A',color:'#fff'}}><p className="text-xs opacity-80">MULTI-HOP RECALL</p><p className="text-xl font-bold mt-1">Vector 0.25 → Hybrid 0.88</p></div><div className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><p className="text-xs" style={{color:'var(--muted)'}}>UNRELATED CONTEXT / ANSWER</p><p className="text-xl font-bold mt-1">Vector 9 → Hybrid 1</p></div><div className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><p className="text-xs" style={{color:'var(--muted)'}}>LATENCY p50 / p95</p><p className="text-xl font-bold mt-1">180 ms / 420 ms</p></div></div>
          {[
            {q:'Where does Raul live?',v:'Austin (stale interval)',h:'NYC (current interval, supersession-aware)',r:'Hybrid follows the superseded chain; vector matches the older text.'},
            {q:'Who works for Acme Corp?',v:'Raul only',h:'Raul + Maya (2-hop via org)',r:'Graph traversal finds both people attached to the same org.'},
            {q:'Where is Lumen based?',v:'Austin (nearest text)',h:'Houston (grounded claim)',r:'Vector drifts to the more frequent place; hybrid trusts the claim.'},
          ].map((row,i)=> <div key={i} className="rounded-2xl p-4 grid md:grid-cols-3 gap-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}><div><p className="text-xs" style={{color:'var(--muted)'}}>QUERY</p><p className="font-semibold mt-1">{row.q}</p></div><div><p className="text-xs" style={{color:'var(--muted)'}}>VECTOR</p><p className="text-sm mt-1">{row.v}</p></div><div><p className="text-xs" style={{color:'var(--muted)'}}>HYBRID</p><p className="text-sm mt-1 font-semibold">{row.h}</p><p className="text-xs mt-2" style={{color:'var(--muted)'}}>{row.r}</p></div></div>)}
          <p className="text-xs" style={{color:'var(--muted)'}}>Shadow mode runs both retrievers and compares them without changing what the user sees. Hybrid wins on multi-hop recall and on keeping unrelated context out.</p>
        </div>
        )}
      </section>

      <aside className="w-full lg:w-[340px] shrink-0 p-3 sm:p-4">
        <div className="rounded-2xl p-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}>
          <p className="text-[11px] tracking-[0.14em] font-semibold" style={{color:'var(--muted)'}}>DETAIL PANEL</p>
          {selectedCl ? (
            <div className="mt-3">
              <span className="text-[11px] px-2 py-1 rounded-full text-white capitalize" style={{background:STATUS_COLOR[selectedCl.status]}}>{selectedCl.status}</span>
              <h2 className="font-display text-xl font-bold leading-tight mt-2">{selectedCl.fact_text}</h2>
              {selectedCl.status==='orphaned' && <p className="text-xs mt-2" style={{color:'var(--muted)'}}>Orphaned = no live evidence remains. The object entity still exists; the source spans were deleted or retracted.</p>}
              <div className="mt-4 rounded-xl p-3 text-sm flex flex-wrap items-center gap-1" style={{background:'var(--surface-2)'}}><b>{entName(selectedCl.subject_id)}</b><ArrowRight size={14}/><span className="mono text-xs">{selectedCl.verb}</span><ArrowRight size={14}/><b>{objLabel(selectedCl)}</b></div>
              <dl className="text-xs mt-4 space-y-2"><div className="flex justify-between gap-2"><dt style={{color:'var(--muted)'}}>Event date</dt><dd className="mono">{selectedCl.event_date}</dd></div><div className="flex justify-between gap-2"><dt style={{color:'var(--muted)'}}>Span ID</dt><dd className="mono">{selectedCl.span_id}</dd></div><div className="flex justify-between gap-2"><dt style={{color:'var(--muted)'}}>Valid interval</dt><dd className="mono">{selectedCl.valid_from||'—'} → {selectedCl.valid_to||'now'}</dd></div><div className="flex justify-between gap-2"><dt style={{color:'var(--muted)'}}>Superseded by</dt><dd className="mono">{selectedCl.superseded_by||'—'}</dd></div></dl>
              <div className="grid grid-cols-2 gap-2 mt-4"><button disabled={busy} onClick={()=>claimAction('confirm',selectedCl)} className="py-2 rounded-full text-xs font-semibold flex items-center justify-center gap-1" style={{background:'#0E6B5A',color:'#fff'}}><Check size={14}/> Confirm</button><button disabled={busy} onClick={()=>claimAction('retract',selectedCl)} className="py-2 rounded-full text-xs font-semibold border" style={{borderColor:'var(--line)'}}>Retract</button></div>
              <h3 className="font-semibold text-sm mt-5 flex items-center gap-2"><Database size={14}/> Evidence · {selectedCl.evidence.length}</h3>
              <div className="space-y-2 mt-2">{selectedCl.evidence.map(e=> <div key={e.id} className="rounded-xl p-3 text-xs" style={{border:'1px solid var(--line)'}}><div className="flex justify-between gap-2"><b>{e.extractor} · {e.version}</b><span className="px-2 py-0.5 rounded-full text-[10px] text-white capitalize" style={{background:e.state==='live'?'#0E6B5A':e.state==='retracted'?'#B23A6B':'#6B7280'}}>{e.state}</span></div><p className="mt-1" style={{color:'var(--muted)'}}>Speaker: {e.speaker} · Act: {e.act}<br/>Event time: {e.event_time}{e.note ? <><br/>Note: {e.note}</> : null}</p><button disabled={e.state==='retracted'||busy} onClick={()=>retractEv(selectedCl,e.id)} className="mt-2 text-xs underline">Retract evidence</button></div>)}</div>
              <button onClick={()=>setSelectedClaim(null)} className="mt-4 text-xs underline">Back to entity detail</button>
            </div>
          ) : selectedEnt ? (
            <div className="mt-3">
              <span className="text-[11px] px-2 py-1 rounded-full text-white capitalize" style={{background:TYPE_COLOR[selectedEnt.type]}}>{selectedEnt.type}</span>
              <h2 className="font-display text-2xl font-bold leading-tight mt-2">{selectedEnt.name}</h2>
              <p className="text-sm mt-2" style={{color:'var(--muted)'}}>{selectedEnt.note}</p>
              <p className="text-xs mt-3"><b>Aliases:</b> {selectedEnt.aliases.length?selectedEnt.aliases.join(', '):'—'}</p>
              <h3 className="font-semibold text-sm mt-5">Claims · {ownerClaims.filter(c=>c.subject_id===selectedEnt.id||c.object_id===selectedEnt.id).length}</h3>
              <div className="space-y-2 mt-2">{ownerClaims.filter(c=>c.subject_id===selectedEnt.id||c.object_id===selectedEnt.id).slice(0,8).map(c=> <button key={c.id} onClick={()=>setSelectedClaim(c.id)} className="w-full text-left rounded-xl p-3 text-xs" style={{border:'1px solid var(--line)'}}><span className="flex justify-between gap-2"><span className="font-semibold">{c.fact_text}</span><span className="shrink-0 px-1.5 py-0.5 rounded-full text-white capitalize" style={{background:STATUS_COLOR[c.status],fontSize:10}}>{c.status}</span></span><span className="mono block mt-1" style={{color:'var(--muted)'}}>{c.verb} · {c.event_date} · {c.span_id}</span></button>)}
              {ownerClaims.filter(c=>c.subject_id===selectedEnt.id||c.object_id===selectedEnt.id).length===0 && <p className="text-xs" style={{color:'var(--muted)'}}>No claims for this entity in this owner slice.</p>}</div>
              <div className="mt-5 rounded-xl p-3" style={{background:'var(--surface-2)'}}><p className="text-xs font-bold">TURN_ENTITY PASSPORT</p><p className="text-xs mt-2 leading-relaxed">Passport for {selectedEnt.name} — one passport per turn where this entity appears. Turns carry span ids, not free-floating facts, so the packer can trace every grounded line back to its turn.</p><p className="mono text-[11px] mt-2" style={{color:'var(--muted)'}}>{passportSpans.length ? passportSpans.map(s=>s.replace('span_','turn_')).join(' · ') : 'No turns for this entity.'}<br/>owner: {owner} · entity: {selectedEnt.id}</p></div>
            </div>
          ) : <p className="text-sm mt-3" style={{color:'var(--muted)'}}>Select an entity or claim to inspect it.</p>}
        </div>
        <div className="rounded-2xl p-4 mt-4" style={{background:'var(--surface)',border:'1px solid var(--line)'}}>
          <h3 className="font-semibold text-sm flex items-center gap-2"><Layers size={14}/> Provenance</h3>
          <p className="text-xs mt-2" style={{color:'var(--muted)'}}>Copy a span id to trace a fact back to its turn.</p>
          <input ref={copyRef} value={provenanceValue} onChange={()=>{}} className="w-full mt-3 rounded-lg px-3 py-2 mono text-xs" style={{border:'1px solid var(--line)',background:'var(--surface)'}} aria-label="Span id to copy" readOnly />
          <button onClick={()=>doCopy(provenanceValue)} className="mt-2 w-full py-2 rounded-full text-xs font-semibold border flex items-center justify-center gap-2" style={{borderColor:'var(--line)'}}><Copy size={13}/> Copy span ID</button>
          {copyDone && <p className="text-xs mt-2" style={{color:'var(--accent)'}}>{copyDone}</p>}
          <div className="flex flex-wrap gap-1 mt-3">{ownerClaims.slice(0,6).map(c=> <button key={c.id} onClick={()=>doCopy(c.span_id)} className="mono text-[10px] px-2 py-1 rounded-full border" style={{borderColor:'var(--line)'}}>{c.span_id}</button>)}</div>
        </div>
        <div className="rounded-2xl p-4 mt-4 text-xs leading-relaxed" style={{background:'var(--sidebar)',color:'#EDEFF2'}}>
          <p className="font-semibold flex items-center gap-2"><Clock size={13}/> How to read this tool</p><p className="mt-2 text-white/70">Graph shows what is connected. Claims show what was said. Timelines show when it was true. Evidence shows why it counts. Retrieval mode changes how the packer chooses — not what is true.</p>
        </div>
      </aside>
    </main>
    <footer className="px-5 pb-6 pt-2 flex flex-wrap gap-4 text-[11px]" style={{color:'var(--muted)'}}><span className="flex items-center gap-1"><Activity size={12}/> Hermes Graph Explorer · demo data</span><span>Owner-scoped: {owner} sees only {owner} data.</span><span>Evidence, not counters · Event time, not processing time.</span></footer>
  </div>
  );
}
