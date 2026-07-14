import { GrpcWebFetchTransport } from "@protobuf-ts/grpcweb-transport";
import { AuctionServiceClient } from "../proto/blindsided.client";

// Point this to your Envoy Proxy (usually 8080)
const transport = new GrpcWebFetchTransport({
  baseUrl: "http://localhost:8080",
});

export const auctionClient = new AuctionServiceClient(transport);
