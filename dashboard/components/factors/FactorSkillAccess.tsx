"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { callApi } from "../../lib/clientApi";
import { ErrorBanner } from "../Page";
import styles from "./Factors.module.css";

type Catalog = {ok:boolean;skills:{id:string;enabled:boolean}[];enabled_revision:string;error?:string};

/** Existing allow-lists are preserved; enabling the new skill is explicit. */
export function FactorSkillAccess(){
  const zh=useLocale().startsWith("zh"),[catalog,setCatalog]=useState<Catalog|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const load=async()=>{
    const result=await callApi<Catalog>("/skills/catalog?scope=all&query=factor_library&limit=200");
    if(!result.ok)throw new Error(result.error||"Skill catalog unavailable");
    setCatalog(result);return result;
  };
  useEffect(()=>{void load().catch(e=>setError(String(e.message||e)));},[]);
  const skill=catalog?.skills.find(item=>item.id==="factor_library");
  if(skill?.enabled)return null;
  if(!catalog&&!error)return null;
  return <div className={`${styles.source} mt-4`}><div><p className="text-sm">{i18nCopy(zh, "copy.components_factors_FactorSkillAccess.001")}</p><p className={styles.muted}>{skill ? (i18nCopy(zh, "copy.components_factors_FactorSkillAccess.002")) : (i18nCopy(zh, "copy.components_factors_FactorSkillAccess.003"))}</p>{error&&<ErrorBanner error={error}/>}</div>
    {skill&&!skill.enabled&&<button className="btn btn-secondary" disabled={busy} onClick={async()=>{setBusy(true);setError("");try{const latest=await load();const result=await callApi<{ok:boolean;error?:string}>("/skills/manage",{method:"POST",body:{action:"enable",skill_id:"factor_library",scope:"builtin",revision:latest.enabled_revision,summary:"Enable factor research from the factor library page"}});if(!result.ok)throw new Error(result.error||"Enable failed");await load();}catch(e){setError(String(e));}finally{setBusy(false);}}}>{busy?(i18nCopy(zh, "copy.components_factors_FactorSkillAccess.004")):(i18nCopy(zh, "copy.components_factors_FactorSkillAccess.005"))}</button>}
  </div>;
}
