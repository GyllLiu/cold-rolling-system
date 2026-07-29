"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { ModelStatus } from "../lib/types";

export function AppShell({ children }: { children: React.ReactNode }) {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [model, setModel] = useState<ModelStatus | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([api<{ status: string }>("/api/health"), api<ModelStatus>("/api/model/status")])
      .then(([, modelStatus]) => {
        if (active) {
          setConnected(true);
          setModel(modelStatus);
        }
      })
      .catch(() => active && setConnected(false));
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="parameter-app">
      <header className="parameter-topbar">
        <div className="parameter-brand">
          <span className="parameter-brand-mark">YH</span>
          <span>
            <b>冷二联首设参数</b>
            <small>COLD TANDEM · DIRECT PREDICTION</small>
          </span>
        </div>
        <div className="parameter-model-state">
          <span className={`connection-dot connection-${connected === null ? "checking" : connected ? "online" : "offline"}`} />
          <span>
            <b>{connected === null ? "模型检查中" : connected ? "模型在线" : "模型离线"}</b>
            <small>{model?.model_version ?? "DIRECT V0.3"}</small>
          </span>
        </div>
      </header>
      <main className="parameter-main">{children}</main>
      <footer className="parameter-footer">
        只读首设建议 · 不写入PLC · 0719封存验证
      </footer>
    </div>
  );
}
