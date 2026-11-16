import {useState} from 'react';
import {Download,Upload,Scale,GitBranch,History} from 'lucide-react';
import {WindowPackage,MergePackage} from '../types/domain';
import {exportPackage,importPackage,resolvePackage} from '../api/merge';
import {Notice} from './common/Notice';
import {time} from '../utils/format';

const FIELD_LABEL:Record<string,string>={split:'角色',note:'批注'};
function fieldLabel(field:string){
  if(FIELD_LABEL[field])return FIELD_LABEL[field];
  if(field.startsWith('values.'))return '数值 · '+field.slice(7);
  return field;
}
function show(value:unknown){
  if(typeof value==='number')return String(value);
  if(typeof value==='string')return value||'（空）';
  return JSON.stringify(value);
}

export interface MergePanelProps{
  studyId:number;
  packages:MergePackage[];
  signedIn:boolean;
  isAdmin:boolean;
  onChanged:()=>Promise<void>;
}

export function MergePanel({studyId,packages,signedIn,isAdmin,onChanged}:MergePanelProps){
  const [branch,setBranch]=useState('offline-a');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const [notes,setNotes]=useState<Record<number,string>>({});
  const pending=packages.filter(p=>p.status==='conflict');

  function saveFile(pkg:WindowPackage){
    const blob=new Blob([JSON.stringify(pkg,null,2)],{type:'application/json'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download=`windows-${pkg.study_code}-${pkg.branch}.json`;a.click();URL.revokeObjectURL(a.href);
  }
  async function act(action:()=>Promise<string>){
    setBusy(true);setError('');setMessage('');
    try{const text=await action();setMessage(text);await onChanged();}
    catch(e){setError((e as Error).message)}
    finally{setBusy(false)}
  }
  function collect(file:File|undefined){
    if(!file){return}
    act(async()=>{
      let parsed:WindowPackage;
      try{parsed=JSON.parse(await file.text()) as WindowPackage}
      catch{throw new Error('资料包不是有效JSON')}
      const result=await importPackage(studyId,parsed);
      if(result.replayed)return `资料包 ${result.package_uid.slice(0,18)}… 已回收过，未改写任何窗口`;
      if(result.status==='merged')return `资料包已合入：${result.summary.changed_rows??0}行变更，基线修订${result.base_revision}→已生效`;
      if(result.status==='ignored')return '资料包与当前一致，无可应用修改';
      return `检测到${result.conflicts.length}处同字段异改；整包待裁决，研究未改动`;
    });
  }

  return <section className="merge-panel">
    <div className="section-heading"><h2>来源资料包往返与三方合并</h2><span>来源身份匹配 · 字段级三方合并 · 不整行覆盖</span></div>
    <p className="caption">导出的是同一批窗口加上本库的共同基线；离线可改数值、拆分与批注。回收时按基线比对：双方不冲突的修改自动保留；同一字段两方改法不同则整包待裁决，选边后原子生效。</p>
    {!signedIn?<p className="role-note">阅读者可核对资料包，导入与裁决需要研究员或负责人权限。</p>:<div className="merge-controls">
      <label>分库标识<input aria-label="分库标识" value={branch} maxLength={64} onChange={e=>setBranch(e.target.value)} placeholder="例如 offline-lab-b" /></label>
      <div className="merge-buttons">
        {branch.trim()&&<button className="outline" disabled={busy} onClick={()=>act(async()=>{const pkg=await exportPackage(studyId,branch.trim());saveFile(pkg);return `已导出资料包 ${pkg.package_uid.slice(0,18)}…（基线修订${pkg.base_revision}）`})}><Download size={14}/>下载资料包</button>}
        <label className="file-pick">回收资料包<input aria-label="回收资料包" type="file" accept="application/json,.json" disabled={busy} onChange={e=>{collect(e.target.files?.[0]);e.target.value=''}}/></label>
      </div>
    </div>}
    <Notice message={error}/>
    {message&&<p className="merge-message" role="status">{message}</p>}
    {pending.length>0&&<div className="merge-pending">
      <div className="section-top"><span className="eyebrow">CONFLICT / 待裁决</span><b className="warning">整包挂起 · 研究不可创建</b></div>
      {pending.map(pkg=><article key={pkg.id} className="conflict-card">
        <header><div><GitBranch size={14}/><b>{pkg.branch}</b><small>{pkg.package_uid.slice(0,22)}…</small></div><span>基线修订 {pkg.base_revision} · {pkg.conflicts.length}处异改</span></header>
        <table><thead><tr><th>窗口</th><th>字段</th><th>共同基线</th><th>本库</th><th>该分库</th></tr></thead><tbody>
          {pkg.conflicts.map((c,i)=><tr key={i}><td>{c.code}</td><td>{fieldLabel(c.field)}</td><td>{show(c.base)}</td><td>{show(c.ours)}</td><td>{show(c.theirs)}</td></tr>)}
        </tbody></table>
        {isAdmin?<div className="approval-form">
          <label>裁决说明<textarea aria-label={'裁决说明 '+pkg.id} value={notes[pkg.id]||''} onChange={e=>setNotes({...notes,[pkg.id]:e.target.value})} placeholder="说明为何保留本库或采用该分库，便于后续追查。"/></label>
          <div>
            <button className="primary" disabled={busy||(notes[pkg.id]||'').length<8} onClick={()=>act(async()=>{const r=await resolvePackage(studyId,pkg.id,'theirs',notes[pkg.id]);return `已采用该分库，整包原子生效（${r.summary.changed_rows??0}行变更）`})}><Scale size={14}/>采用该分库</button>
            <button className="outline" disabled={busy||(notes[pkg.id]||'').length<8} onClick={()=>act(async()=>{await resolvePackage(studyId,pkg.id,'ours',notes[pkg.id]);return '已保留本库，资料包退回；未合入任何修改'})}>保留本库</button>
          </div>
        </div>:<p className="role-note">裁决需要材料负责人权限。</p>}
      </article>)}
    </div>}
    <details className="merge-history">
      <summary><History size={14}/>资料包回收记录（{packages.length}）</summary>
      <div className="table-wrap"><table><thead><tr><th>回收时间</th><th>分库</th><th>基线</th><th>结果</th><th>变更</th><th>包编号</th></tr></thead><tbody>
        {packages.map(p=><tr key={p.id}>
          <td>{time(p.created_at)}</td><td>{p.branch}</td><td>r{p.base_revision}</td>
          <td className={p.status==='conflict'?'warning':p.status==='merged'?'supported':''}>{statusText(p)}</td>
          <td>{p.summary.changed_rows??0}行 / {p.summary.conflict_count??0}冲突</td>
          <td className="mono">{p.package_uid.slice(0,20)}…</td>
        </tr>)}
      </tbody></table></div>
      {!packages.length&&<p className="empty">尚无资料包回收记录。导出后可离线修改，再从此处回收。</p>}
    </details>
  </section>;
}

function statusText(p:MergePackage){
  if(p.status==='merged')return p.resolution==='theirs'?'裁决合入':'自动合入';
  if(p.status==='conflict')return '待裁决';
  if(p.status==='ignored')return '无变更';
  return p.resolution==='stale'?'基线已变 / 退回':'保留本库 / 退回';
}
