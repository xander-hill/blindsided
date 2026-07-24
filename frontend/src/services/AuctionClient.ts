import { GrpcWebFetchTransport } from '@protobuf-ts/grpcweb-transport'
import { config } from '../app/config'
import { AuctionServiceClient } from '../proto/blindsided.client'
import type { AuctionUpdate } from '../proto/blindsided'

const transport = new GrpcWebFetchTransport({ baseUrl: config.grpcWebUrl })
export const rawAuctionClient = new AuctionServiceClient(transport)

export const requestId = (operation: string) =>
  `${operation}:${crypto.randomUUID()}`

export async function createDemoAuction() {
  const endsAtSeconds = BigInt(Math.floor(Date.now() / 1000) + 60 * 20)
  const call = rawAuctionClient.createAuction({
    sellerId: config.demoSellerId,
    title: 'Replica-Failure Field Test',
    category: 'distributed-systems',
    description: 'A single blind auction driven through backup loss and primary promotion.',
    reservePrice: 750,
    endsAt: { seconds: endsAtSeconds, nanos: 0 },
    requestId: requestId('create'),
  })
  const response = await call.response
  if (!response.ok) throw new Error(response.message || 'Auction creation failed')
  return response.auctionId
}

export async function getAuction(auctionId: string, bidderId = config.demoBidderId) {
  const response = await rawAuctionClient.getAuction({ auctionId, bidderId }).response
  if (!response.ok || !response.auction) throw new Error(response.message || 'Auction unavailable')
  return response
}

export async function placeBid(auctionId: string, bidderId: string, amount: number, version: number) {
  const response = await rawAuctionClient.placeBid({
    auctionId, bidderId, amount, expectedVersion: version, requestId: requestId('bid'),
  }).response
  if (!response.success) throw new Error(response.message || 'Bid was rejected')
}

export async function withdrawBid(auctionId: string, bidderId: string, version: number) {
  const response = await rawAuctionClient.withdrawBid({
    auctionId, bidderId, expectedVersion: version, requestId: requestId('withdraw'),
  }).response
  if (!response.success) throw new Error(response.message || 'Withdrawal was rejected')
}

export async function revealAuction(auctionId: string, version: number) {
  const response = await rawAuctionClient.revealAuction({
    auctionId, sellerId: config.demoSellerId, expectedVersion: version, requestId: requestId('reveal'),
  }).response
  if (!response.ok) throw new Error(response.message || 'Reveal was rejected')
}

export function watchAuction(auctionId: string, signal: AbortSignal): AsyncIterable<AuctionUpdate> {
  return rawAuctionClient.watchAuction(
    { auctionId, userId: config.demoBidderId },
    { abort: signal },
  ).responses
}
