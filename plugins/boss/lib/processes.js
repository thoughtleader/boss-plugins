#!/usr/bin/env node
'use strict';
// Observation only. Never signals, launches, waits for, or changes a tracked process.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const paths = require('./paths.js');
const digest = x => crypto.createHash('sha256').update(x).digest('hex');
function create({dir = paths.dataDir(), proc = '/proc'} = {}) {
  const root = path.join(dir, 'process-ownership');
  const boot = () => fs.readFileSync(path.join(proc, 'sys/kernel/random/boot_id'), 'utf8').trim();
  function read(pid) {
    try {
      const raw = fs.readFileSync(path.join(proc, String(pid), 'stat'), 'utf8');
      const s = raw.slice(raw.lastIndexOf(')') + 2).trim().split(/\s+/);
      const status = fs.readFileSync(path.join(proc, String(pid), 'status'), 'utf8');
      return {pid: Number(pid), start: s[19], ppid: Number(s[1]), state: s[0], rssKiB: Number((/^VmRSS:\s+(\d+)/m.exec(status) || [0,0])[1])};
    } catch (e) { if (e.code === 'ENOENT' || e.code === 'ESRCH') return null; throw e; }
  }
  function atomic(file, value) {
    fs.mkdirSync(path.dirname(file), {recursive:true, mode:0o700});
    const tmp = file + '.' + crypto.randomUUID() + '.tmp';
    try { fs.writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n', {mode:0o600}); fs.renameSync(tmp,file); }
    finally { try { fs.unlinkSync(tmp); } catch {} }
  }
  function register({pid, attempt, session, purpose, keep}) {
    if (!/^\d+$/.test(String(pid)) || Number(pid)<1) throw Error('positive PID required');
    if (typeof attempt !== 'string' || !/^[a-zA-Z0-9][\w-]*#[0-9]+[a-zA-Z]?\.[1-9][0-9]*$/.test(attempt)) throw Error('valid attempt required');
    if (!session || !purpose || purpose.length > 500 || !['yes','no'].includes(keep)) throw Error('session, purpose (max 500 chars), and keep yes|no required');
    const live=read(pid); if (!live || live.state==='Z') throw Error('PID is not live');
    const record={v:1, boot:boot(), ...live, attempt, session, purpose, keep:keep==='yes', registered:new Date().toISOString()};
    const key=digest(record.boot+':'+live.pid+':'+live.start+':'+attempt);
    atomic(path.join(root,'records',key+'.json'),record); return record;
  }
  function report({attempt, session} = {}, persist=false) {
    if (!attempt && !session) throw Error('attempt or session required');
    let names; try { names=fs.readdirSync(path.join(root,'records')); } catch(e) { if(e.code==='ENOENT') return {tracked:0, entries:[]}; throw e; }
    const records=names.filter(n=>n.endsWith('.json')).map(n=>JSON.parse(fs.readFileSync(path.join(root,'records',n),'utf8'))).filter(r=>(!attempt||r.attempt===attempt)&&(!session||r.session===session));
    const currentBoot=boot(), all=new Map();
    if(records.length) for(const n of fs.readdirSync(proc).filter(n=>/^\d+$/.test(n))) { const r=read(n); if(r) all.set(r.pid,r); }
    const entries=records.map(r=>{
      const live=all.get(r.pid), same=r.boot===currentBoot&&live&&live.start===r.start;
      const status=r.boot!==currentBoot?'previous-boot':!live?'exited':!same?'pid-reused':live.state==='Z'?'zombie':'running';
      const descendants=[]; const seen=new Set([r.pid]);
      if(same && status==='running') { let changed=true; while(changed) { changed=false; for(const x of all.values()) if(!seen.has(x.pid)&&seen.has(x.ppid)) { seen.add(x.pid); descendants.push(x); changed=true; } } }
      return {...r,status,rssKiB:same?live.rssKiB:0,descendants,disposition:status==='running'?(r.keep?'retain as requested':'review surviving process; no automatic cleanup'):'no action'};
    });
    const result={at:new Date().toISOString(),attempt:attempt||null,session:session||null,tracked:entries.length,entries,scope:'Explicit registrations only; descendants observed while root is live. Unregistered or reparented descendants may be absent. Running does not mean unused.'};
    if(persist && records.length) atomic(path.join(root,'reports',digest(JSON.stringify({attempt,session}))+'.json'),result);
    return result;
  }
  return {register, report};
}
function main(argv) {
  try {
    const [cmd,...args]=argv, opts={};
    for(let i=0;i<args.length;i+=2) { if(!/^--[a-z]+$/.test(args[i])||args[i+1]===undefined) throw Error('use --key value'); opts[args[i].slice(2)]=args[i+1]; }
    const api=create(); let result;
    if(cmd==='register') result=api.register({...opts,session:opts.session||process.env.WFP_SID});
    else if(cmd==='report') result=api.report(opts);
    else throw Error('usage: processes.js register --pid PID --attempt RUN#ROW.N --session SID --purpose TEXT --keep yes|no; or report --attempt ID / --session SID');
    console.log(JSON.stringify(result,null,2));
    return 0;
  } catch(e) { console.error('processes: '+e.message); return 1; }
}
module.exports={create, main};
if(require.main===module) process.exitCode=main(process.argv.slice(2));
