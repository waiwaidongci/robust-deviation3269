import {useCallback,useEffect,useState} from 'react';
import {useSearchParams} from 'react-router-dom';
import {Save,PenLine} from 'lucide-react';
import {useResearchData} from '../hooks/useResearch';
import {useAuth} from '../stores/auth';
import {useAction} from '../hooks/useAction';
import {StudySelector} from '../components/StudySelector';
import {MergePanel} from '../components/MergePanel';
import {DataTable} from '../components/common/DataTable';
import {Notice} from '../components/common/Notice';
import {updateWindow} from '../api/windows';
import {getBaselines,getPackages} from '../api/merge';
import {number,time} from '../utils/format';
import {SplitRoles} from '../constants/enums';
import {Baseline,MergePackage} from '../types/domain';

export function Inputs(){
  const s=useResearchData(),{user}=useAuth(),a=useAction(),[params]=useSearchParams(),
    [id,setId]=useState(Number(params.get('study'))||0),
    [role,setRole]=useState('train'),[selected,setSelected]=useState(0),
    [note,setNote]=useState(''),[draft,setDraft]=useState<Record<string,string>>({}),
    [draftSplit,setDraftSplit]=useState<'train'|'cal'|'test'>('train'),
    [baselines,setBaselines]=useState<Baseline[]>([]),[packages,setPackages]=useState<MergePackage[]>([]);
  const study=s.studies.find(x=>x.id===id)||s.studies[0],
    rows=s.windows.filter(w=>w.split===role),
    window=rows.find(w=>w.id===selected)||rows[0];
  const writable=user?.role!=='viewer',decider=user?.role==='admin';

  const loadMerge=useCallback(async(sid:number)=>{
    const [b,p]=await Promise.all([getBaselines(sid),getPackages(sid)]);
    setBaselines(b);setPackages(p);
  },[]);

  useEffect(()=>{if(study)s.loadStudy(study.id)},[study?.id,s.loadStudy]);
  useEffect(()=>{if(study)loadMerge(study.id).catch(()=>{})},[study?.id,loadMerge]);
  useEffect(()=>{
    setNote(window?.note||'');
    setDraftSplit(window?.split||'train');
    setDraft({});
  },[window?.id,window?.note,window?.split]);

  async function afterWrite(){
    if(!study)return;
    await s.loadStudy(study.id);
    await loadMerge(study.id);
    await s.refresh();
  }

  const edited=(code:string)=>{const raw=draft[code];return raw!==undefined&&raw.trim()!==''&&Number(raw)!==window!.values[code]};
  const dirty=window&&(
    note!==window.note
    || draftSplit!==window.split
    || s.features.some(f=>edited(f.code))
  );
  // Long seed floats are hard to scan; show up to six decimals without
  // touching the stored value unless the analyst actually types.
  const precise=(n:number|undefined)=>n===undefined?'':String(Number(n.toFixed(6)));

  return <div className="page">
    <div className="page-title"><div><p className="eyebrow">SOURCES & ROLE BOUNDARY / 02</p><h1>来源与严格拆分<span>四量预定义；训练、校准、测试各司其职。</span></h1></div></div>
    <Notice message={s.error||a.error} onRetry={s.refresh}/>
    <StudySelector studies={s.studies} value={study?.id||0} onChange={setId}/>
    {study&&<div className={'source-note '+(!study.supported?'warning':'')}>
      <b>{study.reason}</b>
      <p>{study.description}</p>
      <small>{study.source} · 合并修订 r{study.merge_revision} · {study.pending_conflicts?study.pending_conflicts+'个待裁决资料包':'无待裁决'}</small>
    </div>}
    <div className="section-heading"><h2>固定四量口径</h2><span>不使用test选择特征</span></div>
    <DataTable head={['次序','特征','单位','窗口口径']}>{s.features.map(x=><tr key={x.id}><td>{x.position+1}</td><td>{x.label}</td><td>{x.unit}</td><td>{x.meaning}</td></tr>)}</DataTable>
    <div className="source-grid">
      <section>
        <div className="section-heading"><h2>独立窗口清单</h2><span>{rows.length}条 / {role}</span></div>
        <div className="mode-switch">{Object.entries(SplitRoles).map(([key,label])=><button className={role===key?'active':''} key={key} onClick={()=>{setRole(key);setSelected(0)}}>{label}</button>)}</div>
        <div className="compact-table"><DataTable head={['窗口',...s.features.map(f=>f.label),'角色']}>
          {rows.map(w=><tr key={w.id} className={window?.id===w.id?'active-row':''} onClick={()=>setSelected(w.id)}>
            <td>{w.code}{w.last_branch!=='main'&&w.last_branch!=='local'?<small className="branch-tag"> {w.last_branch}</small>:null}</td>
            {s.features.map(f=><td key={f.code}>{number(w.values[f.code],3)}</td>)}
            <td>{w.split}</td>
          </tr>)}
        </DataTable></div>
      </section>
      <aside className="window-reading">
        <span className="eyebrow">SELECTED WINDOW</span>
        <h2>{window?.code}</h2>
        <p>{window&&SplitRoles[window.split]}<br/>来源身份 <span className="mono">{window?.origin_uid.slice(0,20)}…</span><br/>修订 r{window?.revision} · 最近分支 {window?.last_branch||'main'}</p>
        {window&&<div className="window-edit">
          <label>窗口批注<textarea aria-label="窗口批注" value={note} disabled={!writable} onChange={e=>setNote(e.target.value)}/></label>
          <label>拆分角色<select aria-label="拆分角色" value={draftSplit} disabled={!writable} onChange={e=>setDraftSplit(e.target.value as 'train'|'cal'|'test')}>
            {Object.entries(SplitRoles).map(([key,label])=><option key={key} value={key}>{key} / {label}</option>)}
          </select></label>
          {s.features.map(f=><label key={f.code}>{f.label} / {f.unit}
            <input aria-label={'数值 '+f.label} type="number" step="any" disabled={!writable}
              value={draft[f.code]??precise(window.values[f.code])}
              onChange={e=>setDraft({...draft,[f.code]:e.target.value})}/>
          </label>)}
          {/* 编号是来源身份，不可在本地或分支改名 */}
          <small>数值、拆分与批注可改；窗口编号是来源身份，不随编辑变化。</small>
        </div>}
        {writable&&<button className="outline" disabled={a.busy||!window||!dirty} onClick={()=>a.execute(async()=>{
          const values:Record<string,number>={};
          for(const f of s.features){
            const raw=draft[f.code];
            if(raw!==undefined&&raw!==''&&Number(raw)!==window!.values[f.code]){
              if(!Number.isFinite(Number(raw)))throw new Error(f.label+'须为有限数值');
              values[f.code]=Number(raw);
            }
          }
          await updateWindow(window!.id,{note,split:draftSplit,...(Object.keys(values).length?{values}:{})});
          await afterWrite();
        })}><Save size={14}/>保存来源修改</button>}
        <small>修改进入后续快照与资料包；历史运行保持冻结值。</small>
      </aside>
    </div>
    <div className="section-heading"><h2>共同基线</h2><span>资料包以此为依据；每次合入生成新修订</span></div>
    <div className="compact-table"><DataTable head={['修订','分支','窗口数','基线指纹','建立时间']}>
      {baselines.map(b=><tr key={b.revision}><td>r{b.revision}</td><td>{b.branch}</td><td>{b.window_count}</td><td className="mono">{b.base_hash.slice(0,16)}…</td><td>{time(b.created_at)}</td></tr>)}
    </DataTable></div>
    {!baselines.length&&<p className="empty">尚无基线快照。<PenLine size={13}/> 首次导出资料包时会建立。</p>}
    {study&&<MergePanel studyId={study.id} packages={packages} signedIn={!!user} isAdmin={decider} onChanged={afterWrite}/>}
  </div>;
}
