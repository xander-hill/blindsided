/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_GRPC_WEB_URL: string
  readonly VITE_DEMO_CONTROL_URL: string
  readonly VITE_GRAFANA_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
