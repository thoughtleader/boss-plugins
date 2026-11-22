'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), os=require('node:os'), path=require('node:path');
const {create}=require('../lib/processes');
// lib/processes.js reads /proc (Linux). The two tests that register a real process need it; off Linux they skip, with this reason printed.
const NO_PROC=fs.existsSync('/proc/self/stat')?false:'no /proc here (macOS): lib/processes.js reads /proc, so registering a real process is Linux only';
delete process.env.BOSS_PROGRESS_DIR; // the hook children inherit process.env: a direct run behaves like scripts/test-all.sh (no snapshot reaches the live registry)
function fixture(t) {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'orch-process-test-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const proc=path.join(dir,'proc'); fs.mkdirSync(path.join(proc,'sys/kernel/random'),{recursive:true});fs.writeFileSync(path.join(proc,'sys/kernel/random/boot_id'),'boot-a');
 function process(pid,start='100',ppid=1) {const s=Array(22).fill('0');s[0]='S';s[1]=String(ppid);s[19]=start;fs.mkdirSync(path.join(proc,String(pid)),{recursive:true});fs.writeFileSync(path.join(proc,String(pid),'stat'),`${pid} (name with ) spaces) ${s.join(' ')}`);fs.writeFileSync(path.join(proc,String(pid),'status'),'VmRSS: 2048 kB\n');}
 return {dir,proc,process,api:create({dir,proc}),input:{pid:10,attempt:'test-run#1.1',session:'test-session',purpose:'fixture server',keep:'no'}};
}
test('reports surviving root and descendants without changing process files',t=>{const f=fixture(t);f.process(10);f.process(11,'101',10);f.api.register(f.input);const before=fs.readFileSync(path.join(f.proc,'10/stat'),'utf8');const r=f.api.report({attempt:f.input.attempt},true);assert.equal(r.entries[0].status,'running');assert.equal(r.entries[0].descendants[0].pid,11);assert.equal(fs.readFileSync(path.join(f.proc,'10/stat'),'utf8'),before);assert.equal(fs.readdirSync(path.join(f.dir,'process-ownership/reports')).length,1);});
test('PID reuse and reboot cannot claim a new process',t=>{const f=fixture(t);f.process(10);f.api.register(f.input);f.process(10,'200');assert.equal(f.api.report({session:'test-session'}).entries[0].status,'pid-reused');fs.writeFileSync(path.join(f.proc,'sys/kernel/random/boot_id'),'boot-b');assert.equal(f.api.report({session:'test-session'}).entries[0].status,'previous-boot');});
test('exited process is distinguished from surviving intentional server',t=>{const f=fixture(t);f.process(10);f.api.register({...f.input,keep:'yes'});assert.equal(f.api.report({session:'test-session'}).entries[0].disposition,'retain as requested');fs.rmSync(path.join(f.proc,'10'),{recursive:true});assert.equal(f.api.report({session:'test-session'}).entries[0].status,'exited');});
test('owner filtering, idempotent registration, and missing registry are safe',t=>{const f=fixture(t);assert.equal(f.api.report({session:'test-session'},true).tracked,0);assert.equal(fs.existsSync(path.join(f.dir,'process-ownership')),false);f.process(10);f.api.register(f.input);f.api.register(f.input);assert.equal(f.api.report({session:'test-session'}).tracked,1);assert.equal(f.api.report({session:'another-session'}).tracked,0);});
test('rejects invalid registrations and preserves unknown status on permission failure',t=>{const f=fixture(t);assert.throws(()=>f.api.register(f.input),/not live/);f.process(10);assert.throws(()=>f.api.register({...f.input,keep:'maybe'}));f.api.register(f.input);fs.unlinkSync(path.join(f.proc,'10/status'));fs.mkdirSync(path.join(f.proc,'10/status'));assert.throws(()=>f.api.report({session:'test-session'}));});
test('Stop hook writes an observational report and remains silent',{skip:NO_PROC},t=>{
 const {spawnSync}=require('node:child_process');const f=fixture(t);
 fs.mkdirSync(path.join(f.dir,'leases'),{recursive:true});fs.mkdirSync(path.join(f.dir,'runs/test-run'),{recursive:true});
 fs.writeFileSync(path.join(f.dir,'leases/test-run.json'),JSON.stringify({v:1,run:'test-run',session:'test-session',acquired:new Date().toISOString(),renewed:new Date().toISOString(),expires:new Date(Date.now()+3600000).toISOString(),ttl_sec:3600}));
 // Register this test process against real /proc; the hook must leave it alive.
 const real=create({dir:f.dir});real.register({...f.input,pid:process.pid});
 const r=spawnSync(process.execPath,[path.resolve(__dirname,'../hooks/orch-attempts.js'),'stop'],{input:JSON.stringify({session_id:'test-session'}),encoding:'utf8',env:{...process.env,ORCH_DIR:f.dir,ORCH_RUNS_DIR:path.join(f.dir,'runs'),ORCH_PROJECTS_DIR:path.join(f.dir,'projects')},timeout:10000});
 assert.equal(r.status,0);assert.equal(r.stdout,'');assert.equal(r.stderr,'');
 const files=fs.readdirSync(path.join(f.dir,'process-ownership/reports'));assert.equal(files.length,1);
 const saved=JSON.parse(fs.readFileSync(path.join(f.dir,'process-ownership/reports',files[0]),'utf8'));assert.equal(saved.entries[0].status,'running');
 const record=fs.readdirSync(path.join(f.dir,'process-ownership/records'))[0];fs.writeFileSync(path.join(f.dir,'process-ownership/records',record),'broken JSON');
 const failed=spawnSync(process.execPath,[path.resolve(__dirname,'../hooks/orch-attempts.js'),'stop'],{input:JSON.stringify({session_id:'test-session'}),encoding:'utf8',env:{...process.env,ORCH_DIR:f.dir,ORCH_RUNS_DIR:path.join(f.dir,'runs'),ORCH_PROJECTS_DIR:path.join(f.dir,'projects')},timeout:10000});
 assert.equal(failed.status,0);assert.equal(failed.stdout,'');assert.equal(failed.stderr,'');assert.match(fs.readFileSync(path.join(f.dir,'errors.log'),'utf8'),/process report:/);
});
test('the CLI takes its dir from the data dir (BOSS_DATA) and exports main(argv)',{skip:NO_PROC},t=>{
 const {spawnSync}=require('node:child_process');const dir=fs.mkdtempSync(path.join(os.tmpdir(),'orch-process-data-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const env={...process.env,BOSS_DATA:dir};for(const k of ['ORCH_DIR','ORCH_RUNS_DIR','CLAUDE_CONFIG_DIR'])delete env[k];
 const cli=path.resolve(__dirname,'../lib/processes.js');
 const reg=spawnSync(process.execPath,[cli,'register','--pid',String(process.pid),'--attempt','test-run#1.1','--session','s1','--purpose','cli test','--keep','no'],{encoding:'utf8',env,timeout:10000});
 assert.equal(reg.status,0,reg.stderr);assert.equal(fs.readdirSync(path.join(dir,'process-ownership/records')).length,1);
 const rep=spawnSync(process.execPath,[cli,'report','--session','s1'],{encoding:'utf8',env,timeout:10000});
 assert.equal(rep.status,0);assert.equal(JSON.parse(rep.stdout).entries[0].status,'running');
 const bad=spawnSync(process.execPath,[cli,'bogus'],{encoding:'utf8',env,timeout:10000});
 assert.equal(bad.status,1);assert.match(bad.stderr,/^processes: usage: processes\.js register/);
 const mod=require('../lib/processes');assert.equal(typeof mod.main,'function');assert.equal(typeof mod.create,'function');
});
test('requiring the module has no side effects and main() returns a status instead of exiting',t=>{
 const prev=process.exitCode;const err=console.error;const seen=[];console.error=(...a)=>seen.push(a.join(' '));
 try{const {main}=require('../lib/processes');assert.equal(main(['bogus']),1);assert.match(seen.join('\n'),/processes: usage/);assert.equal(process.exitCode,prev);}finally{console.error=err;}
});
