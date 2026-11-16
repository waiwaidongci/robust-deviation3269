import {request} from './client';import {Baseline,MergePackage,WindowPackage} from '../types/domain';
export const getBaselines=(id:number)=>request<Baseline[]>(`/studies/${id}/baselines`);
export const getPackages=(id:number)=>request<MergePackage[]>(`/studies/${id}/packages`);
export const exportPackage=async(id:number,branch:string,baseRevision?:number):Promise<WindowPackage>=>{const query=new URLSearchParams({branch});if(baseRevision)query.set('base_revision',String(baseRevision));const response=await fetch(`/api/studies/${id}/packages/export?${query}`,{headers:{Authorization:'Bearer '+localStorage.getItem('deviation-token')}});if(!response.ok){const body=await response.json().catch(()=>({detail:'导出失败'}));throw new Error(typeof body.detail==='string'?body.detail:'导出失败');}return response.json();};
export const importPackage=(id:number,pkg:WindowPackage)=>request<MergePackage>(`/studies/${id}/packages`,{method:'POST',body:JSON.stringify(pkg)});
export const resolvePackage=(id:number,pid:number,resolution:'ours'|'theirs',note:string)=>request<MergePackage>(`/studies/${id}/packages/${pid}/resolve`,{method:'POST',body:JSON.stringify({resolution,note})});
