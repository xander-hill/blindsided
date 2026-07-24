import { expect, test } from '@playwright/test'

test('creates an auction and preserves the demo through backup and primary failure', async ({ page, request }) => {
  const controlUrl = process.env.VITE_DEMO_CONTROL_URL ?? 'http://127.0.0.1:8090'
  const probe = await request.get(`${controlUrl}/demo/status`).catch(() => null)
  test.skip(!probe?.ok(), 'Requires the Compose cluster and localhost demo-control adapter')

  await page.goto('/')
  const initialEpoch = Number(await page.getByTestId('epoch').textContent())
  await page.getByRole('button', { name: 'Create demo auction' }).click()
  await expect(page.getByTestId('auction-state')).toHaveText('OPEN')
  await expect(page.getByTestId('auction-version')).not.toHaveText('—')
  await expect(page.getByTestId('watch-status')).toHaveText('connected')

  await page.getByRole('button', { name: 'Start simulated bidders' }).click()
  await expect.poll(async () => Number(await page.getByTestId('bidder-count').textContent()), {
    timeout: 30_000,
  }).toBeGreaterThan(0)
  const versionBeforeBid = Number(await page.getByTestId('auction-version').textContent())
  await page.getByLabel('Your bid').fill('1400')
  await page.getByRole('button', { name: 'Place sealed bid' }).click()
  await expect(page.getByText('$1,400.00')).toBeVisible()
  await expect.poll(async () => Number(await page.getByTestId('auction-version').textContent()))
    .toBeGreaterThan(versionBeforeBid)

  await page.getByRole('button', { name: 'Fail backup' }).click()
  await expect(page.getByTestId('cluster-state')).toHaveText(/REPROTECTING|READY/)
  await expect(page.getByTestId('protection-status')).toHaveText('Synchronous', { timeout: 90_000 })
  await expect(page.getByTestId('synchronous-backup')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Fail primary' })).toBeEnabled({ timeout: 90_000 })

  await page.getByRole('button', { name: 'Fail primary' }).click()
  await expect(page.getByTestId('cluster-state')).toHaveText(/FAILING_OVER|UNAVAILABLE|READY/)
  await expect.poll(async () => Number(await page.getByTestId('epoch').textContent()), {
    timeout: 90_000,
  }).toBeGreaterThan(initialEpoch)
  await expect(page.getByTestId('cluster-state')).toHaveText('READY', { timeout: 90_000 })
  await expect(page.getByTestId('protection-status')).toHaveText('Synchronous')

  await page.getByRole('button', { name: 'Stop simulated bidders' }).click().catch(() => undefined)
  await page.getByRole('button', { name: 'Reveal auction' }).click()
  await expect(page.getByTestId('auction-state')).toHaveText('REVEALED')
  await expect(page.getByTestId('final-outcome')).toBeVisible()
  await expect(page.getByRole('button', { name: /sealed bid/ })).toHaveCount(0)
})
