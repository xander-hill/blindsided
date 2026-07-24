export interface AppConfig {
  grpcWebUrl: string
  demoControlUrl: string
  grafanaUrl: string
  demoBidderId: string
  demoSellerId: string
}

function required(name: keyof ImportMetaEnv): string {
  const value = import.meta.env[name]?.trim()
  if (!value) throw new Error(`Missing ${name}. Copy frontend/.env.example to .env.local.`)
  return value.replace(/\/$/, '')
}

export const config: AppConfig = {
  grpcWebUrl: required('VITE_GRPC_WEB_URL'),
  demoControlUrl: required('VITE_DEMO_CONTROL_URL'),
  grafanaUrl: required('VITE_GRAFANA_URL'),
  demoBidderId: 'demo-human',
  demoSellerId: 'demo-seller',
}
