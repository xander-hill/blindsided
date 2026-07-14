#!/bin/bash

# 1. Create the new dedicated namespace
kubectl create namespace blindsided --dry-run=client -o yaml | kubectl apply -f -
kubectl config set-context --current --namespace=blindsided

echo "🚀 Starting BlindSided (Fog of War) Deployment..."

# 2. Apply the Controller (The Cluster Brain)
echo "🧠 Deploying Controller..."
kubectl apply -f deploy/kubernetes/controller.yaml

# Wait a few seconds for the controller service to be ready
sleep 5

# 3. Apply the Storage Layer (The Judge / Vault)
echo "🏛️  Deploying StorageReplicaService StatefulSet..."
kubectl apply -f deploy/kubernetes/storage.yaml

# 4. Apply the Service Layer (The Fog API)
echo "🌐 Deploying Scalable Service Nodes..."
kubectl apply -f deploy/kubernetes/service.yaml

# 5. Apply the gRPC-Web Gateway
echo "🚪 Deploying Envoy Gateway..."
kubectl apply -f deploy/envoy/kubernetes.yaml

echo "------------------------------------------------"
echo "✅ All components applied to namespace: blindsided"
echo "Run 'kubectl get pods' to check status."
echo "------------------------------------------------"
