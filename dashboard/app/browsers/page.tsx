"use client";
import { useLocale } from 'next-intl';
import { BrowserPreferences } from '../../components/BrowserPreferences';

export default function BrowsersPage(){
  const zh=useLocale().startsWith('zh');
  return <div className="mx-auto max-w-4xl p-5 sm:p-8"><header className="mb-7"><h1 className="text-xl font-semibold">{zh?'浏览器设置':'Browser settings'}</h1><p className="mt-2 text-sm text-[color:var(--text-muted)]">{zh?'一个工作浏览器，随任务自动启动。':'One work browser, ready for every task.'}</p></header><BrowserPreferences/></div>;
}
