"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { api } from "../lib/api";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [connected, setConnected] = useState<boolean | null>(null);

  useEffect(() => {
    let active = true;
    api<{ status: string }>("/api/health")
      .then(() => active && setConnected(true))
      .catch(() => active && setConnected(false));
    return () => { active = false; };
  }, []);

  return (
    <div className="parameter-app">
      <header className="parameter-topbar">
        <div className="parameter-brand">
          <span className="parameter-brand-mark">YH</span>
          <span><b>永恒科技 · 冷二联工艺规划</b></span>
        </div>
        <nav className="parameter-navigation" aria-label="主要功能">
          <Link href="/" aria-current={pathname === "/" ? "page" : undefined}>道次推荐</Link>
          <Link href="/capture" aria-current={pathname === "/capture" ? "page" : undefined}>拍照记录</Link>
        </nav>
        {connected === false ? <div className="parameter-model-state">
          <span className="connection-dot connection-offline" />
          <span><b>服务异常</b></span>
        </div> : null}
      </header>
      <main className="parameter-main">{children}</main>
    </div>
  );
}
